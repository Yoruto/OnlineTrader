"""Owner/clock/IPC regression checks with no network activity."""
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from online_trader.errors import TraderError
from online_trader.runtime.simulation import run_session
from online_trader.scheduling.reference import CalibratedClock, ReferenceClock
from tests.test_online_simulation import OnlineSimulationFixture
from tests import test_server as server_tests


class ClockRefreshTests(OnlineSimulationFixture, unittest.TestCase):
    def test_slow_sampling_leaves_protection_time_available_and_failure_blocks_entry(self):
        initial = ReferenceClock(self.port.clock.now().timestamp() - 121, time.monotonic() - 121, 0, ())
        with patch('online_trader.scheduling.reference.measure_reference', return_value=initial):
            clock = CalibratedClock()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def sample():
            calls.append(threading.get_ident())
            entered.set()
            release.wait(2)
            raise TraderError('E_CLOCK', 'Offline time servers')

        with patch('online_trader.scheduling.reference.measure_reference', side_effect=sample):
            clock.poll()
            try:
                self.assertTrue(entered.wait(1))
                before = clock.now()
                for _ in range(10):
                    clock.poll()
                self.assertEqual(len(calls), 1)
                self.assertNotEqual(calls[0], threading.get_ident())
                self.assertTrue(clock.entry_ready())
            finally:
                release.set()
                clock._worker.join(timeout=2)
            clock.poll()
        self.assertFalse(clock.entry_ready())
        self.assertGreaterEqual(clock.now(), before)

    def test_refresh_is_not_installed_if_evidence_write_fails(self):
        initial = ReferenceClock(self.port.clock.now().timestamp() - 121, time.monotonic() - 121, 0, ())
        with patch('online_trader.scheduling.reference.measure_reference', return_value=initial):
            clock = CalibratedClock()
        refreshed = ReferenceClock(clock.now().timestamp(), time.monotonic(), 0, ())
        with patch('online_trader.scheduling.reference.measure_reference', return_value=refreshed):
            clock.poll()
            clock._worker.join(timeout=2)
        def fail(reference):
            raise TraderError('E_STORAGE', 'Injected write failure')
        before = clock.now()
        with self.assertRaises(TraderError):
            clock.poll(before_install=fail)
        self.assertFalse(clock.entry_ready())
        self.assertGreaterEqual(clock.now(), before)

    def test_gateway_closes_even_when_kernel_shutdown_raises(self):
        closed = []
        def fail(): raise TraderError('E_STORAGE', 'Injected shutdown failure')
        fake_kernel = SimpleNamespace(simulation=SimpleNamespace(initialize=fail), close=fail)
        port = SimpleNamespace(close=lambda: closed.append(True))
        with patch('online_trader.runtime.simulation.SimulationGrant.load', return_value=self.grant), \
             patch('online_trader.runtime.simulation.AccountLock'), \
             patch('online_trader.runtime.simulation.CalibratedClock', return_value=self.port.clock), \
             patch('online_trader.runtime.simulation.TqKqPort.connect', return_value=port), \
             patch('online_trader.runtime.simulation.Kernel', return_value=fake_kernel):
            with self.assertRaises(TraderError):
                run_session('unused-offline-scope', 'unused-offline-credentials', Path(self.temp.name)/'shutdown', prepare=True)
        self.assertEqual(closed, [True])


class ClosingDispatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = server_tests.DispatcherTests(); self.fixture.setUp()
        self.kernel, self.server, self.peer = self.fixture.kernel, self.fixture.server, self.fixture.peer
        self.message, self.drain = self.fixture.message, self.fixture.drain

    def tearDown(self): self.fixture.tearDown()

    def inbound(self, message):
        self.server._inbound.put((self.peer, message, 10)); self.server._inbound_bytes += 10

    def test_closing_drain_services_already_queued_snapshot(self):
        self.peer.ready = True; self.peer.role = 'DESKTOP'; self.peer.instance_id = 'closing-desktop'
        self.server._peers.add(self.peer)
        self.inbound(self.message('SnapshotRequest', {'subscription_id': 'closing-status', 'view': 'STATUS',
            'resume_stream_seq': None, 'resume_kernel_boot_id': None, 'resume_projection_schema': None}))
        self.server._drain_outgoing(timeout=.03)
        self.assertIn('SnapshotEnd', [m['type'] for m in self.drain(self.peer)])
        self.assertEqual(self.server._inbound_bytes, 0)

    def test_closing_rejects_new_mutation_and_keeps_durable_authority(self):
        self.peer.ready = True; self.peer.role = 'DESKTOP'
        self.inbound(self.message('OperatorCommand', {})); before = self.kernel._durable_seq
        self.server._service_inbound(closing=True)
        reply, = self.drain(self.peer)
        self.assertEqual(reply['body']['error_code'], 'E_NOT_READY')
        self.assertEqual(self.kernel._durable_seq, before)

    def test_closed_peer_inbound_is_discarded_with_credit_returned(self):
        self.peer.close(); self.inbound(self.message('OperatorCommand', {}))
        self.server._service_inbound()
        self.assertEqual(self.server._inbound_bytes, 0); self.assertEqual(self.drain(self.peer), [])

    def test_stop_from_owner_tick_rejects_new_command_in_same_turn(self):
        stop = threading.Event(); self.peer.ready = True; self.peer.role = 'DESKTOP'
        def tick():
            self.inbound(self.message('OperatorCommand', {})); stop.set()
        with patch.object(self.server, '_acceptor'), patch.object(self.server, '_handle') as handle:
            self.server.serve(stop, owner_tick=tick)
        handle.assert_not_called()
        reply, = self.drain(self.peer)
        self.assertEqual(reply['body']['error_code'], 'E_NOT_READY')
