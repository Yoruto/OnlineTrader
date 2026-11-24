"""Deterministic single-transaction SQLite builder and manifest writer."""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from qa.market_data.schema import (
    BEIJING_TZ,
    SCHEMA_VERSION,
    create_schema,
    normalize_decimal_text,
    normalize_depth_level,
    normalize_int,
    ns_to_utc_iso,
)
from qa.market_data.validate import compute_sha256, verify_fixture


def prior_weekday(trading_day: str) -> str:
    """Return previous Monday-Friday calendar date (explicit override recommended for holidays)."""
    day = date.fromisoformat(trading_day)
    offset = 3 if day.weekday() == 0 else 1
    return (day - timedelta(days=offset)).isoformat()


def _parse_hhmmss(text: str) -> dtime:
    parts = [int(x) for x in text.strip().split(":")]
    return dtime(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)


def _dt_cst_ns(day_str: str, tod: dtime) -> int:
    dt = datetime.combine(date.fromisoformat(day_str), tod, tzinfo=BEIJING_TZ)
    return int(dt.timestamp()) * 1_000_000_000


def build_sessions_from_trading_time(
    symbol: str,
    trading_day: str,
    trading_time: dict[str, list[list[str]]],
    *,
    night_date: str | None = None,
) -> list[dict[str, Any]]:
    """Expand TqSdk quote.trading_time into explicit UTC trading_sessions rows."""
    night_day = night_date or prior_weekday(trading_day)
    sessions: list[dict[str, Any]] = []
    raw_entries: list[tuple[str, str, str, str]] = []

    for start_str, end_str in trading_time.get("night") or ():
        raw_entries.append(("night", night_day, start_str, end_str))
    for start_str, end_str in trading_time.get("day") or ():
        raw_entries.append(("day", trading_day, start_str, end_str))

    for idx, (kind, base_day, start_str, end_str) in enumerate(raw_entries):
        start_t = _parse_hhmmss(start_str)
        end_t = _parse_hhmmss(end_str)
        open_ns = _dt_cst_ns(base_day, start_t)
        close_day = base_day
        if end_t <= start_t:
            close_day = (date.fromisoformat(base_day) + timedelta(days=1)).isoformat()
        close_ns = _dt_cst_ns(close_day, end_t)
        sessions.append({
            "symbol": symbol,
            "trading_day": trading_day,
            "session_id": f"{trading_day}-{kind}-{idx}",
            "session_kind": kind,
            "open_utc": ns_to_utc_iso(open_ns),
            "close_utc": ns_to_utc_iso(close_ns),
            "open_ns": open_ns,
            "close_ns": close_ns,
            "is_opening_session": 1 if idx == 0 else 0,
            "force_close_at_end": 1 if kind == "day" and end_t >= dtime(15, 0) else 0,
        })
    return sessions


def _tick_tuple_from_raw(raw: dict[str, Any], inst: dict[str, Any], trading_day: str) -> tuple[Any, ...]:
    depth_vals: list[Any] = []
    for side in ("bid", "ask"):
        for lvl in range(1, 6):
            p, v = normalize_depth_level(raw.get(f"{side}_price{lvl}"), raw.get(f"{side}_volume{lvl}"))
            depth_vals.extend((p, v))

    return (
        int(raw["sequence"]),
        raw["symbol"],
        trading_day,
        int(raw["source_timestamp_ns"]),
        raw.get("source_sequence"),
        raw.get("received_at_ns"),
        raw.get("batch_id"),
        normalize_decimal_text(raw.get("last_price"), positive=True),
        normalize_decimal_text(raw.get("open_price"), positive=True),
        normalize_decimal_text(raw.get("highest"), positive=True),
        normalize_decimal_text(raw.get("lowest"), positive=True),
        normalize_decimal_text(raw.get("average"), positive=True),
        normalize_int(raw.get("volume"), minimum=0),
        normalize_decimal_text(raw.get("amount"), positive=False),
        normalize_int(raw.get("open_interest"), minimum=0),
        normalize_decimal_text(raw.get("upper_limit"), positive=True) or inst.get("upper_limit"),
        normalize_decimal_text(raw.get("lower_limit"), positive=True) or inst.get("lower_limit"),
        normalize_decimal_text(raw.get("pre_close"), positive=True) or inst.get("pre_close"),
        normalize_decimal_text(raw.get("pre_settlement"), positive=True) or inst.get("pre_settlement"),
        *depth_vals,
    )


