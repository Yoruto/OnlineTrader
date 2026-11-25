"""Trading Workbench delegates Runtime controls to the single CommandBus."""

import json
from types import MappingProxyType, SimpleNamespace
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from online_trader.configuration.dsv import DsvProfile
from online_trader.desktop.runtime_client.state import PendingCommandStatus
from online_trader.desktop.symbol_editor import SymbolEditorDialog
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.errors import TraderError
from online_trader.runtime.portable_config import PortableAccountStore
from qa.target_owner.offline_runtime import profile as single_contract_profile


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

    def _cleanup_runtime_window(self, window):
        window._runtime_shutdown_verified = True
        window._closing = True
        window.close()
        window._finalize_diagnostic_sink(clean_shutdown=True)
        window.deleteLater()
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

    def _portable_runtime_root(self, initial_profile):
        root = Path(self.temp.name) / 'portable-runtime'
        (root / 'private').mkdir(parents=True, exist_ok=True)
        (root / 'runtime.json').write_text(json.dumps({
            'schema_version': 'ONLINE_TRADER_RUNTIME/1',
            'selected_account': 'sim1',
            'accounts': [{
                'id': 'sim1', 'alias': '开发模拟', 'provider': 'TQKQ',
                'broker': '快期模拟', 'line': '默认',
            }],
        }, ensure_ascii=False), encoding='utf-8')
        (root / 'private' / 'OnlineTrader-defaults.json').write_text(json.dumps({
            'accounts': {'sim1': {'username': 'user-one', 'password': 'secret'}},
        }, ensure_ascii=False), encoding='utf-8')
        (root / 'OnlineTrader-parameters.json').write_text(
            json.dumps(initial_profile.to_settings(), ensure_ascii=False),
            encoding='utf-8',
        )
        return root

    @staticmethod
    def _state_from_profile(active_profile, *, boot='boot-runtime-1', authority='DISARMED', seq='2'):
        settings = active_profile.to_record()['settings']
        contracts = [row['code'] for row in settings['symbols']]
        markets = [{
            'contract_id': row['code'],
            'name': row['name'],
            'trading_day': '2026-11-24',
            'last': '7500',
            'bid1': '7499',
            'ask1': '7501',
            'fresh': True,
            'enabled': True,
            'entry_reasons': [],
            'trigger_prices': {'LONG': '7596', 'SHORT': '7404'},
            'price_tick': '1',
            'market_session_active': True,
        } for row in settings['symbols']]
        return {
            'base': {
                'records': [{
                    'record_type': 'ACCOUNT',
                    'record_id': 'TQKQ:user-one',
                    'record_revision': seq,
                    'kernel_state': 'READY',
                    'entry_authority': authority,
                    'funds_quality': 'CURRENT',
                    'equity': '100000',
                    'available': '90000',
                    'margin': '10000',
                    'fees': '0',
                }],
            },
            'view': {
                'view_schema': 'workbench/1',
                'account_key': 'TQKQ:user-one',
                'kernel_boot_id': boot,
                'checkpoint_event_seq': seq,
                'entry_authority': authority,
                'execution_scope': 'ONLINE_SIMULATION',
                'connection': {
                    'broker_connected': True, 'synchronized': True, 'fault': False,
                    'recovery_required': False, 'storage_failed': False, 'clock_fault': None,
                    'pending_transactions': 0,
                },
                'configuration': {
                    'rule_set_id': 'DSV_TARGET_EXECUTION',
                    'config_revision': '1',
                    'live_parameter_save': True,
                    'strategy_profile_hash': active_profile.content_hash,
                    'strategy_profile': active_profile.to_record(),
                    'scoped_contracts': contracts,
                    'strategy_symbols': settings['symbols'],
                },
                'markets': markets,
                'cycles': [],
                'orders': [],
                'trades': [],
                'blocks': [],
                'commands': [],
                'policies': [
                    {'policy_id': 'target-authority-current-boot', 'policy_kind': 'AUTHORITY'},
                    {'policy_id': 'target-execution-current-boot', 'policy_kind': 'EXECUTION'},
                ],
                'capabilities': {'revision': seq, 'operations': [
                    {'operation': 'SAVE_DSV_PARAMETERS', 'scope_type': 'ACCOUNT', 'allowed': True, 'reason': None},
                ]},
                'truncated': False,
            },
        }

    def test_runtime_view_syncs_effective_profile_without_overwriting_unsaved_draft(self):
        base = single_contract_profile()
        root = self._portable_runtime_root(base)
        window = WorkbenchWindow('Runtime.Sync.Test', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        window._runtime_client_store.apply(self._state_from_profile(base), {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64}, time.monotonic_ns())
        self.assertEqual([row['code'] for row in window.dashboard.symbols], ['DCE.p2701'])
        self.assertEqual(window.dashboard.symbol_grid.rowCount(), 1)

        def accept_silver(dialog):
            dialog.fields['code'].setText('ag2702')
            self.assertTrue(dialog.validate_symbol())
            return QDialog.Accepted

        with patch.object(SymbolEditorDialog, 'exec', accept_silver):
            window.dashboard.add_symbol()
        self.assertEqual(window.dashboard.symbol_grid.rowCount(), 2)
        self.assertIn('SHFE.ag2702', window.dashboard.symbol_preview_state.text())

        updated_base = DsvProfile.from_record(base.to_record())
        raw = updated_base.to_record()
        raw['settings']['symbols'][0]['open_offset'] = '88'
        updated_base = DsvProfile.from_record(raw)
        window._runtime_client_store.apply(self._state_from_profile(updated_base, seq='3'), {'generation': 'g1', 'sequence': '2', 'digest': 'b' * 64}, time.monotonic_ns())
        self.assertEqual(window.dashboard.symbol_grid.rowCount(), 2)
        self.assertEqual(window.dashboard.preview_symbols[1]['code'], 'SHFE.ag2702')

    def test_adding_silver_while_live_submits_hot_add_and_focuses_monitor(self):
        base = single_contract_profile()
        root = self._portable_runtime_root(base)
        window = WorkbenchWindow(r'\\.\pipe\runtime-hot-add', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        window._runtime_client_store.apply(
            self._state_from_profile(base, authority='ARMED'),
            {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64},
            time.monotonic_ns(),
        )

        def accept_silver(dialog):
            dialog.fields['code'].setText('ag2702')
            self.assertTrue(dialog.validate_symbol())
            return QDialog.Accepted

        with patch.object(SymbolEditorDialog, 'exec', accept_silver):
            window.dashboard.add_symbol()
        self.assertTrue(window.dashboard.has_unsaved_symbol_changes())

        submitted = []

        def fake_submit(operation, scope, parameters):
            submitted.append((operation, scope, parameters))
            return 'cmd-hot-add-1'

        with (
            patch.object(window._runtime_command_bus, 'submit', side_effect=fake_submit),
            patch('online_trader.runtime.shutdown.shutdown_runtime_and_wait') as stop_call,
        ):
            window.dashboard.export_parameters_preview()

        stop_call.assert_not_called()
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][0], 'SAVE_DSV_PARAMETERS')
        self.assertEqual(
            [row['code'] for row in submitted[0][2]['profile']['settings']['symbols']],
            ['DCE.p2701', 'SHFE.ag2702'],
        )
        updated = DsvProfile.from_record(submitted[0][2]['profile'])
        window._runtime_command_changed(SimpleNamespace(
            command_id='cmd-hot-add-1',
            operation='SAVE_DSV_PARAMETERS',
            status=PendingCommandStatus.COMPLETED,
            receipt={
                'command_id': 'cmd-hot-add-1',
                'status': 'SUCCEEDED',
                'error_code': 'NONE',
                'reason': '新合约参数已保存并生效，已开始同步行情。',
            },
            reason='新合约参数已保存并生效，已开始同步行情。',
        ))
        self.assertIn('已加入运行中策略', window.dashboard.symbol_feedback_label.text())

        window._runtime_client_store.apply(
            self._state_from_profile(updated, authority='ARMED', seq='2'),
            {'generation': 'g1', 'sequence': '2', 'digest': 'b' * 64},
            time.monotonic_ns(),
        )
        self.assertIn('SHFE.ag2702', window.dashboard.dashboard_market_rows)
        selected_cell = window.dashboard.dashboard_markets.item(
            window.dashboard.dashboard_markets.currentRow(), 0,
        )
        self.assertIn('SHFE.ag2702', selected_cell.text())
        self.assertFalse(window.dashboard.has_unsaved_symbol_changes())

    def test_hot_add_rejection_offers_next_boot_overrides_without_interrupting_runtime(self):
        base = single_contract_profile()
        root = self._portable_runtime_root(base)
        window = WorkbenchWindow(r'\\.\pipe\runtime-hot-reject', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        window._runtime_client_store.apply(
            self._state_from_profile(base, authority='ARMED'),
            {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64},
            time.monotonic_ns(),
        )

        def accept_silver(dialog):
            dialog.fields['code'].setText('ag2702')
            self.assertTrue(dialog.validate_symbol())
            return QDialog.Accepted

        with patch.object(SymbolEditorDialog, 'exec', accept_silver):
            window.dashboard.add_symbol()

        class _ClickSaveNextBoot:
            Icon = QMessageBox.Icon
            ButtonRole = QMessageBox.ButtonRole
            StandardButton = QMessageBox.StandardButton
            critical = staticmethod(QMessageBox.critical)
            warning = staticmethod(QMessageBox.warning)
            question = staticmethod(QMessageBox.question)

            def __init__(self, _parent=None):
                self._buttons = []

            def setIcon(self, *_args): pass
            def setWindowTitle(self, *_args): pass
            def setText(self, *_args): pass

            def addButton(self, text, role):
                btn = object()
                self._buttons.append((text, role, btn))
                return btn

            def exec(self):
                return 0

            def clickedButton(self):
                return next(btn for text, _role, btn in self._buttons if '下次启动' in text)

        with patch.object(window._runtime_command_bus, 'submit', return_value='cmd-hot-reject'):
            window.dashboard.export_parameters_preview()

        with (
            patch('online_trader.desktop.workbench.QMessageBox', _ClickSaveNextBoot),
            patch('online_trader.runtime.shutdown.shutdown_runtime_and_wait') as stop_call,
        ):
            window._runtime_command_changed(SimpleNamespace(
                command_id='cmd-hot-reject',
                operation='SAVE_DSV_PARAMETERS',
                status=PendingCommandStatus.REJECTED,
                receipt={
                    'command_id': 'cmd-hot-reject',
                    'status': 'REJECTED',
                    'error_code': 'E_CALENDAR_INCOMPLETE',
                    'reason': '新增合约缺少交易日历信息',
                },
                reason='新增合约缺少交易日历信息',
            ))

        stop_call.assert_not_called()
        reloaded = PortableAccountStore(root).profile()
        self.assertEqual([row['code'] for row in reloaded.to_settings()['symbols']], ['DCE.p2701', 'SHFE.ag2702'])
        self.assertIn('重启后台服务后生效', window.dashboard.symbol_feedback_label.text())

    def test_replacing_contract_while_disarmed_saves_overrides_and_restarts_runtime_to_monitor(self):
        base = single_contract_profile()
        root = self._portable_runtime_root(base)
        window = WorkbenchWindow(r'\\.\pipe\runtime-boot-1', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        window._runtime_client_store.apply(self._state_from_profile(base), {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64}, time.monotonic_ns())

        def accept_silver(dialog):
            dialog.fields['code'].setText('ag2702')
            self.assertTrue(dialog.validate_symbol())
            return QDialog.Accepted

        with patch.object(SymbolEditorDialog, 'exec', accept_silver):
            window.dashboard.add_symbol()
        # Removing DCE.p2701 turns the draft into a replacement rather than a pure live addition.
        window.dashboard.symbol_grid.selectRow(0)
        with patch('online_trader.desktop.workbench_layout.QMessageBox.question', return_value=QMessageBox.Yes):
            window.dashboard.remove_selected_symbols()
        self.assertTrue(window.dashboard.has_unsaved_symbol_changes())

        controllers = []

        class _FakeBootstrapController(QObject):
            ready = Signal(str)
            failed = Signal(object)
            cancelled = Signal()

            def __init__(self, _root, *, account=None, mode='strategy', timeout=90, parent=None):
                super().__init__(parent)
                self.started = False
                controllers.append(self)

            def start(self):
                self.started = True

            def shutdown(self):
                pass

        class _ClickFirstAcceptButton:
            Icon = QMessageBox.Icon
            ButtonRole = QMessageBox.ButtonRole
            StandardButton = QMessageBox.StandardButton
            critical = staticmethod(QMessageBox.critical)
            warning = staticmethod(QMessageBox.warning)
            question = staticmethod(QMessageBox.question)

            def __init__(self, _parent=None):
                self._buttons = []

            def setIcon(self, *_args): pass
            def setWindowTitle(self, *_args): pass
            def setText(self, *_args): pass

            def addButton(self, text, role):
                btn = object()
                self._buttons.append((text, role, btn))
                return btn

            def exec(self):
                return 0

            def clickedButton(self):
                return next(btn for text, _role, btn in self._buttons if '立即重启' in text)

        with (
            patch('online_trader.desktop.workbench.QMessageBox', _ClickFirstAcceptButton),
            patch('online_trader.runtime.shutdown.inspect_runtime_for_exit', return_value=(window.view, 5555, None)),
            patch('online_trader.runtime.shutdown.maintenance_stop_runtime_and_wait', return_value=(True, None)) as stop_call,
            patch('online_trader.desktop.runtime_bootstrap.BootstrapController', _FakeBootstrapController),
        ):
            window.dashboard.export_parameters_preview()

        stop_call.assert_called_once()
        reloaded = PortableAccountStore(root).profile()
        self.assertEqual([row['code'] for row in reloaded.to_settings()['symbols']], ['SHFE.ag2702'])
        self.assertEqual(len(controllers), 1)
        self.assertTrue(controllers[0].started)

        controllers[0].ready.emit(r'\\.\pipe\runtime-boot-2')
        window._runtime_client_store.apply(
            self._state_from_profile(reloaded, boot='boot-runtime-2', seq='4'),
            {'generation': 'g2', 'sequence': '1', 'digest': 'c' * 64},
            time.monotonic_ns(),
        )
        self.assertEqual(window.pipe_name, r'\\.\pipe\runtime-boot-2')
        self.assertIn('SHFE.ag2702', window.dashboard.dashboard_market_rows)
        selected_cell = window.dashboard.dashboard_markets.item(
            window.dashboard.dashboard_markets.currentRow(), 0,
        )
        self.assertIn('SHFE.ag2702', selected_cell.text())
        self.assertFalse(window.dashboard.has_unsaved_symbol_changes())

    def test_removing_contract_with_silver_while_armed_persists_next_boot_overrides_without_interrupting_runtime(self):
        base = single_contract_profile()
        root = self._portable_runtime_root(base)
        window = WorkbenchWindow(r'\\.\pipe\runtime-armed', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        window._runtime_client_store.apply(
            self._state_from_profile(base, authority='ARMED'),
            {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64},
            time.monotonic_ns(),
        )

        def accept_silver(dialog):
            dialog.fields['code'].setText('ag2702')
            self.assertTrue(dialog.validate_symbol())
            return QDialog.Accepted

        with patch.object(SymbolEditorDialog, 'exec', accept_silver):
            window.dashboard.add_symbol()
        window.dashboard.symbol_grid.selectRow(0)
        with patch('online_trader.desktop.workbench_layout.QMessageBox.question', return_value=QMessageBox.Yes):
            window.dashboard.remove_selected_symbols()

        class _ClickSaveNextBoot:
            Icon = QMessageBox.Icon
            ButtonRole = QMessageBox.ButtonRole
            StandardButton = QMessageBox.StandardButton
            critical = staticmethod(QMessageBox.critical)
            warning = staticmethod(QMessageBox.warning)
            question = staticmethod(QMessageBox.question)

            def __init__(self, _parent=None):
                self._buttons = []

            def setIcon(self, *_args): pass
            def setWindowTitle(self, *_args): pass
            def setText(self, *_args): pass

            def addButton(self, text, role):
                btn = object()
                self._buttons.append((text, role, btn))
                return btn

            def exec(self):
                return 0

            def clickedButton(self):
                return next(btn for text, _role, btn in self._buttons if '下次启动' in text)

        with (
            patch('online_trader.desktop.workbench.QMessageBox', _ClickSaveNextBoot),
            patch('online_trader.runtime.shutdown.shutdown_runtime_and_wait') as stop_call,
        ):
            window.dashboard.export_parameters_preview()

        stop_call.assert_not_called()
        reloaded = PortableAccountStore(root).profile()
        self.assertEqual([row['code'] for row in reloaded.to_settings()['symbols']], ['SHFE.ag2702'])
        self.assertIn('重启后台服务后生效', window.dashboard.symbol_feedback_label.text())

    def test_runtime_catalog_load_uses_portable_store_when_legacy_store_is_absent(self):
        base = single_contract_profile()
        root = self._portable_runtime_root(base)
        window = WorkbenchWindow(r'\\.\pipe\runtime-catalog', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        window._runtime_client_store.apply(
            self._state_from_profile(base),
            {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64},
            time.monotonic_ns(),
        )
        started = []
        with patch.object(window.dashboard, 'start_market_read', side_effect=lambda *args, **kwargs: started.append((args, kwargs))):
            self.assertTrue(window.dashboard.load_manual_catalog())
        self.assertEqual(len(started), 1)
        self.assertIsInstance(started[0][1]['store'], PortableAccountStore)
        self.assertTrue(started[0][1]['catalog'])

    def test_home_attention_preserves_command_issue_when_view_is_none(self):
        self.window.view = None
        self.window.home_issue = '命令结果待核对，请查看交易详情中的命令回执'
        self.window.dashboard.render_home()
        self.assertFalse(self.window.dashboard.home_attention.isHidden())
        self.assertIn('命令结果待核对', self.window.dashboard.home_pending.text())
        self.assertFalse(self.window.dashboard.home_detail_button.isHidden())
        self.assertTrue(self.window.dashboard.home_reconcile_button.isHidden())

    def test_home_attention_shows_generic_recovery_fallback_while_honoring_capability_gate(self):
        view = runtime_view()
        view['connection']['recovery_required'] = True
        view['blocks'] = []
        view['capabilities'] = {
            'revision': '2',
            'observed_at_utc': '2026-11-24T01:00:00Z',
            'operations': [
                {'operation': 'REQUEST_RECONCILIATION', 'scope_type': 'ACCOUNT',
                 'allowed': False, 'reason': 'RECONCILE_TEMPORARILY_UNAVAILABLE'},
            ],
        }
        self.window.view = view
        self.window.render_view()
        dashboard = self.window.dashboard
        self.assertFalse(dashboard.home_attention.isHidden())
        self.assertIn('需要人工核对', dashboard.home_pending.text())
        self.assertFalse(dashboard.home_detail_button.isHidden())
        self.assertFalse(dashboard.home_reconcile_button.isHidden())
        self.assertFalse(dashboard.home_reconcile_button.isEnabled())

    def test_switch_runtime_account_verifies_maintenance_and_boots_monitor_only(self):
        base = single_contract_profile()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'runtime'
        (root / 'private').mkdir(parents=True)
        (root / 'runtime.json').write_text(json.dumps({
            'schema_version': 'ONLINE_TRADER_RUNTIME/1',
            'selected_account': 'sim1',
            'accounts': [
                {'id': 'sim1', 'alias': '模拟1', 'provider': 'TQKQ', 'broker': '快期模拟', 'line': '默认'},
                {'id': 'sim2', 'alias': '模拟2', 'provider': 'TQKQ', 'broker': '快期模拟', 'line': '默认'},
            ],
        }), encoding='utf-8')
        (root / 'private' / 'OnlineTrader-defaults.json').write_text(json.dumps({
            'accounts': {
                'sim1': {'username': 'user-one', 'password': 'secret'},
                'sim2': {'username': 'user-two', 'password': 'secret2'},
            },
        }), encoding='utf-8')
        (root / 'OnlineTrader-parameters.json').write_text(
            json.dumps(base.to_settings(), ensure_ascii=False), encoding='utf-8',
        )
        store = PortableAccountStore(root)
        acct2 = store.read()['accounts'][1]

        window = WorkbenchWindow(r'\\.\pipe\runtime-boot-1', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        view = self._state_from_profile(base)
        view['responsibility'] = {'maintenance_allowed': True, 'blockers': []}
        window._runtime_client_store.apply(
            view, {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64}, time.monotonic_ns(),
        )

        controllers = []

        class _FakeBootstrapController(QObject):
            ready = Signal(str)
            failed = Signal(object)
            cancelled = Signal()

            def __init__(self, _root, *, account=None, attach_policy='attach-any',
                         expected_account_key=None, parent=None):
                super().__init__(parent)
                self.account = account
                self.attach_policy = attach_policy
                self.expected_account_key = expected_account_key
                self.started = False
                controllers.append(self)

            def start(self):
                self.started = True

            def shutdown(self):
                pass

        with (
            patch('online_trader.runtime.shutdown.inspect_runtime_for_exit', return_value=(window.view, 5555, None)),
            patch('online_trader.runtime.instance.runtime_owner_exists', return_value=True),
            patch('online_trader.runtime.shutdown.maintenance_stop_runtime_and_wait', return_value=(True, None)) as maint_stop,
            patch('online_trader.desktop.runtime_bootstrap.BootstrapController', _FakeBootstrapController),
        ):
            ok = window.switch_runtime_account(acct2['account_id'])
        self.assertTrue(ok)
        maint_stop.assert_called_once()
        self.assertEqual(maint_stop.call_args.kwargs['reason'], 'SWITCH_ACCOUNT')
        self.assertEqual(len(controllers), 1)
        self.assertEqual(controllers[0].attach_policy, 'attach-exact')
        self.assertEqual(controllers[0].expected_account_key, acct2['account_key'])
        self.assertTrue(controllers[0].started)

        controllers[0].ready.emit(r'\\.\pipe\runtime-boot-2')
        self.assertEqual(PortableAccountStore(root).read()['selected_id'], acct2['account_id'])

    def test_switch_runtime_account_aborts_when_maintenance_stop_fails(self):
        base = single_contract_profile()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'runtime'
        (root / 'private').mkdir(parents=True)
        (root / 'runtime.json').write_text(json.dumps({
            'schema_version': 'ONLINE_TRADER_RUNTIME/1',
            'selected_account': 'sim1',
            'accounts': [
                {'id': 'sim1', 'alias': '模拟1', 'provider': 'TQKQ', 'broker': '快期模拟', 'line': '默认'},
                {'id': 'sim2', 'alias': '模拟2', 'provider': 'TQKQ', 'broker': '快期模拟', 'line': '默认'},
            ],
        }), encoding='utf-8')
        (root / 'private' / 'OnlineTrader-defaults.json').write_text(json.dumps({
            'accounts': {
                'sim1': {'username': 'user-one', 'password': 'secret'},
                'sim2': {'username': 'user-two', 'password': 'secret2'},
            },
        }), encoding='utf-8')
        (root / 'OnlineTrader-parameters.json').write_text(
            json.dumps(base.to_settings(), ensure_ascii=False), encoding='utf-8',
        )
        store = PortableAccountStore(root)
        acct1, acct2 = store.read()['accounts']

        window = WorkbenchWindow(r'\\.\pipe\runtime-boot-1', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        view = self._state_from_profile(base)
        view['responsibility'] = {'maintenance_allowed': True, 'blockers': []}
        window._runtime_client_store.apply(
            view, {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64}, time.monotonic_ns(),
        )

        with (
            patch('online_trader.desktop.workbench.QMessageBox.critical') as critical,
            patch('online_trader.runtime.shutdown.inspect_runtime_for_exit', return_value=(window.view, 5555, None)),
            patch('online_trader.runtime.instance.runtime_owner_exists', return_value=True),
            patch('online_trader.runtime.shutdown.maintenance_stop_runtime_and_wait',
                  return_value=(False, '未能确认无责任受控停机证明')),
        ):
            ok = window.switch_runtime_account(acct2['account_id'])
        self.assertFalse(ok)
        critical.assert_called_once()
        self.assertEqual(window.pipe_name, r'\\.\pipe\runtime-boot-1')
        self.assertEqual(PortableAccountStore(root).read()['selected_id'], acct1['account_id'])

    def test_prepare_update_restart_enforces_maintenance_stop(self):
        base = single_contract_profile()
        root = self._portable_runtime_root(base)
        window = WorkbenchWindow(r'\\.\pipe\runtime-boot-1', runtime_view=True, runtime_root=root)
        self.addCleanup(lambda w=window: self._cleanup_runtime_window(w))
        view = self._state_from_profile(base)
        view['responsibility'] = {'maintenance_allowed': True, 'blockers': []}
        window._runtime_client_store.apply(
            view, {'generation': 'g1', 'sequence': '1', 'digest': 'a' * 64}, time.monotonic_ns(),
        )
        with (
            patch('online_trader.runtime.shutdown.inspect_runtime_for_exit', return_value=(window.view, 5555, None)),
            patch('online_trader.runtime.instance.runtime_owner_exists', return_value=True),
            patch('online_trader.runtime.shutdown.maintenance_stop_runtime_and_wait',
                  return_value=(False, '未能确认无责任受控停机证明')),
        ):
            with self.assertRaises(TraderError) as err:
                window.prepare_update_restart()
            self.assertEqual(err.exception.code, 'E_RUNTIME_STOP')

        with (
            patch('online_trader.runtime.shutdown.inspect_runtime_for_exit', return_value=(window.view, 5555, None)),
            patch('online_trader.runtime.instance.runtime_owner_exists', return_value=True),
            patch('online_trader.runtime.shutdown.maintenance_stop_runtime_and_wait',
                  return_value=(True, None)) as maint_stop,
        ):
            window.prepare_update_restart()
        maint_stop.assert_called_once()
        self.assertEqual(maint_stop.call_args.kwargs['reason'], 'UPDATE')
        self.assertTrue(window._update_restart_in_progress)

    def test_workbench_renders_config_recovery_notice_and_running_fault_intent(self):
        view = runtime_view()
        view['config_recovery_notice'] = {
            'state': 'MATCHES_NEW',
            'message': '上次新参数已保存但运行曾中断，已自动加载新参数并保持停开，请看一眼合约和手数后手动启动策略。',
            'apply_id': 'apply-1',
        }
        self.window.view = view
        self.window.render_view()
        self.assertFalse(self.window.dashboard.home_attention.isHidden())
        self.assertIn('已自动校验策略参数', self.window.dashboard.home_pending.text())
        self.assertIn('上次新参数已保存但运行曾中断', self.window.dashboard.home_pending.text())

        view_fault = runtime_view()
        view_fault['strategy_intent'] = 'RUNNING'
        view_fault['connection']['recovery_required'] = True
        self.window.view = view_fault
        self.window.render_view()
        self.assertEqual(self.window.dashboard.home_state.text(), '策略已因异常暂停')


if __name__ == '__main__':
    unittest.main()
