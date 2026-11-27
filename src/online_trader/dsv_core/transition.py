"""Ordinary-session decisions for the DSV round contract."""

from datetime import datetime, timedelta, timezone
from online_trader.dsv_core.entry_rules import continuation_direction, geometry_limit, hysteresis_ready, opening_jump_block, trigger_prices
from online_trader.dsv_core.protection_rules import half_close_target, trailing_floor
from online_trader.dsv_core.sizing_rules import auto_lots, margin_lots

_BEIJING = timezone(timedelta(hours=8))


def price_value(value):
    if value is None or value == 'NaN':
        return None
    number = float(value)
    return number if number > 0 else None


def entry_price(tick, direction, mode, cap):
    buy = direction == 'LONG'
    opponent = price_value(tick.get('ask1' if buy else 'bid1'))
    last = price_value(tick.get('last'))
    leading = opponent or last
    if mode == 'PASSIVE':
        return cap
    if mode == 'CAP' and leading is not None:
        return min(leading, cap) if buy else max(leading, cap)
    return leading or price_value(tick.get('upper_limit' if buy else 'lower_limit'))


def _lots(market, settings, spec, active_symbols):
    available = market.get('available')
    if available is None:
        return None
    available = float(available)
    per_lot = price_value(market.get('margin_per_lot'))
    multiplier = price_value(market.get('multiplier'))
    if per_lot is None:
        if multiplier is None:
            return None
        per_lot = price_value(market['tick']['last']) * multiplier * 0.15
    limit = settings['max_volume']
    if spec['order_volume']:
        return min(spec['order_volume'], limit, margin_lots(available, per_lot))

    keys = ('equity', 'risk_ratio', 'margin', 'frozen_margin', 'pending_margin', 'symbol_margin')
    if multiplier is None or any(market.get(key) is None for key in keys):
        return None
    equity, risk_ratio, margin, frozen, pending, used_symbol = (float(market[key]) for key in keys)
    risk_cap = min(float(settings['max_risk_ratio']), float(settings['total_margin_cap']), 1.0)
    if risk_ratio >= risk_cap:
        return 0
    all_budget = equity * risk_cap - (margin + frozen + pending)
    symbol_budget = equity * (risk_cap / active_symbols) - used_symbol
    budget = symbol_budget if settings['aggressive_mode'] else min(symbol_budget, all_budget)
    ratio = float(settings['risk_per_trade_ratio'])
    risk_quantity = margin_lots(equity * ratio, float(spec['stop_offset']) * multiplier)
    allowed = auto_lots(margin_lots(min(available, budget), per_lot), limit,
                        aggressive=settings['aggressive_mode'], risk_enabled=ratio > 0,
                        risk_quantity=risk_quantity)
    if allowed == 0 and settings['min_one_lot_fallback'] and min(available, all_budget) >= per_lot:
        return 1
    return allowed


def _clock(frame, market, name):
    return datetime.fromisoformat(market.get('wall_reads', {}).get(name, frame['wall_time']))


def _profit_activation(settings):
    return float(settings['protect_80_ratio']) or 0.8


def _opening_observation(state, market, start, last, lines, stop, profit, spec, settings):
    if state.get('opening_observation_start') != market['session_open']:
        state.update(opening_observation_start=market['session_open'], open_half_reached=False,
                     need_hyst=False, hyst_armed=False)
    observed = market.get('wall_reads', {}).get('opening_observation')
    if observed is None:
        return {}, None
    seconds = (datetime.fromisoformat(observed) - start).total_seconds()
    long, short = lines['LONG'], lines['SHORT']
    jump = False
    if 0 <= seconds <= 60 and not state['need_hyst']:
        ratio = float(settings['open_jump_block_ratio'])
        jump = any(opening_jump_block(last, line, profit, ratio) for line in (long, short))
        if last >= long + profit * 0.5 or last <= short - profit * 0.5:
            state['open_half_reached'] = True
        elif not state['open_half_reached']:
            step = price_value(market['tick'].get('tick_size')) or 1.0
            state['need_hyst'] = any(abs(last - line) <= step * 1.5 for line in (long, short))
    meta = {}
    if jump:
        meta.update(day_stop=True, day_stop_reason='FIRST_MINUTE_JUMP_DAY_STOP')
    if state['need_hyst']:
        direction = 'LONG' if last < (long + short) / 2 else 'SHORT'
        distance = float(spec['hysteresis_points']) or stop
        ready, state['hyst_armed'] = hysteresis_ready(direction, last, lines[direction], distance, state['hyst_armed'])
        if not ready:
            return meta, 'HYSTERESIS_NOT_READY'
    return meta, None


