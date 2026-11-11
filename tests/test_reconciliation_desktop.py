"""Statement review never turns missing scope or stale input into fee consent."""

import copy
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from online_trader.desktop.reconciliation import ReconciliationWindow
from online_trader.desktop.reports import ReportsWindow
from online_trader.errors import TraderError


KEY='DEV:statement-ui'
DAY='2026-11-10'


def preview():
    return {'schema_version':'FILL_RECONCILIATION/1','account_key':KEY,'environment':'ONLINE_SIM',
        'trading_day':DAY,'source_account':KEY,'source_sha256':'a'*64,'statement_hash':'b'*64,
        'checkpoint_event_seq':'17','preview_hash':'c'*64,'blocking':False,
        'summary':{'fee_action_count':1,'total_fee_amount':'0'},
        'rows':[{'row_number':2,'status':'MATCHED_FEE_NEW','account_key':KEY,
            'environment':'ONLINE_SIM','trading_day':DAY,'trade_key':'statement-trade',
            'contract_id':'DCE.p2701','side':'BUY','effect':'OPEN','quantity':3,'price':'1055',
            'fee':'0','local_quantity':3,'local_price':'1055','local_fee':None,'local_fee_quality':'UNKNOWN',
            'local_trade_key':'statement-trade','issues':[]},
            {'row_number':3,'status':'DUPLICATE_EXACT','contract_id':'DCE.p2701',
             'local_trade_key':'statement-trade','fee':'0','issues':['DUPLICATE_SOURCE_ROW']}],
        'local_only':[{'trade_key':'local-missing','trading_day':DAY,'contract_id':'SHFE.rb2701',
            'direction':'SHORT','effect':'REDUCE','quantity':1,'price':'3200','fee':None,'fee_quality':'UNKNOWN'}],
        'actions':[{'trade_key':'statement-trade','amount':'0','quality':'CONFIRMED','revision':1,
            'evidence_id':'statement-fee-evidence','before_fee':None,'fill_binding_hash':'d'*64}]}


class ReconciliationDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):self.windows=[]

    def tearDown(self):
        for window in self.windows:
            if getattr(window,'_worker',None) is not None:self.wait_idle(window)
            window.close()
        self.app.processEvents()

    def window(self,*,previewer=None,registrar=None,exporter=None,pipe_name=None,filled=True):
        window=ReconciliationWindow('ledger.db',KEY,pipe_name=pipe_name,
            previewer=previewer or (lambda **_kwargs:preview()),registrar=registrar,exporter=exporter)
        self.windows.append(window)
        if filled:
            window.statement_edit.setText('statement.csv')
            window.environment.setCurrentIndex(window.environment.findData('ONLINE_SIM'))
            window.day_edit.setText(DAY)
        return window

    def wait_idle(self,window):
        deadline=time.monotonic()+3
        while window._worker is not None and time.monotonic()<deadline:
            self.app.processEvents();QTest.qWait(5)
        self.assertIsNone(window._worker)

    def loaded(self,**kwargs):
        window=self.window(**kwargs);window.preview();self.wait_idle(window);return window

    def test_missing_file_environment_or_explicit_day_does_not_start_a_preview(self):
        calls=[];window=self.window(previewer=lambda **_kwargs:calls.append(1),filled=False)
        window.preview();self.assertIsNone(window._worker);self.assertFalse(window.register_button.isEnabled())
        self.assertEqual(calls,[])
        window.statement_edit.setText('file.csv');window.environment.setCurrentIndex(1)
        for day in ('','20261110','2026-02-30','2026-11-10T01:00:00Z'):
            window.day_edit.setText(day);window.preview()
            self.assertIsNone(window._worker)
        self.assertEqual(calls,[])

    def test_preview_renders_duplicate_local_missing_unknown_and_confirmed_zero_separately(self):
        window=self.loaded()
        self.assertEqual(window.external_table.rowCount(),2)
        self.assertEqual(window.local_table.rowCount(),1)
        self.assertIn('精确匹配',window.external_table.item(0,1).text())
        self.assertIn('完全重复',window.external_table.item(1,1).text())
        self.assertEqual(window.external_table.item(0,12).text(),'0')
        self.assertEqual(window.external_table.item(0,13).text(),'未知')
        self.assertIn('1 条费用 / 0 CNY',window.summary_label.text())
        self.assertIn('a'*64,window.hash_label.text());self.assertIn(KEY,window.scope_label.text())
        self.assertTrue(window.register_button.isEnabled());self.assertFalse(window._stale)

    def test_live_comparison_can_be_viewed_and_exported_but_not_registered(self):
        value=preview();value['environment']='LIVE'
        calls=[];window=self.window(previewer=lambda **_kwargs:value,registrar=lambda **kwargs:calls.append(kwargs))
        window.environment.setCurrentIndex(window.environment.findData('LIVE'))
        window.preview();self.wait_idle(window)
        self.assertFalse(window._stale);self.assertIsNotNone(window._preview)
        self.assertFalse(window.register_button.isEnabled());self.assertTrue(window.xlsx_button.isEnabled())
        self.assertIn('实盘账本仅用于只读对照',window.mode_label.text())
        window.register_fees();self.assertEqual(calls,[])

    def test_each_source_or_scope_change_marks_old_preview_stale_and_disables_registration(self):
        for field in ('path_edit','statement_edit','account_edit','source_account_edit','day_edit','environment'):
            with self.subTest(field=field):
                window=self.loaded();before=window.external_table.item(0,1).text()
                if field=='environment':window.environment.setCurrentIndex(2)
                else:getattr(window,field).setText('changed')
                self.assertTrue(window._stale);self.assertFalse(window.register_button.isEnabled())
                self.assertEqual(window.external_table.item(0,1).text(),before)

    def test_read_failure_keeps_previous_rows_but_cannot_register_them(self):
        calls=[]
        def read(**_kwargs):
            calls.append(1)
            if len(calls)>1:raise TraderError('E_SCOPE','Wrong file')
            return preview()
        window=self.loaded(previewer=read);window.preview();self.wait_idle(window)
        self.assertEqual(window.external_table.rowCount(),2);self.assertTrue(window._stale)
        self.assertFalse(window.register_button.isEnabled());self.assertIn('E_SCOPE',window.status_label.text())

    def test_blocking_or_empty_action_preview_never_enables_registration(self):
        for changed in ({'blocking':True},{'actions':[]}):
            with self.subTest(changed=changed):
                window=self.loaded(previewer=lambda **_kwargs:preview()|changed)
                self.assertIsNotNone(window._preview);self.assertFalse(window.register_button.isEnabled())

    def test_unknown_status_and_invalid_fee_actions_do_not_become_registration_success(self):
        for change in ('status','fee'):
            material=preview()
            if change=='status':material['rows'][0]['status']='FUTURE_UNVERIFIED_STATUS'
            else:material['actions'][0]['amount']='NaN'
            window=self.loaded(previewer=lambda value=material,**_kwargs:copy.deepcopy(value))
            self.assertFalse(window.register_button.isEnabled())
            if change=='status':self.assertEqual(window.external_table.item(0,1).text(),'FUTURE_UNVERIFIED_STATUS')

    def test_scope_mismatched_or_malformed_previewer_result_is_not_published(self):
        for changes in ({'account_key':'other'},{'environment':'LIVE'},{'trading_day':'2026-11-11'},
                        {'preview_hash':'short'},{'blocking':'false'}):
            with self.subTest(changes=changes):
                window=self.loaded(previewer=lambda value=changes,**_kwargs:preview()|value)
                self.assertIsNone(window._preview);self.assertFalse(window.register_button.isEnabled())

    def test_cancel_confirmation_has_no_registration_or_kernel_operation(self):
        calls=[];window=self.loaded(registrar=lambda **kwargs:calls.append(kwargs))
        with patch('online_trader.desktop.reconciliation.QMessageBox.question',return_value=QMessageBox.No) as question:
            window.register_fees()
        self.assertEqual(calls,[]);self.assertIsNone(window._worker)
        text=question.call_args.args[2]
        self.assertIn('仅登记 1 条',text);self.assertIn('合计 0 CNY',text)
        self.assertIn('a'*64,text);self.assertIn('c'*64,text)

    def test_explicit_confirmation_registers_only_the_reviewed_hash_and_invalidates_it(self):
        calls=[]
        def register(**kwargs):calls.append(kwargs);return {'status':'REGISTERED','registered_count':1}
        window=self.loaded(registrar=register)
        with patch('online_trader.desktop.reconciliation.QMessageBox.question',return_value=QMessageBox.Yes):
            window.register_fees();self.wait_idle(window)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0],{'statement_path':Path('statement.csv'),'ledger_path':Path('ledger.db'),
            'account_key':KEY,'environment':'ONLINE_SIM','trading_day':DAY,'source_account':None,
            'expected_preview_hash':'c'*64})
        self.assertTrue(window._stale);self.assertFalse(window.register_button.isEnabled())
        self.assertIn('REGISTERED',window.status_label.text());self.assertIn('重新预览',window.status_label.text())
        with patch('online_trader.desktop.reconciliation.QMessageBox.question') as question:window.register_fees()
        question.assert_not_called();self.assertEqual(len(calls),1)

    def test_input_change_during_confirmation_prevents_registering_a_new_file_with_old_consent(self):
        calls=[];window=self.loaded(registrar=lambda **kwargs:calls.append(kwargs))
        def confirm(*_args):window.statement_edit.setText('replacement.csv');return QMessageBox.Yes
        with patch('online_trader.desktop.reconciliation.QMessageBox.question',side_effect=confirm):window.register_fees()
        self.assertEqual(calls,[]);self.assertIsNone(window._worker);self.assertTrue(window._stale)

    def test_registration_failure_keeps_old_preview_stale_and_does_not_retry(self):
        calls=[]
        def register(**kwargs):calls.append(kwargs);raise TraderError('E_LEDGER_IN_USE','Active writer')
        window=self.loaded(registrar=register)
        with patch('online_trader.desktop.reconciliation.QMessageBox.question',return_value=QMessageBox.Yes):
            window.register_fees();self.wait_idle(window)
        self.assertEqual(len(calls),1);self.assertIn('E_LEDGER_IN_USE',window.status_label.text())
        self.assertEqual(window.external_table.rowCount(),2);self.assertTrue(window._stale)
        self.assertFalse(window.register_button.isEnabled())

    def test_pipe_preview_is_bound_to_account_boot_and_watermark_and_always_read_only(self):
        calls=[]
        class Client:
            last_metadata={'account_key':KEY,'kernel_boot_id':'boot-ui','durable_seq':'17'}
            def __init__(self,pipe):calls.append(('connect',pipe))
            def snapshot(self):return {'records':[{'record_type':'ACCOUNT','record_id':KEY}]}
            def pause(self):raise AssertionError('No control command')
        def read(**kwargs):calls.append(kwargs);return preview()
        with patch('online_trader.desktop.reconciliation.StatusClient',Client):window=self.loaded(previewer=read,pipe_name='test-pipe')
        self.assertEqual(calls[1]['checkpoint_event_seq'],'17');self.assertEqual(calls[1]['expected_kernel_boot_id'],'boot-ui')
        self.assertIn('内核已接受',window.status_label.text());self.assertFalse(window.register_button.isEnabled())
        with patch('online_trader.desktop.reconciliation.QMessageBox.question') as question:window.register_fees()
        question.assert_not_called()

    def test_pipe_missing_boot_wrong_account_or_wrong_watermark_fails_closed(self):
        for problem in ('boot','account','watermark'):
            calls=[]
            class Client:
                last_metadata={'account_key':KEY,'kernel_boot_id':'boot-ui','durable_seq':'17'}
                def __init__(self,_pipe):pass
                def snapshot(self):return {'records':[{'record_type':'ACCOUNT','record_id':'other' if problem=='account' else KEY}]}
            if problem=='boot':Client.last_metadata.pop('kernel_boot_id')
            def read(**kwargs):calls.append(kwargs);return preview()|{'checkpoint_event_seq':'18' if problem=='watermark' else '17'}
            with patch('online_trader.desktop.reconciliation.StatusClient',Client):window=self.loaded(previewer=read,pipe_name='pipe')
            self.assertIsNone(window._preview);self.assertFalse(window.register_button.isEnabled())
            if problem!='watermark':self.assertEqual(calls,[])

    def test_exports_use_the_complete_review_snapshot_and_never_request_overwriting(self):
        calls=[]
        def export(value,path,**kwargs):calls.append((copy.deepcopy(value),path,kwargs));return path
        window=self.loaded(exporter=export);expected=copy.deepcopy(window._preview)
        for kind in ('csv','json','xlsx'):
            with patch('online_trader.desktop.reconciliation.QFileDialog.getSaveFileName',return_value=('report.'+kind,kind)):
                window.export(kind);self.wait_idle(window)
        self.assertEqual(len(calls),3)
        for index,kind in enumerate(('csv','json','xlsx')):
            self.assertEqual(calls[index][0],expected);self.assertEqual(calls[index][2],{'format':kind})
        with patch('online_trader.desktop.reconciliation.QFileDialog.getSaveFileName',return_value=('','')):window.export('csv')
        self.assertEqual(len(calls),3)

    def test_export_failure_preserves_preview_and_does_not_retry(self):
        calls=[]
        def export(*_args,**kwargs):calls.append(kwargs);raise TraderError('E_REPORT_EXISTS','Existing destination')
        window=self.loaded(exporter=export)
        with patch('online_trader.desktop.reconciliation.QFileDialog.getSaveFileName',return_value=('existing.csv','csv')):
            window.export('csv');self.wait_idle(window)
        self.assertEqual(len(calls),1);self.assertIn('E_REPORT_EXISTS',window.status_label.text())
        self.assertFalse(window._stale);self.assertTrue(window.register_button.isEnabled())

    def test_close_during_async_preview_is_local_and_suppresses_late_publication(self):
        entered,release=threading.Event(),threading.Event();calls=[]
        def read(**_kwargs):
            calls.append(1);entered.set()
            if not release.wait(2):raise TraderError('E_TIMEOUT','Timed out')
            return preview()
        window=self.window(previewer=read);start=time.monotonic();window.preview()
        self.assertLess(time.monotonic()-start,0.25);self.assertTrue(entered.wait(1))
        window.preview();window.close();release.set();self.wait_idle(window);self.app.processEvents()
        self.assertEqual(calls,[1]);self.assertIsNone(window._preview)
        window.show();window.preview();self.wait_idle(window);self.assertIsNotNone(window._preview)

    def test_programmatic_input_change_during_async_read_cannot_publish_old_preview(self):
        entered,release=threading.Event(),threading.Event()
        def read(**_kwargs):entered.set();release.wait(2);return preview()
        window=self.window(previewer=read);window.preview();self.assertTrue(entered.wait(1))
        window.day_edit.setText('2026-11-11');release.set();self.wait_idle(window)
        self.assertIsNone(window._preview);self.assertTrue(window._stale);self.assertFalse(window.register_button.isEnabled())

    def test_money_report_button_opens_same_ledger_and_pipe_without_reading_or_controlling_kernel(self):
        window=ReportsWindow('chosen-ledger.db',KEY,pipe_name='same-pipe');self.windows.append(window)
        with patch('online_trader.desktop.reconciliation.ReconciliationWindow') as factory:
            window.open_reconciliation();window.open_reconciliation()
        factory.assert_called_once_with('chosen-ledger.db',KEY,pipe_name='same-pipe')
        self.assertEqual(factory.return_value.show.call_count,2)


if __name__=='__main__':unittest.main()
