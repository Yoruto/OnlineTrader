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


def _base_workbench(*, boot_id="boot-1", owned=0, orders=None, trades=None, cycles=None, clock_fault=None, feed_blocked=False):
    return {
        "kernel_boot_id": boot_id,
        "current_responsibility": {"owned_quantity": owned},
        "orders": orders or [],
        "trades": trades or [],
        "cycles": cycles or [],
        "connection": {"clock_fault": clock_fault},
        "configuration": {"market_health": {"status": "FEED_SUSPECTED_STALLED" if feed_blocked else "HEALTHY", "feed_entry_blocked": feed_blocked}},
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

    trace.record("COMMAND", operation="NEGATIVE_CONTROL_9_INVARIANTS", receipt_status="PASS")
    return trace.finalize(passed=True)
