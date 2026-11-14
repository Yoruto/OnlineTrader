"""Pure entry sizing and admission checks for the captured DSV reference.

This module never opens an account or sends an order.  ``margin_per_lot`` is the
captured quote margin amount; DSV falls back to last price * multiplier * 0.15 when
that amount is absent.  Independent owner/ledger guards still run after this
calculation and immediately before a development request is dispatched.
"""

from datetime import date
from decimal import Decimal, ROUND_FLOOR, localcontext

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.domain.values import MAX_QUANTITY, MAX_SEQUENCE, decimal_text, decimal_value, identifier, order_price
from online_trader.errors import TraderError


_ACCOUNT_REQUIRED = frozenset({"account_key", "trading_day", "mode", "revision", "complete", "equity", "available",
    "margin", "frozen_margin", "pending_margin", "symbol_margin", "risk_ratio", "daily_loss", "orders_today",
    "active_symbols", "stopped_symbols", "unknown_orders"})
_INSTRUMENT_REQUIRED = frozenset({"contract_id", "trading_day", "complete", "tick_size", "multiplier", "margin_rate",
    "lower_limit", "upper_limit", "min_order_qty", "max_order_qty", "days_to_expiry"})


def _invalid(message):
    raise TraderError("E_RISK_INPUT", message)


