"""Runtime-compatible read projections for the Standalone DSV owner."""

from datetime import datetime, timezone
from decimal import Decimal, localcontext

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


def projection_batch(snapshot):
    """Translate committed DSV state to the existing projection/1 records."""
    scope = snapshot['scope']
    revision = str(max(1, int(snapshot.get('sequence') or 0)))
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
    current_positions = set()
    for contract, row in snapshot['symbols'].items():
        position = row.get('position')
        if position is None:
            continue
        current_positions.add(position['id'])
        target = row.get('target_execution') or {}
        cycle_state = ('EXIT_PENDING' if target.get('reduce_pending_qty') or target.get('reduce_needed_qty')
                       else 'HOLDING' if position['owned']
                       else 'ENTRY_PENDING' if any(not terminal(snapshot['orders'][child])
                                                   for child in position['children'])
                       else 'CLOSED')
        records.append({
            'record_type': 'CYCLE', 'record_id': position['id'],
            'record_revision': str(position.get('target_revision', 1)),
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
    order_rows = list(snapshot['orders'].items())
    terminal_current = [(child, order) for child, order in order_rows
                        if terminal(order) and order['position_id'] in current_positions]
    visible_orders = [(child, order) for child, order in order_rows if not terminal(order)]
    visible_ids = {child for child, _ in visible_orders}
    visible_orders.extend((child, order) for child, order in terminal_current[-64:]
                          if child not in visible_ids)
    for child_id, order in visible_orders:
        send_state = 'CONFIRMED' if order['send_state'] == 'NATIVE_SEEN' else order['send_state']
        broker_state = order['broker_state']
        if broker_state == 'ALIVE':
            broker_state = 'PARTIALLY_FILLED' if order['filled_qty'] else 'WORKING'
        records.append({
            'record_type': 'ORDER', 'record_id': child_id,
            'record_revision': str(max(1, int(order.get('revision', 1)))),
            'child_order_id': child_id, 'intent_id': order['intent_id'],
            'position_cycle_id': order['position_id'], 'contract_id': order['contract_id'],
            'origin': 'STRATEGY' if order['effect'] == 'OPEN' else 'PROTECTION',
            'order_quantity': order['original_qty'],
            'confirmed_fill_quantity': order['filled_qty'],
            'broker_order_key': order.get('native_id'), 'attempt_state': send_state,
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
    markets, cycles, orders, trades = [], [], [], []
    for contract, row in snapshot['symbols'].items():
        tick = row.get('tick') or {}
        market = adapter.dispatch_market.get(contract, {})
        quote = row.get('quote_observation') or {}
        fresh = bool(tick and quote.get('age_quality') == 'VALID'
                     and row.get('market_session_active') is not False)
        markets.append({
            'contract_id': contract, 'name': symbols[contract]['name'],
            'trading_day': snapshot['trading_day'], 'session_id': snapshot.get('session_id'),
            'last': tick.get('last'), 'bid1': tick.get('bid1'), 'ask1': tick.get('ask1'),
            'day_high': tick.get('day_high'), 'day_low': tick.get('day_low'),
            'source_time_utc': tick.get('native_time'), 'fresh': fresh,
            'enabled': snapshot['running'], 'entry_reasons': [],
            'market_session_active': market.get('market_session_active'),
            'position_quantity': 0 if row.get('position') is None else row['position']['owned'],
            'net_loss_points': row.get('net_loss'), 'opening_orders': row.get('target_count', 0),
        })
        position = row.get('position')
        if position is None:
            continue
        target = row.get('target_execution') or {}
        cycles.append({
            'position_cycle_id': position['id'], 'contract_id': contract,
            'direction': position['direction'], 'trading_day': position.get('open_day'),
            'lifecycle': 'OPEN' if position['owned'] else 'PLANNED',
            'remaining_quantity': position['owned'],
            'target_remaining_quantity': position['exit_target'],
            'mode': 'PROTECTED' if position.get('protected') else 'NORMAL',
            'quality': 'CONTIGUOUS' if snapshot['synchronized'] else 'UNKNOWN',
            'exit_pending': bool(target.get('reduce_needed_qty') or target.get('reduce_pending_qty')),
            'entry_price': _average_entry(position),
            'stop_price': position.get('stop'), 'take_profit_price': position.get('profit'),
            'last_price': tick.get('last'), 'price_difference': None,
            'margin': None, 'floating_profit': None, 'commission': None,
            'management_state': None, 'plan_id': position.get('target_id', position['id']),
            'plan_revision': str(position.get('target_revision', 1)),
            'target_execution': target,
        })
        for fill in position.get('fills', ()):
            trades.append(dict(fill) | {'position_cycle_id': position['id']})
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
    operation_names = ('ARM_ENTRIES', 'PAUSE_NEW_ENTRIES', 'STOP_DSV_STRATEGY',
                       'MANUAL_CLOSE', 'MANUAL_CLOSE_ALL', 'CANCEL_SELECTED',
                       'CANCEL_OWNED', 'FLATTEN_THEN_STOP', 'REQUEST_RECONCILIATION')
    operations = []
    for operation in operation_names:
        if operation == 'ARM_ENTRIES':
            allowed = synchronized and not snapshot['running']
        elif operation in {'PAUSE_NEW_ENTRIES', 'STOP_DSV_STRATEGY', 'REQUEST_RECONCILIATION'}:
            allowed = True
        else:
            allowed = synchronized
        operations.append({'operation': operation, 'scope_type': 'ACCOUNT',
                           'allowed': allowed, 'reason': None if allowed else 'RUNTIME_NOT_READY'})
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
    if snapshot.get('clock_recovery_required'):
        blocks.append({'reason_code': 'CLOCK_FAULT', 'active': True})
    result = {
        'view_schema': 'workbench/1', 'account_key': scope['account_key'],
        'kernel_boot_id': boot, 'checkpoint_event_seq': revision,
        'entry_authority': 'ARMED' if snapshot['running'] else 'DISARMED',
        'execution_scope': 'ONLINE_SIMULATION', 'configuration': config,
        'connection': {'broker_connected': connected, 'synchronized': synchronized,
                       'fault': False, 'recovery_required': not synchronized,
                       'storage_failed': False,
                       'clock_fault': snapshot.get('clock_recovery_required'),
                       'pending_transactions': 0},
        'markets': markets, 'cycles': cycles, 'orders': orders, 'trades': trades,
        'blocks': blocks, 'commands': list(snapshot.get('runtime_commands', ())), 'policies': [
            {'policy_id': 'target-authority-current-boot', 'policy_kind': 'AUTHORITY'},
            {'policy_id': 'target-execution-current-boot', 'policy_kind': 'EXECUTION'},
        ], 'truncated': False,
        'symbol_entry_switches': {}, 'external_positions': [],
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
