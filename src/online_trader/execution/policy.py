"""Static DSV pricing and SDK repricing rules for explicit offline facts.

No clock, transport, SDK or matching engine is used. A returned action is a
proposal: the owner still commits its intent, checks current facts and sends it.
"""

from decimal import Decimal, localcontext

from online_trader.contracts.codec import canonical_bytes
from online_trader.domain.values import decimal_text, decimal_value, quantity
from online_trader.dsv_core.price_rules import scheduled_close_mode, strategy_price
from online_trader.errors import TraderError


_SIDES = {"BUY", "SELL"}
_TTL_MS = {"PASSIVE": 90_000, "AGGR1": 60_000, "MARKET": 120_000, "CAP": 90_000}


def _side(value):
    if value not in _SIDES:
        raise TraderError("E_EXECUTION_INPUT", "Expected an explicit BUY or SELL side")
    return value


def _milliseconds(value):
    return quantity(value)


def _price(value):
    """Zero/None represent an absent SDK quote, never an executable price."""
    if value is None:
        return None
    number = decimal_value(value)
    if number < 0:
        raise TraderError("E_EXECUTION_INPUT", "Negative market prices are invalid")
    return number if number else None


def select_price(side, market, *, contexts=None):
    """Return the source callback price and whether it is proven executable.

    ``market`` supplies canonical price strings (or None), ``tick_size``, both
    exchange limits and explicit complete/fresh/is_trading/quality facts.
    ``contexts`` is an optional map of PASSIVE/AGGR1/MARKET/CAP rows, each with
    side and nonnegative age_ms; PASSIVE adds level, CAP adds cap. The original
    callback's priority and inclusive TTL are preserved. It did not round or
    clamp every price, so invalid results are refused, not silently repaired.
    """
    _side(side)
    if type(market) is not dict or (contexts is not None and type(contexts) is not dict):
        raise TraderError("E_EXECUTION_INPUT", "Expected explicit market and price contexts")
    contexts = {} if contexts is None else contexts
    canonical_bytes(market); canonical_bytes(contexts)
    if set(contexts) - _TTL_MS.keys():
        raise TraderError("E_EXECUTION_INPUT", "Unsupported DSV price context")
    numbers = {key: _price(market.get(key)) for key in
               ("last", "bid1", "ask1", "tick_size", "lower_limit", "upper_limit")}
    selected = None
    for mode, ttl in _TTL_MS.items():
        row = contexts.get(mode)
        if row is None:
            continue
        if type(row) is not dict:
            raise TraderError("E_EXECUTION_INPUT", "Invalid DSV price context row")
        _side(row.get("side")); age = _milliseconds(row.get("age_ms"))
        value = _price(row.get("level" if mode == "PASSIVE" else "cap")) if mode in {"PASSIVE", "CAP"} else None
        if row["side"] == side and age <= ttl and (mode not in {"PASSIVE", "CAP"} or value is not None):
            selected = (mode, value)
            break
    mode, reference = selected or ("ACTIVE", None)
    opponent = numbers["ask1" if side == "BUY" else "bid1"] or numbers["last"]
    tick = numbers["tick_size"]
    low, high = numbers["lower_limit"], numbers["upper_limit"]
    raw = strategy_price(side, mode, opponent=opponent, tick=tick,
                         limit=high if side == "BUY" else low, reference=reference)
    with localcontext() as context:
        context.prec = 80
        reasons = []
        if (market.get("complete") is not True or market.get("fresh") is not True
                or market.get("is_trading") is not True or market.get("quality") != "CONTIGUOUS"):
            reasons.append("MARKET_FACTS_NOT_EXECUTABLE")
        if raw is None or raw <= 0:
            reasons.append("PRICE_UNAVAILABLE")
        if tick is None:
            reasons.append("TICK_SIZE_UNKNOWN")
        if low is None or high is None:
            reasons.append("PRICE_LIMITS_UNKNOWN")
        elif low > high:
            reasons.append("PRICE_LIMITS_INVALID")
        elif raw is not None and not low <= raw <= high:
            reasons.append("PRICE_OUTSIDE_EXCHANGE_LIMITS")
        if raw is not None and tick is not None and raw % tick:
            reasons.append("PRICE_NOT_TICK_ALIGNED")
    return {"allowed": not reasons, "price": decimal_text(raw) if raw is not None and not reasons else None,
            "raw_price": decimal_text(raw) if raw is not None else None,
            "mode": mode, "reasons": reasons}


