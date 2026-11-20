"""Phase 1 Runtime client gates; isolated Qt and local-pipe coverage only."""

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import unittest
from unittest.mock import patch
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QObject, Signal

from online_trader.contracts.validation import validate_message
from online_trader.desktop.runtime_client.client import CommandBus, RuntimeClientCoordinator
from online_trader.desktop.runtime_client.state import (
    ClientStateStore,
    PendingCommandStatus,
)
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import CancellablePipeClient, PipeListener


ACCOUNT = 'DEV:runtime-client-gate'
SCOPE = {'scope_type': 'CYCLE', 'scope_ids': ['cycle-gate-1']}


def runtime_state(boot):
    return {
        'base': {'records': []},
        'view': {
            'account_key': ACCOUNT,
            'kernel_boot_id': boot,
            'configuration': {'config_revision': '7'},
            'entry_authority': 'DISARMED',
            'entry_policies': [],
            'capabilities': {'operations': [
                {'operation': 'MANUAL_CLOSE', 'allowed': True, 'reason': None},
            ]},
            'markets': [],
            'orders': [],
            'trades': [],
            'cycles': [],
            'diagnostics': [],
            'commands': [],
        },
    }


def cursor(generation, sequence='1'):
    return {'generation': generation, 'sequence': sequence, 'digest': 'a' * 64}


class RecordingLane:
    def __init__(self):
        self.requests = []

    def enqueue(self, request):
        self.requests.append(request)


class CoordinatorStub(QObject):
    command_result = Signal(object)
    command_failed = Signal(object)

    def __init__(self):
        super().__init__()
        self.commands = RecordingLane()


class CommandSafetyGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self._reset_bus()

    def _reset_bus(self):
        self.store = ClientStateStore()
        self.coordinator = CoordinatorStub()
        self.bus = CommandBus(self.coordinator, self.store)
        self.store.apply(runtime_state('boot-A'), cursor('view-A'), 1)

    def _submit_unknown(self):
        command_id = self.bus.submit('MANUAL_CLOSE', SCOPE, {'position_cycle_id': 'cycle-gate-1'})
        request = self.coordinator.commands.requests[-1]
        self.coordinator.command_failed.emit(request | {
            'code': 'E_TIMEOUT', 'message': 'isolated transport timeout',
        })
        self.assertEqual(self.store.pending[command_id].status, PendingCommandStatus.SUBMISSION_UNKNOWN)
        return command_id

    def _query_request(self, command_id):
        self.bus.query(command_id)
        return self.coordinator.commands.requests[-1]

    def test_unknown_command_survives_owner_boot_change_but_not_client_restart(self):
        """Owner changes preserve the ID only inside the current Workbench process."""
        command_id = self._submit_unknown()
        scope_hash = self.store.pending[command_id].scope_hash

        self.store.apply(runtime_state('boot-B'), cursor('view-B'), 2)

        pending = self.store.pending[command_id]
        self.assertEqual(pending.status, PendingCommandStatus.OUTCOME_UNKNOWN)
        self.assertEqual(pending.reason, 'OWNER_RESTARTED')
        self.assertEqual(pending.account_key, ACCOUNT)
        self.assertEqual(pending.scope_hash, scope_hash)
        restored_store = ClientStateStore()
        CommandBus(CoordinatorStub(), restored_store)
        self.assertEqual(restored_store.pending, {})

    def test_receipt_query_transport_failures_keep_original_command_unknown(self):
        """T-05: a failed observation is not evidence that the command failed."""
        for code in ('E_TIMEOUT', 'E_PIPE_CLOSED', 'E_OWNERSHIP'):
            with self.subTest(code=code):
                # Each subtest represents a fresh Workbench process. Clearing
                # only the projected store would leave the process-local
                # registry responsible for the prior unknown command.
                self._reset_bus()
                command_id = self._submit_unknown()
                request = self._query_request(command_id)

                self.coordinator.command_failed.emit(request | {'code': code, 'message': 'query unavailable'})

                pending = self.store.pending[command_id]
                self.assertEqual(pending.status, PendingCommandStatus.OUTCOME_UNKNOWN)
                self.assertEqual(pending.reason, code)
                self.assertNotEqual(pending.status, PendingCommandStatus.REJECTED)

    def test_only_explicit_terminal_receipt_removes_process_pending(self):
        """T-06: explicit Runtime terminal evidence closes the client responsibility."""
        for status in ('SUCCEEDED', 'REJECTED'):
            with self.subTest(status=status):
                self.store.pending.clear()
                self.coordinator.commands.requests.clear()
                command_id = self._submit_unknown()
                request = self._query_request(command_id)
                self.coordinator.command_result.emit(request | {'value': {
                    'command_id': command_id,
                    'account_key': ACCOUNT,
                    'operation': 'MANUAL_CLOSE',
                    'status': status,
                    'reason': 'terminal evidence',
                }})

                self.assertNotIn(command_id, self.store.pending)
                restored_store = ClientStateStore()
                CommandBus(CoordinatorStub(), restored_store)
                self.assertNotIn(command_id, restored_store.pending)

    def test_nonterminal_receipt_remains_pending(self):
        """T-07: reconciliation-required is an unknown outcome, not a terminal result."""
        command_id = self._submit_unknown()
        request = self._query_request(command_id)
        self.coordinator.command_result.emit(request | {'value': {
            'command_id': command_id,
            'account_key': ACCOUNT,
            'operation': 'MANUAL_CLOSE',
            'status': 'NEEDS_RECONCILIATION',
            'reason': 'native outcome is not proved',
        }})

        self.assertEqual(self.store.pending[command_id].status, PendingCommandStatus.OUTCOME_UNKNOWN)

    def test_duplicate_user_intent_is_blocked_without_a_second_command(self):
        """T-08: unresolved same-scope intent cannot create another command ID."""
        command_id = self._submit_unknown()
        before = len(self.coordinator.commands.requests)

        with self.assertRaises(TraderError) as caught:
            self.bus.submit('MANUAL_CLOSE', SCOPE, {'position_cycle_id': 'cycle-gate-1'})

        self.assertEqual(caught.exception.code, 'E_PENDING_COMMAND')
        self.assertIn(command_id, str(caught.exception))
        self.assertEqual(len(self.coordinator.commands.requests), before)


