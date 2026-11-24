"""Round-by-round event-driven TradingScenario orchestrator (PR1)."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import time
from uuid import uuid4

from online_trader.transport.status_client import StatusClient
from qa.e2e import assert_offline_qa_environment
from qa.e2e.assembler import FrameAssembler
from qa.e2e.clock import VirtualClock
from qa.e2e.evidence import TraceRecorder
from qa.e2e.market import FakeMarket
from qa.e2e.oracle import OracleViolation, ReferenceOracle
from qa.target_owner.fake_broker import FakeBroker
from qa.target_owner.offline_runtime import CONTRACT, DAY
from qa.target_owner.process_runtime import OfflineTargetOwnerProcess


@dataclass
class StepOutcome:
    round_seq: int
    frame_id: str
    wall_time: str
    applied_event: dict[str, object]
    outbound_events: list[dict[str, object]]
    workbench: dict[str, object]
    broker_snapshot: dict[str, object]


class BrokerFacade:
    """Scenario-facing facade over FakeBroker that also logs actions to TraceRecorder."""

    def __init__(self, broker: FakeBroker, trace: TraceRecorder) -> None:
        self._broker = broker
        self._trace = trace

    def snapshot(self) -> dict[str, object]:
        return self._broker.snapshot()

    def accept(self, child_order_id: str, *, native_order_id: str | None = None) -> dict[str, object]:
        fact = self._broker.accept_order(child_order_id, native_order_id=native_order_id)
        self._trace.record("BROKER_ACTION", action="ACCEPT", child_order_id=child_order_id, detail=fact)
        return fact

    def reject(self, child_order_id: str, *, native_order_id: str | None = None) -> dict[str, object]:
        fact = self._broker.reject_order(child_order_id, native_order_id=native_order_id)
        self._trace.record("BROKER_ACTION", action="REJECT", child_order_id=child_order_id, detail=fact)
        return fact

    def fill(
        self,
        child_order_id: str,
        quantity: int,
        price: str | int,
        *,
        native_trade_id: str | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        res = self._broker.fill(child_order_id, quantity, price, native_trade_id=native_trade_id)
        self._trace.record(
            "BROKER_ACTION",
            action="FILL",
            child_order_id=child_order_id,
            detail={"quantity": quantity, "price": str(price), "trade": res[1]},
        )
        return res

    def confirm_cancel(self, child_order_id: str) -> dict[str, object]:
        fact = self._broker.confirm_cancel(child_order_id)
        self._trace.record("BROKER_ACTION", action="CONFIRM_CANCEL", child_order_id=child_order_id, detail=fact)
        return fact

    def late_fill(
        self,
        child_order_id: str,
        quantity: int,
        price: str | int,
        *,
        native_trade_id: str | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        res = self._broker.late_fill(child_order_id, quantity, price, native_trade_id=native_trade_id)
        self._trace.record(
            "BROKER_ACTION",
            action="LATE_FILL",
            child_order_id=child_order_id,
            detail={"quantity": quantity, "price": str(price), "trade": res[1]},
        )
        return res

    def replay_trade_fact(self, native_trade_id: str) -> dict[str, object]:
        fact = self._broker.replay_trade_fact(native_trade_id)
        self._trace.record("BROKER_ACTION", action="REPLAY_TRADE", child_order_id=fact.get("child_order_id"), detail=fact)
        return fact

    def report_order(self, child_order_id: str, status: str, filled_qty: int) -> dict[str, object]:
        fact = self._broker.report_order(child_order_id, status, filled_qty)
        self._trace.record("BROKER_ACTION", action="REPORT_ORDER", child_order_id=child_order_id, detail=fact)
        return fact

    def confirm_absent(self, child_order_id: str) -> None:
        self._broker.confirm_absent(child_order_id)
        self._trace.record("BROKER_ACTION", action="CONFIRM_ABSENT", child_order_id=child_order_id, detail={})

    def report_trade(
        self,
        child_order_id: str,
        trade_qty: int,
        price: str | int,
        *,
        native_trade_id: str | None = None,
    ) -> dict[str, object]:
        fact = self._broker.report_trade(child_order_id, trade_qty, price, native_trade_id=native_trade_id)
        self._trace.record("BROKER_ACTION", action="REPORT_TRADE", child_order_id=child_order_id, detail=fact)
        return fact


class TradingScenario:
    """Drives an isolated QA Owner process round-by-round against FakeMarket + FakeBroker."""

    def __init__(
        self,
        root: str | Path,
        *,
        scenario_name: str,
        contract: str = CONTRACT,
        trading_day: str = DAY,
        initial_utc: str = f"{DAY}T01:09:50Z",
        session_open: str = f"{DAY}T01:00:00Z",
        session_close: str = f"{DAY}T02:15:00Z",
        day_low: str = "1020",
        day_high: str = "1088",
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 10.0,
        profile_record: dict[str, object] | None = None,
        use_production_tick_wrapper: bool = False,
    ) -> None:
        assert_offline_qa_environment(block_tqsdk=True)
        self.root = Path(root).resolve()
        self.scenario_name = scenario_name
        self.contract = contract
        self.trading_day = trading_day
        self.run_id = f"e2e-{uuid4().hex[:12]}"
        self.process_dir = self.root / "owner-process"
        self.evidence_dir = self.root / "evidence"
        self.process = OfflineTargetOwnerProcess(
            self.process_dir,
            startup_timeout=startup_timeout,
            shutdown_timeout=shutdown_timeout,
            profile_record=profile_record,
            trading_day=trading_day,
            use_production_tick_wrapper=use_production_tick_wrapper,
        )
        self.clock = VirtualClock(initial_utc)
        self.market = FakeMarket(
            contract_id=contract,
            trading_day=trading_day,
            session_open=session_open,
            session_close=session_close,
            day_low=day_low,
            day_high=day_high,
        )
        self.oracle = ReferenceOracle()
        self.trace = TraceRecorder(self.evidence_dir, scenario_name=scenario_name, run_id=self.run_id)
        self._raw_broker: FakeBroker | None = None
        self.broker: BrokerFacade | None = None
        self.assembler: FrameAssembler | None = None
        self.client: StatusClient | None = None
        self._event_cursor = 0
        self._observed_outbound: list[dict[str, object]] = []
        self._started = False
        self._closed = False
        self._failure_record: dict[str, object] | None = None

    def start(self) -> "TradingScenario":
        if self._started:
            raise RuntimeError("TradingScenario already started")
        self.process.start()
        self._started = True
        assert self.process.account_key is not None
        assert self.process.boot_id is not None
        self._raw_broker = FakeBroker(
            account_key=self.process.account_key,
            contract=self.contract,
            trading_day=self.trading_day,
            trace_path=self.evidence_dir / "broker-events.jsonl",
            run_id=self.run_id,
        )
        self.broker = BrokerFacade(self._raw_broker, self.trace)
        self.assembler = FrameAssembler(
            market=self.market,
            broker=self._raw_broker,
            clock=self.clock,
            trading_day=self.trading_day,
        )
        self.client = StatusClient(
            self.process.pipe,
            timeout=5.0,
            expected_server_pid=self.process.owner_pid,
            expected_account_key=self.process.account_key,
            expected_kernel_boot_id=self.process.boot_id,
        )
        self.oracle.set_boot_expectation(self.process.boot_id, config_revision="1")
        self.trace.record(
            "SCENARIO_STARTED",
            owner_pid=self.process.owner_pid,
            runtime_boot_id=self.process.boot_id,
            account_key=self.process.account_key,
            pipe=self.process.pipe,
        )
        return self

    def workbench(self) -> dict[str, object]:
        assert self.client is not None
        return self.client.workbench()

    def _drain_outbound_events(self, *, frame_id: str | None = None, command_id: str | None = None) -> list[dict[str, object]]:
        """Collect all new OUTBOUND_SEND / OUTBOUND_CANCEL events since the last cursor."""
        assert self._raw_broker is not None
        events = self.process.events
        new_events = events[self._event_cursor:]
        self._event_cursor = len(events)
        collected: list[dict[str, object]] = []
        for event in new_events:
            if event.get("event_type") in {"OUTBOUND_SEND", "OUTBOUND_CANCEL"}:
                self._raw_broker.observe_adapter_event(event)
                tagged = deepcopy(event)
                if frame_id is not None:
                    tagged["causal_frame_id"] = frame_id
                if command_id is not None:
                    tagged["causal_command_id"] = command_id
                self._observed_outbound.append(tagged)
                collected.append(tagged)
        return collected

    def arm_strategy(self) -> dict[str, object]:
        assert self.client is not None
        wb = self.workbench()
        config_rev = str((wb.get("configuration") or {}).get("config_revision", "1"))
        cmd_id = f"arm-{uuid4().hex[:8]}"
        receipt = self.client.command(
            "ARM_ENTRIES",
            {"scope_type": "ACCOUNT", "scope_ids": []},
            {
                "authority_policy_id": "target-authority-current-boot",
                "rule_set_id": "DSV_TARGET_EXECUTION",
                "config_revision": config_rev,
            },
            command_id=cmd_id,
            expected_config_revision=config_rev,
        )
        self._drain_outbound_events(command_id=cmd_id)
        if receipt.get("status") == "SUCCEEDED":
            self.oracle.set_authority_expectation("ARMED")
        self.trace.record("COMMAND", operation="ARM_ENTRIES", command_id=cmd_id, receipt_status=receipt.get("status"))
        return receipt

    def disarm_strategy(self) -> dict[str, object]:
        assert self.client is not None
        wb = self.workbench()
        config_rev = str((wb.get("configuration") or {}).get("config_revision", "1"))
        cmd_id = f"disarm-{uuid4().hex[:8]}"
        receipt = self.client.command(
            "PAUSE_NEW_ENTRIES",
            {"scope_type": "ACCOUNT", "scope_ids": []},
            {"config_revision": config_rev},
            command_id=cmd_id,
            expected_config_revision=config_rev,
        )
        self._drain_outbound_events(command_id=cmd_id)
        if receipt.get("status") == "SUCCEEDED":
            self.oracle.set_authority_expectation("DISARMED")
        self.trace.record("COMMAND", operation="PAUSE_NEW_ENTRIES", command_id=cmd_id, receipt_status=receipt.get("status"))
        return receipt

    def set_adapter_fault(self, *, fail_send: bool = False) -> dict[str, object]:
        """Inject or clear ambiguous send failure in the QA adapter."""
        return self.process.set_adapter_fault(fail_send=fail_send)

    def inject_step_error(self, error_code: str, *, timeout: float = 5.0) -> dict[str, object]:
        """Inject one TraderError(error_code) into the next owner_tick via production execute_owner_tick."""
        cursor = self.process.event_cursor
        self.process.set_adapter_fault(fail_send=False, next_step_error_code=error_code)
        return self.process.wait_for_event("STEP_ERROR_HANDLED", timeout=timeout, after=cursor)

    def roll_trading_day(
        self,
        new_trading_day: str,
        *,
        session_open: str,
        session_close: str,
    ) -> None:
        """Advance FakeBroker, FakeMarket, FrameAssembler and scenario trading_day together."""
        assert self._raw_broker is not None and self.assembler is not None
        self._raw_broker.roll_trading_day(new_trading_day)
        self.market.roll_trading_day(
            new_trading_day,
            session_open=session_open,
            session_close=session_close,
        )
        self.assembler.roll_trading_day(new_trading_day)
        self.trading_day = new_trading_day
        self.trace.record("TRADING_DAY_ROLLED", trading_day=new_trading_day)

    def step(
        self,
        tick_spec: dict[str, object] | None = None,
        *,
        complete: bool = True,
        synchronized: bool | None = None,
        broker_order_snapshot_complete: bool | None = None,
        absent_orders: list[str] | None = None,
        health_reason: str | None = None,
        on_round: Callable[[StepOutcome], None] | None = None,
    ) -> StepOutcome:
        """Execute one deterministic V3.1 round: clock -> market -> broker -> owner -> oracle."""
        assert self._raw_broker is not None and self.assembler is not None
        spec = dict(tick_spec or {})

        # 1. Advance virtual clock
        if "wall_time" in spec:
            self.clock.advance_to(str(spec["wall_time"]))
        else:
            advance_seconds = int(spec.get("advance_seconds", 1))
            self.clock.advance(seconds=advance_seconds)

        # 2. Update FakeMarket
        if "last" in spec or spec.get("drop_tick"):
            self.market.apply_tick(
                wall_time=self.clock.now_utc,
                last=spec.get("last"),
                contract_id=spec.get("contract_id"),  # type: ignore[arg-type]
                bid1=spec.get("bid1"),
                ask1=spec.get("ask1"),
                day_low=spec.get("day_low"),
                day_high=spec.get("day_high"),
                candle_id=spec.get("candle_id"),  # type: ignore[arg-type]
                opening_session=spec.get("opening_session"),  # type: ignore[arg-type]
                market_session_active=spec.get("market_session_active"),  # type: ignore[arg-type]
                drop_tick=bool(spec.get("drop_tick", False)),
            )

        # 3 & 4. Snapshot pending FakeBroker facts and assemble FACT_FRAME
        frame = self.assembler.build(
            complete=complete,
            synchronized=synchronized,
            broker_order_snapshot_complete=broker_order_snapshot_complete,
            absent_orders=absent_orders,
            health_reason=health_reason,
        )
        frame_id = str(frame["id"])
        round_seq = int(frame["sequence"])

        # 5. Deliver frame to real QA Owner subprocess and wait for FACT_FRAME_APPLIED
        cursor_before = self._event_cursor
        self.process.send_fact_frame(frame)
        applied = self.process.wait_for_frame_applied(frame_id, after=cursor_before)
        self._raw_broker.acknowledge_frame(frame_id)

        # 6 & 7. Collect outbound effects for this round and feed FakeBroker
        outbound = self._drain_outbound_events(frame_id=frame_id)

        # 8. Read public Workbench projection and run independent Oracle verification
        wb = self.workbench()
        broker_snap = self._raw_broker.snapshot()
        market_row = next((m for m in (wb.get("markets") or []) if m.get("contract_id") == self.contract), {})
        self.trace.record(
            "ROUND",
            round_seq=round_seq,
            frame_id=frame_id,
            wall_time=frame["wall_time"],
            last=market_row.get("last"),
            entry_authority=wb.get("entry_authority"),
            workbench_owned=(wb.get("current_responsibility") or {}).get("owned_quantity"),
            broker_positions=broker_snap["positions"],
            entry_reasons=market_row.get("entry_reasons"),
            outbound=[
                {
                    "event_type": ev.get("event_type"),
                    "child_order_id": (ev.get("request") or {}).get("child_order_id"),
                    "direction": (ev.get("request") or {}).get("direction"),
                    "native_offset": (ev.get("request") or {}).get("native_offset"),
                    "quantity": (ev.get("request") or {}).get("quantity"),
                    "limit_price": (ev.get("request") or {}).get("limit_price"),
                }
                for ev in outbound
            ],
        )
        try:
            self.oracle.verify(
                broker_snapshot=broker_snap,
                workbench_view=wb,
                round_seq=round_seq,
                frame_id=frame_id,
            )
        except OracleViolation as error:
            self._failure_record = {
                "invariant_id": error.invariant_id,
                "round_seq": error.round_seq,
                "frame_id": error.frame_id,
                "message": str(error),
                "details": error.details,
            }
            self.trace.record("FAILURE", **self._failure_record)
            raise

        outcome = StepOutcome(
            round_seq=round_seq,
            frame_id=frame_id,
            wall_time=str(frame["wall_time"]),
            applied_event=applied,
            outbound_events=outbound,
            workbench=wb,
            broker_snapshot=broker_snap,
        )
        if on_round is not None:
            on_round(outcome)
        return outcome

    def wait_until(
        self,
        predicate: Callable[[dict[str, object], dict[str, object]], bool],
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.05,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Poll public Workbench and FakeBroker snapshots until predicate holds."""
        assert self._raw_broker is not None
        deadline = time.monotonic() + timeout
        while True:
            wb = self.workbench()
            brk = self._raw_broker.snapshot()
            if predicate(wb, brk):
                return wb, brk
            if time.monotonic() >= deadline:
                raise TimeoutError("wait_until predicate did not become true within timeout")
            time.sleep(poll_interval)

    def find_outbound(
        self,
        *,
        event_type: str = "OUTBOUND_SEND",
        native_offset: str | None = None,
        direction: str | None = None,
    ) -> list[dict[str, object]]:
        matches: list[dict[str, object]] = []
        for ev in self._observed_outbound:
            if ev.get("event_type") != event_type:
                continue
            req = ev.get("request") or {}
            if native_offset is not None and req.get("native_offset") != native_offset:
                continue
            if direction is not None and req.get("direction") != direction:
                continue
            matches.append(ev)
        return matches

    def finish(self, *, passed: bool = True, failure_message: str | None = None) -> dict[str, object]:
        final_wb = None
        final_brk = None
        if self._started and self.process.is_alive:
            try:
                final_wb = self.workbench()
            except Exception:
                pass
        if self._raw_broker is not None:
            final_brk = self._raw_broker.snapshot()
        self.close()
        if not passed and self._failure_record is None:
            self._failure_record = {"message": failure_message or "Scenario assertion failed"}
        evidence_files = {
            "inputs": str(self.process_dir / "inputs.jsonl"),
            "observed_events": str(self.process_dir / "observed-events.jsonl"),
            "broker_events": str(self.evidence_dir / "broker-events.jsonl"),
            "scenario_trace": str(self.trace.trace_path),
            "report_json": str(self.trace.report_json_path),
            "report_text": str(self.trace.report_text_path),
        }
        return self.trace.finalize(
            passed=passed,
            failure=self._failure_record,
            final_broker=final_brk,
            final_workbench=final_wb,
            evidence_files=evidence_files,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            try:
                self.process.close()
            finally:
                if self.process.is_alive:
                    raise RuntimeError(f"Orphan QA Owner child process still alive: pid={self.process.pid}")

    def __enter__(self) -> "TradingScenario":
        return self.start()

    def __exit__(self, exc_type, exc_val, _tb) -> None:
        if not self._closed:
            self.finish(passed=exc_type is None, failure_message=None if exc_val is None else str(exc_val))
