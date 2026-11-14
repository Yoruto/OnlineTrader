"""Rehearse returning to a stopped legacy owner at a proved cabinet reset."""

from contextlib import closing
from datetime import datetime
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.dsv_runtime.controller import terminal
from online_trader.dsv_runtime.migration import rollback_snapshots, _source_digest
from online_trader.errors import TraderError
from online_trader.scheduling.calendar import TradingCalendar


_BUCKETS = ('long_today', 'long_historical', 'short_today', 'short_historical')


def _native_flat(frame, contracts):
    return ({row['contract_id'] for row in frame['market']} == contracts and
            all(row['long_qty'] == row['short_qty'] == 0 and row.get('external_orders') is False and
                all(row.get(key) == 0 for key in _BUCKETS) for row in frame['market']))


def _check_reset(db, state, receipt, calendar, blockers):
    data = calendar.to_record()
    contracts = set(state['symbols'])
    day, origin = state['trading_day'], receipt['target_scope']['trading_day']
    by_contract = {item['contract_id']: item['sessions'] for item in data['contracts']}
    if day <= origin:
        blockers.add('NO_NEW_CABINET_DAY')
    if not data['complete'] or not contracts <= by_contract.keys():
        blockers.add('CALENDAR_SCOPE_UNPROVED')
    if state['running'] or not state['synchronized']:
        blockers.add('TARGET_NOT_STOPPED_AND_SYNCHRONIZED')
    if any(row['position'] or row.get('detached_positions') or row.get('native_conflicts')
           for row in state['symbols'].values()):
        blockers.add('TARGET_OWNERSHIP_UNRESOLVED')
    if any(not terminal(order) or order.get('terminal_conflict') for order in state['orders'].values()):
        blockers.add('TARGET_ORDER_UNRESOLVED')
    # A lost call response, including a crash before writing it, can be
    # superseded by independently received terminal evidence for the child.
    pending = db.execute("SELECT child FROM effects WHERE result IS NULL OR result='UNKNOWN'")
    native_orders = {child: (day, identity) for child, day, identity in db.execute(
        'SELECT child,trading_day,native_id FROM native_orders')}
    for (child,) in pending:
        if child not in state['orders'] or not terminal(state['orders'][child]) or child not in native_orders:
            blockers.add('TARGET_EFFECT_UNRESOLVED')

    current_round = boundary = None
    previous_day = origin
    known_native = {}
    for seq, payload in db.execute("SELECT seq,payload FROM events WHERE kind='ROUND' ORDER BY seq"):
        item = strict_loads(payload)
        frame = item['input']
        observed_day = frame.get('trading_day', previous_day)
        if observed_day != previous_day and observed_day == day:
            boundary = (seq, frame, previous_day)
        previous_day = observed_day
        changed_native = False
        for fact in frame['native']:
            identity = (fact['kind'], fact['child_order_id'], fact['native_order_id'], fact.get('native_trade_id'))
            business = {key: fact[key] for key in ('status', 'filled_qty', 'quantity', 'price') if key in fact}
            if known_native.get(identity) != business:
                changed_native = True
                known_native[identity] = business
        if observed_day != day:
            continue
        current_round = (seq, frame)
        result = item['receipt']
        if (result['effects'] or changed_native or any(
                'target' in d or 'consume' in d for d in result['decisions'])):
            blockers.add('NEW_DAY_ALREADY_USED')
    if (boundary is None or current_round is None or boundary[1].get('synchronized') is not True or
            current_round[1].get('synchronized') is not True or
            not _native_flat(boundary[1], contracts) or not _native_flat(current_round[1], contracts)):
        blockers.add('RESET_NATIVE_FLAT_UNPROVED')
    if boundary and current_round and contracts <= by_contract.keys() and data['complete']:
        _calendar_boundary(boundary, current_round, by_contract, contracts, calendar, day, blockers)

    carry = receipt.get('reset_carry')
    policy = receipt.get('reset_policy')
    if carry is None or policy is None or carry.keys() != contracts:
        blockers.add('LEGACY_CARRY_UNPROVED')
    if boundary and carry is not None and policy is not None:
        moment = datetime.fromisoformat(boundary[1]['wall_time']).timestamp()
        for code, row in state['symbols'].items():
            original = carry.get(code)
            if original is None or _carry(original, day, policy, moment) != _carry(row, day, policy, moment):
                blockers.add('CROSS_DAY_BUSINESS_DIFFERS:' + code)
    for code, row in state['symbols'].items():
        if (row['target_count'] or row['last_signal_bar'] is not None or row['net_loss'] != '0' or row.get('stop_streak') or
                row.get('continuation_attempted') or row['day_stop'] and row.get('month_stop') != day[:7] or
                row.get('night_reference') or row.get('need_hyst') or row.get('open_half_reached')):
            blockers.add('NEW_DAY_STATE_CHANGED:' + code)
    count = _check_allocations(db, state, native_orders, blockers)
    if not count:
        blockers.add('NO_CANDIDATE_TRADES_TO_REHEARSE')
    ledger_check = db.execute('PRAGMA quick_check').fetchone()[0]
    if ledger_check != 'ok':
        blockers.add('TARGET_STORAGE_CHECK_FAILED')
    return boundary, current_round, count


