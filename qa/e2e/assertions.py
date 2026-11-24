"""Public-interface and FakeBroker assertions for V3.1 E2E scenarios."""

from __future__ import annotations


def assert_entry_authority(workbench_view: dict[str, object], expected: str) -> None:
    actual = workbench_view.get("entry_authority")
    if actual != expected:
        raise AssertionError(f"Expected entry_authority={expected!r}, got {actual!r}")


def assert_positions(
    *,
    broker_snapshot: dict[str, object],
    workbench_view: dict[str, object],
    long_qty: int = 0,
    short_qty: int = 0,
) -> None:
    pos = broker_snapshot["positions"]
    b_long = int(pos["long_qty"])
    b_short = int(pos["short_qty"])
    if b_long != long_qty or b_short != short_qty:
        raise AssertionError(
            f"Expected FakeBroker positions (long={long_qty}, short={short_qty}), "
            f"got (long={b_long}, short={b_short})"
        )
    resp = workbench_view.get("current_responsibility") or {}
    w_owned = int(resp.get("owned_quantity", 0))
    expected_net = long_qty + short_qty
    if w_owned != expected_net:
        raise AssertionError(
            f"Expected Workbench owned_quantity={expected_net}, got {w_owned}"
        )


def assert_no_active_orders(
    *,
    broker_snapshot: dict[str, object],
    workbench_view: dict[str, object],
) -> None:
    active_broker = list(broker_snapshot.get("active_children") or [])
    if active_broker:
        raise AssertionError(f"Expected 0 active FakeBroker orders, found {active_broker}")
    active_wb = int((workbench_view.get("current_responsibility") or {}).get("active_orders", 0))
    if active_wb != 0:
        raise AssertionError(f"Expected 0 active Workbench orders, found {active_wb}")


def assert_cycle_exit_reason(
    workbench_view: dict[str, object],
    contract_id: str,
    expected_reason: str,
) -> None:
    cycles = [
        c for c in (workbench_view.get("cycles") or [])
        if c.get("contract_id") == contract_id
    ]
    if not cycles:
        raise AssertionError(f"No cycle found for {contract_id} while checking exit_reason={expected_reason!r}")
    actual = cycles[0].get("exit_reason")
    if actual != expected_reason:
        raise AssertionError(f"Expected cycle exit_reason={expected_reason!r}, got {actual!r}")
