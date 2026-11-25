"""Runtime-compatible read projections for the Standalone DSV owner."""

import copy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.values import decimal_text
from online_trader.dsv_runtime.controller import terminal
from online_trader.dsv_runtime.target_execution import contract_pending_summary, effective_position_target
from online_trader.dsv_core.entry_rules import trigger_prices
from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import native_integer
from online_trader.gateway.tqkq import number
from online_trader.runtime.maintenance_policy import evaluate_responsibility


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _diagnostic(code):
    return {
        'diagnostic_version': 1, 'code': code, 'reason': code,
        'kind': 'STATE', 'category': 'EXECUTION', 'severity': 'WARNING',
        'source': 'DSV_TARGET_OWNER', 'operation': None, 'command_id': None,
        'trace_id': None, 'impact_scope': 'CURRENT_RUNTIME',
        'recovery_state': 'WAITING_FOR_BROKER_FACTS', 'retry_policy': 'OWNER_RECONCILES',
        'occurred_at': None, 'context': {},
    }


def _average_entry(position):
    if not position['entry_filled']:
        return None
    with localcontext() as context:
        context.prec = 80
        return decimal_text(Decimal(position['entry_notional']) / position['entry_filled'])


def _level_prices(position):
    theory = position.get('theory')
    stop = position.get('stop')
    profit = position.get('profit')
    if theory is None:
        return stop, profit
    sign = Decimal(1 if position['direction'] == 'LONG' else -1)
    base = Decimal(theory)
    stop_price = decimal_text(base - sign * Decimal(stop)) if stop is not None else None
    profit_price = decimal_text(base + sign * Decimal(profit)) if profit is not None else None
    return stop_price, profit_price


def _trigger_lines(tick, symbol):
    high, low, offset = tick.get('day_high'), tick.get('day_low'), symbol.get('open_offset')
    if high is None or low is None or offset is None:
        return {}
    try:
        lines = trigger_prices(Decimal(high), Decimal(low), Decimal(offset))
    except (ArithmeticError, ValueError):
        return {}
    return {side: decimal_text(value) for side, value in lines.items()}


def _exit_wait_reason(row, position, target):
    if position.get('exit_manual_takeover') or target.get('decision_reason') == 'EXIT_MANUAL_TAKEOVER_REQUIRED':
        return 'EXIT_MANUAL_INTERVENTION_REQUIRED'
    if target.get('status') == 'RECONCILING':
        return 'EXIT_RECONCILING'
    if row.get('exit_wait_reason'):
        return row['exit_wait_reason']
    if position.get('consecutive_exit_rejections', 0) > 0 and target.get('reduce_needed_qty'):
        return 'EXIT_RETRY_SCHEDULED'
    return None


def _schedule_display(adapter, trading_day, contracts):
    calendar = getattr(adapter, 'calendar', None)
    reader = getattr(calendar, 'session_times', None)
    if not callable(reader) or not trading_day or not contracts:
        return None
    try:
        schedules = {code: reader(code, trading_day) for code in contracts}
    except (TraderError, KeyError, TypeError, ValueError):
        return None
    return {
        'trading_day': trading_day,
        'intervals': {code: [list(pair) for pair in rows[:16]] for code, rows in schedules.items()},
        'truncated': any(len(rows) > 16 for rows in schedules.values()),
    }


def _strategy_diagnostics(clock_health, clock_fault, cycles):
    diagnostics = {}
    if isinstance(clock_health, dict):
        status = clock_health.get('reference_status')
        if status in {'OBSERVED_OK', 'CALIBRATED'}:
            diagnostics['ntp'] = 'NTP_ALIGNED'
        elif status == 'REFERENCE_CONFLICT':
            diagnostics['ntp'] = 'NTP_SKEW'
        elif status is not None:
            diagnostics['ntp'] = 'NTP_UNAVAILABLE'
    if clock_fault:
        diagnostics['clock_protection'] = {
            'active_cycles': [row['position_cycle_id'] for row in cycles],
            'blocked_cycles': {},
        }
    return diagnostics


