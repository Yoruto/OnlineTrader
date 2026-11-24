"""PR4 Fault Scenario: Multi-contract QUIET vs FEED_SUSPECTED_STALLED & DISARMED recovery."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import assert_entry_authority, assert_no_active_orders, assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import DAY

CONTRACTS = ("DCE.p2701", "SHFE.ag2702", "DCE.m2701")


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-FAULT-001-multi-contract-stall-quiet",
        contract=CONTRACTS[0],
        contracts=CONTRACTS,
        trading_day=DAY,
        max_quote_age_seconds=5,
    ) as scenario:
        assert scenario.broker is not None

        # 1. Baseline sync for all 3 active contracts
        r1 = scenario.step(
            {"wall_time": f"{DAY}T01:10:00Z"},
            ticks_by_contract={
                code: {"last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"}
                for code in CONTRACTS
            },
        )
        mh1 = (r1.workbench.get("configuration") or {}).get("market_health") or {}
        if mh1.get("status") != "HEALTHY":
            raise AssertionError(f"Expected initial HEALTHY feed status, got {mh1}")

        arm_receipt = scenario.arm_strategy()
        if arm_receipt.get("status") != "SUCCEEDED":
            raise AssertionError(f"ARM_ENTRIES failed: {arm_receipt}")
        for code in CONTRACTS:
            scenario.oracle.set_target_expectation(code, direction="LONG", quantity=5)

        # 2. Only SHFE.ag2702 goes silent for 7s (>5s) while DCE.p2701 and DCE.m2701 keep ticking -> QUIET, not global stall
        r2 = scenario.step(
            {"wall_time": f"{DAY}T01:10:07Z"},
            ticks_by_contract={
                "DCE.p2701": {"last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"},
                "SHFE.ag2702": {"drop_tick": True},
                "DCE.m2701": {"last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"},
            },
        )
        mh2 = (r2.workbench.get("configuration") or {}).get("market_health") or {}
        if mh2.get("status") != "DEGRADED" or mh2.get("feed_entry_blocked"):
            raise AssertionError(f"Single silent symbol must yield DEGRADED (not blocked), got {mh2}")
        symbols2 = mh2.get("symbols") or {}
        if symbols2.get("SHFE.ag2702") != "QUIET":
            raise AssertionError(f"Expected SHFE.ag2702=QUIET, got {symbols2}")
        if symbols2.get("DCE.p2701") != "FRESH" or symbols2.get("DCE.m2701") != "FRESH":
            raise AssertionError(f"Expected active contracts FRESH, got {symbols2}")

        # 3. DCE.p2701 triggers BUY OPEN 5 while SHFE.ag2702 remains silent; accept & fill DCE.p2701
        r3 = scenario.step(
            {"wall_time": f"{DAY}T01:10:08Z"},
            ticks_by_contract={
                "DCE.p2701": {"last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"},
                "SHFE.ag2702": {"drop_tick": True},
                "DCE.m2701": {"last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"},
            },
        )
        p_sends = [
            ev for ev in r3.outbound_events
            if ev.get("event_type") == "OUTBOUND_SEND"
            and (ev.get("request") or {}).get("contract_id") == "DCE.p2701"
        ]
        if len(p_sends) != 1:
            raise AssertionError(f"Expected 1 BUY OPEN on DCE.p2701, got {r3.outbound_events}")
        ag_sends = [
            ev for ev in r3.outbound_events
            if (ev.get("request") or {}).get("contract_id") == "SHFE.ag2702"
        ]
        if ag_sends:
            raise AssertionError(f"Silent contract SHFE.ag2702 must not emit orders: {ag_sends}")
        open_child = str(p_sends[0]["request"]["child_order_id"])
        scenario.broker.accept(open_child)
        scenario.broker.fill(open_child, 5, "1060")
        r4 = scenario.step(
            {"wall_time": f"{DAY}T01:10:09Z"},
            ticks_by_contract={
                "DCE.p2701": {"last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"},
                "SHFE.ag2702": {"drop_tick": True},
                "DCE.m2701": {"last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"},
            },
        )
        assert_positions(broker_snapshot=r4.broker_snapshot, workbench_view=r4.workbench, long_qty=5)

        # 4. ALL active contracts go silent for >= 6s -> FEED_SUSPECTED_STALLED + entry blocked, position kept
        r5 = scenario.step(
            {"wall_time": f"{DAY}T01:10:16Z"},
            ticks_by_contract={code: {"drop_tick": True} for code in CONTRACTS},
        )
        mh5 = (r5.workbench.get("configuration") or {}).get("market_health") or {}
        if mh5.get("status") != "FEED_SUSPECTED_STALLED" or not mh5.get("feed_entry_blocked"):
            raise AssertionError(f"All-silent feed must classify FEED_SUSPECTED_STALLED & block entry, got {mh5}")
        if r5.outbound_events:
            raise AssertionError(f"INV-010: stalled feed emitted orders: {r5.outbound_events}")

        # 5. User clicks PAUSE_NEW_ENTRIES during stall -> stays DISARMED after market recovers
        disarm_receipt = scenario.disarm_strategy()
        if disarm_receipt.get("status") != "SUCCEEDED":
            raise AssertionError(f"PAUSE_NEW_ENTRIES failed during stall: {disarm_receipt}")

        r6 = scenario.step(
            {"wall_time": f"{DAY}T01:10:18Z"},
            ticks_by_contract={
                "DCE.p2701": {"last": "1056", "bid1": "1055", "ask1": "1056", "candle_id": "bar-0110"},
                "SHFE.ag2702": {"last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"},
                # Entry trigger price 1060 on DCE.m2701 must NOT open while DISARMED
                "DCE.m2701": {"last": "1060", "bid1": "1059", "ask1": "1060", "candle_id": "bar-0110"},
            },
        )
        mh6 = (r6.workbench.get("configuration") or {}).get("market_health") or {}
        if mh6.get("status") != "HEALTHY" or mh6.get("feed_entry_blocked"):
            raise AssertionError(f"Recovered feed should be HEALTHY, got {mh6}")
        assert_entry_authority(r6.workbench, "DISARMED")
        if r6.outbound_events:
            raise AssertionError(f"Disarmed strategy must not auto-open after stall recovery: {r6.outbound_events}")

        # 6. Protective STOP_LOSS on DCE.p2701 still works even while DISARMED
        scenario.oracle.set_target_expectation("DCE.p2701", direction="LONG", quantity=5, exit_target=0)
        r7 = scenario.step(
            {"wall_time": f"{DAY}T01:10:20Z"},
            ticks_by_contract={
                "DCE.p2701": {"last": "1052", "bid1": "1051", "ask1": "1052", "candle_id": "bar-0110"},
                "SHFE.ag2702": {"last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"},
                "DCE.m2701": {"last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"},
            },
        )
        close_sends = [
            ev for ev in r7.outbound_events
            if ev.get("event_type") == "OUTBOUND_SEND"
            and (ev.get("request") or {}).get("native_offset") != "OPEN"
        ]
        if len(close_sends) != 1:
            raise AssertionError(f"Expected protective close order on DCE.p2701, got {r7.outbound_events}")
        close_child = str(close_sends[0]["request"]["child_order_id"])
        scenario.broker.accept(close_child)
        scenario.broker.fill(close_child, 5, "1052")
        r8 = scenario.step(
            {"wall_time": f"{DAY}T01:10:22Z"},
            ticks_by_contract={
                code: {"last": "1052", "bid1": "1051", "ask1": "1052", "candle_id": "bar-0110"}
                for code in CONTRACTS
            },
        )
        assert_positions(broker_snapshot=r8.broker_snapshot, workbench_view=r8.workbench, long_qty=0)
        assert_no_active_orders(broker_snapshot=r8.broker_snapshot, workbench_view=r8.workbench)
        return scenario.finish(passed=True)