def continuation_entry(state, frame, market, spec, settings, *, active_symbols, open_symbols):
    tick, night = market['tick'], state.get('night_reference')
    if (not settings['continuation_entry'] or state.get('continuation_attempted') or
            state.get('stop_streak', 0) >= 2 or state.get('continuation_stopped') and settings['cont_stop_day_stop']
            or not night or not night['complete'] or state['day_stop']):
        return None
    last, high, low = (price_value(tick.get(key)) for key in ('last', 'day_high', 'day_low'))
    if None in (last, high, low):
        return None
    start = datetime.fromisoformat(market['session_open'])
    elapsed = (_clock(frame, market, 'chase') - start).total_seconds()
    civil = datetime.fromisoformat(tick['native_time']).astimezone(_BEIJING)
    if (start.astimezone(_BEIJING).strftime('%H:%M') != '09:00' or civil.hour != 9 or
            not 0 <= elapsed <= float(settings['chase_window_minutes']) * 60):
        return None
    opening, stop, profit = (float(spec[key]) for key in ('open_offset', 'stop_offset', 'profit_offset'))
    if (min(opening, stop, profit) <= 0 or state['target_count'] >= settings['max_daily_orders'] or
            settings['max_net_loss_stops'] > 0 and float(state['net_loss']) >= stop * settings['max_net_loss_stops']):
        return None
    maximum = geometry_limit(opening, stop)
    wait = (_clock(frame, market, 'opening') - start).total_seconds()
    if (maximum > 0 and high - low > maximum or settings['no_open_after_open'] and
            not settings['continuation_ignore_no_open'] and 0 <= wait < float(settings['no_open_after_open_minutes']) * 60):
        night.pop('fallback', None)
        return None
    direction = continuation_direction(last, float(night['last']), float(night['high']), float(night['low']),
                                       opening, night.get('fallback'))
    if direction is None or settings['max_symbols_enabled'] and open_symbols >= settings['max_symbols_on']:
        return None
    lots = _lots(market, settings, spec, active_symbols)
    if not lots or not market.get('min_order_qty', 1) <= lots <= market.get('max_order_qty', 2_147_483_647):
        return None
    return {'reason': 'CONTINUATION_ENTRY', 'target': {
        'direction': direction, 'quantity': lots, 'theory': str(last), 'stop': str(stop), 'profit': str(profit),
        'mode': 'ACTIVE', 'cap': str(last), 'order_ms': str(int(_clock(frame, market, 'submit').timestamp() * 1000)),
        'submitted_at': str(_clock(frame, market, 'pending_start').timestamp()),
        'context_at': str(_clock(frame, market, 'price_context').timestamp())}}