def projection_batch(snapshot):
    """Translate committed DSV state to the existing projection/1 records."""
    scope = snapshot['scope']
    revision_num = max(1, int(snapshot.get('projection_seq') or snapshot.get('sequence') or 0))
    revision = str(revision_num)
    fault_code = snapshot.get('owner_fault_code')
    ready = snapshot['synchronized'] and not snapshot.get('clock_recovery_required') and not fault_code
    records = [{
        'record_type': 'ACCOUNT', 'record_id': scope['account_key'], 'record_revision': revision,
        'kernel_state': 'READY' if ready else 'RECONCILING',
        'entry_authority': 'ARMED' if snapshot['running'] else 'DISARMED',
        'equity': None, 'available': None, 'margin': None, 'fees': None,
        'funds_quality': 'UNKNOWN', 'source_epoch': '0', 'fact_barrier_id': None,
    }]
    account_snapshot = snapshot.get('account_snapshot')
    if account_snapshot is not None:
        funds = account_snapshot['funds']
        records[0].update(
            equity=funds.get('balance'), available=funds.get('available'),
            margin=funds.get('margin'), fees=funds.get('commission'),
            funds_quality='KNOWN', source_epoch=str(account_snapshot['source_epoch']))
    all_positions = []
    seen_positions = set()
    for contract, row in snapshot['symbols'].items():
        position = row.get('position')
        if position is not None:
            all_positions.append((contract, position, row.get('target_execution') or {}))
            seen_positions.add(position['id'])
    for position_id, position in sorted((snapshot.get('retired_positions') or {}).items()):
        if position_id not in seen_positions:
            all_positions.append((position['contract_id'], position, {}))
            seen_positions.add(position_id)
    for order in snapshot['orders'].values():
        pos_id = order['position_id']
        if pos_id not in seen_positions:
            pos_orders = [o for o in snapshot['orders'].values() if o['position_id'] == pos_id]
            open_filled = sum(o['filled_qty'] for o in pos_orders if o['effect'] == 'OPEN')
            close_filled = sum(o['filled_qty'] for o in pos_orders if o['effect'] == 'REDUCE')
            owned = max(0, open_filled - close_filled)
            direction = ('LONG' if (order['direction'] == 'BUY') == (order['effect'] == 'OPEN') else 'SHORT')
            all_positions.append((order['contract_id'], {
                'id': pos_id, 'contract_id': order['contract_id'], 'direction': direction,
                'entry_filled': open_filled, 'owned': owned, 'exit_target': 0 if not owned else None,
                'target_id': pos_id, 'target_revision': 1, 'protected': False,
                'children': [o['child_order_id'] for o in pos_orders],
            }, {}))
            seen_positions.add(pos_id)
    sync_pending_by_contract = snapshot.get('sync_pending_by_contract') or {}
    for contract, position, target in all_positions:
        cycle_state = ('EXIT_PENDING' if target.get('reduce_pending_qty') or target.get('reduce_needed_qty')
                       else 'HOLDING' if position['owned']
                       else 'ENTRY_PENDING' if any(not terminal(snapshot['orders'][child])
                                                   for child in position['children']
                                                   if child in snapshot['orders'])
                       else 'CLOSED')
        pos_rev = str(max(revision_num, int(position.get('target_revision', 1))))
        records.append({
            'record_type': 'CYCLE', 'record_id': position['id'],
            'record_revision': pos_rev,
            'position_cycle_id': position['id'], 'contract_id': contract,
            'direction': position['direction'],
            'cycle_state': cycle_state,
            'confirmed_open_quantity': position['entry_filled'],
            'confirmed_close_quantity': max(0, position['entry_filled'] - position['owned']),
            'remaining_quantity': position['owned'],
            'target_remaining_quantity': position['exit_target'],
            'plan_id': position.get('target_id', position['id']),
            'plan_revision': str(position.get('target_revision', 1)),
            'protection_mode': 'PROTECTED' if position.get('protected') else 'NORMAL',
            'protection_quality': 'CONTIGUOUS' if (
                snapshot['synchronized'] and not sync_pending_by_contract.get(contract)
            ) else 'UNKNOWN',
            'protection_persistence': 'DURABLE', 'takeover_state': None,
        })
    for child_id, order in snapshot['orders'].items():
        send_state = 'CONFIRMED' if order['send_state'] == 'NATIVE_SEEN' else order['send_state']
        broker_state = order['broker_state']
        if broker_state == 'ALIVE':
            broker_state = 'PARTIALLY_FILLED' if order['filled_qty'] else 'WORKING'
        ord_rev = str(max(revision_num, int(order.get('revision', 1))))
        raw_native = order.get('native_id')
        broker_key = (None if not isinstance(raw_native, str) or not raw_native
                      else re.sub(r'[^A-Za-z0-9._:-]', ':', raw_native)[:128])
        records.append({
            'record_type': 'ORDER', 'record_id': child_id,
            'record_revision': ord_rev,
            'child_order_id': child_id, 'intent_id': order['intent_id'],
            'position_cycle_id': order['position_id'], 'contract_id': order['contract_id'],
            'origin': 'STRATEGY' if order['effect'] == 'OPEN' else 'PROTECTION',
            'order_quantity': order['original_qty'],
            'confirmed_fill_quantity': order['filled_qty'],
            'broker_order_key': broker_key, 'attempt_state': send_state,
            'broker_order_state': broker_state, 'cancel_state': order['cancel_status'],
        })
    records.append({
        'record_type': 'BLOCK', 'record_id': 'standalone-current-boot-scope',
        'record_revision': revision, 'reason_code': 'CURRENT_BOOT_EXECUTION_ONLY',
        'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []}, 'active': True,
        'since_event_seq': '0', 'release_evidence_id': None,
    })
    return {'records': records}


