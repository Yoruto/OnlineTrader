"""Cross-day account facts read from an immutable prefix of the original ledger."""

from contextlib import closing, contextmanager
from datetime import date, timedelta
from pathlib import Path
import re
import sqlite3
import time

from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import identifier, sequence
from online_trader.errors import TraderError


SECTIONS = ('coverage', 'funds', 'positions', 'orders', 'trades', 'owned-trades')
_FILTERABLE = {'positions', 'orders', 'trades', 'owned-trades'}
_FIELDS = {
    'funds': ('balance', 'pre_balance', 'available', 'margin', 'frozen_margin', 'close_profit',
              'position_profit', 'float_profit', 'commission', 'risk_ratio'),
    'positions': ('contract_id', 'long', 'short'),
    'orders': ('order_id', 'contract_id', 'direction', 'offset', 'quantity', 'remaining', 'status', 'limit_price'),
    'trades': ('trade_id', 'order_id', 'contract_id', 'direction', 'offset', 'quantity', 'price', 'source_time_ns'),
}
_CURSOR = re.compile(r'(0|[1-9][0-9]{0,18})\.([1-9][0-9]{0,8})\.([a-f0-9]{64})\Z')

_OBSERVATIONS = '''WITH snapshot_events AS (
    SELECT event_seq, observed_at_utc, event_type,
           CASE WHEN event_type = 'ONLINE_DSV_STOP_VERIFIED'
                THEN json_extract(payload_json, '$.data.final_account_observation')
                ELSE json_extract(payload_json, '$.data.snapshot') END AS snapshot
    FROM event_journal
    WHERE event_seq <= :checkpoint AND event_type IN (
        'ONLINE_SIMULATION_BASELINE', 'ONLINE_DSV_ACCOUNT_REFRESH', 'ONLINE_FINAL_SNAPSHOT',
        'ONLINE_DSV_FINAL_RELOGIN_VERIFIED', 'ONLINE_FINAL_RELOGIN_VERIFIED', 'ONLINE_DSV_STOP_VERIFIED')
), observations AS (
    SELECT *, json_extract(snapshot, '$.trading_day') AS day, json_extract(snapshot, '$.account_key') AS account,
           json_extract(snapshot, '$.gateway_identity') AS gateway, json_extract(snapshot, '$.source_epoch') AS epoch,
           json_type(snapshot, '$.complete') = 'true' AS complete
    FROM snapshot_events
    WHERE json_extract(snapshot, '$.trading_day') BETWEEN :from_day AND :to_day
)'''

_COVERAGE = _OBSERVATIONS + '''
    SELECT day, count(*) AS observations, sum(CASE WHEN complete THEN 1 ELSE 0 END) AS complete_observations,
           max(event_seq) AS observation_seq, max(CASE WHEN complete THEN event_seq END) AS complete_seq,
           max(CASE WHEN account IS NOT :account_key OR typeof(gateway) != 'text' OR gateway = ''
                         OR typeof(epoch) != 'integer' OR epoch < 1 THEN 1 ELSE 0 END) AS mismatched,
           max(CASE WHEN complete AND (json_type(snapshot, '$.funds') IS NOT 'object'
                         OR json_type(snapshot, '$.positions') IS NOT 'array'
                         OR json_type(snapshot, '$.orders') IS NOT 'array'
                         OR json_type(snapshot, '$.trades') IS NOT 'array') THEN 1 ELSE 0 END) AS malformed
    FROM observations GROUP BY day'''

_STOP_COVERAGE = '''
    SELECT json_extract(payload_json, '$.data.trading_day') AS day, max(event_seq) AS stop_seq
    FROM event_journal
    WHERE event_seq <= :checkpoint AND event_type = 'ONLINE_DSV_STOP_VERIFIED'
      AND json_extract(payload_json, '$.data.trading_day') BETWEEN :from_day AND :to_day
      AND json_type(payload_json, '$.data.positions') = 'integer'
      AND json_extract(payload_json, '$.data.positions') = 0
      AND json_type(payload_json, '$.data.active_orders') = 'integer'
      AND json_extract(payload_json, '$.data.active_orders') = 0
      AND json_type(payload_json, '$.data.native_snapshot_id') = 'text'
      AND json_extract(payload_json, '$.data.native_snapshot_id') != ''
    GROUP BY day'''