def ordinary_entry(state, frame, market, spec, settings, *, active_symbols, open_symbols):
    """Evaluate ordinary entry and report the outcome and any consumed bar."""
    tick = market['tick']
    last, high, low = (price_value(tick.get(key)) for key in ('last', 'day_high', 'day_low'))
    if None in (last, high, low):
        return {'reason': 'QUOTE_UNAVAILABLE'}
    if state['position'] is not None:
        return {'reason': 'POSITION_OR_ENTRY_PENDING'}
    opening, stop, profit = (float(spec[key]) for key in ('open_offset', 'stop_offset', 'profit_offset'))
    target_count = state['target_count']
    if state.get('day_stop') or (settings['max_net_loss_stops'] > 0 and
                                 float(state['net_loss']) >= stop * settings['max_net_loss_stops']):
        return {'reason': 'DAY_STOP'}
    if target_count >= settings['max_daily_orders']:
        return {'reason': 'DAILY_TARGET_LIMIT', 'day_stop': True}
    civil = datetime.fromisoformat(tick['native_time']).astimezone(_BEIJING)
    if not settings['participate_auction'] and civil.hour in (8, 20) and civil.minute >= 55:
        return {'reason': 'AUCTION_DISABLED'}
    local_start, local_end = map(datetime.fromisoformat, (market['session_open'], market['session_close']))
    decision_time = _clock(frame, market, 'session')
    if not local_start <= decision_time < local_end:
        return {'reason': 'OUTSIDE_SESSION'}
    opening_session = market['opening_session']
    elapsed_open = (_clock(frame, market, 'opening') - local_start).total_seconds()
    wait = float(settings['no_open_after_open_minutes']) * 60
    if settings['no_open_after_open'] and opening_session and wait > 0 and 0 <= elapsed_open <= wait:
        return {'reason': 'OPENING_WAIT'}
    entry_close = market.get('entry_close')
    if entry_close is not None:
        remaining = (datetime.fromisoformat(entry_close) - _clock(frame, market, 'entry_close')).total_seconds()
        if 0 <= remaining <= float(settings['no_open_before_close_minutes']) * 60:
            return {'reason': 'CLOSE_ENTRY_WINDOW'}
    age_limit = float(settings['max_quote_age_seconds'])
    native_time = datetime.fromisoformat(tick['native_time']).replace(microsecond=0)
    if age_limit > 0 and (_clock(frame, market, 'quote_age') - native_time).total_seconds() > age_limit:
        return {'reason': 'QUOTE_AGE'}
    # The frozen loop checks expiry once per configured contract (7933–7953).
    expiry = state.get('expiry_days_checked')
    expiry_observation = {}
    if expiry is None and market.get('days_to_expiry') is not None:
        expiry = int(market['days_to_expiry'])
        expiry_observation['expiry_days_checked'] = expiry
    if expiry is not None and expiry <= settings['min_days_to_expiry']:
        return expiry_observation | {'reason': 'EXPIRY_ENTRY_BLOCK', 'days_to_expiry': expiry}
    return expiry_observation | _ordinary_signal(
        state, frame, market, spec, settings, active_symbols, open_symbols,
        local_start, (last, high, low), (opening, stop, profit))


