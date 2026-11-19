"""Trading Workbench Runtime controls; Qt uses public command bodies and never stores tokens."""

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication

from online_trader.desktop.workbench import WorkbenchWindow


class RuntimeWatchStub(QObject):
    updated = Signal(object)
    observed = Signal(object)
    unavailable = Signal(str)
    finished = Signal()

    def __init__(self, pipe, parent):
        super().__init__(parent)
        self.pipe = pipe

    def start(self): pass
    def align(self): pass
    def isRunning(self): return False
    def requestInterruption(self): pass


def runtime_view():
    return {
        'view_schema': 'workbench/1', 'account_key': 'DEV:runtime-workbench',
        'kernel_boot_id': 'boot-runtime-1', 'checkpoint_event_seq': '2',
        'entry_authority': 'DISARMED', 'execution_scope': 'DISARMED',
        'connection': {'broker_connected': True, 'synchronized': True, 'fault': False,
                       'recovery_required': False, 'storage_failed': False, 'clock_fault': None,
                       'pending_transactions': 0},
        'configuration': {'rule_set_id': 'rule-runtime', 'config_revision': '7'},
        'markets': [], 'cycles': [], 'orders': [], 'trades': [], 'blocks': [], 'commands': [],
        'policies': [], 'truncated': False,
    }


class RuntimeWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / 'workbench.ini'), QSettings.IniFormat)
        self.preferences = patch('online_trader.desktop.workbench.workbench_preferences', return_value=self.settings)
        self.preferences.start()
        self.addCleanup(self.preferences.stop)
        with patch('online_trader.desktop.runtime_watch.RuntimeWatch', RuntimeWatchStub):
            self.window = WorkbenchWindow('Runtime.Workbench.Test', runtime_view=True)
        self.addCleanup(self._close)
        self.window._boot = 'boot-runtime-1'
        self.window.view = runtime_view()
        self.window._stale = False
        self.window.snapshot_published_ns = time.monotonic_ns()

    def _close(self):
        self.window.close()
        self.app.processEvents()

    def test_direct_submit_binds_current_runtime_and_persists_no_token(self):
        self.window._runtime_operator_id = 'desk'
        self.window._runtime_operator_token = 'secret-' + 'x' * 40
        self.window._runtime_operator_operations = frozenset({'SET_ENTRY_POLICY'})
        captured = []
        with patch.object(self.window, '_start_runtime_request', side_effect=lambda *args: captured.append(args)):
            self.window._start_runtime_submit(
                'SET_ENTRY_POLICY', {'scope_type': 'CONTRACT', 'scope_ids': ['DCE.p2701']},
                {'origin': 'STRATEGY', 'enabled': False, 'expected_policy_revision': '0',
                 'reason': 'runtime test', 'cancel_owned_entry_remainders': False})
        self.assertEqual(len(captured), 1)
        action, operator, token, body = captured[0]
        self.assertEqual((action, operator), ('submit', 'desk'))
        self.assertEqual(token, self.window._runtime_operator_token)
        self.assertEqual(body['binding'], {'account_key': 'DEV:runtime-workbench',
                                           'kernel_boot_id': 'boot-runtime-1', 'config_revision': '7'})
        self.assertEqual(body['operation'], 'SET_ENTRY_POLICY')
        self.assertEqual(self.window.receipt_identity, {'command_id': body['command_id']})
        persisted = self.settings.value('runtime/pending_receipt', '', str)
        self.assertIn(body['command_id'], persisted)
        self.assertNotIn(token, persisted)

    def test_locked_operator_prompts_before_creating_a_command(self):
        pending = []
        with patch.object(self.window, '_prompt_runtime_operator', side_effect=lambda value: pending.append(value)):
            self.window._start_runtime_submit('MANUAL_CLOSE', {'scope_type': 'CYCLE', 'scope_ids': ['cycle-1']},
                                              {'position_cycle_id': 'cycle-1', 'quantity': 1})
        self.assertEqual(pending[0][0], 'submit')
        self.assertIsNone(self.window.receipt_identity)

    def test_authentication_unlocks_capabilities_but_token_stays_memory_only(self):
        self.window._runtime_auth_token = 'memory-only-' + 'x' * 32
        self.window._runtime_pending_action = None
        self.window._runtime_request_result({'action': 'authenticate', 'value': {
            'status': 'AUTHENTICATED', 'operator': 'desk', 'actor': 'operator-desk',
            'account_key': self.window.view['account_key'], 'operations': ['MANUAL_CLOSE'],
            'expires_at_utc': '2099-01-01T00:00:00Z',
        }})
        self.assertEqual(self.window._runtime_operator_id, 'desk')
        self.assertEqual(self.window._runtime_operator_operations, frozenset({'MANUAL_CLOSE'}))
        self.assertTrue(self.window._runtime_operator_token.startswith('memory-only-'))
        self.assertNotIn('memory-only-', self.settings.value('runtime/operator_id', '', str))
        self.window._clear_runtime_operator()
        self.assertIsNone(self.window._runtime_operator_token)


if __name__ == '__main__':
    unittest.main()