_OWNED_COVERAGE = '''
    SELECT trading_day AS day, count(*) AS fill_count
    FROM fill_facts
    WHERE recorded_event_seq <= :checkpoint AND trading_day BETWEEN :from_day AND :to_day
    GROUP BY trading_day'''

_LAST_COMPLETE = _OBSERVATIONS + ''', daily AS (
    SELECT *, row_number() OVER (PARTITION BY day ORDER BY event_seq DESC) AS rank
    FROM observations WHERE complete
)'''

_FUNDS = _LAST_COMPLETE + '''
    SELECT day, gateway, epoch, event_seq, observed_at_utc, event_type, event_seq AS first_seq,
           'FUNDS' AS record_id, 0 AS conflict, json_extract(snapshot, '$.funds') AS material
    FROM daily WHERE rank = 1 ORDER BY day LIMIT :length OFFSET :offset'''

_POSITIONS = _LAST_COMPLETE + '''
    SELECT day, gateway, epoch, event_seq, observed_at_utc, event_type, event_seq AS first_seq,
           json_extract(native.value, '$.contract_id') AS record_id, 0 AS conflict, native.value AS material
    FROM daily CROSS JOIN json_each(daily.snapshot, '$.positions') AS native
    WHERE rank = 1 AND (:contract IS NULL OR record_id = :contract)
    ORDER BY day, record_id LIMIT :length OFFSET :offset'''

_NATIVE_ROWS = _OBSERVATIONS + ''', native_rows AS (
    SELECT day, gateway, epoch, event_seq, observed_at_utc, event_type,
           json_extract(native.value, :identity) AS record_id, native.key AS row_number,
           native.value AS material, json_extract(native.value, '$.contract_id') AS contract_id
    FROM observations CROSS JOIN json_each(observations.snapshot, :collection) AS native
    WHERE complete
), ranked AS (
    SELECT *, row_number() OVER (identity ORDER BY event_seq DESC, row_number DESC) AS rank,
           min(event_seq) OVER identity AS first_seq,
           CASE WHEN :section = 'trades' THEN min(material) OVER identity != max(material) OVER identity
                ELSE 0 END AS conflict,
           max(:contract IS NULL OR contract_id = :contract) OVER identity AS matches_contract
    FROM native_rows
    WINDOW identity AS (PARTITION BY day, gateway, record_id)
)
    SELECT * FROM ranked WHERE rank = 1 AND matches_contract
    ORDER BY day, gateway, record_id LIMIT :length OFFSET :offset'''

_OWNED_ROWS = '''
    SELECT f.trading_day AS day, f.gateway_identity AS gateway, f.source_epoch AS epoch,
           f.recorded_event_seq AS event_seq, e.observed_at_utc, e.event_type, f.recorded_event_seq AS first_seq,
           f.fill_id AS record_id, 0 AS conflict,
           json_object('fill_id', f.fill_id, 'trade_key', f.trade_key, 'child_order_id', f.broker_order_key,
                       'contract_id', f.contract_id, 'direction', f.side, 'offset_kind', f.offset_kind,
                       'quantity', f.quantity, 'price', f.price,
                       'native_trade_id', json_extract(f.raw_fact_json, '$.native_trade_id')) AS material
    FROM fill_facts AS f JOIN event_journal AS e ON e.event_seq = f.recorded_event_seq
    WHERE f.recorded_event_seq <= :checkpoint AND f.trading_day BETWEEN :from_day AND :to_day
      AND (:contract IS NULL OR f.contract_id = :contract)
    ORDER BY day, gateway, record_id LIMIT :length OFFSET :offset'''


def _date(value):
    try:
        if type(value) is str and len(value) == 10:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
    except ValueError:
        pass
    raise TraderError('E_HISTORY_DATE', 'Use a calendar date in YYYY-MM-DD form')


def validate_day_query(from_day, to_day, section, contract, cursor, limit):
    first, last = _date(from_day), _date(to_day)
    if not 0 <= (last - first).days < 366:
        raise TraderError('E_HISTORY_DATE', 'Choose an ordered date range of at most 366 calendar days')
    if section not in SECTIONS or type(limit) is not int or not 1 <= limit <= 200:
        raise TraderError('E_SCHEMA', 'Choose a supported history section and between 1 and 200 records')
    if contract is not None:
        if section not in _FILTERABLE:
            raise TraderError('E_SCHEMA', 'This history section has no contract filter')
        identifier(contract)
    if cursor is not None and (type(cursor) is not str or len(cursor) > 95 or _CURSOR.fullmatch(cursor) is None):
        raise TraderError('E_HISTORY_CURSOR', 'The history cursor is invalid')
    return first, last