def close_price_context(side, *, elapsed_close_ms, advance_seconds, last_context_age_ms=None):
    """Choose the close-window step; the GUI refresh gate is inclusive 3s.

    The source's constants 10 and 5 are seconds remaining in the close window,
    not attempt counts. No automatic timer is created by this function.
    """
    _side(side); _milliseconds(elapsed_close_ms); quantity(advance_seconds)
    if last_context_age_ms is not None:
        _milliseconds(last_context_age_ms)
    remaining_ms = advance_seconds * 1000 - elapsed_close_ms
    mode = scheduled_close_mode(remaining_ms)
    return {"refresh": last_context_age_ms is None or last_context_age_ms >= 3000,
            "mode": mode, "contexts": {} if mode == "ACTIVE" else {mode: {"side": side, "age_ms": 0}}}


def select_manual_price(mode, side, market, *, elapsed_ms=0, limit_fallback_ratio="0.01"):
    """Price a direct manual request; this does not opt it into SDK chasing."""
    _side(side)
    if mode not in {"对手价", "排队价", "最新价"}:
        raise TraderError("E_EXECUTION_INPUT", "Unsupported manual price mode")
    if type(market) is not dict:
        raise TraderError("E_EXECUTION_INPUT", "Expected explicit manual market facts")
    _milliseconds(elapsed_ms)
    ratio=decimal_value(limit_fallback_ratio)
    if not 0 <= ratio <= 1:
        raise TraderError("E_EXECUTION_INPUT", "Invalid manual limit fallback ratio")
    field = ("last" if mode == "最新价" else "bid1" if (mode == "排队价") == (side == "BUY") else "ask1")
    value = _price(market.get(field))
    reason = "MANUAL_PRICE_UNAVAILABLE"
    if value is None and elapsed_ms >= 4000:
        limit = _price(market.get("upper_limit" if side == "BUY" else "lower_limit"))
        last = _price(market.get("last"))
        if limit is None:
            value = last
        elif last is not None:
            with localcontext() as context:
                context.prec=80
                if abs(limit-last)/last <= ratio:
                    value=limit
                else:
                    reason="MANUAL_LIMIT_FALLBACK_TOO_FAR"
    if value is None:
        # Missing direct mode price cannot use the SDK callback's earlier
        # opponent/last fallback; the source manual resolver waits four seconds.
        selected = select_price(side, market)
        selected.update(allowed=False, price=None, raw_price=None,reasons=[reason])
    else:
        selected = select_price(side, market, contexts={"PASSIVE":{
            "side":side,"age_ms":0,"level":decimal_text(value)}})
    return selected | {"manual_mode": mode, "automatic_reprice": False}


