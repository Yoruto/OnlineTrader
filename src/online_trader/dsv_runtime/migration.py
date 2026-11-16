"""Read-only legacy-ledger conversion and offline rollback review."""

from contextlib import closing, contextmanager, nullcontext
from hashlib import sha256
from importlib.resources import files
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import decimal_value, identifier
from online_trader.dsv_runtime.legacy_history import selected_native_history
from online_trader.dsv_runtime.runner import _day, _utc, initial_state
from online_trader.dsv_storage.journal import Journal, _APPLICATION_ID
from online_trader.errors import TraderError
from online_trader.kernel.statistics import strategy_open_counts
from online_trader.ledger.dsv_rollback import EVENT as REVERSE_EVENT
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.instance import AccountLock
from online_trader.strategy.entry import is_night_session


_FIELDS = {'type', 'source_account_key', 'source_environment', 'source_broker_identity',
           'source_event_seq', 'source_release', 'native_flat', 'session'}
RECOVERY_EVENT = 'SIMNOW_ARCHIVED_FLAT_REVIEWED'
_COLD_CHECKS = {
    'LEGACY_POSITION_OPEN': "SELECT 1 FROM position_cycles WHERE remaining_qty != 0 OR lifecycle != 'CLOSED'",
    'LEGACY_INTENT_PENDING': "SELECT 1 FROM order_intents WHERE status NOT IN ('DONE','ABORTED')",
    'LEGACY_ORDER_UNCONFIRMED': """SELECT 1 FROM child_orders c WHERE
        terminal_evidence_json IS NULL OR
        json_extract(terminal_evidence_json,'$.confirmed_fill_qty') IS NOT confirmed_fill_qty OR
        (broker_state NOT IN ('FILLED','CANCELLED','REJECTED') AND NOT (
            broker_state='NOT_SEEN' AND confirmed_fill_qty=0 AND
            EXISTS (SELECT 1 FROM send_attempts WHERE child_order_id=c.child_order_id
                    AND send_state='DEFINITELY_NOT_SENT') AND
            NOT EXISTS (SELECT 1 FROM send_attempts WHERE child_order_id=c.child_order_id
                        AND send_state!='DEFINITELY_NOT_SENT'))) OR
        (SELECT COALESCE(SUM(f.quantity),0) FROM fill_facts f LEFT JOIN broker_order_links l USING(broker_order_key)
         WHERE l.child_order_id=c.child_order_id OR
               (f.broker_order_key=c.child_order_id AND json_extract(f.raw_fact_json,'$.child_order_id')=c.child_order_id))
         != confirmed_fill_qty""",
    'LEGACY_SEND_UNKNOWN': "SELECT 1 FROM send_attempts WHERE send_state NOT IN ('DEFINITELY_NOT_SENT','CONFIRMED')",
    'LEGACY_CANCEL_PENDING': "SELECT 1 FROM cancel_actions WHERE status NOT IN ('CONFIRMED','REJECTED','NOT_NEEDED')",
    'LEGACY_RESERVATION_PENDING': "SELECT 1 FROM reservations WHERE status IN ('HELD','UNKNOWN')",
    'LEGACY_RECONCILIATION_PENDING': """SELECT 1 FROM reconciliation_differences WHERE status='OPEN'
        UNION ALL SELECT 1 FROM reconciliation_runs WHERE status='IN_PROGRESS'""",
    'LEGACY_UNALLOCATED_FILLS': """SELECT 1 FROM fill_facts f WHERE f.quantity !=
        (SELECT COALESCE(SUM(a.quantity),0) FROM fill_allocations a WHERE a.fill_id=f.fill_id)""",
}
_MAPPED = ['target_count', 'last_signal_bar', 'last_order_ms', 'last_order_direction', 'last_stop',
           'net_loss', 'day_stop', 'day_stop_reason', 'stop_streak', 'stop_days', 'month_stop', 'night_reference',
           'continuation_attempted', 'continuation_stopped', 'legacy_statistics']


