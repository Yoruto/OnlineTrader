"""Full User-Habit Desktop E2E: Real Qt Workbench panel actions & panel-to-Runtime consistency."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import time

from online_trader.transport.runtime_view import RuntimeViewClient
from qa.e2e import assert_offline_qa_environment
from qa.e2e.assembler import FrameAssembler
from qa.e2e.clock import VirtualClock
from qa.e2e.desktop_driver import DesktopWorkbenchDriver
from qa.e2e.desktop_host import QADesktopHost
from qa.e2e.evidence import TraceRecorder
from qa.e2e.market import FakeMarket
from qa.e2e.oracle import ReferenceOracle
from qa.e2e.scenario import build_multi_contract_profile_record
from qa.target_owner.fake_broker import FakeBroker
from qa.target_owner.offline_runtime import CONTRACT, DAY


def _assert_panel_matches_runtime(ui: dict[str, object], runtime_wb: dict[str, object], broker_snap: dict[str, object]) -> None:
    """Assert Qt Workbench UI panel widget contents agree with Runtime projection and FakeBroker truth."""
    runtime_auth = runtime_wb.get("entry_authority")
    if ui.get("view_entry_authority") != runtime_auth:
        raise AssertionError(f"UI authority {ui.get('view_entry_authority')!r} != Runtime {runtime_auth!r}")

    positions_by_contract = broker_snap.get("positions_by_contract") or {
        str(broker_snap["contract_id"]): broker_snap["positions"],
    }
    broker_owned = sum(int(p["long_qty"]) + int(p["short_qty"]) for p in positions_by_contract.values())
    runtime_owned = int((runtime_wb.get("current_responsibility") or {}).get("owned_quantity", 0))
    if int(ui.get("view_owned_quantity") or 0) != runtime_owned or runtime_owned != broker_owned:
        raise AssertionError(
            f"Owned mismatch: UI={ui.get('view_owned_quantity')} Runtime={runtime_owned} Broker={broker_owned}"
        )

    # 1. Cycles table: verify contract, direction ('多'/'空'), and quantity per row
    cycles_rows = ui.get("cycles_rows") or []
    expected_positions: dict[tuple[str, str], int] = {}
    for code, pos in positions_by_contract.items():
        if int(pos["long_qty"]) > 0:
            expected_positions[(code, "多")] = int(pos["long_qty"])
        if int(pos["short_qty"]) > 0:
            expected_positions[(code, "空")] = int(pos["short_qty"])
    ui_positions: dict[tuple[str, str], int] = {}
    for row in cycles_rows:
        if len(row) < 3:
            raise AssertionError(f"Unexpected cycles row shape: {row}")
        key = (str(row[0]), str(row[1]))
        ui_positions[key] = ui_positions.get(key, 0) + int(row[2])
    if ui_positions != expected_positions:
        raise AssertionError(
            f"UI cycles table content mismatch: UI={ui_positions} != Broker={expected_positions} (rows={cycles_rows})"
        )

    # 2. Active orders table: verify native_order_id, contract, side, offset, qty, remaining, price, status
    offset_labels = {"OPEN": "开仓", "CLOSE": "平仓", "CLOSETODAY": "平今", "CLOSEYESTERDAY": "平昨"}
    side_labels = {"BUY": "买", "SELL": "卖"}
    orders_rows = ui.get("orders_rows") or []
    active_ids = list(broker_snap.get("active_children") or [])
    if len(orders_rows) != len(active_ids):
        raise AssertionError(f"UI active orders rows={len(orders_rows)} != Broker active orders {len(active_ids)}: {orders_rows}")
    orders_by_native = {str(row[0]): row for row in orders_rows if len(row) >= 8}
    broker_orders = broker_snap.get("orders") or {}
    for cid in active_ids:
        order = broker_orders[cid]
        req = order["request"]
        native_id = str(order["native_order_id"])
        row = orders_by_native.get(native_id)
        if row is None:
            raise AssertionError(f"Active order {native_id} missing from UI orders table: {orders_rows}")
        expected_rem = int(req["quantity"]) - int(order["traded_qty"])
        if (
            row[1] != str(req["contract_id"])
            or row[2] != side_labels[str(req["direction"])]
            or row[3] != offset_labels[str(req["native_offset"])]
            or int(row[4]) != int(req["quantity"])
            or int(row[5]) != expected_rem
            or Decimal(row[6]) != Decimal(str(req["limit_price"]))
            or row[7] != "挂单"
        ):
            raise AssertionError(
                f"UI order row {row} disagrees with Broker order {order}"
            )

    # 3. Trades table: verify native_trade_id, contract, side, effect, quantity, price
    trades_rows = ui.get("trades_rows") or []
    broker_trades = broker_snap.get("trades") or {}
    if len(trades_rows) != len(broker_trades):
        raise AssertionError(f"UI trades table rows={len(trades_rows)} != Broker trades {len(broker_trades)}: {trades_rows}")
    trades_by_id = {str(row[0]): row for row in trades_rows if len(row) >= 6}
    for tid, trade in broker_trades.items():
        row = trades_by_id.get(str(tid))
        if row is None:
            raise AssertionError(f"Trade {tid} missing from UI trades table: {trades_rows}")
        expected_effect = "开仓" if trade["native_offset"] == "OPEN" else "平仓"
        if (
            row[1] != str(trade["contract_id"])
            or row[2] != side_labels[str(trade["direction"])]
            or row[3] != expected_effect
            or int(row[4]) != int(trade["quantity"])
            or Decimal(row[5]) != Decimal(str(trade["price"]))
        ):
            raise AssertionError(
                f"UI trade row {row} disagrees with Broker trade {trade}"
            )

    btn = ui.get("button_enabled") or {}
    if runtime_auth == "ARMED" and btn.get("arm"):
        raise AssertionError("UI 'arm' button should be disabled when ARMED")
    if runtime_auth == "DISARMED" and not btn.get("arm"):
        raise AssertionError("UI 'arm' button should be enabled when DISARMED and synchronized")


def run(work_dir: Path) -> dict[str, object]:
    assert_offline_qa_environment(block_tqsdk=True)
    host_dir = work_dir / "desktop-host"
    evidence_dir = work_dir / "evidence"
    trace = TraceRecorder(evidence_dir, scenario_name="E2E-DESKTOP-002-full-panel-consistency", run_id="desktop-002")

    profile_rec = build_multi_contract_profile_record((CONTRACT,), max_quote_age_seconds=5)
    with QADesktopHost(host_dir, profile_record=profile_rec, trading_day=DAY) as host:
        assert host.account_key and host.boot_id
        broker = FakeBroker(
            account_key=host.account_key,
            contract=CONTRACT,
            trading_day=DAY,
            trace_path=evidence_dir / "broker-events.jsonl",
            run_id="desktop-002",
        )
        clock = VirtualClock(f"{DAY}T01:09:50Z")
        market = FakeMarket(
            contract_id=CONTRACT,
            trading_day=DAY,
            session_open=f"{DAY}T01:00:00Z",
            session_close=f"{DAY}T02:15:00Z",
        )
        assembler = FrameAssembler(market=market, broker=broker, clock=clock, trading_day=DAY)
        oracle = ReferenceOracle()
        oracle.set_boot_expectation(host.boot_id)
        view_client = RuntimeViewClient(host.pipe)
        cursor = 0
        expected_seq = 1

        def read_workbench_view(*, min_seq: int = 0) -> dict[str, object]:
            deadline = time.monotonic() + 5.0
            while True:
                msg = view_client.read()
                if msg.get("kind") == "BASELINE":
                    view = msg["body"]["view"]
                    if int(view.get("checkpoint_event_seq", 0)) >= min_seq:
                        return view
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for RuntimeViewClient baseline (min_seq={min_seq})")
                time.sleep(0.1)

        def push_round(wall_time: str, last: str | None, *, drop_tick: bool = False) -> list[dict[str, object]]:
            nonlocal cursor, expected_seq
            clock.advance_to(wall_time)
            market.apply_tick(wall_time=clock.now_utc, last=last, candle_id="bar-0110", drop_tick=drop_tick)
            frame = assembler.build()
            fid = str(frame["id"])
            host.send_fact_frame(frame)
            host.wait_for_frame_applied(fid, after=cursor)
            broker.acknowledge_frame(fid)
            expected_seq += 1
            events = host.events[cursor:]
            cursor = len(host.events)
            outbound = [ev for ev in events if ev.get("event_type") in {"OUTBOUND_SEND", "OUTBOUND_CANCEL"}]
            for ev in outbound:
                broker.observe_adapter_event(ev)
            wb = read_workbench_view(min_seq=expected_seq)
            oracle.verify(
                broker_snapshot=broker.snapshot(),
                workbench_view=wb,
                round_seq=int(frame["sequence"]),
                frame_id=fid,
                outbound_events=outbound,
            )
            return outbound

        # 1. Sync baseline and launch real Qt Workbench panel
        push_round(f"{DAY}T01:10:00Z", "1054")

        with DesktopWorkbenchDriver(host.pipe) as gui:
            gui.refresh()
            ui_init = gui.wait_until(
                lambda s: bool(s.get("markets_rows")) and bool((s.get("button_enabled") or {}).get("arm")),
                timeout=8.0,
            )
            _assert_panel_matches_runtime(ui_init, read_workbench_view(min_seq=expected_seq), broker.snapshot())

            # 2. User clicks real 'arm' button on Qt Workbench panel -> verify panel & Runtime agree
            gui.click_button("arm")
            ui_armed = gui.wait_until(lambda s: s.get("view_entry_authority") == "ARMED", timeout=8.0)
            expected_seq += 1
            oracle.set_authority_expectation("ARMED")
            oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)
            _assert_panel_matches_runtime(ui_armed, read_workbench_view(min_seq=expected_seq), broker.snapshot())

            # 3. Trigger BUY OPEN 5 -> accept -> partial fill 3 -> fill 2 -> check UI tables & labels
            sends = push_round(f"{DAY}T01:10:02Z", "1060")
            if len(sends) != 1:
                raise AssertionError(f"Expected 1 BUY OPEN send, got {sends}")
            open_child = str(sends[0]["request"]["child_order_id"])
            broker.accept_order(open_child)
            push_round(f"{DAY}T01:10:03Z", "1060")
            gui.refresh()
            ui_working = gui.wait_until(lambda s: len(s.get("orders_rows", [])) >= 1, timeout=8.0)
            _assert_panel_matches_runtime(ui_working, read_workbench_view(min_seq=expected_seq), broker.snapshot())

            broker.fill(open_child, 3, "1060")
            push_round(f"{DAY}T01:10:04Z", "1060")
            broker.fill(open_child, 2, "1060")
            push_round(f"{DAY}T01:10:05Z", "1061")
            gui.refresh()
            ui_filled = gui.wait_until(
                lambda s: len(s.get("cycles_rows", [])) == 1 and len(s.get("trades_rows", [])) == 2 and len(s.get("protection_rows", [])) >= 1,
                timeout=8.0,
            )
            _assert_panel_matches_runtime(ui_filled, read_workbench_view(min_seq=expected_seq), broker.snapshot())

            # 4. Inject 7s quote stall -> verify panel shows stall/blocks while preserving 5-lot position
            push_round(f"{DAY}T01:10:12Z", None, drop_tick=True)
            gui.refresh()
            ui_stalled = gui.wait_until(
                lambda s: any("滞后" in text or "待核对" in text for text in (s.get("toolbar_status") or []))
                or bool(s.get("blocks_rows")),
                timeout=8.0,
            )
            _assert_panel_matches_runtime(ui_stalled, read_workbench_view(min_seq=expected_seq), broker.snapshot())

            # 5. Market recovers -> user selects position row in panel & clicks real 'close' button (QInputDialog=5)
            push_round(f"{DAY}T01:10:14Z", "1061")
            gui.refresh()
            gui.wait_until(lambda s: len(s.get("cycles_rows", [])) == 1, timeout=8.0)
            gui.select_row("cycles", 0)
            gui.click_button("close", dialog_int=5)
            oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5, exit_target=0)
            expected_seq += 1

            # Wait until Runtime commits MANUAL_CLOSE (target_remaining_quantity == 0), then push next round to emit SELL CLOSE 5
            wb_after_close = read_workbench_view(min_seq=expected_seq)
            close_sends = push_round(f"{DAY}T01:10:15Z", "1061")
            if len(close_sends) != 1:
                raise AssertionError(
                    f"Expected 1 manual close send after panel close click (wb_cycles={wb_after_close.get('cycles')}), got {close_sends}"
                )
            close_child = str(close_sends[0]["request"]["child_order_id"])
            broker.accept_order(close_child)
            push_round(f"{DAY}T01:10:16Z", "1061")
            broker.fill(close_child, 5, "1061")
            push_round(f"{DAY}T01:10:17Z", "1061")
            gui.refresh()
            ui_flat = gui.wait_until(
                lambda s: (
                    int(s.get("view_owned_quantity") or 0) == 0
                    and len(s.get("cycles_rows", [])) == 0
                    and len(s.get("orders_rows", [])) == 0
                    and len(s.get("trades_rows", [])) == 3
                ),
                timeout=8.0,
            )
            _assert_panel_matches_runtime(ui_flat, read_workbench_view(min_seq=expected_seq), broker.snapshot())

            # 6. User clicks real 'pause' button (F6) -> verify DISARMED across panel & Runtime
            gui.click_button("pause")
            ui_paused = gui.wait_until(lambda s: s.get("view_entry_authority") == "DISARMED", timeout=8.0)
            expected_seq += 1
            oracle.set_authority_expectation("DISARMED")
            _assert_panel_matches_runtime(ui_paused, read_workbench_view(min_seq=expected_seq), broker.snapshot())

            gui.close(force=True)

    return trace.finalize(passed=True)