def _ordinary_signal(state, frame, market, spec, settings, active_symbols, open_symbols,
                     local_start, prices, offsets):
    last, high, low = prices
    opening, stop, profit = offsets
    tick = market['tick']
    if market.get('liquidation_at') is not None:
        end = datetime.fromisoformat(market['liquidation_at'])
        if any(0 < (end - moment).total_seconds() <= settings['close_before_seconds']
               for moment in (_clock(frame, market, 'close'), datetime.fromisoformat(tick['native_time']))):
            return {'reason': 'CLOSE_ENTRY_WINDOW'}
    if min(opening, stop, profit) <= 0:
        return {'reason': 'OFFSETS_UNAVAILABLE'}
    lines = trigger_prices(high, low, opening)
    geometry_block = high - low >= geometry_limit(opening, stop)
    bar = tick['candle_id']
    if state['last_signal_bar'] == bar:
        return {'reason': 'GEOMETRY_BLOCK' if geometry_block else 'BAR_CONSUMED'}
    activation = profit * (_profit_activation(settings) if settings['protect_80_enabled']
                           else float(settings['trailing_activate_ratio']))
    long_hit, short_hit = last >= lines['LONG'], last <= lines['SHORT']
    # DSV reports geometry before checking the activation that can consume the bar.
    if settings['no_chase_after_act'] and activation > 0:
        long_level, short_level = lines['LONG'] + activation, lines['SHORT'] - activation
        direction = None
        if long_hit and high >= long_level:
            direction, level = 'LONG', long_level
        elif short_hit and low <= short_level:
            direction, level = 'SHORT', short_level
        if direction is not None:
            return {'reason': 'NO_CHASE_AFTER_ACT', 'consume': bar, 'geometry_block': geometry_block,
                    'direction': direction, 'trigger_price': str(lines[direction]), 'activation_price': str(level)}
    if geometry_block:
        return {'reason': 'GEOMETRY_BLOCK'}
    meta, blocked = _opening_observation(state, market, local_start, last, lines, stop, profit, spec, settings)
    if blocked:
        return {'reason': blocked, 'consume': bar, **meta}
    return meta | _ordinary_target(state, frame, market, spec, settings, active_symbols, open_symbols,
                                   local_start, last, lines, long_hit, short_hit, opening, stop, profit)


def _ordinary_target(state, frame, market, spec, settings, active_symbols, open_symbols,
                     local_start, last, lines, long_hit, short_hit, opening, stop, profit):
    tick = market['tick']
    bar = tick['candle_id']
    direction = 'LONG' if long_hit else 'SHORT' if short_hit else None
    if direction is None:
        return {'reason': 'TRIGGER_NOT_REACHED'}
    if settings['max_symbols_enabled'] and open_symbols >= settings['max_symbols_on']:
        return {'reason': 'ACTIVE_SYMBOL_LIMIT', 'consume': bar}
    theory = lines[direction]
    previous_stop = state.get('last_stop')
    if previous_stop and previous_stop['direction'] == direction:
        if settings['stop_cooldown_enabled'] and previous_stop.get('until') is not None:
            active = market.get('stop_cooldown_active')
            if active is None:
                active = _clock(frame, market, 'cooldown').timestamp() < float(previous_stop['until'])
            if active:
                return {'reason': 'STOP_COOLDOWN', 'consume': bar}
        if settings['require_trigger_update'] and abs(float(previous_stop['theory']) - theory) < 1e-6:
            return {'reason': 'STOP_TRIGGER_UNCHANGED', 'consume': bar}
    elapsed_chase = (_clock(frame, market, 'chase') - local_start).total_seconds()
    near_open = market['opening_session'] and 0 <= elapsed_chase <= float(settings['chase_window_minutes']) * 60
    cap_distance = max(1, round(opening * float(settings['chase_slip_ratio'] if near_open else settings['entry_slip_ratio'])))
    buy = direction == 'LONG'
    opponent = price_value(tick.get('ask1' if buy else 'bid1')) or 0.0
    # DSV performs the boundary comparisons with native floats.
    band = theory + cap_distance if buy else theory - cap_distance
    if buy:
        exceeds = any(p > band for p in (last, opponent)) and not any(0 < p <= theory for p in (last, opponent))
    else:
        exceeds = any(0 < p < band for p in (last, opponent)) and not any(p >= theory for p in (last, opponent))
    if exceeds:
        return {'reason': 'CHASE_CAP_EXCEEDED', 'consume': bar}
    upper, lower = (price_value(tick.get(key)) for key in ('upper_limit', 'lower_limit'))
    if (buy and upper and theory + profit > upper) or (not buy and lower and theory - profit < lower):
        return {'reason': 'TAKE_PROFIT_OUTSIDE_LIMIT', 'consume': bar}
    if (upper and (upper - last) / upper < 0.01) or (lower and (last - lower) / lower < 0.01):
        return {'reason': 'QUOTE_NEAR_LIMIT', 'consume': bar}
    order_time = _clock(frame, market, 'submit').timestamp()
    previous = state.get('last_order_ms')
    if state.get('last_order_direction') == direction and previous is not None:
        active = market.get('entry_throttle_active')
        if active is None:
            active = int(order_time * 1000) - int(previous) < 1000
        if active:
            return {'reason': 'ENTRY_THROTTLED', 'consume': bar}
    lots = _lots(market, settings, spec, active_symbols)
    if not lots:
        return {'reason': 'FUNDS_UNAVAILABLE' if lots is None else 'INSUFFICIENT_MARGIN', 'consume': bar}
    if lots > market.get('max_order_qty', 2_147_483_647) or lots < market.get('min_order_qty', 1):
        return {'reason': 'ORDER_QUANTITY_UNSUPPORTED', 'consume': bar}
    submitted = _clock(frame, market, 'pending_start').timestamp()
    context_at = _clock(frame, market, 'price_context').timestamp()
    mode = 'ACTIVE' if near_open else 'PASSIVE' if settings['passive_entry_first'] else 'CAP'
    return {'reason': 'NORMAL_ENTRY', 'consume': bar, 'target': {'direction': direction, 'quantity': lots,
            'theory': str(theory), 'stop': str(stop), 'profit': str(profit), 'mode': mode, 'cap': str(band),
            'order_ms': str(int(order_time * 1000)), 'submitted_at': str(submitted), 'context_at': str(context_at)}}