def decide_order_action(order, desired_price, *, can_increase, remaining_quantity,
                        other_pending_quantity, timeout_due=False, reprice_cancelled=False):
    """Propose WAIT/CANCEL/REPLACE/DONE/STOP without changing an order.

    The owner passes an OrderState record plus ``side``. ``reprice_cancelled``
    proves a durable source-price cancellation reason; CANCELLED alone does not
    establish it. A replacement still needs a new owner intent and final gate.
    ``remaining_quantity`` is the whole intent's remaining target, before the
    disjoint other pending quantity. It never comes from a send return.
    """
    if type(order) is not dict:
        raise TraderError("E_EXECUTION_INPUT", "Expected an explicit order record")
    canonical_bytes(order)
    side = _side(order.get("side"))
    if order.get("effect") not in {"OPEN", "REDUCE"}:
        raise TraderError("E_EXECUTION_INPUT", "Expected an OPEN or REDUCE order")
    for value in (can_increase, timeout_due, reprice_cancelled):
        if type(value) is not bool:
            raise TraderError("E_EXECUTION_INPUT", "Expected explicit execution switches")
    total = quantity(order.get("original_qty"), positive=True)
    filled = quantity(order.get("filled_qty"))
    if filled > total:
        raise TraderError("E_EXECUTION_INPUT", "Confirmed fill exceeds the original order")
    remaining = quantity(remaining_quantity); pending = quantity(other_pending_quantity)
    needed = max(0, remaining - pending)
    old = decimal_value(order.get("limit_price"), positive=True)
    desired = None if desired_price is None else decimal_value(desired_price, positive=True)
    result = {"action": "WAIT", "quantity": 0, "price": None, "reasons": []}

    def outcome(action, reason, *, amount=0):
        return result | {"action": action, "quantity": amount,
                         "price": desired_price if action == "REPLACE" else None, "reasons": [reason]}

    terminal = order.get("terminal_confirmed")
    if type(terminal) is not bool:
        raise TraderError("E_EXECUTION_INPUT", "Missing terminal fact")
    if terminal:
        reported = order.get("reported_fill_qty")
        if reported is not None:
            quantity(reported)
        if reported != filled:
            return outcome("WAIT", "TERMINAL_FILL_DETAILS_INCOMPLETE")
        if order.get("send_state") == "UNKNOWN":
            return outcome("WAIT", "SEND_RESULT_UNKNOWN")
        if filled == total:
            return outcome("DONE", "ORDER_FULLY_FILLED")
        if order["effect"] == "OPEN" and (timeout_due or not can_increase):
            return outcome("STOP", "ENTRY_TIMEOUT" if timeout_due else "ENTRY_AUTHORITY_REVOKED")
        if not needed:
            return outcome("DONE", "TARGET_ALREADY_COVERED")
        if order.get("broker_state") == "REJECTED":
            return outcome("STOP", "UNEXPECTED_ORDER_TERMINAL")
        reusable = order.get("send_state") == "DEFINITELY_NOT_SENT" or (
            order.get("broker_state") == "CANCELLED" and reprice_cancelled)
        if not reusable:
            return outcome("STOP", "UNEXPECTED_ORDER_TERMINAL")
        if desired is None:
            return outcome("WAIT", "LEGAL_REPLACEMENT_PRICE_UNAVAILABLE")
        return outcome("REPLACE", "REPRICE_TERMINAL_RECONCILED", amount=needed)
    if order.get("send_state") in {"UNKNOWN", "SEND_POSSIBLE"}:
        return outcome("WAIT", "SEND_RESULT_UNKNOWN")
    if order.get("cancel_status"):
        return outcome("WAIT", "CANCELLATION_PENDING")
    if filled == total:
        return outcome("WAIT", "ORDER_TERMINAL_NOT_PROVEN")
    if order["effect"] == "OPEN" and (timeout_due or not can_increase):
        return outcome("CANCEL", "ENTRY_TIMEOUT" if timeout_due else "ENTRY_AUTHORITY_REVOKED")
    if needed < total - filled:
        return outcome("CANCEL", "TARGET_QUANTITY_REDUCED")
    if desired is None:
        return outcome("WAIT", "LEGAL_REPLACEMENT_PRICE_UNAVAILABLE")
    if (side == "BUY" and desired > old) or (side == "SELL" and desired < old):
        return outcome("CANCEL", "NEW_QUOTE_PRICE_WORSENED")
    return outcome("WAIT", "PRICE_EQUAL_OR_IMPROVED")
