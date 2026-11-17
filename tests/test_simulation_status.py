"""Owner/clock/IPC regression checks with no network activity."""
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from online_trader.errors import TraderError
from online_trader.runtime.simulation import SimulationSession, run_session
from online_trader.scheduling.reference import ReferenceClock
from tests.test_online_simulation import OnlineSimulationFixture
from tests import test_server as server_tests


class ClockRefreshTests(OnlineSimulationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.port.clock.monotonic_anchor = time.monotonic()-121
        self.port.poll_updates = lambda seconds: None
        self.session = SimulationSession(self.kernel, Path(self.temp.name)/'session', emit=lambda value: None)
        self.release = threading.Event()

    def tearDown(self):
        self.session.close(); self.release.set()
        worker = self.session._reference_worker
        if worker is not None: worker.join(timeout=2)
        super().tearDown()

    def test_slow_sampling_is_single_flight_and_does_not_own_kernel_or_renew_old_clock(self):
        threads = []; entered = threading.Event(); old = self.port.clock
        def sample():
            threads.append(threading.get_ident()); entered.set(); self.release.wait(2)
            raise TraderError('E_CLOCK', 'Injected timeout')
        with patch('online_trader.runtime.simulation.measure_reference', sample):
            self.session._refresh_reference(); self.assertTrue(entered.wait(1))
            for _ in range(10): self.session._refresh_reference()
            self.assertEqual(len(threads), 1); self.assertNotEqual(threads[0], self.kernel._owner)
            self.assertIs(self.port.clock, old)
            self.release.set(); self.session._reference_worker.join(timeout=1)
            self.session._refresh_reference()
            self.assertIs(self.port.clock, old)

    def test_owner_commits_reference_evidence_before_installing_result(self):
        old = self.port.clock
        new = ReferenceClock(old.now().timestamp(), time.monotonic(), 0, ())
        with patch('online_trader.runtime.simulation.measure_reference', return_value=new):
            self.session._refresh_reference(); self.session._reference_worker.join(timeout=1)
        self.assertIs(self.port.clock, old)
        original = self.session.owner.capture; owners = []
        def capture(event, data):
            owners.append(threading.get_ident()); self.assertIs(self.port.clock, old)
            return original(event, data)
        with patch.object(self.session.owner, 'capture', side_effect=capture): self.session._refresh_reference()
        self.assertEqual(owners, [self.kernel._owner]); self.assertIs(self.port.clock, new)
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM event_journal WHERE event_type='ONLINE_CLOCK_REFERENCE_REFRESH'")), 1)

    def test_failed_reference_commit_does_not_install_uncommitted_clock(self):
        old = self.port.clock
        self.session._reference_results.put(ReferenceClock(old.now().timestamp(), time.monotonic(), 0, ()))
        with patch.object(self.session.owner, 'capture', side_effect=TraderError('E_STORAGE', 'Injected write failure')):
            with self.assertRaises(TraderError): self.session._refresh_reference()
        self.assertIs(self.port.clock, old)

    def test_expired_sample_never_replaces_existing_reference(self):
        old = self.port.clock
        self.session._reference_results.put(ReferenceClock(old.now().timestamp(), time.monotonic()-301, 0, ()))
        with self.assertRaises(TraderError): self.session._refresh_reference()
        self.assertIs(self.port.clock, old)

    def test_closed_session_cannot_accept_late_reference_result_or_write_again(self):
        old = self.port.clock; before = self.kernel._durable_seq
        self.session.close()
        self.session._reference_results.put(ReferenceClock(old.now().timestamp(), time.monotonic(), 0, ()))
        self.session._refresh_reference()
        self.assertIs(self.port.clock, old); self.assertEqual(self.kernel._durable_seq, before)

    def test_reference_failure_cannot_extend_expired_clock_or_rearm_owner(self):
        self.port.clock = ReferenceClock(1000, time.monotonic()-301, 0, ())
        self.session._reference_results.put(None)
        self.session.step()
        self.assertEqual(self.session.phase, 'BLOCKED_E_CLOCK'); self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.port.sent, [])

    def test_gateway_closes_even_when_kernel_shutdown_raises(self):
        closed = []
        def fail(): raise TraderError('E_STORAGE', 'Injected shutdown failure')
        fake_kernel = SimpleNamespace(simulation=SimpleNamespace(initialize=fail), close=fail)
        port = SimpleNamespace(close=lambda: closed.append('gateway'))
        lease = SimpleNamespace(close=lambda: closed.append('account'))
        with patch('online_trader.runtime.simulation.SimulationGrant.load', return_value=self.grant), \
             patch('online_trader.scheduling.reference.measure_utc_anchor',
                   return_value=ReferenceClock(1000, time.monotonic(), 0, ())), \
             patch('online_trader.runtime.simulation.AccountLock', return_value=lease), \
             patch('online_trader.runtime.simulation.TqKqPort.connect', return_value=port), \
             patch('online_trader.runtime.simulation.Kernel', return_value=fake_kernel):
            with self.assertRaises(TraderError):
                run_session('unused-offline-scope', 'unused-offline-credentials', Path(self.temp.name)/'shutdown', prepare=True)
        self.assertEqual(closed, ['gateway', 'account'])


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
