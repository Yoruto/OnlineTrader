"""Four-mode simulated broker matcher separated from Runtime logic.

Matches outbound orders against Layer-1 market truth (never software-observed stale quotes),
respects available simulated book liquidity without double-spending it across orders,
and supports deterministic fault injection (no-ack, reject, partial fill, late fill, fact hold).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from online_trader.domain.values import decimal_text
from qa.e2e.scenario import BrokerFacade
from qa.scenario_engine.models import BrokerMatchMode, FaultRule, MarketTickTruth


@dataclass
class MatcherStats:
    accepted: int = 0
    rejected: int = 0
    fills: int = 0
    partial_fills: int = 0
    late_fills: int = 0
    cancels_confirmed: int = 0
    replayed_trades: int = 0
    no_ack_held: int = 0
    trade_log: list[dict[str, object]] = field(default_factory=list)


class OrderMatcher:
    """Drives FakeBroker transitions according to BrokerMatchMode and active FaultRules."""

    def __init__(
        self,
        *,
        broker: BrokerFacade,
        mode: BrokerMatchMode = BrokerMatchMode.BOOK_CONSTRAINED,
        conservative_delay_rounds: int = 2,
    ) -> None:
        self._broker = broker
        self.mode = mode
        self.conservative_delay_rounds = max(1, int(conservative_delay_rounds))
        self.stats = MatcherStats()
        self._order_first_seen_round: dict[str, int] = {}
        self._no_ack_orders: set[str] = set()
        self._late_fill_injected: set[str] = set()
        self._replayed_trade_ids: set[str] = set()
        self._facts_held: bool = False
        # Track consumed simulated top-of-book liquidity per (symbol, source_time_ns, side)
        self._consumed_liquidity: dict[tuple[str, int, str], int] = {}

    def _available_liquidity(self, truth: MarketTickTruth, *, side: str) -> int:
        raw_vol = truth.ask_volume1 if side == "ASK" else truth.bid_volume1
        if raw_vol is None:
            # Recorded book without volume field: assume bounded default of 10 lots per tick
            raw_vol = 10
        key = (truth.symbol, truth.source_time_ns, side)
        used = self._consumed_liquidity.get(key, 0)
        return max(0, int(raw_vol) - used)

    def _consume_liquidity(self, truth: MarketTickTruth, *, side: str, qty: int) -> None:
        key = (truth.symbol, truth.source_time_ns, side)
        self._consumed_liquidity[key] = self._consumed_liquidity.get(key, 0) + int(qty)

    def _can_fill_book(
        self,
        *,
        direction: str,
        limit_price: Decimal,
        remaining_qty: int,
        truth: MarketTickTruth,
    ) -> tuple[int, str | None]:
        """Return ``(fillable_qty, fill_price)`` under BOOK_CONSTRAINED rules."""
        if not truth.market_session_active:
            return 0, None
        if direction == "BUY":
            if truth.ask1 is None:
                return 0, None
            ask_dec = Decimal(truth.ask1)
            if limit_price < ask_dec:
                return 0, None
            avail = self._available_liquidity(truth, side="ASK")
            qty = min(remaining_qty, avail)
            if qty <= 0:
                return 0, None
            self._consume_liquidity(truth, side="ASK", qty=qty)
            return qty, decimal_text(ask_dec)
        else:
            if truth.bid1 is None:
                return 0, None
            bid_dec = Decimal(truth.bid1)
            if limit_price > bid_dec:
                return 0, None
            avail = self._available_liquidity(truth, side="BID")
            qty = min(remaining_qty, avail)
            if qty <= 0:
                return 0, None
            self._consume_liquidity(truth, side="BID", qty=qty)
            return qty, decimal_text(bid_dec)

    def step(
        self,
        *,
        round_seq: int,
        wall_time: str,
        market_truth: dict[str, MarketTickTruth],
        active_rules: list[FaultRule],
        outbound_events: list[dict[str, object]],
    ) -> list[str]:
        """Process broker matching and return trading event tags observed this round."""
        observed_events: list[str] = []

        # 1. Manage broker return-channel fact holding / release
        hold_requested = any(r.broker_hold_facts for r in active_rules)
        release_reverse = any(r.broker_release_reverse for r in active_rules)
        if hold_requested and not self._facts_held:
            self._broker.set_fact_delivery_hold(True)
            self._broker.hold_pending_facts()
            self._facts_held = True
        elif not hold_requested and self._facts_held:
            self._broker.set_fact_delivery_hold(False)
            self._broker.release_held_facts(reverse_order=release_reverse)
            self._facts_held = False

        # 2. Classify outbound events for trigger evaluation
        open_send_contracts: set[str] = set()
        for ev in outbound_events:
            ev_type = ev.get("event_type")
            req = ev.get("request") or {}
            if ev_type == "OUTBOUND_SEND":
                if req.get("native_offset") == "OPEN":
                    observed_events.append("OPEN_ORDER_SENT")
                    if req.get("contract_id"):
                        open_send_contracts.add(str(req["contract_id"]))
                else:
                    observed_events.append("CLOSE_ORDER_SENT")
            elif ev_type == "OUTBOUND_CANCEL":
                observed_events.append("CANCEL_REQUESTED")
        if len(open_send_contracts) >= 2:
            observed_events.append("MULTI_CONTRACT_ORDER")

        # 3. Evaluate all orders in FakeBroker
        snap = self._broker.snapshot()
        orders: dict[str, dict[str, object]] = snap["orders"]  # type: ignore[assignment]

        for cid, order in orders.items():
            req = order["request"]  # type: ignore[index]
            sym = str(req["contract_id"])
            status = str(order["status"])
            qty = int(req["quantity"])
            traded = int(order["traded_qty"])
            rem = max(0, qty - traded)
            direction = str(req["direction"])
            limit_price_str = str(req["limit_price"])
            limit_price = Decimal(limit_price_str)

            if cid not in self._order_first_seen_round:
                self._order_first_seen_round[cid] = round_seq

            sym_rules = [r for r in active_rules if not r.contracts or sym in r.contracts]
            no_ack_active = any(r.broker_no_ack for r in sym_rules)
            reject_active = any(r.broker_reject for r in sym_rules)
            partial_caps = [r.broker_partial_fill_qty for r in sym_rules if r.broker_partial_fill_qty is not None]
            late_fill_caps = [r.broker_late_fill_qty for r in sym_rules if r.broker_late_fill_qty is not None]
            replay_trade_active = any(r.broker_replay_trade for r in sym_rules)

            # Handle undecided SEND_REQUESTED orders
            if status == "SEND_REQUESTED":
                if no_ack_active:
                    if cid not in self._no_ack_orders:
                        self._no_ack_orders.add(cid)
                        self.stats.no_ack_held += 1
                    continue
                if reject_active:
                    self._broker.reject(cid)
                    self.stats.rejected += 1
                    continue
                if self.mode == BrokerMatchMode.CONSERVATIVE:
                    if round_seq - self._order_first_seen_round[cid] < 1:
                        continue
                self._broker.accept(cid)
                self.stats.accepted += 1
                self._no_ack_orders.discard(cid)
                status = "ALIVE"

            if status != "ALIVE" or rem <= 0:
                continue

            truth = market_truth.get(sym)

            # Handle cancel requests (including cancel-race late fill)
            if order.get("cancel_effect_ids"):
                self._broker.confirm_cancel(cid)
                self.stats.cancels_confirmed += 1
                if late_fill_caps and cid not in self._late_fill_injected and rem > 0:
                    late_qty = min(rem, max(1, int(late_fill_caps[0])))
                    fill_px = truth.last if truth is not None else limit_price_str
                    _, trade_fact = self._broker.late_fill(cid, late_qty, fill_px)
                    self._late_fill_injected.add(cid)
                    self.stats.late_fills += 1
                    self.stats.trade_log.append({
                        "round_seq": round_seq,
                        "wall_time": wall_time,
                        "child_order_id": cid,
                        "contract_id": sym,
                        "kind": "LATE_FILL",
                        "quantity": late_qty,
                        "price": fill_px,
                        "native_trade_id": trade_fact.get("native_trade_id"),
                    })
                    observed_events.append("LATE_FILL")
                    if replay_trade_active and trade_fact.get("native_trade_id"):
                        tid = str(trade_fact["native_trade_id"])
                        if tid not in self._replayed_trade_ids:
                            self._broker.replay_trade_fact(tid)
                            self._replayed_trade_ids.add(tid)
                            self.stats.replayed_trades += 1
                continue

            # Determine fill quantity & price by mode
            fill_qty = 0
            fill_price: str | None = None

            if self.mode == BrokerMatchMode.IDEAL:
                fill_qty = rem
                fill_price = limit_price_str
            elif self.mode == BrokerMatchMode.BOOK_CONSTRAINED:
                if truth is not None:
                    fill_qty, fill_price = self._can_fill_book(
                        direction=direction,
                        limit_price=limit_price,
                        remaining_qty=rem,
                        truth=truth,
                    )
            elif self.mode == BrokerMatchMode.CONSERVATIVE:
                age = round_seq - self._order_first_seen_round[cid]
                if age >= self.conservative_delay_rounds and truth is not None:
                    avail_qty, fill_price = self._can_fill_book(
                        direction=direction,
                        limit_price=limit_price,
                        remaining_qty=rem,
                        truth=truth,
                    )
                    if avail_qty > 0:
                        fill_qty = max(1, avail_qty // 2) if avail_qty > 1 else avail_qty
            elif self.mode == BrokerMatchMode.FAULTY:
                if truth is not None:
                    fill_qty, fill_price = self._can_fill_book(
                        direction=direction,
                        limit_price=limit_price,
                        remaining_qty=rem,
                        truth=truth,
                    )
                else:
                    fill_qty, fill_price = rem, limit_price_str

            # Apply explicit partial fill cap from active fault rule
            if fill_qty > 0 and partial_caps:
                cap = max(1, int(partial_caps[0]))
                if traded == 0 and rem > cap:
                    fill_qty = min(fill_qty, cap)
                elif traded >= cap:
                    # Keep remainder resting while partial-fill fault rule is active
                    fill_qty = 0

            if fill_qty > 0 and fill_price is not None:
                _, trade_fact = self._broker.fill(cid, fill_qty, fill_price)
                self.stats.fills += 1
                is_partial = (traded + fill_qty) < qty
                if is_partial:
                    self.stats.partial_fills += 1
                    observed_events.append("PARTIAL_FILL")
                observed_events.append("FILL")
                self.stats.trade_log.append({
                    "round_seq": round_seq,
                    "wall_time": wall_time,
                    "child_order_id": cid,
                    "contract_id": sym,
                    "kind": "PARTIAL_FILL" if is_partial else "FILL",
                    "quantity": fill_qty,
                    "price": fill_price,
                    "native_trade_id": trade_fact.get("native_trade_id"),
                })
                if replay_trade_active and trade_fact.get("native_trade_id"):
                    tid = str(trade_fact["native_trade_id"])
                    if tid not in self._replayed_trade_ids:
                        self._broker.replay_trade_fact(tid)
                        self._replayed_trade_ids.add(tid)
                        self.stats.replayed_trades += 1

        return observed_events

    def flush_held_facts(self, *, reverse_order: bool = False) -> None:
        if self._facts_held:
            self._broker.set_fact_delivery_hold(False)
            self._broker.release_held_facts(reverse_order=reverse_order)
            self._facts_held = False
