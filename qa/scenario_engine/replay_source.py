"""General read-only SQLite market replay source for MARKET_REPLAY_FIXTURE/1 databases."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import math
from pathlib import Path
import sqlite3
from typing import Any

from online_trader.domain.values import decimal_text
from qa.market_data.schema import BEIJING_TZ, ns_to_utc_iso
from qa.scenario_engine.models import InstrumentSpec, MarketTickTruth, SessionWindow


def _utc_iso_to_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"Timestamp must carry explicit UTC offset: {value!r}")
    utc_dt = parsed.astimezone(timezone.utc)
    return int(utc_dt.timestamp() * 1_000_000_000)


def _canon_opt(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return decimal_text(Decimal(text))


def compute_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RecordedReplaySource:
    """Streams real recorded market truth from a read-only SQLite fixture.

    Preserves missing bid1/ask1 (never fabricates book prices from last),
    respects per-contract trading sessions (including SHFE silver night session),
    and supports tick-by-tick (bucket_seconds=0) or bucketed iteration.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Recorded market fixture not found: {self.db_path}")
        self._sha256: str | None = None
        self.metadata: dict[str, str] = {}
        self.instruments: dict[str, InstrumentSpec] = {}
        self.sessions_by_symbol: dict[str, list[SessionWindow]] = {}
        self._load_metadata()

    @property
    def sha256(self) -> str:
        if self._sha256 is None:
            self._sha256 = compute_file_sha256(self.db_path)
        return self._sha256

    @property
    def trading_day(self) -> str:
        if "trading_day" in self.metadata:
            return self.metadata["trading_day"]
        for spec in self.instruments.values():
            return spec.trading_day
        raise RuntimeError(f"Unable to determine trading_day from {self.db_path}")

    @property
    def available_contracts(self) -> tuple[str, ...]:
        return tuple(sorted(self.instruments.keys()))

    def _connect_ro(self) -> sqlite3.Connection:
        uri = self.db_path.as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def _load_metadata(self) -> None:
        conn = self._connect_ro()
        try:
            for key, value in conn.execute("SELECT key, value FROM dataset_metadata").fetchall():
                self.metadata[str(key)] = str(value)

            for row in conn.execute(
                "SELECT symbol, trading_day, exchange_id, instrument_id, price_tick, volume_multiple, "
                "upper_limit, lower_limit, pre_close, pre_settlement FROM instruments ORDER BY symbol ASC"
            ).fetchall():
                spec = InstrumentSpec(
                    symbol=str(row[0]),
                    trading_day=str(row[1]),
                    exchange_id=str(row[2]),
                    instrument_id=str(row[3]),
                    price_tick=Decimal(str(row[4])),
                    volume_multiple=Decimal(str(row[5])),
                    upper_limit=_canon_opt(row[6]),
                    lower_limit=_canon_opt(row[7]),
                    pre_close=_canon_opt(row[8]),
                    pre_settlement=_canon_opt(row[9]),
                )
                self.instruments[spec.symbol] = spec

            for row in conn.execute(
                "SELECT symbol, trading_day, session_id, session_kind, open_utc, close_utc, "
                "open_ns, close_ns, is_opening_session, force_close_at_end "
                "FROM trading_sessions ORDER BY symbol ASC, open_ns ASC"
            ).fetchall():
                win = SessionWindow(
                    symbol=str(row[0]),
                    trading_day=str(row[1]),
                    session_id=str(row[2]),
                    session_kind=str(row[3]),
                    open_utc=str(row[4]),
                    close_utc=str(row[5]),
                    open_ns=int(row[6]),
                    close_ns=int(row[7]),
                    is_opening_session=bool(row[8]),
                    force_close_at_end=bool(row[9]),
                )
                self.sessions_by_symbol.setdefault(win.symbol, []).append(win)
        finally:
            conn.close()

    def session_for(self, symbol: str, timestamp_ns: int) -> SessionWindow | None:
        """Find the active trading session window for a contract at timestamp_ns."""
        windows = self.sessions_by_symbol.get(symbol)
        if not windows:
            return None
        for win in windows:
            if win.open_ns <= timestamp_ns < win.close_ns:
                return win
        return None

    def day_session_bounds(self, contracts: tuple[str, ...]) -> tuple[str, str]:
        """Return overall (earliest open_utc, latest close_utc) across selected contracts."""
        opens: list[tuple[int, str]] = []
        closes: list[tuple[int, str]] = []
        for sym in contracts:
            for win in self.sessions_by_symbol.get(sym, ()):
                opens.append((win.open_ns, win.open_utc))
                closes.append((win.close_ns, win.close_utc))
        if opens and closes:
            return min(opens, key=lambda x: x[0])[1], max(closes, key=lambda x: x[0])[1]
        day = self.trading_day
        return f"{day}T01:00:00Z", f"{day}T07:00:00Z"

    def derive_profile_offsets(
        self,
        contracts: tuple[str, ...],
        *,
        start_utc: str | None = None,
        window_minutes: int = 15,
    ) -> dict[str, dict[str, int]]:
        """Derive reasonable DSV offsets per contract from the opening window of the dataset."""
        start_ns = _utc_iso_to_ns(start_utc) if start_utc else None
        conn = self._connect_ro()
        offsets: dict[str, dict[str, int]] = {}
        try:
            for sym in contracts:
                spec = self.instruments[sym]
                if start_ns is None:
                    first_row = conn.execute(
                        "SELECT MIN(source_timestamp_ns) FROM market_ticks WHERE symbol = ? AND last_price IS NOT NULL",
                        (sym,),
                    ).fetchone()
                    sym_start = int(first_row[0]) if first_row and first_row[0] is not None else 0
                else:
                    sym_start = start_ns
                sym_end = sym_start + window_minutes * 60 * 1_000_000_000
                rows = conn.execute(
                    "SELECT last_price, lowest, highest FROM market_ticks "
                    "WHERE symbol = ? AND source_timestamp_ns >= ? AND source_timestamp_ns <= ? "
                    "AND last_price IS NOT NULL ORDER BY source_timestamp_ns ASC, sequence ASC",
                    (sym, sym_start, sym_end),
                ).fetchall()
                prices = [Decimal(r[0]) for r in rows]
                lows = [Decimal(r[1]) for r in rows if r[1] is not None]
                highs = [Decimal(r[2]) for r in rows if r[2] is not None]
                if lows and highs:
                    r_price = max(highs) - min(lows)
                elif prices:
                    r_price = max(prices) - min(prices)
                else:
                    r_price = spec.price_tick * 10
                r_ticks = max(1, int((r_price / spec.price_tick).to_integral_value()))
                stop_ticks = max(3, round(0.3 * r_ticks))
                open_ticks = max(stop_ticks, math.ceil((r_ticks + stop_ticks) / 2) + 1)
                profit_ticks = 2 * stop_ticks
                offsets[sym] = {
                    "range_ticks": r_ticks,
                    "open_offset": max(1, int((Decimal(open_ticks) * spec.price_tick).to_integral_value())),
                    "stop_offset": max(1, int((Decimal(stop_ticks) * spec.price_tick).to_integral_value())),
                    "profit_offset": max(1, int((Decimal(profit_ticks) * spec.price_tick).to_integral_value())),
                }
        finally:
            conn.close()
        return offsets

    def iter_rounds(
        self,
        *,
        contracts: tuple[str, ...] | None = None,
        start_utc: str | None = None,
        end_utc: str | None = None,
        bucket_seconds: int = 0,
        max_rounds: int | None = None,
    ) -> Iterator[tuple[int, str, dict[str, MarketTickTruth]]]:
        """Yield ``(round_time_ns, round_time_utc, truth_by_contract)`` in strict timestamp order.

        - ``bucket_seconds == 0``: true tick-by-tick stream (groups only identical source timestamps).
        - ``bucket_seconds > 0``: downsamples into fixed buckets while keeping running day_low/day_high.
        """
        selected = tuple(contracts) if contracts else self.available_contracts
        missing = [c for c in selected if c not in self.instruments]
        if missing:
            raise ValueError(f"Requested contracts not found in {self.db_path}: {missing}")
        if bucket_seconds < 0:
            raise ValueError("bucket_seconds must be >= 0")

        placeholders = ",".join("?" for _ in selected)
        params: list[object] = list(selected)
        where = [f"symbol IN ({placeholders})", "last_price IS NOT NULL"]
        if start_utc is not None:
            where.append("source_timestamp_ns >= ?")
            params.append(_utc_iso_to_ns(start_utc))
        if end_utc is not None:
            where.append("source_timestamp_ns <= ?")
            params.append(_utc_iso_to_ns(end_utc))

        sql = (
            "SELECT symbol, trading_day, source_timestamp_ns, last_price, "
            "bid_price1, bid_volume1, ask_price1, ask_volume1, lowest, highest, "
            "lower_limit, upper_limit "
            f"FROM market_ticks WHERE {' AND '.join(where)} "
            "ORDER BY source_timestamp_ns ASC, sequence ASC"
        )

        running_low: dict[str, Decimal] = {}
        running_high: dict[str, Decimal] = {}
        bucket_ns = bucket_seconds * 1_000_000_000
        emitted = 0

        conn = self._connect_ro()
        try:
            cursor = conn.execute(sql, params)
            current_key_ns: int | None = None
            current_batch: dict[str, MarketTickTruth] = {}
            anchor_ns: int | None = None

            for row in cursor:
                sym = str(row[0])
                day = str(row[1])
                ts_ns = int(row[2])
                last_str = _canon_opt(row[3])
                if last_str is None:
                    continue
                last_d = Decimal(last_str)
                low_d = Decimal(str(row[8])) if row[8] is not None else last_d
                high_d = Decimal(str(row[9])) if row[9] is not None else last_d
                running_low[sym] = min(running_low[sym], low_d, last_d) if sym in running_low else min(low_d, last_d)
                running_high[sym] = max(running_high[sym], high_d, last_d) if sym in running_high else max(high_d, last_d)

                if anchor_ns is None:
                    anchor_ns = ts_ns
                if bucket_ns > 0:
                    idx = max(1, (ts_ns - anchor_ns + bucket_ns - 1) // bucket_ns)
                    key_ns = anchor_ns + idx * bucket_ns
                else:
                    key_ns = ts_ns

                if current_key_ns is not None and key_ns != current_key_ns and current_batch:
                    yield current_key_ns, ns_to_utc_iso(current_key_ns), current_batch
                    emitted += 1
                    if max_rounds is not None and emitted >= max_rounds:
                        return
                    current_batch = {}

                current_key_ns = key_ns
                spec = self.instruments[sym]
                win = self.session_for(sym, ts_ns)
                dt_cst = (
                    datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ts_ns // 1_000)
                ).astimezone(BEIJING_TZ)
                truth = MarketTickTruth(
                    symbol=sym,
                    trading_day=day,
                    source_time_ns=ts_ns,
                    source_time_utc=ns_to_utc_iso(ts_ns),
                    last=last_str,
                    bid1=_canon_opt(row[4]),
                    bid_volume1=int(row[5]) if row[5] is not None else None,
                    ask1=_canon_opt(row[6]),
                    ask_volume1=int(row[7]) if row[7] is not None else None,
                    day_low=decimal_text(running_low[sym]),
                    day_high=decimal_text(running_high[sym]),
                    lower_limit=_canon_opt(row[10]) or spec.lower_limit,
                    upper_limit=_canon_opt(row[11]) or spec.upper_limit,
                    candle_id=f"bar-{dt_cst.strftime('%H%M')}",
                    opening_session=win.is_opening_session if win is not None else True,
                    market_session_active=win is not None if self.sessions_by_symbol.get(sym) else True,
                    session_open_utc=win.open_utc if win is not None else None,
                    session_close_utc=win.close_utc if win is not None else None,
                )
                current_batch[sym] = truth

            if current_key_ns is not None and current_batch:
                if max_rounds is None or emitted < max_rounds:
                    yield current_key_ns, ns_to_utc_iso(current_key_ns), current_batch
        finally:
            conn.close()
