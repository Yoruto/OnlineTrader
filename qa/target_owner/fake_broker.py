"""Parent-side, event-driven Fake Broker for the QA Target Owner harness.

Outbound adapter calls are observations only. Broker acceptance, order updates,
trades and position changes happen only through explicit methods below.
This module is QA-only and never connects to a real gateway.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from threading import RLock

from online_trader.domain.values import decimal_text, decimal_value, quantity
from online_trader.errors import TraderError
from qa.target_owner.offline_runtime import CONTRACT, DAY


class FakeBrokerError(ValueError):
    """The requested fake broker transition contradicts its explicit facts."""


@dataclass
class FakeBrokerOrder:
    request: dict[str, object]
    send_effect_id: str
    send_adapter_result: str
    native_order_id: str | None = None
    status: str = "SEND_REQUESTED"
    reported_filled_qty: int = 0
    traded_qty: int = 0
    cancel_effect_ids: list[str] = field(default_factory=list)


class FakeBroker:
    """One-contract offline broker model with separately ordered native facts.

    The model starts flat. Call ``observe_adapter_event`` for Owner outputs,
    then explicitly call ``accept_order``, ``report_order``, ``report_trade``
    or ``confirm_cancel`` to create broker facts. ``prepare_frame`` snapshots
    those facts and current broker positions for the next Owner round.
    """

    def __init__(self, *, account_key: str, contract: str = CONTRACT,
                 trading_day: str = DAY, provider: str = "OFFLINE",
                 session_id: str = "qa-fake-broker-session",
                 trace_path: str | Path,
                 run_id: str | None = None):
        if not isinstance(account_key, str) or not account_key.startswith("DEV:"):
            raise FakeBrokerError("Fake Broker requires a DEV account key")
        if contract != CONTRACT:
            raise FakeBrokerError(f"This first Fake Broker model supports only {CONTRACT}")
        if trading_day != DAY:
            raise FakeBrokerError(f"This first Fake Broker model is fixed to {DAY}")
        if provider != "OFFLINE":
            raise FakeBrokerError("Fake Broker provider must be OFFLINE")
        if not isinstance(session_id, str) or not session_id:
            raise FakeBrokerError("A stable fake session id is required")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            raise FakeBrokerError("A nonempty harness run id is required")

        self.account_key = account_key
        self.contract = contract
        self.trading_day = trading_day
        self.provider = provider
        self.session_id = session_id
        self.run_id = run_id or "qa-fake-broker"
        self._orders: dict[str, FakeBrokerOrder] = {}
        self._native_orders: dict[str, str] = {}
        self._trades: dict[str, dict[str, object]] = {}
        self._positions = {
            "long_today": 0, "long_historical": 0,
            "short_today": 0, "short_historical": 0,
        }
        self._pending_facts: list[dict[str, object]] = []
        self._sequence = 0
        self._last_monotonic_ms = -1
        self._next_order_id = 1
        self._next_trade_id = 1
        self._trace_seq = 0
        self._events: list[dict[str, object]] = []
        self._trace_path = Path(trace_path).resolve()
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        self._trace_path.open("x", encoding="utf-8").close()
        self._inflight: dict[str, object] | None = None
        self._inflight_fact_count = 0
        self._delivery_cursor: int | None = None
        self._delivery_started = False
        self._lock = RLock()

    @property
    def orders(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                child: {
                    "request": deepcopy(order.request),
                    "send_effect_id": order.send_effect_id,
                    "send_adapter_result": order.send_adapter_result,
                    "native_order_id": order.native_order_id,
                    "status": order.status,
                    "reported_filled_qty": order.reported_filled_qty,
                    "traded_qty": order.traded_qty,
                    "cancel_effect_ids": list(order.cancel_effect_ids),
                }
                for child, order in self._orders.items()
            }

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(deepcopy(self._events))

    @property
    def pending_facts(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(deepcopy(self._pending_facts))

    @property
    def positions(self) -> dict[str, int]:
        with self._lock:
            return self._position_snapshot()

    def observe_adapter_event(self, event: dict[str, object]) -> None:
        """Record a Fake Adapter call without inventing a broker response."""
        if not isinstance(event, dict):
            raise FakeBrokerError("Adapter observation must be an object")
        event_type = event.get("event_type")
        request = event.get("request")
        if event_type not in {"OUTBOUND_SEND", "OUTBOUND_CANCEL"} or not isinstance(request, dict):
            raise FakeBrokerError("Expected an observed outbound send or cancel")
        if event.get("adapter_result") not in {"RETURNED", "ERROR"}:
            raise FakeBrokerError("Adapter observation has no explicit call result")

        with self._lock:
            child_id = request.get("child_order_id")
            effect_id = request.get("id")
            if not isinstance(child_id, str) or not child_id or not isinstance(effect_id, str) or not effect_id:
                raise FakeBrokerError("Outbound request lacks child and effect identities")
            if (request.get("account_key") != self.account_key or
                    request.get("provider") != self.provider or
                    request.get("contract_id") != self.contract or
                    request.get("trading_day") != self.trading_day):
                raise FakeBrokerError("Outbound request is outside this fake account scope")

            if event_type == "OUTBOUND_SEND":
                if request.get("kind") != "SEND":
                    raise FakeBrokerError("Observed send does not contain a SEND effect")
                direction = request.get("direction")
                offset = request.get("native_offset")
                if direction not in {"BUY", "SELL"} or offset not in {
                        "OPEN", "CLOSE", "CLOSETODAY", "CLOSEYESTERDAY"}:
                    raise FakeBrokerError("Fake Broker does not recognize the order side or offset")
                qty = self._quantity(request.get("quantity"), positive=True)
                limit_price = self._price(request.get("limit_price"))
                clean_request = deepcopy(request)
                clean_request["quantity"] = qty
                clean_request["limit_price"] = limit_price
                prior = self._orders.get(child_id)
                if prior is None:
                    self._orders[child_id] = FakeBrokerOrder(
                        request=clean_request,
                        send_effect_id=effect_id,
                        send_adapter_result=str(event["adapter_result"]),
                    )
                elif prior.request != clean_request or prior.send_effect_id != effect_id:
                    raise FakeBrokerError("A child id was reused for a different send request")
                self._record("OUTBOUND_SEND_OBSERVED", child_order_id=child_id,
                             effect_id=effect_id, adapter_result=event["adapter_result"],
                             request=clean_request)
                return

            if request.get("kind") != "CANCEL":
                raise FakeBrokerError("Observed cancel does not contain a CANCEL effect")
            order = self._orders.get(child_id)
            if order is None or order.native_order_id is None:
                raise FakeBrokerError("Cancel request has no previously accepted fake order")
            if request.get("native_order_id") != order.native_order_id:
                raise FakeBrokerError("Cancel request changed the accepted native order identity")
            if effect_id not in order.cancel_effect_ids:
                order.cancel_effect_ids.append(effect_id)
            self._record("OUTBOUND_CANCEL_OBSERVED", child_order_id=child_id,
                         effect_id=effect_id, native_order_id=order.native_order_id,
                         adapter_result=event["adapter_result"], broker_state=order.status)

    def accept_order(self, child_order_id: str, *, native_order_id: str | None = None) -> dict[str, object]:
        """Explicitly accept an observed send and queue an ALIVE order fact."""
        with self._lock:
            order = self._send_order(child_order_id)
            if order.status != "SEND_REQUESTED":
                raise FakeBrokerError("Order acceptance was already decided")
            native_id = native_order_id or f"fake-native-order-{self._next_order_id:06d}"
            self._next_order_id += 1
            self._bind_native_id(child_order_id, order, native_id)
            fact = self._queue_order_fact(order, "ALIVE", 0)
            self._record("ORDER_ACCEPTED", child_order_id=child_order_id,
                         native_order_id=native_id, fact=fact)
            return deepcopy(fact)

    def reject_order(self, child_order_id: str, *, native_order_id: str | None = None) -> dict[str, object]:
        """Explicitly reject an observed send; a send call alone is not acceptance."""
        with self._lock:
            order = self._send_order(child_order_id)
            if order.status != "SEND_REQUESTED" or order.traded_qty or order.reported_filled_qty:
                raise FakeBrokerError("Only an undecided, unfilled order can be rejected")
            native_id = native_order_id or f"fake-native-order-{self._next_order_id:06d}"
            self._next_order_id += 1
            self._bind_native_id(child_order_id, order, native_id)
            fact = self._queue_order_fact(order, "REJECTED", 0)
            self._record("ORDER_REJECTED", child_order_id=child_order_id,
                         native_order_id=native_id, fact=fact)
            return deepcopy(fact)

    def report_order(self, child_order_id: str, status: str, filled_qty: int) -> dict[str, object]:
        """Queue a native order status independently from trade detail."""
        with self._lock:
            order = self._accepted_order(child_order_id)
            fact = self._queue_order_fact(order, status, filled_qty)
            self._record("ORDER_FACT_CREATED", child_order_id=child_order_id,
                         native_order_id=order.native_order_id, fact=fact)
            return deepcopy(fact)

    def report_trade(self, child_order_id: str, trade_qty: int, price: str | int | Decimal,
                     *, native_trade_id: str | None = None,
                     fill_day: str | None = None) -> dict[str, object]:
        """Queue one unique trade fact and update broker position from that fill."""
        with self._lock:
            order = self._accepted_order(child_order_id)
            qty = self._quantity(trade_qty, positive=True)
            if order.traded_qty + qty > int(order.request["quantity"]):
                raise FakeBrokerError("Trade quantity exceeds the original order")
            next_total = order.traded_qty + qty
            if order.status in {"CANCELLED", "FILLED", "REJECTED"} and next_total > order.reported_filled_qty:
                raise FakeBrokerError("Terminal order needs a matching cumulative update before a late trade")
            if order.status not in {"ALIVE", "CANCELLED", "FILLED"}:
                raise FakeBrokerError("Trade requires an accepted, non-rejected order")
            if fill_day is not None and fill_day != self.trading_day:
                raise FakeBrokerError("This first Fake Broker model supports same-day fills only")
            trade_id = native_trade_id or f"fake-native-trade-{self._next_trade_id:06d}"
            if (not isinstance(trade_id, str) or not trade_id or len(trade_id) > 512
                    or not trade_id.isprintable()):
                raise FakeBrokerError("Native trade id must be nonempty, printable and bounded")
            self._next_trade_id += 1
            if trade_id in self._trades:
                raise FakeBrokerError("Native trade id must be unique")
            price_text = self._price(price)
            self._book_position(order, qty)
            fact = self._base_fact(order) | {
                "kind": "TRADE", "native_trade_id": trade_id,
                "quantity": qty, "price": price_text,
            }
            if fill_day is not None:
                fact["fill_day"] = fill_day
            order.traded_qty = next_total
            self._trades[trade_id] = deepcopy(fact)
            self._pending_facts.append(deepcopy(fact))
            self._record("TRADE_FACT_CREATED", child_order_id=child_order_id,
                         native_order_id=order.native_order_id, fact=fact,
                         positions=self._position_snapshot())
            return deepcopy(fact)

    def fill(self, child_order_id: str, trade_qty: int, price: str | int | Decimal,
             *, native_trade_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
        """Convenience transition: cumulative order update followed by one trade."""
        with self._lock:
            order = self._accepted_order(child_order_id)
            if order.status != "ALIVE":
                raise FakeBrokerError("Use late_fill for a cancelled order")
            qty = self._quantity(trade_qty, positive=True)
            total = order.traded_qty + qty
            if total > int(order.request["quantity"]):
                raise FakeBrokerError("Trade quantity exceeds the original order")
            before = dict(self._positions)
            self._book_position(order, qty)
            self._positions = before
            order_fact = self._queue_order_fact(
                order, "FILLED" if total == int(order.request["quantity"]) else "ALIVE",
                max(order.reported_filled_qty, total))
            trade_fact = self.report_trade(
                child_order_id, qty, price, native_trade_id=native_trade_id)
            self._record("ORDER_FILLED_OR_PARTIALLY_FILLED", child_order_id=child_order_id,
                         order_fact=order_fact, trade_fact=trade_fact)
            return deepcopy(order_fact), deepcopy(trade_fact)

    def confirm_cancel(self, child_order_id: str) -> dict[str, object]:
        """Confirm a previously observed cancellation request with terminal facts."""
        with self._lock:
            order = self._accepted_order(child_order_id)
            if not order.cancel_effect_ids:
                raise FakeBrokerError("No outbound cancellation request was observed")
            if order.status != "ALIVE":
                raise FakeBrokerError("Only an ALIVE order can receive a cancel confirmation")
            fact = self._queue_order_fact(order, "CANCELLED", order.reported_filled_qty)
            self._record("CANCEL_CONFIRMED", child_order_id=child_order_id,
                         native_order_id=order.native_order_id, fact=fact)
            return deepcopy(fact)

    def late_fill(self, child_order_id: str, trade_qty: int, price: str | int | Decimal,
                  *, native_trade_id: str | None = None) -> tuple[dict[str, object], dict[str, object]]:
        """Model a trade arriving after cancellation, with its cumulative correction."""
        with self._lock:
            order = self._accepted_order(child_order_id)
            if order.status != "CANCELLED":
                raise FakeBrokerError("Late fill requires a broker-confirmed CANCELLED order")
            qty = self._quantity(trade_qty, positive=True)
            total = order.traded_qty + qty
            if total > int(order.request["quantity"]):
                raise FakeBrokerError("Late trade quantity exceeds the original order")
            before = dict(self._positions)
            self._book_position(order, qty)
            self._positions = before
            order_fact = self._queue_order_fact(order, "CANCELLED", max(order.reported_filled_qty, total))
            trade_fact = self.report_trade(
                child_order_id, qty, price, native_trade_id=native_trade_id)
            self._record("LATE_TRADE_AFTER_CANCEL", child_order_id=child_order_id,
                         order_fact=order_fact, trade_fact=trade_fact)
            return deepcopy(order_fact), deepcopy(trade_fact)

    def replay_trade_fact(self, native_trade_id: str) -> dict[str, object]:
        """Re-emit an existing fact without changing broker positions."""
        with self._lock:
            fact = self._trades.get(native_trade_id)
            if fact is None:
                raise FakeBrokerError("Cannot replay an unknown native trade")
            self._pending_facts.append(deepcopy(fact))
            self._record("TRADE_FACT_REDELIVERED", native_trade_id=native_trade_id,
                         fact=fact, positions=self._position_snapshot())
            return deepcopy(fact)

    def prepare_frame(self, *, wall_time: str, monotonic_ms: int,
                      last: str | int | Decimal = "1060",
                      bid1: str | int | Decimal | None = None,
                      ask1: str | int | Decimal | None = None,
                      candle_id: str | None = None, complete: bool = True,
                      synchronized: bool | None = None) -> dict[str, object]:
        """Prepare one explicit-time Owner frame; acknowledge it after APPLIED."""
        with self._lock:
            if self._inflight is not None:
                raise FakeBrokerError("A broker frame is already prepared; deliver or resolve it first")
            stamp = self._utc(wall_time)
            if type(monotonic_ms) is not int or monotonic_ms < 0 or monotonic_ms <= self._last_monotonic_ms:
                raise FakeBrokerError("Scenario monotonic time must move forward explicitly")
            if type(complete) is not bool:
                raise FakeBrokerError("Frame completeness must be explicit")
            if synchronized is None:
                synchronized = complete
            if type(synchronized) is not bool or synchronized and not complete:
                raise FakeBrokerError("An incomplete frame cannot claim synchronization")
            if candle_id is not None and not isinstance(candle_id, str):
                raise FakeBrokerError("Candle id must be a string or None")

            last_text = self._price(last)
            bid_text = last_text if bid1 is None else self._price(bid1)
            ask_text = last_text if ask1 is None else self._price(ask1)
            position = self._position_snapshot()
            facts = deepcopy(self._pending_facts) if complete else []
            active_children = [child for child, order in self._orders.items()
                               if order.status == "ALIVE" and order.native_order_id is not None]
            sequence = self._sequence + 1
            frame_id = f"fake-broker-round-{sequence}"
            frame = {
                "id": frame_id,
                "sequence": sequence,
                "session_id": self.session_id,
                "wall_time": stamp,
                "monotonic_ms": monotonic_ms,
                "complete": complete,
                "synchronized": synchronized,
                "sync_orders": active_children,
                "market": [{
                    "contract_id": self.contract,
                    "ticks": [{
                        "native_time": stamp, "candle_id": candle_id, "last": last_text,
                        "day_low": "1020", "day_high": "1088", "bid1": bid_text,
                        "ask1": ask_text, "tick_size": "1", "lower_limit": "900",
                        "upper_limit": "1200",
                    }],
                    "opening_session": True,
                    "session_open": f"{self.trading_day}T01:00:00Z",
                    "session_close": f"{self.trading_day}T02:15:00Z",
                    "long_qty": position["long_qty"], "short_qty": position["short_qty"],
                    "available": "100000", "margin_per_lot": "1000",
                    "external_orders": False,
                }],
                "native": facts,
            }
            self._sequence = sequence
            self._last_monotonic_ms = monotonic_ms
            self._inflight = deepcopy(frame)
            self._inflight_fact_count = len(facts)
            self._delivery_cursor = None
            self._delivery_started = False
            self._record("BROKER_FRAME_PREPARED", frame_id=frame_id,
                         sequence=sequence, native_fact_count=len(facts),
                         complete=complete, synchronized=synchronized,
                         positions=position)
            return deepcopy(frame)

    def deliver_frame(self, owner_process, **frame_inputs) -> dict[str, object]:
        """Submit one prepared frame and acknowledge it only after Owner APPLIED."""
        with self._lock:
            if self._inflight is None:
                if not frame_inputs:
                    raise FakeBrokerError("Frame inputs are required for a new delivery")
                frame = self.prepare_frame(**frame_inputs)
                self._delivery_cursor = owner_process.event_cursor
            else:
                if frame_inputs:
                    raise FakeBrokerError("Resume the prepared delivery without changing its frame")
                frame = deepcopy(self._inflight)
                if self._delivery_cursor is None:
                    self._delivery_cursor = owner_process.event_cursor
            cursor = self._delivery_cursor
            should_send = not self._delivery_started
            self._delivery_started = True

        if should_send:
            owner_process.send_fact_frame(frame)
        for event in owner_process.events[cursor:]:
            if (event.get("event_type") == "FACT_FRAME_REJECTED" and
                    event.get("frame_id") == frame["id"]):
                raise FakeBrokerError(f"Owner rejected broker frame: {event.get('reason_code')}")
        applied = owner_process.wait_for_frame_applied(
            frame["id"], after=cursor)
        self.acknowledge_frame(frame["id"])
        return applied

    def acknowledge_frame(self, frame_id: str) -> None:
        """Commit the fact prefix only after the Owner reports this frame applied."""
        with self._lock:
            if self._inflight is None or self._inflight.get("id") != frame_id:
                raise FakeBrokerError("Applied frame identity differs from the prepared broker frame")
            del self._pending_facts[:self._inflight_fact_count]
            self._record("BROKER_FRAME_APPLIED", frame_id=frame_id,
                         sequence=self._inflight["sequence"],
                         consumed_fact_count=self._inflight_fact_count)
            self._inflight = None
            self._inflight_fact_count = 0
            self._delivery_cursor = None
            self._delivery_started = False

    def _send_order(self, child_order_id: str) -> FakeBrokerOrder:
        order = self._orders.get(child_order_id)
        if order is None:
            raise FakeBrokerError("No matching outbound send was observed")
        return order

    def _accepted_order(self, child_order_id: str) -> FakeBrokerOrder:
        order = self._send_order(child_order_id)
        if order.native_order_id is None or order.status == "SEND_REQUESTED":
            raise FakeBrokerError("Broker has not explicitly accepted or rejected this send")
        return order

    def _bind_native_id(self, child_order_id: str, order: FakeBrokerOrder, native_order_id: str) -> None:
        if (not isinstance(native_order_id, str) or not native_order_id or
                len(native_order_id) > 512 or not native_order_id.isprintable()):
            raise FakeBrokerError("Native order id must be nonempty")
        prior = self._native_orders.get(native_order_id)
        if prior is not None and prior != child_order_id:
            raise FakeBrokerError("Native order id is already bound to another child")
        order.native_order_id = native_order_id
        self._native_orders[native_order_id] = child_order_id

    def _queue_order_fact(self, order: FakeBrokerOrder, status: str,
                          filled_qty: int) -> dict[str, object]:
        if order.native_order_id is None:
            raise FakeBrokerError("Native order status requires an assigned native id")
        if status not in {"ALIVE", "FILLED", "CANCELLED", "REJECTED"}:
            raise FakeBrokerError("Unsupported fake native order status")
        filled = self._quantity(filled_qty)
        original = int(order.request["quantity"])
        if (filled > original or filled < order.traded_qty or
                filled < order.reported_filled_qty):
            raise FakeBrokerError("Cumulative order fill contradicts trade facts")
        if status == "FILLED" and filled != original:
            raise FakeBrokerError("FILLED status requires the full original quantity")
        if status == "REJECTED" and filled:
            raise FakeBrokerError("REJECTED status cannot report fills")
        if status == "ALIVE" and filled == original:
            raise FakeBrokerError("A fully filled order cannot remain ALIVE")
        current = order.status
        if current == "SEND_REQUESTED":
            if status == "ALIVE":
                raise FakeBrokerError("Use accept_order to report initial acceptance")
        elif current in {"CANCELLED", "FILLED", "REJECTED"}:
            if status != current:
                raise FakeBrokerError("Terminal broker status cannot change")
            if current != "CANCELLED" and filled != order.reported_filled_qty:
                raise FakeBrokerError("Only CANCELLED may receive a cumulative late-fill correction")
            if filled < order.reported_filled_qty:
                raise FakeBrokerError("Terminal cumulative fill cannot move backward")
        elif status == "REJECTED" and order.traded_qty:
            raise FakeBrokerError("A traded order cannot be rejected")

        order.status = status
        order.reported_filled_qty = filled
        fact = self._base_fact(order) | {"kind": "ORDER", "status": status, "filled_qty": filled}
        self._pending_facts.append(deepcopy(fact))
        return fact

    def _base_fact(self, order: FakeBrokerOrder) -> dict[str, object]:
        if order.native_order_id is None:
            raise FakeBrokerError("Native fact requires a broker order identity")
        request = order.request
        return {
            "account_key": self.account_key,
            "provider": self.provider,
            "contract_id": self.contract,
            "direction": request["direction"],
            "native_offset": request["native_offset"],
            "child_order_id": request["child_order_id"],
            "native_order_id": order.native_order_id,
            "trading_day": self.trading_day,
        }

    def _book_position(self, order: FakeBrokerOrder, qty: int) -> None:
        request = order.request
        offset = request["native_offset"]
        direction = request["direction"]
        if offset == "OPEN":
            side = "long" if direction == "BUY" else "short"
            self._positions[f"{side}_today"] += qty
            return
        side = "short" if direction == "BUY" else "long"
        if offset == "CLOSETODAY":
            key = f"{side}_today"
            if self._positions[key] < qty:
                raise FakeBrokerError("Close fill exceeds same-day broker position")
            self._positions[key] -= qty
            return
        if offset == "CLOSEYESTERDAY":
            key = f"{side}_historical"
            if self._positions[key] < qty:
                raise FakeBrokerError("Close fill exceeds historical broker position")
            self._positions[key] -= qty
            return
        today_key, historical_key = f"{side}_today", f"{side}_historical"
        available = self._positions[today_key] + self._positions[historical_key]
        if available < qty:
            raise FakeBrokerError("Close fill exceeds broker position")
        from_today = min(qty, self._positions[today_key])
        self._positions[today_key] -= from_today
        self._positions[historical_key] -= qty - from_today

    def _position_snapshot(self) -> dict[str, int]:
        values = dict(self._positions)
        values["long_qty"] = values["long_today"] + values["long_historical"]
        values["short_qty"] = values["short_today"] + values["short_historical"]
        return values

    @staticmethod
    def _quantity(value: object, *, positive: bool = False) -> int:
        try:
            return quantity(value, positive=positive)
        except TraderError as error:
            raise FakeBrokerError("Invalid fake broker quantity") from error

    @staticmethod
    def _price(value: str | int | Decimal) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise FakeBrokerError("Fake broker prices must be exact decimal values")
        try:
            parsed = Decimal(str(value))
            result = decimal_text(parsed)
            decimal_value(result, positive=True)
        except (InvalidOperation, TraderError) as error:
            raise FakeBrokerError("Fake broker price is not a positive canonical decimal") from error
        return result

    @staticmethod
    def _utc(value: str) -> str:
        if not isinstance(value, str):
            raise FakeBrokerError("Scenario wall time must be explicit ISO UTC text")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise FakeBrokerError("Scenario wall time must be valid ISO UTC text") from error
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise FakeBrokerError("Scenario wall time must carry a UTC offset")
        return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _record(self, event_type: str, **fields) -> None:
        self._trace_seq += 1
        event = {"run_id": self.run_id, "source": "QA_FAKE_BROKER",
                 "broker_event_seq": self._trace_seq, "event_type": event_type, **fields}
        self._events.append(deepcopy(event))
        if self._trace_path is not None:
            self._trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self._trace_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"),
                                        default=str, allow_nan=False) + "\n")
