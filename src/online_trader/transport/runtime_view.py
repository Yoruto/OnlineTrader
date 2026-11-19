"""Versioned read and subscription protocol for the standalone runtime."""

from collections import deque
from threading import Condition
import time
from uuid import uuid4

from online_trader.contracts.codec import MAX_FRAME_BYTES, canonical_bytes, content_hash
from online_trader.domain.values import identifier, sequence
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import connect
from online_trader.transport.workbench_snapshot import MAX_AGE_NS


PROTOCOL = 'ONLINE_TRADER_RUNTIME_VIEW/1'
DAY_HISTORY_FIELDS = ('from_day', 'to_day', 'section', 'contract', 'cursor', 'limit')
_CURSOR_KEYS = {'generation', 'sequence', 'digest'}
_ROWS = {
    ('base', 'records'): ('record_type', 'record_id'),
    ('view', 'markets'): ('contract_id',),
    ('view', 'cycles'): ('position_cycle_id',),
    ('view', 'orders'): ('child_order_id',),
    ('view', 'trades'): ('trade_key',),
    ('view', 'commands'): ('command_id',),
}


def _cursor(value):
    if (type(value) is not dict or value.keys() != _CURSOR_KEYS or type(value['digest']) is not str
            or len(value['digest']) != 64 or any(c not in '0123456789abcdef' for c in value['digest'])):
        raise TraderError('E_SNAPSHOT', 'Invalid runtime view cursor')
    identifier(value['generation'])
    sequence(value['sequence'], positive=True)


def validate_wire(message):
    if type(message) is not dict or message.get('protocol') != PROTOCOL:
        raise TraderError('E_SCHEMA', 'Invalid standalone view protocol')
    kind = message.get('kind')
    if kind == 'history':
        if message.keys() != {'protocol', 'kind', 'before', 'limit'}:
            raise TraderError('E_SCHEMA', 'Unexpected account history request fields')
        if message['before'] is not None:
            sequence(message['before'], positive=True)
        if type(message['limit']) is not int or not 1 <= message['limit'] <= 20:
            raise TraderError('E_SCHEMA', 'Invalid history page size')
        return
    if kind == 'history-days':
        if message.keys() != {'protocol', 'kind', *DAY_HISTORY_FIELDS}:
            raise TraderError('E_SCHEMA', 'Unexpected account date history request fields')
        return
    if kind in ('HISTORY', 'HISTORY_ERROR'):
        if message.keys() != {'protocol', 'kind', 'body'} or type(message['body']) is not dict:
            raise TraderError('E_SCHEMA', 'Invalid account history response')
        return
    if kind in ('snapshot', 'align', 'subscribe'):
        if message.keys() != {'protocol', 'kind', 'cursor'}:
            raise TraderError('E_SCHEMA', 'Unexpected standalone view request fields')
        if message['cursor'] is not None:
            _cursor(message['cursor'])
        return
    if (kind not in ('BASELINE', 'DELTA', 'CURRENT', 'UNAVAILABLE')
            or message.keys() != {'protocol', 'kind', 'cursor', 'published_ns', 'body'}):
        raise TraderError('E_SCHEMA', 'Invalid standalone view response')
    if kind == 'UNAVAILABLE':
        if any(message[key] is not None for key in ('cursor', 'published_ns', 'body')):
            raise TraderError('E_SCHEMA', 'Unavailable standalone view still contains state')
        return
    _cursor(message['cursor'])
    if (type(message['published_ns']) is not str or not message['published_ns'].isdigit()
            or len(message['published_ns']) > 20):
        raise TraderError('E_SCHEMA', 'Invalid standalone view publication time')


def _row_map(rows, fields):
    if type(rows) is not list:
        return None
    indexed = {}
    for row in rows:
        if type(row) is not dict or any(type(row.get(name)) is not str for name in fields):
            return None
        key = '\x1f'.join(row[name] for name in fields)
        if key in indexed:
            return None
        indexed[key] = row
    return indexed


def _diff(previous, current):
    changes = {}
    for section, fields in current.items():
        before = previous[section]
        if before == fields:
            continue
        patch = {'set': {}, 'remove': sorted(before.keys() - fields.keys()), 'rows': {}}
        for key, value in fields.items():
            if key in before and before[key] == value:
                continue
            identity = _ROWS.get((section, key))
            old = _row_map(before.get(key), identity) if identity else None
            new = _row_map(value, identity) if identity else None
            if old is None or new is None:
                patch['set'][key] = value
            else:
                patch['rows'][key] = {'upsert': {row: item for row, item in new.items() if old.get(row) != item},
                    'remove': sorted(old.keys() - new.keys()), 'order': list(new) if list(old) != list(new) else None}
        changes[section] = patch
    return changes


def _apply_delta(current, changes):
    candidate = {section: fields.copy() for section, fields in current.items()}
    for section, patch in changes.items():
        values = candidate[section]
        for key in patch['remove']:
            del values[key]
        values.update(patch['set'])
        for key, rows in patch['rows'].items():
            original = _row_map(values[key], _ROWS[(section, key)])
            for removed in rows['remove']:
                del original[removed]
            original.update(rows['upsert'])
            order = rows['order'] if rows['order'] is not None else original
            values[key] = [original[row] for row in order]
    return candidate