def protect(position, tick, settings, spec):
    """Evaluate one current quote in the source protection order."""
    owned = position['owned']
    if not owned or position['exit_target'] == 0 or price_value(tick.get('last')) is None:
        return position, []
    last = float(tick['last'])
    theory, stop, profit = (float(position[k]) for k in ('theory', 'stop', 'profit'))
    move = (last - theory) if position['direction'] == 'LONG' else (theory - last)
    peak = max(float(position['peak']), move)
    updated = position | {'peak': str(peak)}
    events = []
    if settings['protect_80_enabled'] and not updated['protected'] and peak >= profit * _profit_activation(settings):
        updated['protected'] = True
        events.append({'reason': 'PROTECTION_ACTIVATED'})
    reason = None
    if updated['protected']:
        if move >= profit:
            reason = 'TAKE_PROFIT'
        elif move <= profit * (float(settings['protect_keep_ratio']) or 0.5):
            reason = 'PROTECTED_FLOOR'
    else:
        virtual = owned if updated['exit_target'] is None else min(owned, updated['exit_target'])
        stage = updated['half_stage']
        first_ratio = float(spec['half_close_ratio'])
        first = first_ratio * profit
        second_ratio = min(0.95, max(0.05, float(settings['half_stage2_trigger_ratio'])))
        second = second_ratio * profit
        if second <= first:
            second = first * 1.5
        if settings['half_close_enabled'] and 0 < first_ratio < 1 and virtual >= 2 and (
                stage == 0 and move >= first or stage == 1 and settings['half_stage2'] and (
                    move >= second or settings.get('half_pullback_close', False) and move <= first)):
            # Frozen gui.decide_half_close 1871-1877: pullback is a second-stage
            # trigger, not an independent exit. Retain one remaining target.
            target = half_close_target(virtual)
            updated.update(half_stage=stage + 1, exit_target=target)
            events.append({'reason': f'PARTIAL_STAGE_{stage + 1}', 'remaining': target})
        if move <= -stop:
            reason = 'STOP_LOSS'
        elif move >= profit:
            reason = 'TAKE_PROFIT'
        elif (activation := float(settings['trailing_activate_ratio'])) > 0 and peak >= profit * activation:
            if move <= trailing_floor(peak, profit, float(settings['trailing_pullback_ratio'])):
                reason = 'TRAILING_STOP'
    if reason:
        updated['exit_target'] = 0
        events.append({'reason': reason, 'remaining': 0, 'move': str(move)})
    return updated, events
