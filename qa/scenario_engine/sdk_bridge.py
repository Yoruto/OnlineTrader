"""Lightweight FakeTqApi bridge to drive the real production DsvSdkAdapter offline.

Closes the gap where OfflineAdapter bypasses DsvSdkAdapter quote parsing,
source_age_raw_ns / silence_ns tracking, NaN sentinel book handling, and
market_permission gating.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.scheduling.clock import ClockSample
from qa.scenario_engine.models import InstrumentSpec, MarketTickTruth, ObservedTick, SessionWindow


BEIJING_TZ = timezone(timedelta(hours=8))


def _iso_to_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"Expected timezone-aware ISO string, got {value!r}")
    return parsed.astimezone(timezone.utc)


def _utc_to_tq_datetime(iso_utc: str) -> str:
    return _iso_to_utc(iso_utc).astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")


def _to_float_or_sentinel(value: str | int | float | Decimal | None, *, use_nan: bool = True) -> float | None:
    if value is None:
        return float("nan") if use_nan else None
    return float(value)


class BridgeClock:
    """Controllable (utc_datetime, monotonic_ms) clock matching DsvSdkAdapter's clock protocol."""

    def __init__(self, initial_utc: str, *, monotonic_ms: int = 1_000, clock_epoch: str | None = None) -> None:
        self.utc = _iso_to_utc(initial_utc)
        self.monotonic_ms = int(monotonic_ms)
        self.epoch = clock_epoch or uuid4().hex

    def __call__(self) -> tuple[datetime, int]:
        return self.utc, self.monotonic_ms

    def advance(self, *, milliseconds: int, wall_ms: int | None = None) -> None:
        self.utc += timedelta(milliseconds=milliseconds if wall_ms is None else wall_ms)
        self.monotonic_ms += int(milliseconds)

    def advance_to(self, target_utc: str, *, monotonic_delta_ms: int | None = None) -> None:
        target = _iso_to_utc(target_utc)
        delta_ms = int((target - self.utc).total_seconds() * 1_000)
        step_ms = max(1, delta_ms) if monotonic_delta_ms is None else int(monotonic_delta_ms)
        self.utc = target
        self.monotonic_ms += step_ms

    def sample(self, *, clock_epoch: str | None = None) -> ClockSample:
        iso = self.utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return ClockSample(
            now_utc=iso,
            monotonic_ns=str(self.monotonic_ms * 1_000_000),
            clock_epoch=clock_epoch or self.epoch,
            sample_span_ns="0",
        )


