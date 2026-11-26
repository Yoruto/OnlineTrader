"""Independent FakeMarket simulator separated from FakeBroker.

Generates explicit contract quote rows, session windows and minute-bar IDs
without embedding order acceptance or fill logic.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from online_trader.domain.values import decimal_text, decimal_value


class FakeMarketError(ValueError):
    """Raised when market tick or contract state is invalid."""


def _utc_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise FakeMarketError(f"Market timestamp must carry UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc)


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
    last_quote_wall_time: str | None = None
    available: str = "100000"
    margin_per_lot: str = "1000"
    multiplier: str = "10"
    drop_tick: bool = False
    external_orders: bool = False
    age_quality_override: str | None = None
    quote_reason_override: str | None = None
    fallback_book_to_last: bool = True
    pending_ticks: list[dict[str, object]] = field(default_factory=list)


class FakeMarket:
    """Simulated market data source independent from FakeBroker."""

    def __init__(
        self,
        *,
        contract_id: str,
        contracts: list[str] | tuple[str, ...] | None = None,
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
        fallback_book_to_last: bool = True,
    ) -> None:
        ordered = tuple(dict.fromkeys([contract_id, *(contracts or ())]))
        self._contracts: dict[str, ContractMarketState] = {
            code: ContractMarketState(
                contract_id=code,
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
                fallback_book_to_last=bool(fallback_book_to_last),
            )
            for code in ordered
        }
        self._default_contract = contract_id
        self._history: list[dict[str, object]] = []

    @property
    def contracts(self) -> tuple[str, ...]:
        return tuple(self._contracts.keys())

    def set_session_state(
        self,
        contract_id: str | None = None,
        *,
        opening_session: bool | None = None,
        market_session_active: bool | None = None,
    ) -> None:
        targets = [self._contracts[contract_id]] if contract_id else list(self._contracts.values())
        for state in targets:
            if opening_session is not None:
                state.opening_session = bool(opening_session)
            if market_session_active is not None:
                state.market_session_active = bool(market_session_active)

    def inject_quote_fault(
        self,
        contract_id: str,
        *,
        age_quality: str | None = None,
        reason: str | None = None,
        drop_tick: bool | None = None,
    ) -> None:
        state = self._contracts.get(contract_id)
        if state is None:
            raise FakeMarketError(f"Unknown market contract: {contract_id}")
        state.age_quality_override = age_quality
        state.quote_reason_override = reason
        if drop_tick is not None:
            state.drop_tick = bool(drop_tick)

    def set_fallback_book_to_last(self, enabled: bool, *, contract_id: str | None = None) -> None:
        """Enable or disable replacing missing bid1/ask1 with last price."""
        targets = [self._contracts[contract_id]] if contract_id else list(self._contracts.values())
        for state in targets:
            state.fallback_book_to_last = bool(enabled)

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
        delivery_time: str | None = None,
        drop_tick: bool = False,
        fallback_book_to_last: bool | None = None,
        append_to_buffer: bool = False,
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
            if not append_to_buffer:
                state.pending_ticks.clear()
            if native_time is not None:
                state.native_time = native_time
            self._history.append({
                "contract_id": code,
                "wall_time": wall_time,
                "drop_tick": True,
            })
            return

        effective_fallback = (
            state.fallback_book_to_last if fallback_book_to_last is None else bool(fallback_book_to_last)
        )
        last_text = _canon_price(last)
        if bid1 is None:
            bid_text: str | None = last_text if effective_fallback else None
        else:
            bid_text = _canon_price(bid1)
        if ask1 is None:
            ask_text: str | None = last_text if effective_fallback else None
        else:
            ask_text = _canon_price(ask1)
        state.last = last_text
        state.bid1 = bid_text
        state.ask1 = ask_text
        state.native_time = native_time or wall_time
        state.last_quote_wall_time = delivery_time or wall_time
        state.age_quality_override = None
        state.quote_reason_override = None
        tick_payload: dict[str, object] = {
            "native_time": state.native_time,
            "candle_id": state.candle_id,
            "last": last_text,
            "day_low": state.day_low,
            "day_high": state.day_high,
            "bid1": bid_text,
            "ask1": ask_text,
            "tick_size": state.tick_size,
            "lower_limit": state.lower_limit,
            "upper_limit": state.upper_limit,
        }
        if append_to_buffer:
            state.pending_ticks.append(tick_payload)
        else:
            state.pending_ticks.clear()
        self._history.append({
            "contract_id": code,
            "wall_time": wall_time,
            "delivery_time": state.last_quote_wall_time,
            "native_time": state.native_time,
            "last": last_text,
            "bid1": bid_text,
            "ask1": ask_text,
            "day_low": state.day_low,
            "day_high": state.day_high,
            "candle_id": state.candle_id,
        })

    def snapshot(self, *, wall_time: str | None = None, consume_buffer: bool = True) -> list[dict[str, object]]:
        """Return market rows ready to be merged with FakeBroker position buckets."""
        now_dt = _utc_dt(wall_time) if wall_time is not None else None
        rows: list[dict[str, object]] = []
        for state in self._contracts.values():
            ticks: list[dict[str, object]] = []
            if not state.drop_tick:
                if state.pending_ticks:
                    ticks = deepcopy(state.pending_ticks)
                    if consume_buffer:
                        state.pending_ticks.clear()
                elif state.last is not None and state.native_time is not None:
                    bid_val = state.bid1 if (state.bid1 is not None or not state.fallback_book_to_last) else state.last
                    ask_val = state.ask1 if (state.ask1 is not None or not state.fallback_book_to_last) else state.last
                    ticks.append({
                        "native_time": state.native_time,
                        "candle_id": state.candle_id,
                        "last": state.last,
                        "day_low": state.day_low,
                        "day_high": state.day_high,
                        "bid1": bid_val,
                        "ask1": ask_val,
                        "tick_size": state.tick_size,
                        "lower_limit": state.lower_limit,
                        "upper_limit": state.upper_limit,
                    })
            elif consume_buffer:
                state.pending_ticks.clear()
            source_age_ns: int | None = None
            silence_ns: int | None = None
            if now_dt is not None and state.last_quote_wall_time is not None and state.native_time is not None:
                native_dt = _utc_dt(state.native_time)
                quote_wall_dt = _utc_dt(state.last_quote_wall_time)
                source_age_ns = max(0, int((now_dt - native_dt).total_seconds() * 1_000_000_000))
                silence_ns = max(0, int((now_dt - quote_wall_dt).total_seconds() * 1_000_000_000))
            elif ticks:
                source_age_ns = 0
                silence_ns = 0

            obs: dict[str, object] = {
                "age_quality": state.age_quality_override or "VALID",
                "reason": state.quote_reason_override if state.quote_reason_override is not None else (None if ticks else "NO_TICK"),
                "source_time_raw": state.native_time,
            }
            if source_age_ns is not None:
                obs["source_age_raw_ns"] = str(source_age_ns)
            if silence_ns is not None:
                obs["silence_ns"] = str(silence_ns)

            row: dict[str, object] = {
                "contract_id": state.contract_id,
                "ticks": ticks,
                "opening_session": state.opening_session,
                "session_open": state.session_open,
                "session_close": state.session_close,
                "market_session_active": state.market_session_active,
                "quote_observation": obs,
                "available": state.available,
                "margin_per_lot": state.margin_per_lot,
                "multiplier": state.multiplier,
                "external_orders": state.external_orders,
            }
            rows.append(row)
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
