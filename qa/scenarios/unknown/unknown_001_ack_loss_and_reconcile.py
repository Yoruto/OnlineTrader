"""PR2 Scenario: Send UNKNOWN (ACK loss) blocks duplicate open and recovers on delayed Broker ACK."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import assert_no_active_orders, assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-UNKNOWN-001-ack-loss-delayed-recovery",
        contract=CONTRACT,
        trading_day=DAY,
    ) as scenario:
        assert scenario.broker is not None

        # Sync and arm
        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        scenario.arm_strategy()
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

        # Inject ambiguous send failure -> Runtime enters UNKNOWN on first BUY OPEN 5
        scenario.set_adapter_fault(fail_send=True)
        r_unknown = scenario.step({"wall_time": f"{DAY}T01:10:05Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        scenario.set_adapter_fault(fail_send=False)

        open_sends = [
            ev for ev in r_unknown.outbound_events
            if ev.get("event_type") == "OUTBOUND_SEND"
        ]
        if len(open_sends) != 1 or open_sends[0].get("adapter_result") != "ERROR":
            raise AssertionError(f"Expected 1 ERROR outbound send on UNKNOWN fault, got {open_sends}")
        child_id = str(open_sends[0]["request"]["child_order_id"])

        # Push 2 more ticks while UNKNOWN is unresolved -> INV-002 forbids duplicate open
        for sec in (7, 9):
            r_wait = scenario.step({"wall_time": f"{DAY}T01:10:{sec:02d}Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
            if r_wait.outbound_events:
                raise AssertionError(f"INV-002 violation: emitted order while UNKNOWN unresolved: {r_wait.outbound_events}")

        # Broker actually had the order and emits delayed ALIVE -> Runtime binds native_order_id
        scenario.broker.accept(child_id)
        scenario.step({"wall_time": f"{DAY}T01:10:11Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})

        # Broker fills all 5 lots
        scenario.broker.fill(child_id, 5, "1060")
        r_filled = scenario.step({"wall_time": f"{DAY}T01:10:13Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_filled.broker_snapshot, workbench_view=r_filled.workbench, long_qty=5)
        assert_no_active_orders(broker_snapshot=r_filled.broker_snapshot, workbench_view=r_filled.workbench)

        return scenario.finish(passed=True)
