"""PR4 Negative Control: Verify ReferenceOracle catches injected INV-001..INV-015 violations."""

from __future__ import annotations

from pathlib import Path

from qa.e2e import assert_offline_qa_environment
from qa.e2e.evidence import TraceRecorder
from qa.e2e.oracle import OracleViolation, ReferenceOracle
from qa.target_owner.offline_runtime import CONTRACT


def _expect_violation(expected_inv: str, fn) -> None:
    try:
        fn()
    except OracleViolation as error:
        if error.invariant_id != expected_inv:
            raise AssertionError(f"Expected {expected_inv}, got {error.invariant_id}: {error}") from error
        return
    raise AssertionError(f"Negative control failed to catch {expected_inv}")


def _base_broker(*, orders=None, trades=None, long_qty=0, active_children=None, pending_fact_count=0):
    return {
        "contract_id": CONTRACT,
        "contracts": [CONTRACT],
        "positions": {"long_today": long_qty, "long_historical": 0, "short_today": 0, "short_historical": 0, "long_qty": long_qty, "short_qty": 0},
        "positions_by_contract": {
            CONTRACT: {"long_today": long_qty, "long_historical": 0, "short_today": 0, "short_historical": 0, "long_qty": long_qty, "short_qty": 0},
        },
        "orders": orders or {},
        "trades": trades or {},
        "active_children": active_children or [],
        "pending_fact_count": pending_fact_count,
    }


def _base_workbench(*, boot_id="boot-1", owned=0, orders=None, trades=None, cycles=None,
                    clock_fault=None, feed_blocked=False, entry_authority="DISARMED", config_revision="1"):
    return {
        "kernel_boot_id": boot_id,
        "entry_authority": entry_authority,
        "current_responsibility": {"owned_quantity": owned},
        "orders": orders or [],
        "trades": trades or [],
        "cycles": cycles or [],
        "connection": {"clock_fault": clock_fault},
        "configuration": {
            "config_revision": config_revision,
            "market_health": {"status": "FEED_SUSPECTED_STALLED" if feed_blocked else "HEALTHY", "feed_entry_blocked": feed_blocked},
        },
    }


