"""Private, bounded publication of the execution owner's native account reads."""

from datetime import datetime, timezone
import logging
from threading import Event, Thread
import time

from online_trader.errors import TraderError
from online_trader.runtime.processes import parent_pid
from online_trader.transport.named_pipe import PipeListener
from online_trader.transport.runtime_account import AccountObservation, TABLES, account_pages, validate_wire


_LOGGER = logging.getLogger(__name__)


class AccountPublication:
    def __init__(self, pipe, account_key, trusted_client_pid):
        self._trusted_client_pid = trusted_client_pid
        self._sample = None
        self._processed = None
        self._material = None
        self._value = AccountObservation.waiting(account_key, 'E_ACCOUNT_SYNC')
        self._listener = PipeListener(pipe + '.account-source', validator=validate_wire)
        self._stop = Event()
        self._ready = Event()
        self._projector = Thread(target=self._prepare, daemon=True)
        self._thread = Thread(target=self._serve, daemon=True)
        self._projector.start()
        self._thread.start()

    def capture(self, kernel):
        """Offer one native value without queuing or waiting on the read worker."""
        try:
            snapshot = self._native_snapshot(kernel)
            if snapshot is None:
                self._sample = ('E_ACCOUNT_SYNC', None)
            else:
                stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                observed = {'observed_ns': str(time.monotonic_ns()), 'observed_at_utc': stamp,
                            'kernel_boot_id': kernel.boot_id, 'checkpoint_event_seq': str(kernel._durable_seq)}
                self._sample = (None, (snapshot, observed))
            return snapshot
        except Exception:
            self._sample = ('E_ACCOUNT_CAPTURE', None)
            return None
        finally:
            self._ready.set()

    @staticmethod
    def _native_snapshot(kernel):
        port = kernel.port
        if kernel.simulation is None or not getattr(port, 'broker_connected', False) or port.fault:
            return None
        snapshot = port.snapshot()
        if not snapshot['complete'] or snapshot['trading_day'] is None or snapshot['funds'].get('balance') is None:
            return None
        kernel.simulation._validate_snapshot(snapshot)
        return snapshot

    def _apply(self, sample):
        reason, observation = sample
        value = self._value
        if reason is None:
            native, observed = observation
            facts = {key: native[key] for key in ('account_key', 'gateway_identity', 'source_epoch',
                                                    'trading_day', 'funds')}
            sections = {key: native[key] for key in TABLES}
            material = facts | sections | {'kernel_boot_id': observed['kernel_boot_id']}
            try:
                if material == self._material:
                    snapshot_id, pages = value.state['snapshot_id'], value.pages
                else:
                    snapshot_id = native['snapshot_id']
                    pages = account_pages(snapshot_id, sections)
                self._value = AccountObservation(facts | observed | {'snapshot_id': snapshot_id,
                    'status': 'READY', 'reason_code': None, 'environment': 'ONLINE_SIM',
                    'coverage': 'CURRENT_BROKER_SNAPSHOT', 'counts': {key: len(rows) for key, rows in sections.items()}}, pages)
                self._material = material
                return
            except Exception as error:
                reason = error.code if isinstance(error, TraderError) else 'E_ACCOUNT_PUBLICATION'
        if reason != value.state['reason_code'] and reason != 'E_ACCOUNT_SYNC':
            _LOGGER.warning('%s', reason)
        self._value = value.unavailable(reason)

    def _prepare(self):
        while not self._stop.is_set():
            if not self._ready.wait(.25):
                continue
            self._ready.clear()
            sample = self._sample
            if sample is not None and sample is not self._processed:
                self._apply(sample)
                self._processed = sample

    def _serve(self):
        while not self._stop.is_set():
            try:
                with self._listener.accept(timeout=.2) as connection:
                    allowed = self._trusted_client_pid() if callable(self._trusted_client_pid) else self._trusted_client_pid
                    if allowed <= 0 or (connection.peer_pid != allowed and parent_pid(connection.peer_pid) != allowed):
                        continue
                    request = connection.recv(timeout=.25)
                    if request['kind'] != 'read':
                        continue
                    self._value.current().send(connection, request['known_id'])
            except (OSError, TraderError):
                self._stop.wait(.01)

    def close(self):
        self._stop.set()
        self._ready.set()
        self._thread.join(timeout=1)
        self._listener.close()
        self._projector.join(timeout=1)
