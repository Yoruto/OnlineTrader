"""PR4 Scenario: Clock discontinuity forces DISARMED and blocks new entries (INV-010)."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import assert_entry_authority, assert_no_active_orders
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-CLOCK-001-discontinuity-disarm",
        contract=CONTRACT,
        trading_day=DAY,
    ) as scenario:
        assert scenario.broker is not None

        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        scenario.arm_strategy()

        # Inject MONOTONIC_DISCONTINUITY while price crosses trigger line 1060 -> INV-010 must disarm & block
        r_fault = scenario.step(
            {"wall_time": f"{DAY}T01:10:05Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"},
            health_reason="MONOTONIC_DISCONTINUITY",
        )
        assert_entry_authority(r_fault.workbench, "DISARMED")
        if r_fault.workbench["connection"].get("clock_fault") != "MONOTONIC_DISCONTINUITY":
            raise AssertionError(f"Expected clock_fault=MONOTONIC_DISCONTINUITY, got {r_fault.workbench['connection']}")
        if r_fault.outbound_events:
            raise AssertionError(f"INV-010 violation: clock fault allowed entry order: {r_fault.outbound_events}")

        # Next tick without recovery still blocks entry and stays DISARMED
        r_next = scenario.step({"wall_time": f"{DAY}T01:10:07Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        assert_entry_authority(r_next.workbench, "DISARMED")
        if r_next.outbound_events:
            raise AssertionError(f"INV-010 violation: entry sent before clock recovery: {r_next.outbound_events}")
        assert_no_active_orders(broker_snapshot=r_next.broker_snapshot, workbench_view=r_next.workbench)

        return scenario.finish(passed=True)
