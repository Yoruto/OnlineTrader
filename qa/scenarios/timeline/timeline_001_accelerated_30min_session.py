"""Accelerated 35-minute virtual-time multi-bar trading & fault simulation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from qa.e2e.assertions import assert_cycle_exit_reason, assert_no_active_orders, assert_positions
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import DAY

CONTRACTS = ("DCE.p2701", "SHFE.ag2702")


def _ts(minute_offset: int, second: int = 0) -> tuple[str, str]:
    base = datetime(2026, 11, 13, 1, 5, 0, tzinfo=timezone.utc)
    dt = base + timedelta(minutes=minute_offset, seconds=second)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z"), f"bar-{dt.strftime('%H%M')}"


def run(work_dir: Path) -> dict[str, object]:
    wall_started = time.monotonic()
    with TradingScenario(
        work_dir,
        scenario_name="E2E-TIMELINE-001-accelerated-35min",
        contract=CONTRACTS[0],
        contracts=CONTRACTS,
        trading_day=DAY,
        initial_utc=f"{DAY}T01:04:50Z",
        session_open=f"{DAY}T01:00:00Z",
        session_close=f"{DAY}T02:15:00Z",
        max_quote_age_seconds=5,
    ) as scenario:
        assert scenario.broker is not None

        def tick_both(m: int, sec: int, p_last: str, ag_last: str, **kwargs):
            wt, cid = _ts(m, sec)
            return scenario.step(
                {"wall_time": wt},
                ticks_by_contract={
                    "DCE.p2701": {"last": p_last, "bid1": str(int(p_last) - 1), "ask1": p_last, "candle_id": cid},
                    "SHFE.ag2702": {"last": ag_last, "bid1": str(int(ag_last) - 1), "ask1": ag_last, "candle_id": cid},
                },
                **kwargs,
            )

        # Minute 0 (01:05): sync baseline + ARM
        tick_both(0, 0, "1054", "1054")
        arm = scenario.arm_strategy()
        if arm.get("status") != "SUCCEEDED":
            raise AssertionError(f"ARM_ENTRIES failed: {arm}")
        for code in CONTRACTS:
            scenario.oracle.set_target_expectation(code, direction="LONG", quantity=5)

        # Minute 1 (01:06): DCE.p2701 entry trigger -> 3+2 partial fills
        r_entry1 = tick_both(1, 5, "1060", "1054")
        c1 = str(r_entry1.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(c1)
        tick_both(1, 20, "1060", "1054")
        scenario.broker.fill(c1, 3, "1060")
        tick_both(1, 35, "1061", "1055")
        scenario.broker.fill(c1, 2, "1060")
        r_held1 = tick_both(2, 0, "1061", "1055")
        assert_positions(broker_snapshot=r_held1.broker_snapshot, workbench_view=r_held1.workbench, long_qty=5)

        # Minutes 3..8: normal bars + SHFE.ag2702 silent (QUIET) for 1 minute then resume
        for m in range(3, 7):
            tick_both(m, 0, "1063", "1055")
        wt_q, cid_q = _ts(7, 10)
        r_quiet = scenario.step(
            {"wall_time": wt_q},
            ticks_by_contract={
                "DCE.p2701": {"last": "1064", "bid1": "1063", "ask1": "1064", "candle_id": cid_q},
                "SHFE.ag2702": {"drop_tick": True},
            },
        )
        mh_q = (r_quiet.workbench.get("configuration") or {}).get("market_health") or {}
        if mh_q.get("symbols", {}).get("SHFE.ag2702") != "QUIET":
            raise AssertionError(f"Expected SHFE.ag2702=QUIET during 70s silence, got {mh_q}")
        tick_both(8, 0, "1065", "1054")

        # Minutes 9..11: temporary global stall then recovery
        wt_s, _ = _ts(9, 10)
        r_stall = scenario.step({"wall_time": wt_s}, ticks_by_contract={c: {"drop_tick": True} for c in CONTRACTS})
        mh_s = (r_stall.workbench.get("configuration") or {}).get("market_health") or {}
        if mh_s.get("status") != "FEED_SUSPECTED_STALLED":
            raise AssertionError(f"Expected FEED_SUSPECTED_STALLED, got {mh_s}")
        tick_both(11, 0, "1066", "1055")

        # Minutes 12..14: disconnect & reconnect while holding DCE.p2701
        wt_d, _ = _ts(12, 0)
        r_disc = scenario.step({"wall_time": wt_d}, complete=False, connection_complete=False)
        if ((r_disc.workbench.get("configuration") or {}).get("market_health") or {}).get("status") != "RECONNECTING":
            raise AssertionError("Expected RECONNECTING on disconnect")
        tick_both(14, 0, "1068", "1055", complete=True, synchronized=True)

        # Minutes 15..18: DCE.p2701 hits TAKE_PROFIT (1080 = 1060 + 20) -> close 5 lots
        for m in (15, 16):
            tick_both(m, 0, "1072", "1055")
        scenario.oracle.set_target_expectation("DCE.p2701", direction="LONG", quantity=5, exit_target=0)
        r_tp = tick_both(17, 5, "1080", "1055")
        assert_cycle_exit_reason(r_tp.workbench, "DCE.p2701", "TAKE_PROFIT")
        c_tp = str(r_tp.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(c_tp)
        scenario.broker.fill(c_tp, 5, "1080")
        r_flat1 = tick_both(18, 0, "1055", "1055")
        assert_positions(broker_snapshot=r_flat1.broker_snapshot, workbench_view=r_flat1.workbench, long_qty=0)
        scenario.oracle.exit_active.pop("DCE.p2701", None)

        # Minutes 19..28 (10 min): quiet mid-session bars across 10 bars
        for m in range(19, 29):
            tick_both(m, 0, "1055", "1056")

        # Minutes 29..35: SHFE.ag2702 opens 5 lots @ 1060, then stops out @ 1052 -> flat at +35min
        r_entry2 = tick_both(29, 5, "1055", "1060")
        c2 = str(r_entry2.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(c2)
        scenario.broker.fill(c2, 5, "1060")
        tick_both(31, 0, "1055", "1061")
        scenario.oracle.set_target_expectation("SHFE.ag2702", direction="LONG", quantity=5, exit_target=0)
        r_sl2 = tick_both(33, 5, "1055", "1052")
        assert_cycle_exit_reason(r_sl2.workbench, "SHFE.ag2702", "STOP_LOSS")
        c_sl2 = str(r_sl2.outbound_events[0]["request"]["child_order_id"])
        scenario.broker.accept(c_sl2)
        scenario.broker.fill(c_sl2, 5, "1052")
        r_end = tick_both(35, 0, "1055", "1052")
        assert_no_active_orders(broker_snapshot=r_end.broker_snapshot, workbench_view=r_end.workbench)
        for code in CONTRACTS:
            pos = r_end.broker_snapshot["positions_by_contract"][code]
            if pos["long_qty"] != 0 or pos["short_qty"] != 0:
                raise AssertionError(f"Expected flat {code} at end of 35min session, got {pos}")

        virtual_elapsed_min = (
            datetime.fromisoformat(r_end.wall_time.replace("Z", "+00:00"))
            - datetime(2026, 11, 13, 1, 5, 0, tzinfo=timezone.utc)
        ).total_seconds() / 60.0
        if virtual_elapsed_min < 35.0:
            raise AssertionError(f"Expected >= 35 virtual minutes simulated, got {virtual_elapsed_min}")

        scenario.trace.record(
            "COMMAND",
            operation="ACCELERATED_35MIN_SUMMARY",
            receipt_status=f"virtual_min={virtual_elapsed_min:.1f},wall_sec={time.monotonic() - wall_started:.2f}",
        )
        return scenario.finish(passed=True)
