"""Offline FakeMarket + FakeBroker replay scenario using recorded 2026-11-25 SQLite fixture."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.domain.values import decimal_text
from qa.e2e.assertions import assert_no_active_orders
from qa.e2e.scenario import TradingScenario

FIXTURE_DB = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "market"
    / "recorded"
    / "2026-11-25"
    / "market_fixture.sqlite3"
)
TRADING_DAY = "2026-11-25"
DEFAULT_CONTRACTS = ("DCE.p2701", "SHFE.ag2702", "SHFE.bu2701")
# Morning continuous session: 2026-11-25 09:00:00 .. 11:30:00 CST (01:00:00Z .. 03:30:00Z)
WINDOW_START_UTC = datetime(2026, 11, 25, 1, 0, 0, tzinfo=timezone.utc)
WINDOW_END_UTC = datetime(2026, 11, 25, 3, 30, 0, tzinfo=timezone.utc)
BUCKET_SECONDS = 5
BEIJING_TZ = timezone(timedelta(hours=8))


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canon(value: Any) -> str:
    return decimal_text(Decimal(str(value)))


def _load_replay_data(
    db_path: Path,
    candidate_contracts: tuple[str, ...],
    *,
    bucket_seconds: int = BUCKET_SECONDS,
) -> tuple[
    tuple[str, ...],
    dict[str, dict[str, Any]],
    list[tuple[datetime, dict[str, dict[str, Any]]]],
    dict[str, dict[str, int]],
]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        instruments: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT symbol, price_tick, volume_multiple, upper_limit, lower_limit "
            "FROM instruments WHERE symbol IN ({})".format(",".join("?" for _ in candidate_contracts)),
            candidate_contracts,
        ).fetchall():
            instruments[row[0]] = {
                "symbol": row[0],
                "price_tick": Decimal(row[1]),
                "volume_multiple": Decimal(row[2]),
                "upper_limit": _canon(row[3]),
                "lower_limit": _canon(row[4]),
            }

        start_ns = int(WINDOW_START_UTC.timestamp()) * 1_000_000_000
        end_ns = int(WINDOW_END_UTC.timestamp()) * 1_000_000_000
        first_15m_end_ns = start_ns + 15 * 60 * 1_000_000_000

        selected: list[str] = []
        raw_by_contract: dict[str, list[tuple[int, str, str | None, str | None, str | None, str | None]]] = {}
        for sym in candidate_contracts:
            if sym not in instruments:
                continue
            rows = conn.execute(
                "SELECT source_timestamp_ns, last_price, bid_price1, ask_price1, lowest, highest "
                "FROM market_ticks "
                "WHERE symbol = ? AND source_timestamp_ns >= ? AND source_timestamp_ns <= ? "
                "AND last_price IS NOT NULL "
                "ORDER BY source_timestamp_ns ASC, sequence ASC",
                (sym, start_ns, end_ns),
            ).fetchall()
            if len(rows) >= 1000:
                selected.append(sym)
                raw_by_contract[sym] = rows
    finally:
        conn.close()

    if not selected:
        raise RuntimeError(f"No liquid contracts (>=1000 ticks) found in {db_path}")

    # Compute first-15m range and derive per-contract offsets
    offsets_by_contract: dict[str, dict[str, int]] = {}
    for sym in selected:
        tick_size = instruments[sym]["price_tick"]
        first15_prices = [
            Decimal(r[1]) for r in raw_by_contract[sym] if r[0] <= first_15m_end_ns
        ]
        first15_lows = [
            Decimal(r[4]) for r in raw_by_contract[sym] if r[0] <= first_15m_end_ns and r[4] is not None
        ]
        first15_highs = [
            Decimal(r[5]) for r in raw_by_contract[sym] if r[0] <= first_15m_end_ns and r[5] is not None
        ]
        if first15_lows and first15_highs:
            r_price = max(first15_highs) - min(first15_lows)
        elif first15_prices:
            r_price = max(first15_prices) - min(first15_prices)
        else:
            r_price = tick_size * 10
        r_ticks = max(1, int((r_price / tick_size).to_integral_value()))
        stop_ticks = max(3, round(0.3 * r_ticks))
        open_ticks = max(stop_ticks, math.ceil((r_ticks + stop_ticks) / 2) + 1)
        profit_ticks = 2 * stop_ticks
        offsets_by_contract[sym] = {
            "range_ticks": r_ticks,
            "open_offset": int(Decimal(open_ticks) * tick_size),
            "stop_offset": int(Decimal(stop_ticks) * tick_size),
            "profit_offset": int(Decimal(profit_ticks) * tick_size),
        }

    # Downsample into fixed bucket_seconds rounds
    bucket_ns = bucket_seconds * 1_000_000_000
    buckets: dict[int, dict[str, dict[str, Any]]] = {}
    running_low: dict[str, Decimal] = {}
    running_high: dict[str, Decimal] = {}

    for sym in selected:
        for ts_ns, last_s, bid1_s, ask1_s, lowest_s, highest_s in raw_by_contract[sym]:
            last_d = Decimal(last_s)
            low_d = Decimal(lowest_s) if lowest_s is not None else last_d
            high_d = Decimal(highest_s) if highest_s is not None else last_d
            running_low[sym] = min(running_low[sym], low_d, last_d) if sym in running_low else min(low_d, last_d)
            running_high[sym] = max(running_high[sym], high_d, last_d) if sym in running_high else max(high_d, last_d)

            offset_ns = ts_ns - start_ns
            b_idx = max(1, (offset_ns + bucket_ns - 1) // bucket_ns)
            bucket_end_ns = min(end_ns, start_ns + b_idx * bucket_ns)
            bucket_dt = datetime.fromtimestamp(bucket_end_ns / 1_000_000_000, tz=timezone.utc)
            cst_dt = bucket_dt.astimezone(BEIJING_TZ)

            tick_dt = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
            buckets.setdefault(bucket_end_ns, {})[sym] = {
                "last": _canon(last_s),
                "bid1": _canon(bid1_s) if bid1_s is not None else _canon(last_s),
                "ask1": _canon(ask1_s) if ask1_s is not None else _canon(last_s),
                "day_low": decimal_text(running_low[sym]),
                "day_high": decimal_text(running_high[sym]),
                "candle_id": f"bar-{cst_dt.strftime('%H%M')}",
                "native_time": _iso_utc(tick_dt),
            }

    ordered_rounds: list[tuple[datetime, dict[str, dict[str, Any]]]] = []
    for bucket_end_ns in sorted(buckets):
        dt = datetime.fromtimestamp(bucket_end_ns / 1_000_000_000, tz=timezone.utc)
        ordered_rounds.append((dt, buckets[bucket_end_ns]))

    return tuple(selected), instruments, ordered_rounds, offsets_by_contract


def _build_replay_profile(
    contracts: tuple[str, ...],
    offsets_by_contract: dict[str, dict[str, int]],
    *,
    max_quote_age_seconds: int = 30,
    order_volume: int = 1,
) -> dict[str, object]:
    settings = load_confirmed_baseline()[0].to_settings()
    template = settings["symbols"][0]
    settings["symbols"] = [
        template | {
            "code": code,
            "open_offset": offsets_by_contract[code]["open_offset"],
            "stop_offset": offsets_by_contract[code]["stop_offset"],
            "profit_offset": offsets_by_contract[code]["profit_offset"],
            "order_volume": order_volume,
        }
        for code in contracts
    ]
    settings["trade"].update(
        continuation_entry=False,
        no_chase_after_act=False,
        no_open_after_open=False,
        passive_entry_first=False,
        protect_80_enabled=False,
        trailing_activate_ratio=0,
        max_quote_age_seconds=max_quote_age_seconds,
    )
    return DsvProfile.from_settings(settings).to_record()


def run(work_dir: Path, *, max_quote_age_seconds: int = 30, bucket_seconds: int = BUCKET_SECONDS) -> dict[str, object]:
    contracts, instruments, rounds, offsets_by_contract = _load_replay_data(
        FIXTURE_DB,
        DEFAULT_CONTRACTS,
        bucket_seconds=bucket_seconds,
    )
    first_dt = rounds[0][0]
    initial_utc = _iso_utc(first_dt - timedelta(seconds=10))
    profile_record = _build_replay_profile(
        contracts,
        offsets_by_contract,
        max_quote_age_seconds=max_quote_age_seconds,
        order_volume=1,
    )

    first_prices: dict[str, dict[str, str]] = {}
    for _, ticks_map in rounds:
        for sym, t in ticks_map.items():
            if sym not in first_prices:
                first_prices[sym] = t
        if len(first_prices) == len(contracts):
            break

    trade_log: list[dict[str, Any]] = []
    open_positions_cost: dict[str, tuple[str, Decimal, int]] = {}
    realized_pnl_by_contract: dict[str, Decimal] = {c: Decimal("0") for c in contracts}
    entry_count_by_contract: dict[str, int] = {c: 0 for c in contracts}
    close_count_by_contract: dict[str, int] = {c: 0 for c in contracts}

    with TradingScenario(
        work_dir,
        scenario_name="E2E-REPLAY-001-recorded-2026-11-25",
        contract=contracts[0],
        contracts=contracts,
        trading_day=TRADING_DAY,
        initial_utc=initial_utc,
        session_open=f"{TRADING_DAY}T01:00:00Z",
        session_close=f"{TRADING_DAY}T07:00:00Z",
        day_low=first_prices[contracts[0]]["day_low"],
        day_high=first_prices[contracts[0]]["day_high"],
        profile_record=profile_record,
        max_quote_age_seconds=max_quote_age_seconds,
    ) as scenario:
        assert scenario.broker is not None

        for code in contracts:
            state = scenario.market._contracts[code]
            inst = instruments[code]
            fp = first_prices[code]
            last_dec = Decimal(fp["last"])
            mult = inst["volume_multiple"]
            state.tick_size = decimal_text(inst["price_tick"])
            state.multiplier = decimal_text(mult)
            state.lower_limit = inst["lower_limit"]
            state.upper_limit = inst["upper_limit"]
            state.day_low = fp["day_low"]
            state.day_high = fp["day_high"]
            state.available = "10000000"
            state.margin_per_lot = decimal_text((last_dec * mult * Decimal("0.15")).quantize(Decimal("1")))

        scenario.trace.record(
            "COMMAND",
            operation="REPLAY_PARAMS",
            receipt_status=json.dumps(offsets_by_contract, ensure_ascii=False, sort_keys=True),
        )

        def _handle_outbound(outcome_obj) -> None:
            assert scenario.broker is not None
            wb = outcome_obj.workbench
            cycles_by_contract = {
                str(c.get("contract_id")): c for c in (wb.get("cycles") or []) if c.get("contract_id")
            }
            for ev in outcome_obj.outbound_events:
                ev_type = ev.get("event_type")
                req = ev.get("request") or {}
                cid = str(req.get("child_order_id") or "")
                code = str(req.get("contract_id") or "")
                if not cid or not code:
                    continue
                if ev_type == "OUTBOUND_SEND":
                    qty = int(req["quantity"])
                    direction = str(req["direction"])
                    offset = str(req["native_offset"])
                    limit_price = str(req["limit_price"])
                    if offset == "OPEN":
                        strat_dir = "LONG" if direction == "BUY" else "SHORT"
                        scenario.oracle.set_target_expectation(code, direction=strat_dir, quantity=qty)
                        scenario.broker.accept(cid)
                        scenario.broker.fill(cid, qty, limit_price)
                        open_positions_cost[code] = (strat_dir, Decimal(limit_price), qty)
                        entry_count_by_contract[code] += 1
                        trade_log.append({
                            "round_seq": outcome_obj.round_seq,
                            "wall_time": outcome_obj.wall_time,
                            "contract_id": code,
                            "action": "OPEN",
                            "direction": strat_dir,
                            "quantity": qty,
                            "price": limit_price,
                            "reason": "ENTRY_TRIGGER",
                        })
                    else:
                        held = open_positions_cost.get(code)
                        held_dir = held[0] if held else ("LONG" if direction == "SELL" else "SHORT")
                        held_qty = held[2] if held else qty
                        scenario.oracle.set_target_expectation(
                            code, direction=held_dir, quantity=held_qty, exit_target=0
                        )
                        scenario.broker.accept(cid)
                        scenario.broker.fill(cid, qty, limit_price)
                        exit_reason = (cycles_by_contract.get(code) or {}).get("exit_reason") or offset
                        pnl = Decimal("0")
                        if held:
                            entry_price = held[1]
                            mult = instruments[code]["volume_multiple"]
                            diff = (
                                (Decimal(limit_price) - entry_price)
                                if held_dir == "LONG"
                                else (entry_price - Decimal(limit_price))
                            )
                            pnl = diff * Decimal(qty) * mult
                            realized_pnl_by_contract[code] += pnl
                            open_positions_cost.pop(code, None)
                        close_count_by_contract[code] += 1
                        trade_log.append({
                            "round_seq": outcome_obj.round_seq,
                            "wall_time": outcome_obj.wall_time,
                            "contract_id": code,
                            "action": "CLOSE",
                            "direction": held_dir,
                            "quantity": qty,
                            "price": limit_price,
                            "reason": exit_reason,
                            "realized_pnl": decimal_text(pnl),
                        })
                elif ev_type == "OUTBOUND_CANCEL":
                    snap = scenario.broker.snapshot()
                    order = (snap.get("orders") or {}).get(cid)
                    if order and order.get("status") == "ALIVE":
                        scenario.broker.confirm_cancel(cid)

            # Clear oracle expectations once contract is confirmed flat
            snap_after = scenario.broker.snapshot()
            pos_by_c = snap_after.get("positions_by_contract") or {}
            for code in contracts:
                pos = pos_by_c.get(code) or {}
                if int(pos.get("long_qty", 0)) == 0 and int(pos.get("short_qty", 0)) == 0:
                    if code in scenario.oracle.exit_active and code not in open_positions_cost:
                        scenario.oracle.exit_active.pop(code, None)
                        scenario.oracle.expected_targets.pop(code, None)

        # Round 1: initial sync, then ARM_ENTRIES
        first_dt, first_ticks = rounds[0]
        r0 = scenario.step({"wall_time": _iso_utc(first_dt)}, ticks_by_contract=first_ticks)
        if not r0.workbench["connection"]["synchronized"]:
            raise AssertionError("Runtime did not synchronize on first replay round")
        arm = scenario.arm_strategy()
        if arm.get("status") != "SUCCEEDED":
            raise AssertionError(f"ARM_ENTRIES failed: {arm}")

        last_ticks = dict(first_ticks)
        last_dt = first_dt
        for dt, ticks_map in rounds[1:]:
            last_dt = dt
            for code, t in ticks_map.items():
                last_ticks[code] = t
            outcome = scenario.step({"wall_time": _iso_utc(dt)}, ticks_by_contract=ticks_map)
            _handle_outbound(outcome)

        # Drain up to 3 extra rounds if any broker facts or positions remain
        for extra in range(1, 4):
            snap = scenario.broker.snapshot()
            has_pos = any(
                int(p.get("long_qty", 0)) > 0 or int(p.get("short_qty", 0)) > 0
                for p in (snap.get("positions_by_contract") or {}).values()
            )
            if not has_pos and int(snap.get("pending_fact_count", 0)) == 0:
                break
            extra_dt = last_dt + timedelta(seconds=bucket_seconds * extra)
            extra_ticks = {
                code: dict(t, native_time=_iso_utc(extra_dt)) for code, t in last_ticks.items()
            }
            outcome = scenario.step({"wall_time": _iso_utc(extra_dt)}, ticks_by_contract=extra_ticks)
            _handle_outbound(outcome)

        final_snap = scenario.broker.snapshot()
        final_wb = scenario.workbench()
        assert_no_active_orders(broker_snapshot=final_snap, workbench_view=final_wb)

        summary = {
            "trading_day": TRADING_DAY,
            "fixture_db": str(FIXTURE_DB),
            "window_utc": [_iso_utc(WINDOW_START_UTC), _iso_utc(WINDOW_END_UTC)],
            "bucket_seconds": bucket_seconds,
            "rounds_replayed": len(rounds),
            "contracts": list(contracts),
            "offsets_by_contract": offsets_by_contract,
            "entry_count_by_contract": entry_count_by_contract,
            "close_count_by_contract": close_count_by_contract,
            "total_opens": sum(entry_count_by_contract.values()),
            "total_closes": sum(close_count_by_contract.values()),
            "realized_pnl_by_contract": {k: decimal_text(v) for k, v in realized_pnl_by_contract.items()},
            "total_realized_pnl": decimal_text(sum(realized_pnl_by_contract.values(), Decimal("0"))),
            "final_positions": final_snap.get("positions_by_contract"),
            "trades": trade_log,
        }
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "replay-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return scenario.finish(passed=True)
