"""Read committed activity on a separate thread and publish bounded snapshots."""

from collections import deque
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Event, Thread

from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError


class WorkbenchEvidence:
    def __init__(self, database):
        self._uri = Path(database).resolve().as_uri() + '?mode=ro'
        self._wake = Event()
        self._stop = Event()
        self._seq = 0
        self._last_decisions = {}
        self._decisions = deque(maxlen=100)
        self._dispatch = deque(maxlen=50)
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
                if kind == 'ROUND':
                    self._project_round(seq, strict_loads(payload))
                elif kind == 'EFFECT_RESULT':
                    self._project_dispatch(seq, strict_loads(payload))
                self._seq = seq
        if self._stop.is_set():
            return
        activity = sorted((*self._decisions, *self._dispatch), key=lambda entry: entry[0], reverse=True)
        fills = reader.execute('SELECT trading_day,payload FROM native_trades ORDER BY rowid DESC LIMIT 100').fetchall()
        self._result = {'history_status': 'READY', 'activity': [row for _, row in activity],
                        'fills': [{'fill_day': day, **strict_loads(payload)} for day, payload in fills]}

    def _project_dispatch(self, seq, event):
        if event['result'] in ('DEFINITELY_NOT_SENT', 'UNKNOWN'):
            stack = event.get('exception', {}).get('stack', ())
            self._dispatch.append((seq, {
                'reason': event['result'], 'effect_kind': event.get('kind'), 'at': event.get('at'),
                'contract_id': event.get('contract_id'), 'child_order_id': event['child_order_id'],
                'error_code': event.get('error_code'), 'rejection_reason': event.get('rejection_reason'),
                'exception_type': stack[0].get('type') if stack else None,
            }))

    def _project_round(self, seq, event):
        receipt = event['receipt']
        expiry_days = {row['contract_id']: row.get('days_to_expiry') for row in event['input']['market']}
        signatures = {}
        for decision in receipt['decisions']:
            contract = decision['contract_id']
            reason = decision['reason']
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
                self._decisions.append((seq, row | {'at': receipt.get('evaluated_at')}))
        self._last_decisions.update(signatures)
