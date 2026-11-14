"""DSV D08–D09 calculations for a protection stage."""

from decimal import Decimal, ROUND_HALF_EVEN


def half_close_target(quantity: int) -> int:
    """Return remaining lots after the caller has established at least two lots."""
    close = int((Decimal(quantity) / 2).to_integral_value(rounding=ROUND_HALF_EVEN))
    return quantity - min(max(close, 1), quantity - 1)


def trailing_floor(peak: Decimal, take_profit: Decimal, ratio: Decimal) -> Decimal:
    return peak - max(take_profit * ratio, peak * ratio)
