"""Read committed activity on a separate thread and publish bounded snapshots."""

from collections import deque
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Event, Thread

from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError


def _text(value):
    """Validate optional display metadata without stringifying arbitrary payloads."""
    if value is not None and not isinstance(value, str):
        raise ValueError('Invalid history text field')
    return value


class WorkbenchEvidence:
    def __init__(self, database):
        self._uri = Path(database).resolve().as_uri() + '?mode=ro'
        self._wake = Event()
        self._stop = Event()
        self._seq = 0
        self._last_decisions = {}
        self._decisions = deque(maxlen=100)
        self._dispatch = deque(maxlen=50)
        self._warnings = deque(maxlen=20)
        self._result = {'history_status': 'LOADING', 'activity': [], 'fills': []}
        self._thread = Thread(target=self._run, name='DSV workbench evidence', daemon=True)
        self._thread.start()

    def request(self):
        self._wake.set()
        return self._result

    def close(self):
        self._stop.set()
        self._wake.set()
        self._thread.join()

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                with closing(sqlite3.connect(self._uri, uri=True, timeout=0.05)) as reader:
                    self._read(reader)
            except (OSError, sqlite3.Error, TraderError, KeyError, TypeError, ValueError):
                self._result = self._result | {'history_status': 'UNAVAILABLE'}

    def _read(self, reader):
        end, = reader.execute('SELECT COALESCE(MAX(seq), 0) FROM events').fetchone()
        while self._seq < end and not self._stop.is_set():
            # Copy a bounded batch and release its read statement before
            # decoding JSON. Parsing never holds a database read lock.
            batch = reader.execute(
                'SELECT seq,kind,payload FROM events WHERE seq>? AND seq<=? ORDER BY seq LIMIT 32',
                (self._seq, end)).fetchall()
            if not batch:
                break
            for seq, kind, payload in batch:
                try:
                    if kind == 'ROUND':
                        self._project_round(seq, strict_loads(payload))
                    elif kind == 'EFFECT_RESULT':
                        self._project_dispatch(seq, strict_loads(payload), reader)
                except (TraderError, KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
                    # Only malformed auxiliary event data is skipped. A SQLite
                    # failure escapes to _run without advancing this event, so a
                    # transient lock/read failure can be retried. Never edit facts.
                    code = ('E_HISTORY_EFFECT_CONFLICT' if isinstance(error, TraderError)
                            and error.code == 'E_HISTORY_EFFECT_CONFLICT' else 'E_HISTORY_EVENT_FORMAT')
                    self._warning(seq, kind, code)
                self._seq = seq
        if self._stop.is_set():
            return
        activity = sorted((*self._decisions, *self._dispatch, *self._warnings),
                          key=lambda entry: entry[0], reverse=True)
        fills = reader.execute('SELECT trading_day,payload FROM native_trades ORDER BY rowid DESC LIMIT 100').fetchall()
        self._result = {'history_status': 'DEGRADED' if self._warnings else 'READY',
                        'activity': [row for _, row in activity],
                        'fills': [{'fill_day': day, **strict_loads(payload)} for day, payload in fills]}

    def _warning(self, seq, kind, code):
        # No payload, source text or exception message crosses into the UI.
        self._warnings.append((seq, {'reason': 'HISTORY_RECORD_INCOMPLETE',
                                    'event_seq': str(seq), 'event_kind': kind, 'error_code': code}))

    def _dispatch_identity(self, reader, event):
        names = ('child_order_id', 'kind', 'contract_id')
        identity = {name: _text(event.get(name)) for name in names}
        if all(identity.values()):
            return identity, None
        effect_id = _text(event.get('id'))
        prepared = None if not effect_id else reader.execute(
            'SELECT child,kind,request FROM effects WHERE id=?', (effect_id,)).fetchone()
        if prepared is None:
            return identity, 'E_HISTORY_EFFECT_MISSING'
        child, kind, payload = prepared
        request = strict_loads(payload)
        if (not isinstance(request, dict) or request.get('id') != effect_id
                or not isinstance(child, str) or not child or kind not in ('SEND', 'CANCEL')
                or request.get('child_order_id') != child or request.get('kind') != kind
                or not isinstance(request.get('contract_id'), str) or not request['contract_id']):
            raise TraderError('E_HISTORY_EFFECT_CONFLICT', 'Inconsistent prepared history identity')
        recovered = {name: request[name] for name in names}
        if any(identity[name] is not None and identity[name] != recovered[name] for name in names):
            raise TraderError('E_HISTORY_EFFECT_CONFLICT', 'Conflicting history identity')
        # Resolve ONLY missing display identity from the exact committed request.
        # Neither its current result nor the order-ID suffix proves an outcome.
        return recovered, None

    def _project_dispatch(self, seq, event, reader):
        if event['result'] in ('DEFINITELY_NOT_SENT', 'UNKNOWN'):
            identity, warning = self._dispatch_identity(reader, event)
            if identity['kind'] not in (None, 'SEND', 'CANCEL'):
                raise ValueError('Invalid history action kind')
            stack = event.get('exception', {}).get('stack', ())
            row = {
                'reason': event['result'], 'effect_kind': identity['kind'], 'at': _text(event.get('at')),
                'contract_id': identity['contract_id'], 'child_order_id': identity['child_order_id'],
                'error_code': _text(event.get('error_code')), 'rejection_reason': _text(event.get('rejection_reason')),
                'exception_type': _text(stack[0].get('type')) if stack else None,
                'event_seq': str(seq),
            }
            if warning:
                self._warning(seq, 'EFFECT_RESULT', warning)
            self._dispatch.append((seq, row))

    def _project_round(self, seq, event):
        receipt = event['receipt']
        expiry_days = {row['contract_id']: row.get('days_to_expiry') for row in event['input']['market']}
        signatures, projected = {}, []
        for decision in receipt['decisions']:
            contract = decision['contract_id']
            reason = decision['reason']
            if not isinstance(contract, str) or not isinstance(reason, str):
                raise ValueError('Invalid history decision identity')
            row = {'reason': reason, 'contract_id': contract}
            if reason == 'EXPIRY_ENTRY_BLOCK':
                row['days_to_expiry'] = decision.get('days_to_expiry', expiry_days.get(contract))
            elif reason == 'PROTECTED_FLOOR':
                row.update(nominal_reference_price=decision.get('nominal_reference_price'),
                           nominal_move=decision.get('nominal_move'))
            if decision.get('day_stop_reason'):
                row['day_stop_reason'] = decision['day_stop_reason']
            signature = tuple(row.items())
            signatures.setdefault(contract, set()).add(signature)
            if signature not in self._last_decisions.get(contract, ()):
                projected.append((seq, row | {'at': _text(receipt.get('evaluated_at'))}))
        # Do not publish half a malformed ROUND or change its deduplication state.
        self._decisions.extend(projected)
        self._last_decisions.update(signatures)
