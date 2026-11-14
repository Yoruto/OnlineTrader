import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.qt_lifecycle import dispose_widget

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.contracts.validation import validate_message
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.desktop.workbench_layout import suggested_lots
from online_trader.kernel.workbench import workbench_view
from online_trader.transport.status_client import StatusClient
from tests import test_online_dsv, test_workbench


def comparison_fixture(run, view):
    directory = run/'comparison-example'
    directory.mkdir()
    (directory/'manifest.json').write_bytes(canonical_bytes({
        'schema_version':'DSV_COMPARISON/1', 'events_file':'events.jsonl',
        'account_key':view['account_key'], 'kernel_boot_id':view['kernel_boot_id'],
    }))
    return directory


class DsvWorkbenchParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_empty_bound_strategy_log_keeps_chinese_filter_feedback(self):
        window = WorkbenchWindow(None)
        window.timer.stop()
        self.addCleanup(dispose_widget, window)
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run/'strategy-log.jsonl').touch()
            view = test_workbench.view()
            comparison_fixture(run, view)
            view['configuration'].update(run_directory=str(run), scoped_contracts=['DCE.p2701'])
            view['markets'] = [{'contract_id':'DCE.p2701','fresh':True,'trading_day':'2026-11-12',
                                'entry_reasons':['GEOMETRY_BLOCK']}]
            window.view = view
            window.render_view()
            window.dashboard.show_details(window.dashboard.log_tab)
            window.dashboard.log_filter.setCurrentText('工作台')
            self.assertIn('几何条件不满足', window.dashboard.log_text.toPlainText())

    def test_committed_80_percent_stop_is_separate_from_geometry_and_position(self):
        case = test_online_dsv.OnlineDsvTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.entered(trade={'protect_80_enabled':True,'protect_stop_day':True,'half_close_enabled':False})
        before = workbench_view(case.kernel)
        self.assertIsNone(before['markets'][0]['day_stop_reason'])
        self.assertFalse(before['cycles'][0]['exit_pending'])
        self.assertEqual(before['cycles'][0]['entry_price'],'1055')
        self.assertEqual([row['trading_day'] for row in before['equity_history']],['2026-11-10'])

        case.quote(price=1096,high=1096)
        held = workbench_view(case.kernel)
        self.assertIn('GEOMETRY_BLOCK',held['markets'][0]['entry_reasons'])
        self.assertEqual(held['markets'][0]['day_stop_reason'],'浮盈曾达止盈80%')
        self.assertFalse(held['cycles'][0]['exit_pending'])

        case.quote(price=1078,high=1096)
        pending = workbench_view(case.kernel)
        self.assertEqual(pending['markets'][0]['day_stop_reason'],'浮盈曾达止盈80%')
        self.assertTrue(pending['cycles'][0]['exit_pending'])

    def test_three_dsv_status_boxes_and_cabinet_account_display(self):
        window = WorkbenchWindow(None)
        window.timer.stop()
        self.addCleanup(dispose_widget, window)
        view = test_workbench.view()
        codes = ['DCE.p2701','DCE.l2701','SHFE.bu2701','CZCE.OI701','SHFE.ag2702']
        view['configuration']['scoped_contracts'] = codes
        view['entry_authority'] = 'ARMED'
        view['markets'] = [{'contract_id':code,'fresh':True,'trading_day':'2026-11-12',
                            'entry_reasons':['GEOMETRY_BLOCK']} for code in codes]
        view['markets'][1]['day_stop_reason'] = '净亏达40点'
        view['markets'][4]['day_stop_reason'] = '浮盈曾达止盈80%'
        view['cycles'] = [
            {'contract_id':'DCE.p2701','direction':'LONG','remaining_quantity':1,
             'lifecycle':'HOLDING','position_cycle_id':'held','exit_pending':False},
            {'contract_id':'SHFE.bu2701','direction':'SHORT','remaining_quantity':1,
             'lifecycle':'EXIT_PENDING','position_cycle_id':'exiting','exit_pending':True},
        ]
        view['account_metrics'] = {'trading_day':'2026-11-12','balance':'998440.85',
                                   'pre_balance':'1000818.31','close_profit':'-2020',
                                   'commission':'357.46','position_profit':'0','margin':'0',
                                   'risk_ratio':'0.012','opening_orders':6}
        window.dashboard.render(view)
        waiting,stopped,pending = [label.text() for label in window.dashboard.status_labels]
        self.assertEqual(waiting.count('几何条件不满足，暂不建仓'),3)
        self.assertIn('DCE.l2701',stopped)
        self.assertIn('浮盈曾达止盈80%',stopped)
        self.assertNotIn('DCE.p2701',stopped)
        self.assertIn('SHFE.bu2701',pending)
        self.assertNotIn('DCE.p2701',pending)
        fields = window.dashboard.fund_metrics
        self.assertIn('-2,377.46',fields['today_pnl'][0].text())
        self.assertIn('1.20%',fields['risk_ratio'][0].text())
        self.assertIn('0.00%',fields['margin_ratio'][0].text())

    def test_strategy_log_filters_export_and_clear_preserve_dsv_record_order(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            window = WorkbenchWindow(None)
            window.timer.stop()
            try:
                window.selected_account = {'account_id': 'this-account', 'account_key': 'account-test',
                                           'environment': 'ONLINE_SIM', 'masked_login': '****82'}
                window._strategy_starter = SimpleNamespace(account_id='this-account',run_directory=run,
                                                         is_preparing=lambda: False, status_timer=Mock())
                dashboard = window.dashboard
                strategy = run/'strategy-log.jsonl'
                started = '2026-11-12T05:30:00Z'
                (run/'executor.log').write_bytes(canonical_bytes({'phase':'DSV_OBSERVING',
                                                                  'observed_at_utc':started})+b'\n')
                view = test_workbench.view()
                comparison = comparison_fixture(run, view)
                (comparison/'events.jsonl').write_bytes(canonical_bytes({'kind':'DSV_LOG','data':{
                    'source_time_utc':started,'contract_id':'DCE.p2701','kind':'ENTRY_FILTER','reason':'GEOMETRY'}})+b'\n')
                content = [('提示','棕榈油(DCE.p2701) 振幅超出上限'),('提示','棕榈油(DCE.p2701) 当日高低及触发价'),
                           ('跳过','棕榈油(DCE.p2701) 当日行情已跑过多头激活位')]
                kinds = ['GEOMETRY_BLOCK','TRIGGER_HINT','LONG:NO_CHASE_AFTER_ACT']
                records = b''.join(canonical_bytes({'observed_at_utc':started,'tag':tag,'message':message,
                    'kind':kind,'trading_day':'2026-11-12','contract_id':'DCE.p2701'})+b'\n'
                    for (tag,message),kind in zip(content,kinds))
                strategy.write_bytes(records)
                view['entry_authority'] = 'ARMED'
                view['configuration']['scoped_contracts'] = ['DCE.p2701']
                view['markets'] = [{'contract_id':'DCE.p2701','fresh':True,'last':'8932','source_time_utc':started,'trading_day':'2026-11-12',
                                    'entry_reasons':['GEOMETRY_BLOCK','LONG:NO_CHASE_AFTER_ACT','RECONCILIATION_REQUIRED']}]
                window.view = view
                window.render_view()
                detail = window.tables['markets'].item(0,window.tables['markets'].columnCount()-1).text()
                self.assertIn('当日行情已跑过多头激活位',detail)
                self.assertIn('当日行情已跑过多头激活位',dashboard.status_labels[0].text())
                dashboard.add_activity('本地连接诊断')
                dashboard.show_details(dashboard.log_tab)
                dashboard.log_filter.setCurrentText('工作台')
                self.assertIn('本地连接诊断',dashboard.log_text.toPlainText())
                self.assertIn('柜台待核对',dashboard.log_text.toPlainText())
                self.assertNotIn('几何条件不满足',dashboard.log_text.toPlainText())
                dashboard.log_filter.setCurrentText('DSV 对照')
                self.assertEqual(dashboard.visible_log_lines,['[13:30:00] [DSV] DCE.p2701 · ENTRY_FILTER GEOMETRY'])
                dashboard.log_filter.setCurrentText('执行器')
                expected = ['[13:30:00] [执行器] 策略正在监控所选合约',
                            *(f'[13:30:00] [{tag}] {message}' for tag,message in content)]
                self.assertEqual(dashboard.visible_log_lines,expected)
                exported = run/'export.txt'
                with patch('online_trader.desktop.workbench_layout.QFileDialog.getSaveFileName',return_value=(str(exported),'')):
                    dashboard.export_log()
                self.assertEqual(exported.read_text(encoding='utf-8').splitlines(),expected)
                dashboard.log_event_filter.setCurrentText('错误')
                dashboard.log_filter.setCurrentText('工作台')
                dashboard.add_activity('SHFE.ag2702 状态刷新失败',category='error')
                dashboard.add_activity('DCE.p2701 状态刷新失败',category='error')
                dashboard.log_search.setText('shfe.AG2702')
                self.assertEqual(len(dashboard.visible_log_lines),1)
                self.assertIn('SHFE.ag2702',dashboard.visible_log_lines[0])
                dashboard.log_search.clear()
                dashboard.log_event_filter.setCurrentText('全部事件')
                dashboard.log_filter.setCurrentText('执行器')
                with patch('online_trader.desktop.workbench_layout.QMessageBox.question',return_value=QMessageBox.Yes):
                    dashboard.clear_log()
                self.assertEqual(dashboard.visible_log_lines,[])
                self.assertEqual(strategy.read_bytes(),records)
                with strategy.open('ab') as stream:
                    stream.write(canonical_bytes({'observed_at_utc':'2026-11-12T06:50:00Z','tag':'执行',
                                                  'message':'棕榈油(DCE.p2701) 距收盘不足 10 分钟，不再开新仓'})+b'\n')
                dashboard.update_logs()
                self.assertEqual(dashboard.visible_log_lines,['[14:50:00] [执行] 棕榈油(DCE.p2701) 距收盘不足 10 分钟，不再开新仓'])
                window.selected_account = {'account_id': 'different-account', 'account_key': 'different-account-key',
                                           'environment': 'ONLINE_SIM', 'masked_login': '****16'}
                dashboard.update_logs()
                self.assertEqual(dashboard.visible_log_lines,[])
            finally:
                dispose_widget(window)

    def test_dsv_auto_log_tag_appears_in_both_open_and_close_filters(self):
        window = WorkbenchWindow(None)
        self.addCleanup(dispose_widget, window)
        window.timer.stop()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            window.selected_account = {'account_id': 'this-account', 'account_key': 'account-test',
                                           'environment': 'ONLINE_SIM', 'masked_login': '****82'}
            window._session_dialog = SimpleNamespace(account_id='this-account',run_directory=run,
                                                     clear_scheduled_start=Mock())
            window.view = test_workbench.view()
            comparison = comparison_fixture(run, window.view)
            rows = [{'kind':'DSV_LOG','data':{'kind':kind,'operator_tag':tag,'contract_id':code,
                                            'source_time_utc':'2026-11-12T05:30:00Z'}}
                    for kind,tag,code in [('EXIT_DECISION','auto','DCE.p2701'),('EXIT_DECISION','manual','SHFE.bu2701'),
                                          ('EXIT_DECISION','half','DCE.l2701'),('EXIT_DECISION','close_win','SHFE.ag2702')]]
            rows.append({'kind':'DSV_LOG','data':{'kind':'EXIT_DECISION', 'contract_id':'CZCE.OI701',
                         'source_time_utc':'2026-11-12T05:30:00Z', 'message':'[平仓] 单笔:-5'}})
            automatic_close = '[13:30:01] [DSV] CZCE.OI701 · [自动] [平仓] 自动退出'
            rows.append({'kind':'DSV_LOG','data':{'kind':'EXIT_DECISION', 'contract_id':'CZCE.OI701',
                         'source_time_utc':'2026-11-12T05:30:01Z', 'message':'[自动] [平仓] 自动退出',
                         'operator_tag':'auto'}})
            (comparison/'events.jsonl').write_bytes(b''.join(canonical_bytes(row)+b'\n' for row in rows))
            dashboard = window.dashboard
            dashboard.show_details(dashboard.log_tab)
            dashboard.log_filter.setCurrentText('DSV 对照')
            for category in ('开仓','平仓','手动'):
                dashboard.log_event_filter.setCurrentText(category)
                expected = 'SHFE.bu2701' if category=='手动' else 'DCE.p2701'
                expected_rows = [f'[13:30:00] [DSV] {expected} · EXIT_DECISION']
                if category == '平仓':
                    expected_rows.append(automatic_close)
                self.assertEqual(dashboard.visible_log_lines, expected_rows)
            dashboard.log_filter.setCurrentText('全部')
            dashboard.log_event_filter.setCurrentText('平仓')
            self.assertEqual(dashboard.visible_log_lines,
                             ['[13:30:00] [DSV] DCE.p2701 · EXIT_DECISION', automatic_close])

    def test_workbench_log_rejects_another_owner_in_the_published_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            local = QSettings(str(run/'ui.ini'), QSettings.IniFormat)
            with patch('online_trader.desktop.workbench_layout.QSettings', return_value=local):
                window = WorkbenchWindow(None)
            self.addCleanup(dispose_widget, window)
            window.timer.stop()
            view = test_workbench.view()
            comparison_fixture(run, view | {'kernel_boot_id':'previous-boot'})
            record = {'observed_at_utc':'2026-11-12T05:30:00Z', 'tag':'策略', 'message':'other owner log'}
            (run/'strategy-log.jsonl').write_bytes(canonical_bytes(record)+b'\n')
            view['configuration']['run_directory'] = str(run)
            window.view = view
            dashboard = window.dashboard
            dashboard.show_details(dashboard.log_tab)
            dashboard.log_filter.setCurrentText('执行器')
            self.assertEqual(dashboard.visible_log_lines, [])
            self.assertIsNone(dashboard.latest_run())

    def test_workbench_log_revalidates_owner_when_archive_changes_during_read(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            local = QSettings(str(run/'ui.ini'), QSettings.IniFormat)
            with patch('online_trader.desktop.workbench_layout.QSettings', return_value=local):
                window = WorkbenchWindow(None)
            window.timer.stop()
            self.addCleanup(dispose_widget, window)
            view = test_workbench.view()
            comparison_fixture(run, view)
            strategy = run/'strategy-log.jsonl'
            record = {'observed_at_utc':'2026-11-12T05:30:00Z', 'tag':'策略', 'message':'current owner log'}
            strategy.write_bytes(canonical_bytes(record)+b'\n')
            view['configuration']['run_directory'] = str(run)
            window.view = view
            dashboard = window.dashboard
            dashboard.show_details(dashboard.log_tab)
            dashboard.log_filter.setCurrentText('执行器')
            self.assertIn('current owner log', dashboard.log_text.toPlainText())

            log_reader = dashboard.log_tail(strategy)
            read = log_reader.read
            def add_foreign_archive(path):
                (run/'comparison-other').mkdir(exist_ok=True)
                with strategy.open('ab') as stream:
                    stream.write(canonical_bytes(record | {'message':'foreign owner log'})+b'\n')
                return read(path)
            with patch.object(log_reader, 'read', side_effect=add_foreign_archive):
                dashboard.update_logs()
            self.assertEqual(dashboard.visible_log_lines, [])
            self.assertIsNone(dashboard.latest_run())

    def test_operation_log_recovery_and_rebuilt_rows_are_bound_to_current_owner(self):
        from online_trader.runtime.strategy_log import publish_log_recovery

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            run = parent / 'run'
            run.mkdir()
            local = QSettings(str(parent/'ui.ini'), QSettings.IniFormat)
            with patch('online_trader.desktop.workbench_layout.QSettings', return_value=local):
                window = WorkbenchWindow(None)
            window.timer.stop()
            self.addCleanup(dispose_widget, window)
            view = test_workbench.view()
            identity = {key: view[key] for key in ('account_key', 'kernel_boot_id')}
            comparison_fixture(run, view)
            recovery = {'schema_version': 'DSV_OPERATION_LOG_RECOVERY/1', **identity,
                        'run_directory': str(run), 'status': 'DEGRADED',
                        'observed_at_utc': '2026-11-12T05:30:00Z',
                        'first_event_seq': '12', 'through_event_seq': '16'}
            path = parent / 'run.operation-log-recovery.json'
            publish_log_recovery(path, recovery)
            view['configuration']['run_directory'] = str(run)
            window.view = view
            dashboard = window.dashboard
            dashboard.show_details(dashboard.log_tab)
            dashboard.log_filter.setCurrentText('执行器')
            self.assertIn('操作日志降级', dashboard.log_text.toPlainText())

            publish_log_recovery(path, recovery | {'status': 'RECOVERED'})
            record = identity | {'observed_at_utc': '2026-11-12T05:30:00Z', 'tag': '执行', 'message': '已补回报单'}
            other = record | {'account_key': 'other-owner', 'message': '其他账户的报单'}
            (run/'strategy-log.jsonl').write_bytes(canonical_bytes(record)+b'\n'+canonical_bytes(other)+b'\n')
            dashboard.update_logs()
            self.assertIn('已从账本补回操作日志', dashboard.log_text.toPlainText())
            self.assertIn('已补回报单', dashboard.log_text.toPlainText())
            self.assertNotIn('其他账户的报单', dashboard.log_text.toPlainText())

            publish_log_recovery(path, recovery | {'kernel_boot_id': 'foreign-boot'})
            dashboard.update_logs()
            self.assertNotIn('操作日志降级', dashboard.log_text.toPlainText())
            self.assertNotIn('已从账本补回操作日志', dashboard.log_text.toPlainText())

    def test_suggested_lots_is_display_only_with_unknown_inputs(self):
        window = WorkbenchWindow(None)
        self.addCleanup(dispose_widget, window)
        window.timer.stop()
        dashboard = window.dashboard
        settings = dashboard.profile.to_record()['settings']
        trade = settings['trade'] | {'risk_per_trade_ratio':'0.002','max_volume':30,'risk_reference_equity':'1000000',
                                    'max_risk_ratio':'0.85','total_margin_cap':'0.7','aggressive_mode':False}
        symbol = settings['symbols'][0] | {'stop_offset':'16'}
        quote = {'fresh':True,'volume_multiple':'10','last':'9000','margin':'10000'}
        self.assertEqual(suggested_lots(symbol,trade,5,{},quote,None)[0],12)
        self.assertEqual(suggested_lots(symbol,trade | {'aggressive_mode':True},5,{},quote,None)[0],14)
        self.assertIsNone(suggested_lots(symbol,trade,5,{},quote | {'fresh':False},None)[0])
        grid = dashboard.symbol_grid
        view = test_workbench.view()
        view['account_metrics'] = {'balance':'1000000','trading_day':'2026-11-10'}
        view['markets'] = [quote | {'contract_id':symbol['code']}]
        window.view = view
        window.render_view()
        self.assertEqual(grid.horizontalHeaderItem(11).text(),'建议手数')
        self.assertEqual(grid.item(0,11).text(),'12 手')
        self.assertEqual(dashboard.profile.to_record()['settings'],settings)

    def test_independent_workbench_displays_owner_bound_cabinet_day_sessions(self):
        case = test_online_dsv.OnlineDsvTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.start()
        window = WorkbenchWindow(None)
        self.addCleanup(dispose_widget, window)
        window.timer.stop()
        window.view = workbench_view(case.kernel)
        window.render_view()
        dashboard = window.dashboard
        self.assertIn('执行器已绑定 · 柜台交易日 2026-11-10',dashboard.calendar_status.text())
        self.assertEqual(dashboard.session_grid.rowCount(),1)
        self.assertEqual(dashboard.session_grid.item(0,2).text(),'09:00–15:00')

    def test_unsolicited_terminal_close_only_alerts_when_unreserved(self):
        case = test_online_dsv.OnlineDsvTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.entered(trade={'half_close_enabled':False})
        case.quote(1038)
        self.assertEqual(case.api.inserted[-1]['offset'],'CLOSE')
        code = 'EXIT_REDUCTION_TERMINAL_UNFILLED'
        self.assertNotIn(code,{row['reason_code'] for row in workbench_view(case.kernel)['blocks']})
        case.api.terminal(case.api.inserted[-1]['order_id'])
        case.session.step()
        view = workbench_view(case.kernel)
        self.assertIn(code,{row['reason_code'] for row in view['blocks']})
        self.assertEqual(view['cycles'][0]['remaining_quantity'],5)
        self.assertEqual(len(case.api.inserted),2)
        window = WorkbenchWindow(None)
        self.addCleanup(dispose_widget, window)
        window.timer.stop()
        window.view = view
        window.render_view()
        blocks = window.tables['blocks']
        self.assertTrue(any('剩余目标待核对' in blocks.item(i,0).text() for i in range(blocks.rowCount())))
        self.assertNotIn(case.code,window.dashboard.status_labels[2].text())

    def test_workbench_labels_retained_quote_stale_and_keeps_price(self):
        window = WorkbenchWindow(None)
        window.timer.stop()
        self.addCleanup(dispose_widget, window)
        view = test_workbench.view()
        view['configuration']['scoped_contracts'] = ['DCE.p2701']
        view['markets'] = [{'contract_id':'DCE.p2701','fresh':False,'last':'8932',
                            'source_time_utc':'2026-11-12T05:30:00Z','entry_reasons':[]}]
        window.dashboard.render(view)
        self.assertIn('行情已滞后',window.dashboard.status_labels[0].text())
        self.assertEqual(window.dashboard.instrument_grid.item(0,3).text(),'8932')
        self.assertEqual(window.dashboard.instrument_grid.item(0,4).text(),'行情已滞后')
        self.assertIn('8932',window.dashboard.manual_quote.text())

    def test_manual_controls_use_selected_id_and_quick_open_opponent(self):
        window = WorkbenchWindow(None)
        window.timer.stop()
        self.addCleanup(dispose_widget, window)
        window.close_manual_cycle = Mock()
        window.cancel_manual_children = Mock()
        window.submit_manual = Mock()
        view = test_workbench.view()
        view['configuration'].update(scoped_contracts=['DCE.p2701'],manual_workbench=True)
        view['account_metrics'] = {'trading_day':'2026-11-12'}
        view['orders'] = [
            {'child_order_id':'prior-day','contract_id':'DCE.p2701','effect':'OPEN',
             'native_trading_day':'2026-11-11','potential_remaining':1},
            {'child_order_id':'current','contract_id':'DCE.p2701','effect':'OPEN',
             'native_trading_day':'2026-11-12','potential_remaining':1},
        ]
        view['cycles'] = [{'contract_id':'DCE.p2701','direction':'LONG','remaining_quantity':3,
                           'lifecycle':'HOLDING','position_cycle_id':'cycle'}]
        window.view = view
        dashboard = window.dashboard
        dashboard.render(view)
        dashboard.update_manual_controls(True)
        dashboard.manual_tables['orders'].selectRow(0)
        dashboard.manual_tables['cycles'].selectRow(0)
        dashboard.manual_cancel.click()
        dashboard.manual_close_buttons[1].click()
        dashboard.manual_close_buttons[None].click()
        dashboard.quick_sizes[1].click()
        window.cancel_manual_children.assert_called_once_with(['current'])
        self.assertEqual([call.args for call in window.close_manual_cycle.call_args_list],[('cycle',1),('cycle',None)])
        self.assertFalse(dashboard.manual_close_buttons[5].isEnabled())
        window.submit_manual.assert_called_once_with('DCE.p2701','LONG',2,'OPPONENT')

    def test_toolbar_template_updates_preview_without_changing_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            local = QSettings(str(Path(directory)/'ui.ini'),QSettings.IniFormat)
            with patch('online_trader.desktop.workbench_layout.QSettings',return_value=local):
                window = WorkbenchWindow(None)
            try:
                window.timer.stop()
                dashboard = window.dashboard
                dialog = dashboard.parameter_dialog()
                binding = dashboard.profile.content_hash
                self.assertTrue(dialog.set_template_directory(directory))
                preview = dialog.profile.to_settings()
                preview['trade']['max_volume'] = 17
                self.assertTrue(dialog.apply_edited_preview(DsvProfile.from_settings(preview),expected_hash=binding))
                self.assertTrue(dialog.save_template('DSV样本'))
                dialog.reset_to_baseline()
                self.assertEqual(dashboard.template_picker.currentText(),'DSV样本')
                dashboard.apply_selected_template()
                self.assertEqual(dashboard.parameter_fields['max_volume'].text(),'17')
                self.assertEqual(dashboard.profile.content_hash,binding)
                self.assertIn('预览',dashboard.parameter_state.text())
            finally:
                dispose_widget(window)

    def test_equity_date_filter_and_export_use_recorded_cabinet_values(self):
        window = WorkbenchWindow(None)
        window.timer.stop()
        self.addCleanup(dispose_widget, window)
        view = test_workbench.view()
        view['equity_history'] = [
            {'trading_day':day,'balance':balance,'pre_balance':previous,
             'observed_at_utc':f'{day}T07:00:00Z'}
            for day,balance,previous in [('2026-11-10','1000','950'),('2026-11-11','1200','1000'),('2026-11-12','1300','1200')]
        ]
        dashboard = window.dashboard
        dashboard.render(view)
        dashboard.equity_from.setCurrentText('2026-11-11')
        dashboard.equity_to.setCurrentText('2026-11-12')
        dashboard.equity_mode.setCurrentText('收益曲线')
        self.assertEqual([item['balance'] for item in dashboard.equity.rows],['250','350'])
        window.dashboard.show_details(window.dashboard.monitor_tab)
        window.show()
        self.app.processEvents()
        with tempfile.TemporaryDirectory() as directory:
            png = Path(directory)/'curve.png'
            csv = Path(directory)/'curve.csv'
            with patch('online_trader.desktop.workbench_layout.QFileDialog.getSaveFileName',
                       side_effect=[(str(png),''),(str(csv),'')]):
                dashboard.export_equity_png()
                dashboard.export_equity_csv()
            self.assertTrue(png.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            content = csv.read_text(encoding='utf-8-sig')
            self.assertNotIn('2026-11-10',content)
            self.assertIn('2026-11-11,1200,200,250,0.00%',content)
            self.assertIn('2026-11-12,1300,100,350,8.33%',content)

    def test_native_minute_chart_drops_padding_and_replaces_ui_sampling(self):
        case = test_online_dsv.OnlineDsvTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.start()
        bars = [
            {'datetime':0,'open':100.0,'high':101.0,'low':99.0,'close':100.0},
            {'datetime':1762740000000000000,'open':100.0,'high':101.0,'low':99.0,'close':105.0},
            {'datetime':1762740060000000000,'open':100.0,'high':104.0,'low':99.0,'close':102.0},
        ]
        frame = SimpleNamespace(tail=lambda count:SimpleNamespace(to_dict=lambda mode:bars[-count:]))
        case.kernel.port.minute_chart_serials[case.code] = frame
        case.api.is_serial_ready = lambda serial:True
        result = workbench_view(case.kernel)
        self.assertEqual(result['minute_bars'][case.code],[[29379001,'100','104','99','102']])
        validate_message(StatusClient._message('WorkbenchSnapshot',{'payload':result,'content_hash':content_hash(result)}))
        window = WorkbenchWindow(None)
        window.timer.stop()
        self.addCleanup(dispose_widget, window)
        window.dashboard.render(result)
        window.dashboard.candle_contract.setCurrentText(case.code)
        self.assertEqual(window.dashboard.candle_source.text(),'原生 1 分钟')
