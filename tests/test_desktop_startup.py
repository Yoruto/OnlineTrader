import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, QSettings
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget
from PySide6.QtTest import QTest

from online_trader.accounts import AccountStore
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import load_profile
from online_trader.contracts.codec import strict_loads
from online_trader.desktop.startup import StrategyStarter
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.errors import TraderError
from tests.qt_lifecycle import dispose_widget, flush_deferred_deletes
from tests.test_accounts import MemoryVault


class DesktopStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AccountStore(self.root / 'accounts', vault=MemoryVault())
        state = self.store.initialize_simulation_accounts({
            'simnow_username': 'offline-sn-start', 'simnow_password': 'synthetic-secret',
            'kq_username': 'offline-market', 'kq_password': 'synthetic-market-secret'})
        self.account_id = state['selected_id']
        self.profile = load_confirmed_baseline()[0]

    def idle(self, starter):
        deadline = time.monotonic() + 5
        while starter._worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.assertIsNone(starter._worker)

    def starter(self, parent=None):
        starter = StrategyStarter(self.store, self.account_id, self.profile, parent)
        if parent is None:
            self.addCleanup(flush_deferred_deletes)
            self.addCleanup(starter.deleteLater)
            self.addCleanup(starter.status_timer.stop)
        return starter

    def window(self, **options):
        settings = QSettings(str(self.root / 'view.ini'), QSettings.IniFormat)
        with (patch('online_trader.desktop.workbench.workbench_preferences', return_value=settings),
              patch('online_trader.desktop.workbench_layout.workbench_preferences', return_value=settings)):
            window = WorkbenchWindow(None, store=self.store, account_id=self.account_id, **options)
        self.addCleanup(self.close_window, window)
        window.timer.stop()
        window.show()
        self.app.processEvents()
        return window

    def test_main_button_rechecks_dsv_before_writing_start_request(self):
        path = self.root / 'config.json'
        path.write_text(self.profile.to_json(), encoding='utf-8')
        window = self.window(dsv_config=path)
        self.assertEqual(window.dashboard.symbol_grid.item(4, 1).text(), 'SHFE.ag2702')

        changed = self.profile.to_settings()
        changed['symbols'][4].update(code='SHFE.ag2702', open_offset=96, stop_offset=15, profit_offset=139)
        changed['symbols'][4].pop('code_new')
        changed['trade']['auto_start_on_launch'] = False
        path.write_text(DsvProfile.from_settings(changed).to_json(), encoding='utf-8')
        expected = load_profile(path, dsv=True)
        with patch('online_trader.runtime.startup_environment.launch_bootstrap', return_value={'pid': 123}) as launch:
            window.command_buttons['arm'].click()
            launch.assert_not_called()
            self.assertFalse((self.store.directory / 'desktop-starts').exists())
            self.assertIn('DSV 参数已变化', window.home_feedback)
            self.assertEqual(window.dashboard.symbol_grid.item(4, 1).text(), 'SHFE.ag2702')
            self.assertEqual(window.dashboard.symbol_grid.item(4, 5).text(), '96')
            self.assertFalse(window.dashboard.parameter_switches['auto_start_on_launch'].isChecked())
            self.assertEqual(window.dashboard.calculator.base_hash, expected.content_hash)

            window.command_buttons['arm'].click()
            self.idle(window._strategy_starter)
            launch.assert_called_once()
        request = strict_loads(Path(launch.call_args.args[0]).read_bytes())
        self.assertEqual(request['profile'], expected.to_record())

    def test_invalid_linked_dsv_file_preserves_display_and_blocks_start(self):
        path = self.root / 'config.json'
        path.write_text(self.profile.to_json(), encoding='utf-8')
        window = self.window(dsv_config=path)
        path.write_text('{', encoding='utf-8')
        with patch('online_trader.runtime.startup_environment.launch_bootstrap') as launch:
            window.command_buttons['arm'].click()
            launch.assert_not_called()
        self.assertIn('E_DSV_PROFILE', window.home_feedback)
        self.assertFalse((self.store.directory / 'desktop-starts').exists())
        self.assertEqual(window.dashboard.symbol_grid.item(4, 1).text(), 'SHFE.ag2702')

    def test_quote_reference_updates_display_without_starting_strategy(self):
        window = self.window()
        code = window.dashboard.profile.to_record()['settings']['symbols'][0]['code']
        row = {'contract_id':code, 'last':'1000','margin':'1000','volume_multiple':'10',
               'commission':'3','price_tick':'1','main_contract':'DCE.p2703','main_expiry_date':'2027-03-31','ready':True}
        with (patch('online_trader.runtime.startup_metadata.read_startup_metadata',return_value=[row]) as read,
              patch('online_trader.runtime.startup_environment.launch_bootstrap') as launch):
            window.dashboard.quote_reference_button.click()
            worker = window.dashboard.quote_worker
            self.assertIsNotNone(worker)
            self.assertTrue(worker.wait(5_000))
            self.app.processEvents()
        self.assertEqual(read.call_args.kwargs,{'quote_preview':True})
        launch.assert_not_called()
        self.assertIsNone(window.view)
        self.assertEqual(window.dashboard.symbol_grid.item(0,2).text(),'DCE.p2703')
        self.assertIn('10.00%',window.dashboard.instrument_rules.grid.item(0,2).text())

    def test_manual_catalog_loads_and_filters_without_starting_a_session(self):
        window = self.window()
        dashboard = window.dashboard
        with (patch('online_trader.runtime.startup_metadata.read_contract_catalog',
                    return_value=['DCE.p2705','DCE.p2709','GFEX.ps2705']) as read,
              patch('online_trader.runtime.startup_environment.launch_bootstrap') as launch):
            dashboard.manual_catalog_button.click()
            self.assertTrue(dashboard.quote_worker.wait(5_000))
            self.app.processEvents()
        read.assert_called_once_with(self.store,self.account_id)
        launch.assert_not_called()
        self.assertIn('已加载 3 个',dashboard.manual_catalog_status.text())
        dashboard.manual_code.lineEdit().textEdited.emit('棕榈05')
        self.assertEqual([dashboard.manual_code.itemText(i) for i in range(dashboard.manual_code.count())],['DCE.p2705'])
        dashboard.manual_code.lineEdit().textEdited.emit('不存在')
        self.assertIn('未找到',dashboard.manual_catalog_status.text())
        self.assertFalse(dashboard.submit_button.isEnabled())

    def close_window(self, window):
        starter = getattr(window, '_strategy_starter', None)
        if starter is not None:
            self.idle(starter)
            starter.status_timer.stop()
        window._closing = True
        dispose_widget(window)
        self.app.processEvents()

    def assert_no_start_dialog(self):
        self.assertEqual([widget for widget in self.app.topLevelWidgets()
                          if isinstance(widget, QDialog) and widget.isVisible()], [])

    def test_constructing_starter_has_no_window_credentials_files_or_processes(self):
        before = set(self.app.topLevelWidgets())
        with patch.object(self.store.vault, 'get') as credentials, patch('subprocess.Popen') as spawn:
            starter = self.starter()
            self.assertIsInstance(starter, QObject)
            self.assertNotIsInstance(starter, QWidget)
            self.assertEqual(set(self.app.topLevelWidgets()), before)
            self.assertFalse(starter.status_timer.isActive())
            self.assertFalse(starter.is_preparing())
            self.assertFalse(starter._launched)
            credentials.assert_not_called()
            spawn.assert_not_called()
        self.assertFalse((self.store.directory / 'desktop-starts').exists())

    def test_direct_start_freezes_profile_and_ignores_repeats_without_reading_credentials(self):
        starter = self.starter()
        messages = []
        starter.status_changed.connect(messages.append)
        with (patch('online_trader.runtime.startup_environment.launch_bootstrap', return_value={'pid': 123}) as launch,
              patch.object(self.store.vault, 'get') as credentials):
            starter.start()
            starter.start()
            self.idle(starter)
            starter.start()
            launch.assert_called_once()
            credentials.assert_not_called()
        request = strict_loads(Path(launch.call_args.args[0]).read_bytes())
        self.assertEqual(request['profile'], self.profile.to_record())
        self.assertEqual(request['account_id'], self.account_id)
        self.assertTrue(request['arm'])
        for secret in ('synthetic-secret', 'synthetic-market-secret', 'offline-sn-start', 'offline-market'):
            self.assertNotIn(secret, str(request))
        self.assertEqual(starter.run_directory, Path(launch.call_args.args[1]))
        self.assertTrue(starter._launched)
        self.assertEqual(messages[-1], starter.status_text)
        self.assert_no_start_dialog()

    def test_one_main_button_click_uses_latest_parameters_and_starts_without_dialog(self):
        window = self.window()
        window.tabs.setCurrentWidget(window.dashboard.home_tab)
        self.assertFalse(window.dashboard.template_picker.isVisible())
        window.tabs.setCurrentWidget(window.dashboard.parameter_tab)
        window.dashboard.parameter_sections.setCurrentIndex(1)
        self.assertTrue(window.dashboard.template_picker.isVisible())
        changed = self.profile.to_settings()
        changed['trade']['max_volume'] = 7
        latest = DsvProfile.from_settings(changed)
        # The reusable starter was created before the user changed the parameters.
        window._strategy_starter = self.starter(window)
        with (patch.object(window, 'current_start_profile', return_value=latest),
              patch('online_trader.runtime.startup_environment.launch_bootstrap', return_value={'pid': 123}) as launch,
              patch.object(QMessageBox, 'question') as confirmation):
            window.command_buttons['arm'].click()
            starter = window._strategy_starter
            self.idle(starter)
            launch.assert_called_once()
            confirmation.assert_not_called()
        request = strict_loads(Path(launch.call_args.args[0]).read_bytes())
        self.assertEqual(request['profile'], latest.to_record())
        self.assertEqual(starter.profile, latest)
        self.assertFalse(window.command_buttons['arm'].isEnabled())
        self.assertEqual(window.home_feedback, starter.status_text)
        self.assert_no_start_dialog()

    def test_failed_start_shows_main_feedback_and_retry_uses_changed_parameters(self):
        window = self.window()
        with patch('online_trader.runtime.startup_environment.launch_bootstrap', side_effect=TraderError('E_DEPENDENCY', 'synthetic')):
            window.command_buttons['arm'].click()
            starter = window._strategy_starter
            self.idle(starter)
        self.assertIn('组件不可用', starter.status_text)
        self.assertEqual(window.home_feedback, starter.status_text)
        self.assertIn('组件不可用', window.dashboard.home_event.text())
        self.assertTrue(window.command_buttons['arm'].isEnabled())
        self.assertFalse(starter._launched)
        changed = self.profile.to_settings()
        changed['trade']['max_volume'] = 9
        latest = DsvProfile.from_settings(changed)
        with (patch.object(window, 'current_start_profile', return_value=latest),
              patch('online_trader.runtime.startup_environment.launch_bootstrap', return_value={'pid': 123}) as launch):
            window.command_buttons['arm'].click()
            self.idle(starter)
            launch.assert_called_once()
        request = strict_loads(Path(launch.call_args.args[0]).read_bytes())
        self.assertEqual(request['profile'], latest.to_record())
        self.assertTrue(starter._launched)
        self.assert_no_start_dialog()

    def test_catalogue_account_change_refuses_old_starter_before_launch(self):
        starter = self.starter()
        state = self.store.read()
        other = next(row for row in state['accounts'] if row['account_id'] != self.account_id)
        self.store.change('select', expected_revision=state['revision'], account_id=other['account_id'])
        with patch('online_trader.runtime.startup_environment.launch_bootstrap') as launch:
            starter.start()
            self.idle(starter)
            launch.assert_not_called()
        self.assertIn('已切换账号', starter.status_text)
        self.assertFalse(starter._launched)
        self.assertFalse((self.store.directory / 'desktop-starts').exists())

    def test_parent_account_change_refuses_old_starter_before_launch(self):
        window = self.window()
        starter = self.starter(window)
        window.selected_account = next(row for row in self.store.read()['accounts'] if row['account_id'] != self.account_id)
        with patch('online_trader.runtime.startup_environment.launch_bootstrap') as launch:
            starter.start()
            self.idle(starter)
            launch.assert_not_called()
        self.assertIn('已切换账号', starter.status_text)
        self.assertEqual(window.home_feedback, starter.status_text)
        self.assertFalse((self.store.directory / 'desktop-starts').exists())

    def test_child_exit_is_reported_on_main_window_and_allows_retry(self):
        window = self.window()
        starter = self.starter(window)
        window._strategy_starter = starter
        starter.run_directory = self.root / 'mocked-run'
        starter.run_directory.mkdir()
        starter._process_id = 123
        starter._launched = True
        with patch('online_trader.runtime.startup_environment.process_running', return_value=False):
            starter.update_launch_status()
        self.assertIn('启动进程已退出', starter.status_text)
        self.assertEqual(window.home_feedback, starter.status_text)
        self.assertFalse(starter._launched)
        self.assertTrue(window.command_buttons['arm'].isEnabled())

    def test_failed_signal_does_not_allow_account_switch_or_attach_before_thread_finishes(self):
        from online_trader.desktop.startup import _StartWorker

        window = self.window()
        release = Event()

        def fail_then_wait(worker):
            worker.failed.emit({'code': 'E_DEPENDENCY'})
            release.wait(5)

        with patch.object(_StartWorker, 'run', fail_then_wait):
            window.command_buttons['arm'].click()
            starter = window._strategy_starter
            try:
                deadline = time.monotonic() + 3
                while 'E_DEPENDENCY' not in starter.status_text and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(5)
                self.assertIn('E_DEPENDENCY', starter.status_text)
                self.assertFalse(window._startup_in_progress)
                self.assertIsNone(window._worker)
                self.assertTrue(starter.is_preparing())
                original_account = window.selected_account
                original_pipe = window.pipe_name
                state = self.store.read()
                other = next(row for row in state['accounts'] if row['account_id'] != self.account_id)
                picker = window.dashboard.account_picker
                picker.setCurrentIndex(picker.findData(other['account_id']))

                with (patch.object(self.store, 'change', wraps=self.store.change) as change,
                      patch.object(starter, 'deleteLater') as delete,
                      patch.object(window, 'refresh') as refresh):
                    window.select_account()
                    window.attach_selected()
                    change.assert_not_called()
                    delete.assert_not_called()
                    refresh.assert_not_called()
                    self.assertEqual(self.store.read()['selected_id'], self.account_id)
                    self.assertIs(window.selected_account, original_account)
                    self.assertEqual(window.pipe_name, original_pipe)
                    self.assertIs(window._strategy_starter, starter)
                    self.assertTrue(starter.is_preparing())
            finally:
                release.set()
                self.idle(starter)
        self.assertFalse(starter.is_preparing())

    def test_account_dialog_switches_the_current_workbench_and_keeps_old_owner_barrier(self):
        from online_trader.desktop.accounts import AccountDialog
        from online_trader.runtime.instance import AccountLock

        window = self.window()
        state = self.store.read()
        old = self.store.get(self.account_id)
        new = next(row for row in state['accounts'] if row['account_id'] != self.account_id)

        def choose(dialog):
            dialog.table.selectRow(next(index for index, row in enumerate(state['accounts'])
                                        if row['account_id'] == new['account_id']))
            with AccountLock(old['account_key']):
                dialog.select()
            self.assertIn('E_INSTANCE_EXISTS', dialog.notice.text())
            self.assertEqual(self.store.read()['selected_id'], self.account_id)
            self.assertEqual(window.selected_account, old)
            dialog.table.cellDoubleClicked.emit(dialog.table.currentRow(), 0)
            self.assertEqual(window.selected_account, new)
            self.assertEqual(window.pipe_name, self.store.pipe_name(new))
            self.assertIsNone(window.view)
            dialog.reject()

        with (patch.object(AccountDialog, 'exec', choose),
              patch.object(window, 'refresh') as refresh):
            window.accounts_button.click()
            refresh.assert_called_once_with()
        self.assertEqual(self.store.read()['selected_id'], new['account_id'])
        self.assertEqual(window.dashboard.account_picker.currentData(), new['account_id'])


if __name__ == '__main__':
    unittest.main()
