"""D05 lot calculations for normalized DSV business inputs."""

from online_trader.domain.values import MAX_QUANTITY


def margin_lots(budget: float, per_lot: float) -> int:
    return min(MAX_QUANTITY, max(0, int(budget / per_lot)))


def auto_lots(margin_quantity: int, maximum: int, *, aggressive: bool,
              risk_enabled: bool, risk_quantity: int) -> int:
    quantity = min(margin_quantity, maximum)
    if not aggressive and risk_enabled and 0 < risk_quantity < quantity:
        return risk_quantity
    return quantity
