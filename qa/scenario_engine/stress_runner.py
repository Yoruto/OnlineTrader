"""Realtime 5-dimensional stress runner decoupled from lockstep TradingScenario.step().

Pushes market FACT_FRAMEs asynchronously on wall-clock schedule, processes broker
events with configurable return delay/rate, monitors Runtime + optional Qt UI via
ExternalPerformanceMonitor, and compares against performance baselines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import Event, Lock, Thread
import time
from typing import Any
from uuid import uuid4

from online_trader.domain.values import decimal_text
from online_trader.transport.status_client import StatusClient
from qa.e2e import assert_offline_qa_environment
from qa.e2e.assembler import FrameAssembler
from qa.e2e.clock import VirtualClock
from qa.e2e.desktop_host import QADesktopHost
from qa.e2e.evidence import TraceRecorder
from qa.e2e.market import FakeMarket
from qa.e2e.oracle import OracleViolation, ReferenceOracle
from qa.market_data.schema import ns_to_utc_iso
from qa.scenario_engine.models import ENGINE_VERSION, Verdict
from qa.scenario_engine.monitor import (
    DegradationThresholds,
    ExternalPerformanceMonitor,
    compare_with_baseline,
)
from qa.scenario_engine.replay_source import RecordedReplaySource
from qa.scenario_engine.runner import _git_commit_sha, build_default_profile
from qa.scenario_engine.synthesizer import MarketSynthesizer, SynthesisSpec
from qa.scenario_engine.ui_stress import WorkbenchUIStressDriver
from qa.target_owner.fake_broker import FakeBroker
from qa.target_owner.process_runtime import OfflineTargetOwnerProcess


@dataclass(frozen=True)
class StressProfile:
    """Five-dimensional realtime stress configuration."""

    scenario_name: str
    fixture_db: Path
    contracts: tuple[str, ...] = ()
    start_utc: str | None = None
    end_utc: str | None = None
    bucket_seconds: int = 1
    # 5 independent stress dimensions
    market_rate_multiplier: float = 3.0
    broker_event_rate_multiplier: float = 1.0
    broker_return_delay_seconds: float = 0.0
    workbench_ops_per_minute: float = 10.0
    duration_seconds: float = 30.0
    # Runtime & monitoring controls
    max_inflight_frames: int = 32
    with_workbench_ui: bool = False
    ui_offscreen: bool = True
    enable_market_diagnostics: bool = True
    order_volume: int = 2
    max_quote_age_seconds: int = 30
    synthesis: SynthesisSpec | None = None
    baseline_path: Path | None = None
    save_baseline_path: Path | None = None
    degradation_thresholds: DegradationThresholds = field(default_factory=DegradationThresholds)

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario_name": self.scenario_name,
            "fixture_db": str(self.fixture_db),
            "contracts": list(self.contracts),
            "market_rate_multiplier": self.market_rate_multiplier,
            "broker_event_rate_multiplier": self.broker_event_rate_multiplier,
            "broker_return_delay_seconds": self.broker_return_delay_seconds,
            "workbench_ops_per_minute": self.workbench_ops_per_minute,
            "duration_seconds": self.duration_seconds,
            "max_inflight_frames": self.max_inflight_frames,
            "with_workbench_ui": self.with_workbench_ui,
            "enable_market_diagnostics": self.enable_market_diagnostics,
        }


class RealtimeStressRunner:
    """Runs asynchronous wall-clock stress against isolated QA Owner or DesktopHost."""

    def __init__(self, profile: StressProfile, *, work_dir: str | Path) -> None:
        assert_offline_qa_environment(block_tqsdk=True)
        self.profile = profile
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.source = RecordedReplaySource(profile.fixture_db)

    def run(self) -> dict[str, object]:
        repo_root = Path(__file__).resolve().parents[2]
        git_sha = _git_commit_sha(repo_root)
        contracts = self.profile.contracts or self.source.available_contracts

        if self.profile.synthesis is not None:
            rounds = list(
                MarketSynthesizer(self.source).generate_rounds(
                    contracts=contracts,
                    spec=self.profile.synthesis,
                    start_utc=self.profile.start_utc,
                    end_utc=self.profile.end_utc,
                    bucket_seconds=self.profile.bucket_seconds,
                )
            )
        else:
            rounds = list(
                self.source.iter_rounds(
                    contracts=contracts,
                    start_utc=self.profile.start_utc,
                    end_utc=self.profile.end_utc,
                    bucket_seconds=self.profile.bucket_seconds,
                )
            )
        if not rounds:
            return {
                "schema_version": "MARKET_STRESS_REPORT/1",
                "engine_version": ENGINE_VERSION,
                "scenario_name": self.profile.scenario_name,
                "verdict": Verdict.BLOCKED.value,
                "failure": {"reason": "NO_TICKS_FOR_STRESS_RUN"},
            }

        offsets = self.source.derive_profile_offsets(contracts, start_utc=self.profile.start_utc)
        profile_record = build_default_profile(
            contracts,
            offsets,
            max_quote_age_seconds=self.profile.max_quote_age_seconds,
            order_volume=self.profile.order_volume,
        )
        session_open, session_close = self.source.day_session_bounds(contracts)
        first_ns, first_utc, _ = rounds[0]
        initial_utc = ns_to_utc_iso(first_ns - 10 * 1_000_000_000)

        host_dir = self.work_dir / "host"
        evidence_dir = self.work_dir / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"stress-{uuid4().hex[:10]}"

        host: Any
        if self.profile.with_workbench_ui:
            host = QADesktopHost(
                host_dir,
                profile_record=profile_record,
                trading_day=self.source.trading_day,
            )
        else:
            host = OfflineTargetOwnerProcess(
                host_dir,
                profile_record=profile_record,
                trading_day=self.source.trading_day,
                use_production_tick_wrapper=True,
                enable_market_diagnostics=self.profile.enable_market_diagnostics,
            )

        verdict = Verdict.PASS
        failure_info: dict[str, object] | None = None
        perf_summary: dict[str, object] = {}
        ui_summary: dict[str, object] | None = None
        baseline_diff: dict[str, object] | None = None

        try:
            host.start()
            assert host.account_key is not None and host.boot_id is not None

            broker = FakeBroker(
                account_key=host.account_key,
                contract=contracts[0],
                contracts=contracts,
                trading_day=self.source.trading_day,
                trace_path=evidence_dir / "broker-events.jsonl",
                run_id=run_id,
            )
            clock = VirtualClock(initial_utc)
            market = FakeMarket(
                contract_id=contracts[0],
                contracts=contracts,
                trading_day=self.source.trading_day,
                session_open=session_open,
                session_close=session_close,
                fallback_book_to_last=False,
            )
            for sym in contracts:
                inst = self.source.instruments[sym]
                st = market._contracts[sym]
                st.tick_size = decimal_text(inst.price_tick)
                st.multiplier = decimal_text(inst.volume_multiple)
                if inst.lower_limit is not None:
                    st.lower_limit = inst.lower_limit
                if inst.upper_limit is not None:
                    st.upper_limit = inst.upper_limit
                st.available = "10000000"

            assembler = FrameAssembler(
                market=market,
                broker=broker,
                clock=clock,
                trading_day=self.source.trading_day,
            )
            client = StatusClient(
                host.pipe,
                timeout=5.0,
                expected_server_pid=host.owner_pid,
                expected_account_key=host.account_key,
                expected_kernel_boot_id=host.boot_id,
            )
            oracle = ReferenceOracle()
            oracle.set_boot_expectation(host.boot_id, config_revision="1")

            # Initial synchronous round + ARM
            clock.advance_to(first_utc)
            for sym, t in rounds[0][2].items():
                market.apply_tick(
                    wall_time=first_utc,
                    last=t.last,
                    contract_id=sym,
                    bid1=t.bid1,
                    ask1=t.ask1,
                    day_low=t.day_low,
                    day_high=t.day_high,
                    candle_id=t.candle_id,
                    native_time=t.source_time_utc,
                    delivery_time=first_utc,
                    fallback_book_to_last=False,
                )
            f0 = assembler.build()
            cursor0 = host.event_cursor
            host.send_fact_frame(f0)
            host.wait_for_frame_applied(str(f0["id"]), after=cursor0)
            broker.acknowledge_frame(str(f0["id"]))

            wb0 = client.workbench()
            rev = str((wb0.get("configuration") or {}).get("config_revision", "1"))
            arm = client.command(
                "ARM_ENTRIES",
                {"scope_type": "ACCOUNT", "scope_ids": []},
                {
                    "authority_policy_id": "target-authority-current-boot",
                    "rule_set_id": "DSV_TARGET_EXECUTION",
                    "config_revision": rev,
                },
                command_id=f"stress-arm-{uuid4().hex[:8]}",
                expected_config_revision=rev,
            )
            if arm.get("status") != "SUCCEEDED":
                raise RuntimeError(f"ARM_ENTRIES failed before stress run: {arm}")
            oracle.set_authority_expectation("ARMED")

            # Start external monitor isolated from feed & UI threads
            monitor = ExternalPerformanceMonitor(
                output_jsonl=self.work_dir / "stress-metrics.jsonl",
                owner_pid=host.owner_pid,
                view_pid=getattr(host, "view_pid", None),
                log_dir=host_dir,
                sample_interval_seconds=0.5,
                ipc_probe=client.workbench,
                backlog_probe=lambda: host.pending_frame_count,
            )

            stop_event = Event()
            state_lock = Lock()
            sent_timestamps: dict[str, int] = {}
            delayed_broker_actions: list[tuple[float, str, str, dict[str, object]]] = []
            event_cursor = [host.event_cursor]
            feed_errors: list[BaseException] = []

            def _event_watcher() -> None:
                while not stop_event.is_set() or host.pending_frame_count > 0:
                    events = host.events
                    new_events = events[event_cursor[0]:]
                    event_cursor[0] = len(events)
                    now_mono = time.monotonic()
                    for ev in new_events:
                        ev_type = ev.get("event_type")
                        if ev_type == "FACT_FRAME_APPLIED":
                            fid = str(ev.get("frame_id") or "")
                            with state_lock:
                                sent_ns = sent_timestamps.pop(fid, None)
                                if broker._inflight is not None and broker._inflight.get("id") == fid:
                                    broker.acknowledge_frame(fid)
                            if sent_ns is not None:
                                monitor.record_frame_applied((time.monotonic_ns() - sent_ns) / 1_000_000.0)
                        elif ev_type in {"OUTBOUND_SEND", "OUTBOUND_CANCEL"}:
                            with state_lock:
                                broker.observe_adapter_event(ev)
                                req = ev.get("request") or {}
                                cid = str(req.get("child_order_id") or "")
                                if cid:
                                    due = now_mono + max(0.0, self.profile.broker_return_delay_seconds)
                                    delayed_broker_actions.append((due, str(ev_type), cid, dict(req)))

                    # Execute due broker responses
                    with state_lock:
                        still_pending = []
                        for due, kind, cid, req in delayed_broker_actions:
                            if now_mono >= due:
                                snap = broker.snapshot()
                                order = (snap.get("orders") or {}).get(cid)
                                if not order:
                                    continue
                                if kind == "OUTBOUND_SEND" and order.get("status") == "SEND_REQUESTED":
                                    broker.accept_order(cid)
                                    qty = int(req.get("quantity") or 1)
                                    px = str(req.get("limit_price") or "1")
                                    _, trade_fact = broker.fill(cid, qty, px)
                                    # Optional amplified duplicate trade events
                                    extra_dups = max(0, int(self.profile.broker_event_rate_multiplier) - 1)
                                    tid = trade_fact.get("native_trade_id")
                                    for _ in range(extra_dups):
                                        if tid:
                                            broker.replay_trade_fact(str(tid))
                                elif kind == "OUTBOUND_CANCEL" and order.get("status") == "ALIVE":
                                    broker.confirm_cancel(cid)
                            else:
                                still_pending.append((due, kind, cid, req))
                        delayed_broker_actions[:] = still_pending

                    if stop_event.is_set() and host.pending_frame_count == 0 and not delayed_broker_actions:
                        break
                    time.sleep(0.01)

            def _feed_pusher() -> None:
                try:
                    rate = max(0.1, float(self.profile.market_rate_multiplier))
                    base_interval_s = max(0.01, (max(1, self.profile.bucket_seconds) / rate))
                    end_mono = time.monotonic() + max(1.0, float(self.profile.duration_seconds))
                    idx = 1
                    last_virtual_ns = first_ns
                    while time.monotonic() < end_mono and not stop_event.is_set():
                        # Backpressure guard: track without crashing queue
                        if host.pending_frame_count >= self.profile.max_inflight_frames:
                            monitor.record_backpressure()
                            time.sleep(0.02)
                            continue
                        with state_lock:
                            # FakeBroker enforces 1 inflight broker snapshot at a time;
                            # wait briefly if previous frame has not been applied yet
                            if broker._inflight is not None:
                                pass_wait = True
                            else:
                                pass_wait = False
                        if pass_wait:
                            monitor.record_backpressure()
                            time.sleep(0.005)
                            continue

                        _, _, truth_map = rounds[idx % len(rounds)]
                        idx += 1
                        last_virtual_ns += max(1, self.profile.bucket_seconds) * 1_000_000_000
                        v_utc = ns_to_utc_iso(last_virtual_ns)

                        with state_lock:
                            clock.advance_to(v_utc)
                            for sym, t in truth_map.items():
                                market.apply_tick(
                                    wall_time=v_utc,
                                    last=t.last,
                                    contract_id=sym,
                                    bid1=t.bid1,
                                    ask1=t.ask1,
                                    day_low=t.day_low,
                                    day_high=t.day_high,
                                    candle_id=t.candle_id,
                                    native_time=v_utc,
                                    delivery_time=v_utc,
                                    fallback_book_to_last=False,
                                )
                            frame = assembler.build()
                            fid, sent_ns = host.submit_fact_frame_async(frame)
                            sent_timestamps[fid] = sent_ns
                        monitor.record_frame_sent()
                        time.sleep(base_interval_s)
                except BaseException as exc:
                    feed_errors.append(exc)
                finally:
                    stop_event.set()

            monitor.start()
            watcher_thread = Thread(target=_event_watcher, name="stress-event-watcher", daemon=True)
            pusher_thread = Thread(target=_feed_pusher, name="stress-feed-pusher", daemon=True)
            watcher_thread.start()
            pusher_thread.start()

            if self.profile.with_workbench_ui:
                ui_driver = WorkbenchUIStressDriver(
                    pipe_name=host.pipe,
                    runtime_root=host_dir,
                    monitor=monitor,
                    ops_per_minute=self.profile.workbench_ops_per_minute,
                    duration_seconds=self.profile.duration_seconds,
                    offscreen=self.profile.ui_offscreen,
                )
                ui_summary = ui_driver.run()
                stop_event.set()

            pusher_thread.join(timeout=self.profile.duration_seconds + 15.0)
            stop_event.set()
            watcher_thread.join(timeout=15.0)
            perf_summary = monitor.stop()

            if feed_errors:
                raise feed_errors[0]

            # Final Oracle safety & consistency verification after drain
            final_wb = client.workbench()
            final_brk = broker.snapshot()
            oracle.verify(
                broker_snapshot=final_brk,
                workbench_view=final_wb,
                round_seq=assembler.sequence,
                frame_id=f"stress-final-{assembler.sequence}",
            )

            # Optional baseline regression check -> DEGRADED
            if self.profile.baseline_path is not None and self.profile.baseline_path.is_file():
                base_data = json.loads(self.profile.baseline_path.read_text(encoding="utf-8"))
                base_perf = base_data.get("performance") or base_data
                baseline_diff = compare_with_baseline(
                    perf_summary,
                    base_perf,
                    thresholds=self.profile.degradation_thresholds,
                )
                if baseline_diff.get("degraded"):
                    verdict = Verdict.DEGRADED
                    failure_info = {
                        "reason": "PERFORMANCE_DEGRADED_VS_BASELINE",
                        "regressions": baseline_diff["regressions"],
                    }

        except OracleViolation as error:
            verdict = Verdict.FAIL
            failure_info = {
                "invariant_id": error.invariant_id,
                "round_seq": error.round_seq,
                "message": str(error),
                "details": error.details,
            }
        except Exception as error:
            verdict = Verdict.BLOCKED
            failure_info = {
                "error_type": type(error).__name__,
                "message": str(error),
            }
        finally:
            host.close()

        report = {
            "schema_version": "MARKET_STRESS_REPORT/1",
            "engine_version": ENGINE_VERSION,
            "scenario_name": self.profile.scenario_name,
            "verdict": verdict.value,
            "dataset_sha256": self.source.sha256,
            "git_commit": git_sha,
            "stress_profile": self.profile.to_dict(),
            "performance": perf_summary,
            "ui_stress": ui_summary,
            "baseline_comparison": baseline_diff,
            "failure": failure_info,
        }
        report_path = self.work_dir / "stress-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        if self.profile.save_baseline_path is not None and verdict == Verdict.PASS:
            save_target = Path(self.profile.save_baseline_path).resolve()
            save_target.parent.mkdir(parents=True, exist_ok=True)
            save_target.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        return report