def _page_boundary(db, values, ledger_created, cursor):
    maximum = db.execute('SELECT coalesce(max(event_seq), 0) FROM event_journal').fetchone()[0]
    checkpoint, offset, expected = maximum, 0, None
    if cursor is not None:
        matched = _CURSOR.fullmatch(cursor)
        checkpoint, offset, expected = sequence(matched[1]), int(matched[2]), matched[3]
        if checkpoint > maximum:
            raise TraderError('E_HISTORY_CURSOR', 'The history cursor exceeds this ledger checkpoint')
    prefix = db.execute('SELECT tx_id, payload_hash FROM event_journal WHERE event_seq = ?', (checkpoint,)).fetchone()
    if checkpoint and prefix is None:
        raise TraderError('E_HISTORY_CURSOR', 'The history cursor checkpoint no longer exists')
    digest = content_hash(values | {'ledger_created_at': ledger_created, 'checkpoint': str(checkpoint),
                                    'checkpoint_identity': list(prefix) if prefix else None})
    if expected is not None and expected != digest:
        raise TraderError('E_HISTORY_CURSOR', 'The history cursor belongs to another ledger or query')
    return checkpoint, offset, digest


def _seq(value):
    return str(value) if value is not None else None


@contextmanager
def _history_reader_gate(account_key):
    """Serialize history readers before probing the exclusive execution-owner lock."""
    guard = None
    try:
        from online_trader.runtime.instance import AccountLock
        deadline = time.monotonic() + 5
        while True:
            try:
                guard = AccountLock('HISTORY-READ:' + account_key)
                break
            except TraderError as error:
                if error.code == 'E_PLATFORM':
                    break
                if error.code != 'E_INSTANCE_EXISTS':
                    raise
                if time.monotonic() >= deadline:
                    raise TraderError('E_BUSY', 'Another account history reader is still active') from None
                time.sleep(.025)
        yield
    finally:
        if guard is not None:
            guard.close()


@contextmanager
def history_database(path, account_key):
    """Use a side-effect-free cold read without weakening a live WAL reader."""
    lease = None
    with _history_reader_gate(account_key):
        try:
            try:
                from online_trader.runtime.instance import AccountLock
                lease = AccountLock(account_key)
            except TraderError:
                # A live execution owner keeps SQLite's normal WAL locking. History
                # readers themselves are serialized above, so reader-reader
                # contention cannot be misclassified as an execution owner.
                lease = None
            suffix = '?mode=ro'
            if (lease is not None and not path.with_name(path.name + '-wal').exists()
                    and not path.with_name(path.name + '-shm').exists()):
                # The account lock prevents an owner starting while this checkpointed
                # cold ledger is open. Immutable mode avoids creating empty WAL/SHM
                # coordination files and preserves the source directory byte-for-byte.
                suffix += '&immutable=1'
            with closing(sqlite3.connect(path.as_uri() + suffix, uri=True, timeout=.5)) as db:
                yield db
        finally:
            if lease is not None:
                lease.close()


def _coverage(db, params, first, last):
    daily = {}
    for row in db.execute(_COVERAGE, params):
        if row['mismatched']:
            raise TraderError('E_SCOPE', 'An account observation does not match the selected ledger identity')
        if row['malformed']:
            raise TraderError('E_STORAGE', 'A complete account observation lacks a native data section')
        _date(row['day'])
        daily[row['day']] = row
    stops = {row['day']: row['stop_seq'] for row in db.execute(_STOP_COVERAGE, params)}
    owned = {row['day']: row['fill_count'] for row in db.execute(_OWNED_COVERAGE, params)}
    coverage = []
    for offset in range((last - first).days + 1):
        day = (first + timedelta(days=offset)).isoformat()
        row = daily.get(day)
        observed = row['observations'] if row is not None else 0
        complete = row['complete_observations'] if row is not None else 0
        stop = stops.get(day)
        fill_count = owned.get(day, 0)
        status = ('OBSERVED_PARTIAL' if complete else 'OBSERVED_INCOMPLETE' if observed else
                  'LEDGER_FACTS_ONLY' if stop or fill_count else 'NOT_OBSERVED')
        coverage.append({'trading_day': day, 'status': status, 'complete_day': False,
                         'observation_count': observed, 'complete_observation_count': complete,
                         'latest_observation_event_seq': _seq(row['observation_seq']) if row is not None else None,
                         'latest_complete_event_seq': _seq(row['complete_seq']) if row is not None else None,
                         'verified_flat_stop_event_seq': _seq(stop), 'owned_trade_count': fill_count})
    return coverage


