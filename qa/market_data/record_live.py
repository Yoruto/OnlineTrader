"""QA-only live TqSdk recorder for capturing a complete trading day with an ordinary account.

Subscribes to the four target contracts using ``get_tick_serial`` (incremental
exchange tick ``id`` cursor) plus ``get_quote`` and warmup ``get_kline_serial``
without connecting any trading account or calling order APIs. Preserves
``source_timestamp_ns``, client ``received_at_ns``, and ``batch_id`` across
``wait_update`` cycles, then compiles the finalized recording into the standard
read-only SQLite fixture and manifest.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
for entry in (str(ROOT), str(SRC_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qa.market_data.builder import build_sessions_from_trading_time, hash_records, write_market_fixture
from qa.market_data.schema import DEFAULT_CONTRACTS, SCHEMA_VERSION, normalize_decimal_text, normalize_int
from qa.online_kq_preflight import credentials


def _extract_new_ticks(
    symbol: str,
    df: Any,
    last_id: int | None,
    *,
    received_at_ns: int,
    batch_id: str,
    inst: dict[str, Any],
) -> tuple[list[dict[str, Any]], int | None]:
    valid = df.dropna(subset=["datetime"])
    valid = valid[valid["datetime"] > 0]
    if last_id is not None:
        valid = valid[valid["id"] > last_id]
    rows: list[dict[str, Any]] = []
    max_id = last_id
    for row in valid.to_dict("records"):
        tid = normalize_int(row.get("id"), minimum=0)
        if tid is not None and (max_id is None or tid > max_id):
            max_id = tid
        item: dict[str, Any] = {
            "symbol": symbol,
            "source_timestamp_ns": int(row["datetime"]),
            "source_sequence": tid,
            "received_at_ns": received_at_ns,
            "batch_id": batch_id,
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
        rows.append(item)
    return rows, max_id


def record_trading_day(
    *,
    contracts: tuple[str, ...],
    trading_day: str,
    night_date: str | None,
    staging_jsonl: Path,
    db_path: Path,
    manifest_path: Path,
    stop_after_close_seconds: int = 120,
    max_duration_seconds: int | None = None,
) -> dict[str, Any]:
    auth = credentials()
    logging.disable(logging.CRITICAL)
    from tqsdk import TqApi, TqAuth, __version__ as tqsdk_version

    staging_jsonl = staging_jsonl.resolve()
    staging_jsonl.parent.mkdir(parents=True, exist_ok=True)

    api = TqApi(
        auth=TqAuth(auth["username"], auth["password"]),
        debug=False,
        web_gui=False,
        disable_print=True,
    )
    del auth
    try:
        quotes = {s: api.get_quote(s) for s in contracts}
        tick_dfs = {s: api.get_tick_serial(s, data_length=500) for s in contracts}
        k1m_dfs = {s: api.get_kline_serial(s, 60, data_length=2000) for s in contracts}
        k1d_dfs = {s: api.get_kline_serial(s, 86400, data_length=30) for s in contracts}

        instruments: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        for s in contracts:
            q = quotes[s]
            ex, ins = s.split(".", 1)
            expire_sec = getattr(q, "expire_datetime", None)
            expire_ns = int(expire_sec) * 1_000_000_000 if isinstance(expire_sec, (int, float)) and expire_sec > 0 else None
            instruments.append({
                "symbol": s,
                "trading_day": trading_day,
                "exchange_id": ex,
                "instrument_id": ins,
                "price_tick": normalize_decimal_text(getattr(q, "price_tick", None), positive=True) or "1",
                "volume_multiple": normalize_decimal_text(getattr(q, "volume_multiple", None), positive=True) or "1",
                "upper_limit": normalize_decimal_text(getattr(q, "upper_limit", None), positive=True),
                "lower_limit": normalize_decimal_text(getattr(q, "lower_limit", None), positive=True),
                "pre_close": normalize_decimal_text(getattr(q, "pre_close", None), positive=True),
                "pre_settlement": normalize_decimal_text(getattr(q, "pre_settlement", None), positive=True),
                "expire_datetime_ns": expire_ns,
            })
            sessions.extend(
                build_sessions_from_trading_time(
                    s,
                    trading_day,
                    getattr(q, "trading_time", None) or {},
                    night_date=night_date,
                )
            )

        inst_map = {r["symbol"]: r for r in instruments}
        day_start_ns = min(s["open_ns"] for s in sessions)
        day_end_ns = max(s["close_ns"] for s in sessions)
        stop_wall_ns = day_end_ns + stop_after_close_seconds * 1_000_000_000
        started_mono = time.monotonic()

        last_ids: dict[str, int | None] = {s: None for s in contracts}
        recorded_ticks: list[dict[str, Any]] = []
        batch_seq = 0

        with staging_jsonl.open("w", encoding="utf-8") as stream:
            while True:
                now_ns = time.time_ns()
                if now_ns >= stop_wall_ns:
                    break
                if max_duration_seconds is not None and (time.monotonic() - started_mono) >= max_duration_seconds:
                    break
                updated = api.wait_update(deadline=time.time() + 1.0)
                if not updated:
                    continue
                batch_seq += 1
                recv_ns = time.time_ns()
                batch_id = f"live-{batch_seq:08d}"
                for s in contracts:
                    if not api.is_changing(tick_dfs[s]):
                        continue
                    new_rows, new_max = _extract_new_ticks(
                        s,
                        tick_dfs[s],
                        last_ids[s],
                        received_at_ns=recv_ns,
                        batch_id=batch_id,
                        inst=inst_map[s],
                    )
                    last_ids[s] = new_max
                    for row in new_rows:
                        ts_ns = int(row["source_timestamp_ns"])
                        if ts_ns < day_start_ns - 300 * 1_000_000_000 or ts_ns > day_end_ns + 60 * 1_000_000_000:
                            continue
                        recorded_ticks.append(row)
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()

        # Refresh klines once recording window completes
        raw_klines: list[dict[str, Any]] = []
        for s in contracts:
            for dur, df in ((60, k1m_dfs[s]), (86400, k1d_dfs[s])):
                valid = df.dropna(subset=["datetime"])
                valid = valid[valid["datetime"] > 0]
                for row in valid.to_dict("records"):
                    raw_klines.append({
                        "symbol": s,
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

        metadata = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": f"ot-market-{trading_day}-4contracts-live",
            "trading_day": trading_day,
            "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": {
                "provider": "TqSdk",
                "sdk_version": tqsdk_version,
                "method": "live_get_tick_serial+wait_update",
                "account_tier": "ORDINARY",
                "raw_tick_count": len(recorded_ticks),
                "raw_ticks_sha256": hash_records(recorded_ticks),
            },
            "coverage_scope": "FULL_TRADING_DAY" if time.time_ns() >= stop_wall_ns else "PARTIAL_LIVE_CAPTURE",
            "time_semantics": {
                "source_timestamp_ns": "UTC epoch nanoseconds from exchange/TqSdk tick",
                "received_at_ns": "Client UTC epoch nanoseconds via time.time_ns() on wait_update return",
                "tie_break_order": "(source_timestamp_ns, source_sequence, symbol, input_order)",
            },
            "known_gaps": [],
            "license_notice": "QA-only live recorded market fixture; contains zero user credentials, orders, or account ledgers.",
        }
        return write_market_fixture(
            db_path=db_path,
            manifest_path=manifest_path,
            dataset_id=str(metadata["dataset_id"]),
            trading_day=trading_day,
            instruments=instruments,
            sessions=sessions,
            raw_ticks=recorded_ticks,
            raw_klines=raw_klines,
            metadata=metadata,
        )
    finally:
        api.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live record 4-contract TqSdk ticks into QA SQLite fixture.")
    parser.add_argument("--trading-day", required=True, help="Target cabinet trading day YYYY-MM-DD")
    parser.add_argument("--night-date", default=None, help="Prior night calendar date YYYY-MM-DD")
    parser.add_argument("--contracts", nargs="+", default=list(DEFAULT_CONTRACTS))
    parser.add_argument("--staging", type=Path, default=ROOT / "output" / "qa_recorder" / "ticks.jsonl")
    parser.add_argument("--db", type=Path, default=ROOT / "qa" / "fixtures" / "market" / "market_fixture.sqlite3")
    parser.add_argument("--manifest", type=Path, default=ROOT / "qa" / "fixtures" / "market" / "manifest.json")
    parser.add_argument("--max-duration-seconds", type=int, default=None, help="Optional cap for bounded smoke recording")
    args = parser.parse_args(argv)

    manifest = record_trading_day(
        contracts=tuple(args.contracts),
        trading_day=args.trading_day,
        night_date=args.night_date,
        staging_jsonl=args.staging,
        db_path=args.db,
        manifest_path=args.manifest,
        max_duration_seconds=args.max_duration_seconds,
    )
    print(json.dumps({
        "dataset_id": manifest["dataset_id"],
        "coverage_scope": manifest["coverage_scope"],
        "database_sha256": manifest["database_sha256"],
        "table_counts": manifest["table_counts"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
