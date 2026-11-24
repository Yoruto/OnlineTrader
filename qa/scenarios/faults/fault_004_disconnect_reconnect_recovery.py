"""PR4 Fault Scenario: Disconnect (RECONNECTING) -> Resync (RESYNCHRONIZING) -> Reconnect & Exit."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import assert_cycle_exit_reason, assert_no_active_orders, assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-FAULT-004-disconnect-reconnect-recovery",
        contract=CONTRACT,
        trading_day=DAY,
        max_quote_age_seconds=5,
    ) as scenario:
        assert scenario.broker is not None

        # 1. Baseline sync + arm + open 5 lots
        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        arm = scenario.arm_strategy()
        if arm.get("status") != "SUCCEEDED":
            raise AssertionError(f"ARM_ENTRIES failed: {arm}")
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

        r_open = scenario.step({"wall_time": f"{DAY}T01:10:05Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        open_child = str(r_open.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(open_child)
        scenario.broker.fill(open_child, 5, "1060")
        r_held = scenario.step({"wall_time": f"{DAY}T01:10:06Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_held.broker_snapshot, workbench_view=r_held.workbench, long_qty=5)

        # 2. Disconnect -> complete=False, connection_complete=False -> RECONNECTING, position & authority kept
        r_disc = scenario.step(
            {"wall_time": f"{DAY}T01:10:08Z"},
            complete=False,
            connection_complete=False,
        )
        mh_disc = (r_disc.workbench.get("configuration") or {}).get("market_health") or {}
        if mh_disc.get("status") != "RECONNECTING" or not mh_disc.get("feed_entry_blocked"):
            raise AssertionError(f"Expected RECONNECTING & entry blocked on disconnect, got {mh_disc}")
        if r_disc.workbench["connection"]["synchronized"]:
            raise AssertionError("Expected synchronized=False during disconnect")
        if r_disc.outbound_events:
            raise AssertionError(f"Disconnect must not emit orders: {r_disc.outbound_events}")

        # 3. Reconnected transport, awaiting full native resync -> RESYNCHRONIZING
        r_resync = scenario.step(
            {"wall_time": f"{DAY}T01:10:10Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"},
            complete=True,
            synchronized=False,
        )
        mh_resync = (r_resync.workbench.get("configuration") or {}).get("market_health") or {}
        if mh_resync.get("status") != "RESYNCHRONIZING" or not mh_resync.get("feed_entry_blocked"):
            raise AssertionError(f"Expected RESYNCHRONIZING while synchronized=False, got {mh_resync}")
        if r_resync.outbound_events:
            raise AssertionError(f"Resynchronizing round must not emit orders: {r_resync.outbound_events}")

        # 4. Full reconnect synchronization restored -> HEALTHY, still holding 5 lots cleanly
        r_ok = scenario.step(
            {"wall_time": f"{DAY}T01:10:12Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"},
            complete=True,
            synchronized=True,
        )
        mh_ok = (r_ok.workbench.get("configuration") or {}).get("market_health") or {}
        if mh_ok.get("status") != "HEALTHY" or mh_ok.get("feed_entry_blocked"):
            raise AssertionError(f"Expected HEALTHY after reconnect sync, got {mh_ok}")
        assert_positions(broker_snapshot=r_ok.broker_snapshot, workbench_view=r_ok.workbench, long_qty=5)

        # 5. Stop-loss after reconnect cleanly closes all 5 lots
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5, exit_target=0)
        r_exit = scenario.step({"wall_time": f"{DAY}T01:10:14Z", "last": "1052", "bid1": "1051", "ask1": "1052", "candle_id": "bar-0110"})
        assert_cycle_exit_reason(r_exit.workbench, CONTRACT, "STOP_LOSS")
        close_child = str(r_exit.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(close_child)
        scenario.broker.fill(close_child, 5, "1052")
        r_flat = scenario.step({"wall_time": f"{DAY}T01:10:16Z", "last": "1052", "bid1": "1051", "ask1": "1052", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_flat.broker_snapshot, workbench_view=r_flat.workbench, long_qty=0)
        assert_no_active_orders(broker_snapshot=r_flat.broker_snapshot, workbench_view=r_flat.workbench)

        return scenario.finish(passed=True)
