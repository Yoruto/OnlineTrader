"""Independent Reference Oracle and safety invariants (INV-001..INV-015).

Derives expected account state solely from FakeBroker facts, observed outbound
effects and explicit scenario/strategy expectations — never from Runtime
internal state variables.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


class OracleViolation(AssertionError):
    """Raised when Runtime public state violates an independent safety invariant."""

    def __init__(
        self,
        invariant_id: str,
        message: str,
        *,
        round_seq: int = 0,
        frame_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self.invariant_id = invariant_id
        self.round_seq = round_seq
        self.frame_id = frame_id
        self.details = details or {}
        super().__init__(f"[{invariant_id}] round={round_seq} frame={frame_id}: {message}")


@dataclass
class ExpectedAccountState:
    """Independent account state derived from FakeBroker truth."""

    long_today: int = 0
    long_historical: int = 0
    short_today: int = 0
    short_historical: int = 0
    orders: dict[str, dict[str, object]] = field(default_factory=dict)
    trades: dict[str, dict[str, object]] = field(default_factory=dict)
    effective_targets: dict[str, dict[str, object]] = field(default_factory=dict)
    pending_open_quantity: dict[str, int] = field(default_factory=dict)
    pending_close_quantity: dict[str, int] = field(default_factory=dict)
    exit_responsibilities: dict[str, dict[str, object]] = field(default_factory=dict)
    unowned_positions: dict[str, dict[str, int]] = field(default_factory=dict)
    entry_authority: str = "DISARMED"
    configuration_revision: str = "1"
    runtime_boot_id: str | None = None


class ReferenceOracle:
    """Independent safety oracle checking INV-001..INV-015 against FakeBroker truth."""

    def __init__(self) -> None:
        self.expected_targets: dict[str, dict[str, object]] = {}
        self.expected_entry_authority: str | None = None
        self.expected_config_revision: str | None = None
        self.expected_boot_id: str | None = None
        self.seen_trade_ids: set[str] = set()
        self.exit_active: dict[str, dict[str, object]] = {}
        self.expected_unowned_positions: dict[str, dict[str, int]] = {}

    def set_unowned_position(self, contract_id: str, *, long_qty: int = 0, short_qty: int = 0) -> None:
        self.expected_unowned_positions[contract_id] = {
            "long_qty": int(long_qty),
            "short_qty": int(short_qty),
        }

    def set_target_expectation(
        self,
        contract_id: str,
        *,
        direction: str,
        quantity: int,
        exit_target: int | None = None,
    ) -> None:
        self.expected_targets[contract_id] = {
            "direction": direction,
            "quantity": int(quantity),
            "exit_target": exit_target,
        }
        if exit_target is not None:
            self.exit_active[contract_id] = {
                "direction": direction,
                "exit_target": int(exit_target),
            }

    def set_authority_expectation(self, authority: str) -> None:
        self.expected_entry_authority = authority

    def set_boot_expectation(self, boot_id: str, config_revision: str = "1") -> None:
        self.expected_boot_id = boot_id
        self.expected_config_revision = str(config_revision)

    def derive_state(self, broker_snapshot: dict[str, object]) -> ExpectedAccountState:
        positions = broker_snapshot["positions"]
        orders = broker_snapshot["orders"]
        trades = broker_snapshot["trades"]
        pending_open: dict[str, int] = {}
        pending_close: dict[str, int] = {}

        for order in orders.values():
            req = order["request"]
            contract = str(req["contract_id"])
            status = str(order["status"])
            if status not in {"SEND_REQUESTED", "ALIVE"}:
                continue
            rem = max(0, int(req["quantity"]) - int(order["traded_qty"]))
            if req["native_offset"] == "OPEN":
                pending_open[contract] = pending_open.get(contract, 0) + rem
            else:
                pending_close[contract] = pending_close.get(contract, 0) + rem

        return ExpectedAccountState(
            long_today=int(positions["long_today"]),
            long_historical=int(positions["long_historical"]),
            short_today=int(positions["short_today"]),
            short_historical=int(positions["short_historical"]),
            orders=deepcopy(orders),
            trades=deepcopy(trades),
            effective_targets=deepcopy(self.expected_targets),
            pending_open_quantity=pending_open,
            pending_close_quantity=pending_close,
            exit_responsibilities=deepcopy(self.exit_active),
            entry_authority=self.expected_entry_authority or "DISARMED",
            configuration_revision=self.expected_config_revision or "1",
            runtime_boot_id=self.expected_boot_id,
        )

    def verify(
        self,
        *,
        broker_snapshot: dict[str, object],
        workbench_view: dict[str, object],
        round_seq: int,
        frame_id: str | None,
    ) -> ExpectedAccountState:
        """Verify INV-001..INV-015 invariants on the current round."""
        state = self.derive_state(broker_snapshot)
        contract = str(broker_snapshot["contract_id"])
        broker_pos = broker_snapshot["positions"]
        broker_long = int(broker_pos["long_qty"])
        broker_short = int(broker_pos["short_qty"])
        broker_net = broker_long + broker_short

        # 1. Boot & configuration identity checks
        if self.expected_boot_id is not None and workbench_view.get("kernel_boot_id") != self.expected_boot_id:
            raise OracleViolation(
                "INV-009",
                f"Workbench kernel_boot_id {workbench_view.get('kernel_boot_id')!r} "
                f"!= expected {self.expected_boot_id!r}",
                round_seq=round_seq,
                frame_id=frame_id,
            )

        # 2. Capacity & duplicate/UNKNOWN/cancel-in-flight checks (INV-001, INV-002, INV-003, INV-005)
        open_orders = [
            o for o in state.orders.values()
            if o["request"]["native_offset"] == "OPEN" and o["status"] in {"SEND_REQUESTED", "ALIVE"}
        ]
        unknown_open = [o for o in open_orders if o["status"] == "SEND_REQUESTED"]
        cancelling_open = [o for o in open_orders if o["cancel_effect_ids"] and o["status"] == "ALIVE"]

        target_spec = self.expected_targets.get(contract)
        if target_spec is not None:
            direction = target_spec["direction"]
            target_qty = int(target_spec["quantity"])
            broker_owned = broker_long if direction == "LONG" else broker_short
            pending_open_qty = state.pending_open_quantity.get(contract, 0)
            potential_total = broker_owned + pending_open_qty
            if potential_total > target_qty:
                inv_id = (
                    "INV-002" if unknown_open else
                    "INV-003" if cancelling_open else
                    "INV-001" if len(open_orders) > 1 else
                    "INV-005"
                )
                raise OracleViolation(
                    inv_id,
                    f"Potential open exposure {potential_total} (owned={broker_owned} + "
                    f"pending_open={pending_open_qty}) exceeds target {target_qty}",
                    round_seq=round_seq,
                    frame_id=frame_id,
                    details={
                        "broker_owned": broker_owned,
                        "pending_open": pending_open_qty,
                        "target_qty": target_qty,
                        "open_orders": [o["request"]["child_order_id"] for o in open_orders],
                    },
                )

        # 3. INV-004: Trade deduplication in Runtime projection
        runtime_trades = workbench_view.get("trades") or []
        seen_runtime_trade_ids: set[str] = set()
        for trade in runtime_trades:
            tid = str(trade.get("native_trade_id") or "")
            if tid and tid in seen_runtime_trade_ids:
                raise OracleViolation(
                    "INV-004",
                    f"Duplicate native_trade_id in Runtime projection: {tid}",
                    round_seq=round_seq,
                    frame_id=frame_id,
                )
            if tid:
                seen_runtime_trade_ids.add(tid)

        # 4. INV-015: Only enforce strict Broker <-> Runtime convergence when no facts are pending
        pending_fact_count = int(broker_snapshot.get("pending_fact_count", 0))
        if pending_fact_count == 0:
            unowned = self.expected_unowned_positions.get(contract) or {}
            expected_unowned_net = int(unowned.get("long_qty", 0)) + int(unowned.get("short_qty", 0))
            expected_owned_net = max(0, broker_net - expected_unowned_net)
            runtime_owned = int((workbench_view.get("current_responsibility") or {}).get("owned_quantity", 0))
            if runtime_owned != expected_owned_net:
                raise OracleViolation(
                    "INV-015",
                    f"Runtime owned_quantity={runtime_owned} disagrees with expected owned={expected_owned_net} "
                    f"(broker_net={broker_net}, unowned={expected_unowned_net})",
                    round_seq=round_seq,
                    frame_id=frame_id,
                    details={"runtime_owned": runtime_owned, "broker_positions": broker_pos},
                )

            broker_active_ids = set(broker_snapshot["active_children"])
            runtime_active_ids = {
                str(row["child_order_id"])
                for row in (workbench_view.get("orders") or [])
                if not row.get("terminal_confirmed")
                and row.get("send_state") != "DEFINITELY_NOT_SENT"
                and row.get("broker_state") not in {"CANCELLED", "FILLED", "REJECTED"}
            }
            # Note: SEND_REQUESTED (UNKNOWN) orders may exist in Runtime before Broker assigns native_order_id.
            undecided_ids = {
                cid for cid, o in state.orders.items() if o["status"] == "SEND_REQUESTED"
            }
            missing_in_runtime = broker_active_ids - runtime_active_ids
            unexpected_in_runtime = runtime_active_ids - broker_active_ids - undecided_ids
            if missing_in_runtime or unexpected_in_runtime:
                raise OracleViolation(
                    "INV-015",
                    f"Active orders diverge between Runtime and FakeBroker: "
                    f"missing={sorted(missing_in_runtime)}, unexpected={sorted(unexpected_in_runtime)}",
                    round_seq=round_seq,
                    frame_id=frame_id,
                )

            broker_trade_ids = set(state.trades.keys())
            if broker_trade_ids != seen_runtime_trade_ids:
                raise OracleViolation(
                    "INV-015",
                    f"Confirmed trade IDs diverge: broker={sorted(broker_trade_ids)}, "
                    f"runtime={sorted(seen_runtime_trade_ids)}",
                    round_seq=round_seq,
                    frame_id=frame_id,
                )

            # INV-006 / INV-007: Exit responsibility cannot disappear while Broker still holds position
            exit_spec = self.exit_active.get(contract)
            if exit_spec is not None and broker_net > int(exit_spec["exit_target"]):
                cycles = [
                    c for c in (workbench_view.get("cycles") or [])
                    if c.get("contract_id") == contract
                ]
                if not cycles:
                    raise OracleViolation(
                        "INV-006",
                        f"Exit responsibility disappeared while Broker still holds {broker_net} lots",
                        round_seq=round_seq,
                        frame_id=frame_id,
                    )
                target_rem = cycles[0].get("target_remaining_quantity")
                if target_rem is None or int(target_rem) > int(exit_spec["exit_target"]):
                    raise OracleViolation(
                        "INV-007",
                        f"Cycle target_remaining_quantity={target_rem} failed to preserve "
                        f"exit target {exit_spec['exit_target']}",
                        round_seq=round_seq,
                        frame_id=frame_id,
                    )

        return state
