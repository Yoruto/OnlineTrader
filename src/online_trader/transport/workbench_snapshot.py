"""A local, read-only pipe serving the last complete owner publication."""

from datetime import datetime, timezone
import logging
from threading import Event, Lock, Thread
import time

from online_trader.contracts.codec import HEADER, encode_frame, strict_loads
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import PipeListener, connect


MAX_AGE_NS = 5_000_000_000
PUBLICATION_INTERVAL_NS = 2_500_000_000


def _validate(message):
    if (message.keys() != {'protocol', 'published_ns', 'published_utc', 'base', 'view'}
            or message['protocol'] != 'ONLINE_TRADER_WORKBENCH_SNAPSHOT/1'
            or not isinstance(message['base'], dict) or not isinstance(message['view'], dict)
            or not isinstance(message['published_ns'], str)
            or not message['published_ns'].isdigit() or len(message['published_ns']) > 20
            or not isinstance(message['published_utc'], str)):
        raise TraderError('E_SNAPSHOT', 'Invalid workbench publication')


class WorkbenchPublication:
    def __init__(self, pipe, *, interval_ns=PUBLICATION_INTERVAL_NS, deferred=False):
        self._listener = PipeListener(pipe + '.snapshot', validator=_validate)
        self._stop = Event()
        self._frame = None
        self._next_publication = 0
        self._interval_ns = interval_ns
        self.status = {'error_code': None, 'last_success_monotonic_ns': None}
        self._encoder = None
        if deferred:
            self._pending = None
            self._pending_lock = Lock()
            self._ready = Event()
            self._encoder = Thread(target=self._encode_pending, daemon=True)
            self._encoder.start()
        self._thread = Thread(target=self._serve, daemon=True)
        self._thread.start()

    def publish(self, project):
        now = time.monotonic_ns()
        if now < self._next_publication:
            return False
        if self._encoder is not None:
            with self._pending_lock:
                if self._pending is not None:
                    self._next_publication = now + self._interval_ns
                    return False
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            value = project()
            if self._encoder is None:
                self._encode(*value, now, stamp)
            else:
                with self._pending_lock:
                    if self._pending is not None:
                        return False
                    self._pending = (value, now, stamp)
                    self._ready.set()
            return True
        except Exception as error:
            self._failure(error)
            return False
        finally:
            self._next_publication = time.monotonic_ns() + self._interval_ns

    def _failure(self, error):
        code = ('PUBLICATION_OVERSIZE' if getattr(error, 'code', None) in
                {'E_FRAME', 'E_WORKBENCH_CAPACITY'} else 'PUBLICATION_FAILED')
        if self.status['error_code'] != code:
            logging.getLogger(__name__).warning('%s', code)
        self.status = self.status | {'error_code': code}

    def _encode_pending(self):
        while not self._stop.is_set():
            if not self._ready.wait(.25):
                continue
            with self._pending_lock:
                value, self._pending = self._pending, None
                self._ready.clear()
            if value is not None:
                complete, now, stamp = value
                try:
                    self._encode(*complete(), now, stamp)
                except Exception as error:
                    self._failure(error)

    def _encode(self, base, view, now, stamp):
        self._frame = encode_frame({
            'protocol': 'ONLINE_TRADER_WORKBENCH_SNAPSHOT/1',
            'published_ns': str(now), 'published_utc': stamp, 'base': base, 'view': view,
        })
        code = 'PUBLICATION_OVERSIZE' if view.get('detail_unavailable') == 'CURRENT_STATE_OVERSIZE' else None
        if code and self.status['error_code'] != code:
            logging.getLogger(__name__).warning('%s', code)
        self.status = {'error_code': code, 'last_success_monotonic_ns': str(now)}

    def _serve(self):
        while not self._stop.is_set():
            try:
                with self._listener.accept(timeout=.2) as connection:
                    frame = self._frame
                    if frame is not None:
                        connection.send(strict_loads(frame[HEADER.size:]), timeout=.25)
            except (OSError, TraderError):
                if self._stop.wait(.01):
                    break

    def close(self):
        self._stop.set()
        if self._encoder is not None:
            self._ready.set()
            self._encoder.join(timeout=1)
        self._thread.join(timeout=1)
        self._listener.close()


def read_workbench_publication(pipe, account_key):
    try:
        with connect(pipe + '.snapshot', timeout=.2, validator=_validate) as connection:
            message = connection.recv(timeout=.4)
        view = message['view']
        records = message['base']['records']
        accounts = [row for row in records if row['record_type'] == 'ACCOUNT']
        published_ns = int(message['published_ns'])
        if (view['view_schema'] != 'workbench/1' or not 0 <= time.monotonic_ns() - published_ns < MAX_AGE_NS
                or len(accounts) != 1 or accounts[0]['record_id'] != view['account_key']
                or accounts[0]['record_revision'] != view['checkpoint_event_seq']
                or account_key is not None and view['account_key'] != account_key):
            return None
        return {'base': message['base'], 'view': view, 'published_ns': published_ns,
                'metadata': {'kernel_boot_id': view['kernel_boot_id'], 'received_at_utc': message['published_utc']}}
    except (OSError, TraderError, KeyError, TypeError, ValueError):
        return None