class ViewHistory:
    """A transport-side cache. It never reads or holds transaction authority."""

    def __init__(self):
        self._changed = Condition()
        self._head = None
        self._state = None
        self._published_ns = 0
        self._history = deque()
        self._history_bytes = 0

    def publish(self, value):
        view = {key: row for key, row in value['view'].items() if key != 'status_projection'}
        state = {'base': value['base'], 'view': view}
        digest = content_hash(state)
        with self._changed:
            if value['published_ns'] <= self._published_ns:
                return
            self._published_ns = value['published_ns']
            if self._head is not None and self._head['digest'] == digest:
                return
            if self._state is None or self._state['view']['kernel_boot_id'] != view['kernel_boot_id']:
                self._head = {'generation': 'view-' + uuid4().hex, 'sequence': '1', 'digest': digest}
                self._history.clear()
                self._history_bytes = 0
            else:
                head = {'generation': self._head['generation'],
                        'sequence': str(int(self._head['sequence']) + 1), 'digest': digest}
                patch = {'from': self._head, 'changes': _diff(self._state, state)}
                entry = self._message('DELTA', head, patch)
                size = len(canonical_bytes(entry))
                if size <= MAX_FRAME_BYTES:
                    self._history.append((entry, size))
                    self._history_bytes += size
                else:
                    self._history.clear()
                    self._history_bytes = 0
                while len(self._history) > 128 or self._history_bytes > 8 * 1024 * 1024:
                    _, removed = self._history.popleft()
                    self._history_bytes -= removed
                self._head = head
            self._state = state
            self._changed.notify_all()

    def _message(self, kind, cursor, body):
        return {'protocol': PROTOCOL, 'kind': kind, 'cursor': cursor, 'body': body,
                'published_ns': str(self._published_ns) if cursor is not None else None}

    def _response(self, cursor):
        if self._head is None or time.monotonic_ns() - self._published_ns >= MAX_AGE_NS:
            return self._message('UNAVAILABLE', None, None)
        if cursor == self._head:
            return self._message('CURRENT', self._head, None)
        for entry, _ in self._history:
            if entry['body']['from'] == cursor:
                return entry
        return self._message('BASELINE', self._head, self._state)

    def response(self, cursor):
        with self._changed:
            return self._response(cursor)

    def wait(self, cursor, timeout):
        with self._changed:
            self._changed.wait_for(lambda: self._head != cursor, timeout)
            return self._response(cursor)


class ViewReplica:
    """Apply a whole baseline or one contiguous versioned change atomically."""

    def __init__(self):
        self.cursor = None
        self.state = None

    def apply(self, message):
        kind, cursor = message['kind'], message['cursor']
        if kind == 'UNAVAILABLE':
            return False
        if kind == 'CURRENT':
            if cursor != self.cursor:
                raise TraderError('E_SNAPSHOT', 'Runtime view cursor does not match the local replica')
            return False
        if kind == 'BASELINE':
            candidate = message['body']
        else:
            if (self.cursor is None or message['body']['from'] != self.cursor
                    or self.cursor['generation'] != cursor['generation']
                    or int(self.cursor['sequence']) + 1 != int(cursor['sequence'])):
                raise TraderError('E_SNAPSHOT', 'Runtime view delta does not follow the current cursor')
            try:
                candidate = _apply_delta(self.state, message['body']['changes'])
            except (KeyError, TypeError, AttributeError):
                raise TraderError('E_SNAPSHOT', 'The runtime row changes cannot be applied') from None
        if type(candidate) is not dict or candidate.keys() != {'base', 'view'} or content_hash(candidate) != cursor['digest']:
            raise TraderError('E_SNAPSHOT', 'Runtime view content does not match its publication digest')
        self.state, self.cursor = candidate, cursor
        return True


class RuntimeViewClient:
    def __init__(self, pipe):
        self.pipe = pipe + '.view'

    @staticmethod
    def _request(kind, cursor):
        return {'protocol': PROTOCOL, 'kind': kind, 'cursor': cursor}

    def read(self, cursor=None):
        with connect(self.pipe, timeout=1, validator=validate_wire) as connection:
            connection.send(self._request('align' if cursor else 'snapshot', cursor), timeout=1)
            return connection.recv(timeout=2)

    def subscribe(self, cursor=None):
        with connect(self.pipe, timeout=1, validator=validate_wire) as connection:
            connection.send(self._request('subscribe', cursor), timeout=1)
            while True:
                yield connection.recv(timeout=3)

    def history(self, *, before=None, limit=5):
        return self._read_history('history', {'before': before, 'limit': limit})

    def history_days(self, *, from_day, to_day, section='coverage', contract=None, cursor=None, limit=50):
        return self._read_history('history-days', {'from_day': from_day, 'to_day': to_day, 'section': section,
                                                   'contract': contract, 'cursor': cursor, 'limit': limit})

    def _read_history(self, kind, fields):
        with connect(self.pipe, timeout=1, validator=validate_wire) as connection:
            connection.send({'protocol': PROTOCOL, 'kind': kind, **fields}, timeout=1)
            response = connection.recv(timeout=7 if kind == 'history-days' else 3)
        if response['kind'] == 'HISTORY_ERROR':
            raise TraderError(response['body']['code'], response['body']['message'])
        if response['kind'] != 'HISTORY':
            raise TraderError('E_SCHEMA', 'Expected a runtime history page')
        return response['body']