def write_market_fixture(
    *,
    db_path: Path,
    manifest_path: Path,
    dataset_id: str,
    trading_day: str,
    instruments: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    raw_ticks: list[dict[str, Any]],
    raw_klines: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Deterministically sort ticks, write temporary SQLite DB, validate, and emit manifest."""
    db_path = db_path.resolve()
    manifest_path = manifest_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    inst_by_symbol = {row["symbol"]: row for row in instruments}

    # Deterministic sort: source_timestamp_ns, source_sequence (or 0), symbol, raw_order
    indexed_ticks = list(enumerate(raw_ticks))
    indexed_ticks.sort(
        key=lambda pair: (
            int(pair[1]["source_timestamp_ns"]),
            int(pair[1]["source_sequence"]) if pair[1].get("source_sequence") is not None else 0,
            str(pair[1]["symbol"]),
            pair[0],
        )
    )
    ordered_rows = []
    for seq, (_, row) in enumerate(indexed_ticks, start=1):
        item = dict(row)
        item["sequence"] = seq
        ordered_rows.append(_tick_tuple_from_raw(item, inst_by_symbol[item["symbol"]], trading_day))

    # Deduplicate and sort klines by (symbol, duration_seconds, datetime_ns)
    kline_map: dict[tuple[str, int, int], tuple[Any, ...]] = {}
    for row in raw_klines:
        o = normalize_decimal_text(row.get("open"), positive=True)
        h = normalize_decimal_text(row.get("high"), positive=True)
        l = normalize_decimal_text(row.get("low"), positive=True)
        c = normalize_decimal_text(row.get("close"), positive=True)
        v = normalize_int(row.get("volume"), minimum=0)
        if None in (o, h, l, c, v):
            continue
        key = (str(row["symbol"]), int(row["duration_seconds"]), int(row["datetime_ns"]))
        kline_map[key] = (
            key[0],
            key[1],
            key[2],
            normalize_int(row.get("kline_id"), minimum=0),
            o,
            h,
            l,
            c,
            v,
            normalize_int(row.get("open_oi"), minimum=0),
            normalize_int(row.get("close_oi"), minimum=0),
        )
    ordered_klines = [kline_map[k] for k in sorted(kline_map)]

    tmp_db = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    conn = sqlite3.connect(str(tmp_db))
    try:
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.execute("PRAGMA page_size = 4096;")
        create_schema(conn)
        with conn:
            for key, value in sorted(metadata.items()):
                encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
                conn.execute(
                    "INSERT INTO dataset_metadata(key, value) VALUES (?, ?)",
                    (str(key), encoded),
                )
            conn.executemany(
                "INSERT INTO instruments(symbol, trading_day, exchange_id, instrument_id, price_tick, "
                "volume_multiple, upper_limit, lower_limit, pre_close, pre_settlement, expire_datetime_ns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["symbol"],
                        r["trading_day"],
                        r["exchange_id"],
                        r["instrument_id"],
                        r["price_tick"],
                        r["volume_multiple"],
                        r.get("upper_limit"),
                        r.get("lower_limit"),
                        r.get("pre_close"),
                        r.get("pre_settlement"),
                        r.get("expire_datetime_ns"),
                    )
                    for r in sorted(instruments, key=lambda x: x["symbol"])
                ],
            )
            conn.executemany(
                "INSERT INTO trading_sessions(symbol, trading_day, session_id, session_kind, "
                "open_utc, close_utc, open_ns, close_ns, is_opening_session, force_close_at_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        s["symbol"],
                        s["trading_day"],
                        s["session_id"],
                        s["session_kind"],
                        s["open_utc"],
                        s["close_utc"],
                        int(s["open_ns"]),
                        int(s["close_ns"]),
                        int(s["is_opening_session"]),
                        int(s["force_close_at_end"]),
                    )
                    for s in sessions
                ],
            )
            tick_columns = [
                "sequence", "symbol", "trading_day", "source_timestamp_ns",
                "source_sequence", "received_at_ns", "batch_id",
                "last_price", "open_price", "highest", "lowest", "average",
                "volume", "amount", "open_interest",
                "upper_limit", "lower_limit", "pre_close", "pre_settlement",
            ]
            for side in ("bid", "ask"):
                for lvl in range(1, 6):
                    tick_columns.extend((f"{side}_price{lvl}", f"{side}_volume{lvl}"))
            placeholders = ", ".join(["?"] * len(tick_columns))
            col_list = ", ".join(tick_columns)
            conn.executemany(
                f"INSERT INTO market_ticks({col_list}) VALUES ({placeholders})",
                ordered_rows,
            )
            conn.executemany(
                "INSERT INTO market_klines(symbol, duration_seconds, datetime_ns, kline_id, "
                "open, high, low, close, volume, open_oi, close_oi) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ordered_klines,
            )
        conn.execute("VACUUM;")
    finally:
        conn.close()

    validation = verify_fixture(tmp_db)
    if not validation["ok"]:
        raise RuntimeError(f"Fixture validation failed before commit: {validation['errors']}")

    tmp_db.replace(db_path)
    final_sha = compute_sha256(db_path)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "trading_day": trading_day,
        "contracts": sorted(inst_by_symbol),
        "database_file": db_path.name,
        "database_size_bytes": db_path.stat().st_size,
        "database_sha256": final_sha,
        "source": metadata.get("source"),
        "coverage_scope": metadata.get("coverage_scope"),
        "time_semantics": metadata.get("time_semantics"),
        "known_gaps": metadata.get("known_gaps"),
        "license_notice": metadata.get("license_notice"),
        "table_counts": validation["table_counts"],
        "kline_counts": validation["kline_counts"],
        "depth_report": validation["depth_report"],
        "integrity_report": validation["integrity_report"],
        "warnings": validation["warnings"],
    }
    manifest_tmp = manifest_path.with_suffix(".json.tmp")
    manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_tmp.replace(manifest_path)

    # Re-verify final database against final manifest
    final_check = verify_fixture(db_path, manifest_path=manifest_path)
    if not final_check["ok"]:
        raise RuntimeError(f"Final manifest verification failed: {final_check['errors']}")
    return manifest


def hash_records(records: list[dict[str, Any]]) -> str:
    """Compute a deterministic SHA-256 hash over raw input records for provenance."""
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