def _history_record(row, section):
    if type(row['record_id']) is not str or not row['record_id']:
        raise TraderError('E_STORAGE', 'The original history record has no native identity')
    fact = None
    if not row['conflict']:
        try:
            value = strict_loads(row['material'])
            if type(value) is not dict:
                raise ValueError()
        except ValueError:
            raise TraderError('E_STORAGE', 'The original history record has invalid fields') from None
        fact = {key: value.get(key) for key in _FIELDS[section]} if section in _FIELDS else value
    return {'trading_day': row['day'], 'gateway_identity': row['gateway'], 'source_epoch': row['epoch'],
            'trading_day_basis': 'PROVED_OWNED_FILL_DAY' if section == 'owned-trades' else 'BROKER_OBSERVATION_DAY',
            'record_id': row['record_id'], 'event_type': row['event_type'], 'event_seq': _seq(row['event_seq']),
            'first_event_seq': _seq(row['first_seq']), 'observed_at_utc': row['observed_at_utc'],
            'status': 'CONFLICT' if row['conflict'] else 'OBSERVED', 'fact': fact}


def _record_page(db, params, coverage, limit):
    section = params['section']
    if section == 'coverage':
        return [], False
    sql = {'funds': _FUNDS, 'positions': _POSITIONS, 'owned-trades': _OWNED_ROWS}.get(section, _NATIVE_ROWS)
    if section in ('orders', 'trades'):
        params.update(collection='$.' + section, identity='$.order_id' if section == 'orders' else '$.trade_id')
    records = []
    size = len(canonical_bytes(coverage))
    for row in db.execute(sql, params):
        if len(records) == limit:
            return records, True
        record = _history_record(row, section)
        size += len(canonical_bytes(record)) + 1
        if size > 900_000:
            if not records:
                raise TraderError('E_HISTORY_CAPACITY', 'The history record does not fit one response')
            return records, True
        records.append(record)
    return records, False


def read_account_days(ledger, account_key, *, from_day, to_day, section='coverage', contract=None, cursor=None, limit=50):
    first, last = validate_day_query(from_day, to_day, section, contract, cursor, limit)
    path = Path(ledger).resolve()
    if not path.is_file():
        raise TraderError('E_STORAGE', 'The selected account has no execution ledger')
    values = {'account_key': identifier(account_key), 'from_day': from_day, 'to_day': to_day,
              'section': section, 'contract': contract}
    deadline = time.monotonic() + 5
    try:
        with history_database(path, account_key) as db:
            db.row_factory = sqlite3.Row
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 2000)
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            scope = db.execute('SELECT account_key, environment, schema_version, created_at_utc FROM ledger_scope').fetchall()
            if len(scope) != 1 or tuple(scope[0])[:3] != (account_key, 'ONLINE_SIM', 1):
                raise TraderError('E_SCOPE', 'History does not belong to the selected simulation account')
            checkpoint, offset, digest = _page_boundary(db, values, scope[0]['created_at_utc'], cursor)
            params = values | {'checkpoint': checkpoint, 'offset': offset, 'length': limit + 1}
            coverage = _coverage(db, params, first, last)
            records, has_next = _record_page(db, params, coverage, limit)
    except sqlite3.Error as error:
        if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
            raise TraderError('E_HISTORY_LIMIT', 'The history query exceeded its time limit. Use a shorter date range.') from None
        raise TraderError('E_STORAGE', 'Cannot read the selected account history checkpoint') from None

    next_cursor = f'{checkpoint}.{offset + len(records)}.{digest}' if has_next else None
    return values | {'schema_version': 'ONLINE_TRADER_ACCOUNT_DAY_HISTORY/1', 'environment': 'ONLINE_SIM',
                     'read_at_event_seq': str(checkpoint), 'coverage': coverage, 'records': records,
                     'next_cursor': next_cursor}
