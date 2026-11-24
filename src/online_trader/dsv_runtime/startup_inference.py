"""Strict strategy-proved Plan B startup inference for Standalone Runtime.

Pure deterministic module: no SDK, SQLite, UI, I/O or system clock access.
Infers which existing broker positions and intraday counters can be strictly
proved under the configured DSV strategy rules; any unprovable or excess
holdings resolve to target 0 so they flatten upon strategy ARM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from online_trader.domain.values import decimal_text
from online_trader.dsv_core.entry_rules import geometry_limit, trigger_prices
from online_trader.dsv_core.transition import price_value, protect


_BEIJING = timezone(timedelta(hours=8))


def _utc(value):
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _trade_time_utc(trade):
    if 'timestamp_utc' in trade and trade['timestamp_utc']:
        return _utc(trade['timestamp_utc'])
    stamp = trade.get('source_time_ns')
    if stamp is not None:
        try:
            ns = int(stamp)
            if ns > 0:
                sec, rem = divmod(ns, 1_000_000_000)
                return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
                    seconds=sec, microseconds=rem // 1000)
        except (TypeError, ValueError):
            pass
    return None


def _minute_floor(dt):
    return dt.replace(second=0, microsecond=0)


def _bar_time_utc(bar):
    if bar.get('time_utc'):
        return _utc(bar['time_utc'])
    stamp = bar.get('datetime_ns') or bar.get('datetime')
    if stamp is not None:
        try:
            ns = int(stamp)
            if ns > 0:
                sec, rem = divmod(ns, 1_000_000_000)
                return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
                    seconds=sec, microseconds=rem // 1000)
        except (TypeError, ValueError):
            pass
    return None


def _evaluate_side_plan_b(
    *,
    side: str,
    today_qty: int,
    open_lots: list[dict],
    spec: dict,
    trade_settings: dict,
    market: dict,
    bars: list[dict],
):
    """Strictly verify whether ``today_qty`` on ``side`` is strategy-proved."""
    if today_qty <= 0:
        return 0, None, 'FLAT_SIDE'
    if not open_lots:
        return 0, None, 'UNPROVED_NO_TODAY_FILL'
    lot_qty = sum(int(lot['qty']) for lot in open_lots)
    if lot_qty <= 0:
        return 0, None, 'UNPROVED_NO_TODAY_FILL'

    open_offset = float(spec.get('open_offset') or 0)
    stop_offset = float(spec.get('stop_offset') or 0)
    profit_offset = float(spec.get('profit_offset') or 0)
    if min(open_offset, stop_offset, profit_offset) <= 0:
        return 0, None, 'UNPROVED_OFFSETS_UNAVAILABLE'

    earliest_lot = open_lots[0]
    t_open = earliest_lot['time']
    if t_open is None:
        return 0, None, 'UNPROVED_MISSING_FILL_TIME'

    # Check entry trading window constraints.
    civil = t_open.astimezone(_BEIJING)
    if not trade_settings.get('participate_auction', True):
        if (civil.hour in (8, 20) and civil.minute >= 55) or (civil.hour in (9, 21) and civil.minute == 0):
            return 0, None, 'UNPROVED_OUTSIDE_ENTRY_WINDOW'
    sess_open = _utc(market.get('session_open'))
    sess_close = _utc(market.get('session_close'))
    if sess_open is not None and sess_close is not None:
        if sess_open <= t_open < sess_close:
            if trade_settings.get('no_open_after_open') and market.get('opening_session', True):
                wait = float(trade_settings.get('no_open_after_open_minutes') or 0) * 60
                if wait > 0 and 0 <= (t_open - sess_open).total_seconds() <= wait:
                    return 0, None, 'UNPROVED_OUTSIDE_ENTRY_WINDOW'
            no_before_close = float(trade_settings.get('no_open_before_close_minutes') or 0) * 60
            if no_before_close > 0 and 0 <= (sess_close - t_open).total_seconds() <= no_before_close:
                return 0, None, 'UNPROVED_OUTSIDE_ENTRY_WINDOW'

    # Find matching minute bar for opening fill.
    if not bars:
        return 0, None, 'UNPROVED_NO_KLINE_EVIDENCE'
    open_minute = _minute_floor(t_open)
    open_bar_idx = None
    for idx, bar in enumerate(bars):
        b_time = bar['time']
        if b_time is not None and _minute_floor(b_time) == open_minute:
            open_bar_idx = idx
            break
    if open_bar_idx is None:
        return 0, None, 'UNPROVED_NO_KLINE_EVIDENCE'

    prefix_bars = bars[:open_bar_idx + 1]
    cum_high = max(b['high'] for b in prefix_bars)
    cum_low = min(b['low'] for b in prefix_bars)
    lines = trigger_prices(cum_high, cum_low, open_offset)
    geom = geometry_limit(open_offset, stop_offset)
    if geom > 0 and (cum_high - cum_low) > geom:
        return 0, None, 'UNPROVED_SIGNAL_MISMATCH'

    open_bar = bars[open_bar_idx]
    theory = float(lines[side])
    if side == 'LONG' and open_bar['high'] < theory:
        return 0, None, 'UNPROVED_SIGNAL_MISMATCH'
    if side == 'SHORT' and open_bar['low'] > theory:
        return 0, None, 'UNPROVED_SIGNAL_MISMATCH'

    total_notional = sum(Decimal(str(lot['price'])) * int(lot['qty']) for lot in open_lots)
    avg_entry = float(total_notional / Decimal(lot_qty))
    slip_ratio = max(
        float(trade_settings.get('entry_slip_ratio') or 0),
        float(trade_settings.get('chase_slip_ratio') or 0),
    )
    cap_dist = max(1.0, round(open_offset * slip_ratio))
    if side == 'LONG' and (avg_entry > theory + cap_dist or avg_entry < cum_low - cap_dist):
        return 0, None, 'UNPROVED_SIGNAL_MISMATCH'
    if side == 'SHORT' and (avg_entry < theory - cap_dist or avg_entry > cum_high + cap_dist):
        return 0, None, 'UNPROVED_SIGNAL_MISMATCH'

    # Cap by strategy configured volume.
    order_vol = int(spec.get('order_volume') or 0)
    max_vol = int(trade_settings.get('max_volume') or 0)
    if order_vol > 0 and max_vol > 0:
        qty_cap = min(order_vol, max_vol)
    elif order_vol > 0:
        qty_cap = order_vol
    elif max_vol > 0:
        qty_cap = max_vol
    else:
        qty_cap = today_qty
    proved_base_qty = min(today_qty, lot_qty, qty_cap)
    if proved_base_qty <= 0:
        return 0, None, 'UNPROVED_QUANTITY_ZERO'

    # Replay subsequent bars + current tick through protection rules.
    sim_pos = {
        'direction': side,
        'theory': decimal_text(Decimal(str(theory))),
        'stop': decimal_text(Decimal(str(stop_offset))),
        'profit': decimal_text(Decimal(str(profit_offset))),
        'owned': proved_base_qty,
        'peak': '0',
        'protected': False,
        'half_stage': 0,
        'exit_target': None,
    }
    sim_settings = dict(trade_settings)
    for bar in bars[open_bar_idx + 1:]:
        # Check open -> adverse extreme -> favorable extreme -> close
        adverse = bar['low'] if side == 'LONG' else bar['high']
        favorable = bar['high'] if side == 'LONG' else bar['low']
        for px in (bar['open'], adverse, favorable, bar['close']):
            sim_pos, events = protect(sim_pos, {'last': str(px)}, sim_settings, spec)
            for ev in events:
                if ev.get('remaining') == 0:
                    return 0, {
                        'exit_reason': ev.get('reason'),
                        'day_stop_triggered': (
                            (ev.get('reason') == 'TAKE_PROFIT' and bool(sim_settings.get('stop_day_on_profit', True)))
                            or (ev.get('reason') == 'TRAILING_STOP' and bool(sim_settings.get('stop_day_on_trailing')))
                            or (ev.get('reason') == 'PROTECTED_FLOOR' and bool(sim_settings.get('protect_stop_day')))
                        ),
                    }, 'PROVED_SHOULD_HAVE_EXITED'
                if 'remaining' in ev and int(ev['remaining']) < sim_pos['owned']:
                    sim_pos['owned'] = max(0, int(ev['remaining']))
                    sim_pos['exit_target'] = None

    tick = market.get('tick')
    if tick is not None and price_value(tick.get('last')) is not None:
        sim_pos, events = protect(sim_pos, tick, sim_settings, spec)
        for ev in events:
            if ev.get('remaining') == 0:
                return 0, {
                    'exit_reason': ev.get('reason'),
                    'day_stop_triggered': (
                        (ev.get('reason') == 'TAKE_PROFIT' and bool(sim_settings.get('stop_day_on_profit', True)))
                        or (ev.get('reason') == 'TRAILING_STOP' and bool(sim_settings.get('stop_day_on_trailing')))
                        or (ev.get('reason') == 'PROTECTED_FLOOR' and bool(sim_settings.get('protect_stop_day')))
                    ),
                }, 'PROVED_SHOULD_HAVE_EXITED'
            if 'remaining' in ev and int(ev['remaining']) < sim_pos['owned']:
                sim_pos['owned'] = max(0, int(ev['remaining']))
                sim_pos['exit_target'] = None

    final_qty = min(today_qty, int(sim_pos['owned']))
    if final_qty <= 0:
        return 0, None, 'PROVED_SHOULD_HAVE_EXITED'

    return final_qty, {
        'direction': side,
        'quantity': final_qty,
        'theory': sim_pos['theory'],
        'stop': sim_pos['stop'],
        'profit': sim_pos['profit'],
        'peak': sim_pos['peak'],
        'protected': bool(sim_pos['protected']),
        'half_stage': int(sim_pos['half_stage']),
        'entry_avg': decimal_text(Decimal(str(avg_entry))),
        'entry_ms': str(int(t_open.timestamp() * 1000)),
        'entry_at': str(t_open.timestamp()),
    }, 'PROVED_OPEN_MATCH'


def infer_startup_symbol_state(
    *,
    contract_id: str,
    spec: dict,
    trade_settings: dict,
    trading_day: str,
    market: dict,
    today_trades: list[dict] | None = None,
    minute_bars: list[dict] | None = None,
    has_prior_day_force_close: bool = True,
) -> dict:
    """Strictly infer startup target & protection baseline for one configured contract."""
    actual_long = int(market.get('long_qty') or 0)
    actual_short = int(market.get('short_qty') or 0)
    long_today = int(market.get('long_today', actual_long))
    long_hist = int(market.get('long_historical', 0))
    short_today = int(market.get('short_today', actual_short))
    short_hist = int(market.get('short_historical', 0))

    normalized_bars = []
    for raw in (minute_bars or ()):
        t = _bar_time_utc(raw)
        try:
            o, h, l, c = (float(raw['open']), float(raw['high']), float(raw['low']), float(raw['close']))
        except (KeyError, TypeError, ValueError):
            continue
        if t is None or min(o, h, l, c) <= 0 or l > h:
            continue
        normalized_bars.append({'time': t, 'open': o, 'high': h, 'low': l, 'close': c,
                                'bar_id': raw.get('bar_id')})
    normalized_bars.sort(key=lambda b: b['time'])

    # Replay today's trades FIFO to reconstruct target_count, net_loss, stop_streak, and open lots.
    parsed_trades = []
    for idx, tr in enumerate(today_trades or ()):
        offset = str(tr.get('offset') or tr.get('native_offset') or '')
        direction = str(tr.get('direction') or '')
        if direction not in {'BUY', 'SELL'} or offset not in {'OPEN', 'CLOSE', 'CLOSETODAY', 'CLOSEYESTERDAY'}:
            continue
        qty = int(tr.get('quantity') or 0)
        if qty <= 0 or price_value(tr.get('price')) is None:
            continue
        t = _trade_time_utc(tr)
        parsed_trades.append({
            'seq': idx,
            'trade_id': str(tr.get('trade_id') or tr.get('native_trade_id') or idx),
            'order_id': str(tr.get('order_id') or tr.get('native_order_id') or idx),
            'direction': direction,
            'offset': offset,
            'qty': qty,
            'price': Decimal(str(tr['price'])),
            'time': t,
        })
    parsed_trades.sort(key=lambda r: (r['time'] or datetime.min.replace(tzinfo=timezone.utc), r['seq']))

    open_lots: dict[str, list[dict]] = {'LONG': [], 'SHORT': []}
    distinct_open_orders: set[str] = set()
    net_loss = Decimal(0)
    stop_streak = 0
    day_stop = False
    day_stop_reason = None
    last_signal_bar = None
    profit_offset = Decimal(str(spec.get('profit_offset') or 0))
    stop_offset = Decimal(str(spec.get('stop_offset') or 0))

    for tr in parsed_trades:
        if tr['offset'] == 'OPEN':
            side = 'LONG' if tr['direction'] == 'BUY' else 'SHORT'
            distinct_open_orders.add(tr['order_id'])
            open_lots[side].append({
                'order_id': tr['order_id'],
                'qty': tr['qty'],
                'price': tr['price'],
                'time': tr['time'],
            })
            if tr['time'] is not None:
                m = _minute_floor(tr['time'])
                for b in normalized_bars:
                    if _minute_floor(b['time']) == m and b.get('bar_id'):
                        last_signal_bar = b['bar_id']
                        break
        else:
            side = 'SHORT' if tr['direction'] == 'BUY' else 'LONG'
            rem = tr['qty']
            lots = open_lots[side]
            matched_pnl = Decimal(0)
            matched_qty = 0
            while rem > 0 and lots:
                head = lots[0]
                take = min(rem, head['qty'])
                diff = (tr['price'] - head['price']) if side == 'LONG' else (head['price'] - tr['price'])
                matched_pnl += diff * take
                matched_qty += take
                rem -= take
                head['qty'] -= take
                if head['qty'] <= 0:
                    lots.pop(0)
            if matched_qty > 0:
                avg_move = matched_pnl / Decimal(matched_qty)
                net_loss -= avg_move
                if avg_move < 0:
                    stop_streak += 1
                else:
                    stop_streak = 0
                if profit_offset > 0 and avg_move >= profit_offset and trade_settings.get('stop_day_on_profit', True):
                    day_stop = True
                    day_stop_reason = '止盈触发'

    target_count = len(distinct_open_orders)
    max_daily = int(trade_settings.get('max_daily_orders') or 0)
    if max_daily > 0 and target_count >= max_daily:
        day_stop = True
        day_stop_reason = day_stop_reason or 'DAILY_TARGET_LIMIT'
    max_stops = Decimal(str(trade_settings.get('max_net_loss_stops') or 0))
    if max_stops > 0 and stop_offset > 0 and net_loss >= stop_offset * max_stops:
        day_stop = True
        day_stop_reason = day_stop_reason or 'NET_LOSS_STOP'

    base_summary = {
        'contract_id': contract_id,
        'trading_day': trading_day,
        'actual_long': actual_long,
        'actual_short': actual_short,
        'target_long': 0,
        'target_short': 0,
        'target_count': target_count,
        'net_loss': decimal_text(net_loss),
        'stop_streak': stop_streak,
        'day_stop': day_stop,
        'day_stop_reason': day_stop_reason,
        'last_signal_bar': last_signal_bar,
        'proved_position': None,
    }

    if actual_long == 0 and actual_short == 0:
        reason = 'FLAT_DAY_STOP_RECONSTRUCTED' if day_stop else 'FLAT_BASELINE'
        return base_summary | {'reason': reason}

    if day_stop:
        return base_summary | {'reason': 'DAY_STOP_ACTIVE'}

    if has_prior_day_force_close and long_today == 0 and short_today == 0 and (long_hist > 0 or short_hist > 0):
        return base_summary | {'reason': 'UNPROVED_OVERNIGHT_POSITION'}

    # Evaluate today positions per side.
    long_qty_proved, long_meta, long_reason = _evaluate_side_plan_b(
        side='LONG', today_qty=long_today, open_lots=open_lots['LONG'],
        spec=spec, trade_settings=trade_settings, market=market, bars=normalized_bars,
    )
    short_qty_proved, short_meta, short_reason = _evaluate_side_plan_b(
        side='SHORT', today_qty=short_today, open_lots=open_lots['SHORT'],
        spec=spec, trade_settings=trade_settings, market=market, bars=normalized_bars,
    )

    for meta in (long_meta, short_meta):
        if isinstance(meta, dict) and meta.get('day_stop_triggered') and not day_stop:
            base_summary['day_stop'] = True
            base_summary['day_stop_reason'] = meta.get('exit_reason') or 'PROTECT_EXIT'

    if long_qty_proved > 0 and short_qty_proved > 0:
        return base_summary | {'reason': 'UNPROVED_TWO_SIDED_POSITION'}

    if long_qty_proved > 0:
        reason = 'PROVED_OPEN_MATCH' if actual_long == long_qty_proved and actual_short == 0 else 'PROVED_PARTIAL_RETAIN'
        if actual_short > 0:
            reason = 'PROVED_ONE_SIDE_OPPOSITE_TO_FLATTEN'
        return base_summary | {
            'target_long': long_qty_proved,
            'proved_position': long_meta,
            'reason': reason,
        }

    if short_qty_proved > 0:
        reason = 'PROVED_OPEN_MATCH' if actual_short == short_qty_proved and actual_long == 0 else 'PROVED_PARTIAL_RETAIN'
        if actual_long > 0:
            reason = 'PROVED_ONE_SIDE_OPPOSITE_TO_FLATTEN'
        return base_summary | {
            'target_short': short_qty_proved,
            'proved_position': short_meta,
            'reason': reason,
        }

    # Neither today side proved -> choose informative reason
    if long_today > 0 and long_reason != 'FLAT_SIDE':
        reason = long_reason
    elif short_today > 0 and short_reason != 'FLAT_SIDE':
        reason = short_reason
    else:
        reason = 'UNPROVED_OVERNIGHT_POSITION'
    return base_summary | {'reason': reason}