class BlockingCommandClient:
    instances = []

    def __init__(self, _pipe):
        self.started = threading.Event()
        self.closed = threading.Event()
        self.calls = []
        self.instances.append(self)

    def request(self, operation, body):
        self.calls.append((operation, body))
        self.started.set()
        if not self.closed.wait(10):
            raise TraderError('E_TIMEOUT', 'fake command deadline expired')
        raise TraderError('E_PIPE_CLOSED', 'fake command pipe closed')

    def close(self):
        self.closed.set()


class BlockingViewClient:
    instances = []

    def __init__(self, _pipe):
        self.subscription_started = threading.Event()
        self.query_started = threading.Event()
        self.closed = threading.Event()
        self.instances.append(self)

    def subscribe(self, _cursor=None):
        self.subscription_started.set()
        if not self.closed.wait(10):
            raise TraderError('E_TIMEOUT', 'fake subscription deadline expired')
        raise TraderError('E_PIPE_CLOSED', 'fake subscription pipe closed')
        yield None

    def history_days(self, **_parameters):
        self.query_started.set()
        if not self.closed.wait(10):
            raise TraderError('E_TIMEOUT', 'fake history deadline expired')
        raise TraderError('E_PIPE_CLOSED', 'fake history pipe closed')

    def runtime_log(self, **parameters):
        return self.history_days(**parameters)

    def close(self):
        self.closed.set()


class BlockingAccountClient:
    instances = []

    def __init__(self, _pipe):
        self.started = threading.Event()
        self.closed = threading.Event()
        self.instances.append(self)

    def read(self):
        self.started.set()
        if not self.closed.wait(10):
            raise TraderError('E_TIMEOUT', 'fake account deadline expired')
        raise TraderError('E_PIPE_CLOSED', 'fake account pipe closed')

    def close(self):
        self.closed.set()


class CoordinatorShutdownGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        BlockingCommandClient.instances.clear()
        BlockingViewClient.instances.clear()
        BlockingAccountClient.instances.clear()
        self.patches = (
            patch('online_trader.desktop.runtime_client.client.RuntimeWorkbenchClient', BlockingCommandClient),
            patch('online_trader.desktop.runtime_client.client.RuntimeViewClient', BlockingViewClient),
            patch('online_trader.desktop.runtime_client.client.RuntimeAccountClient', BlockingAccountClient),
        )
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _coordinator(self):
        result = RuntimeClientCoordinator('Runtime.Client.Gate', ClientStateStore())
        self.addCleanup(result.close)
        return result

    @staticmethod
    def _assert_stopped(coordinator):
        for worker in (coordinator.subscription, coordinator.commands, coordinator.queries):
            if worker.isRunning():
                raise AssertionError(f'{type(worker).__name__} remained running after close')

    def test_close_cancels_blocked_subscription_and_command_before_returning(self):
        """T-01/T-10: close owns cancellation and is idempotent."""
        coordinator = self._coordinator()
        coordinator.start()
        coordinator.commands.enqueue({
            'request_id': 'request-close-command', 'action': 'receipt',
            'body': {'command_id': 'command-close-1'},
        })
        self.assertTrue(BlockingViewClient.instances[0].subscription_started.wait(2))
        self.assertTrue(BlockingCommandClient.instances[0].started.wait(2))

        started = time.monotonic()
        coordinator.close()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2, 'close waited for the fake business I/O timeout')
        self._assert_stopped(coordinator)
        coordinator.close()

    def test_close_cancels_blocked_history_query(self):
        """T-02: slow-query I/O has the same owned shutdown contract."""
        coordinator = self._coordinator()
        coordinator.start()
        coordinator.queries.enqueue({
            'request_id': 'request-close-history', 'kind': 'history',
            'parameters': {'from_day': '2026-11-18', 'to_day': '2026-11-19'},
        })
        query_client = BlockingViewClient.instances[1]
        self.assertTrue(query_client.query_started.wait(2))

        coordinator.close()

        self._assert_stopped(coordinator)
        self.assertTrue(query_client.closed.is_set())

    def test_close_does_not_send_requests_queued_behind_blocked_command(self):
        """T-09: closing the client cannot emit an extra queued business action."""
        coordinator = self._coordinator()
        coordinator.start()
        lane = coordinator.commands
        lane.enqueue({'request_id': 'request-first', 'action': 'receipt',
                      'body': {'command_id': 'command-first'}})
        self.assertTrue(BlockingCommandClient.instances[0].started.wait(2))
        lane.enqueue({'request_id': 'request-second', 'action': 'submit',
                      'body': {'command_id': 'command-second'}})

        coordinator.close()

        self._assert_stopped(coordinator)
        self.assertEqual(len(BlockingCommandClient.instances[0].calls), 1)
        self.assertEqual(BlockingCommandClient.instances[0].calls[0][1]['command_id'], 'command-first')


@unittest.skipUnless(os.name == 'nt', 'Windows named-pipe cancellation integration')
class NativeCancellationGateTests(unittest.TestCase):
    def test_client_close_interrupts_an_active_native_pipe_receive(self):
        """T-01 transport evidence: CancelIoEx ends an in-flight receive."""
        name = 'online-trader-client-cancel-' + uuid.uuid4().hex
        client = CancellablePipeClient()
        with PipeListener(name) as listener, ThreadPoolExecutor(max_workers=2) as pool:
            accepted = pool.submit(listener.accept, timeout=3)
            entered = threading.Event()

            def receive():
                with client.open_connection(name, timeout=3, validator=validate_message) as connection:
                    entered.set()
                    return connection.recv(timeout=8)

            pending = pool.submit(receive)
            server = accepted.result(timeout=4)
            try:
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                client.close()
                with self.assertRaises(TraderError):
                    pending.result(timeout=2)
                self.assertLess(time.monotonic() - started, 2)
            finally:
                server.close()


if __name__ == '__main__':
    unittest.main()
