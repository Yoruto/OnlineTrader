"""PR4 Fault Scenario: SDK pump gap / step block -> OWNER_STALLED & TargetMarketDiagnostics JSONL."""

from __future__ import annotations

import json
from pathlib import Path
import time

from qa.e2e.assertions import assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-FAULT-002-sdk-pump-gap-diagnostics",
        contract=CONTRACT,
        trading_day=DAY,
        max_quote_age_seconds=5,
        use_production_tick_wrapper=True,
        enable_market_diagnostics=True,
    ) as scenario:
        assert scenario.broker is not None

        # 1. Baseline sync + arm + open 5 lots
        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})
        arm = scenario.arm_strategy()
        if arm.get("status") != "SUCCEEDED":
            raise AssertionError(f"ARM_ENTRIES failed: {arm}")
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

        r2 = scenario.step({"wall_time": f"{DAY}T01:10:02Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        open_child = str(r2.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(open_child)
        scenario.broker.fill(open_child, 5, "1060")
        r3 = scenario.step({"wall_time": f"{DAY}T01:10:03Z", "last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"})
        assert_positions(broker_snapshot=r3.broker_snapshot, workbench_view=r3.workbench, long_qty=5)

        # 2. Inject a 6s SDK pump gap + 1.1s real step delay to trigger TargetMarketDiagnostics summary + anomaly
        scenario.set_adapter_fault(fail_send=False, step_delay_ms=1100)
        r4 = scenario.step(
            {"wall_time": f"{DAY}T01:10:09Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"},
            sdk_pump_gap_ns=6_000_000_000,
            sdk_updated=True,
        )
        mh4 = (r4.workbench.get("configuration") or {}).get("market_health") or {}
        if mh4.get("status") != "OWNER_STALLED" or not mh4.get("feed_entry_blocked"):
            raise AssertionError(f"Expected OWNER_STALLED & entry blocked on 6s pump gap, got {mh4}")
        assert_positions(broker_snapshot=r4.broker_snapshot, workbench_view=r4.workbench, long_qty=5)

        # 3. Advance one more tick after >1s so TargetMarketDiagnostics flushes the second summary window
        time.sleep(1.1)
        scenario.step(
            {"wall_time": f"{DAY}T01:10:11Z", "last": "1061", "bid1": "1060", "ask1": "1061", "candle_id": "bar-0110"},
        )
        runtime_root = scenario.process.runtime_root
        summary_path = runtime_root / "target-market-summary.jsonl"
        anomaly_path = runtime_root / "target-market-anomaly.jsonl"
        deadline = time.monotonic() + 3.0
        summaries: list[dict[str, object]] = []
        anomalies: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            summaries = _read_jsonl(summary_path)
            anomalies = _read_jsonl(anomaly_path)
            if (
                any((row.get("sdk_pump_gap_max_ms") or 0) >= 6000 for row in summaries)
                and any(row.get("trigger") == "SDK_PUMP_GAP" for row in anomalies)
            ):
                break
            time.sleep(0.05)
        if not summaries:
            raise AssertionError("target-market-summary.jsonl was not written")
        if not anomalies:
            raise AssertionError("target-market-anomaly.jsonl was not written on 6s pump gap")
        if not any(row.get("trigger") == "SDK_PUMP_GAP" for row in anomalies):
            raise AssertionError(f"Expected SDK_PUMP_GAP anomaly trigger, got {anomalies}")
        if not any((row.get("sdk_pump_gap_max_ms") or 0) >= 6000 for row in summaries):
            raise AssertionError(f"Expected sdk_pump_gap_max_ms >= 6000 in summary, got {summaries}")

        return scenario.finish(passed=True)
