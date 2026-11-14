"""Pure DSV nominal-loss, continuation and monthly entry-stop state reducer.

The owner supplies already reconciled nominal points and a complete explicit
trading-day calendar.  This module does not infer nominal P&L from currency P&L
or synthesize missing sessions as weekdays.
"""

from datetime import date
import re

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.domain.values import MAX_QUANTITY, decimal_value, identifier
from online_trader.dsv_core.business_statistics import (
    reduce_stops, MONTH_STOP_REASON as MONTH_STOP_REASON, NET_LOSS_REASON as NET_LOSS_REASON,
)
from online_trader.errors import TraderError


MAX_HISTORY_DATES = 512


def _invalid(message):
    raise TraderError("E_STRATEGY_STOP_INPUT", message)


def _day(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            _invalid("Stop facts require YYYY-MM-DD dates")
    except ValueError:
        _invalid("Stop facts require YYYY-MM-DD dates")
    return value


def _dates(value):
    if type(value) is not list or len(value) > MAX_HISTORY_DATES:
        _invalid("Stop history and calendars must be bounded explicit date lists")
    result = [_day(item) for item in value]
    if len(result) != len(set(result)):
        _invalid("Repeated stop or calendar date")
    return result


def evaluate_stops(profile, contract_id, trading_day, *, net_loss_points, stop_streak,
                   continuation_stopped, day_stopped, day_stopped_reason, stop_days,
                   month_stop, trading_days):
    """Return next stop state and separate ordinary/continuation entry gates.

    ``trading_days`` is a caller-verified complete calendar interval, not an
    arbitrary sample of market dates.  Monthly evaluation needs its current date
    and the preceding N-1 trading dates explicitly present.  Loss history retains
    its source append order; the active monthly tracking block trims each append
    to the latest 12 entries. A zero month_stop_days skips that entire block.
    """
    if not isinstance(profile, DsvProfile):_invalid("Expected a validated DSV profile")
    try:contract_id = identifier(contract_id)
    except TraderError:_invalid("Invalid stop contract")
    today = _day(trading_day)
    try:loss = decimal_value(net_loss_points)
    except TraderError:_invalid("Nominal loss points must be a canonical Decimal string")
    if type(stop_streak) is not int or not 0 <= stop_streak <= MAX_QUANTITY:
        _invalid("Stop streak must be a bounded nonnegative integer")
    if type(continuation_stopped) is not bool or type(day_stopped) is not bool:
        _invalid("Stop flags require explicit booleans")
    if day_stopped_reason is not None and (type(day_stopped_reason) is not str
            or len(day_stopped_reason) > 256 or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in day_stopped_reason)):
        _invalid("Invalid saved day-stop reason")
    if month_stop is not None and (type(month_stop) is not str or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", month_stop)):
        _invalid("Month stop must be YYYY-MM or null")
    if month_stop is not None:
        _day(month_stop + "-01")
    history = _dates(stop_days); calendar = _dates(trading_days)
    if any(day > today for day in history):_invalid("Stop history cannot contain a future trading day")
    settings = profile.to_record()["settings"]
    row = next((row for row in settings["symbols"] if row["code"] == contract_id), None)
    if row is None:_invalid("Stop contract is not configured")

    result = reduce_stops(contract_id, today, loss=loss, stop_streak=stop_streak,
                          continuation_stopped=continuation_stopped, day_stopped=day_stopped,
                          day_stopped_reason=day_stopped_reason, history=history, month_stop=month_stop,
                          calendar=calendar, stop_offset=row["stop_offset"], trade=settings["trade"])
    canonical_bytes(result)
    return result
