"""PR4 Scenario: Shared production execute_owner_tick error handling + cross-day rollover."""

from __future__ import annotations

import json
from pathlib import Path

from qa.e2e.assertions import assert_entry_authority, assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    next_day = "2026-11-16"
    with TradingScenario(
        work_dir,
        scenario_name="E2E-RECOVERY-002-tick-error-and-crossday",
        contract=CONTRACT,
        trading_day=DAY,
        use_production_tick_wrapper=True,
    ) as scenario:
        assert scenario.broker is not None

        # 1. Sync & Arm
        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        scenario.arm_strategy()

        # 2. Inject TRANSIENT_DISCONNECT via production execute_owner_tick -> authority preserved, phase=WAITING_FOR_NATIVE_SYNC
        ev_transient = scenario.inject_step_error("E_DSV_SDK_SYNC")
        diag = ev_transient["diagnostics"]
        if diag["error_category"] != "TRANSIENT_DISCONNECT":
            raise AssertionError(f"Expected TRANSIENT_DISCONNECT, got {diag}")
        if diag["authority_effect"] != "SYNCHRONIZATION_LOST_AUTHORITY_PRESERVED":
            raise AssertionError(f"Expected authority preserved on transient error, got {diag}")

        # Recover on next synchronized round -> phase READY, still ARMED
        r_rec = scenario.step({"wall_time": f"{DAY}T01:10:03Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        assert_entry_authority(r_rec.workbench, "ARMED")

        # 3. Open and fill 5 lots on DAY (long_today=5)
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)
        r_open = scenario.step({"wall_time": f"{DAY}T01:10:05Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        child_id = str(r_open.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(child_id)
        scenario.step({"wall_time": f"{DAY}T01:10:06Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        scenario.broker.fill(child_id, 5, "1060")
        r_day1 = scenario.step({"wall_time": f"{DAY}T01:10:07Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r_day1.broker_snapshot, workbench_view=r_day1.workbench, long_qty=5)

        # 4. Roll trading day -> FakeBroker converts long_today=5 -> long_historical=5
        scenario.roll_trading_day(
            next_day,
            session_open=f"{next_day}T01:00:00Z",
            session_close=f"{next_day}T02:15:00Z",
        )
        r_day2 = scenario.step({"wall_time": f"{next_day}T01:10:00Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-day2"})
        if int(r_day2.broker_snapshot["positions"]["long_historical"]) != 5:
            raise AssertionError(f"Expected long_historical=5 after rollover, got {r_day2.broker_snapshot['positions']}")
        assert_positions(broker_snapshot=r_day2.broker_snapshot, workbench_view=r_day2.workbench, long_qty=5)

        # 5. Inject PERSISTENT_DISARM -> must disarm & write NEEDS_REVIEW status
        ev_persist = scenario.inject_step_error("E_DSV_NATIVE_FACT")
        diag2 = ev_persist["diagnostics"]
        if diag2["error_category"] != "PERSISTENT_DISARM" or diag2["authority_effect"] != "DISARMED_RECOVERY_REQUIRED":
            raise AssertionError(f"Expected PERSISTENT_DISARM + DISARMED_RECOVERY_REQUIRED, got {diag2}")
        status_file = scenario.process.runtime_root / "target-owner-status.json"
        status_json = json.loads(status_file.read_text(encoding="utf-8"))
        if status_json.get("phase") != "NEEDS_REVIEW" or status_json.get("entry_authority") != "DISARMED":
            raise AssertionError(f"Unexpected target-owner-status.json after persistent error: {status_json}")

        return scenario.finish(passed=True)
