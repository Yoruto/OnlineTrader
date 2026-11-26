"""Import live-recorded JSONL market data (meta, ticks) into QA SQLite fixtures."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
for entry in (str(ROOT), str(SRC_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qa.market_data.builder import (
    build_sessions_from_trading_time,
    prior_weekday,
    write_market_fixture,
)
from qa.market_data.schema import SCHEMA_VERSION, normalize_decimal_text, normalize_int
from qa.market_data.validate import compute_sha256


def _normalize_hhmmss_for_builder(text: str) -> str:
    """Convert TqSdk >=24h night session times (e.g. '26:30:00') into 0..23h format."""
    parts = [int(x) for x in text.strip().split(":")]
    hour = parts[0]
    minute = parts[1]
    second = parts[2] if len(parts) > 2 else 0
    if hour >= 24:
        hour -= 24
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _normalize_trading_time(trading_time: dict[str, Any]) -> dict[str, list[list[str]]]:
    normalized: dict[str, list[list[str]]] = {}
    for kind in ("night", "day"):
        entries = trading_time.get(kind) or []
        normalized[kind] = [
            [_normalize_hhmmss_for_builder(start), _normalize_hhmmss_for_builder(end)]
            for start, end in entries
        ]
    return normalized


def _resolve_night_date(trading_day: str, calendar_record: dict[str, Any] | None) -> str:
    """Return the most recent trading date strictly before trading_day."""
    if calendar_record:
        candidates = [
            str(entry["date"])
            for entry in calendar_record.get("dates") or ()
            if entry.get("trading") and str(entry.get("date", "")) < trading_day
        ]
        if candidates:
            return max(candidates)
    return prior_weekday(trading_day)


def _build_instrument_row(symbol: str, trading_day: str, meta: dict[str, Any]) -> dict[str, Any]:
    if "." in symbol:
        ex, ins = symbol.split(".", 1)
    else:
        ex = str(meta.get("exchange_id") or "")
        ins = str(meta.get("instrument_id") or symbol)
        if ins.startswith(f"{ex}."):
            ins = ins[len(ex) + 1 :]

    expire_sec = meta.get("expire_datetime")
    expire_ns = (
        int(expire_sec) * 1_000_000_000
        if isinstance(expire_sec, (int, float)) and expire_sec > 0
        else None
    )
    return {
        "symbol": symbol,
        "trading_day": trading_day,
        "exchange_id": str(meta.get("exchange_id") or ex),
        "instrument_id": ins,
        "price_tick": normalize_decimal_text(meta.get("price_tick"), positive=True) or "1",
        "volume_multiple": normalize_decimal_text(meta.get("volume_multiple"), positive=True) or "1",
        "upper_limit": normalize_decimal_text(meta.get("upper_limit"), positive=True),
        "lower_limit": normalize_decimal_text(meta.get("lower_limit"), positive=True),
        "pre_close": normalize_decimal_text(meta.get("pre_close"), positive=True),
        "pre_settlement": normalize_decimal_text(meta.get("pre_settlement"), positive=True),
        "expire_datetime_ns": expire_ns,
    }


def _load_meta_jsonl(
    meta_path: Path,
    trading_day: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]], int]:
    """Stream meta.jsonl to extract latest instrument_meta, trading_calendar, and kline_bars."""
    inst_meta_by_symbol: dict[str, dict[str, Any]] = {}
    fallback_meta_by_symbol: dict[str, dict[str, Any]] = {}
    last_calendar: dict[str, Any] | None = None
    kline_map: dict[tuple[str, int, int], dict[str, Any]] = {}
    line_count = 0

    with meta_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            line_count += 1
            rec = json.loads(line)
            rtype = rec.get("type")
            if rtype == "instrument_meta":
                sym = rec.get("symbol")
                if not sym:
                    continue
                if rec.get("trading_day") == trading_day:
                    inst_meta_by_symbol[sym] = rec
                else:
                    fallback_meta_by_symbol[sym] = rec
            elif rtype == "trading_calendar":
                last_calendar = rec
            elif rtype == "kline_bar":
                sym = rec.get("symbol")
                dur = rec.get("duration_seconds")
                dt_ns = rec.get("datetime_ns")
                if not sym or dur is None or dt_ns is None:
                    continue
                key = (str(sym), int(dur), int(dt_ns))
                kline_map[key] = {
                    "symbol": key[0],
                    "duration_seconds": key[1],
                    "datetime_ns": key[2],
                    "kline_id": rec.get("kline_id"),
                    "open": rec.get("open"),
                    "high": rec.get("high"),
                    "low": rec.get("low"),
                    "close": rec.get("close"),
                    "volume": rec.get("volume"),
                    "open_oi": rec.get("open_oi"),
                    "close_oi": rec.get("close_oi"),
                }

    if not inst_meta_by_symbol:
        inst_meta_by_symbol = fallback_meta_by_symbol

    symbols = set(inst_meta_by_symbol)
    klines = [row for key, row in kline_map.items() if key[0] in symbols]
    return inst_meta_by_symbol, last_calendar, klines, line_count


def _build_tick_record(rec: dict[str, Any], inst: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "symbol": rec["symbol"],
        "source_timestamp_ns": int(rec["source_timestamp_ns"]),
        "source_sequence": normalize_int(rec.get("source_sequence"), minimum=0),
        "received_at_ns": normalize_int(rec.get("received_at_ns"), minimum=0),
        "batch_id": None,
        "last_price": rec.get("last_price"),
        "open_price": None,
        "highest": rec.get("highest"),
        "lowest": rec.get("lowest"),
        "average": rec.get("average"),
        "volume": rec.get("volume"),
        "amount": rec.get("amount"),
        "open_interest": rec.get("open_interest"),
        "upper_limit": inst["upper_limit"],
        "lower_limit": inst["lower_limit"],
        "pre_close": inst["pre_close"],
        "pre_settlement": inst["pre_settlement"],
    }
    for side in ("bid", "ask"):
        for lvl in range(1, 6):
            item[f"{side}_price{lvl}"] = rec.get(f"{side}_price{lvl}")
            item[f"{side}_volume{lvl}"] = rec.get(f"{side}_volume{lvl}")
    return item


def import_trading_day(
    *,
    day_dir: Path,
    trading_day: str,
    out_dir: Path,
    coverage_scope_override: str | None = None,
) -> dict[str, Any]:
    meta_path = day_dir / f"{trading_day}.meta.jsonl"
    ticks_path = day_dir / f"{trading_day}.ticks.jsonl"
    quotes_path = day_dir / f"{trading_day}.quotes.jsonl"

    if not meta_path.is_file():
        raise FileNotFoundError(f"Missing meta.jsonl for {trading_day}: {meta_path}")
    if not ticks_path.is_file():
        raise FileNotFoundError(f"Missing ticks.jsonl for {trading_day}: {ticks_path}")

    inst_meta_by_symbol, calendar_rec, raw_klines, meta_line_count = _load_meta_jsonl(meta_path, trading_day)
    if not inst_meta_by_symbol:
        raise RuntimeError(f"No instrument_meta records found in {meta_path} for {trading_day}")

    night_date = _resolve_night_date(trading_day, calendar_rec)
    instruments: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for sym in sorted(inst_meta_by_symbol):
        meta_rec = inst_meta_by_symbol[sym]
        instruments.append(_build_instrument_row(sym, trading_day, meta_rec))
        sessions.extend(
            build_sessions_from_trading_time(
                sym,
                trading_day,
                _normalize_trading_time(meta_rec.get("trading_time") or {}),
                night_date=night_date,
            )
        )

    if not sessions:
        raise RuntimeError(f"No trading_sessions built from {meta_path} for {trading_day}")

    inst_map = {row["symbol"]: row for row in instruments}
    day_start_ns = min(s["open_ns"] for s in sessions)
    day_end_ns = max(s["close_ns"] for s in sessions)
    window_start_ns = day_start_ns - 300 * 1_000_000_000
    window_end_ns = day_end_ns + 60 * 1_000_000_000

    raw_tick_count = 0
    dropped_duplicate = 0
    dropped_out_of_window = 0
    dropped_unknown_symbol = 0
    kept_ticks: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int | None, int]] = set()
    max_kept_ts_ns = 0

    with ticks_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") != "tick":
                continue
            raw_tick_count += 1
            sym = rec.get("symbol")
            if sym not in inst_map:
                dropped_unknown_symbol += 1
                continue
            ts_raw = rec.get("source_timestamp_ns")
            if ts_raw is None or int(ts_raw) <= 0:
                dropped_out_of_window += 1
                continue
            ts_ns = int(ts_raw)
            if ts_ns < window_start_ns or ts_ns > window_end_ns:
                dropped_out_of_window += 1
                continue
            seq_val = normalize_int(rec.get("source_sequence"), minimum=0)
            dedup_key = (str(sym), seq_val, ts_ns)
            if dedup_key in seen_keys:
                dropped_duplicate += 1
                continue
            seen_keys.add(dedup_key)
            if ts_ns > max_kept_ts_ns:
                max_kept_ts_ns = ts_ns
            kept_ticks.append(_build_tick_record(rec, inst_map[sym]))

    if coverage_scope_override:
        coverage_scope = coverage_scope_override
    elif kept_ticks and max_kept_ts_ns >= (day_end_ns - 120 * 1_000_000_000):
        coverage_scope = "FULL_TRADING_DAY"
    else:
        coverage_scope = "PARTIAL_LIVE_CAPTURE"

    input_files: list[dict[str, Any]] = [
        {
            "file": meta_path.name,
            "size_bytes": meta_path.stat().st_size,
            "sha256": compute_sha256(meta_path),
            "line_count": meta_line_count,
        },
        {
            "file": ticks_path.name,
            "size_bytes": ticks_path.stat().st_size,
            "sha256": compute_sha256(ticks_path),
            "raw_tick_count": raw_tick_count,
        },
    ]
    if quotes_path.is_file():
        input_files.append({
            "file": quotes_path.name,
            "size_bytes": quotes_path.stat().st_size,
            "sha256": compute_sha256(quotes_path),
            "imported": False,
        })

    known_gaps = [
        "Quote snapshot files (*.quotes.jsonl) are not part of MARKET_REPLAY_FIXTURE/1 and were not imported."
    ]
    if coverage_scope != "FULL_TRADING_DAY":
        known_gaps.append(
            f"Partial live capture for {trading_day}: last kept tick precedes final session close by more than 120s."
        )

    dataset_id = f"ot-market-{trading_day}-{len(instruments)}contracts-recorded"
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "trading_day": trading_day,
        "night_date": night_date,
        "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "provider": "TqSdk live recorder (user desktop data)",
            "method": "import_recording",
            "raw_tick_count": raw_tick_count,
            "kept_tick_count": len(kept_ticks),
            "dropped_duplicate": dropped_duplicate,
            "dropped_out_of_window": dropped_out_of_window,
            "dropped_unknown_symbol": dropped_unknown_symbol,
            "input_files": input_files,
        },
        "coverage_scope": coverage_scope,
        "time_semantics": {
            "source_timestamp_ns": "UTC epoch nanoseconds from exchange/TqSdk tick",
            "received_at_ns": "Client UTC epoch nanoseconds recorded on live tick receipt",
            "tie_break_order": "(source_timestamp_ns, source_sequence, symbol, input_order)",
        },
        "known_gaps": known_gaps,
        "license_notice": "QA-only live recorded market fixture; contains zero user credentials, orders, or account ledgers.",
    }

    db_path = out_dir / "market_fixture.sqlite3"
    manifest_path = out_dir / "manifest.json"
    return write_market_fixture(
        db_path=db_path,
        manifest_path=manifest_path,
        dataset_id=dataset_id,
        trading_day=trading_day,
        instruments=instruments,
        sessions=sessions,
        raw_ticks=kept_ticks,
        raw_klines=raw_klines,
        metadata=metadata,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert recorded JSONL market data into QA SQLite fixtures.")
    parser.add_argument("--source-dir", type=Path, required=True, help="Root directory containing YYYY-MM-DD folders")
    parser.add_argument("--trading-day", nargs="+", required=True, help="One or more trading days (YYYY-MM-DD)")
    parser.add_argument(
        "--out-root",
        type=Path,
        default=ROOT / "qa" / "fixtures" / "market" / "recorded",
        help="Output root directory (creates <out-root>/<trading_day>/)",
    )
    parser.add_argument("--coverage-scope", default=None, help="Optional coverage_scope override")
    args = parser.parse_args(argv)

    source_dir = args.source_dir.resolve()
    out_root = args.out_root.resolve()
    summaries: list[dict[str, Any]] = []
    for day in args.trading_day:
        manifest = import_trading_day(
            day_dir=source_dir / day,
            trading_day=day,
            out_dir=out_root / day,
            coverage_scope_override=args.coverage_scope,
        )
        summaries.append({
            "trading_day": day,
            "dataset_id": manifest["dataset_id"],
            "coverage_scope": manifest["coverage_scope"],
            "database_file": str((out_root / day / manifest["database_file"]).resolve()),
            "database_size_bytes": manifest["database_size_bytes"],
            "database_sha256": manifest["database_sha256"],
            "table_counts": manifest["table_counts"],
            "kline_counts": manifest["kline_counts"],
            "warnings_count": len(manifest.get("warnings") or []),
            "source": manifest["source"],
        })

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
