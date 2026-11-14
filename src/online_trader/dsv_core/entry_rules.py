"""Entry calculations used by the current strategy adapter.

Source locations for D01, D03 and D07 are recorded in strategy-dsv-evidence.md.
The adapter owns market readiness, opportunity consumption and order intents.
"""

from decimal import Decimal


def trigger_prices(day_high: Decimal, day_low: Decimal, offset: Decimal) -> dict[str, Decimal]:
    return {"LONG": day_low + offset, "SHORT": day_high - offset}


def geometry_limit(open_offset: Decimal, stop_offset: Decimal) -> Decimal:
    return 2 * open_offset - stop_offset


def continuation_direction(price, night_last, night_high, night_low, offset, fallback=None):
    """Choose the direction allowed by the overnight range and opening gap."""
    if price >= night_low + offset:
        direction = "LONG"
    elif price <= night_high - offset:
        direction = "SHORT"
    else:
        direction = None
    if price == night_last:
        return direction or fallback
    if (price > night_last and direction == "SHORT") or (price < night_last and direction == "LONG"):
        return direction
    return None


def opening_jump_block(last_price, trigger, profit_distance, ratio):
    """DSV gui.opening_jump_block uses an inclusive absolute distance."""
    if all(type(value) is float for value in (last_price, trigger, profit_distance, ratio)):
        return last_price > 0 and trigger > 0 and profit_distance > 0 and abs(last_price - trigger) >= profit_distance * ratio
    price, line, take, factor = map(Decimal, (last_price, trigger, profit_distance, ratio))
    return price > 0 and line > 0 and take > 0 and abs(price - line) >= take * factor


def hysteresis_ready(direction, last_price, trigger, distance, armed):
    """A reverse excursion arms hysteresis before the later trigger check."""
    values = last_price, trigger, distance
    price, line, hysteresis = values if all(type(value) is float for value in values) else tuple(map(Decimal, values))
    if hysteresis <= 0 or armed:
        return True, bool(armed)
    reached = price <= line - hysteresis if direction == "LONG" else price >= line + hysteresis
    return bool(reached), bool(reached)


def chase_within_cap(direction, last_price, opponent_price, trigger, cap):
    """Either favorable quote can override the adverse quote of the pair."""
    last, opponent, line, distance = map(Decimal, (last_price, opponent_price, trigger, cap))
    values = (last, opponent)
    if direction == "LONG":
        adverse = any(price > line + distance for price in values)
        favourable = any(0 < price <= line for price in values)
    else:
        adverse = any(0 < price < line - distance for price in values)
        favourable = any(price >= line for price in values)
    return not adverse or favourable
