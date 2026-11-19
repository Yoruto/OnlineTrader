"""Framed, atomic reads of one account observation from the runtime cache."""

from dataclasses import dataclass
from datetime import date, datetime
import time

from online_trader.contracts.codec import canonical_bytes
from online_trader.domain.values import identifier, sequence
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import connect


PROTOCOL = 'ONLINE_TRADER_RUNTIME_ACCOUNT/1'
TABLES = ('positions', 'orders', 'trades')
MAX_ROWS = 32_768
PAGE_ROWS = 256
PAGE_BYTES = 768_000
MAX_BYTES = 16 * 1024 * 1024
MAX_AGE_NS = 5_000_000_000
_HEADER = {'account_key', 'environment', 'kernel_boot_id', 'snapshot_id', 'gateway_identity',
           'source_epoch', 'observed_at_utc', 'observed_ns', 'trading_day', 'checkpoint_event_seq',
           'status', 'reason_code', 'coverage', 'funds', 'counts'}


def _invalid():
    raise TraderError('E_SCHEMA', 'Invalid runtime account observation frame')


def validate_wire(message):
    if message.get('protocol') != PROTOCOL:
        _invalid()
    kind = message.get('kind')
    if kind == 'read':
        if message.keys() != {'protocol', 'kind', 'known_id'}:
            _invalid()
        if message['known_id'] is not None:
            identifier(message['known_id'])
    elif kind == 'observation':
        if (message.keys() != {'protocol', 'kind', 'state', 'reused'} or type(message['reused']) is not bool
                or type(message['state']) is not dict or message['state'].keys() != _HEADER):
            _invalid()
        state = message['state']
        identifier(state['account_key'])
        if state['status'] not in ('READY', 'STALE', 'WAITING') or state['environment'] != 'ONLINE_SIM':
            _invalid()
        if state['status'] == 'READY':
            if state['reason_code'] is not None:
                _invalid()
        else:
            identifier(state['reason_code'])
        if state['status'] == 'WAITING':
            if (message['reused'] or any(state[key] is not None for key in
                    _HEADER - {'account_key', 'environment', 'status', 'reason_code'})):
                _invalid()
            return
        identifier(state['snapshot_id'])
        identifier(state['kernel_boot_id'])
        identifier(state['gateway_identity'])
        if (state['coverage'] != 'CURRENT_BROKER_SNAPSHOT' or type(state['source_epoch']) is not int
                or state['source_epoch'] < 1
                or any(type(state[key]) is not str or len(state[key]) > 19 for key in
                       ('observed_ns', 'checkpoint_event_seq'))):
            _invalid()
        sequence(state['observed_ns'], positive=True)
        sequence(state['checkpoint_event_seq'])
        try:
            observed = state['observed_at_utc']
            day = state['trading_day']
            if type(observed) is not str or not observed.endswith('Z') or not 20 <= len(observed) <= 27:
                _invalid()
            datetime.fromisoformat(observed)
            if type(day) is not str or len(day) != 10 or date.fromisoformat(day).isoformat() != day:
                _invalid()
        except ValueError:
            _invalid()
        counts = state['counts']
        if (type(state['funds']) is not dict or type(counts) is not dict or counts.keys() != set(TABLES)
                or any(type(value) is not int or not 0 <= value <= MAX_ROWS for value in counts.values())
                or sum(counts.values()) > MAX_ROWS):
            _invalid()
    elif kind == 'page':
        if (message.keys() != {'protocol', 'kind', 'snapshot_id', 'table', 'start', 'rows'}
                or message['table'] not in TABLES or type(message['start']) is not int
                or not 0 <= message['start'] < MAX_ROWS or type(message['rows']) is not list
                or not 1 <= len(message['rows']) <= PAGE_ROWS or any(type(row) is not dict for row in message['rows'])):
            _invalid()
        identifier(message['snapshot_id'])
    else:
        _invalid()


def _capacity():
    raise TraderError('E_ACCOUNT_CAPACITY', 'The complete account observation exceeds the bounded publication capacity')