@contextmanager
def _readonly(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db


def _source_schema(db):
    ddl = files('online_trader').joinpath('resources/schema.sql').read_text(encoding='utf-8')
    query = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    with closing(sqlite3.connect(':memory:')) as reference:
        reference.executescript(ddl)
        if list(map(tuple, db.execute(query))) != list(reference.execute(query)):
            raise TraderError('E_DSV_MIGRATION_SCHEMA', 'Legacy ledger schema differs from the supported source')
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
        raise TraderError('E_DSV_MIGRATION_SCHEMA', 'Legacy ledger failed SQLite integrity checks')


def _source_digest(db):
    digest = sha256()
    for line in db.iterdump():
        digest.update(line.encode('utf-8') + b'\n')
    return digest.hexdigest()


def _selected_native_history(db, contracts):
    return selected_native_history(db, contracts, recovery_event=RECOVERY_EVENT)


def _business_state(state):
    volatile = {'tick', 'tick_session', 'quote_clock'}
    return {'day': state['trading_day'], 'orders': state['orders'], 'retired': state['retired_positions'],
            'symbols': {key: {k: v for k, v in row.items() if k not in volatile}
                        for key, row in state['symbols'].items()}}


def _scope(manifest):
    if type(manifest) is not dict or manifest.keys() != _FIELDS or manifest['type'] != 'legacy-dsv-migration/1':
        raise TraderError('E_DSV_MIGRATION', 'Expected a versioned legacy migration manifest')
    identifier(manifest['source_account_key'])
    if not re.fullmatch('[0-9a-f]{64}', manifest['source_release']):
        raise TraderError('E_DSV_MIGRATION', 'Source release needs a SHA-256 identifier')
    if not re.fullmatch('0|[1-9][0-9]{0,18}', manifest['source_event_seq']):
        raise TraderError('E_DSV_MIGRATION', 'Source event watermark must be canonical')
    header = manifest['session']
    if (type(header) is not dict or set(header) - {'type', 'account_key', 'provider', 'trading_day', 'profile'}
            or header.get('type', 'session') != 'session'):
        raise TraderError('E_DSV_MIGRATION', 'Expected a bound destination session')
    profile = DsvProfile.from_record(header['profile'])
    day = _day(header['trading_day'])
    scope = {key: header[key] for key in ('account_key', 'provider', 'trading_day')}
    identifier(scope['account_key'])
    sim_prefix = {'TQKQ': 'SIM:KQ:', 'TQ_SIMNOW': 'SIM:SIMNOW:'}.get(scope['provider'])
    if sim_prefix:
        if (scope['account_key'] != manifest['source_account_key'] or not scope['account_key'].startswith(sim_prefix)
                or manifest['source_environment'] != 'ONLINE_SIM' or manifest['source_broker_identity'] != scope['provider']):
            raise TraderError('E_DSV_MIGRATION', 'Simulation migration must preserve the original account binding')
    elif (scope['provider'] != 'OFFLINE' or not scope['account_key'].startswith('DEV:')
          or scope['account_key'] == manifest['source_account_key']):
        raise TraderError('E_DSV_MIGRATION', 'Use a distinct development identity for offline rehearsal')
    scope['configuration_id'] = profile.content_hash
    return scope, profile


def _manual_profile(db, data, account, day):
    descriptor = data.get('online_scope', {})
    try:
        grant = DsvSimulationGrant.parse(canonical_bytes(descriptor['grant']))
    except (KeyError, TypeError, TraderError):
        return None
    if not grant.manual_workbench or grant.account_key != account or not grant.permits_day(day):
        return None
    approval = db.execute('SELECT manifest_hash FROM approval_manifests WHERE approval_record_id=?',
                          (grant.value['grant_id'],)).fetchone()
    if approval is not None and approval[0] == descriptor.get('grant_hash') == grant.content_hash:
        return grant.value['profile_hash']
    return None


def _manual_observation_only(data, entry, account, manual_profile):
    report = data.get('report', {})
    if (manual_profile is None or entry.get('profile_hash') != manual_profile
            or entry.get('account_key') != account or report.get('profile_hash') != manual_profile
            or report.get('status') != 'OBSERVED' or report.get('child_order_id') is not None
            or report.get('candidates') != [] or 'MANUAL_WORKBENCH_SCOPE' not in report.get('reasons', [])):
        return False
    for field in ('first_minute_blocks', 'hysteresis_armed', 'last_ordinary_attempt', 'last_signal_bar',
                  'last_trigger_prices', 'need_hyst', 'night_close_fallback_cleared', 'night_reference',
                  'open_half_reached', 'unproved_consumed_bar'):
        value = entry.get(field)
        has_state = any(value.values()) if type(value) is dict else bool(value)
        if has_state:
            return False
    return True


def _material(db, state, scope, manifest, profile, blockers):
    day = state['trading_day']
    keys = set(state['symbols'])
    cycles, entries, stops, statistics, stop_receipts = {}, {}, {}, {}, {}
    closed_statistics = set()
    state_evidence = set()
    archived = {}
    manual_profile = None
    account = manifest['source_account_key']
    observation_proposals = {row[0] for row in db.execute("""
        SELECT recorded_event_seq FROM strategy_proposals GROUP BY recorded_event_seq
        HAVING COUNT(*) = 1 AND MAX(proposal_kind='STATE_UPDATE' AND status='COMMITTED' AND intent_id IS NULL)=1
    """)}
    for record in db.execute('SELECT event_seq,event_type,payload_json,payload_hash FROM event_journal ORDER BY event_seq'):
        raw = record['payload_json'].encode('utf-8')
        if sha256(raw).hexdigest() != record['payload_hash']:
            raise TraderError('E_DSV_MIGRATION_SCHEMA', 'Legacy journal payload hash is inconsistent')
        data = strict_loads(raw)['data']
        if record['event_type'] == 'ONLINE_SIMULATION_SCOPE_COMMITTED':
            manual_profile = _manual_profile(db, data, account, day)
        if record['event_type'] == REVERSE_EVENT:
            blockers.add('LEGACY_REVERSE_HANDOFF_REQUIRES_REVIEW')
        cycle = data.get('cycle')
        if cycle:
            cycles[cycle['position_cycle_id']] = cycle
        entry = data.get('dsv_strategy_state')
        stop = data.get('dsv_strategy_stops')
        if cycle and record['event_type'] == 'ENTRY_PREPARED':
            metadata = cycle.get('strategy_metadata') or {}
            entry = metadata.get('strategy_state') or entry
            stop = metadata.get('stop_result') or stop
        if (entry and entry['contract_id'] in keys and entry['trading_day'] == day
                and record['event_type'] == 'DSV_STRATEGY_OBSERVED'
                and record['event_seq'] in observation_proposals
                and _manual_observation_only(data, entry, account, manual_profile)):
            code = entry['contract_id']
            seq = str(record['event_seq'])
            item = archived.setdefault(code, {'count': 0, 'first_event_seq': seq})
            item.update(count=item['count'] + 1, last_event_seq=seq)
            entry = None
        if entry and entry['contract_id'] in keys and entry['trading_day'] == day:
            state_evidence.add((entry['contract_id'], 'ENTRY'))
            if entry['account_key'] != manifest['source_account_key'] or entry['profile_hash'] != scope['configuration_id']:
                blockers.add('LEGACY_ENTRY_PROFILE_MISMATCH')
            if entry.get('last_signal_bar') is not None and entry.get('consumption_version') != 1:
                blockers.add('LEGACY_BAR_CONSUMPTION_UNPROVED')
            if entry.get('coverage_lost') or entry.get('unproved_consumed_bar') is not None:
                blockers.add('LEGACY_OBSERVATION_COVERAGE_UNKNOWN')
            entries[entry['contract_id']] = entry
        update = data.get('nominal_statistics')
        if update:
            value, receipt = update['state'], update['receipt']
            code, dated = value['contract_id'], value['trading_day']
            if value['account_key'] != manifest['source_account_key']:
                blockers.add('LEGACY_STATISTICS_SCOPE_MISMATCH')
            if code in keys and dated <= day:
                statistics[(code, dated)] = value
                stop = update['stop_result']
                if receipt.get('exit_reason') == 'STOP_LOSS':
                    stop_receipts[code] = receipt
                if receipt['type'] == 'CYCLE_CLOSED':
                    closed_statistics.add(receipt['position_cycle_id'])
                if dated == day and receipt['profile_hash'] != scope['configuration_id']:
                    blockers.add('LEGACY_STATISTICS_PROFILE_MISMATCH')
        if stop:
            value = stop['state']
            key = value['contract_id'], value['trading_day']
            if key[0] in keys and key[1] <= day:
                stops[key] = value
    projections = {row['position_cycle_id']: row for row in db.execute(
        'SELECT position_cycle_id,contract_id,trading_day,lifecycle,confirmed_open_qty FROM position_cycles')}
    for identity, row in projections.items():
        cycle = cycles.get(identity)
        if cycle is None or row['lifecycle'] != cycle['lifecycle']:
            blockers.add('LEGACY_CYCLE_PROJECTION_UNPROVED')
        elif row['contract_id'] in keys and row['trading_day'] == day and cycle.get('entry_origin') == 'STRATEGY':
            candidate = (cycle.get('strategy_metadata') or {}).get('candidate', {})
            if candidate.get('profile_hash') != scope['configuration_id'] or row['contract_id'] not in entries:
                blockers.add('LEGACY_TARGET_PROFILE_UNPROVED')
            if row['confirmed_open_qty'] and identity not in closed_statistics:
                blockers.add('LEGACY_STATISTICS_UNPROVED')
    if set(cycles) - set(projections):
        blockers.add('LEGACY_CYCLE_PROJECTION_UNPROVED')
    counts = strategy_open_counts(cycles.values(), day)
    trade = profile.to_record()['settings']['trade']
    for code, row in state['symbols'].items():
        row['target_count'] = counts.get(code, 0)
        entry = entries.get(code, {})
        row['last_signal_bar'] = entry.get('last_signal_bar')
        if entry.get('first_minute_blocks') or entry.get('need_hyst') or any(entry.get('hysteresis_armed', {}).values()):
            blockers.add('LEGACY_ACTIVE_HYSTERESIS_UNMAPPED')
        if night := entry.get('night_reference'):
            row['night_reference'] = {'native_time': night['source_time_utc'], 'high': night['high'],
                                      'low': night['low'], 'last': night['last'], 'complete': night['boundary_proved']}
        if attempt := entry.get('last_ordinary_attempt'):
            row.update(last_order_direction=attempt['direction'],
                       last_order_ms=str(int(_utc(attempt['at_utc']).timestamp() * 1000)))
        same_day_cycles = [cycle for cycle in cycles.values() if cycle['plan']['contract_id'] == code
                           and cycle['plan']['trading_day'] == day]
        row['continuation_attempted'] = any((cycle.get('strategy_metadata') or {}).get('candidate', {}).get(
            'entry_kind') == 'CONTINUATION' for cycle in same_day_cycles)
        if row['night_reference'] and not entry.get('night_close_fallback_cleared'):
            exits = [cycle for cycle in same_day_cycles if (cycle.get('nominal_exit') or {}).get('exit_reason') == 'SESSION_CLOSE']
            if exits:
                dated_exits = [(_utc(cycle['nominal_exit']['trigger_clock']['now_utc']), cycle) for cycle in exits]
                night_exits = [(at, cycle) for at, cycle in dated_exits if is_night_session(at)]
                if night_exits:
                    row['night_reference']['fallback'] = max(night_exits, key=lambda item: item[0])[1]['plan']['direction']
        if stat := statistics.get((code, day)):
            row['legacy_statistics'] = {key: stat[key] for key in ('daily_pnl_points', 'stop_count', 'profit_count',
                                                                    'trade_count', 'total_volume', 'day_stopped_reason')}
            row.update(net_loss=stat['net_loss_points'], stop_streak=stat['stop_streak'],
                       day_stop=stat['day_stopped'], day_stop_reason=stat['day_stopped_reason'],
                       continuation_stopped=stat['continuation_stopped'])
            decimal_value(row['net_loss'])
        history = [saved for (other, _), saved in stops.items() if other == code]
        row['stop_days'] = sorted({date for saved in history for date in saved['stop_days']})[-12:]
        if any(saved['month_stop'] == day[:7] for saved in history):
            row['month_stop'] = day[:7]
        if (code, day) in stops:
            stop_state = stops[(code, day)]
            row['day_stop'] |= stop_state['day_stopped']
            row['day_stop_reason'] = stop_state['day_stopped_reason']
            row.update(net_loss=stop_state['net_loss_points'], stop_streak=stop_state['stop_streak'],
                       continuation_stopped=stop_state['continuation_stopped'])
            decimal_value(row['net_loss'])
        latest_stats = max((date for other, date in statistics if other == code), default=None)
        if stop := stop_receipts.get(code):
            stat = statistics[(code, latest_stats)]
            direction = stop['direction']
            until = stat['cooldown_until'][direction]
            if until is not None or trade['require_trigger_update']:
                row['last_stop'] = {'direction': direction, 'theory': stop['theoretical_entry_price'],
                                    'until': str(_utc(until).timestamp()) if until else None}
    for block in db.execute('SELECT scope_key,reason_code FROM day_blocks WHERE trading_day=? AND status=\'ACTIVE\'', (day,)):
        code, reason = block
        if (reason == 'PROTECTION_LOCK' and code in state['symbols'] and
                ((code, 'ENTRY') in state_evidence or (code, day) in statistics)):
            state['symbols'][code].update(day_stop=True, day_stop_reason=reason)
        elif code in keys or code == manifest['source_account_key']:
            blockers.add('LEGACY_DAY_BLOCKS_UNMAPPED')
    return archived


def migrate(source, manifest, target=None):
    scope, profile = _scope(manifest)
    if target is not None and (target.exists() or target.is_symlink()):
        raise TraderError('E_DUPLICATE_CONFLICT', 'Migration needs a new destination file')
    state = initial_state(scope['trading_day'], [row['code'] for row in profile.to_record()['settings']['symbols']])
    state['synchronized'] = False
    blockers = set()
    with AccountLock(manifest['source_account_key']), _readonly(source) as db:
        _source_schema(db)
        source_binding = db.execute('SELECT account_key,environment,broker_identity FROM ledger_scope WHERE singleton=1').fetchone()
        if source_binding is None or tuple(source_binding) != tuple(manifest[key] for key in (
                'source_account_key', 'source_environment', 'source_broker_identity')):
            raise TraderError('E_DSV_MIGRATION_SCOPE', 'Legacy ledger scope does not match the request')
        watermark = str(db.execute('SELECT COALESCE(MAX(event_seq),0) FROM event_journal').fetchone()[0])
        if watermark != manifest['source_event_seq']:
            blockers.add('LEGACY_WATERMARK_CHANGED')
        for reason, query in _COLD_CHECKS.items():
            if db.execute(query).fetchone():
                blockers.add(reason)
        native = manifest['native_flat']
        expected = [{'contract_id': code, 'long_qty': 0, 'short_qty': 0, 'active_orders': 0, 'unknown_results': 0}
                    for code in state['symbols']]
        if (type(native) is not list or sorted(map(content_hash, native)) != sorted(map(content_hash, expected))):
            blockers.add('NATIVE_FLAT_SCOPE_UNPROVED')
        if db.execute('SELECT 1 FROM business_counters WHERE trading_day>=?', (scope['trading_day'],)).fetchone():
            blockers.add('LEGACY_BUSINESS_COUNTERS_UNMAPPED')
        if db.execute("SELECT 1 FROM day_blocks WHERE trading_day>? AND status='ACTIVE'", (scope['trading_day'],)).fetchone():
            blockers.add('LEGACY_DAY_BLOCKS_UNMAPPED')
        if db.execute('SELECT 1 FROM position_cycles WHERE trading_day > ?', (scope['trading_day'],)).fetchone():
            blockers.add('LEGACY_NEWER_TRADING_DAY')
        archived = _material(db, state, scope, manifest, profile, blockers)
        receipt = {'type': 'DSV_OFFLINE_MIGRATION/1', 'source_schema': '1', 'target_schema': '2',
                   'source_release': manifest['source_release'], 'source_event_seq': watermark,
                   'source_digest': _source_digest(db), 'source_account_hash': content_hash(manifest['source_account_key']),
                   'target_scope': scope, 'target_state_digest': content_hash(state),
                   'target_business_digest': content_hash(_business_state(state)), 'native_digest': content_hash(native),
                   'mapped_fields': _MAPPED, 'archive_only': ['fills', 'fees', 'closed_cycles', 'logs', 'execution_authority'],
                   'historical_fills': str(db.execute('SELECT COUNT(*) FROM fill_facts').fetchone()[0])}
        if archived:
            receipt['archived_manual_observations'] = archived
        receipt['reset_carry'] = {
            code: {key: row.get(key) for key in ('last_stop', 'month_stop', 'stop_days')}
            for code, row in state['symbols'].items()}
        profile_trade = profile.to_record()['settings']['trade']
        receipt['reset_policy'] = {key: profile_trade[key] for key in ('require_trigger_update', 'stop_cooldown_enabled')}
        if scope['provider'] != 'OFFLINE':
            receipt['legacy_native_orders'] = _selected_native_history(db, set(state['symbols']))
        receipt['migration_id'] = content_hash(receipt)
        receipt['blockers'] = sorted(blockers)
        receipt['mode'] = 'PREFLIGHT'
        if target is not None and not blockers:
            if scope['account_key'] == manifest['source_account_key']:
                _publish(target, scope, state, receipt)
            else:
                with AccountLock(scope['account_key']):
                    _publish(target, scope, state, receipt)
            receipt['mode'] = 'IMPORTED'
        return receipt


def _publish(target, scope, state, receipt):
    target = Path(os.path.abspath(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent, prefix='.dsv-migration-') as directory:
        temporary = Path(directory) / 'ledger.sqlite'
        with closing(Journal(temporary, scope, state)) as journal, journal.transaction():
            journal.event('LEGACY_MIGRATION', receipt | {'mode': 'IMPORTED'})
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise TraderError('E_DUPLICATE_CONFLICT', 'Migration destination already exists') from error


@contextmanager
def rollback_snapshots(source, target):
    with _readonly(target) as db:
        receipt = read_migration_receipt(db)
    scope = receipt['target_scope']
    blockers = set()
    with _readonly(source) as legacy:
        _source_schema(legacy)
        account = legacy.execute('SELECT account_key FROM ledger_scope WHERE singleton=1').fetchone()
    if account is None or content_hash(account[0]) != receipt['source_account_hash']:
        raise TraderError('E_DSV_MIGRATION_SCOPE', 'Legacy account differs from the conversion receipt')
    with (AccountLock(account[0]),
          nullcontext() if account[0] == scope['account_key'] else AccountLock(scope['account_key']),
          _readonly(target) as db, _readonly(source) as legacy):
        _source_schema(legacy)
        if _source_digest(legacy) != receipt['source_digest']:
            blockers.add('SOURCE_CHANGED')
        repeat = db.execute("SELECT payload FROM events WHERE seq=1 AND kind='LEGACY_MIGRATION'").fetchone()
        if repeat is None or strict_loads(repeat[0]) != receipt:
            raise TraderError('E_DSV_ROLLBACK', 'The target conversion receipt changed during review')
        if strict_loads(db.execute('SELECT payload FROM scope').fetchone()[0]) != scope:
            blockers.add('TARGET_SCOPE_CHANGED')
        state = strict_loads(db.execute('SELECT payload FROM checkpoint').fetchone()[0])
        yield legacy, db, receipt, state, blockers


def read_migration_receipt(db):
    signature = (db.execute('PRAGMA application_id').fetchone()[0], db.execute('PRAGMA user_version').fetchone()[0])
    if signature != (_APPLICATION_ID, 2):
        raise TraderError('E_DSV_ROLLBACK', 'Expected a migrated DSV fact store')
    found = db.execute("SELECT payload FROM events WHERE seq=1 AND kind='LEGACY_MIGRATION'").fetchone()
    if not found:
        raise TraderError('E_DSV_ROLLBACK', 'Target has no legacy conversion receipt')
    receipt = strict_loads(found[0])
    identity = {key: value for key, value in receipt.items() if key not in {'blockers', 'mode', 'migration_id'}}
    if receipt['mode'] != 'IMPORTED' or receipt['blockers'] or content_hash(identity) != receipt['migration_id']:
        raise TraderError('E_DSV_ROLLBACK', 'Target conversion receipt is inconsistent')
    return receipt


def rollback_review(source, target):
    with rollback_snapshots(source, target) as (_, db, receipt, state, blockers):
        if content_hash(_business_state(state)) != receipt['target_business_digest']:
            blockers.add('TARGET_BUSINESS_CHANGED')
        if any(db.execute(f'SELECT 1 FROM {table}').fetchone() for table in ('effects', 'native_orders', 'native_trades')):
            blockers.add('TARGET_EFFECT_OR_NATIVE_FACT')
        for (payload,) in db.execute("SELECT payload FROM events WHERE kind='ROUND'"):
            decisions = strict_loads(payload)['receipt']['decisions']
            if any('consume' in decision or 'target' in decision for decision in decisions):
                blockers.add('TARGET_BUSINESS_CONSUMED')
    return {'type': 'DSV_OFFLINE_ROLLBACK_REVIEW/1', 'migration_id': receipt['migration_id'],
            'eligible': not blockers, 'blockers': sorted(blockers)}
