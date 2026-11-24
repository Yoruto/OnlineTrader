"""PR3 DesktopE2E Scenario: Real Qt Workbench + QADesktopHost + ViewProcess lifecycle & trade."""

from __future__ import annotations

from pathlib import Path
import time
from uuid import uuid4

from online_trader.runtime.instance import runtime_owner_exists
from online_trader.runtime.instance_registry import read_runtime_instance
from online_trader.runtime.operator_api import RuntimeWorkbenchClient
from online_trader.transport.runtime_view import RuntimeViewClient
from qa.e2e import assert_offline_qa_environment
from qa.e2e.assembler import FrameAssembler
from qa.e2e.clock import VirtualClock
from qa.e2e.desktop_driver import DesktopWorkbenchDriver
from qa.e2e.desktop_host import QADesktopHost
from qa.e2e.evidence import TraceRecorder
from qa.e2e.market import FakeMarket
from qa.e2e.oracle import ReferenceOracle
from qa.target_owner.fake_broker import FakeBroker
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    assert_offline_qa_environment(block_tqsdk=True)
    host_dir = work_dir / "desktop-host"
    evidence_dir = work_dir / "evidence"
    trace = TraceRecorder(evidence_dir, scenario_name="E2E-DESKTOP-001-workbench-lifecycle", run_id="desktop-001")

    host = QADesktopHost(host_dir, trading_day=DAY).start()
    try:
        assert host.account_key and host.boot_id
        broker = FakeBroker(
            account_key=host.account_key,
            contract=CONTRACT,
            trading_day=DAY,
            trace_path=evidence_dir / "broker-events.jsonl",
            run_id="desktop-001",
        )
        clock = VirtualClock(f"{DAY}T01:09:50Z")
        market = FakeMarket(
            contract_id=CONTRACT,
            trading_day=DAY,
            session_open=f"{DAY}T01:00:00Z",
            session_close=f"{DAY}T02:15:00Z",
        )
        assembler = FrameAssembler(market=market, broker=broker, clock=clock, trading_day=DAY)
        oracle = ReferenceOracle()
        oracle.set_boot_expectation(host.boot_id)
        view_client = RuntimeViewClient(host.pipe)
        cursor = 0
        expected_seq = 1

        def read_workbench_view(*, min_seq: int = 0) -> dict[str, object]:
            deadline = time.monotonic() + 5.0
            while True:
                msg = view_client.read()
                if msg.get("kind") == "BASELINE":
                    view = msg["body"]["view"]
                    if int(view.get("checkpoint_event_seq", 0)) >= min_seq:
                        return view
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for RuntimeViewClient baseline (min_seq={min_seq})")
                time.sleep(0.1)

        def push_round(wall_time: str, last: str) -> list[dict[str, object]]:
            nonlocal cursor, expected_seq
            clock.advance_to(wall_time)
            market.apply_tick(wall_time=clock.now_utc, last=last, candle_id="bar-0110")
            frame = assembler.build()
            fid = str(frame["id"])
            host.send_fact_frame(frame)
            host.wait_for_frame_applied(fid, after=cursor)
            broker.acknowledge_frame(fid)
            expected_seq += 1
            events = host.events[cursor:]
            cursor = len(host.events)
            outbound = [ev for ev in events if ev.get("event_type") in {"OUTBOUND_SEND", "OUTBOUND_CANCEL"}]
            for ev in outbound:
                broker.observe_adapter_event(ev)
            wb = read_workbench_view(min_seq=expected_seq)
            oracle.verify(broker_snapshot=broker.snapshot(), workbench_view=wb, round_seq=int(frame["sequence"]), frame_id=fid)
            return outbound

        # Sync first so Workbench displays READY/DISARMED
        push_round(f"{DAY}T01:10:00Z", "1054")

        with DesktopWorkbenchDriver(host.pipe) as gui:
            # 1. Click real Qt button 'arm' (F5) -> Runtime transitions to ARMED
            gui.click_button("arm")
            gui.wait_until(lambda s: s.get("view_entry_authority") == "ARMED", timeout=8.0)
            expected_seq += 1
            oracle.set_authority_expectation("ARMED")
            oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

            # 2. Trigger BUY OPEN 5 -> Broker accepts & fills -> Qt displays position
            sends = push_round(f"{DAY}T01:10:05Z", "1060")
            if len(sends) != 1:
                raise AssertionError(f"Expected 1 BUY OPEN send, got {sends}")
            child_id = str(sends[0]["request"]["child_order_id"])
            broker.accept_order(child_id)
            push_round(f"{DAY}T01:10:06Z", "1060")
            broker.fill(child_id, 5, "1060")
            push_round(f"{DAY}T01:10:07Z", "1061")
            gui.refresh()
            gui.wait_until(lambda s: len(s.get("protection_rows", [])) >= 1, timeout=8.0)

            # 3. Close Workbench window -> verify Runtime keeps running (INV-012: no extra orders)
            visible_after_close, tray_ok = gui.close_to_tray()
            if tray_ok and visible_after_close:
                raise AssertionError("Expected Workbench window to hide to tray on close")
            extra = push_round(f"{DAY}T01:10:09Z", "1061")
            if extra:
                raise AssertionError(f"INV-012 violation: closing Workbench emitted orders: {extra}")

            # 4. Reopen Workbench, click real Pause button, then stop-loss flat before clean exit
            if tray_ok and not visible_after_close:
                gui.reopen_from_tray()
            gui.click_button("pause")
            gui.wait_until(lambda s: s.get("view_entry_authority") == "DISARMED", timeout=8.0)
            expected_seq += 1
            oracle.set_authority_expectation("DISARMED")

            # Stop-loss exit so safe shutdown proceeds without modal confirmation dialog
            close_sends = push_round(f"{DAY}T01:10:15Z", "1052")
            oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5, exit_target=0)
            close_id = str(close_sends[0]["request"]["child_order_id"])
            broker.accept_order(close_id)
            push_round(f"{DAY}T01:10:16Z", "1052")
            broker.fill(close_id, 5, "1052")
            push_round(f"{DAY}T01:10:17Z", "1051")

            # 5. Controlled Workbench / Runtime exit -> waits for Host + ViewProcess clean exit
            ext_reg = read_runtime_instance()
            external_runtime_present = runtime_owner_exists() and (
                ext_reg is None or ext_reg.get("pipe") != host.pipe
            )
            if external_runtime_present:
                # Another unrelated Runtime owns the host-wide RuntimeLock; verify
                # FLATTEN_THEN_STOP + Host/ViewProcess exit directly without touching it.
                wb_view = read_workbench_view(min_seq=expected_seq)
                RuntimeWorkbenchClient(host.pipe).request(
                    "submit",
                    {
                        "command_id": f"exit-{uuid4().hex}",
                        "operation": "FLATTEN_THEN_STOP",
                        "scope": {"scope_type": "ACCOUNT", "scope_ids": []},
                        "parameters": {"execution_policy_id": "target-execution-current-boot"},
                        "binding": {
                            "account_key": wb_view["account_key"],
                            "kernel_boot_id": wb_view["kernel_boot_id"],
                            "config_revision": str(wb_view["configuration"]["config_revision"]),
                        },
                    },
                )
                host.wait_for_exit(timeout=15.0)
                gui.close(force=True)
                trace.record("EXTERNAL_RUNTIME_DETECTED", external_pid=(ext_reg or {}).get("pid"))
            else:
                gui.exit_workbench(timeout=20.0)
                host.wait_for_exit(timeout=15.0)
            if host.is_alive:
                raise AssertionError("QADesktopHost process remained alive after Workbench exit")
    finally:
        host.close(force=True)

    return trace.finalize(passed=True)
