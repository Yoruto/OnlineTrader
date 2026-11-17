"""A local, read-only pipe serving the last complete owner publication."""

from datetime import datetime, timezone
from threading import Event, Thread
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
    def __init__(self, pipe):
        self._listener = PipeListener(pipe + '.snapshot', validator=_validate)
        self._stop = Event()
        self._frame = None
        self._next_publication = 0
        self._thread = Thread(target=self._serve, daemon=True)
        self._thread.start()

    def publish(self, project):
        now = time.monotonic_ns()
        if now < self._next_publication:
            return
        self._next_publication = now + PUBLICATION_INTERVAL_NS
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            base, view = project()
            self._frame = encode_frame({
                'protocol': 'ONLINE_TRADER_WORKBENCH_SNAPSHOT/1',
                'published_ns': str(now), 'published_utc': stamp, 'base': base, 'view': view,
            })
        except Exception:
            # Keep serving the previous complete frame with its original age.
            return

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
        if (view['view_schema'] != 'workbench/1' or published_ns > time.monotonic_ns()
                or len(accounts) != 1 or accounts[0]['record_id'] != view['account_key']
                or accounts[0]['record_revision'] != view['checkpoint_event_seq']
                or account_key is not None and view['account_key'] != account_key):
            return None
        return {'base': message['base'], 'view': view, 'published_ns': published_ns,
                'metadata': {'kernel_boot_id': view['kernel_boot_id'], 'received_at_utc': message['published_utc']}}
    except (OSError, TraderError, KeyError, TypeError, ValueError):
        return None
