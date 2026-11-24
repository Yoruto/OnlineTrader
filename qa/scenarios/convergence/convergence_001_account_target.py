"""E2E Convergence Scenario: external active order cancel -> SHORT flatten -> LONG open with Oracle."""

from __future__ import annotations

from pathlib import Path

from qa.e2e.assertions import (
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
        scenario_name="E2E-CONVERGENCE-001-external-cancel-flatten-reversal",
        contract=CONTRACT,
        trading_day=DAY,
    ) as scenario:
        assert scenario.broker is not None and scenario._raw_broker is not None

        # Seed external 10-lot SHORT + 1 active non-strategy order BEFORE first sync
        scenario._raw_broker.seed_external_position(CONTRACT, short_today=10)
        scenario._raw_broker.add_external_order(
            "ext-native-001", CONTRACT, "SELL", "OPEN", 2, limit_price="1065",
        )
        scenario.oracle.set_unowned_position(CONTRACT, short_qty=10)

        # Round 1 (DISARMED): preview only, zero orders/cancels sent
        r1 = scenario.step(ticks["SYNC_BASELINE"])
        assert_entry_authority(r1.workbench, "DISARMED")
        if r1.outbound_events:
            raise AssertionError(f"DISARMED convergence leaked outbound actions: {r1.outbound_events}")
        if r1.workbench.get("external_positions"):
            raise AssertionError(f"Standalone convergence must not emit legacy external_positions: {r1.workbench.get('external_positions')}")
        acc1 = (r1.workbench.get("connection") or {}).get("account_positions") or []
        if not acc1 or acc1[0].get("actual_short") != 10 or acc1[0].get("target_short") != 0:
            raise AssertionError(f"Unexpected DISARMED account_positions preview: {acc1}")

        # Arm strategy via ControlServer
        arm = scenario.arm_strategy()
        if arm.get("status") != "SUCCEEDED":
            raise AssertionError(f"ARM_ENTRIES failed: {arm}")

        # Round 2 (ARMED, signal 1060 would want LONG): cancels external order FIRST without opening LONG
        r2 = scenario.step(ticks["TRIGGER_ENTRY"])
        ext_cancels = [
            ev for ev in r2.outbound_events
            if ev.get("event_type") == "OUTBOUND_CANCEL"
            and (ev.get("request") or {}).get("external_order") is True
        ]
        if len(ext_cancels) != 1 or (ext_cancels[0].get("request") or {}).get("native_order_id") != "ext-native-001":
            raise AssertionError(f"Expected cancellation of ext-native-001 first, got {r2.outbound_events}")
        if any(ev.get("event_type") == "OUTBOUND_SEND" for ev in r2.outbound_events):
            raise AssertionError("Must not send close/open while external active order still exists on contract")

        # Broker finishes external cancel -> Round 3 flattens SHORT 10 aggressively (`CONVERGENCE`)
        scenario._raw_broker.confirm_external_cancel("ext-native-001")
        r3 = scenario.step(dict(ticks["AFTER_ACCEPT"], last="1060", bid1="1059", ask1="1060"))
        conv_sends = [
            ev for ev in r3.outbound_events
            if ev.get("event_type") == "OUTBOUND_SEND"
            and (ev.get("request") or {}).get("order_source") == "CONVERGENCE"
        ]
        if len(conv_sends) != 1:
            raise AssertionError(f"Expected 1 CONVERGENCE BUY CLOSE 10 send, got {r3.outbound_events}")
        conv_req = conv_sends[0]["request"]
        if conv_req["direction"] != "BUY" or conv_req["native_offset"] != "CLOSE" or conv_req["quantity"] != 10:
            raise AssertionError(f"Unexpected convergence close request: {conv_req}")
        conv_cid = str(conv_req["child_order_id"])

        # Accept & fill SHORT flatten at broker -> short_qty drops 10 -> 0
        scenario.broker.accept(conv_cid)
        scenario.broker.fill(conv_cid, 10, "1058")
        scenario.oracle.set_unowned_position(CONTRACT, short_qty=0)
        r4 = scenario.step(dict(ticks["AFTER_PARTIAL_3"], last="1060", bid1="1059", ask1="1060", candle_id="bar-after-conv"))
        # As soon as flat is observed on r4, strategy immediately sends BUY OPEN 5 on r4!
        if r4.broker_snapshot["positions"]["short_qty"] != 0:
            raise AssertionError(f"Expected SHORT flattened to 0, got {r4.broker_snapshot['positions']}")
        open_sends = [
            ev for ev in r4.outbound_events
            if ev.get("event_type") == "OUTBOUND_SEND"
            and (ev.get("request") or {}).get("native_offset") == "OPEN"
        ]
        if len(open_sends) != 1:
            raise AssertionError(f"Expected strategy BUY OPEN 5 once SHORT flattened, got {r4.outbound_events}")
        open_req = open_sends[0]["request"]
        open_cid = str(open_req["child_order_id"])
        scenario.oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)

        scenario.broker.accept(open_cid)
        scenario.broker.fill(open_cid, 5, "1060")
        r5 = scenario.step(dict(ticks["HOLD_POSITION"], candle_id="bar-holding"))
        assert_positions(broker_snapshot=r5.broker_snapshot, workbench_view=r5.workbench, long_qty=5)
        assert_no_active_orders(broker_snapshot=r5.broker_snapshot, workbench_view=r5.workbench)

        return scenario.finish(passed=True)
