"""Qt replica consumer for the independent runtime view endpoint."""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event
import time

from PySide6.QtCore import QThread, Signal

from online_trader.contracts.codec import content_hash
from online_trader.errors import TraderError
from online_trader.transport.runtime_view import RuntimeViewClient, ViewReplica


class RuntimeWatch(QThread):
    updated = Signal(object)
    observed = Signal(object)
    unavailable = Signal(str)

    def __init__(self, pipe, parent):
        super().__init__(parent)
        self.client = RuntimeViewClient(pipe)
        self._align = Event()

    def align(self):
        self._align.set()

    def run(self):
        replica = ViewReplica()
        last_ns = 0
        while not self.isInterruptionRequested():
            try:
                with closing(self.client.subscribe(replica.cursor)) as updates:
                    deadline = time.monotonic() + 2.5
                    for message in updates:
                        if self.isInterruptionRequested():
                            return
                        last_ns = self._apply(replica, message, last_ns)
                        if time.monotonic() >= deadline or self._align.is_set():
                            self._align.clear()
                            cursor = replica.cursor
                            if cursor is not None:
                                cursor = cursor | {'digest': content_hash(replica.state)}
                            last_ns = self._apply(replica, self.client.read(cursor), last_ns)
                            deadline = time.monotonic() + 2.5
            except (OSError, TraderError, KeyError, TypeError, ValueError) as error:
                self.unavailable.emit(getattr(error, 'code', 'E_RUNTIME_VIEW'))
                replica = ViewReplica()
                last_ns = 0
                self.msleep(300)

    def _apply(self, replica, message, last_ns):
        if message['kind'] == 'UNAVAILABLE':
            self.unavailable.emit('E_RUNTIME_VIEW_UNAVAILABLE')
            return last_ns
        stamp = int(message['published_ns'])
        cursor = message['cursor']
        local = replica.cursor
        if stamp < last_ns or (local is not None and local['generation'] == cursor['generation']
                               and int(cursor['sequence']) < int(local['sequence'])):
            return last_ns
        if message['kind'] == 'DELTA' and cursor == local:
            return last_ns
        if replica.apply(message):
            self.updated.emit({'action': 'refresh', 'value': deepcopy(replica.state) | {
                'published_ns': stamp, 'metadata': {'kernel_boot_id': replica.state['view']['kernel_boot_id'],
                    'received_at_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')},
            }})
        else:
            self.observed.emit(stamp)
        return max(last_ns, stamp)