def run(work_dir: Path) -> dict[str, object]:
    assert_offline_qa_environment(block_tqsdk=True)
    trace = TraceRecorder(work_dir / "evidence", scenario_name="E2E-STATEFUL-002-oracle-negative-control", run_id="neg-ctrl")

    # INV-009: Boot mismatch
    o = ReferenceOracle()
    o.set_boot_expectation("boot-expected")
    _expect_violation("INV-009", lambda: o.verify(broker_snapshot=_base_broker(), workbench_view=_base_workbench(boot_id="boot-wrong"), round_seq=1, frame_id="f1"))

    # INV-010: OPEN outbound during clock fault or feed block
    o = ReferenceOracle()
    _expect_violation("INV-010", lambda: o.verify(
        broker_snapshot=_base_broker(),
        workbench_view=_base_workbench(clock_fault="MONOTONIC_DISCONTINUITY"),
        round_seq=1,
        frame_id="f2",
        outbound_events=[{"event_type": "OUTBOUND_SEND", "request": {"native_offset": "OPEN", "contract_id": CONTRACT}}],
    ))
    _expect_violation("INV-010", lambda: o.verify(
        broker_snapshot=_base_broker(),
        workbench_view=_base_workbench(feed_blocked=True),
        round_seq=1,
        frame_id="f3",
        outbound_events=[{"event_type": "OUTBOUND_SEND", "request": {"native_offset": "OPEN", "contract_id": CONTRACT}}],
    ))

    # INV-001 / INV-002 / INV-003: Excess open capacity
    for inv, order_row in (
        ("INV-002", {"request": {"contract_id": CONTRACT, "native_offset": "OPEN", "quantity": 6, "child_order_id": "c1"}, "status": "SEND_REQUESTED", "traded_qty": 0, "cancel_effect_ids": []}),
        ("INV-003", {"request": {"contract_id": CONTRACT, "native_offset": "OPEN", "quantity": 6, "child_order_id": "c2"}, "status": "ALIVE", "traded_qty": 0, "cancel_effect_ids": ["x1"]}),
    ):
        o = ReferenceOracle()
        o.set_target_expectation(CONTRACT, direction="LONG", quantity=5)
        _expect_violation(inv, lambda r=order_row: o.verify(broker_snapshot=_base_broker(orders={"c": r}), workbench_view=_base_workbench(), round_seq=1, frame_id="f4"))

    # INV-004: Duplicate trade ID in projection
    o = ReferenceOracle()
    _expect_violation("INV-004", lambda: o.verify(
        broker_snapshot=_base_broker(),
        workbench_view=_base_workbench(trades=[{"native_trade_id": "t1"}, {"native_trade_id": "t1"}]),
        round_seq=1,
        frame_id="f5",
    ))

    # INV-006 / INV-007: Missing or non-zero exit responsibility while holding
    o = ReferenceOracle()
    o.set_target_expectation(CONTRACT, direction="LONG", quantity=5, exit_target=0)
    _expect_violation("INV-006", lambda: o.verify(
        broker_snapshot=_base_broker(long_qty=5),
        workbench_view=_base_workbench(owned=5, cycles=[]),
        round_seq=1,
        frame_id="f6",
    ))
    _expect_violation("INV-007", lambda: o.verify(
        broker_snapshot=_base_broker(long_qty=5),
        workbench_view=_base_workbench(owned=5, cycles=[{"contract_id": CONTRACT, "target_remaining_quantity": 5}]),
        round_seq=1,
        frame_id="f7",
    ))

    # INV-015: Owned quantity divergence
    o = ReferenceOracle()
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=_base_broker(long_qty=5),
        workbench_view=_base_workbench(owned=0),
        round_seq=1,
        frame_id="f8",
    ))

    # INV-009: Authority & config_revision mismatch
    o = ReferenceOracle()
    o.set_boot_expectation("boot-1", config_revision="2")
    o.set_authority_expectation("ARMED")
    _expect_violation("INV-009", lambda: o.verify(
        broker_snapshot=_base_broker(),
        workbench_view=_base_workbench(entry_authority="DISARMED", config_revision="2"),
        round_seq=1, frame_id="f9",
    ))
    _expect_violation("INV-009", lambda: o.verify(
        broker_snapshot=_base_broker(),
        workbench_view=_base_workbench(entry_authority="ARMED", config_revision="1"),
        round_seq=1, frame_id="f10",
    ))

    # INV-005: Close order exceeds closable position
    o = ReferenceOracle()
    close_order = {
        "request": {"contract_id": CONTRACT, "direction": "SELL", "native_offset": "CLOSETODAY", "quantity": 6, "child_order_id": "c-close"},
        "status": "ALIVE", "traded_qty": 0, "cancel_effect_ids": [],
    }
    _expect_violation("INV-005", lambda: o.verify(
        broker_snapshot=_base_broker(long_qty=5, orders={"c-close": close_order}, active_children=["c-close"]),
        workbench_view=_base_workbench(owned=5, cycles=[{"contract_id": CONTRACT, "direction": "LONG", "remaining_quantity": 5}]),
        round_seq=1, frame_id="f11",
    ))

    # INV-015: Per-contract attribution swap (same total 5 lots, wrong contract)
    other = "SHFE.ag2612"
    multi_broker = _base_broker(long_qty=5)
    multi_broker["contracts"] = [CONTRACT, other]
    multi_broker["positions_by_contract"][other] = {
        "long_today": 0, "long_historical": 0, "short_today": 0, "short_historical": 0, "long_qty": 0, "short_qty": 0,
    }
    o = ReferenceOracle()
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=multi_broker,
        workbench_view=_base_workbench(owned=5, cycles=[{"contract_id": other, "direction": "LONG", "remaining_quantity": 5}]),
        round_seq=1, frame_id="f12",
    ))

    # INV-015: Direction flip (LONG vs SHORT, same total 5 lots)
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=_base_broker(long_qty=5),
        workbench_view=_base_workbench(owned=5, cycles=[{"contract_id": CONTRACT, "direction": "SHORT", "remaining_quantity": 5}]),
        round_seq=1, frame_id="f13",
    ))

    # INV-015: Trade price mismatch (same trade ID, wrong price)
    broker_trade = {
        "native_trade_id": "t-1", "contract_id": CONTRACT, "direction": "BUY",
        "native_offset": "OPEN", "quantity": 5, "price": "1060",
    }
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=_base_broker(long_qty=5, trades={"t-1": broker_trade}),
        workbench_view=_base_workbench(
            owned=5,
            cycles=[{"contract_id": CONTRACT, "direction": "LONG", "remaining_quantity": 5}],
            trades=[{
                "native_trade_id": "t-1", "contract_id": CONTRACT, "direction": "LONG",
                "quantity": 5, "price": "1065", "effect": "OPEN", "offset_kind": "OPEN",
            }],
        ),
        round_seq=1, frame_id="f14",
    ))

    def _confirmed_broker_order(*, child_id="c-1", contract=CONTRACT, direction="BUY", offset="OPEN",
                                qty=5, price="1060", status="ALIVE", traded=0, native_id="nat-1"):
        return {
            "request": {
                "child_order_id": child_id, "contract_id": contract,
                "direction": direction, "native_offset": offset,
                "quantity": qty, "limit_price": price,
            },
            "status": status,
            "traded_qty": traded,
            "reported_filled_qty": traded,
            "native_order_id": native_id,
            "cancel_effect_ids": [],
        }

    def _valid_rt_order(*, child_id="c-1", contract=CONTRACT, direction="LONG", effect="OPEN",
                        offset="OPEN", qty=5, price="1060", filled=0, remaining=5,
                        send_state="NATIVE_SEEN", broker_state="ALIVE", native_id="nat-1",
                        terminal_confirmed=False):
        return {
            "child_order_id": child_id,
            "contract_id": contract,
            "direction": direction,
            "effect": effect,
            "native_offset": offset,
            "original_qty": qty,
            "limit_price": price,
            "filled_qty": filled,
            "potential_remaining": remaining,
            "send_state": send_state,
            "broker_state": broker_state,
            "native_order_id": native_id,
            "terminal_confirmed": terminal_confirmed,
            "order_source": "STRATEGY",
        }

    # Positive control: SEND_REQUESTED without native_order_id is allowed during pre-ack transition
    o = ReferenceOracle()
    o.set_boot_expectation("boot-1", config_revision="1")
    o.set_target_expectation(CONTRACT, direction="LONG", quantity=5)
    o.verify(
        broker_snapshot=_base_broker(orders={
            "c-pending": _confirmed_broker_order(
                child_id="c-pending", status="SEND_REQUESTED", native_id=None,
            )
        }),
        workbench_view=_base_workbench(orders=[
            _valid_rt_order(
                child_id="c-pending", send_state="CALL_RETURNED", broker_state=None, native_id=None,
            )
        ]),
        round_seq=1, frame_id="f15-pre-ack-ok",
    )

    # INV-015: Order original_qty 5 -> 3 mismatch
    o = ReferenceOracle()
    o.set_target_expectation(CONTRACT, direction="LONG", quantity=5)
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=_base_broker(orders={"c-1": _confirmed_broker_order()}, active_children=["c-1"]),
        workbench_view=_base_workbench(orders=[_valid_rt_order(qty=3, remaining=3)]),
        round_seq=1, frame_id="f16-qty",
    ))

    # INV-015: Order limit_price mismatch
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=_base_broker(orders={"c-1": _confirmed_broker_order(price="1060")}, active_children=["c-1"]),
        workbench_view=_base_workbench(orders=[_valid_rt_order(price="1062")]),
        round_seq=1, frame_id="f17-price",
    ))

    # INV-015: Order remaining quantity (potential_remaining) mismatch
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=_base_broker(orders={"c-1": _confirmed_broker_order(qty=5, traded=2)}, active_children=["c-1"]),
        workbench_view=_base_workbench(orders=[_valid_rt_order(qty=5, filled=2, remaining=5)]),
        round_seq=1, frame_id="f18-rem",
    ))

    # INV-015: Close today misrecorded as close yesterday (SHFE CLOSETODAY vs CLOSEYESTERDAY)
    shfe = "SHFE.ag2702"
    shfe_broker = _base_broker(long_qty=5)
    shfe_broker["contract_id"] = shfe
    shfe_broker["contracts"] = [shfe]
    shfe_broker["positions_by_contract"] = {
        shfe: {"long_today": 5, "long_historical": 0, "short_today": 0, "short_historical": 0, "long_qty": 5, "short_qty": 0}
    }
    shfe_broker["orders"] = {
        "c-close": _confirmed_broker_order(
            child_id="c-close", contract=shfe, direction="SELL", offset="CLOSETODAY", qty=5, price="5000",
        )
    }
    shfe_broker["active_children"] = ["c-close"]
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=shfe_broker,
        workbench_view=_base_workbench(
            owned=5,
            cycles=[{"contract_id": shfe, "direction": "LONG", "remaining_quantity": 5}],
            orders=[_valid_rt_order(
                child_id="c-close", contract=shfe, direction="LONG", effect="REDUCE",
                offset="CLOSEYESTERDAY", qty=5, price="5000",
            )],
        ),
        round_seq=1, frame_id="f19-offset",
    ))

    # INV-015: Trade close today misrecorded as close yesterday
    shfe_trade_broker = _base_broker(long_qty=0)
    shfe_trade_broker["contract_id"] = shfe
    shfe_trade_broker["contracts"] = [shfe]
    shfe_trade_broker["positions_by_contract"] = {
        shfe: {"long_today": 0, "long_historical": 0, "short_today": 0, "short_historical": 0, "long_qty": 0, "short_qty": 0}
    }
    shfe_trade_broker["trades"] = {
        "t-close": {
            "native_trade_id": "t-close", "contract_id": shfe, "direction": "SELL",
            "native_offset": "CLOSETODAY", "quantity": 5, "price": "5000",
        }
    }
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=shfe_trade_broker,
        workbench_view=_base_workbench(
            trades=[{
                "native_trade_id": "t-close", "contract_id": shfe, "direction": "LONG",
                "quantity": 5, "price": "5000", "effect": "REDUCE", "offset_kind": "CLOSEYESTERDAY",
            }],
        ),
        round_seq=1, frame_id="f20-trade-offset",
    ))

    # INV-015: Order direction LONG misrecorded as SHORT
    _expect_violation("INV-015", lambda: o.verify(
        broker_snapshot=_base_broker(orders={"c-1": _confirmed_broker_order()}, active_children=["c-1"]),
        workbench_view=_base_workbench(orders=[_valid_rt_order(direction="SHORT")]),
        round_seq=1, frame_id="f21-order-dir",
    ))

    # INV-015: Confirmed order missing required fields (native_order_id / broker_state / limit_price / effect)
    for tag, drop_key in (
        ("native_order_id", "native_order_id"),
        ("broker_state", "broker_state"),
        ("limit_price", "limit_price"),
        ("effect", "effect"),
        ("native_offset", "native_offset"),
        ("potential_remaining", "potential_remaining"),
    ):
        bad_order = _valid_rt_order()
        bad_order.pop(drop_key)
        _expect_violation("INV-015", lambda bo=bad_order, t=tag: o.verify(
            broker_snapshot=_base_broker(orders={"c-1": _confirmed_broker_order()}, active_children=["c-1"]),
            workbench_view=_base_workbench(orders=[bo]),
            round_seq=1, frame_id=f"f22-missing-{t}",
        ))

    # INV-009: Missing kernel_boot_id or config_revision even without explicit expectations
    o_plain = ReferenceOracle()
    _expect_violation("INV-009", lambda: o_plain.verify(
        broker_snapshot=_base_broker(),
        workbench_view=_base_workbench(boot_id=""),
        round_seq=1, frame_id="f23-missing-boot",
    ))
    _expect_violation("INV-009", lambda: o_plain.verify(
        broker_snapshot=_base_broker(),
        workbench_view=_base_workbench(config_revision=""),
        round_seq=1, frame_id="f24-missing-rev",
    ))

    trace.record("COMMAND", operation="NEGATIVE_CONTROL_24_INVARIANTS", receipt_status="PASS")
    return trace.finalize(passed=True)
