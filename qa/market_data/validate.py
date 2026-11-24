"""Read-only SQLite market fixture validator, depth analyzer, and CLI."""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
for entry in (str(ROOT), str(SRC_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from online_trader.domain.values import decimal_value, order_price
from qa.market_data.schema import SCHEMA_VERSION, ns_to_beijing_iso, ns_to_utc_iso


def compute_sha256(path: str | Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Market fixture database not found: {resolved}")
    uri = resolved.as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _classify_level(total_rows: int, valid_pairs: int, zero_vol: int, level: int) -> str:
    if total_rows == 0:
        return "NO_ROWS"
    if valid_pairs == 0:
        return "NOT_PROVIDED_OR_EMPTY" if level > 1 else "EMPTY_OR_UNAVAILABLE"
    if valid_pairs == total_rows:
        return "COMPLETE"
    if zero_vol > 0 and valid_pairs + zero_vol == total_rows:
        return "PRESENT_WITH_ZERO_VOLUME_QUOTES"
    return "PARTIAL"


def verify_fixture(
    db_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
    max_gap_report_seconds: float = 30.0,
) -> dict[str, Any]:
    """Validate SQLite fixture integrity, price alignment, depth coverage, and optional manifest."""
    path = Path(db_path).resolve()
    actual_sha256 = compute_sha256(path)
    errors: list[str] = []
    warnings: list[str] = []

    manifest_data: dict[str, Any] | None = None
    if manifest_path is not None:
        mpath = Path(manifest_path).resolve()
        manifest_data = json.loads(mpath.read_text(encoding="utf-8"))
        if manifest_data.get("database_sha256") != actual_sha256:
            errors.append(
                f"SHA-256 mismatch: manifest={manifest_data.get('database_sha256')} actual={actual_sha256}"
            )
        if manifest_data.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"Unsupported manifest schema_version: {manifest_data.get('schema_version')}")

    conn = _connect_readonly(path)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            errors.append(f"SQLite integrity_check failed: {integrity}")

        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required_tables = {
            "dataset_metadata",
            "instruments",
            "trading_sessions",
            "market_ticks",
            "market_klines",
        }
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            errors.append(f"Missing required tables: {missing_tables}")
            return {"ok": False, "database_sha256": actual_sha256, "errors": errors, "warnings": warnings}

        meta = {
            row[0]: row[1]
            for row in conn.execute("SELECT key, value FROM dataset_metadata").fetchall()
        }
        if meta.get("schema_version") != SCHEMA_VERSION:
            errors.append(f"Metadata schema_version mismatch: {meta.get('schema_version')}")

        instruments = {}
        for row in conn.execute(
            "SELECT symbol, trading_day, price_tick, volume_multiple, upper_limit, lower_limit, pre_close, pre_settlement "
            "FROM instruments ORDER BY symbol"
        ).fetchall():
            instruments[row[0]] = {
                "trading_day": row[1],
                "price_tick": row[2],
                "volume_multiple": row[3],
                "upper_limit": row[4],
                "lower_limit": row[5],
                "pre_close": row[6],
                "pre_settlement": row[7],
            }

        sessions_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT symbol, trading_day, session_id, session_kind, open_utc, close_utc, open_ns, close_ns, "
            "is_opening_session, force_close_at_end FROM trading_sessions ORDER BY symbol, open_ns"
        ).fetchall():
            sessions_by_symbol.setdefault(row[0], []).append({
                "trading_day": row[1],
                "session_id": row[2],
                "session_kind": row[3],
                "open_utc": row[4],
                "close_utc": row[5],
                "open_ns": int(row[6]),
                "close_ns": int(row[7]),
                "is_opening_session": bool(row[8]),
                "force_close_at_end": bool(row[9]),
            })

        # Verify global sequence monotonicity and timestamp monotonicity
        total_ticks = conn.execute("SELECT COUNT(*) FROM market_ticks").fetchone()[0]
        seq_bounds = conn.execute("SELECT MIN(sequence), MAX(sequence) FROM market_ticks").fetchone()
        if total_ticks > 0 and (seq_bounds[0] != 1 or seq_bounds[1] != total_ticks):
            errors.append(f"Non-contiguous tick sequences: min={seq_bounds[0]}, max={seq_bounds[1]}, count={total_ticks}")

        total_klines = conn.execute("SELECT COUNT(*) FROM market_klines").fetchone()[0]

        depth_report: dict[str, Any] = {}
        integrity_report: dict[str, Any] = {}

        depth_cols = []
        for side in ("bid", "ask"):
            for lvl in range(1, 6):
                depth_cols.append(f"{side}_price{lvl}")
                depth_cols.append(f"{side}_volume{lvl}")
        select_sql = (
            "SELECT sequence, symbol, trading_day, source_timestamp_ns, source_sequence, "
            "last_price, highest, lowest, volume, upper_limit, lower_limit, "
            + ", ".join(depth_cols)
            + " FROM market_ticks WHERE symbol = ? ORDER BY sequence ASC"
        )

        gap_threshold_ns = int(max_gap_report_seconds * 1_000_000_000)

        for symbol, inst in sorted(instruments.items()):
            tick_size = inst["price_tick"]
            rows = conn.execute(select_sql, (symbol,)).fetchall()
            row_count = len(rows)

            prev_ts: int | None = None
            prev_vol: int | None = None
            duplicate_ts = 0
            out_of_order_ts = 0
            vol_regressions = 0
            price_tick_errors = 0
            crossed_book_count = 0
            limit_violations = 0
            outside_session_ticks = 0
            in_session_large_gaps: list[dict[str, Any]] = []

            side_stats = {
                side: {
                    f"L{lvl}": {
                        "field_exists": True,
                        "valid_price_count": 0,
                        "valid_volume_count": 0,
                        "valid_pair_count": 0,
                        "zero_volume_with_price_count": 0,
                        "missing_count": 0,
                    }
                    for lvl in range(1, 6)
                }
                for side in ("bid", "ask")
            }

            sessions = sessions_by_symbol.get(symbol, [])
            session_counts = {s["session_id"]: {"count": 0, "first_ns": None, "last_ns": None} for s in sessions}

            def _find_session(ts_ns: int) -> dict[str, Any] | None:
                for s in sessions:
                    if s["open_ns"] <= ts_ns <= s["close_ns"]:
                        return s
                return None

            upper_dec = Decimal(inst["upper_limit"]) if inst["upper_limit"] else None
            lower_dec = Decimal(inst["lower_limit"]) if inst["lower_limit"] else None

            for row in rows:
                seq, _, _, ts_ns, _, last_p, _, _, vol, row_upper, row_lower, *dvals = row
                if prev_ts is not None:
                    if ts_ns < prev_ts:
                        out_of_order_ts += 1
                    elif ts_ns == prev_ts:
                        duplicate_ts += 1
                prev_ts = ts_ns

                if vol is not None:
                    if prev_vol is not None and vol < prev_vol:
                        vol_regressions += 1
                    prev_vol = vol

                if last_p is not None:
                    try:
                        last_dec = order_price(last_p, tick_size)
                        hi_dec = Decimal(row_upper) if row_upper else upper_dec
                        lo_dec = Decimal(row_lower) if row_lower else lower_dec
                        if hi_dec is not None and last_dec > hi_dec:
                            limit_violations += 1
                        if lo_dec is not None and last_dec < lo_dec:
                            limit_violations += 1
                    except Exception:
                        price_tick_errors += 1

                matched_session = _find_session(ts_ns)
                if matched_session is None:
                    outside_session_ticks += 1
                else:
                    sc = session_counts[matched_session["session_id"]]
                    sc["count"] += 1
                    if sc["first_ns"] is None:
                        sc["first_ns"] = ts_ns
                    else:
                        gap = ts_ns - sc["last_ns"]
                        if gap > gap_threshold_ns:
                            in_session_large_gaps.append({
                                "session_id": matched_session["session_id"],
                                "gap_seconds": round(gap / 1e9, 3),
                                "from_cst": ns_to_beijing_iso(sc["last_ns"]),
                                "to_cst": ns_to_beijing_iso(ts_ns),
                            })
                    sc["last_ns"] = ts_ns

                # Depth fields layout: bid1..5 (idx 0..9), ask1..5 (idx 10..19)
                bid1_dec = None
                ask1_dec = None
                for s_idx, side in enumerate(("bid", "ask")):
                    for lvl in range(1, 6):
                        base = s_idx * 10 + (lvl - 1) * 2
                        p_val = dvals[base]
                        v_val = dvals[base + 1]
                        st = side_stats[side][f"L{lvl}"]
                        p_valid = False
                        if p_val is not None:
                            try:
                                dec_p = order_price(p_val, tick_size)
                                decimal_value(p_val, positive=True)
                                p_valid = True
                                st["valid_price_count"] += 1
                                if lvl == 1:
                                    if side == "bid":
                                        bid1_dec = dec_p
                                    else:
                                        ask1_dec = dec_p
                            except Exception:
                                price_tick_errors += 1
                        v_valid = v_val is not None and v_val >= 0
                        if v_valid:
                            st["valid_volume_count"] += 1
                        if p_valid and v_valid and v_val > 0:
                            st["valid_pair_count"] += 1
                        elif p_valid and v_val == 0:
                            st["zero_volume_with_price_count"] += 1
                        else:
                            st["missing_count"] += 1

                if bid1_dec is not None and ask1_dec is not None and bid1_dec > ask1_dec:
                    crossed_book_count += 1

            if out_of_order_ts:
                errors.append(f"{symbol}: {out_of_order_ts} out-of-order source_timestamp_ns rows")
            if vol_regressions:
                warnings.append(f"{symbol}: {vol_regressions} volume regressions observed")
            if price_tick_errors:
                errors.append(f"{symbol}: {price_tick_errors} price_tick alignment errors")
            if limit_violations:
                warnings.append(f"{symbol}: {limit_violations} last_price outside upper/lower limit")
            if crossed_book_count:
                warnings.append(f"{symbol}: {crossed_book_count} crossed L1 quotes (bid1 > ask1)")
            if outside_session_ticks:
                warnings.append(f"{symbol}: {outside_session_ticks} ticks outside configured trading_sessions")

            for side in ("bid", "ask"):
                for lvl in range(1, 6):
                    st = side_stats[side][f"L{lvl}"]
                    st["valid_pair_ratio"] = round(st["valid_pair_count"] / row_count, 6) if row_count else 0.0
                    st["classification"] = _classify_level(
                        row_count,
                        st["valid_pair_count"],
                        st["zero_volume_with_price_count"],
                        lvl,
                    )

            l5_available = all(
                side_stats[side][f"L{lvl}"]["valid_pair_count"] > 0
                for side in ("bid", "ask")
                for lvl in range(1, 6)
            )
            l1_available = all(
                side_stats[side]["L1"]["valid_pair_count"] > 0
                for side in ("bid", "ask")
            )
            depth_report[symbol] = {
                "row_count": row_count,
                "depth_capability": "FIVE_LEVEL" if l5_available else ("ONE_LEVEL_ONLY" if l1_available else "DEGRADED"),
                "sides": side_stats,
            }

            session_summary = {}
            for s in sessions:
                sc = session_counts[s["session_id"]]
                session_summary[s["session_id"]] = {
                    "session_kind": s["session_kind"],
                    "open_utc": s["open_utc"],
                    "close_utc": s["close_utc"],
                    "row_count": sc["count"],
                    "first_utc": ns_to_utc_iso(sc["first_ns"]) if sc["first_ns"] is not None else None,
                    "last_utc": ns_to_utc_iso(sc["last_ns"]) if sc["last_ns"] is not None else None,
                    "first_cst": ns_to_beijing_iso(sc["first_ns"]) if sc["first_ns"] is not None else None,
                    "last_cst": ns_to_beijing_iso(sc["last_ns"]) if sc["last_ns"] is not None else None,
                }

            integrity_report[symbol] = {
                "row_count": row_count,
                "first_utc": ns_to_utc_iso(rows[0][3]) if rows else None,
                "last_utc": ns_to_utc_iso(rows[-1][3]) if rows else None,
                "first_cst": ns_to_beijing_iso(rows[0][3]) if rows else None,
                "last_cst": ns_to_beijing_iso(rows[-1][3]) if rows else None,
                "duplicate_source_timestamps": duplicate_ts,
                "out_of_order_source_timestamps": out_of_order_ts,
                "volume_regressions": vol_regressions,
                "price_tick_errors": price_tick_errors,
                "crossed_l1_quotes": crossed_book_count,
                "outside_session_ticks": outside_session_ticks,
                "in_session_gaps_over_threshold": in_session_large_gaps[:20],
                "sessions": session_summary,
            }

        kline_counts: dict[str, dict[str, int]] = {}
        for symbol, dur, cnt in conn.execute(
            "SELECT symbol, duration_seconds, COUNT(*) FROM market_klines GROUP BY symbol, duration_seconds ORDER BY symbol, duration_seconds"
        ).fetchall():
            kline_counts.setdefault(symbol, {})[str(dur)] = int(cnt)

        if manifest_data is not None:
            expected_counts = manifest_data.get("table_counts") or {}
            if expected_counts.get("market_ticks") != total_ticks:
                errors.append(
                    f"market_ticks count mismatch: manifest={expected_counts.get('market_ticks')} actual={total_ticks}"
                )
            if expected_counts.get("market_klines") != total_klines:
                errors.append(
                    f"market_klines count mismatch: manifest={expected_counts.get('market_klines')} actual={total_klines}"
                )

        return {
            "ok": not errors,
            "database_path": str(path),
            "database_sha256": actual_sha256,
            "schema_version": meta.get("schema_version"),
            "table_counts": {
                "market_ticks": total_ticks,
                "market_klines": total_klines,
                "instruments": len(instruments),
                "trading_sessions": sum(len(v) for v in sessions_by_symbol.values()),
            },
            "kline_counts": kline_counts,
            "depth_report": depth_report,
            "integrity_report": integrity_report,
            "errors": errors,
            "warnings": warnings,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify QA SQLite market replay fixture.")
    parser.add_argument("db", type=Path, help="Path to SQLite fixture database")
    parser.add_argument("--manifest", type=Path, default=None, help="Optional manifest.json to verify against")
    args = parser.parse_args(argv)
    report = verify_fixture(args.db, manifest_path=args.manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
