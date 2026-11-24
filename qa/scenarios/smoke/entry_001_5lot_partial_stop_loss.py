"""PR1 Smoke Scenario: 5-lot LONG entry, 3+2 partial fills, and STOP_LOSS full close."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import (
    assert_cycle_exit_reason,
    assert_entry_authority,
    assert_no_active_orders,
    assert_positions,
)
from qa.e2e.market import FakeMarket
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "market" / "dce_p2701_entry_stop_loss.jsonl"


def run(work_dir: Path) -> dict[str, object]:
    ticks = {row["phase"]: row for row in FakeMarket.load_jsonl(FIXTURE_PATH)}

    with TradingScenario(
        work_dir,
        scenario_name="E2E-SMOKE-001-5lot-partial-stop-loss",
        contract=CONTRACT,
        trading_day=DAY,
    ) as scenario:
        assert scenario.broker is not None

        # 1. Verify initial DISARMED state over real Named Pipe before first sync
        initial = scenario.workbench()
        assert_entry_authority(initial, "DISARMED")

        # 2. Baseline sync round (price 1054 between trigger lines 1048..1060)
        r1 = scenario.step(ticks["SYNC_BASELINE"])
        if not r1.workbench["connection"]["synchronized"]:
            raise AssertionError("Runtime did not become synchronized after complete baseline frame")

        # 3. Arm strategy via public ControlServer command and declare target cap to Oracle
        arm_receipt = scenario.arm_strategy()
        if arm_receipt.get("status") != "SUCCEEDED":
            raise AssertionError(f"ARM_ENTRIES failed: {arm_receipt}")
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

        # 4. Entry trigger tick (last=1060 >= LONG line 1060) -> expect BUY OPEN 5
        r2 = scenario.step(ticks["TRIGGER_ENTRY"])
        open_sends = [
            ev for ev in r2.outbound_events
            if ev.get("event_type") == "OUTBOUND_SEND"
            and (ev.get("request") or {}).get("native_offset") == "OPEN"
        ]
        if len(open_sends) != 1:
            raise AssertionError(f"Expected exactly 1 BUY OPEN order on trigger round, got {len(open_sends)}")
        open_req = open_sends[0]["request"]
        if open_req["direction"] != "BUY" or int(open_req["quantity"]) != 5:
            raise AssertionError(f"Unexpected BUY OPEN request: {open_req}")
        open_child_id = str(open_req["child_order_id"])

        # 5. Broker accepts open order, Runtime receives ALIVE
        scenario.broker.accept(open_child_id)
        scenario.step(ticks["AFTER_ACCEPT"])

        # 6. Broker partial fill: 3 lots @ 1060 -> expect owned=3, remaining active 2
        scenario.broker.fill(open_child_id, 3, "1060")
        r4 = scenario.step(ticks["AFTER_PARTIAL_3"])
        assert_positions(broker_snapshot=r4.broker_snapshot, workbench_view=r4.workbench, long_qty=3)

        # 7. Broker remaining fill: 2 lots @ 1060 -> expect owned=5, 0 active orders
        scenario.broker.fill(open_child_id, 2, "1060")
        r5 = scenario.step(ticks["AFTER_FILL_2"])
        assert_positions(broker_snapshot=r5.broker_snapshot, workbench_view=r5.workbench, long_qty=5)
        assert_no_active_orders(broker_snapshot=r5.broker_snapshot, workbench_view=r5.workbench)

        # 8. Intermediate tick inside profit/stop bounds -> still holding 5 lots
        r6 = scenario.step(ticks["HOLD_POSITION"])
        assert_positions(broker_snapshot=r6.broker_snapshot, workbench_view=r6.workbench, long_qty=5)

        # 9. Stop-loss tick (last=1052: move=-8 <= -stop_offset 8) -> expect SELL CLOSE 5
        r7 = scenario.step(ticks["TRIGGER_STOP_LOSS"])
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5, exit_target=0)
        assert_cycle_exit_reason(r7.workbench, CONTRACT, "STOP_LOSS")
        close_sends = [
            ev for ev in r7.outbound_events
            if ev.get("event_type") == "OUTBOUND_SEND"
            and (ev.get("request") or {}).get("native_offset") != "OPEN"
        ]
        if len(close_sends) != 1:
            raise AssertionError(f"Expected exactly 1 close order on STOP_LOSS, got {len(close_sends)}")
        close_req = close_sends[0]["request"]
        if close_req["direction"] != "SELL" or int(close_req["quantity"]) != 5:
            raise AssertionError(f"Unexpected close request: {close_req}")
        close_child_id = str(close_req["child_order_id"])

        # 10. Broker accepts close order, then fills all 5 lots @ 1052
        scenario.broker.accept(close_child_id)
        scenario.step(ticks["AFTER_CLOSE_ACCEPT"])
        scenario.broker.fill(close_child_id, 5, "1052")
        r9 = scenario.step(ticks["AFTER_CLOSE_FILL"])

        # 11. Final verification: flat position, 0 active orders
        assert_positions(broker_snapshot=r9.broker_snapshot, workbench_view=r9.workbench, long_qty=0, short_qty=0)
        assert_no_active_orders(broker_snapshot=r9.broker_snapshot, workbench_view=r9.workbench)

        return scenario.finish(passed=True)
