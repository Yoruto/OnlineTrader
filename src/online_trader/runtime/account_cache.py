"""Rebuildable broker-account history cache; never an execution authority."""

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Full, Queue
import re
import sqlite3
from threading import Event, Thread

from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import identifier, sequence
from online_trader.errors import TraderError


SCHEMA = 'ONLINE_TRADER_BROKER_HISTORY/1'
DAY_SCHEMA = 'ONLINE_TRADER_ACCOUNT_DAY_HISTORY/1'
SECTIONS = ('coverage', 'funds', 'positions', 'orders', 'trades', 'owned-trades')
_FILTERABLE = {'positions', 'orders', 'trades', 'owned-trades'}
_CURSOR = re.compile(r'(0|[1-9][0-9]{0,18})\.([0-9]{1,9})\.([a-f0-9]{64})\Z')
_FIELDS = {
    'funds': ('balance', 'pre_balance', 'available', 'margin', 'frozen_margin', 'close_profit',
              'position_profit', 'float_profit', 'commission', 'risk_ratio'),
    'positions': ('contract_id', 'long', 'short'),
    'orders': ('order_id', 'contract_id', 'direction', 'offset', 'quantity', 'remaining', 'status', 'limit_price'),
    'trades': ('trade_id', 'order_id', 'contract_id', 'direction', 'offset', 'quantity', 'price', 'source_time_ns'),
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _open(path):
    connection = sqlite3.connect(Path(path), timeout=2)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_cache(path, account_key, provider):
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(_open(target)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS broker_history_scope(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                schema_version TEXT NOT NULL,
                account_key TEXT NOT NULL,
                environment TEXT NOT NULL,
                provider TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broker_snapshots(
                snapshot_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id TEXT NOT NULL UNIQUE,
                trading_day TEXT NOT NULL,
                gateway_identity TEXT NOT NULL,
                source_epoch INTEGER NOT NULL,
                observed_at_utc TEXT NOT NULL,
                complete INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS broker_snapshots_day
                ON broker_snapshots(trading_day, snapshot_seq);
            CREATE TABLE IF NOT EXISTS history_coverage(
                trading_day TEXT NOT NULL,
                category TEXT NOT NULL,
                coverage_state TEXT NOT NULL,
                source_epoch INTEGER NOT NULL,
                updated_at_utc TEXT NOT NULL,
                PRIMARY KEY (trading_day, category)
            );
        ''')
        scope = db.execute('SELECT schema_version,account_key,environment,provider FROM broker_history_scope').fetchall()
        expected = (SCHEMA, account_key, 'ONLINE_SIM', provider)
        if not scope:
            db.execute('INSERT INTO broker_history_scope VALUES(1,?,?,?,?,?)', (*expected, _utc_now()))
        elif [tuple(scope[0])] != [expected]:
            raise TraderError('E_SCOPE', 'The broker history cache belongs to another account or provider')
        db.commit()
    return target


def _validate_snapshot(snapshot, account_key):
    if (type(snapshot) is not dict or snapshot.get('account_key') != account_key
            or type(snapshot.get('snapshot_id')) is not str or not snapshot['snapshot_id']
            or type(snapshot.get('trading_day')) is not str
            or type(snapshot.get('gateway_identity')) is not str or not snapshot['gateway_identity']
            or type(snapshot.get('source_epoch')) is not int or snapshot['source_epoch'] < 1
            or type(snapshot.get('complete')) is not bool
            or type(snapshot.get('funds')) is not dict
            or any(type(snapshot.get(key)) is not list for key in ('positions', 'orders', 'trades'))):
        raise TraderError('E_ACCOUNT_SYNC', 'The broker snapshot is incomplete for history caching')
    try:
        if date.fromisoformat(snapshot['trading_day']).isoformat() != snapshot['trading_day']:
            raise ValueError()
    except ValueError:
        raise TraderError('E_ACCOUNT_SYNC', 'The broker snapshot has no valid cabinet day') from None
    return canonical_bytes(snapshot)


class BrokerHistoryWriter:
    """Bounded asynchronous snapshot writer; trading never waits on cache I/O."""

    def __init__(self, path, account_key, provider, *, on_error=None):
        self.path = initialize_cache(path, account_key, provider)
        self.account_key = account_key
        self.on_error = on_error
        self._closed = Event()
        self.close_completed = None
        self.queue = Queue(maxsize=1)
        self.thread = Thread(target=self._run, name='broker-history-cache', daemon=True)
        self.thread.start()

    def offer(self, snapshot, observed_at_utc=None):
        if self._closed.is_set():
            return
        item = (snapshot, observed_at_utc or _utc_now())
        try:
            self.queue.put_nowait(item)
        except Exception:
            if self._closed.is_set():
                return
            try:self.queue.get_nowait()
            except Empty:pass
            if self._closed.is_set():
                return
            try:self.queue.put_nowait(item)
            except Exception:pass

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            snapshot, observed = item
            try:
                raw = _validate_snapshot(snapshot, self.account_key)
                coverage = 'COMPLETE' if snapshot['complete'] else 'PARTIAL'
                with closing(_open(self.path)) as db:
                    db.execute('PRAGMA synchronous=NORMAL')
                    db.execute('''INSERT OR IGNORE INTO broker_snapshots
                        (snapshot_id,trading_day,gateway_identity,source_epoch,observed_at_utc,complete,payload_json,payload_hash)
                        VALUES(?,?,?,?,?,?,?,?)''', (
                        snapshot['snapshot_id'], snapshot['trading_day'], snapshot['gateway_identity'],
                        snapshot['source_epoch'], observed, int(snapshot['complete']), raw.decode('utf-8'),
                        content_hash(snapshot)))
                    db.execute('''INSERT INTO history_coverage
                        (trading_day,category,coverage_state,source_epoch,updated_at_utc)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(trading_day,category) DO UPDATE SET
                            coverage_state=CASE
                                WHEN history_coverage.coverage_state='COMPLETE' THEN 'COMPLETE'
                                ELSE excluded.coverage_state
                            END,
                            source_epoch=excluded.source_epoch,
                            updated_at_utc=excluded.updated_at_utc''', (
                        snapshot['trading_day'], 'ACCOUNT_SNAPSHOT', coverage,
                        snapshot['source_epoch'], observed))
                    db.commit()
            except Exception as error:
                if self.on_error is not None:
                    try:self.on_error(error)
                    except Exception:pass

    def close(self, timeout=3.0):
        self._closed.set()
        enqueued = False
        try:
            self.queue.put_nowait(None)
            enqueued = True
        except Full:
            try:
                self.queue.get_nowait()
            except Empty:
                pass
            try:
                self.queue.put_nowait(None)
                enqueued = True
            except Full:
                try:
                    self.queue.put(None, timeout=0.5)
                    enqueued = True
                except Exception:
                    enqueued = False
        if enqueued:
            self.thread.join(timeout=timeout)
        completed = not self.thread.is_alive()
        self.close_completed = completed
        return completed


def _scope(db, account_key):
    rows = db.execute('SELECT schema_version,account_key,environment,provider,created_at_utc FROM broker_history_scope').fetchall()
    if len(rows) != 1 or rows[0]['schema_version'] != SCHEMA or rows[0]['account_key'] != account_key:
        raise TraderError('E_SCOPE', 'History cache does not belong to the selected account')
    return rows[0]


def recent_history(path, account_key, *, before=None, limit=5):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise TraderError('E_SCHEMA', 'History page size must be between 1 and 20')
    threshold = sequence(before, positive=True) - 1 if before is not None else 9_223_372_036_854_775_807
    target = Path(path).resolve()
    if not target.is_file():
        return {'account_key': account_key, 'read_at_event_seq': '0', 'next_before': None,
                'coverage': 'NOT_SYNCHRONIZED', 'observations': []}
    try:
        with closing(_open(target)) as db:
            _scope(db, account_key)
            checkpoint = db.execute('SELECT coalesce(max(snapshot_seq),0) FROM broker_snapshots').fetchone()[0]
            rows = db.execute('''SELECT * FROM broker_snapshots WHERE snapshot_seq<=?
                ORDER BY snapshot_seq DESC LIMIT ?''', (threshold, limit + 1)).fetchall()
    except sqlite3.Error:
        raise TraderError('E_STORAGE', 'Cannot read the broker history cache') from None
    observations = []
    for row in rows[:limit]:
        value = strict_loads(row['payload_json'])
        observations.append({key: value.get(key) for key in ('trading_day', 'gateway_identity', 'source_epoch',
            'complete', 'funds', 'positions', 'orders', 'trades')} | {
            'event_seq': str(row['snapshot_seq']), 'observed_at_utc': row['observed_at_utc']})
    return {'account_key': account_key, 'read_at_event_seq': str(checkpoint),
            'next_before': observations[-1]['event_seq'] if len(rows) > limit and observations else None,
            'coverage': 'OBSERVED_BROKER_SNAPSHOTS' if observations else 'NOT_SYNCHRONIZED',
            'observations': observations}


def _day(value):
    try:
        if type(value) is str and date.fromisoformat(value).isoformat() == value:
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise TraderError('E_HISTORY_DATE', 'Use a calendar date in YYYY-MM-DD form')


def _material_row(day, row, section, identity, material):
    fields = _FIELDS.get(section)
    fact = material if fields is None else {key: material.get(key) for key in fields}
    return {'trading_day': day, 'gateway_identity': row['gateway_identity'], 'source_epoch': row['source_epoch'],
            'trading_day_basis': 'BROKER_OBSERVATION_DAY', 'record_id': identity,
            'event_type': 'BROKER_ACCOUNT_SNAPSHOT', 'event_seq': str(row['snapshot_seq']),
            'first_event_seq': str(row['snapshot_seq']), 'observed_at_utc': row['observed_at_utc'],
            'status': 'OBSERVED', 'fact': fact}


def read_account_days(path, account_key, *, from_day, to_day, section='coverage', contract=None, cursor=None, limit=50):
    first, last = _day(from_day), _day(to_day)
    if (not 0 <= (last - first).days < 366 or section not in SECTIONS
            or type(limit) is not int or not 1 <= limit <= 200):
        raise TraderError('E_SCHEMA', 'Choose a supported date range, section and page size')
    if contract is not None:
        if section not in _FILTERABLE:
            raise TraderError('E_SCHEMA', 'This history section has no contract filter')
        identifier(contract)
    target = Path(path).resolve()
    base = {'account_key': identifier(account_key), 'from_day': from_day, 'to_day': to_day,
            'section': section, 'contract': contract, 'schema_version': DAY_SCHEMA, 'environment': 'ONLINE_SIM'}
    if not target.is_file():
        return base | {'read_at_event_seq': '0', 'coverage': [], 'records': [], 'next_cursor': None}
    try:
        with closing(_open(target)) as db:
            scope = _scope(db, account_key)
            checkpoint = db.execute('SELECT coalesce(max(snapshot_seq),0) FROM broker_snapshots').fetchone()[0]
            offset, expected = 0, None
            if cursor is not None:
                match = _CURSOR.fullmatch(cursor) if type(cursor) is str else None
                if match is None:
                    raise TraderError('E_HISTORY_CURSOR', 'The history cursor is invalid')
                requested, offset, expected = int(match[1]), int(match[2]), match[3]
                if requested > checkpoint:
                    raise TraderError('E_HISTORY_CURSOR', 'The history cursor exceeds this cache checkpoint')
                checkpoint = requested
            digest = content_hash({key: base[key] for key in ('account_key','from_day','to_day','section','contract')}
                                  | {'created_at_utc': scope['created_at_utc'], 'checkpoint': str(checkpoint)})
            if expected is not None and expected != digest:
                raise TraderError('E_HISTORY_CURSOR', 'The history cursor belongs to another query')
            rows = db.execute('''SELECT * FROM broker_snapshots WHERE snapshot_seq<=?
                AND trading_day BETWEEN ? AND ? ORDER BY snapshot_seq''', (checkpoint, from_day, to_day)).fetchall()
    except sqlite3.Error:
        raise TraderError('E_STORAGE', 'Cannot read the broker history cache') from None
    by_day = {}
    decoded = []
    decode_payload = section not in ('coverage', 'owned-trades')
    for row in rows:
        stats = by_day.setdefault(row['trading_day'], {'observations': 0, 'complete_observations': 0,
                                                       'observation_seq': 0, 'complete_seq': None})
        stats['observations'] += 1
        stats['observation_seq'] = row['snapshot_seq']
        if row['complete']:
            stats['complete_observations'] += 1
            stats['complete_seq'] = row['snapshot_seq']
            if decode_payload:
                decoded.append((row, strict_loads(row['payload_json'])))
    coverage = []
    for day_offset in range((last - first).days + 1):
        day = (first + timedelta(days=day_offset)).isoformat()
        stats = by_day.get(day, {'observations': 0, 'complete_observations': 0,
                                 'observation_seq': None, 'complete_seq': None})
        observed, complete = stats['observations'], stats['complete_observations']
        coverage.append({'trading_day': day,
                         'status': 'OBSERVED_PARTIAL' if complete else
                                   'OBSERVED_INCOMPLETE' if observed else 'NOT_OBSERVED',
                         'complete_day': False,
                         'observation_count': observed,
                         'complete_observation_count': complete,
                         'latest_observation_event_seq': (str(stats['observation_seq'])
                                                          if stats['observation_seq'] is not None else None),
                         'latest_complete_event_seq': (str(stats['complete_seq'])
                                                       if stats['complete_seq'] is not None else None),
                         'verified_flat_stop_event_seq': None,
                         'owned_trade_count': 0})
    records = []
    if section != 'coverage' and section != 'owned-trades':
        selected = {}
        for row, value in decoded:
            if not row['complete']:
                continue
            day = row['trading_day']
            if section == 'funds':
                selected[(day, 'FUNDS')] = _material_row(day, row, section, 'FUNDS', value['funds'])
                continue
            identity_key = 'order_id' if section == 'orders' else 'trade_id' if section == 'trades' else 'contract_id'
            for material in value[section]:
                identity = material.get(identity_key)
                if type(identity) is not str or not identity:
                    continue
                if contract is not None and material.get('contract_id') != contract:
                    continue
                selected[(day, row['gateway_identity'], identity)] = _material_row(day, row, section, identity, material)
        records = [selected[key] for key in sorted(selected)]
    page = records[offset:offset + limit]
    next_cursor = f'{checkpoint}.{offset + len(page)}.{digest}' if offset + len(page) < len(records) else None
    return base | {'read_at_event_seq': str(checkpoint), 'coverage': coverage,
                   'records': page, 'next_cursor': next_cursor}


def broker_report(path, account_key):
    target = Path(path).resolve()
    if not target.is_file():
        return {'schema_version': 'BROKER_ACCOUNT_REPORT/1', 'account_key': account_key,
                'read_at_snapshot_seq': '0', 'coverage': 'NOT_SYNCHRONIZED', 'daily': [], 'trades': []}
    with closing(_open(target)) as db:
        _scope(db, account_key)
        checkpoint = db.execute('SELECT coalesce(max(snapshot_seq),0) FROM broker_snapshots').fetchone()[0]
        rows = db.execute('SELECT * FROM broker_snapshots ORDER BY snapshot_seq').fetchall()
    daily, trades = {}, {}
    for row in rows:
        value = strict_loads(row['payload_json'])
        if not row['complete']:
            continue
        funds = value['funds']
        daily[row['trading_day']] = {'trading_day': row['trading_day'], 'observed_at_utc': row['observed_at_utc'],
            **{key: funds.get(key) for key in ('balance','pre_balance','available','margin','close_profit',
                                               'position_profit','float_profit','commission')}}
        for trade in value['trades']:
            identity = trade.get('trade_id')
            if type(identity) is str and identity:
                trades[(row['trading_day'], row['gateway_identity'], identity)] = trade
    return {'schema_version': 'BROKER_ACCOUNT_REPORT/1', 'account_key': account_key,
            'read_at_snapshot_seq': str(checkpoint),
            'coverage': 'OBSERVED_BROKER_SNAPSHOTS' if rows else 'NOT_SYNCHRONIZED',
            'daily': [daily[key] for key in sorted(daily)],
            'trades': [trades[key] for key in sorted(trades)]}
