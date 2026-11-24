"""Independent FakeMarket simulator separated from FakeBroker.

Generates explicit contract quote rows, session windows and minute-bar IDs
without embedding order acceptance or fill logic.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from online_trader.domain.values import decimal_text, decimal_value


class FakeMarketError(ValueError):
    """Raised when market tick or contract state is invalid."""


def _canon_price(value: str | int | Decimal) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise FakeMarketError(f"Market price must be canonical decimal: {value!r}")
    try:
        text = decimal_text(Decimal(str(value)))
        decimal_value(text, positive=True)
    except (InvalidOperation, Exception) as error:
        raise FakeMarketError(f"Invalid positive market price: {value!r}") from error
    return text


@dataclass
class ContractMarketState:
    contract_id: str
    trading_day: str
    session_open: str
    session_close: str
    opening_session: bool = True
    market_session_active: bool = True
    tick_size: str = "1"
    lower_limit: str = "900"
    upper_limit: str = "1200"
    day_low: str = "1020"
    day_high: str = "1088"
    last: str | None = None
    bid1: str | None = None
    ask1: str | None = None
    candle_id: str | None = "qa-bar-001"
    native_time: str | None = None
    available: str = "100000"
    margin_per_lot: str = "1000"
    multiplier: str = "10"
    drop_tick: bool = False
    external_orders: bool = False


class FakeMarket:
    """Simulated market data source independent from FakeBroker."""

    def __init__(
        self,
        *,
        contract_id: str,
        trading_day: str,
        session_open: str,
        session_close: str,
        day_low: str | int | Decimal = "1020",
        day_high: str | int | Decimal = "1088",
        lower_limit: str | int | Decimal = "900",
        upper_limit: str | int | Decimal = "1200",
        tick_size: str | int | Decimal = "1",
        available: str | int | Decimal = "100000",
        margin_per_lot: str | int | Decimal = "1000",
        multiplier: str | int | Decimal = "10",
    ) -> None:
        self._contracts: dict[str, ContractMarketState] = {
            contract_id: ContractMarketState(
                contract_id=contract_id,
                trading_day=trading_day,
                session_open=session_open,
                session_close=session_close,
                day_low=_canon_price(day_low),
                day_high=_canon_price(day_high),
                lower_limit=_canon_price(lower_limit),
                upper_limit=_canon_price(upper_limit),
                tick_size=_canon_price(tick_size),
                available=_canon_price(available),
                margin_per_lot=_canon_price(margin_per_lot),
                multiplier=_canon_price(multiplier),
            )
        }
        self._default_contract = contract_id
        self._history: list[dict[str, object]] = []

    def apply_tick(
        self,
        *,
        wall_time: str,
        last: str | int | Decimal | None,
        contract_id: str | None = None,
        bid1: str | int | Decimal | None = None,
        ask1: str | int | Decimal | None = None,
        day_low: str | int | Decimal | None = None,
        day_high: str | int | Decimal | None = None,
        candle_id: str | None = None,
        opening_session: bool | None = None,
        market_session_active: bool | None = None,
        native_time: str | None = None,
        drop_tick: bool = False,
    ) -> None:
        code = contract_id or self._default_contract
        state = self._contracts.get(code)
        if state is None:
            raise FakeMarketError(f"Unknown market contract: {code}")
        state.drop_tick = bool(drop_tick)
        if opening_session is not None:
            state.opening_session = bool(opening_session)
        if market_session_active is not None:
            state.market_session_active = bool(market_session_active)
        if day_low is not None:
            state.day_low = _canon_price(day_low)
        if day_high is not None:
            state.day_high = _canon_price(day_high)
        if candle_id is not None:
            state.candle_id = candle_id
        if drop_tick or last is None:
            state.native_time = native_time or wall_time
            self._history.append({
                "contract_id": code,
                "wall_time": wall_time,
                "drop_tick": True,
            })
            return

        last_text = _canon_price(last)
        bid_text = last_text if bid1 is None else _canon_price(bid1)
        ask_text = last_text if ask1 is None else _canon_price(ask1)
        state.last = last_text
        state.bid1 = bid_text
        state.ask1 = ask_text
        state.native_time = native_time or wall_time
        self._history.append({
            "contract_id": code,
            "wall_time": wall_time,
            "native_time": state.native_time,
            "last": last_text,
            "bid1": bid_text,
            "ask1": ask_text,
            "day_low": state.day_low,
            "day_high": state.day_high,
            "candle_id": state.candle_id,
        })

    def snapshot(self) -> list[dict[str, object]]:
        """Return market rows ready to be merged with FakeBroker position buckets."""
        rows: list[dict[str, object]] = []
        for state in self._contracts.values():
            ticks: list[dict[str, object]] = []
            if not state.drop_tick and state.last is not None and state.native_time is not None:
                ticks.append({
                    "native_time": state.native_time,
                    "candle_id": state.candle_id,
                    "last": state.last,
                    "day_low": state.day_low,
                    "day_high": state.day_high,
                    "bid1": state.bid1 or state.last,
                    "ask1": state.ask1 or state.last,
                    "tick_size": state.tick_size,
                    "lower_limit": state.lower_limit,
                    "upper_limit": state.upper_limit,
                })
            rows.append({
                "contract_id": state.contract_id,
                "ticks": ticks,
                "opening_session": state.opening_session,
                "session_open": state.session_open,
                "session_close": state.session_close,
                "market_session_active": state.market_session_active,
                "quote_observation": {
                    "age_quality": "VALID",
                    "reason": None if ticks else "NO_TICK",
                },
                "available": state.available,
                "margin_per_lot": state.margin_per_lot,
                "multiplier": state.multiplier,
                "external_orders": state.external_orders,
            })
        return rows

    @property
    def history(self) -> tuple[dict[str, object], ...]:
        return tuple(deepcopy(self._history))

    def roll_trading_day(self, new_trading_day: str, *, session_open: str, session_close: str) -> None:
        for state in self._contracts.values():
            state.trading_day = new_trading_day
            state.session_open = session_open
            state.session_close = session_close
            state.drop_tick = False

    @staticmethod
    def load_jsonl(path: str | Path) -> list[dict[str, object]]:
        """Load ordered tick dicts from a JSONL fixture file."""
        rows: list[dict[str, object]] = []
        with Path(path).open("r", encoding="utf-8") as stream:
            for line_no, raw in enumerate(stream, 1):
                text = raw.strip()
                if not text or text.startswith("#"):
                    continue
                item = json.loads(text)
                if not isinstance(item, dict):
                    raise FakeMarketError(f"Line {line_no} in {path} is not a JSON object")
                rows.append(item)
        return rows
