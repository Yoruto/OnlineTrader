"""Exact, bounded domain values; no float or implicit zero/default conversions."""

from decimal import Decimal, localcontext
import re

from online_trader.errors import TraderError

MAX_QUANTITY = 2_147_483_647
MAX_SEQUENCE = 9_223_372_036_854_775_807
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{0,11}[1-9])?\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def decimal_value(value: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value) or value == "-0":
        raise TraderError("E_DECIMAL", "Expected a canonical decimal string")
    parsed = Decimal(value)
    if positive and parsed <= 0:
        raise TraderError("E_DECIMAL", "Value must be positive")
    return parsed


def decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TraderError("E_DECIMAL", "Expected a finite Decimal")
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if Decimal(result) == 0 else result


def quantity(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or not (int(positive) <= value <= MAX_QUANTITY):
        raise TraderError("E_QUANTITY", "Quantity is outside the supported integer range")
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TraderError("E_ID", "Invalid opaque identifier")
    return value


def sequence(value: str, *, positive: bool = False) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise TraderError("E_SEQUENCE", "Expected an integer string")
    parsed = int(value)
    if not int(positive) <= parsed <= MAX_SEQUENCE:
        raise TraderError("E_SEQUENCE", "Sequence is outside the supported range")
    return parsed


def order_price(value: str, tick_size: str) -> Decimal:
    price, tick = decimal_value(value), decimal_value(tick_size, positive=True)
    with localcontext() as context:
        context.prec = 80
        if price % tick:
            raise TraderError("E_PRICE_ALIGNMENT", "Order price must align exactly with tick size")
    return price
