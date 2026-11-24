"""Independent Reference Oracle and safety invariants (INV-001..INV-015).

Derives expected account state solely from FakeBroker facts, observed outbound
effects and explicit scenario/strategy expectations — never from Runtime
internal state variables.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


def _strategy_direction(broker_direction: str, native_offset: str) -> str:
    return "LONG" if (broker_direction == "BUY") == (native_offset == "OPEN") else "SHORT"


def _same_decimal(left: object, right: object) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        return str(left) == str(right)


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
    positions_by_contract: dict[str, dict[str, int]] = field(default_factory=dict)
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

    def set_config_revision_expectation(self, config_revision: str) -> None:
        self.expected_config_revision = str(config_revision)

    def derive_state(self, broker_snapshot: dict[str, object]) -> ExpectedAccountState:
        positions = broker_snapshot["positions"]
        positions_by_contract = broker_snapshot.get("positions_by_contract") or {
            str(broker_snapshot["contract_id"]): positions
        }
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
            positions_by_contract=deepcopy(positions_by_contract),
            orders=deepcopy(orders),
            trades=deepcopy(trades),
            effective_targets=deepcopy(self.expected_targets),
            pending_open_quantity=pending_open,
            pending_close_quantity=pending_close,
            exit_responsibilities=deepcopy(self.exit_active),
            unowned_positions=deepcopy(self.expected_unowned_positions),
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
        outbound_events: list[dict[str, object]] | None = None,
    ) -> ExpectedAccountState:
        """Verify INV-001..INV-015 invariants on the current round."""
        state = self.derive_state(broker_snapshot)
        contracts = tuple(broker_snapshot.get("contracts") or (str(broker_snapshot["contract_id"]),))
        positions_by_contract = state.positions_by_contract

        # 1. Boot, authority & configuration identity checks (INV-009)
        if self.expected_boot_id is not None and workbench_view.get("kernel_boot_id") != self.expected_boot_id:
            raise OracleViolation(
                "INV-009",
                f"Workbench kernel_boot_id {workbench_view.get('kernel_boot_id')!r} "
                f"!= expected {self.expected_boot_id!r}",
                round_seq=round_seq,
                frame_id=frame_id,
            )
        if self.expected_entry_authority is not None:
            actual_auth = workbench_view.get("entry_authority")
            clock_fault = (workbench_view.get("connection") or {}).get("clock_fault")
            expected_auth = "DISARMED" if clock_fault else self.expected_entry_authority
            if actual_auth != expected_auth:
                raise OracleViolation(
                    "INV-009",
                    f"Workbench entry_authority {actual_auth!r} != expected {expected_auth!r}",
                    round_seq=round_seq,
                    frame_id=frame_id,
                )
        if self.expected_config_revision is not None:
            actual_rev = (workbench_view.get("configuration") or {}).get("config_revision")
            if str(actual_rev) != self.expected_config_revision:
                raise OracleViolation(
                    "INV-009",
                    f"Workbench config_revision {actual_rev!r} != expected {self.expected_config_revision!r}",
                    round_seq=round_seq,
                    frame_id=frame_id,
                )

        # 1b. INV-010: No new OPEN order permitted under clock fault or blocked feed health
        if outbound_events:
            conn = workbench_view.get("connection") or {}
            market_health = ((workbench_view.get("configuration") or {}).get("market_health") or {})
            if conn.get("clock_fault") or market_health.get("feed_entry_blocked"):
                open_sends = [
                    ev for ev in outbound_events
                    if ev.get("event_type") == "OUTBOUND_SEND"
                    and (ev.get("request") or {}).get("native_offset") == "OPEN"
                ]
                if open_sends:
                    raise OracleViolation(
                        "INV-010",
                        "New OPEN order emitted while clock_fault or feed_entry_blocked was active",
                        round_seq=round_seq,
                        frame_id=frame_id,
                        details={
                            "clock_fault": conn.get("clock_fault"),
                            "feed_status": market_health.get("status"),
                            "open_sends": [ev.get("request") for ev in open_sends],
                        },
                    )

        # 2. Capacity & duplicate/UNKNOWN/cancel-in-flight checks per contract (INV-001, INV-002, INV-003, INV-005)
        for contract in contracts:
            broker_pos = positions_by_contract.get(contract) or broker_snapshot["positions"]
            broker_long = int(broker_pos["long_qty"])
            broker_short = int(broker_pos["short_qty"])
            open_orders = [
                o for o in state.orders.values()
                if o["request"]["contract_id"] == contract
                and o["request"]["native_offset"] == "OPEN"
                and o["status"] in {"SEND_REQUESTED", "ALIVE"}
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
                        f"[{contract}] Potential open exposure {potential_total} (owned={broker_owned} + "
                        f"pending_open={pending_open_qty}) exceeds target {target_qty}",
                        round_seq=round_seq,
                        frame_id=frame_id,
                        details={
                            "contract_id": contract,
                            "broker_owned": broker_owned,
                            "pending_open": pending_open_qty,
                            "target_qty": target_qty,
                            "open_orders": [o["request"]["child_order_id"] for o in open_orders],
                        },
                    )

            # INV-005: Pending close quantity per side and today/historical bucket cannot exceed closable position
            close_orders = [
                o for o in state.orders.values()
                if o["request"]["contract_id"] == contract
                and o["request"]["native_offset"] in {"CLOSE", "CLOSETODAY", "CLOSEYESTERDAY"}
                and o["status"] in {"SEND_REQUESTED", "ALIVE"}
            ]
            close_totals = {
                ("LONG", "ALL"): 0, ("LONG", "TODAY"): 0, ("LONG", "HIST"): 0,
                ("SHORT", "ALL"): 0, ("SHORT", "TODAY"): 0, ("SHORT", "HIST"): 0,
            }
            for order in close_orders:
                req = order["request"]
                rem = max(0, int(req["quantity"]) - int(order["traded_qty"]))
                side = _strategy_direction(str(req["direction"]), str(req["native_offset"]))
                close_totals[(side, "ALL")] += rem
                if req["native_offset"] == "CLOSETODAY":
                    close_totals[(side, "TODAY")] += rem
                elif req["native_offset"] == "CLOSEYESTERDAY":
                    close_totals[(side, "HIST")] += rem
            closable_limits = (
                ("LONG", "ALL", broker_long),
                ("LONG", "TODAY", int(broker_pos.get("long_today", broker_long))),
                ("LONG", "HIST", int(broker_pos.get("long_historical", broker_long))),
                ("SHORT", "ALL", broker_short),
                ("SHORT", "TODAY", int(broker_pos.get("short_today", broker_short))),
                ("SHORT", "HIST", int(broker_pos.get("short_historical", broker_short))),
            )
            for side, bucket, limit in closable_limits:
                pending_close = close_totals[(side, bucket)]
                if pending_close > limit:
                    raise OracleViolation(
                        "INV-005",
                        f"[{contract}] Pending {side}/{bucket} close quantity {pending_close} "
                        f"exceeds closable broker position {limit}",
                        round_seq=round_seq,
                        frame_id=frame_id,
                        details={
                            "contract_id": contract,
                            "side": side,
                            "bucket": bucket,
                            "pending_close": pending_close,
                            "closable": limit,
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

        # 4. INV-015 / INV-006 / INV-007: Strict Broker <-> Runtime convergence when no facts are pending
        pending_fact_count = int(broker_snapshot.get("pending_fact_count", 0))
        if pending_fact_count == 0:
            # Check INV-006 / INV-007 first so missing exit responsibility reports the specific invariant.
            for contract in contracts:
                broker_pos = positions_by_contract.get(contract) or broker_snapshot["positions"]
                broker_net = int(broker_pos["long_qty"]) + int(broker_pos["short_qty"])
                exit_spec = self.exit_active.get(contract)
                if exit_spec is not None and broker_net > int(exit_spec["exit_target"]):
                    cycles = [
                        c for c in (workbench_view.get("cycles") or [])
                        if c.get("contract_id") == contract
                    ]
                    if not cycles:
                        raise OracleViolation(
                            "INV-006",
                            f"[{contract}] Exit responsibility disappeared while Broker still holds {broker_net} lots",
                            round_seq=round_seq,
                            frame_id=frame_id,
                        )
                    target_rem = cycles[0].get("target_remaining_quantity")
                    if target_rem is None or int(target_rem) > int(exit_spec["exit_target"]):
                        raise OracleViolation(
                            "INV-007",
                            f"[{contract}] Cycle target_remaining_quantity={target_rem} failed to preserve "
                            f"exit target {exit_spec['exit_target']}",
                            round_seq=round_seq,
                            frame_id=frame_id,
                        )

            # Aggregate and per-(contract, direction) owned position convergence
            runtime_owned_by_side: dict[tuple[str, str], int] = {}
            for cycle in (workbench_view.get("cycles") or []):
                qty = int(cycle.get("remaining_quantity") or 0)
                if qty <= 0:
                    continue
                key = (str(cycle.get("contract_id") or ""), str(cycle.get("direction") or ""))
                runtime_owned_by_side[key] = runtime_owned_by_side.get(key, 0) + qty

            expected_owned_by_side: dict[tuple[str, str], int] = {}
            expected_total_owned = 0
            for contract in contracts:
                broker_pos = positions_by_contract.get(contract) or broker_snapshot["positions"]
                unowned = self.expected_unowned_positions.get(contract) or {}
                exp_long = max(0, int(broker_pos["long_qty"]) - int(unowned.get("long_qty", 0)))
                exp_short = max(0, int(broker_pos["short_qty"]) - int(unowned.get("short_qty", 0)))
                if exp_long:
                    expected_owned_by_side[(contract, "LONG")] = exp_long
                if exp_short:
                    expected_owned_by_side[(contract, "SHORT")] = exp_short
                expected_total_owned += exp_long + exp_short

            runtime_owned = int((workbench_view.get("current_responsibility") or {}).get("owned_quantity", 0))
            if runtime_owned != expected_total_owned:
                raise OracleViolation(
                    "INV-015",
                    f"Runtime owned_quantity={runtime_owned} disagrees with expected owned={expected_total_owned}",
                    round_seq=round_seq,
                    frame_id=frame_id,
                    details={"runtime_owned": runtime_owned, "positions_by_contract": positions_by_contract},
                )
            if runtime_owned_by_side != expected_owned_by_side:
                raise OracleViolation(
                    "INV-015",
                    f"Per-contract/direction owned positions diverge: runtime={runtime_owned_by_side}, "
                    f"expected={expected_owned_by_side}",
                    round_seq=round_seq,
                    frame_id=frame_id,
                    details={
                        "runtime_owned_by_side": {f"{c}:{d}": q for (c, d), q in runtime_owned_by_side.items()},
                        "expected_owned_by_side": {f"{c}:{d}": q for (c, d), q in expected_owned_by_side.items()},
                    },
                )

            runtime_orders = workbench_view.get("orders") or []
            runtime_orders_by_id = {str(row["child_order_id"]): row for row in runtime_orders if row.get("child_order_id")}
            broker_active_ids = set(broker_snapshot["active_children"])
            runtime_active_ids = {
                str(row["child_order_id"])
                for row in runtime_orders
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
            for cid, broker_order in state.orders.items():
                rt_order = runtime_orders_by_id.get(cid)
                if rt_order is None:
                    continue
                req = broker_order["request"]
                expected_dir = _strategy_direction(str(req["direction"]), str(req["native_offset"]))
                if rt_order.get("contract_id") is not None and rt_order.get("contract_id") != req["contract_id"]:
                    raise OracleViolation(
                        "INV-015",
                        f"Order {cid} contract_id {rt_order.get('contract_id')!r} != {req['contract_id']!r}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                if rt_order.get("direction") is not None and rt_order.get("direction") != expected_dir:
                    raise OracleViolation(
                        "INV-015",
                        f"Order {cid} direction {rt_order.get('direction')!r} != {expected_dir!r}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                if rt_order.get("original_qty") is not None and int(rt_order["original_qty"]) != int(req["quantity"]):
                    raise OracleViolation(
                        "INV-015",
                        f"Order {cid} original_qty {rt_order.get('original_qty')} != {req['quantity']}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                if rt_order.get("filled_qty") is not None and int(rt_order["filled_qty"]) != int(broker_order["traded_qty"]):
                    raise OracleViolation(
                        "INV-015",
                        f"Order {cid} filled_qty {rt_order.get('filled_qty')} != broker traded_qty {broker_order['traded_qty']}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                broker_status = str(broker_order["status"])
                if broker_status in {"ALIVE", "FILLED", "CANCELLED", "REJECTED"} and rt_order.get("broker_state") is not None:
                    if rt_order.get("broker_state") != broker_status:
                        raise OracleViolation(
                            "INV-015",
                            f"Order {cid} broker_state {rt_order.get('broker_state')!r} != {broker_status!r}",
                            round_seq=round_seq, frame_id=frame_id,
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
            runtime_trades_by_id = {
                str(t.get("native_trade_id")): t for t in runtime_trades if t.get("native_trade_id")
            }
            for tid, broker_trade in state.trades.items():
                rt_trade = runtime_trades_by_id[tid]
                expected_dir = _strategy_direction(
                    str(broker_trade["direction"]), str(broker_trade["native_offset"])
                )
                expected_effects = {"OPEN"} if broker_trade["native_offset"] == "OPEN" else {"CLOSE", "REDUCE"}
                if rt_trade.get("contract_id") != broker_trade["contract_id"]:
                    raise OracleViolation(
                        "INV-015",
                        f"Trade {tid} contract_id {rt_trade.get('contract_id')!r} != {broker_trade['contract_id']!r}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                if int(rt_trade.get("quantity") or 0) != int(broker_trade["quantity"]):
                    raise OracleViolation(
                        "INV-015",
                        f"Trade {tid} quantity {rt_trade.get('quantity')} != {broker_trade['quantity']}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                if not _same_decimal(rt_trade.get("price"), broker_trade["price"]):
                    raise OracleViolation(
                        "INV-015",
                        f"Trade {tid} price {rt_trade.get('price')!r} != {broker_trade['price']!r}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                if rt_trade.get("direction") != expected_dir:
                    raise OracleViolation(
                        "INV-015",
                        f"Trade {tid} direction {rt_trade.get('direction')!r} != {expected_dir!r}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                if rt_trade.get("effect") is not None and rt_trade.get("effect") not in expected_effects:
                    raise OracleViolation(
                        "INV-015",
                        f"Trade {tid} effect {rt_trade.get('effect')!r} not in {sorted(expected_effects)}",
                        round_seq=round_seq, frame_id=frame_id,
                    )
                if rt_trade.get("offset_kind") is not None and rt_trade.get("offset_kind") != broker_trade["native_offset"]:
                    raise OracleViolation(
                        "INV-015",
                        f"Trade {tid} offset_kind {rt_trade.get('offset_kind')!r} != {broker_trade['native_offset']!r}",
                        round_seq=round_seq, frame_id=frame_id,
                    )

        return state
