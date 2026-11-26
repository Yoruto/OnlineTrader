"""Market Scenario Runner: deterministic replay, event-triggered faults, and strict verdicting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import time

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.domain.values import decimal_text
from qa.e2e.oracle import OracleViolation
from qa.e2e.scenario import StepOutcome, TradingScenario
from qa.market_data.schema import ns_to_utc_iso
from qa.scenario_engine.channel import MarketChannel
from qa.scenario_engine.matcher import OrderMatcher
from qa.scenario_engine.models import (
    ENGINE_VERSION,
    FaultRule,
    ScenarioConfig,
    ScenarioResult,
    TriggerKind,
    TriggerRecord,
    Verdict,
)
from qa.scenario_engine.replay_source import RecordedReplaySource
from qa.scenario_engine.synthesizer import MarketSynthesizer


def _iso_to_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000_000_000)


def _git_commit_sha(repo_root: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
            timeout=3.0,
        )
        return out.decode("utf-8").strip() or None
    except Exception:
        return None


def build_default_profile(
    contracts: tuple[str, ...],
    offsets_by_contract: dict[str, dict[str, int]],
    *,
    max_quote_age_seconds: int = 30,
    order_volume: int = 1,
) -> dict[str, object]:
    settings = load_confirmed_baseline()[0].to_settings()
    template = settings["symbols"][0]
    settings["symbols"] = [
        template | {
            "code": code,
            "open_offset": offsets_by_contract[code]["open_offset"],
            "stop_offset": offsets_by_contract[code]["stop_offset"],
            "profit_offset": offsets_by_contract[code]["profit_offset"],
            "order_volume": order_volume,
        }
        for code in contracts
    ]
    settings["trade"].update(
        continuation_entry=False,
        no_chase_after_act=False,
        no_open_after_open=False,
        passive_entry_first=False,
        protect_80_enabled=False,
        trailing_activate_ratio=0,
        max_quote_age_seconds=max_quote_age_seconds,
    )
    return DsvProfile.from_settings(settings).to_record()


class _TriggerTracker:
    """Evaluates time-based and event-based fault rules and records coverage."""

    def __init__(self, rules: tuple[FaultRule, ...]) -> None:
        self._rules = rules
        self._records: dict[str, TriggerRecord] = {
            rule.rule_id: TriggerRecord(
                rule_id=rule.rule_id,
                trigger_kind=rule.trigger_kind.value,
                required=rule.required,
            )
            for rule in rules
        }
        self._seen_first_open = False
        self._seen_first_fill = False

    def active_rules(self, *, now_ns: int, round_seq: int, wall_time: str) -> list[FaultRule]:
        active: list[FaultRule] = []
        for rule in self._rules:
            rec = self._records[rule.rule_id]
            if rule.trigger_kind == TriggerKind.TIME_RANGE:
                if rule.start_utc is None or rule.end_utc is None:
                    continue
                start_ns = _iso_to_ns(rule.start_utc)
                end_ns = _iso_to_ns(rule.end_utc)
                if start_ns <= now_ns <= end_ns:
                    if not rec.triggered:
                        rec.triggered = True
                        rec.first_round_seq = round_seq
                        rec.first_wall_time = wall_time
                    rec.trigger_count += 1
                    active.append(rule)
                elif rec.triggered and now_ns > end_ns:
                    rec.resolved = True
            else:
                if rec.triggered and rec.active_until_ns is not None:
                    if now_ns <= rec.active_until_ns:
                        active.append(rule)
                    else:
                        rec.resolved = True
        return active

    def observe_events(
        self,
        *,
        now_ns: int,
        round_seq: int,
        wall_time: str,
        matcher_events: list[str],
        workbench: dict[str, object],
        recovered_contracts: list[str],
    ) -> None:
        event_set = set(matcher_events)
        cycles = workbench.get("cycles") or []
        stop_loss_hit = any(
            str(c.get("exit_reason") or "").upper().find("STOP") >= 0
            for c in cycles
            if isinstance(c, dict)
        )

        for rule in self._rules:
            rec = self._records[rule.rule_id]
            fired = False
            kind = rule.trigger_kind
            if kind == TriggerKind.FIRST_OPEN_ORDER and "OPEN_ORDER_SENT" in event_set and not self._seen_first_open:
                fired = True
            elif kind == TriggerKind.FIRST_FILL and "FILL" in event_set and not self._seen_first_fill:
                fired = True
            elif kind == TriggerKind.PARTIAL_FILL and "PARTIAL_FILL" in event_set:
                fired = True
            elif kind == TriggerKind.CANCEL_REQUESTED and "CANCEL_REQUESTED" in event_set:
                fired = True
            elif kind == TriggerKind.STOP_LOSS_TRIGGERED and stop_loss_hit:
                fired = True
            elif kind == TriggerKind.MULTI_CONTRACT_ORDER and "MULTI_CONTRACT_ORDER" in event_set:
                fired = True
            elif kind == TriggerKind.FEED_RECOVERED and recovered_contracts:
                if not rule.contracts or any(c in recovered_contracts for c in rule.contracts):
                    fired = True

            if fired:
                if not rec.triggered:
                    rec.triggered = True
                    rec.first_round_seq = round_seq
                    rec.first_wall_time = wall_time
                rec.trigger_count += 1
                rec.resolved = False
                rec.active_until_ns = now_ns + max(1, rule.duration_seconds) * 1_000_000_000

        if "OPEN_ORDER_SENT" in event_set:
            self._seen_first_open = True
        if "FILL" in event_set:
            self._seen_first_fill = True

    def coverage(self) -> tuple[list[dict[str, object]], list[str]]:
        items = [rec.to_dict() for rec in self._records.values()]
        uncovered = [rec.rule_id for rec in self._records.values() if rec.required and not rec.triggered]
        return items, uncovered


class MarketScenarioRunner:
    """Executes a ScenarioConfig against isolated QA Target Owner with strict verdicting."""

    def __init__(self, config: ScenarioConfig, *, work_dir: str | Path) -> None:
        self.config = config
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.source = RecordedReplaySource(config.fixture_db)

    def run(self) -> ScenarioResult:
        started_monotonic = time.monotonic()
        repo_root = Path(__file__).resolve().parents[2]
        git_sha = _git_commit_sha(repo_root)
        contracts = self.config.contracts or self.source.available_contracts
        tracker = _TriggerTracker(self.config.fault_rules)

        # Materialize round stream (raw recorded replay or Phase-2 synthesized stream)
        if self.config.synthesis is not None:
            synthesizer = MarketSynthesizer(self.source)
            rounds = list(
                synthesizer.generate_rounds(
                    contracts=contracts,
                    spec=self.config.synthesis,
                    start_utc=self.config.start_utc,
                    end_utc=self.config.end_utc,
                    bucket_seconds=self.config.bucket_seconds,
                    max_rounds=self.config.max_rounds,
                )
            )
        else:
            rounds = list(
                self.source.iter_rounds(
                    contracts=contracts,
                    start_utc=self.config.start_utc,
                    end_utc=self.config.end_utc,
                    bucket_seconds=self.config.bucket_seconds,
                    max_rounds=self.config.max_rounds,
                )
            )
        if not rounds:
            return self._write_result(
                ScenarioResult(
                    scenario_name=self.config.scenario_name,
                    verdict=Verdict.BLOCKED,
                    engine_version=ENGINE_VERSION,
                    dataset_sha256=self.source.sha256,
                    git_commit=git_sha,
                    rounds_executed=0,
                    contracts=contracts,
                    trigger_coverage=[],
                    uncovered_rules=[],
                    failure={"reason": "NO_TICKS_IN_SELECTED_WINDOW"},
                )
            )

        profile_record = self.config.profile_record
        if profile_record is None:
            offsets = self.source.derive_profile_offsets(contracts, start_utc=self.config.start_utc)
            profile_record = build_default_profile(
                contracts,
                offsets,
                max_quote_age_seconds=self.config.max_quote_age_seconds,
                order_volume=self.config.order_volume,
            )

        session_open, session_close = self.source.day_session_bounds(contracts)
        first_ns, first_utc, first_truth = rounds[0]
        initial_utc = ns_to_utc_iso(first_ns - 10 * 1_000_000_000)

        first_by_contract: dict[str, object] = {}
        for _, _, truth_map in rounds:
            for sym, t in truth_map.items():
                if sym not in first_by_contract:
                    first_by_contract[sym] = t
            if len(first_by_contract) == len(contracts):
                break

        channel = MarketChannel(contracts)
        rounds_executed = 0
        failure_info: dict[str, object] | None = None
        verdict = Verdict.PASS
        matcher_stats_summary: dict[str, object] = {}
        final_positions: object = None

        try:
            with TradingScenario(
                self.work_dir,
                scenario_name=self.config.scenario_name,
                contract=contracts[0],
                contracts=contracts,
                trading_day=self.source.trading_day,
                initial_utc=initial_utc,
                session_open=session_open,
                session_close=session_close,
                profile_record=profile_record,
                max_quote_age_seconds=self.config.max_quote_age_seconds,
                fallback_book_to_last=False,
            ) as scenario:
                assert scenario.broker is not None

                # Configure per-contract instrument metadata in FakeMarket
                for sym in contracts:
                    inst = self.source.instruments[sym]
                    state = scenario.market._contracts[sym]
                    state.tick_size = decimal_text(inst.price_tick)
                    state.multiplier = decimal_text(inst.volume_multiple)
                    if inst.lower_limit is not None:
                        state.lower_limit = inst.lower_limit
                    if inst.upper_limit is not None:
                        state.upper_limit = inst.upper_limit
                    state.available = "10000000"
                    ft = first_by_contract.get(sym)
                    if ft is not None:
                        state.day_low = ft.day_low  # type: ignore[attr-defined]
                        state.day_high = ft.day_high  # type: ignore[attr-defined]
                        last_d = Decimal(ft.last)  # type: ignore[attr-defined]
                        state.margin_per_lot = decimal_text(
                            (last_d * inst.volume_multiple * Decimal("0.15")).quantize(Decimal("1"))
                        )

                # Pre-register independent expected targets (never derived from Runtime orders)
                for sym, spec in self.config.expected_targets.items():
                    scenario.oracle.set_target_expectation(
                        sym,
                        direction=str(spec["direction"]),
                        quantity=int(spec["quantity"]),
                        exit_target=spec.get("exit_target"),  # type: ignore[arg-type]
                    )

                matcher = OrderMatcher(broker=scenario.broker, mode=self.config.match_mode)

                # Round 0: sync & ARM
                r0_ticks = channel.process_round(
                    now_ns=first_ns,
                    now_utc=first_utc,
                    truth_by_contract=first_truth,
                    active_rules=[],
                )
                r0 = scenario.step({"wall_time": first_utc}, ticks_by_contract=r0_ticks)
                rounds_executed += 1
                if not (r0.workbench.get("connection") or {}).get("synchronized"):
                    raise RuntimeError("Runtime failed to synchronize on initial replay round")
                arm = scenario.arm_strategy()
                if arm.get("status") != "SUCCEEDED":
                    raise RuntimeError(f"ARM_ENTRIES failed: {arm}")

                # Main replay loop
                last_ns = first_ns
                for now_ns, now_utc, truth_map in rounds[1:]:
                    if time.monotonic() - started_monotonic > self.config.wall_timeout_seconds:
                        verdict = Verdict.TIMEOUT
                        failure_info = {
                            "reason": "WALL_TIMEOUT_EXCEEDED",
                            "timeout_seconds": self.config.wall_timeout_seconds,
                            "rounds_executed": rounds_executed,
                        }
                        break

                    # Ensure strictly advancing virtual clock
                    if now_ns <= last_ns:
                        now_ns = last_ns + 1_000_000  # +1 ms
                        now_utc = ns_to_utc_iso(now_ns)
                    last_ns = now_ns

                    active_rules = tracker.active_rules(
                        now_ns=now_ns, round_seq=rounds_executed + 1, wall_time=now_utc
                    )
                    observed_ticks = channel.process_round(
                        now_ns=now_ns,
                        now_utc=now_utc,
                        truth_by_contract=truth_map,
                        active_rules=active_rules,
                    )
                    outcome: StepOutcome = scenario.step(
                        {"wall_time": now_utc},
                        ticks_by_contract=observed_ticks,
                    )
                    rounds_executed += 1

                    matcher_events = matcher.step(
                        round_seq=outcome.round_seq,
                        wall_time=outcome.wall_time,
                        market_truth=channel.latest_truth,
                        active_rules=active_rules,
                        outbound_events=outcome.outbound_events,
                    )
                    tracker.observe_events(
                        now_ns=now_ns,
                        round_seq=outcome.round_seq,
                        wall_time=outcome.wall_time,
                        matcher_events=matcher_events,
                        workbench=outcome.workbench,
                        recovered_contracts=channel.recovered_contracts_last_round,
                    )

                # Recovery drain phase: release any held facts and let convergence finish
                matcher.flush_held_facts()
                if verdict == Verdict.PASS:
                    for extra in range(1, max(1, self.config.drain_rounds) + 1):
                        drain_ns = last_ns + extra * 1_000_000_000
                        drain_utc = ns_to_utc_iso(drain_ns)
                        drain_ticks = channel.process_round(
                            now_ns=drain_ns,
                            now_utc=drain_utc,
                            truth_by_contract=channel.latest_truth,
                            active_rules=[],
                        )
                        outcome = scenario.step({"wall_time": drain_utc}, ticks_by_contract=drain_ticks)
                        rounds_executed += 1
                        matcher.step(
                            round_seq=outcome.round_seq,
                            wall_time=outcome.wall_time,
                            market_truth=channel.latest_truth,
                            active_rules=[],
                            outbound_events=outcome.outbound_events,
                        )
                        snap = scenario.broker.snapshot()
                        if int(snap.get("pending_fact_count", 0)) == 0 and not snap.get("active_children"):
                            break

                    final_snap = scenario.broker.snapshot()
                    final_positions = final_snap.get("positions_by_contract")
                    if self.config.require_flat_convergence:
                        for sym in contracts:
                            pos = (final_positions or {}).get(sym) or {}  # type: ignore[union-attr]
                            long_q = int(pos.get("long_qty", 0))
                            short_q = int(pos.get("short_qty", 0))
                            if long_q > 0 and not channel.is_limit_locked(sym, close_buy=False):
                                verdict = Verdict.FAIL
                                failure_info = {
                                    "reason": "CONVERGENCE_NOT_FLAT",
                                    "contract_id": sym,
                                    "remaining_long": long_q,
                                }
                                break
                            if short_q > 0 and not channel.is_limit_locked(sym, close_buy=True):
                                verdict = Verdict.FAIL
                                failure_info = {
                                    "reason": "CONVERGENCE_NOT_FLAT",
                                    "contract_id": sym,
                                    "remaining_short": short_q,
                                }
                                break

                matcher_stats_summary = {
                    "accepted": matcher.stats.accepted,
                    "rejected": matcher.stats.rejected,
                    "fills": matcher.stats.fills,
                    "partial_fills": matcher.stats.partial_fills,
                    "late_fills": matcher.stats.late_fills,
                    "cancels_confirmed": matcher.stats.cancels_confirmed,
                    "replayed_trades": matcher.stats.replayed_trades,
                    "no_ack_held": matcher.stats.no_ack_held,
                }
                scenario.finish(
                    passed=(verdict == Verdict.PASS),
                    failure_message=None if failure_info is None else json.dumps(failure_info, ensure_ascii=False),
                )
        except OracleViolation as error:
            verdict = Verdict.FAIL
            failure_info = {
                "invariant_id": error.invariant_id,
                "round_seq": error.round_seq,
                "frame_id": error.frame_id,
                "message": str(error),
                "details": error.details,
            }
        except Exception as error:
            verdict = Verdict.BLOCKED
            failure_info = {
                "error_type": type(error).__name__,
                "message": str(error),
            }

        coverage_items, uncovered = tracker.coverage()
        if verdict == Verdict.PASS and uncovered:
            verdict = Verdict.NOT_COVERED
            failure_info = {
                "reason": "REQUIRED_TRIGGERS_NOT_COVERED",
                "uncovered_rules": uncovered,
            }

        generated_stream_path: str | None = None
        if self.config.export_generated_stream_jsonl or verdict in {Verdict.FAIL, Verdict.TIMEOUT}:
            stream_file = self.work_dir / "generated-market-stream.jsonl"
            with stream_file.open("w", encoding="utf-8") as stream:
                for ts_ns, ts_utc, truth_map in rounds:
                    record = {
                        "round_time_ns": ts_ns,
                        "round_time_utc": ts_utc,
                        "ticks": {
                            sym: {
                                "source_time_utc": t.source_time_utc,
                                "last": t.last,
                                "bid1": t.bid1,
                                "bid_volume1": t.bid_volume1,
                                "ask1": t.ask1,
                                "ask_volume1": t.ask_volume1,
                                "day_low": t.day_low,
                                "day_high": t.day_high,
                                "candle_id": t.candle_id,
                            }
                            for sym, t in truth_map.items()
                        },
                    }
                    stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            generated_stream_path = str(stream_file)

        return self._write_result(
            ScenarioResult(
                scenario_name=self.config.scenario_name,
                verdict=verdict,
                engine_version=ENGINE_VERSION,
                dataset_sha256=self.source.sha256,
                git_commit=git_sha,
                rounds_executed=rounds_executed,
                contracts=contracts,
                trigger_coverage=coverage_items,
                uncovered_rules=uncovered,
                failure=failure_info,
                summary={
                    "fixture_db": str(self.config.fixture_db),
                    "match_mode": self.config.match_mode.value,
                    "bucket_seconds": self.config.bucket_seconds,
                    "seed": self.config.seed,
                    "synthesis": (
                        self.config.synthesis.to_dict()
                        if hasattr(self.config.synthesis, "to_dict")
                        else None
                    ),
                    "generated_stream_jsonl": generated_stream_path,
                    "matcher": matcher_stats_summary,
                    "final_positions": final_positions,
                    "profile_record": profile_record,
                },
            )
        )

    def _write_result(self, result: ScenarioResult) -> ScenarioResult:
        evidence_path = self.work_dir / "scenario-engine-result.json"
        result.evidence_path = str(evidence_path)
        evidence_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return result