def workbench_positions(snapshot, after=None):
    rows = []
    for contract, row in sorted(snapshot['symbols'].items()):
        position = row.get('position')
        if (position is None or not position['owned']
                or after is not None and position['id'] <= after):
            continue
        rows.append({'position_cycle_id': position['id'], 'contract_id': contract,
                     'direction': position['direction'], 'remaining_quantity': position['owned'],
                     'plan_revision': str(position.get('target_revision', 1))})
    rows.sort(key=lambda item: item['position_cycle_id'])
    return {'account_key': snapshot['scope']['account_key'],
            'kernel_boot_id': snapshot['scope']['runtime_boot_id'],
            'checkpoint_event_seq': str(snapshot['sequence']),
            'positions': rows[:64], 'has_more': len(rows) > 64}


def format_minute_chart_bars(raw_bars_by_code):
    """Validate and format raw (datetime, open, high, low, close) bar tuples off-thread."""
    result = {}
    for code, rows in (raw_bars_by_code or {}).items():
        bars = {}
        for stamp_raw, open_raw, high_raw, low_raw, close_raw in rows:
            try:
                stamp = native_integer(stamp_raw, minimum=60_000_000_000)
                values = tuple(number(val) for val in (open_raw, high_raw, low_raw, close_raw))
                if any(value is None or len(value) > 32 for value in values):
                    continue
                opening, high, low, closing = map(Decimal, values)
                if 0 < low <= min(opening, closing) <= max(opening, closing) <= high:
                    bars[stamp // 60_000_000_000] = list(values)
            except (TraderError, TypeError, ValueError, ArithmeticError):
                continue
        if bars:
            result[code] = [[minute, *bars[minute]] for minute in sorted(bars)]
    return result


def workbench_view(snapshot, *, profile, adapter=None, configuration_revision, projection_inputs=None):
    scope = snapshot['scope']
    boot = scope['runtime_boot_id']
    revision = str(max(1, int(snapshot.get('projection_seq') or snapshot.get('sequence') or 0)))
    profile_record = profile if isinstance(profile, dict) else profile.to_record()
    profile_hash = (
        projection_inputs.get('profile_hash')
        if projection_inputs is not None and projection_inputs.get('profile_hash') is not None
        else (profile_record.get('content_hash') if isinstance(profile, dict) else profile.content_hash)
    )
    if profile_hash is None:
        from online_trader.configuration.dsv import DsvProfile
        profile_hash = DsvProfile.from_record(profile_record).content_hash
    settings = profile_record['settings']
    symbols = {item['code']: item for item in settings['symbols']}
    latest_decisions = snapshot.get('latest_decisions') or {}
    if projection_inputs is not None:
        dispatch_markets = projection_inputs.get('dispatch_market') or {}
        unconfigured_dispatch = projection_inputs.get('unconfigured_dispatch_market') or {}
        market_details_map = projection_inputs.get('market_details') or {}
        connected = bool(projection_inputs.get('broker_connected'))
        adapter_synchronized = bool(projection_inputs.get('adapter_synchronized'))
        clock_health = projection_inputs.get('clock_health')
        market_health = projection_inputs.get('market_health')
        schedule_display = projection_inputs.get('schedule_display')
        calendar_hash = projection_inputs.get('calendar_hash')
        if 'minute_bars' in projection_inputs:
            minute_bars = projection_inputs['minute_bars']
        else:
            minute_bars = format_minute_chart_bars(projection_inputs.get('minute_raw'))
    else:
        raw_dispatch = getattr(adapter, 'dispatch_market', {}) or {}
        dispatch_markets = {k: v for k, v in raw_dispatch.items() if k in symbols}
        unconfigured_dispatch = {k: v for k, v in raw_dispatch.items() if k not in symbols}
        market_details_map = None
        detail_reader = getattr(adapter, 'display_market_details', None)
        connected = bool(adapter.connection and adapter.connection.broker_connected)
        adapter_synchronized = bool(adapter.synchronized)
        clock_health = getattr(adapter, '_health', None)
        market_health = getattr(adapter, 'market_health', None)
        schedule_display = _schedule_display(adapter, snapshot.get('trading_day'), list(symbols))
        calendar_hash = adapter.grant.value['calendar_hash']
        bar_reader = getattr(adapter, 'minute_chart_bars', None)
        minute_bars = bar_reader() if callable(bar_reader) else {}
    symbol_health_map = (market_health or {}).get('symbols') or {}
    feed_health_status = (market_health or {}).get('status')
    startup_inference = snapshot.get('startup_inference') or {}
    convergence_status = snapshot.get('convergence_status') or {}
    markets, cycles, orders, trades, external_positions, account_positions = [], [], [], [], [], []
    seen_trade_positions = set()
    for contract, row in snapshot['symbols'].items():
        tick = row.get('tick') or {}
        market = dispatch_markets.get(contract, {})
        quote = row.get('quote_observation') or {}
        decision = latest_decisions.get(contract) or {}
        decision_reasons = list(dict.fromkeys(
            item.get('reason') for item in decision.get('decisions', ()) if item.get('reason')))
        fresh = bool(tick and quote.get('age_quality') == 'VALID'
                     and row.get('market_session_active') is not False)
        position = row.get('position')
        all_snapshot_orders = list((snapshot.get('orders') or {}).values())
        pos_orders = (
            [snapshot['orders'][oid] for oid in position.get('children', ()) if oid in (snapshot.get('orders') or {})]
            if position is not None else ()
        )
        actual_long = int(market.get('long_qty', 0) or 0)
        actual_short = int(market.get('short_qty', 0) or 0)
        target_long, target_short = effective_position_target(
            position, orders=pos_orders, terminal=terminal)
        excess_long = max(0, actual_long - target_long)
        excess_short = max(0, actual_short - target_short)
        deficit_long = max(0, target_long - actual_long)
        deficit_short = max(0, target_short - actual_short)
        pending = contract_pending_summary(all_snapshot_orders, contract, terminal)
        conv = convergence_status.get(contract) or {}
        inf = startup_inference.get(contract) or {}
        perm = market.get('market_permission') or {}
        if conv.get('status'):
            conv_state = conv['status']
        elif market.get('position_known') is False:
            conv_state = 'DEFERRED_FACTS_INCOMPLETE'
        elif actual_long == target_long and actual_short == target_short and pending['total_pending'] == 0:
            conv_state = 'ALIGNED'
        elif not snapshot['running']:
            conv_state = 'PREVIEW_NEEDS_CONVERGENCE'
        elif deficit_long or deficit_short or pending['open_long'] or pending['open_short']:
            conv_state = 'OPENING'
        elif pending['close_long'] or pending['close_short']:
            conv_state = 'CONVERGING_FILL_PENDING'
        else:
            conv_state = 'CONVERGING'
        conv_wait_reason = conv.get('wait_reason') or (
            perm.get('wait_reason') if (excess_long or excess_short or deficit_long or deficit_short) else None
        )
        market_phase = conv.get('market_phase') or perm.get('market_phase')
        evidence_state = conv.get('evidence_state') or perm.get('evidence_state')
        account_positions.append({
            'contract_id': contract,
            'name': symbols[contract]['name'],
            'configured': True,
            'actual_long': actual_long,
            'actual_short': actual_short,
            'target_long': target_long,
            'target_short': target_short,
            'excess_long': excess_long,
            'excess_short': excess_short,
            'deficit_long': deficit_long,
            'deficit_short': deficit_short,
            'pending_open_long': conv.get('pending_open_long', pending['open_long']),
            'pending_open_short': conv.get('pending_open_short', pending['open_short']),
            'pending_close_long': conv.get('pending_close_long', pending['close_long']),
            'pending_close_short': conv.get('pending_close_short', pending['close_short']),
            'status': conv_state,
            'wait_reason': conv_wait_reason,
            'failure_count': int(conv.get('failure_count') or 0),
            'market_phase': market_phase,
            'evidence_state': evidence_state,
            'startup_reason': inf.get('reason'),
            'market_session_active': market.get('market_session_active', True),
        })
        if actual_long or actual_short:
            decision_reasons = [
                reason for reason in decision_reasons
                if reason not in {'UNOWNED_POSITION', 'EXTERNAL_POSITION_PRESENT'}
            ]
        entry_reasons = decision_reasons
        day_stop_reason = ((row.get('day_stop_reason') or '当日已停止开仓')
                           if row.get('day_stop') else None)
        live_last = tick.get('last') if tick.get('last') is not None else market.get('last_price')
        if live_last is None and market.get('ticks'):
            live_last = (market['ticks'][-1] or {}).get('last')
        market_row = {
            'contract_id': contract, 'name': symbols[contract]['name'],
            'trading_day': snapshot['trading_day'], 'session_id': snapshot.get('session_id'),
            'last': live_last,
            'bid1': tick.get('bid1'), 'ask1': tick.get('ask1'),
            'day_high': tick.get('day_high'), 'day_low': tick.get('day_low'),
            'upper_limit': tick.get('upper_limit'), 'lower_limit': tick.get('lower_limit'),
            'margin': market.get('margin_per_lot'), 'volume_multiple': market.get('multiplier'),
            'source_time_utc': tick.get('native_time'),
            'source_time_raw': quote.get('source_time_raw') or tick.get('native_time'),
            'source_age_raw_ns': quote.get('source_age_raw_ns'),
            'quote_time_reason': quote.get('reason'),
            'fresh': fresh,
            'enabled': True,
            'configured': True,
            'actual_long': actual_long,
            'actual_short': actual_short,
            'target_long': target_long,
            'target_short': target_short,
            'convergence_status': conv_state,
            'convergence_wait_reason': conv_wait_reason,
            'market_phase': market_phase,
            'evidence_state': evidence_state,
            'day_stop_reason': day_stop_reason,
            'entry_reasons': list(dict.fromkeys(entry_reasons)),
            'trigger_prices': _trigger_lines(tick, symbols[contract]),
            'decision_time_utc': decision.get('decision_time_utc'),
            'price_tick': tick.get('tick_size'),
            'market_session_active': market.get('market_session_active'),
            'symbol_health': symbol_health_map.get(contract),
            'feed_health_status': feed_health_status,
            'position_quantity': 0 if position is None else position['owned'],
            'net_loss_points': row.get('net_loss'), 'opening_orders': row.get('target_count', 0),
        }
        if market_details_map is not None:
            details = market_details_map.get(contract)
            if details:
                market_row.update(details)
        elif callable(detail_reader):
            market_row.update(detail_reader(contract, snapshot.get('trading_day'), fresh=fresh))
        markets.append(market_row)
        if position is None:
            continue
        target = row.get('target_execution') or {}
        entry_price = _average_entry(position)
        stop_price, take_profit_price = _level_prices(position)
        last_price = tick.get('last')
        sign = Decimal(1 if position['direction'] == 'LONG' else -1)
        price_diff = (decimal_text(sign * (Decimal(last_price) - Decimal(entry_price)))
                      if last_price is not None and entry_price is not None else None)
        multiplier = market.get('multiplier')
        floating_profit = (
            decimal_text(Decimal(price_diff) * Decimal(position['owned']) * Decimal(str(multiplier)))
            if price_diff is not None and multiplier is not None and position['owned'] > 0 else None
        )
        margin_per_lot = market.get('margin_per_lot')
        margin = (
            decimal_text(Decimal(str(margin_per_lot)) * Decimal(position['owned']))
            if margin_per_lot is not None and position['owned'] > 0 else None
        )
        half_stage = int(position.get('half_stage') or 0)
        exit_pending = bool(target.get('reduce_needed_qty') or target.get('reduce_pending_qty'))
        rollback_exit = position.get('rollback_exit') or {}
        exit_reason = rollback_exit.get('reason')
        cycles.append({
            'position_cycle_id': position['id'], 'contract_id': contract,
            'direction': position['direction'], 'trading_day': position.get('open_day'),
            'lifecycle': 'OPEN' if position['owned'] else 'PLANNED',
            'remaining_quantity': position['owned'],
            'target_remaining_quantity': position['exit_target'],
            'mode': 'PROTECTED' if position.get('protected') else 'NORMAL',
            'quality': 'CONTIGUOUS' if (
                snapshot['synchronized'] and not (snapshot.get('sync_pending_by_contract') or {}).get(contract)
            ) else 'UNKNOWN',
            'exit_pending': exit_pending,
            'entry_price': entry_price,
            'stop_price': stop_price, 'take_profit_price': take_profit_price,
            'last_price': last_price, 'price_difference': price_diff,
            'margin': margin, 'floating_profit': floating_profit, 'commission': None,
            'initial_open_confirmation': (
                {'status': 'CONFIRMED', 'quantity': position['entry_filled'],
                 'value': position['entry_notional']}
                if position.get('entry_filled') else None
            ),
            'phase1_status': 'DONE' if half_stage >= 1 else 'NOT_TRIGGERED',
            'phase2_status': 'DONE' if half_stage >= 2 else 'NOT_TRIGGERED',
            'phase1_target': (position['exit_target']
                              if half_stage == 1 and position.get('exit_target') not in (None, 0) else None),
            'phase2_target': (position['exit_target']
                              if half_stage >= 2 and position.get('exit_target') not in (None, 0) else None),
            'break_even_status': 'NOT_TRIGGERED',
            'max_favorable_move': position.get('peak'),
            'exit_reason': exit_reason,
            'exit_reasons': [exit_reason] if exit_reason else [],
            'exit_reference_price': rollback_exit.get('price'),
            'exit_wait_reason': _exit_wait_reason(row, position, target) if exit_pending else None,
            'entry_tag': position.get('entry_kind'),
            'management_state': None, 'plan_id': position.get('target_id', position['id']),
            'plan_revision': str(position.get('target_revision', 1)),
            'target_execution': target,
        })
        seen_trade_positions.add(position['id'])
        for fill in position.get('fills', ()):
            trades.append(dict(fill) | {'position_cycle_id': position['id']})
    for position_id, position in sorted((snapshot.get('retired_positions') or {}).items()):
        if position_id in seen_trade_positions:
            continue
        for fill in position.get('fills', ()):
            trades.append(dict(fill) | {'position_cycle_id': position['id']})
    unconf_seen = set(unconfigured_dispatch) | {
        cid for cid, conv in convergence_status.items() if conv.get('configured') is False
    }
    for cid in sorted(unconf_seen):
        um = unconfigured_dispatch.get(cid) or {}
        conv = convergence_status.get(cid) or {}
        perm = um.get('market_permission') or {}
        actual_long = int(um.get('long_qty', conv.get('actual_long', 0)) or 0)
        actual_short = int(um.get('short_qty', conv.get('actual_short', 0)) or 0)
        last_px = um.get('last_price')
        if last_px is None and um.get('ticks'):
            last_px = um['ticks'][-1].get('last')
        conv_state = conv.get('status') or (
            'ALIGNED' if (actual_long == 0 and actual_short == 0)
            else ('PREVIEW_NEEDS_CONVERGENCE' if not snapshot['running'] else 'CONVERGING')
        )
        account_positions.append({
            'contract_id': cid,
            'name': cid,
            'configured': False,
            'actual_long': actual_long,
            'actual_short': actual_short,
            'target_long': 0,
            'target_short': 0,
            'excess_long': actual_long,
            'excess_short': actual_short,
            'last': last_px,
            'status': conv_state,
            'wait_reason': conv.get('wait_reason') or perm.get('wait_reason'),
            'failure_count': int(conv.get('failure_count') or 0),
            'market_phase': conv.get('market_phase') or perm.get('market_phase'),
            'evidence_state': conv.get('evidence_state') or perm.get('evidence_state'),
            'startup_reason': 'UNCONFIGURED_CONTRACT',
            'market_session_active': um.get('market_session_active', False),
        })
    trades.sort(key=lambda item: (int(item.get('event_seq') or 0), str(item.get('child_order_id') or '')))
    for child_id, order in snapshot['orders'].items():
        if order.get('convergence_order'):
            for cfill in order.get('convergence_fills', ()):
                trades.append(dict(cfill))
        remaining = 0 if terminal(order) else max(0, order['original_qty'] - order['filled_qty'])
        strategy_direction = ('LONG' if (order['direction'] == 'BUY') == (order['effect'] == 'OPEN')
                              else 'SHORT')
        orders.append({key: order.get(key) for key in (
            'child_order_id', 'intent_id', 'attempt_id', 'effect', 'original_qty', 'filled_qty',
            'limit_price', 'native_offset', 'native_trading_day', 'send_state', 'broker_state',
            'cancel_status', 'terminal_confirmed', 'reported_fill_qty')}
            | {'position_cycle_id': order['position_id'], 'contract_id': order['contract_id'],
               'native_order_id': order.get('native_id'), 'direction': strategy_direction,
               'source_time_ns': order.get('native_order_time_ns'),
               'native_message': order.get('native_message'),
               'potential_remaining': remaining,
               'order_source': order.get('order_source') or 'STRATEGY'})
    synchronized = connected and snapshot['synchronized'] and adapter_synchronized
    storage_failed = bool(snapshot.get('storage_failed', False))
    clock_fault = snapshot.get('clock_recovery_required')
    owner_fault = snapshot.get('owner_fault_code')
    maintenance = snapshot.get('maintenance')
    responsibility = snapshot.get('responsibility_summary')
    if not isinstance(responsibility, dict):
        responsibility = evaluate_responsibility(
            snapshot,
            strategy_intent=snapshot.get('strategy_intent') or ('RUNNING' if snapshot.get('running') else 'STOPPED'),
            has_start_pending=bool(snapshot.get('has_start_pending')),
            maintenance=maintenance,
        )
    operation_scopes = {
        'ARM_ENTRIES': 'ACCOUNT',
        'PAUSE_NEW_ENTRIES': 'ACCOUNT',
        'STOP_DSV_STRATEGY': 'ACCOUNT',
        'MANUAL_CLOSE': 'CYCLE',
        'MANUAL_CLOSE_ALL': 'ACCOUNT',
        'CANCEL_SELECTED': 'SELECTED_ORDERS',
        'CANCEL_OWNED': 'OWNED_ORDERS',
        'FLATTEN_THEN_STOP': 'ACCOUNT',
        'SAFE_STOP_FOR_MAINTENANCE': 'ACCOUNT',
        'REQUEST_RECONCILIATION': 'ACCOUNT',
        'SAVE_DSV_PARAMETERS': 'ACCOUNT',
    }
    operations = []
    for operation, scope_type in operation_scopes.items():
        if storage_failed:
            allowed, reason = False, 'E_STORAGE'
        elif operation == 'SAFE_STOP_FOR_MAINTENANCE':
            allowed = bool(responsibility['maintenance_allowed']) and connected and adapter_synchronized
            reason = (None if allowed else
                      (responsibility['blockers'][0] if responsibility.get('blockers') else 'RUNTIME_NOT_READY'))
        elif operation == 'ARM_ENTRIES':
            allowed = (synchronized and not snapshot['running'] and not clock_fault
                       and not owner_fault and maintenance is None)
            reason = (None if allowed else
                      'MAINTENANCE_IN_FLIGHT' if maintenance is not None else
                      'CLOCK_FAULT' if clock_fault else
                      owner_fault if owner_fault else
                      'ALREADY_ARMED' if snapshot['running'] else 'RUNTIME_NOT_READY')
        elif operation == 'SAVE_DSV_PARAMETERS':
            allowed = synchronized and not clock_fault and not owner_fault and maintenance is None
            reason = (None if allowed else
                      'MAINTENANCE_IN_FLIGHT' if maintenance is not None else
                      'CLOCK_FAULT' if clock_fault else
                      owner_fault if owner_fault else 'RUNTIME_NOT_READY')
        elif operation == 'FLATTEN_THEN_STOP':
            flat_and_clean = not cycles and not orders and not (snapshot.get('sync_pending_by_contract') or {}) and not any(
                row.get('native_conflicts') or row.get('detached_positions')
                for row in snapshot['symbols'].values()
            )
            allowed = synchronized or flat_and_clean
            reason = None if allowed else 'RUNTIME_NOT_READY'
        elif operation in {'PAUSE_NEW_ENTRIES', 'STOP_DSV_STRATEGY', 'REQUEST_RECONCILIATION'}:
            allowed, reason = True, None
        else:
            allowed = synchronized
            reason = None if allowed else 'RUNTIME_NOT_READY'
        operations.append({'operation': operation, 'scope_type': scope_type,
                           'allowed': allowed, 'reason': reason})
    strategy_diagnostics = _strategy_diagnostics(clock_health, clock_fault, cycles)
    config = {
        'config_revision': str(configuration_revision),
        'rule_set_id': 'DSV_TARGET_EXECUTION',
        'execution_core': 'DSV_TARGET_EXECUTION',
        'strategy_profile_hash': profile_hash,
        'strategy_profile': profile_record,
        'scoped_contracts': list(symbols), 'strategy_symbols': list(symbols.values()),
        'cancel_on_stop': settings['trade']['cancel_on_stop'],
        'manual_close_price_mode': settings['trade']['close_price_mode'],
        'manual_workbench': False, 'live_parameter_save': True,
        'calendar_hash': calendar_hash,
        'runtime_identity': snapshot.get('runtime_identity'),
        'run_directory': snapshot.get('run_directory'),
    }
    if isinstance(clock_health, dict):
        config['clock_health'] = copy.deepcopy(clock_health)
    if isinstance(market_health, dict):
        config['market_health'] = copy.deepcopy(market_health)
        if market_health.get('age_filter_disabled'):
            config['age_filter_disabled'] = True
    if schedule_display is not None:
        config['schedule_display'] = schedule_display
    if strategy_diagnostics:
        config['strategy_diagnostics'] = strategy_diagnostics
    config_snapshot = {
        'schema_version': 'ONLINE_TRADER_CONFIG_SNAPSHOT/1',
        'account_key': scope['account_key'], 'kernel_boot_id': boot,
        'config_revision': str(configuration_revision), 'payload_hash': profile_hash,
        'rule_set_id': 'DSV_TARGET_EXECUTION', 'profile_hash': profile_hash,
        'effective_boundary': 'CURRENT_BOOT', 'created_event_seq': '0',
        'effective_at_utc': None, 'decision_inputs': {'calendar_hash': calendar_hash},
        'integrity': 'VERIFIED', 'errors': [],
    }
    config['config_snapshot_hash'] = content_hash(config_snapshot)
    owner_fault_detail = snapshot.get('owner_fault_detail')
    has_sync_isolation = bool(snapshot.get('sync_pending_by_contract'))
    blocks = []
    if not synchronized or has_sync_isolation:
        blocks.append({'reason_code': 'BROKER_FACTS_INCOMPLETE', 'active': True})
    if clock_fault:
        blocks.append({'reason_code': 'CLOCK_FAULT', 'active': True})
    if owner_fault:
        block = {'reason_code': owner_fault, 'active': True}
        if owner_fault_detail:
            block['detail'] = owner_fault_detail
        blocks.append(block)
    if isinstance(market_health, dict) and market_health.get('feed_entry_blocked') and synchronized and not clock_fault and not owner_fault:
        blocks.append({'reason_code': 'MARKET_FEED_' + str(market_health.get('status') or 'DEGRADED'), 'active': True})
    observed_at_utc = _utc_now()
    result = {
        'view_schema': 'workbench/1', 'account_key': scope['account_key'],
        'kernel_boot_id': boot, 'checkpoint_event_seq': revision,
        'entry_authority': 'ARMED' if snapshot['running'] else 'DISARMED',
        'execution_scope': 'ONLINE_SIMULATION', 'configuration': config,
        'connection': {'broker_connected': connected, 'synchronized': synchronized,
                       'fault': False,
                       'recovery_required': not synchronized or has_sync_isolation or bool(owner_fault),
                       'owner_fault_code': owner_fault,
                       'owner_fault_detail': owner_fault_detail,
                       'storage_failed': storage_failed,
                       'clock_fault': clock_fault,
                       'execution_mode': 'STANDALONE',
                       'account_positions': account_positions,
                       'non_strategy_orders_count': len(snapshot.get('convergence_cancels') or {}),
                       'pending_transactions': 0},
        'markets': markets, 'cycles': cycles, 'orders': orders, 'trades': trades,
        'minute_bars': minute_bars,
        'blocks': blocks, 'commands': list(snapshot.get('runtime_commands', ())), 'policies': [
            {'policy_id': 'target-authority-current-boot', 'policy_kind': 'AUTHORITY'},
            {'policy_id': 'target-execution-current-boot', 'policy_kind': 'EXECUTION'},
        ], 'truncated': False,
        'symbol_entry_switches': {}, 'external_positions': external_positions,
        'config_snapshot': config_snapshot,
        'contract_catalog': {
            'revision': revision,
            'observed_at_utc': observed_at_utc,
            'complete': False,
            'source': 'RUNTIME_SCOPED_MARKET_CACHE',
            'contracts': sorted(symbols)[:4_096],
        },
        'capabilities': {'revision': revision, 'observed_at_utc': observed_at_utc,
                         'operations': operations},
        'diagnostics': [_diagnostic(block['reason_code']) for block in blocks],
        'current_responsibility': {
            'cycles': len(cycles),
            'owned_quantity': sum(row['remaining_quantity'] for row in cycles),
            'active_orders': sum(1 for order in snapshot['orders'].values() if not terminal(order)),
        },
        'runtime_mode': responsibility['mode'],
        'strategy_intent': responsibility['strategy_intent'],
        'responsibility': {
            'state': responsibility['state'],
            'protected_lots_total': responsibility['protected_lots_total'],
            'unresolved_children': len(responsibility['unresolved_child_ids']),
            'unresolved_convergence_cancels': len(responsibility['unresolved_convergence_cancel_ids']),
            'conflict_contracts': list(responsibility['conflict_contracts'])[:64],
            'preview_only_contracts': list(responsibility['preview_only_contracts'])[:64],
            'blockers': list(responsibility['blockers'])[:32],
            'maintenance_allowed': bool(responsibility['maintenance_allowed']) and connected and adapter_synchronized,
        },
        'maintenance': (
            {
                'command_id': str(maintenance['command_id']),
                'reason': str(maintenance['reason']),
                'state': str(maintenance['state']),
            }
            if isinstance(maintenance, dict) else None
        ),
        'maintenance_completion': (
            {
                'command_id': str(completion['command_id']),
                'reason': str(completion['reason']),
                'state': str(completion['state']),
                'blockers': list((completion.get('responsibility') or {}).get('blockers') or ())[:32],
                'completed_at_utc': str(completion['completed_at_utc']),
            }
            if isinstance((completion := snapshot.get('maintenance_completion')), dict) else None
        ),
        'config_recovery_notice': (
            {
                'state': str(notice['state']),
                'message': str(notice['message']),
                'apply_id': (None if notice.get('apply_id') is None else str(notice['apply_id'])),
            }
            if isinstance((notice := snapshot.get('config_recovery_notice')), dict) else None
        ),
    }
    account_snapshot = snapshot.get('account_snapshot')
    if account_snapshot is not None:
        result['account_metrics'] = dict(account_snapshot['funds']) | {
            'trading_day': account_snapshot['trading_day'],
            'opening_orders': sum(row.get('target_count', 0) for row in snapshot['symbols'].values()),
        }
    result['status_projection'] = projection_batch(snapshot)
    history_omitted = {'cycles': 0, 'orders': 0, 'trades': 0, 'commands': 0, 'minute_bars': 0}
    active_children = {child for child, order in snapshot['orders'].items() if not terminal(order)}
    if len(active_children) <= 512:
        active_rows = [row for row in result['orders'] if row['child_order_id'] in active_children]
        terminal_rows = [row for row in result['orders'] if row['child_order_id'] not in active_children]
        keep_terminal = max(0, 512 - len(active_rows))
        history_omitted['orders'] = max(0, len(terminal_rows) - keep_terminal)
        result['orders'] = active_rows + terminal_rows[-keep_terminal:] if keep_terminal else active_rows
    for name, limit in (('trades', 512), ('commands', 64)):
        rows = result[name]
        if len(rows) > limit:
            history_omitted[name] = len(rows) - limit
            result[name] = rows[-limit:]
    result['history_omitted'] = history_omitted
    result['history_truncated'] = any(history_omitted.values())
    if len(canonical_bytes({'base': result['status_projection'], 'view': result})) > 950_000:
        result['truncated'] = True
        result['detail_unavailable'] = 'CURRENT_STATE_OVERSIZE'
        result['detail_omitted'] = {name: len(result.get(name, ())) for name in (
            'cycles', 'orders', 'trades', 'markets', 'blocks', 'commands', 'external_positions')}
        for name in result['detail_omitted']:
            result[name] = []
        result['blocks'] = [{'reason_code': 'WORKBENCH_DETAIL_CAPACITY', 'active': True}]
        result.pop('minute_bars', None)
    return result
