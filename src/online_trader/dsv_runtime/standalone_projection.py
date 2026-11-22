"""Runtime-compatible read projections for the Standalone DSV owner."""

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.values import decimal_text
from online_trader.dsv_runtime.controller import terminal


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


def projection_batch(snapshot):
    """Translate committed DSV state to the existing projection/1 records."""
    scope = snapshot['scope']
    revision_num = max(1, int(snapshot.get('projection_seq') or snapshot.get('sequence') or 0))
    revision = str(revision_num)
    ready = snapshot['synchronized'] and not snapshot.get('clock_recovery_required')
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
            'protection_quality': 'CONTIGUOUS' if snapshot['synchronized'] else 'UNKNOWN',
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
                     'direction': position['direction'], 'remaining_quantity': position['owned']})
    rows.sort(key=lambda item: item['position_cycle_id'])
    return {'account_key': snapshot['scope']['account_key'],
            'kernel_boot_id': snapshot['scope']['runtime_boot_id'],
            'checkpoint_event_seq': str(snapshot.get('sequence') or 0),
            'positions': rows[:64], 'has_more': len(rows) > 64}


def workbench_view(snapshot, *, profile, adapter, configuration_revision):
    scope = snapshot['scope']
    boot = scope['runtime_boot_id']
    revision = str(max(1, int(snapshot.get('sequence') or 0)))
    settings = profile.to_record()['settings']
    symbols = {item['code']: item for item in settings['symbols']}
    markets, cycles, orders, trades, external_positions = [], [], [], [], []
    seen_trade_positions = set()
    for contract, row in snapshot['symbols'].items():
        tick = row.get('tick') or {}
        market = adapter.dispatch_market.get(contract, {})
        quote = row.get('quote_observation') or {}
        fresh = bool(tick and quote.get('age_quality') == 'VALID'
                     and row.get('market_session_active') is not False)
        position = row.get('position')
        owned_long = position['owned'] if position is not None and position['direction'] == 'LONG' else 0
        owned_short = position['owned'] if position is not None and position['direction'] == 'SHORT' else 0
        ext_long = max(0, int(market.get('long_qty', 0) or 0) - owned_long)
        ext_short = max(0, int(market.get('short_qty', 0) or 0) - owned_short)
        if ext_long > 0:
            external_positions.append({
                'contract_id': contract, 'direction': 'LONG', 'quantity': ext_long,
                'classification': 'UNOWNED_BROKER_POSITION',
            })
        if ext_short > 0:
            external_positions.append({
                'contract_id': contract, 'direction': 'SHORT', 'quantity': ext_short,
                'classification': 'UNOWNED_BROKER_POSITION',
            })
        markets.append({
            'contract_id': contract, 'name': symbols[contract]['name'],
            'trading_day': snapshot['trading_day'], 'session_id': snapshot.get('session_id'),
            'last': tick.get('last'), 'bid1': tick.get('bid1'), 'ask1': tick.get('ask1'),
            'day_high': tick.get('day_high'), 'day_low': tick.get('day_low'),
            'source_time_utc': tick.get('native_time'),
            'source_time_raw': quote.get('source_time_raw') or tick.get('native_time'),
            'source_age_raw_ns': quote.get('source_age_raw_ns'),
            'quote_time_reason': quote.get('reason'),
            'fresh': fresh,
            'enabled': bool(snapshot['running'] and not row.get('day_stop', False)),
            'entry_reasons': ['EXTERNAL_POSITION_PRESENT'] if (ext_long or ext_short) else [],
            'market_session_active': market.get('market_session_active'),
            'position_quantity': 0 if position is None else position['owned'],
            'net_loss_points': row.get('net_loss'), 'opening_orders': row.get('target_count', 0),
        })
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
        cycles.append({
            'position_cycle_id': position['id'], 'contract_id': contract,
            'direction': position['direction'], 'trading_day': position.get('open_day'),
            'lifecycle': 'OPEN' if position['owned'] else 'PLANNED',
            'remaining_quantity': position['owned'],
            'target_remaining_quantity': position['exit_target'],
            'mode': 'PROTECTED' if position.get('protected') else 'NORMAL',
            'quality': 'CONTIGUOUS' if snapshot['synchronized'] else 'UNKNOWN',
            'exit_pending': bool(target.get('reduce_needed_qty') or target.get('reduce_pending_qty')),
            'entry_price': entry_price,
            'stop_price': stop_price, 'take_profit_price': take_profit_price,
            'last_price': last_price, 'price_difference': price_diff,
            'margin': margin, 'floating_profit': floating_profit, 'commission': None,
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
    trades.sort(key=lambda item: (int(item.get('event_seq') or 0), str(item.get('child_order_id') or '')))
    for child_id, order in snapshot['orders'].items():
        remaining = 0 if terminal(order) else max(0, order['original_qty'] - order['filled_qty'])
        strategy_direction = ('LONG' if (order['direction'] == 'BUY') == (order['effect'] == 'OPEN')
                              else 'SHORT')
        orders.append({key: order.get(key) for key in (
            'child_order_id', 'intent_id', 'attempt_id', 'effect', 'original_qty', 'filled_qty',
            'limit_price', 'native_offset', 'native_trading_day', 'send_state', 'broker_state',
            'cancel_status', 'terminal_confirmed', 'reported_fill_qty')}
            | {'position_cycle_id': order['position_id'], 'contract_id': order['contract_id'],
               'native_order_id': order.get('native_id'), 'direction': strategy_direction,
               'potential_remaining': remaining, 'order_source': 'STRATEGY'})
    connected = bool(adapter.connection and adapter.connection.broker_connected)
    synchronized = connected and snapshot['synchronized'] and adapter.synchronized
    storage_failed = bool(snapshot.get('storage_failed', False))
    clock_fault = snapshot.get('clock_recovery_required')
    operation_scopes = {
        'ARM_ENTRIES': 'ACCOUNT',
        'PAUSE_NEW_ENTRIES': 'ACCOUNT',
        'STOP_DSV_STRATEGY': 'ACCOUNT',
        'MANUAL_CLOSE': 'CYCLE',
        'MANUAL_CLOSE_ALL': 'ACCOUNT',
        'CANCEL_SELECTED': 'SELECTED_ORDERS',
        'CANCEL_OWNED': 'OWNED_ORDERS',
        'FLATTEN_THEN_STOP': 'ACCOUNT',
        'REQUEST_RECONCILIATION': 'ACCOUNT',
    }
    operations = []
    for operation, scope_type in operation_scopes.items():
        if storage_failed:
            allowed, reason = False, 'E_STORAGE'
        elif operation == 'ARM_ENTRIES':
            allowed = synchronized and not snapshot['running'] and not clock_fault
            reason = (None if allowed else
                      'CLOCK_FAULT' if clock_fault else
                      'ALREADY_ARMED' if snapshot['running'] else 'RUNTIME_NOT_READY')
        elif operation in {'PAUSE_NEW_ENTRIES', 'STOP_DSV_STRATEGY', 'REQUEST_RECONCILIATION'}:
            allowed, reason = True, None
        else:
            allowed = synchronized
            reason = None if allowed else 'RUNTIME_NOT_READY'
        operations.append({'operation': operation, 'scope_type': scope_type,
                           'allowed': allowed, 'reason': reason})
    config = {
        'config_revision': str(configuration_revision),
        'rule_set_id': 'DSV_TARGET_EXECUTION',
        'execution_core': 'DSV_TARGET_EXECUTION',
        'strategy_profile_hash': profile.content_hash,
        'scoped_contracts': list(symbols), 'strategy_symbols': list(symbols.values()),
        'cancel_on_stop': settings['trade']['cancel_on_stop'],
        'manual_close_price_mode': settings['trade']['close_price_mode'],
        'manual_workbench': False, 'live_parameter_save': False,
        'calendar_hash': adapter.grant.value['calendar_hash'],
        'runtime_identity': snapshot.get('runtime_identity'),
    }
    config_snapshot = {
        'schema_version': 'ONLINE_TRADER_CONFIG_SNAPSHOT/1',
        'account_key': scope['account_key'], 'kernel_boot_id': boot,
        'config_revision': str(configuration_revision), 'payload_hash': profile.content_hash,
        'rule_set_id': 'DSV_TARGET_EXECUTION', 'profile_hash': profile.content_hash,
        'effective_boundary': 'CURRENT_BOOT', 'created_event_seq': '0',
        'effective_at_utc': None, 'decision_inputs': {'calendar_hash': adapter.grant.value['calendar_hash']},
        'integrity': 'VERIFIED', 'errors': [],
    }
    config['config_snapshot_hash'] = content_hash(config_snapshot)
    blocks = []
    if not synchronized:
        blocks.append({'reason_code': 'BROKER_FACTS_INCOMPLETE', 'active': True})
    if clock_fault:
        blocks.append({'reason_code': 'CLOCK_FAULT', 'active': True})
    if any(row.get('external_order_block') for row in snapshot['symbols'].values()):
        blocks.append({'reason_code': 'E_FOREIGN_ACTIVE_ORDER', 'active': True})
    result = {
        'view_schema': 'workbench/1', 'account_key': scope['account_key'],
        'kernel_boot_id': boot, 'checkpoint_event_seq': revision,
        'entry_authority': 'ARMED' if snapshot['running'] else 'DISARMED',
        'execution_scope': 'ONLINE_SIMULATION', 'configuration': config,
        'connection': {'broker_connected': connected, 'synchronized': synchronized,
                       'fault': False, 'recovery_required': not synchronized,
                       'storage_failed': storage_failed,
                       'clock_fault': clock_fault,
                       'pending_transactions': 0},
        'markets': markets, 'cycles': cycles, 'orders': orders, 'trades': trades,
        'blocks': blocks, 'commands': list(snapshot.get('runtime_commands', ())), 'policies': [
            {'policy_id': 'target-authority-current-boot', 'policy_kind': 'AUTHORITY'},
            {'policy_id': 'target-execution-current-boot', 'policy_kind': 'EXECUTION'},
        ], 'truncated': False,
        'symbol_entry_switches': {}, 'external_positions': external_positions,
        'config_snapshot': config_snapshot,
        'capabilities': {'revision': revision, 'observed_at_utc': _utc_now(),
                         'operations': operations},
        'diagnostics': [_diagnostic(block['reason_code']) for block in blocks],
        'current_responsibility': {
            'cycles': len(cycles),
            'owned_quantity': sum(row['remaining_quantity'] for row in cycles),
            'active_orders': sum(1 for order in snapshot['orders'].values() if not terminal(order)),
        },
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
    return result
