"""Pure pricing calculations for DSV D11 and the scheduled close window."""

from decimal import Decimal, localcontext


def strategy_price(side: str, mode: str, *, opponent: Decimal | None,
                   tick: Decimal | None, limit: Decimal | None,
                   reference: Decimal | None) -> Decimal | None:
    """Calculate the price before the execution adapter checks market facts."""
    sign = Decimal(1) if side == "BUY" else Decimal(-1)
    with localcontext() as context:
        context.prec = 80
        if mode == "PASSIVE":
            return reference
        if mode == "MARKET" and limit is not None:
            return limit
        if opponent is None:
            return None
        if mode == "MARKET":
            return opponent + sign * 10 * (tick or Decimal(1))
        if mode == "AGGR1":
            return opponent + sign * (tick or Decimal(1))
        if mode == "CAP":
            return min(opponent, reference) if side == "BUY" else max(opponent, reference)
        return opponent


def scheduled_close_mode(remaining_ms: int) -> str:
    if remaining_ms <= 5_000:
        return "MARKET"
    if remaining_ms <= 10_000:
        return "AGGR1"
    return "ACTIVE"
