"""PR2 Scenario: Duplicate trade fact deduplication (INV-004) and exit rejection recovery (INV-006/007)."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import assert_cycle_exit_reason, assert_no_active_orders, assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-EXIT-001-reject-recovery-and-dup-trade",
        contract=CONTRACT,
        trading_day=DAY,
    ) as scenario:
        assert scenario.broker is not None

        # Sync, arm, and open 5 lots
        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        scenario.arm_strategy()
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

        r_open = scenario.step({"wall_time": f"{DAY}T01:10:05Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        open_id = str(r_open.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(open_id)
        scenario.step({"wall_time": f"{DAY}T01:10:06Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        _, trade_fact = scenario.broker.fill(open_id, 5, "1060")
        r_filled = scenario.step({"wall_time": f"{DAY}T01:10:07Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_filled.broker_snapshot, workbench_view=r_filled.workbench, long_qty=5)

        # Replay duplicate native_trade_id -> INV-004 must keep owned=5 and 1 unique trade
        scenario.broker.replay_trade_fact(str(trade_fact["native_trade_id"]))
        r_dup = scenario.step({"wall_time": f"{DAY}T01:10:08Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_dup.broker_snapshot, workbench_view=r_dup.workbench, long_qty=5)

        # Trigger STOP_LOSS -> first SELL CLOSE 5 is REJECTED by FakeBroker
        r_stop = scenario.step({"wall_time": f"{DAY}T01:10:15Z", "last": "1052", "bid1": "1052", "ask1": "1053", "candle_id": "bar-0110"})
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5, exit_target=0)
        assert_cycle_exit_reason(r_stop.workbench, CONTRACT, "STOP_LOSS")
        close_1 = str(r_stop.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.reject(close_1)

        # Next fresh tick delivers REJECTED -> INV-006/007 require preserving exit responsibility & retrying SELL CLOSE 5
        r_retry = scenario.step({"wall_time": f"{DAY}T01:10:16Z", "last": "1052", "bid1": "1052", "ask1": "1053", "candle_id": "bar-0110"})
        retry_sends = [ev for ev in r_retry.outbound_events if ev.get("event_type") == "OUTBOUND_SEND"]
        if len(retry_sends) != 1 or retry_sends[0]["request"]["direction"] != "SELL" or int(retry_sends[0]["request"]["quantity"]) != 5:
            raise AssertionError(f"Expected exit retry SELL CLOSE 5 after rejection, got {retry_sends}")
        close_2 = str(retry_sends[0]["request"]["child_order_id"])

        scenario.broker.accept(close_2)
        scenario.step({"wall_time": f"{DAY}T01:10:17Z", "last": "1052", "bid1": "1052", "ask1": "1053", "candle_id": "bar-0110"})
        scenario.broker.fill(close_2, 5, "1052")
        r_flat = scenario.step({"wall_time": f"{DAY}T01:10:18Z", "last": "1051", "bid1": "1050", "ask1": "1051", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_flat.broker_snapshot, workbench_view=r_flat.workbench, long_qty=0)
        assert_no_active_orders(broker_snapshot=r_flat.broker_snapshot, workbench_view=r_flat.workbench)

        return scenario.finish(passed=True)
