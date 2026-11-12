import copy
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest
from online_trader.contracts.codec import content_hash
from online_trader.contracts.validation import validate_message
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.workbench import workbench_view
from online_trader.transport.status_client import StatusClient
from qa.recording_port import RecordingPort
from tests import test_desktop as desktop_fixtures
from tests import test_online_dsv as dsv_fixtures


def view():
    return {'view_schema':'workbench/1','account_key':'account-test','kernel_boot_id':'boot-test','checkpoint_event_seq':'2',
        'entry_authority':'DISARMED','execution_scope':'DISARMED','connection':{'broker_connected':False,'synchronized':False,'fault':False,
        'recovery_required':False,'storage_failed':False,'clock_fault':None,'pending_transactions':0},
        'configuration':{'rule_set_id':'rule-test','config_revision':'1'},'markets':[],'cycles':[],'orders':[],'trades':[],
        'blocks':[],'commands':[],'policies':[],'truncated':False}


class WorkbenchPipe(desktop_fixtures.FakePipe):
    def send(self,message,*,timeout):
        if message['type']=='WorkbenchRequest':
            validate_message(message);self.sent.append(copy.deepcopy(message));value=view()
            self.queue.append(self.response('WorkbenchSnapshot',{'payload':value,'content_hash':content_hash(value)},message))
        elif message['type']=='GetCommandStatus':
            validate_message(message);self.sent.append(copy.deepcopy(message))
            self.queue.append(self.response('CommandReceipt',message['body']|{'status':'COMMITTED','receipt_revision':'1','tx_id':'tx-test',
                'event_seq':'3','durable_seq':'3','state_revision':'2','error_code':'NONE','reason':'Persisted','evidence_ids':[]},message))
        else:super().send(message,timeout=timeout)


class WorkbenchClientTests(unittest.TestCase):
    def test_checked_workbench_view_is_account_and_boot_bound(self):
        pipe=WorkbenchPipe()
        with patch('online_trader.transport.status_client.connect',return_value=pipe):
            client=StatusClient('pipe',expected_account_key='account-test',expected_kernel_boot_id='boot-test')
            self.assertEqual(client.workbench(),view());self.assertEqual(client.last_metadata['kernel_boot_id'],'boot-test')
        self.assertTrue(pipe.closed)
    def test_wrong_content_hash_inner_account_or_boot_never_publishes(self):
        for field,value in [('content_hash','0'*64),('account_key','another'),('kernel_boot_id','other-boot')]:
            def transform(message):
                if message['type']=='WorkbenchSnapshot':
                    if field=='content_hash':message['body'][field]=value
                    else:
                        message['body']['payload'][field]=value;message['body']['content_hash']=content_hash(message['body']['payload'])
                return message
            with self.subTest(field=field),patch('online_trader.transport.status_client.connect',return_value=WorkbenchPipe(transform)),self.assertRaises(TraderError):
                StatusClient('pipe').workbench()
    def test_changed_owner_refuses_command_before_context_or_send(self):
        for expected in ({'expected_account_key':'other'},{'expected_kernel_boot_id':'old-boot'}):
            pipe=WorkbenchPipe()
            with self.subTest(expected=expected),patch('online_trader.transport.status_client.connect',return_value=pipe),self.assertRaises(TraderError):
                StatusClient('pipe',**expected).command('CANCEL_OWNED',{'scope_type':'OWNED_ORDERS','scope_ids':[]},{'include_exits':False})
            self.assertEqual([m['type'] for m in pipe.sent],['Hello'])
    def test_selected_cancel_uses_exact_ids_context_and_receipt(self):
        pipe=WorkbenchPipe()
        def transform(message):
            if message['type']=='RequestContext':message['body']['scope_id']='child-owned'
            return message
        pipe.transform=transform
        with patch('online_trader.transport.status_client.connect',return_value=pipe):
            client=StatusClient('pipe');result=client.command('CANCEL_SELECTED',{'scope_type':'SELECTED_ORDERS','scope_ids':['child-owned']},{'child_order_ids':['child-owned']})
        self.assertEqual(result['status'],'SUCCEEDED');self.assertEqual(pipe.sent[-1]['body']['parameters']['child_order_ids'],['child-owned'])
        self.assertEqual(client.last_command['command_id'],result['command_id'])
    def test_timeout_keeps_identity_and_status_query_does_not_resend(self):
        def drop(message):
            if message['type']=='CommandReceipt':raise TraderError('E_TIMEOUT','Offline dropped reply')
            return message
        with patch('online_trader.transport.status_client.connect',return_value=WorkbenchPipe(drop)):
            client=StatusClient('pipe')
            with self.assertRaises(TraderError):client.command('CANCEL_OWNED',{'scope_type':'OWNED_ORDERS','scope_ids':[]},{'include_exits':False})
        self.assertIsNotNone(client.last_command);pipe=WorkbenchPipe()
        with patch('online_trader.transport.status_client.connect',return_value=pipe):self.assertEqual(client.command_status(client.last_command)['status'],'COMMITTED')
        self.assertEqual([m['type'] for m in pipe.sent],['Hello','GetCommandStatus'])
    def test_read_extension_does_not_admit_inbound_market_facts(self):
        message=StatusClient._message('WorkbenchRequest',{'market':'1055'})
        with self.assertRaises(TraderError):validate_message(message)