def account_pages(snapshot_id, sections):
    if sum(map(len, sections.values())) > MAX_ROWS:
        _capacity()
    pages = []
    total_size = 0
    for table in TABLES:
        start, size, batch = 0, 0, []
        for row in sections[table]:
            row_size = len(canonical_bytes(row)) + 1
            total_size += row_size
            if row_size > PAGE_BYTES or total_size > MAX_BYTES:
                _capacity()
            if len(batch) == PAGE_ROWS or size + row_size > PAGE_BYTES:
                pages.append(_page(snapshot_id, table, start, batch))
                start += len(batch)
                size, batch = 0, []
            batch.append(row)
            size += row_size
        if batch:
            pages.append(_page(snapshot_id, table, start, batch))
    return tuple(pages)


def _page(snapshot_id, table, start, rows):
    return {'protocol': PROTOCOL, 'kind': 'page', 'snapshot_id': snapshot_id,
            'table': table, 'start': start, 'rows': rows}


@dataclass(frozen=True)
class AccountObservation:
    state: dict
    pages: tuple = ()

    @classmethod
    def waiting(cls, account_key, reason):
        return cls(dict.fromkeys(_HEADER) | {'account_key': account_key, 'environment': 'ONLINE_SIM',
                                             'status': 'WAITING', 'reason_code': reason})

    def unavailable(self, reason):
        status = 'STALE' if self.state['snapshot_id'] is not None else 'WAITING'
        return AccountObservation(self.state | {'status': status, 'reason_code': reason}, self.pages)

    def current(self):
        observed = self.state['observed_ns']
        if self.state['status'] == 'READY' and not 0 <= time.monotonic_ns() - int(observed) < MAX_AGE_NS:
            return self.unavailable('E_ACCOUNT_SOURCE_STALLED')
        return self

    def send(self, connection, known_id):
        reused = known_id is not None and known_id == self.state['snapshot_id']
        deadline = time.monotonic() + 2
        connection.send({'protocol': PROTOCOL, 'kind': 'observation', 'state': self.state, 'reused': reused}, timeout=.5)
        if not reused:
            for page in self.pages:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TraderError('E_TIMEOUT', 'The account observation did not finish sending')
                connection.send(page, timeout=min(.5, remaining))

    def record(self):
        sections = dict.fromkeys(TABLES)
        if self.state['counts'] is not None:
            sections = {table: [] for table in TABLES}
            for page in self.pages:
                sections[page['table']].extend(page['rows'])
        return self.state | sections | {'schema_version': PROTOCOL, 'complete': self.state['counts'] is not None}


def read_observation(pipe, *, account_key=None, previous=None):
    known_id = previous.state['snapshot_id'] if previous is not None else None
    deadline = time.monotonic() + 3
    with connect(pipe, timeout=.8, validator=validate_wire) as connection:
        connection.send({'protocol': PROTOCOL, 'kind': 'read', 'known_id': known_id}, timeout=.5)
        response = connection.recv(timeout=1)
        if response['kind'] != 'observation':
            _invalid()
        state = response['state']
        if account_key is not None and state['account_key'] != account_key:
            raise TraderError('E_SCOPE', 'The account observation belongs to a different account')
        if response['reused']:
            if (known_id is None or state['snapshot_id'] != known_id
                    or any(state[key] != previous.state[key] for key in
                           ('account_key', 'kernel_boot_id', 'gateway_identity', 'source_epoch', 'trading_day', 'counts', 'funds'))):
                _invalid()
            return AccountObservation(state, previous.pages)
        pages = []
        total_size = 0
        for table in TABLES:
            count = (state['counts'] or {}).get(table, 0)
            start = 0
            while start < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TraderError('E_TIMEOUT', 'The account observation did not finish reading')
                page = connection.recv(timeout=remaining)
                if (page['kind'] != 'page' or page['snapshot_id'] != state['snapshot_id'] or page['table'] != table
                        or page['start'] != start or start + len(page['rows']) > count):
                    _invalid()
                total_size += len(canonical_bytes(page['rows'])) - 1
                if total_size > MAX_BYTES:
                    _capacity()
                pages.append(page)
                start += len(page['rows'])
    return AccountObservation(state, tuple(pages))


class RuntimeAccountClient:
    def __init__(self, pipe, *, expected_account_key=None):
        self.pipe = pipe + '.account'
        self.expected_account_key = expected_account_key

    def read(self):
        return read_observation(self.pipe, account_key=self.expected_account_key).record()