def _day(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            _invalid("Trading day must be YYYY-MM-DD")
    except ValueError:
        _invalid("Trading day must be YYYY-MM-DD")
    return value


def _decimal(value, *, nullable=True):
    if value is None and nullable:return None
    try:return decimal_value(value)
    except TraderError:_invalid("Risk facts require canonical Decimal strings")


def _integer(value, *, minimum=0, maximum=MAX_QUANTITY):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid("Risk quantity or revision is outside the supported range")
    return value


def _boolean(value):
    if type(value) is not bool:_invalid("Risk quality and switches require booleans")
    return value


def _identities(value):
    if type(value) is not list or len(value) > 256 or any(type(item) is not str for item in value):
        _invalid("Risk symbol identities must be a bounded unique list")
    if len(value) != len(set(value)):
        _invalid("Risk symbol identities must be a bounded unique list")
    try:
        return [identifier(item) for item in value]
    except TraderError:_invalid("Invalid risk symbol identity")


def _floor(value):
    return min(MAX_QUANTITY, max(0, int(value.to_integral_value(rounding=ROUND_FLOOR))))


def _dsv_lots(budget, per_lot):
    return min(MAX_QUANTITY, max(0, int(budget / per_lot)))


def calculate_daily_loss(*, balance, pre_balance, close_profit, float_profit, commission):
    """Return DSV's nonnegative daily loss amount, or None for missing source facts.

    A positive pre_balance selects balance-pre_balance and does not deduct fees
    again.  Otherwise the source uses close_profit+float_profit-commission.
    """
    values = {key: _decimal(value) for key, value in {"balance": balance, "pre_balance": pre_balance,
        "close_profit": close_profit, "float_profit": float_profit, "commission": commission}.items()}
    if values["pre_balance"] is None:return None
    with localcontext() as context:
        context.prec = 80
        if values["pre_balance"] > 0:
            if values["balance"] is None:return None
            today = values["balance"] - values["pre_balance"]
        else:
            if any(values[key] is None for key in ("close_profit", "float_profit", "commission")):return None
            today = values["close_profit"] + values["float_profit"] - values["commission"]
        return decimal_text(max(Decimal(0), -today))


def evaluate_entry(profile, *, direction, contract_id, stop_distance, limit_price,
                   requested_qty, account, instrument, market_price=None):
    """Return a bounded canonical result for one configured strategy row.

    Account ``pending_margin`` excludes amounts already in margin/frozen_margin;
    ``symbol_margin`` is this configured contract's observed position margin.
    ``stopped_symbols`` is an explicit, complete fact set supplied by the owner,
    not a value inferred from the number of currently active positions.
    ``mode`` is explicitly ``live`` or ``simulation``; ``days_to_expiry`` is a
    natural-day difference, matching the captured DSV expiry comparison.
    """
    if not isinstance(profile, DsvProfile):_invalid("Expected a validated DSV profile")
    if direction not in {"LONG", "SHORT"}:_invalid("Invalid entry direction")
    try:contract_id = identifier(contract_id)
    except TraderError:_invalid("Invalid entry contract")
    requested_qty = _integer(requested_qty)
    stop = _decimal(stop_distance, nullable=False); price = _decimal(limit_price, nullable=False)
    last = price if market_price is None else _decimal(market_price, nullable=False)
    if type(account) is not dict or not _ACCOUNT_REQUIRED <= account.keys():
        _invalid("Incomplete account fact structure")
    if type(instrument) is not dict or not _INSTRUMENT_REQUIRED <= instrument.keys():
        _invalid("Incomplete instrument fact structure")
    try:
        account_key = identifier(account["account_key"])
        instrument_code = identifier(instrument["contract_id"])
    except TraderError:_invalid("Invalid risk fact scope")
    day = _day(account["trading_day"]); instrument_day = _day(instrument["trading_day"])
    if account["mode"] not in {"live", "simulation"}:_invalid("Risk facts require an explicit account mode")
    revision = _integer(account["revision"], minimum=1, maximum=MAX_SEQUENCE)
    account_complete = _boolean(account["complete"]); instrument_complete = _boolean(instrument["complete"])
    unknown = _boolean(account["unknown_orders"])
    orders = _integer(account["orders_today"])
    active = _identities(account["active_symbols"]); stopped = _identities(account["stopped_symbols"])
    values = {key: _decimal(account[key]) for key in ("equity", "available", "margin", "frozen_margin",
        "pending_margin", "symbol_margin", "risk_ratio", "daily_loss")}
    metadata = {key: _decimal(instrument[key]) for key in ("tick_size", "multiplier", "margin_rate", "lower_limit", "upper_limit")}
    margin_amount = _decimal(instrument.get("margin_per_lot"))
    minimum = _integer(instrument["min_order_qty"], minimum=1)
    maximum = _integer(instrument["max_order_qty"], minimum=1)
    expiry = None if instrument["days_to_expiry"] is None else _integer(instrument["days_to_expiry"],minimum=-MAX_QUANTITY)
    if minimum > maximum:_invalid("Instrument quantity bounds are inverted")
    settings = profile.to_record()["settings"]; trade = settings["trade"]
    rows = {row["code"]: row for row in settings["symbols"]}
    row = rows.get(contract_id)
    metrics = {"profile_hash": profile.content_hash, "account_key": account_key, "trading_day": day,
        "fact_revision": str(revision), "contract_id": contract_id, "direction": direction,
        "requested_quantity": requested_qty, "execution_authority": False,
        "live_protection_effective": trade["live_protect"] and account["mode"] == "live"}
    reasons = []
    def reject(reason):
        if reason not in reasons:reasons.append(reason)
    if contract_id != instrument_code or day != instrument_day:
        reject("FACT_SCOPE_MISMATCH")
    if "contract_id" in account and account["contract_id"] != contract_id:
        reject("FACT_SCOPE_MISMATCH")
    if "account_key" in instrument and instrument["account_key"] != account_key:
        reject("FACT_SCOPE_MISMATCH")
    if not account_complete:reject("ACCOUNT_FACTS_INCOMPLETE")
    if not instrument_complete:reject("INSTRUMENT_FACTS_INCOMPLETE")
    if unknown:reject("UNKNOWN_ORDERS")
    if row is None:reject("CONTRACT_NOT_CONFIGURED")
    elif (stop != Decimal(row["stop_offset"]) or requested_qty != row["order_volume"]):
        reject("CONFIGURATION_MISMATCH")
    if row is not None and any(Decimal(row[key]) <= 0 for key in ("open_offset", "stop_offset", "profit_offset")):
        reject("SYMBOL_OFFSETS_INCOMPLETE")
    if stop <= 0:reject("STOP_DISTANCE_INVALID")
    if price <= 0:reject("PRICE_NOT_POSITIVE")
    if (margin_amount is None or margin_amount <= 0) and last <= 0:
        reject("MARGIN_REFERENCE_PRICE_NOT_POSITIVE")
    fixed = requested_qty > 0
    required_values = ("available",) if fixed else (
        "equity", "available", "margin", "frozen_margin", "pending_margin", "symbol_margin", "risk_ratio")
    missing = [key for key in required_values if values[key] is None]
    if missing:
        metrics["missing_fields"] = missing
        reject("ACCOUNT_VALUES_UNKNOWN")
    if not fixed:
        if any(values[key] is not None and values[key] < 0
               for key in ("margin", "frozen_margin", "pending_margin", "symbol_margin", "risk_ratio")):
            reject("ACCOUNT_VALUES_INVALID")
        if values["equity"] is not None and values["equity"] <= 0:
            reject("EQUITY_NOT_POSITIVE")
    if values["available"] is not None and values["available"] < 0:
        reject("AVAILABLE_NOT_POSITIVE")
    if metadata["tick_size"] is None or metadata["tick_size"] <= 0 or metadata["multiplier"] is None or metadata["multiplier"] <= 0:
        reject("INSTRUMENT_VALUES_UNKNOWN")
    if metadata["lower_limit"] is None or metadata["upper_limit"] is None:
        reject("PRICE_LIMITS_UNKNOWN")
    elif metadata["lower_limit"] > metadata["upper_limit"]:
        reject("PRICE_LIMITS_INVALID")
    elif not metadata["lower_limit"] <= price <= metadata["upper_limit"]:
        reject("PRICE_OUTSIDE_LIMITS")
    if metadata["tick_size"] is not None and metadata["tick_size"] > 0:
        try:order_price(limit_price, decimal_text(metadata["tick_size"]))
        except TraderError:reject("PRICE_NOT_ALIGNED")
    if contract_id in stopped:reject("SYMBOL_DAY_STOPPED")
    if orders >= trade["max_daily_orders"]:reject("DAILY_ORDER_LIMIT")
    if trade["max_symbols_enabled"] and contract_id not in active and len(active) >= trade["max_symbols_on"]:
        reject("ACTIVE_SYMBOL_LIMIT")
    if trade["rollover_stop_enabled"]:
        if expiry is None:reject("EXPIRY_UNKNOWN")
        elif expiry <= trade["min_days_to_expiry"]:reject("EXPIRY_ENTRY_BLOCK")
    live_protection = trade["live_protect"] and account["mode"] == "live"
    if live_protection and Decimal(trade["live_daily_loss_limit"]) > 0:
        if values["daily_loss"] is None:reject("DAILY_LOSS_UNKNOWN")
        elif values["daily_loss"] < 0:reject("DAILY_LOSS_INVALID")
        elif values["daily_loss"] >= Decimal(trade["live_daily_loss_limit"]):reject("LIVE_DAILY_LOSS_LIMIT")
    if reasons:return {"allowed": False, "quantity": 0, "reasons": reasons, "metrics": metrics}

    with localcontext() as context:
        context.prec = 80
        available = values["available"]
        per_lot = margin_amount if margin_amount is not None and margin_amount > 0 else last * metadata["multiplier"] * Decimal("0.15")
        multiplier = float(metadata["multiplier"])
        dsv_available = float(available)
        dsv_per_lot = (float(margin_amount) if margin_amount is not None and margin_amount > 0
                       else float(last) * multiplier * 0.15)
        metrics.update({
            "margin_per_lot": decimal_text(per_lot),
            "margin_source": "QUOTE_MARGIN" if margin_amount is not None and margin_amount > 0 else "PRICE_MULTIPLIER_15_PERCENT",
            "min_one_lot_used": False,
        })
        if fixed:
            quantity = min(requested_qty, trade["max_volume"], _floor(available / per_lot),
                           _dsv_lots(dsv_available, dsv_per_lot))
            metrics["sizing_branch"] = "FIXED"
        else:
            equity = values["equity"]
            cap = min(Decimal(trade["max_risk_ratio"]), Decimal(trade["total_margin_cap"]), Decimal(1))
            used = values["margin"] + values["frozen_margin"] + values["pending_margin"]
            configured_stops = {symbol for symbol in stopped if symbol in rows}
            participating = max(1, len(rows) - len(configured_stops))
            symbol_budget = equity * (cap / participating) - values["symbol_margin"]
            total_budget = equity * cap - used
            risk_per_lot = stop * metadata["multiplier"]
            risk_budget = equity * Decimal(trade["risk_per_trade_ratio"])
            dsv_equity, dsv_cap = float(equity), float(cap)
            risk_volume = _dsv_lots(dsv_equity * float(trade["risk_per_trade_ratio"]), float(stop) * multiplier)
            metrics.update({"sizing_branch": "AUTO", "effective_risk_cap": decimal_text(cap),
                "used_margin": decimal_text(used), "participating_configured_symbols": participating,
                "symbol_margin_budget": decimal_text(symbol_budget), "account_margin_budget": decimal_text(total_budget),
                "risk_per_lot": decimal_text(risk_per_lot), "single_trade_risk_budget": decimal_text(risk_budget),
                "risk_based_quantity": risk_volume})
            if values["risk_ratio"] >= cap:
                reject("EFFECTIVE_RISK_CAP")
                quantity = 0
            else:
                budget = symbol_budget if trade["aggressive_mode"] else min(symbol_budget, total_budget)
                dsv_used = float(values["margin"]) + float(values["frozen_margin"]) + float(values["pending_margin"])
                dsv_total_budget = dsv_equity * dsv_cap - dsv_used
                dsv_symbol_budget = dsv_equity * (dsv_cap / participating) - float(values["symbol_margin"])
                dsv_budget = dsv_symbol_budget if trade["aggressive_mode"] else min(dsv_symbol_budget, dsv_total_budget)
                # Exact monetary bounds still cap the compatible proposal and back its reservation.
                margin_quantity = min(_floor(min(available, budget) / per_lot),
                                      _dsv_lots(min(dsv_available, dsv_budget), dsv_per_lot))
                metrics["margin_based_quantity"] = margin_quantity
                quantity = min(margin_quantity, trade["max_volume"])
                if not trade["aggressive_mode"] and Decimal(trade["risk_per_trade_ratio"]) > 0 and 0 < risk_volume < quantity:
                    quantity = risk_volume
                # A zero risk_volume does not shrink a positive margin quantity in
                # the DSV implementation.  The fallback is a separate margin path.
                if (quantity <= 0 and trade["min_one_lot_fallback"] and min(available, total_budget) >= per_lot
                        and min(dsv_available, dsv_total_budget) >= dsv_per_lot):
                    quantity = 1; metrics["min_one_lot_used"] = True
                if live_protection:
                    live_cap = 1 if trade["live_first_one_lot"] else max(trade["live_max_lots"], 1)
                    quantity = min(quantity, live_cap)
                    metrics["live_auto_quantity_cap"] = live_cap
        if quantity > maximum:
            quantity = maximum
            metrics["instrument_quantity_capped"] = True
        if quantity < minimum:
            reject("INSUFFICIENT_QUANTITY")
            quantity = 0
        if quantity <= 0 and not reasons:reject("INSUFFICIENT_MARGIN")
    result = {"allowed": not reasons and quantity > 0, "quantity": quantity if not reasons else 0,
        "reasons": reasons, "metrics": metrics}
    canonical_bytes(result)
    return result
