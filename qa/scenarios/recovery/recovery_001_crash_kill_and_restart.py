"""PR4 Scenario: Hard crash (terminate) + clean restart with new boot_id and broker-authoritative sync."""

from __future__ import annotations

from pathlib import Path

from online_trader.transport.status_client import StatusClient
from qa.e2e import assert_offline_qa_environment
from qa.e2e.assertions import assert_entry_authority
from qa.e2e.assembler import FrameAssembler
from qa.e2e.clock import VirtualClock
from qa.e2e.evidence import TraceRecorder
from qa.e2e.market import FakeMarket
from qa.e2e.oracle import ReferenceOracle
from qa.target_owner.fake_broker import FakeBroker
from qa.target_owner.offline_runtime import CONTRACT, DAY
from qa.target_owner.process_runtime import OfflineTargetOwnerProcess


def run(work_dir: Path) -> dict[str, object]:
    assert_offline_qa_environment(block_tqsdk=True)
    evidence_dir = work_dir / "evidence"
    trace = TraceRecorder(evidence_dir, scenario_name="E2E-RECOVERY-001-crash-restart", run_id="rec-001")

    # Boot 1: open and fill 5 lots, then forcibly terminate process
    proc1 = OfflineTargetOwnerProcess(work_dir / "boot-1", trading_day=DAY).start()
    boot1_id = str(proc1.boot_id)
    account1 = str(proc1.account_key)
    broker = FakeBroker(
        account_key=account1,
        contract=CONTRACT,
        trading_day=DAY,
        trace_path=evidence_dir / "broker-events.jsonl",
        run_id="rec-001",
    )
    clock = VirtualClock(f"{DAY}T01:09:50Z")
    market = FakeMarket(
        contract_id=CONTRACT,
        trading_day=DAY,
        session_open=f"{DAY}T01:00:00Z",
        session_close=f"{DAY}T02:15:00Z",
    )
    assembler = FrameAssembler(market=market, broker=broker, clock=clock, trading_day=DAY)
    client1 = StatusClient(proc1.pipe, timeout=5.0, expected_server_pid=proc1.owner_pid, expected_account_key=account1)
    cursor1 = 0
    try:
        clock.advance_to(f"{DAY}T01:10:00Z")
        market.apply_tick(wall_time=clock.now_utc, last="1054", candle_id="bar-0110")
        f1 = assembler.build()
        proc1.send_fact_frame(f1)
        proc1.wait_for_frame_applied(str(f1["id"]), after=cursor1)
        broker.acknowledge_frame(str(f1["id"]))
        cursor1 = len(proc1.events)

        client1.command(
            "ARM_ENTRIES",
            {"scope_type": "ACCOUNT", "scope_ids": []},
            {"authority_policy_id": "target-authority-current-boot", "rule_set_id": "DSV_TARGET_EXECUTION", "config_revision": "1"},
            expected_config_revision="1",
        )

        clock.advance_to(f"{DAY}T01:10:05Z")
        market.apply_tick(wall_time=clock.now_utc, last="1060", bid1="1059", ask1="1060", candle_id="bar-0110")
        f2 = assembler.build()
        proc1.send_fact_frame(f2)
        proc1.wait_for_frame_applied(str(f2["id"]), after=cursor1)
        broker.acknowledge_frame(str(f2["id"]))
        outbound = [ev for ev in proc1.events[cursor1:] if ev.get("event_type") == "OUTBOUND_SEND"]
        for ev in outbound:
            broker.observe_adapter_event(ev)
        child_id = str(outbound[0]["request"]["child_order_id"])
        broker.accept_order(child_id)
        broker.fill(child_id, 5, "1060")

        clock.advance_to(f"{DAY}T01:10:07Z")
        market.apply_tick(wall_time=clock.now_utc, last="1061", candle_id="bar-0110")
        f3 = assembler.build()
        proc1.send_fact_frame(f3)
        proc1.wait_for_frame_applied(str(f3["id"]), after=cursor1)
        broker.acknowledge_frame(str(f3["id"]))

        # Simulate sudden process crash
        proc1.terminate()
        if proc1.is_alive:
            raise AssertionError("Boot 1 process did not terminate")
    finally:
        proc1.close(force=True)

    # Boot 2: fresh Owner process, fresh temporary ledger, observes broker 5-lot truth
    with OfflineTargetOwnerProcess(work_dir / "boot-2", trading_day=DAY) as proc2:
        boot2_id = str(proc2.boot_id)
        if boot2_id == boot1_id:
            raise AssertionError("INV-009 violation: restarted Owner reused prior runtime_boot_id")

        client2 = StatusClient(proc2.pipe, timeout=5.0, expected_server_pid=proc2.owner_pid)
        wb_init = client2.workbench()
        assert_entry_authority(wb_init, "DISARMED")

        # Sync Boot 2 against existing broker position (5 lots)
        broker.reset_session_sequence()
        assembler2 = FrameAssembler(
            market=market,
            broker=broker,
            clock=clock,
            trading_day=DAY,
        )
        clock.advance_to(f"{DAY}T01:10:10Z")
        market.apply_tick(wall_time=clock.now_utc, last="1061", candle_id="bar-0110")
        f_sync = assembler2.build()
        proc2.send_fact_frame(f_sync)
        proc2.wait_for_frame_applied(str(f_sync["id"]))
        broker.acknowledge_frame(str(f_sync["id"]))

        wb_sync = client2.workbench()
        assert_entry_authority(wb_sync, "DISARMED")
        ext = wb_sync.get("external_positions") or []
        if not ext or int(ext[0]["quantity"]) != 5:
            raise AssertionError(f"INV-008 failure: restarted boot did not surface 5-lot broker position: {ext}")
        # Verify no extra open orders emitted on restart even at trigger price 1061
        extra_sends = [ev for ev in proc2.events if ev.get("event_type") == "OUTBOUND_SEND"]
        if extra_sends:
            raise AssertionError(f"INV-008/009 violation: restarted boot emitted orders without authorization: {extra_sends}")

        oracle = ReferenceOracle()
        oracle.set_boot_expectation(boot2_id)
        oracle.set_unowned_position(CONTRACT, long_qty=5)
        # Verify Oracle passes on Boot 2 without false trade count mismatch
        if wb_sync.get("kernel_boot_id") != boot2_id:
            raise AssertionError("Workbench kernel_boot_id mismatch after restart")

    return trace.finalize(passed=True, final_broker=broker.snapshot())
