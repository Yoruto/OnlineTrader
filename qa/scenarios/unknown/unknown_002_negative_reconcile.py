"""PR2 Scenario: UNKNOWN order cleared only via complete-snapshot negative reconciliation."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import assert_no_active_orders, assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-UNKNOWN-002-negative-reconcile",
        contract=CONTRACT,
        trading_day=DAY,
    ) as scenario:
        assert scenario.broker is not None

        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        scenario.arm_strategy()
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

        scenario.set_adapter_fault(fail_send=True)
        r_unknown = scenario.step({"wall_time": f"{DAY}T01:10:05Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        scenario.set_adapter_fault(fail_send=False)
        child_id = str(r_unknown.outbound_events[0]["request"]["child_order_id"])

        # Complete broker snapshot proves child_id is absent on broker -> negative reconciliation
        scenario.broker.confirm_absent(child_id)
        r_reconciled = scenario.step(
            {"wall_time": f"{DAY}T01:10:07Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"},
            broker_order_snapshot_complete=True,
            absent_orders=[child_id],
        )
        if r_reconciled.outbound_events:
            raise AssertionError(
                f"Negative reconciliation must stop the consumed entry rather than auto-reopen: {r_reconciled.outbound_events}"
            )
        assert_positions(broker_snapshot=r_reconciled.broker_snapshot, workbench_view=r_reconciled.workbench, long_qty=0)
        assert_no_active_orders(broker_snapshot=r_reconciled.broker_snapshot, workbench_view=r_reconciled.workbench)

        return scenario.finish(passed=True)