def _check_allocations(db, state, native_orders, blockers):
    allocations = {}
    for position in state['retired_positions'].values():
        if position['owned']:
            blockers.add('TARGET_OWNERSHIP_UNRESOLVED')
        for fill in position['fills']:
            key = tuple(fill['trade_key'])
            if key in allocations:
                blockers.add('TARGET_TRADE_ALLOCATION_UNPROVED')
            allocations[key] = position['id'], fill
    count = 0
    totals = dict.fromkeys(state['orders'], 0)
    for day, native_id, native_trade, payload in db.execute(
            'SELECT trading_day,native_order,native_trade,payload FROM native_trades'):
        fact = strict_loads(payload)
        child = fact['child_order_id']
        order = state['orders'].get(child)
        saved = allocations.pop((day, native_id, native_trade), None)
        if (order is None or native_orders.get(child) != (day, native_id) or saved is None or
                saved[0] != order['position_id'] or saved[1]['trading_day'] != fact.get('fill_day', day) or
                fact['quantity'] != saved[1]['quantity'] or order['effect'] != saved[1]['effect']):
            blockers.add('TARGET_TRADE_ALLOCATION_UNPROVED')
        else:
            totals[child] += fact['quantity']
        count += 1
    if allocations or any(order['filled_qty'] != totals[key] for key, order in state['orders'].items()):
        blockers.add('TARGET_TRADE_ALLOCATION_UNPROVED')
    return count


def _calendar_boundary(boundary, current_round, by_contract, contracts, calendar, day, blockers):
    _, frame, old = boundary
    stamp = frame['wall_time']
    latest = current_round[1]['wall_time']
    if datetime.fromisoformat(latest) < datetime.fromisoformat(stamp):
        blockers.add('RESET_WALL_CLOCK_REGRESSED')
    markets = {row['contract_id']: row for row in frame['market']}
    for code in contracts:
        sessions = by_contract[code]
        labels = sorted({item['trading_day'] for item in sessions})
        next_day = next((label for label in labels if label > old), None)
        observed = calendar.query(code, stamp)
        source = next((item for item in sessions if item['trading_day'] == day), None)
        market = markets.get(code)
        if (old not in labels or next_day != day or source is None or not observed['is_trading'] or
                observed['session']['session_id'] != source['session_id'] or market is None or
                (market['session_open'], market['session_close']) != (source['open_utc'], source['close_utc'])):
            blockers.add('RESET_NOT_FIRST_PROVED_SESSION')
            continue
        end = datetime.fromisoformat(source['close_utc'])
        if datetime.fromisoformat(latest) >= end:
            blockers.add('RESET_REVIEW_WINDOW_ENDED')


def _carry(row, day, policy, moment):
    last = row.get('last_stop') or {}
    cooldown = last.get('until')
    return {'month_stop': day[:7] if row.get('month_stop') == day[:7] else None,
            'stop_days': row.get('stop_days', []),
            'last_trigger': (last['direction'], last['theory']) if last and policy['require_trigger_update'] else None,
            'active_cooldown': (last['direction'], float(cooldown)) if cooldown is not None and
                               policy['stop_cooldown_enabled'] and float(cooldown) > moment else None}


def rehearse(source, candidate, calendar_record, target=None):
    calendar = TradingCalendar.from_record(calendar_record)
    if target is not None and (target.exists() or target.is_symlink()):
        raise TraderError('E_DUPLICATE_CONFLICT', 'Use a new isolated rehearsal directory')
    with rollback_snapshots(source, candidate) as (legacy, db, receipt, state, blockers):
        boundary, current, count = _check_reset(db, state, receipt, calendar, blockers)
        result = {'type': 'DSV_OFFLINE_ROLLBACK_REHEARSAL/1', 'migration_id': receipt['migration_id'],
                  'mode': 'RESET_BOUNDARY_REVIEW', 'eligible': not blockers, 'blockers': sorted(blockers),
                  'cabinet_day': state['trading_day'], 'calendar_hash': calendar.content_hash,
                  'legacy_digest': receipt['source_digest'], 'candidate_digest': _source_digest(db),
                  'archived_trades': str(count), 'reset_event_seq': str(boundary[0]) if boundary else None,
                  'latest_event_seq': str(current[0]) if current else None,
                  'source_restarted': False, 'execution_authority': False}
        if target is not None and result['eligible']:
            result.update(mode='ISOLATED_REHEARSAL')
            _publish(legacy, db, target, result)
    return result


def _publish(legacy, db, target, receipt):
    target = Path(os.path.abspath(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent, prefix='.dsv-rollback-rehearsal-') as location:
        stage = Path(location) / 'rehearsal'
        stage.mkdir()
        for name, original in (('legacy-snapshot.sqlite', legacy), ('candidate-archive.sqlite', db)):
            path = stage / name
            with closing(sqlite3.connect(path)) as copy:
                original.backup(copy)
                if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or _source_digest(copy) != _source_digest(original):
                    raise TraderError('E_DSV_ROLLBACK', 'Rehearsal snapshot verification failed')
            digest = sha256()
            with path.open('rb+') as stream:
                for block in iter(lambda: stream.read(64 * 1024), b''):
                    digest.update(block)
                os.fsync(stream.fileno())
            receipt.setdefault('snapshots', {})[name] = digest.hexdigest()
        receipt['rehearsal_id'] = content_hash(receipt)
        with (stage / 'rollback-receipt.json').open('xb') as stream:
            stream.write(canonical_bytes(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        stage.rename(target)