class FakeTqApi:
    """Multi-contract offline TqApi stub feeding real DsvSdkAdapter without network."""

    def __init__(
        self,
        contracts: tuple[str, ...],
        *,
        instruments: dict[str, InstrumentSpec] | None = None,
    ) -> None:
        self.contracts = tuple(contracts)
        self.quotes: dict[str, SimpleNamespace] = {}
        self.klines: dict[str, SimpleNamespace] = {}
        self.positions: dict[str, SimpleNamespace] = {}
        self.funds = SimpleNamespace(
            available=10_000_000.0,
            balance=10_000_000.0,
            margin=0.0,
            frozen_margin=0.0,
            risk_ratio=0.0,
        )
        self.orders: dict[str, SimpleNamespace] = {}
        self.trades: dict[str, SimpleNamespace] = {}
        self.calls: list[tuple[object, ...]] = []

        for sym in self.contracts:
            spec = (instruments or {}).get(sym)
            self.quotes[sym] = SimpleNamespace(
                datetime="",
                expired=False,
                last_price=float("nan"),
                highest=float("nan"),
                lowest=float("nan"),
                bid_price1=float("nan"),
                ask_price1=float("nan"),
                price_tick=float(spec.price_tick) if spec else 1.0,
                lower_limit=float(spec.lower_limit) if (spec and spec.lower_limit) else 900.0,
                upper_limit=float(spec.upper_limit) if (spec and spec.upper_limit) else 1200.0,
                volume_multiple=float(spec.volume_multiple) if spec else 10.0,
                margin=1000.0,
            )
            self.klines[sym] = SimpleNamespace(iloc=[{"datetime": 0}])
            self.positions[sym] = SimpleNamespace(
                pos_long=0,
                pos_long_today=0,
                pos_long_his=0,
                pos_short=0,
                pos_short_today=0,
                pos_short_his=0,
                margin=0.0,
            )

    def apply_observed_tick(
        self,
        contract_id: str,
        tick: ObservedTick | MarketTickTruth | dict[str, object],
        *,
        use_nan_for_missing_book: bool = True,
    ) -> None:
        """Populate a TqSdk-shaped Quote from an engine ObservedTick, MarketTickTruth, or step dict."""
        quote = self.quotes[contract_id]
        if isinstance(tick, ObservedTick):
            if tick.drop_tick or tick.last is None:
                return
            source_utc = tick.source_time_utc
            last_v, bid1_v, ask1_v = tick.last, tick.bid1, tick.ask1
            low_v, high_v = tick.day_low, tick.day_high
        elif isinstance(tick, MarketTickTruth):
            source_utc = tick.source_time_utc
            last_v, bid1_v, ask1_v = tick.last, tick.bid1, tick.ask1
            low_v, high_v = tick.day_low, tick.day_high
            if tick.lower_limit is not None:
                quote.lower_limit = float(tick.lower_limit)
            if tick.upper_limit is not None:
                quote.upper_limit = float(tick.upper_limit)
        else:
            if tick.get("drop_tick") or tick.get("last") is None:
                return
            source_utc = str(tick.get("native_time") or tick.get("source_time_utc"))
            last_v = tick.get("last")  # type: ignore[assignment]
            bid1_v = tick.get("bid1")  # type: ignore[assignment]
            ask1_v = tick.get("ask1")  # type: ignore[assignment]
            low_v = tick.get("day_low")  # type: ignore[assignment]
            high_v = tick.get("day_high")  # type: ignore[assignment]

        quote.datetime = _utc_to_tq_datetime(source_utc)
        quote.last_price = float(last_v)  # type: ignore[arg-type]
        quote.bid_price1 = _to_float_or_sentinel(bid1_v, use_nan=use_nan_for_missing_book)  # type: ignore[arg-type]
        quote.ask_price1 = _to_float_or_sentinel(ask1_v, use_nan=use_nan_for_missing_book)  # type: ignore[arg-type]
        if low_v is not None:
            quote.lowest = float(low_v)  # type: ignore[arg-type]
        if high_v is not None:
            quote.highest = float(high_v)  # type: ignore[arg-type]
        dt = _iso_to_utc(source_utc)
        self.klines[contract_id].iloc[0]["datetime"] = (int(dt.timestamp()) // 60) * 60 * 1_000_000_000

    def set_raw_quote_datetime(self, contract_id: str, raw_datetime: str) -> None:
        self.quotes[contract_id].datetime = raw_datetime

    # ---- TqApi surface consumed by DsvSdkAdapter ----

    def get_quote(self, symbol: str) -> SimpleNamespace:
        return self.quotes[symbol]

    def get_kline_serial(self, symbol: str, duration: int, *, data_length: int) -> SimpleNamespace:
        return self.klines[symbol]

    def get_position(self, symbol: str, *, account: object = None) -> SimpleNamespace:
        return self.positions[symbol]

    def get_account(self, *, account: object = None) -> SimpleNamespace:
        return self.funds

    def get_order(self, *, account: object = None) -> dict[str, SimpleNamespace]:
        return self.orders

    def get_trade(self, *, account: object = None) -> dict[str, SimpleNamespace]:
        return self.trades

    def wait_update(self, *, deadline: float) -> bool:
        return False

    def insert_order(
        self,
        *,
        symbol: str,
        direction: str,
        offset: str,
        volume: int,
        limit_price: float,
        order_id: str,
        account: object = None,
    ) -> SimpleNamespace:
        exchange, instrument = symbol.split(".")
        order = SimpleNamespace(
            order_id=order_id,
            exchange_id=exchange,
            instrument_id=instrument,
            direction=direction,
            offset=offset,
            volume_orign=volume,
            volume_left=volume,
            limit_price=limit_price,
            status="ALIVE",
            insert_date_time=0,
            is_error=False,
            _this_session=True,
        )
        self.orders[order_id] = order
        self.calls.append(("SEND", order_id, symbol, direction, offset, volume, limit_price))
        return order

    def cancel_order(self, order_id: str, *, account: object = None) -> None:
        self.calls.append(("CANCEL", order_id))


def build_calendar_from_sessions(
    *,
    contracts: tuple[str, ...],
    sessions_by_symbol: dict[str, list[SessionWindow]],
    coverage_from_utc: str,
    coverage_to_utc: str,
    calendar_id: str = "qa-sdk-bridge-calendar",
) -> TradingCalendar:
    """Build a production TradingCalendar from recorded SessionWindow definitions."""
    contract_records = []
    for sym in contracts:
        windows = sessions_by_symbol.get(sym, [])
        contract_records.append({
            "contract_id": sym,
            "sessions": [
                {
                    "session_id": w.session_id,
                    "trading_day": w.trading_day,
                    "open_utc": w.open_utc,
                    "close_utc": w.close_utc,
                    "is_opening_session": w.is_opening_session,
                    "force_close_at_end": w.force_close_at_end,
                }
                for w in windows
            ],
        })
    return TradingCalendar.from_record({
        "schema_version": "TRADING_CALENDAR/1",
        "calendar_id": calendar_id,
        "calendar_version": "1",
        "complete": True,
        "coverage_from_utc": coverage_from_utc,
        "coverage_to_utc": coverage_to_utc,
        "contracts": contract_records,
    })


class DsvSdkAdapterBridge:
    """Drives the real production DsvSdkAdapter with FakeTqApi + BridgeClock."""

    def __init__(
        self,
        *,
        contracts: tuple[str, ...],
        profile: DsvProfile,
        calendar: TradingCalendar,
        initial_utc: str,
        account_key: str = "DEV:sdk-bridge",
        instruments: dict[str, InstrumentSpec] | None = None,
        owner_mode: str = "STANDALONE",
    ) -> None:
        self.contracts = tuple(contracts)
        self.clock = BridgeClock(initial_utc)
        self.api = FakeTqApi(self.contracts, instruments=instruments)
        self.adapter = DsvSdkAdapter(
            self.api,
            object(),
            account_key=account_key,
            profile=profile,
            calendar=calendar,
            sdk_version=SDK_VERSION,
            clock=self.clock,
        )
        self.adapter.owner_mode = owner_mode

    def observe_market_row(
        self,
        contract_id: str,
        *,
        trading_day: str,
        previous_symbol_state: dict[str, Any] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Invoke real DsvSdkAdapter._observe_quote + _market and return (market_row, observation)."""
        sample = self.clock.sample()
        window = self.adapter.calendar.query(contract_id, sample.now_utc)
        quote_time, observation = self.adapter._observe_quote(
            contract_id,
            sample,
            window,
            trading_day,
        )
        row = self.adapter._market(
            contract_id,
            trading_day,
            self.clock.utc,
            self.api.funds,
            previous_symbol_state or {},
            False,
            {},
            quote_time=quote_time,
            window=window,
            observation=observation,
            sample=sample,
        )
        return row, observation
