"""PR2 Scenario: Reprice cancel race + late fill after cancel + remaining quantity replacement."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import assert_no_active_orders, assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-CANCEL-001-reprice-and-late-fill",
        contract=CONTRACT,
        trading_day=DAY,
    ) as scenario:
        assert scenario.broker is not None

        # Sync & arm
        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        scenario.arm_strategy()
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

        # Trigger BUY OPEN 5 at ask1=1060
        r_open = scenario.step({"wall_time": f"{DAY}T01:10:05Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        child_1 = str(r_open.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(child_1)
        scenario.step({"wall_time": f"{DAY}T01:10:06Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})

        # Price moves to ask1=1061 (within cap 1061) -> Controller emits OUTBOUND_CANCEL (REPRICE)
        r_reprice = scenario.step({"wall_time": f"{DAY}T01:10:07Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        cancels = [ev for ev in r_reprice.outbound_events if ev.get("event_type") == "OUTBOUND_CANCEL"]
        sends = [ev for ev in r_reprice.outbound_events if ev.get("event_type") == "OUTBOUND_SEND"]
        if len(cancels) != 1 or sends:
            raise AssertionError(f"Expected 1 REPRICE cancel and 0 sends while cancel pending, got cancels={cancels}, sends={sends}")

        # Next tick while cancel is STILL pending -> INV-003 forbids sending replacement yet
        r_pending = scenario.step({"wall_time": f"{DAY}T01:10:08Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        if r_pending.outbound_events:
            raise AssertionError(f"INV-003 violation: sent order while cancel unconfirmed: {r_pending.outbound_events}")

        # Broker confirms cancel AND reports a 3-lot late fill on child_1
        scenario.broker.confirm_cancel(child_1)
        scenario.broker.late_fill(child_1, 3, "1060")
        r_late = scenario.step({"wall_time": f"{DAY}T01:10:09Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_late.broker_snapshot, workbench_view=r_late.workbench, long_qty=3)

        # Runtime should send replacement BUY OPEN for remaining 2 lots (5 - 3 = 2)
        sends_after = [ev for ev in r_late.outbound_events if ev.get("event_type") == "OUTBOUND_SEND"]
        if len(sends_after) != 1 or int(sends_after[0]["request"]["quantity"]) != 2:
            raise AssertionError(f"Expected replacement BUY OPEN qty=2 after 3-lot late fill, got {sends_after}")
        child_2 = str(sends_after[0]["request"]["child_order_id"])

        scenario.broker.accept(child_2)
        scenario.step({"wall_time": f"{DAY}T01:10:10Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        scenario.broker.fill(child_2, 2, "1061")
        r_done = scenario.step({"wall_time": f"{DAY}T01:10:11Z", "last": "1062", "bid1": "1061", "ask1": "1062", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_done.broker_snapshot, workbench_view=r_done.workbench, long_qty=5)
        assert_no_active_orders(broker_snapshot=r_done.broker_snapshot, workbench_view=r_done.workbench)

        return scenario.finish(passed=True)
