"""Multi-day cross-trading-day endurance runner (Phase 4).

Executes 3..20+ virtual trading days within a single TradingScenario lifecycle,
rolling trading days via FakeBroker/FakeMarket/FrameAssembler (today -> historical
position conversion, session boundary transitions, daily state resets) while
enforcing ReferenceOracle invariants across every day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import random

from online_trader.domain.values import decimal_text
from qa.e2e.oracle import OracleViolation
from qa.e2e.scenario import StepOutcome, TradingScenario
from qa.market_data.schema import ns_to_utc_iso
from qa.scenario_engine.channel import MarketChannel
from qa.scenario_engine.matcher import OrderMatcher
from qa.scenario_engine.models import (
    BrokerMatchMode,
    ChannelFaultKind,
    ENGINE_VERSION,
    FaultRule,
    MarketTickTruth,
    TriggerKind,
    Verdict,
)
from qa.scenario_engine.replay_source import RecordedReplaySource
from qa.scenario_engine.runner import _TriggerTracker, _git_commit_sha, build_default_profile
from qa.scenario_engine.synthesizer import GenerationMode, MarketSynthesizer, SynthesisSpec


def _next_trading_day(current_day: str) -> str:
    parsed = datetime.strptime(current_day, "%Y-%m-%d").date()
    candidate = parsed + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def _day_offset_ns(source_day: str, target_day: str) -> int:
    src = datetime.strptime(source_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    dst = datetime.strptime(target_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int((dst - src).total_seconds()) * 1_000_000_000


def _shift_iso_day(iso_utc: str, delta_ns: int) -> str:
    parsed = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    shifted = parsed + timedelta(microseconds=delta_ns // 1_000)
    return shifted.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class MultiDayConfig:
    """Configuration for multi-trading-day endurance & cross-day rollover tests."""

    scenario_name: str
    fixture_dbs: tuple[Path, ...]
    days: int = 3
    contracts: tuple[str, ...] = ()
    start_window_offset_minutes: int = 0
    window_minutes_per_day: int = 20
    bucket_seconds: int = 2
    max_rounds_per_day: int = 120
    match_mode: BrokerMatchMode = BrokerMatchMode.BOOK_CONSTRAINED
    order_volume: int = 2
    max_quote_age_seconds: int = 30
    seed: int = 2026
    inject_daily_faults: bool = True
    volatility_range: tuple[float, float] = (0.9, 1.8)
    drift_range: tuple[float, float] = (-0.2, 0.2)


class MultiDayEnduranceRunner:
    """Runs multiple consecutive virtual trading days with cross-day position rollover."""

    def __init__(self, config: MultiDayConfig, *, work_dir: str | Path) -> None:
        if config.days < 1:
            raise ValueError("MultiDayConfig.days must be >= 1")
        if not config.fixture_dbs:
            raise ValueError("MultiDayConfig.fixture_dbs must be non-empty")
        self.config = config
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.sources = [RecordedReplaySource(p) for p in config.fixture_dbs]

    def run(self) -> dict[str, object]:
        repo_root = Path(__file__).resolve().parents[2]
        git_sha = _git_commit_sha(repo_root)
        rng = random.Random(self.config.seed)

        primary_source = self.sources[0]
        contracts = self.config.contracts or primary_source.available_contracts
        offsets = primary_source.derive_profile_offsets(contracts)
        profile_record = build_default_profile(
            contracts,
            offsets,
            max_quote_age_seconds=self.config.max_quote_age_seconds,
            order_volume=self.config.order_volume,
        )

        current_day = primary_source.trading_day
        base_open, base_close = primary_source.day_session_bounds(contracts)
        first_rounds_preview = list(
            primary_source.iter_rounds(
                contracts=contracts,
                bucket_seconds=self.config.bucket_seconds,
                max_rounds=2,
            )
        )
        if not first_rounds_preview:
            return {
                "schema_version": "MARKET_MULTIDAY_REPORT/1",
                "engine_version": ENGINE_VERSION,
                "scenario_name": self.config.scenario_name,
                "verdict": Verdict.BLOCKED.value,
                "failure": {"reason": "NO_TICKS_IN_PRIMARY_FIXTURE"},
            }
        initial_utc = ns_to_utc_iso(first_rounds_preview[0][0] - 10 * 1_000_000_000)

        verdict = Verdict.PASS
        failure_info: dict[str, object] | None = None
        daily_summaries: list[dict[str, object]] = []
        total_rounds = 0

        try:
            with TradingScenario(
                self.work_dir,
                scenario_name=self.config.scenario_name,
                contract=contracts[0],
                contracts=contracts,
                trading_day=current_day,
                initial_utc=initial_utc,
                session_open=base_open,
                session_close=base_close,
                profile_record=profile_record,
                max_quote_age_seconds=self.config.max_quote_age_seconds,
                fallback_book_to_last=False,
            ) as scenario:
                assert scenario.broker is not None

                for sym in contracts:
                    inst = primary_source.instruments[sym]
                    st = scenario.market._contracts[sym]
                    st.tick_size = decimal_text(inst.price_tick)
                    st.multiplier = decimal_text(inst.volume_multiple)
                    if inst.lower_limit is not None:
                        st.lower_limit = inst.lower_limit
                    if inst.upper_limit is not None:
                        st.upper_limit = inst.upper_limit
                    st.available = "10000000"

                channel = MarketChannel(contracts)
                matcher = OrderMatcher(broker=scenario.broker, mode=self.config.match_mode)
                armed = False
                last_global_ns = first_rounds_preview[0][0] - 5 * 1_000_000_000

                for day_idx in range(self.config.days):
                    src = self.sources[day_idx % len(self.sources)]
                    day_seed = rng.randint(1, 2**31 - 1)
                    vol_scale = round(rng.uniform(*self.config.volatility_range), 2)
                    drift = round(rng.uniform(*self.config.drift_range), 3)

                    day_open_raw, day_close_raw = src.day_session_bounds(contracts)
                    shift_ns = _day_offset_ns(src.trading_day, current_day)
                    shifted_open = _shift_iso_day(day_open_raw, shift_ns)
                    shifted_close = _shift_iso_day(day_close_raw, shift_ns)

                    if day_idx > 0:
                        scenario.roll_trading_day(
                            current_day,
                            session_open=shifted_open,
                            session_close=shifted_close,
                        )

                    syn_spec = SynthesisSpec(
                        mode=GenerationMode.DERIVED_TRANSFORM if day_idx > 0 else GenerationMode.RAW_REPLAY,
                        volatility_scale=vol_scale,
                        drift_ticks_per_step=drift,
                        reverse_trend_at_ratio=0.55 if day_idx % 2 == 1 else None,
                        seed=day_seed,
                    )
                    raw_day_rounds = list(
                        MarketSynthesizer(src).generate_rounds(
                            contracts=contracts,
                            spec=syn_spec,
                            bucket_seconds=self.config.bucket_seconds,
                            max_rounds=self.config.max_rounds_per_day,
                        )
                    )
                    if not raw_day_rounds:
                        current_day = _next_trading_day(current_day)
                        continue

                    # Shift timestamps to current_day while guaranteeing strict monotonicity
                    shifted_rounds: list[tuple[int, str, dict[str, MarketTickTruth]]] = []
                    for r_ns, _r_utc, t_map in raw_day_rounds:
                        ns = r_ns + shift_ns
                        if ns <= last_global_ns:
                            ns = last_global_ns + max(1, self.config.bucket_seconds) * 1_000_000_000
                        last_global_ns = ns
                        u = ns_to_utc_iso(ns)
                        new_map = {
                            sym: MarketTickTruth(
                                symbol=t.symbol,
                                trading_day=current_day,
                                source_time_ns=ns,
                                source_time_utc=u,
                                last=t.last,
                                bid1=t.bid1,
                                bid_volume1=t.bid_volume1,
                                ask1=t.ask1,
                                ask_volume1=t.ask_volume1,
                                day_low=t.day_low,
                                day_high=t.day_high,
                                lower_limit=t.lower_limit,
                                upper_limit=t.upper_limit,
                                candle_id=t.candle_id,
                                opening_session=t.opening_session,
                                market_session_active=t.market_session_active,
                                session_open_utc=shifted_open,
                                session_close_utc=shifted_close,
                            )
                            for sym, t in t_map.items()
                        }
                        shifted_rounds.append((ns, u, new_map))

                    # Build optional daily fault rule
                    day_rules: tuple[FaultRule, ...] = ()
                    if self.config.inject_daily_faults and len(shifted_rounds) >= 6:
                        mid_idx = len(shifted_rounds) // 2
                        f_start = shifted_rounds[mid_idx][1]
                        f_end = shifted_rounds[min(len(shifted_rounds) - 1, mid_idx + 3)][1]
                        day_rules = (
                            FaultRule(
                                rule_id=f"DAY{day_idx + 1}_CHANNEL_FAULT",
                                trigger_kind=TriggerKind.TIME_RANGE,
                                fault_kind=rng.choice([
                                    ChannelFaultKind.BURST,
                                    ChannelFaultKind.DELAY,
                                    ChannelFaultKind.DROP,
                                ]),
                                contracts=(rng.choice(contracts),),
                                start_utc=f_start,
                                end_utc=f_end,
                                delay_seconds=6,
                                required=True,
                            ),
                        )
                    day_tracker = _TriggerTracker(day_rules)
                    day_rounds_run = 0

                    for ns, u, truth_map in shifted_rounds:
                        active = day_tracker.active_rules(
                            now_ns=ns, round_seq=total_rounds + 1, wall_time=u
                        )
                        obs_ticks = channel.process_round(
                            now_ns=ns,
                            now_utc=u,
                            truth_by_contract=truth_map,
                            active_rules=active,
                        )
                        outcome: StepOutcome = scenario.step({"wall_time": u}, ticks_by_contract=obs_ticks)
                        total_rounds += 1
                        day_rounds_run += 1

                        if not armed:
                            arm = scenario.arm_strategy()
                            if arm.get("status") != "SUCCEEDED":
                                raise RuntimeError(f"ARM_ENTRIES failed on day {current_day}: {arm}")
                            armed = True

                        events = matcher.step(
                            round_seq=outcome.round_seq,
                            wall_time=outcome.wall_time,
                            market_truth=channel.latest_truth,
                            active_rules=active,
                            outbound_events=outcome.outbound_events,
                        )
                        day_tracker.observe_events(
                            now_ns=ns,
                            round_seq=outcome.round_seq,
                            wall_time=outcome.wall_time,
                            matcher_events=events,
                            workbench=outcome.workbench,
                            recovered_contracts=channel.recovered_contracts_last_round,
                        )

                    matcher.flush_held_facts()
                    day_snap = scenario.broker.snapshot()
                    cov_items, _ = day_tracker.coverage()
                    daily_summaries.append({
                        "day_index": day_idx + 1,
                        "trading_day": current_day,
                        "source_fixture": str(src.db_path),
                        "source_sha256": src.sha256,
                        "rounds": day_rounds_run,
                        "vol_scale": vol_scale,
                        "drift": drift,
                        "end_positions": day_snap.get("positions_by_contract"),
                        "trigger_coverage": cov_items,
                    })
                    current_day = _next_trading_day(current_day)

                scenario.finish(passed=(verdict == Verdict.PASS))

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

        report = {
            "schema_version": "MARKET_MULTIDAY_REPORT/1",
            "engine_version": ENGINE_VERSION,
            "scenario_name": self.config.scenario_name,
            "verdict": verdict.value,
            "git_commit": git_sha,
            "seed": self.config.seed,
            "days_requested": self.config.days,
            "days_completed": len(daily_summaries),
            "total_rounds": total_rounds,
            "contracts": list(contracts),
            "daily_summaries": daily_summaries,
            "failure": failure_info,
        }
        (self.work_dir / "multiday-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return report
