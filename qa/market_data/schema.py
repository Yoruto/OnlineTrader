"""SQLite schema and canonical value normalization for QA market replay fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import math
import sqlite3
from typing import Any

from online_trader.domain.values import decimal_text, decimal_value


SCHEMA_VERSION = "MARKET_REPLAY_FIXTURE/1"
DEFAULT_CONTRACTS: tuple[str, ...] = (
    "DCE.p2701",
    "DCE.l2701",
    "SHFE.bu2701",
    "CZCE.OI701",
)
BEIJING_TZ = timezone(timedelta(hours=8))

_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dataset_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    trading_day TEXT NOT NULL,
    exchange_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    price_tick TEXT NOT NULL,
    volume_multiple TEXT NOT NULL,
    upper_limit TEXT,
    lower_limit TEXT,
    pre_close TEXT,
    pre_settlement TEXT,
    expire_datetime_ns INTEGER
);

CREATE TABLE IF NOT EXISTS trading_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_kind TEXT NOT NULL,
    open_utc TEXT NOT NULL,
    close_utc TEXT NOT NULL,
    open_ns INTEGER NOT NULL,
    close_ns INTEGER NOT NULL,
    is_opening_session INTEGER NOT NULL,
    force_close_at_end INTEGER NOT NULL,
    UNIQUE(symbol, trading_day, session_id)
);

CREATE TABLE IF NOT EXISTS market_ticks (
    sequence INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    source_timestamp_ns INTEGER NOT NULL,
    source_sequence INTEGER,
    received_at_ns INTEGER,
    batch_id TEXT,
    last_price TEXT,
    open_price TEXT,
    highest TEXT,
    lowest TEXT,
    average TEXT,
    volume INTEGER,
    amount TEXT,
    open_interest INTEGER,
    upper_limit TEXT,
    lower_limit TEXT,
    pre_close TEXT,
    pre_settlement TEXT,
    bid_price1 TEXT,
    bid_volume1 INTEGER,
    ask_price1 TEXT,
    ask_volume1 INTEGER,
    bid_price2 TEXT,
    bid_volume2 INTEGER,
    ask_price2 TEXT,
    ask_volume2 INTEGER,
    bid_price3 TEXT,
    bid_volume3 INTEGER,
    ask_price3 TEXT,
    ask_volume3 INTEGER,
    bid_price4 TEXT,
    bid_volume4 INTEGER,
    ask_price4 TEXT,
    ask_volume4 INTEGER,
    bid_price5 TEXT,
    bid_volume5 INTEGER,
    ask_price5 TEXT,
    ask_volume5 INTEGER
);

CREATE TABLE IF NOT EXISTS market_klines (
    symbol TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    datetime_ns INTEGER NOT NULL,
    kline_id INTEGER,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume INTEGER NOT NULL,
    open_oi INTEGER,
    close_oi INTEGER,
    PRIMARY KEY (symbol, duration_seconds, datetime_ns)
);

CREATE INDEX IF NOT EXISTS idx_market_ticks_symbol_ts
    ON market_ticks(symbol, source_timestamp_ns, sequence);

CREATE INDEX IF NOT EXISTS idx_market_ticks_day_seq
    ON market_ticks(trading_day, sequence);

CREATE INDEX IF NOT EXISTS idx_market_klines_lookup
    ON market_klines(symbol, duration_seconds, datetime_ns);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes in the target SQLite database."""
    conn.executescript(_DDL)


def _is_sentinel_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, float):
        return not math.isfinite(value) or abs(value) >= 1e18
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.lower() in {"nan", "inf", "+inf", "-inf", "none", "null", "-"}:
            return True
    return False


def normalize_decimal_text(
    value: Any,
    *,
    positive: bool = False,
    allow_zero: bool = True,
    max_decimals: int = 12,
) -> str | None:
    """Convert a supplier numeric value into a canonical decimal string or None."""
    if _is_sentinel_number(value):
        return None
    try:
        raw = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not raw.is_finite() or abs(raw) >= Decimal("1e18"):
        return None
    if positive and raw <= 0:
        return None
    if not allow_zero and raw == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 64
        text = decimal_text(raw)
        try:
            decimal_value(text, positive=positive)
            return text
        except Exception:
            quant = Decimal("1").scaleb(-max_decimals)
            rounded = raw.quantize(quant, rounding=ROUND_HALF_UP)
            text = decimal_text(rounded)
            decimal_value(text, positive=positive)
            return text


def normalize_int(value: Any, *, minimum: int = 0) -> int | None:
    """Convert a supplier integer/float count into a bounded int or None."""
    if _is_sentinel_number(value):
        return None
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite() or dec != dec.to_integral_value():
        return None
    parsed = int(dec)
    if parsed < minimum or parsed > 9_223_372_036_854_775_807:
        return None
    return parsed


def normalize_depth_level(price_raw: Any, volume_raw: Any) -> tuple[str | None, int | None]:
    """Normalize one bid/ask book level; missing/invalid prices yield NULL price and NULL volume."""
    price = normalize_decimal_text(price_raw, positive=True)
    if price is None:
        return None, None
    volume = normalize_int(volume_raw, minimum=0)
    return price, volume


def ns_to_utc_iso(timestamp_ns: int) -> str:
    """Convert UTC epoch nanoseconds to canonical ISO-8601 Z string."""
    sec, rem_ns = divmod(int(timestamp_ns), 1_000_000_000)
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=sec, microseconds=rem_ns // 1_000)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ns_to_beijing_iso(timestamp_ns: int) -> str:
    """Convert UTC epoch nanoseconds to explicit +08:00 ISO string."""
    sec, rem_ns = divmod(int(timestamp_ns), 1_000_000_000)
    dt = (
        datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=sec, microseconds=rem_ns // 1_000)
    ).astimezone(BEIJING_TZ)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+08:00")
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
