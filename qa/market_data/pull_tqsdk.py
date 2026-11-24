"""Pull market ticks and warmup klines from TqSdk (supports ordinary accounts).

For ordinary accounts without the Pro ``tq_dl`` feature, ``get_tick_serial``
retrieves up to the latest 10,000 ticks per contract (typically ~1.5 hours of
active trading) alongside multi-day 1-minute and daily klines. The generated
manifest explicitly labels partial vs full trading-day coverage.

No trading account (TqKq/TqAccount) is connected and no order API is called.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
for entry in (str(ROOT), str(SRC_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qa.market_data.builder import build_sessions_from_trading_time, hash_records, write_market_fixture
from qa.market_data.schema import DEFAULT_CONTRACTS, SCHEMA_VERSION, normalize_decimal_text, normalize_int
from qa.online_kq_preflight import credentials


DEFAULT_DB = ROOT / "qa" / "fixtures" / "market" / "market_fixture.sqlite3"
DEFAULT_MANIFEST = ROOT / "qa" / "fixtures" / "market" / "manifest.json"


def _df_records(df: Any) -> list[dict[str, Any]]:
    valid = df.dropna(subset=["datetime"])
    valid = valid[valid["datetime"] > 0]
    return valid.to_dict("records")


def pull_from_tqsdk(
    *,
    contracts: tuple[str, ...],
    trading_day: str,
    night_date: str | None,
    tick_length: int,
    kline_1m_length: int,
    kline_1d_length: int,
    db_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    auth = credentials()
    logging.disable(logging.CRITICAL)
    from tqsdk import TqApi, TqAuth, __version__ as tqsdk_version

    api = TqApi(
        auth=TqAuth(auth["username"], auth["password"]),
        debug=False,
        web_gui=False,
        disable_print=True,
    )
    del auth
    try:
        has_tq_dl = bool(api._auth._has_feature("tq_dl"))
        instruments: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        raw_ticks: list[dict[str, Any]] = []
        raw_klines: list[dict[str, Any]] = []
        raw_provenance: list[dict[str, Any]] = []

        for symbol in contracts:
            quote = api.get_quote(symbol)
            tick_df = api.get_tick_serial(symbol, data_length=min(10000, max(1, tick_length)))
            k1m_df = api.get_kline_serial(symbol, 60, data_length=min(10000, max(1, kline_1m_length)))
            k1d_df = api.get_kline_serial(symbol, 86400, data_length=min(10000, max(1, kline_1d_length)))

            exchange_id, instrument_id = symbol.split(".", 1)
            expire_sec = getattr(quote, "expire_datetime", None)
            expire_ns = int(expire_sec) * 1_000_000_000 if isinstance(expire_sec, (int, float)) and expire_sec > 0 else None

            inst = {
                "symbol": symbol,
                "trading_day": trading_day,
                "exchange_id": exchange_id,
                "instrument_id": instrument_id,
                "price_tick": normalize_decimal_text(getattr(quote, "price_tick", None), positive=True) or "1",
                "volume_multiple": normalize_decimal_text(getattr(quote, "volume_multiple", None), positive=True) or "1",
                "upper_limit": normalize_decimal_text(getattr(quote, "upper_limit", None), positive=True),
                "lower_limit": normalize_decimal_text(getattr(quote, "lower_limit", None), positive=True),
                "pre_close": normalize_decimal_text(getattr(quote, "pre_close", None), positive=True),
                "pre_settlement": normalize_decimal_text(getattr(quote, "pre_settlement", None), positive=True),
                "expire_datetime_ns": expire_ns,
            }
            instruments.append(inst)

            trading_time = getattr(quote, "trading_time", None) or {}
            sym_sessions = build_sessions_from_trading_time(
                symbol,
                trading_day,
                trading_time,
                night_date=night_date,
            )
            sessions.extend(sym_sessions)

            day_start_ns = min(s["open_ns"] for s in sym_sessions) if sym_sessions else 0
            day_end_ns = max(s["close_ns"] for s in sym_sessions) if sym_sessions else 2**62

            tick_rows = _df_records(tick_df)
            kept_ticks = 0
            for row in tick_rows:
                ts_ns = int(row["datetime"])
                # Allow 5-minute call-auction window before opening session and 60s close tail
                if ts_ns < day_start_ns - 300 * 1_000_000_000 or ts_ns > day_end_ns + 60 * 1_000_000_000:
                    continue
                kept_ticks += 1
                item = {
                    "symbol": symbol,
                    "source_timestamp_ns": ts_ns,
                    "source_sequence": normalize_int(row.get("id"), minimum=0),
                    "received_at_ns": None,
                    "batch_id": None,
                    "last_price": row.get("last_price"),
                    "open_price": None,
                    "highest": row.get("highest"),
                    "lowest": row.get("lowest"),
                    "average": row.get("average"),
                    "volume": row.get("volume"),
                    "amount": row.get("amount"),
                    "open_interest": row.get("open_interest"),
                    "upper_limit": inst["upper_limit"],
                    "lower_limit": inst["lower_limit"],
                    "pre_close": inst["pre_close"],
                    "pre_settlement": inst["pre_settlement"],
                }
                for side in ("bid", "ask"):
                    for lvl in range(1, 6):
                        item[f"{side}_price{lvl}"] = row.get(f"{side}_price{lvl}")
                        item[f"{side}_volume{lvl}"] = row.get(f"{side}_volume{lvl}")
                raw_ticks.append(item)

            for dur, df in ((60, k1m_df), (86400, k1d_df)):
                for row in _df_records(df):
                    raw_klines.append({
                        "symbol": symbol,
                        "duration_seconds": dur,
                        "datetime_ns": int(row["datetime"]),
                        "kline_id": normalize_int(row.get("id"), minimum=0),
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "close": row.get("close"),
                        "volume": row.get("volume"),
                        "open_oi": row.get("open_oi"),
                        "close_oi": row.get("close_oi"),
                    })

            raw_provenance.append({
                "symbol": symbol,
                "raw_tick_rows": len(tick_rows),
                "kept_tick_rows": kept_ticks,
                "first_tick_ns": int(tick_rows[0]["datetime"]) if tick_rows else None,
                "last_tick_ns": int(tick_rows[-1]["datetime"]) if tick_rows else None,
            })

        # Determine honest coverage scope
        session_covered = set()
        for sym in contracts:
            sym_sess = [s for s in sessions if s["symbol"] == sym]
            sym_ts = [t["source_timestamp_ns"] for t in raw_ticks if t["symbol"] == sym]
            if sym_ts:
                min_t, max_t = min(sym_ts), max(sym_ts)
                for s in sym_sess:
                    if min_t <= s["close_ns"] and max_t >= s["open_ns"]:
                        session_covered.add(s["session_id"])

        total_sessions = len(sessions)
        if len(session_covered) == total_sessions and total_sessions > 0:
            coverage_scope = "FULL_TRADING_DAY"
        elif session_covered:
            coverage_scope = "PARTIAL_DAY_AFTERNOON_TAIL"
        else:
            coverage_scope = "EMPTY"

        known_gaps = []
        if coverage_scope != "FULL_TRADING_DAY":
            known_gaps.append(
                "Ordinary TqSdk account lacks tq_dl Pro download permission; "
                "get_tick_serial is capped at the latest 10,000 ticks per contract "
                "(covering afternoon tail only; night and morning tick sessions require Pro download or live recording)."
            )

        metadata = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": f"ot-market-{trading_day}-4contracts",
            "trading_day": trading_day,
            "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": {
                "provider": "TqSdk",
                "sdk_version": tqsdk_version,
                "method": "get_tick_serial+get_kline_serial+get_quote",
                "has_tq_dl": has_tq_dl,
                "account_tier": "PRO" if has_tq_dl else "ORDINARY",
                "per_symbol_provenance": raw_provenance,
                "raw_provenance_sha256": hash_records(raw_provenance),
            },
            "coverage_scope": coverage_scope,
            "time_semantics": {
                "source_timestamp_ns": "UTC epoch nanoseconds converted from exchange/TqSdk tick timestamp",
                "received_at_ns": "NULL for serial history pull (no client receive clock)",
                "tie_break_order": "(source_timestamp_ns, source_sequence, symbol, input_order)",
            },
            "known_gaps": known_gaps,
            "license_notice": "QA-only replay fixture obtained via user's TqSdk ordinary account; contains zero user credentials, orders, or account ledgers.",
        }

        return write_market_fixture(
            db_path=db_path,
            manifest_path=manifest_path,
            dataset_id=str(metadata["dataset_id"]),
            trading_day=trading_day,
            instruments=instruments,
            sessions=sessions,
            raw_ticks=raw_ticks,
            raw_klines=raw_klines,
            metadata=metadata,
        )
    finally:
        api.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull real 4-contract TqSdk ticks & klines into SQLite fixture.")
    parser.add_argument("--trading-day", required=True, help="Target cabinet trading day YYYY-MM-DD (e.g. 2026-11-24)")
    parser.add_argument("--night-date", default=None, help="Optional calendar date of prior night session YYYY-MM-DD")
    parser.add_argument("--contracts", nargs="+", default=list(DEFAULT_CONTRACTS), help="Contracts to fetch independently")
    parser.add_argument("--tick-length", type=int, default=10000, help="get_tick_serial length per symbol (max 10000)")
    parser.add_argument("--kline-1m-length", type=int, default=2000, help="1-minute bar length per symbol")
    parser.add_argument("--kline-1d-length", type=int, default=30, help="Daily bar length per symbol")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Output SQLite database path")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Output manifest.json path")
    args = parser.parse_args(argv)

    manifest = pull_from_tqsdk(
        contracts=tuple(args.contracts),
        trading_day=args.trading_day,
        night_date=args.night_date,
        tick_length=args.tick_length,
        kline_1m_length=args.kline_1m_length,
        kline_1d_length=args.kline_1d_length,
        db_path=args.db,
        manifest_path=args.manifest,
    )
    print(json.dumps({
        "dataset_id": manifest["dataset_id"],
        "coverage_scope": manifest["coverage_scope"],
        "database_sha256": manifest["database_sha256"],
        "database_size_bytes": manifest["database_size_bytes"],
        "table_counts": manifest["table_counts"],
        "kline_counts": manifest["kline_counts"],
        "depth_summary": {
            sym: info["depth_capability"]
            for sym, info in manifest["depth_report"].items()
        },
        "warnings": manifest["warnings"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
