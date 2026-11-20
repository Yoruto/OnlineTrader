"""Trading Workbench delegates Runtime controls to the single CommandBus."""

from types import MappingProxyType
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.errors import TraderError


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
        settings = QSettings(str(Path(self.temp.name) / 'workbench.ini'), QSettings.IniFormat)
        self.preferences = patch(
            'online_trader.desktop.workbench.workbench_preferences', return_value=settings)
        self.coordinator_start = patch(
            'online_trader.desktop.runtime_client.client.RuntimeClientCoordinator.start')
        self.coordinator_close = patch(
            'online_trader.desktop.runtime_client.client.RuntimeClientCoordinator.close')
        for item in (self.preferences, self.coordinator_start, self.coordinator_close):
            item.start()
            self.addCleanup(item.stop)
        self.window = WorkbenchWindow('Runtime.Workbench.Test', runtime_view=True)
        self.addCleanup(self._close)
        self.window._boot = 'boot-runtime-1'
        self.window.view = runtime_view()
        self.window._stale = False
        self.window.snapshot_published_ns = time.monotonic_ns()

    def _close(self):
        self.window.close()
        self.app.processEvents()

    def test_submit_delegates_the_current_intent_to_command_bus(self):
        scope = {'scope_type': 'CONTRACT', 'scope_ids': ['DCE.p2701']}
        parameters = {'origin': 'STRATEGY', 'enabled': False,
                      'expected_policy_revision': '0', 'reason': 'runtime test',
                      'cancel_owned_entry_remainders': False}

        with patch.object(self.window._runtime_command_bus, 'submit', return_value='qt-current') as submit:
            self.window._start_runtime_submit('SET_ENTRY_POLICY', scope, parameters)

        submit.assert_called_once_with('SET_ENTRY_POLICY', scope, parameters)
        self.assertEqual(self.window.receipt_identity, {'command_id': 'qt-current'})
        self.assertIn('qt-current', self.window.home_feedback)

    def test_submit_without_a_current_view_does_not_reach_command_bus(self):
        self.window.view = None

        with patch.object(self.window._runtime_command_bus, 'submit') as submit:
            self.window._start_runtime_submit(
                'MANUAL_CLOSE', {'scope_type': 'CYCLE', 'scope_ids': ['cycle-1']},
                {'position_cycle_id': 'cycle-1', 'quantity': 1})

        submit.assert_not_called()
        self.assertIn('当前完整 Runtime 状态', self.window.home_feedback)

    def test_command_bus_rejection_is_shown_without_a_receipt_identity(self):
        with patch.object(
                self.window._runtime_command_bus, 'submit',
                side_effect=TraderError('E_CAPABILITY', 'operation unavailable')):
            self.window._start_runtime_submit(
                'MANUAL_CLOSE', {'scope_type': 'CYCLE', 'scope_ids': ['cycle-1']},
                {'position_cycle_id': 'cycle-1', 'quantity': 1})

        self.assertIsNone(self.window.receipt_identity)
        self.assertIn('E_CAPABILITY', self.window.home_feedback)

    def test_frozen_runtime_account_display_is_used_by_header_and_picker(self):
        view = runtime_view()
        view['account_display'] = MappingProxyType({
            'provider': 'TQ_SIMNOW', 'alias': '开发模拟', 'masked_login': '12***89',
        })
        self.window.view = view

        self.assertEqual(self.window.display_account(), ('SimNow 模拟', '12***89'))
        self.window.dashboard.reload_accounts()
        self.assertIn('开发模拟', self.window.dashboard.account_picker.currentText())
        self.assertFalse(self.window.dashboard.account_picker.isEnabled())
        self.assertFalse(self.window.accounts_button.isEnabled())

    def test_new_workbench_process_starts_without_legacy_pending_state(self):
        self.assertEqual(self.window._runtime_client_store.pending, {})
        self.window._runtime_command_bus.migrate_legacy_pending()
        self.assertEqual(self.window._runtime_client_store.pending, {})


if __name__ == '__main__':
    unittest.main()
