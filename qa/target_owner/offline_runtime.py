"""Isolated public-pipe fixture for the Standalone target-execution owner.

This module is QA-only.  It accepts in-memory fact frames, owns a disposable
Journal and exposes only the product ControlServer pipe.  It has no credential,
gateway or network input and is excluded from the production wheel.
"""

from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.standalone_owner import StandaloneDsvOwner
from online_trader.kernel.server import ControlServer
from online_trader.runtime.session_storage import RuntimeSessionStorage
from qa.recording_port import RecordingPort


CONTRACT = 'DCE.p2701'
DAY = '2026-11-13'


def profile():
    settings = load_confirmed_baseline()[0].to_settings()
    settings['symbols'] = [settings['symbols'][0] | {
        'code': CONTRACT, 'open_offset': 40, 'stop_offset': 8,
        'profit_offset': 20, 'order_volume': 5,
    }]
    settings['trade'].update(
        continuation_entry=False, no_chase_after_act=False,
        passive_entry_first=False, protect_80_enabled=False,
        trailing_activate_ratio=0,
    )
    return DsvProfile.from_settings(settings)


def frame(sequence, *, complete=True, owned=0, facts=(), price=1060):
    wall = f'2026-11-13T01:10:{sequence:02d}Z'
    return {
        'id': f'qa-round-{sequence}', 'sequence': sequence,
        'session_id': 'qa-session', 'wall_time': wall,
        'monotonic_ms': sequence * 1000, 'complete': bool(complete),
        # A complete cabinet baseline explicitly claims the full active-child
        # set.  The fixture starts flat, so the ordered set is empty.
        'synchronized': bool(complete), 'sync_orders': [],
        'market': [{
            'contract_id': CONTRACT, 'ticks': [{
                'native_time': wall, 'candle_id': 'qa-bar',
                'last': str(price), 'day_low': '1020', 'day_high': '1088',
                'bid1': str(price), 'ask1': str(price), 'tick_size': '1',
                'lower_limit': '900', 'upper_limit': '1200',
            }], 'opening_session': True,
            'session_open': '2026-11-13T01:00:00Z',
            'session_close': '2026-11-13T02:15:00Z',
            'long_qty': owned, 'short_qty': 0,
            'available': '100000', 'margin_per_lot': '1000',
        }], 'native': list(facts),
    }


def order_fact(child, status, filled=0):
    return {
        'kind': 'ORDER', 'child_order_id': child,
        'native_order_id': child + '|qa-native', 'status': status,
        'filled_qty': filled,
    }


class OfflineConnection:
    def __init__(self):
        self.broker_connected = True
        self.read_only = False
        self.closed = False
        self.network_calls = 0

    def complete(self):
        return True

    def close(self):
        self.closed = True


class OfflineAdapter(RecordingPort):
    """Controller port plus the minimum owner-facing adapter contract."""

    def __init__(self, frames):
        super().__init__()
        self.frames = frames
        self.connection = OfflineConnection()
        self.synchronized = False
        self.dispatch_market = {}
        self.grant = SimpleNamespace(value={'calendar_hash': 'qa-calendar'})

    def command(self, session, command):
        return session.command(command)

    def step(self, session, *, trading_day, complete):
        try:
            current = self.frames.get_nowait()
        except Empty:
            return None
        if not current.pop('complete'):
            self.synchronized = False
            session.disconnect('QA_INCOMPLETE_BROKER_BASELINE')
            return {'type': 'DISCONNECTED', 'synchronized': False}
        result = session.apply_round(current)
        self.synchronized = session.snapshot()['synchronized']
        return result


class OfflineTargetRuntime:
    """Run the real target Owner and ControlServer on their owner thread."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.pipe = 'online-trader-target-qa-' + uuid4().hex
        self.frames = Queue()
        self.calls = Queue()
        self.ready = Event()
        self.stop_event = Event()
        self.failure = None
        self.boot_id = None
        self.ledger = None
        self.owner = None
        self.adapter = None
        self.thread = Thread(target=self._run, name='target-owner-qa', daemon=True)

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError('Offline target owner did not start')
        if self.failure is not None:
            raise self.failure
        return self

    def _run(self):
        try:
            with RuntimeSessionStorage() as storage:
                self.boot_id, self.ledger = storage.boot_id, storage.ledger
                account = 'DEV:target-owner-' + uuid4().hex
                binding = StandaloneOwnerBinding(
                    storage.boot_id, account, 'OFFLINE', object(), object())
                self.adapter = OfflineAdapter(self.frames)
                session = DsvSession(
                    storage.ledger, account_key=account, provider='OFFLINE',
                    trading_day=DAY, profile=profile(), port=self.adapter,
                    _owner_binding=binding)
                session.disconnect('QA_OWNER_CREATED_DISARMED')
                self.owner = StandaloneDsvOwner(
                    session, self.adapter, self.adapter.connection, profile(),
                    binding, configuration_revision='1')
                # One event represents both an explicit QA shutdown and the
                # product FLATTEN_THEN_STOP completion signal.
                self.owner.stop_event = self.stop_event
                server = ControlServer(self.owner, self.pipe)
                self.ready.set()
                server.serve(
                    stop_event=self.stop_event,
                    owner_tick=self._tick,
                    publish_workbench=False)
                self.owner.close()
        except BaseException as error:
            self.failure = error
            self.ready.set()

    def submit(self, value):
        self.frames.put(value)

    def _tick(self):
        self.owner.step(trading_day=DAY, complete=True)
        try:
            callback, result = self.calls.get_nowait()
        except Empty:
            return
        try:
            result.put((True, callback(self.owner)))
        except BaseException as error:
            result.put((False, error))

    def call_owner(self, callback, timeout=5):
        result = Queue(maxsize=1)
        self.calls.put((callback, result))
        ok, value = result.get(timeout=timeout)
        if not ok:
            raise value
        return value

    def close(self):
        self.stop_event.set()
        self.thread.join(10)
        if self.thread.is_alive():
            raise RuntimeError('Offline target owner did not stop')
        if self.failure is not None:
            raise self.failure

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