class WorkbenchOwnerTests(unittest.TestCase):
    def setUp(self):self.case=dsv_fixtures.OnlineDsvTests();self.case.setUp()
    def tearDown(self):self.case.tearDown()
    def test_observation_rebuild_keeps_last_quote_and_its_original_age(self):
        x = self.case
        x.start(max_quote_age_ms=5000)
        before = workbench_view(x.kernel)['markets'][0]
        self.assertTrue(before['fresh'])
        x.clock.advance(1000)
        x.api.tick(x.code, last=1055)
        x.clock.advance(6000)
        x.session.step()

        market = workbench_view(x.kernel)['markets'][0]
        self.assertEqual((market['last'], market['source_time_utc']), (before['last'], before['source_time_utc']))
        self.assertFalse(market['fresh'])
        self.assertNotIn('WAITING_FRESH_QUOTE', market['entry_reasons'])
        self.assertNotIn((x.code, before['trading_day']), x.session.driver.strategy._latest)
        self.assertEqual(x.api.inserted, [])

        x.quote(price=1048)
        updated = workbench_view(x.kernel)['markets'][0]
        self.assertEqual(updated['last'], '1048')
        self.assertGreater(updated['source_time_utc'], market['source_time_utc'])
        self.assertTrue(updated['fresh'])

    def test_rebuilt_quote_is_fresh_only_in_its_original_source_epoch(self):
        x = self.case
        x.start(max_quote_age_ms=5000)
        before = workbench_view(x.kernel)['markets'][0]
        x.clock.advance(1000)
        x.api.tick(x.code, last=1055)
        events = dict(x.session.feed.batches())[x.code]
        x.session._prime_path(x.code, events, extend=True, preserve_authority=True)

        market = workbench_view(x.kernel)['markets'][0]
        self.assertEqual((market['last'], market['source_time_utc']), (before['last'], before['source_time_utc']))
        self.assertTrue(market['fresh'])
        x.port.source_epoch += 1
        changed = workbench_view(x.kernel)['markets'][0]
        self.assertEqual((changed['last'], changed['source_time_utc']), (before['last'], before['source_time_utc']))
        self.assertFalse(changed['fresh'])
        self.assertEqual(x.api.inserted, [])

    def test_native_view_keeps_advanced_protection_orders_and_owned_trades(self):
        x=self.case;x.start(code='SHFE.bu2701');x.quote(1079);x.api.fill(x.api.inserted[0]['order_id'],5,1079);x.session.step()
        before=x.kernel._durable_seq;calls=len(x.api.inserted)
        result=workbench_view(x.kernel)
        self.assertEqual(result['cycles'][0]['remaining_quantity'],5);self.assertEqual(len(result['trades']),1)
        self.assertFalse(result['connection']['broker_connected']);self.assertTrue(result['connection']['synchronized'])
        self.assertEqual(result['cycles'][0]['close_route_review']['routes'],[{'native_offset':'CLOSETODAY','quantity':5}])
        self.assertEqual(x.kernel._durable_seq,before);self.assertEqual(len(x.api.inserted),calls)
        response=StatusClient._message('WorkbenchSnapshot',{'payload':result,'content_hash':content_hash(result)})
        validate_message(response)
    def test_native_foreign_bucket_is_explicitly_blocked_in_view(self):
        x=self.case;x.start(code='SHFE.bu2701');x.quote(1079);x.api.fill(x.api.inserted[0]['order_id'],5,1079);x.session.step();x.api.positions[x.code].pos_long_his=1
        result=workbench_view(x.kernel)['cycles'][0]['close_route_review']
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['error_code'],'E_OWNERSHIP')
    def test_view_never_includes_unaccepted_cycle(self):
        from concurrent.futures import Future
        x=self.case;x.start();x.clock.advance(1000);x.api.tick(last=1055)
        event=x.session.feed.batches()[0][1][-1];account,instrument=x.session.feed.facts(event,x.kernel)
        future=x.session.driver.on_market(event,account,instrument);pending=x.kernel._pending[0]
        ack=pending.future.result(timeout=3);held=Future();pending.future=held
        try:
            result=workbench_view(x.kernel);self.assertEqual(result['cycles'],[]);self.assertEqual(result['orders'],[])
        finally:held.set_result(ack);x.kernel.wait_idle()
        self.assertTrue(future.done())


class WorkbenchDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def idle(self,window):
        deadline=time.monotonic()+4
        while window._worker is not None and time.monotonic()<deadline:self.app.processEvents();QTest.qWait(5)
        self.assertIsNone(window._worker)

    def test_confirmed_arm_waits_for_refresh_and_keeps_the_receipt(self):
        calls=[];release=Event();release.set()
        class Client:
            last_command=None;last_metadata={'kernel_boot_id':'boot-test'};revision='1'
            def __init__(self,*args,**kwargs):pass
            def snapshot(self):
                release.wait(3)
                return desktop_fixtures.projection()
            def workbench(self):
                value=view()
                value['configuration']['config_revision']=self.revision
                value['policies']=[{'policy_id':'authority-policy','policy_kind':'AUTHORITY'}]
                return value
            def command(self,*args):
                calls.append(args)
                return {'command_id':'arm-command','payload_hash':'0'*64,'status':'SUCCEEDED','error_code':'NONE','reason':'Saved'}
        window=WorkbenchWindow('pipe',client_factory=Client)
        try:
            window.timer.stop();window.refresh();self.idle(window)
            def confirm(*args):
                release.clear();window.refresh()
                self.assertTrue(window.command_buttons['arm'].isEnabled())
                self.assertFalse(window.pause_button.isEnabled())
                return QMessageBox.Yes
            with patch.object(QMessageBox,'question',side_effect=confirm):window.arm()
            self.assertEqual(calls,[])
            release.set();self.idle(window)
            self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],'ARM_ENTRIES')
            window.refresh();self.idle(window)
            self.assertIn('SUCCEEDED',window.notice_label.text())
            with patch.object(QMessageBox,'question',side_effect=confirm):window.arm()
            Client.revision='2';release.set();self.idle(window)
            self.assertEqual(len(calls),1)
            self.assertIn('未发送',window.notice_label.text())
        finally:release.set();window.close();self.app.processEvents()
    def test_tabs_render_native_detail_and_unknown_connection_separately(self):
        class Client:
            last_command=None;last_metadata={'kernel_boot_id':'boot-test'}
            def __init__(self,*args,**kwargs):pass
            def snapshot(self):return desktop_fixtures.projection()
            def workbench(self):
                value=view();value['markets']=[{'contract_id':'DCE.p2701','name':'棕榈油','fresh':False}];return value
        window=WorkbenchWindow('pipe',client_factory=Client)
        try:
            window.timer.stop();window.refresh();self.idle(window)
            self.assertFalse(window._stale);self.assertIn('未连接',window.connection_detail.text());self.assertEqual(window.tabs.count(),8)
            self.assertEqual([window.tabs.tabText(index) for index in range(8)],
                ['策略主控','实时监控','挂单汇总','手动下单','运行日志','品种信息','偏移计算','系统设置'])
            self.assertFalse(window.dashboard.submit_button.isEnabled())
            self.assertIn('未知',window.funds_label.text());window.filter_input.setText('p2701');self.assertFalse(window.tables['markets'].isRowHidden(0))
            window.filter_input.setText('铜');self.assertTrue(window.tables['markets'].isRowHidden(0))
        finally:window.close();self.app.processEvents()
    def test_order_selection_follows_original_id_when_refresh_reorders_rows(self):
        class Client:
            last_command=None;last_metadata={'kernel_boot_id':'boot-test'};reverse=False
            def __init__(self,*args,**kwargs):pass
            def snapshot(self):return desktop_fixtures.projection()
            def workbench(self):
                result=view()
                result['orders']=[{'child_order_id':name,'contract_id':code,'effect':'OPEN'} for name,code in
                    (('order-a','DCE.p2701'),('order-b','DCE.l2701'))]
                if self.reverse:result['orders'].reverse()
                return result
        window=WorkbenchWindow('pipe',client_factory=Client)
        try:
            window.timer.stop();window.refresh();self.idle(window)
            window.tables['orders'].selectRow(0)
            Client.reverse=True;window.refresh();self.idle(window)
            self.assertEqual(window._selected_identities()['orders'],{'order-a'})
            self.assertEqual(window.tables['orders'].selectionModel().selectedRows()[0].row(),1)
        finally:window.close();self.app.processEvents()
    def test_selected_symbol_buttons_use_contract_scope_and_keep_global_authority_separate(self):
        calls=[]
        class Client:
            last_command=None;last_metadata={'kernel_boot_id':'boot-test'}
            def __init__(self,*args,**kwargs):pass
            def snapshot(self):return desktop_fixtures.projection()
            def workbench(self):
                value=view();value['markets']=[{'contract_id':'DCE.p2701','name':'棕榈油','enabled':False}]
                value['policies']=[{'policy_id':'authority-policy','policy_kind':'AUTHORITY'}];return value
            def command(self,*args):
                calls.append(args);return {'command_id':'symbol-command','payload_hash':'0'*64,'status':'SUCCEEDED','error_code':'NONE','reason':'Saved'}
        window=WorkbenchWindow('pipe',client_factory=Client)
        try:
            window.timer.stop();window.refresh();self.idle(window);window.tables['markets'].selectRow(0)
            window.set_symbol_entries(False);self.idle(window)
            self.assertEqual(calls[-1],('PAUSE_NEW_ENTRIES',{'scope_type':'CONTRACT','scope_ids':['DCE.p2701']},{'cancel_owned_entry_remainders':True}))
            window.refresh();self.idle(window);window.tables['markets'].selectRow(0);window.set_symbol_entries(True);self.idle(window)
            self.assertEqual(calls[-1][0],'ARM_ENTRIES');self.assertEqual(calls[-1][1],{'scope_type':'CONTRACT','scope_ids':['DCE.p2701']})
            self.assertEqual(window.view['entry_authority'],'DISARMED')
        finally:window.close();self.app.processEvents()
    def test_failed_refresh_keeps_rows_but_disables_commands(self):
        class Client:
            last_command=None;last_metadata={'kernel_boot_id':'boot-test'};fail=False
            def __init__(self,*args,**kwargs):pass
            def snapshot(self):
                if self.fail:raise TraderError('E_TIMEOUT','Offline')
                return desktop_fixtures.projection()
            def workbench(self):return view()
        window=WorkbenchWindow('pipe',client_factory=Client)
        try:
            window.timer.stop();window.refresh();self.idle(window);Client.fail=True;window.refresh();self.idle(window)
            self.assertTrue(window._stale);self.assertFalse(window.command_buttons['arm'].isEnabled());self.assertIn('过期',window.connection_label.text())
        finally:window.close();self.app.processEvents()
    def test_pause_routes_to_bound_original_owner_and_marks_stale(self):
        calls=[];release=Event();release.set()
        class Client:
            last_command=None;last_metadata={'kernel_boot_id':'boot-test'};authority='ARMED'
            def __init__(self,*args,**kwargs):calls.append(kwargs)
            def snapshot(self):
                release.wait(3)
                return desktop_fixtures.projection()
            def workbench(self):return view()|{'entry_authority':self.authority}
            def command(self,*args):
                calls.append(args);Client.authority='DISARMED'
                return {'command_id':'pause-owned','payload_hash':'0'*64,'status':'COMMITTED','error_code':'NONE','reason':'Persisted'}
        window=WorkbenchWindow('pipe',client_factory=Client)
        try:
            window.timer.stop();window.refresh();self.idle(window)
            release.clear();window.refresh()
            self.assertTrue(window.pause_button.isEnabled())
            self.assertTrue(window.command_buttons['reconcile'].isEnabled())
            self.assertFalse(window.command_buttons['arm'].isEnabled())
            window.pause_button.click()
            self.assertFalse(any(isinstance(call,tuple) for call in calls))
            release.set();self.idle(window)
            self.assertTrue(window._stale);self.assertIn('COMMITTED',window.notice_label.text())
            self.assertEqual(calls[-2]['expected_account_key'],'account-test');self.assertEqual(calls[-2]['expected_kernel_boot_id'],'boot-test')
            self.assertEqual(calls[-1][0],'PAUSE_NEW_ENTRIES')
            window.refresh();self.idle(window)
            self.assertTrue(window.command_buttons['arm'].isEnabled())
            self.assertFalse(window.pause_button.isEnabled())
        finally:release.set();window.close();self.app.processEvents()

    def test_dsv_stop_renders_native_receipt_and_waits_to_start_again(self):
        calls=[]
        class Client:
            last_command=None
            last_metadata={'kernel_boot_id':'boot-test'}
            authority='ARMED'
            pending=False
            receipt={'command_id':'stop-test','payload_hash':'0'*64,'status':'EXECUTING',
                     'error_code':'NONE','reason':'等待柜台确认','receipt_revision':'1'}
            def __init__(self,*args,**kwargs):pass
            def snapshot(self):return desktop_fixtures.projection()
            def workbench(self):
                current=view()
                current['configuration'].update(cancel_on_stop=True,stop_cancel_pending=self.pending)
                return current|{'entry_authority':self.authority,'commands':[self.receipt] if calls else []}
            def command(self,*args):
                calls.append(args)
                Client.authority='DISARMED'
                Client.pending=True
                return self.receipt
        window=WorkbenchWindow('pipe',client_factory=Client)
        try:
            window.timer.stop();window.refresh();self.idle(window)
            self.assertEqual(window.pause_button.text(),'停止策略 (F6)')
            window.pause_button.click();self.idle(window)
            self.assertEqual(calls,[('STOP_DSV_STRATEGY',{'scope_type':'ACCOUNT','scope_ids':[]},{})])
            window.refresh();self.idle(window)
            self.assertFalse(window.command_buttons['arm'].isEnabled())
            self.assertIn('等待柜台确认',window.notice_label.text())
            Client.pending=False
            Client.receipt=Client.receipt|{'status':'SUCCEEDED','reason':'已核实撤单终态','receipt_revision':'2'}
            window.refresh();self.idle(window)
            self.assertTrue(window.command_buttons['arm'].isEnabled())
            self.assertIn('已核实撤单终态',window.notice_label.text())
        finally:window.close();self.app.processEvents()
