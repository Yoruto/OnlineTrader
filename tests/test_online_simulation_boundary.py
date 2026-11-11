from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import copy,hashlib,json,sys,tempfile,time,unittest

from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import TqKqPort,credentials,number
from online_trader.online_scope import SimulationGrant
from online_trader.runtime.simulation import SimulationSession
from online_trader.scheduling.reference import ReferenceClock,measure_reference
from online_trader.protection.reducer import initial_state,SimulationPlanPermit
from tests.test_online_simulation import FakeClock,FakeOnlinePort,OnlineSimulationFixture,grant_for


class SimulationGrantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.grant=grant_for(Path(self.temp.name)/'ledger.db')
    def test_grant_return_value_cannot_mutate_scope(self):
        value=self.grant.value;value['quantity']=10
        self.assertEqual(self.grant.value['quantity'],1)
    def test_rejects_expanded_or_wrong_typed_scope(self):
        for values in ({'quantity':True},{'quantity':2},{'opening_attempts':2},{'hold_seconds':61},{'contracts':['SHFE.au2612']},
                       {'contracts':['DCE.p2701','DCE.p2701']},{'ledger_path':'ledger.db'},{'account_fingerprint':'x'*16},
                       {'not_after_utc':'2026-11-10T06:35:00Z'},{'trading_day':'2026-11-11'},{'not_before_utc':None},
                       {'unknown':1},{'grant_id':'contains space'}):
            with self.subTest(values=values),self.assertRaises(TraderError):SimulationGrant.parse(canonical_bytes(self.grant.value|values))
    def test_window_is_explicit_inclusive_and_aware(self):
        self.assertTrue(self.grant.in_window(datetime(2026,11,10,5,30,tzinfo=timezone.utc)))
        self.assertFalse(self.grant.in_window(datetime(2026,11,10,5,29,59,tzinfo=timezone.utc)))
        self.assertFalse(self.grant.in_window(datetime(2026,11,10,5,35,1,tzinfo=timezone.utc)))
    def test_missing_or_oversized_grant_file_is_rejected(self):
        path=Path(self.temp.name)/'grant.json'
        with self.assertRaises(TraderError):SimulationGrant.load(path)
        path.write_bytes(b' '*32769)
        with self.assertRaises(TraderError):SimulationGrant.load(path)


class ReferenceClockTests(unittest.TestCase):
    def test_monotonic_progress_ignores_os_clock_changes(self):
        clock=ReferenceClock(1000,20,2.5,())
        with patch('online_trader.scheduling.reference.time.monotonic',return_value=30),patch('online_trader.scheduling.reference.time.time',return_value=-100000):
            self.assertEqual(clock.now().timestamp(),1010)
        self.assertFalse(clock.record()['os_clock_changed'])
    def test_reference_expires_and_regression_blocks(self):
        for now in (19,320,400):
            with self.subTest(now=now),patch('online_trader.scheduling.reference.time.monotonic',return_value=now),self.assertRaises(TraderError):ReferenceClock(1000,20,0,()).now()
    def test_unavailable_ntp_cannot_fall_back_to_host_clock(self):
        with patch('online_trader.scheduling.reference.socket.socket',side_effect=OSError('unavailable')),self.assertRaises(TraderError):measure_reference()


class NativeApi:
    def __init__(self,account=None,**kwargs):
        self._account=account or NS(_get_trade_more_data=lambda data:False);self._data={'notify':{}};self.orders={};self.trades={};self.closed=0;self.inserted=[];self.cancelled=[]
        self.pos=NS(pos_long=0,pos_short=0)
        self.quote=NS(datetime='2026-11-10 13:30:01.000000',expired=False,bid_price1=999.,ask_price1=1000.,last_price=1000.,price_tick=1.,lower_limit=900.,upper_limit=1100.,bid_volume1=10,ask_volume1=10,volume_multiple=10)
    def get_quote(self,code):return self.quote
    def get_account(self,**kw):return NS(balance=1000000.,available=1000000.,margin=0.)
    def get_position(self,code=None,**kw):return self.pos if code else {'DCE.p2701':self.pos}
    def get_order(self,**kw):return self.orders
    def get_trade(self,**kw):return self.trades
    def wait_update(self,**kw):return True
    def insert_order(self,**kw):self.inserted.append(kw);return NS(order_id=kw['order_id'])
    def cancel_order(self,oid,**kw):self.cancelled.append(oid)
    def close(self):self.closed+=1


class TqKqAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.grant=grant_for(Path(self.temp.name)/'ledger.db')
        self.api=NativeApi();self.clock=FakeClock();self.port=TqKqPort(self.api,self.api._account,self.grant,self.clock)
        self.request={'account_key':self.grant.account_key,'contract_id':'DCE.p2701','trading_day':'2026-11-10','original_qty':1,'effect':'OPEN','direction':'BUY','limit_price':'1000','child_order_id':'child-1'}
    def test_native_send_uses_explicit_simulation_account_and_deterministic_owned_id(self):
        self.port.send(self.request);call=self.api.inserted[0]
        self.assertIs(call['account'],self.api._account);self.assertEqual(call['offset'],'OPEN');self.assertEqual(call['volume'],1)
        self.assertEqual(call['order_id'],self.port.order_id('child-1'));self.assertEqual(len(call['order_id']),32)
    def test_unknown_native_call_cannot_be_replayed(self):
        with patch.object(self.api,'insert_order',side_effect=OSError('uncertain')):
            with self.assertRaises(OSError):self.port.send(self.request)
        with self.assertRaises(TraderError):self.port.send(self.request)
    def test_second_opening_new_id_is_blocked_and_short_open_is_unsupported(self):
        with self.assertRaises(TraderError):self.port.send(self.request|{'direction':'SELL'})
        self.port.send(self.request)
        with self.assertRaises(TraderError):self.port.send(self.request|{'child_order_id':'another'})
        self.assertEqual(len(self.api.inserted),1)
    def test_stale_future_crossed_or_empty_quote_prevents_send(self):
        for values in ({'datetime':'2026-11-10 13:29:00'},{'datetime':'2026-11-10 13:31:00'},{'bid_price1':1001.},{'ask_volume1':0},{'expired':True},{'price_tick':float('nan')}):
            original=vars(self.api.quote).copy()
            try:
                vars(self.api.quote).update(values)
                with self.subTest(values=values),self.assertRaises(TraderError):self.port.validate_send(self.request)
            finally:vars(self.api.quote).update(original)
        self.assertEqual(self.api.inserted,[])
    def test_read_only_adapter_cannot_send_or_cancel(self):
        self.port.read_only=True
        with self.assertRaises(TraderError):self.port.send(self.request)
        with self.assertRaises(TraderError):self.port.cancel(self.request)
    def test_foreign_position_and_wrong_price_prevent_send(self):
        self.api.pos.pos_long=1
        with self.assertRaises(TraderError):self.port.send(self.request)
        self.api.pos.pos_long=0
        for price in ('1000.5','1101'):
            with self.subTest(price=price),self.assertRaises(TraderError):self.port.send(self.request|{'limit_price':price})
    def test_only_exact_single_long_can_be_reduced(self):
        close=self.request|{'effect':'REDUCE','direction':'SELL'}
        with self.assertRaises(TraderError):self.port.validate_send(close)
        self.api.pos.pos_long=1;self.port.validate_send(close)
        self.api.pos.pos_short=1
        with self.assertRaises(TraderError):self.port.validate_send(close)
    def test_disconnect_then_reconnect_is_latched_until_owner_reconciliation(self):
        self.api._data['notify']['a']={'code':2019112910};self.port.poll_updates()
        self.assertTrue(self.port.fault);self.assertFalse(self.port.complete())
        self.api._data['notify']['b']={'code':2019112902};self.port.poll_updates()
        self.assertTrue(self.port.complete());self.assertTrue(self.port.fault);self.assertEqual(self.port.source_epoch,2)
        with self.assertRaises(TraderError):self.port.send(self.request)
    def test_callback_identity_conflict_is_rejected(self):
        self.port.send(self.request);oid=self.port.order_id('child-1')
        self.api.orders[oid]=NS(volume_orign=1,volume_left=1,exchange_id='DCE',instrument_id='l2701',direction='BUY',offset='OPEN',status='ALIVE',trade_records={})
        with self.assertRaises(TraderError):self.port.observations()
    def test_missing_native_order_is_not_proof_of_no_send(self):
        self.port.send(self.request);self.assertEqual(self.port.observations(),[]);self.assertIn('child-1',self.port._issued)
    def test_snapshot_uses_exact_numbers_and_keeps_unknown_money_unknown(self):
        snapshot=self.port.snapshot();self.assertEqual(snapshot['funds']['balance'],'1000000');self.assertIsNone(snapshot['funds']['commission'])
        self.assertEqual(snapshot['gateway_identity'],self.port.gateway_identity);canonical_bytes(snapshot)
    def test_foreign_cancel_is_rejected_and_close_is_idempotent(self):
        with self.assertRaises(TraderError):self.port.cancel(self.request)
        self.port.close();self.port.close();self.assertEqual(self.api.closed,1)
    def test_credentials_parser_is_scoped_and_errors_do_not_echo_secrets(self):
        path=Path(self.temp.name)/'credentials.txt';path.write_text('SimNow账号\n用户名:other\n密码:otherpass\n快期账号\n用户名:test-user\n密码:test-secret\n',encoding='utf-8')
        self.assertEqual(credentials(path),{'username':'test-user','password':'test-secret'})
        path.write_text('快期账号\n用户名:test-user\n密码:test-secret\n密码:duplicate',encoding='utf-8')
        with self.assertRaises(TraderError) as failure:credentials(path)
        self.assertNotIn('test-secret',str(failure.exception))
    def test_explicit_connect_only_constructs_tqkq_and_no_sdk_module_is_needed_for_import(self):
        class Account:
            def _get_trade_more_data(self,data):return False
        username='test-user';path=Path(self.temp.name)/'credentials.txt';path.write_text('快期账号\n用户名:'+username+'\n密码:secret',encoding='utf-8')
        grant=grant_for(Path(self.temp.name)/'ledger.db',account_fingerprint=hashlib.sha256(username.encode()).hexdigest()[:16])
        module=NS(TqApi=NativeApi,TqKq=Account,TqAuth=lambda u,p:NS())
        with patch.dict(sys.modules,{'tqsdk':module}):port=TqKqPort.connect(path,grant,self.clock,read_only=True)
        self.assertIs(type(port.account),Account);self.assertTrue(port.read_only)
        self.assertTrue(port.broker_connected);port.close();self.assertFalse(port.broker_connected)
    def test_unknown_numeric_values_are_not_zero(self):
        for value in (True,None,'1',float('nan'),float('inf')):self.assertIsNone(number(value))


class SimulationSessionTests(OnlineSimulationFixture,unittest.TestCase):
    def setUp(self):
        super().setUp();self.port.clock.monotonic_anchor=time.monotonic();self.port.poll_updates=lambda seconds:None
        self.session=SimulationSession(self.kernel,Path(self.temp.name)/'session',emit=lambda event:None)
    def test_session_needs_two_distinct_quotes_and_keeps_one_opening(self):
        self.session.step();self.assertEqual(self.port.sent,[])
        self.port.prices['source_time']='quote-2';self.session.step();self.assertEqual(len(self.port.sent),1)
        self.session.step();self.assertEqual(len(self.port.sent),1)
    def test_future_window_never_arms_or_creates_intent(self):
        self.port.clock.value-=timedelta(minutes=1);self.session.step()
        self.assertEqual(self.session.phase,'WAITING_AUTHORIZED_OPEN_WINDOW');self.assertEqual(self.kernel._staged,{})
        self.assertFalse(self.kernel.snapshot()['armed'])
    def test_explicit_pause_before_open_cannot_be_auto_rearmed(self):
        self.kernel.pause();self.kernel.wait_idle();self.session.step()
        self.assertTrue(self.session.stop.is_set());self.assertEqual(self.session.result['status'],'PAUSED_BEFORE_OPEN');self.assertEqual(self.port.sent,[])
    def test_operator_protocol_pause_is_respected_by_the_session_owner(self):
        from tests.test_control import ControlTests
        proxy=NS(kernel=self.kernel,account=self.kernel.account_key)
        message=ControlTests.message(proxy,'PAUSE_NEW_ENTRIES',{'cancel_owned_entry_remainders':False})
        future=self.kernel.execute_command(message,'desktop');self.kernel.wait_idle();self.assertEqual(future.result()['status'],'SUCCEEDED')
        self.session.step();self.port.prices['source_time']='after-pause';self.session.step()
        self.assertTrue(self.session.stop.is_set());self.assertEqual(self.port.sent,[])
    def test_stop_file_before_open_prevents_intent(self):
        (self.session.output/'stop-before-open').touch();self.session.step()
        self.assertTrue(self.session.stop.is_set());self.assertEqual(self.port.sent,[])
    def test_window_expiry_leaves_owner_disarmed(self):
        self.port.clock.value+=timedelta(minutes=6);self.session.step()
        self.assertEqual(self.session.result['status'],'NO_ENTRY_WITHIN_WINDOW');self.assertFalse(self.kernel.snapshot()['armed'])
    def test_sdk_exception_is_sanitized_and_blocks_new_openings(self):
        self.port.poll_updates=lambda seconds:(_ for _ in ()).throw(OSError('private details'))
        self.session.step();self.assertEqual(self.session.phase,'BLOCKED_E_GATEWAY_RUNTIME');self.assertFalse(self.kernel.snapshot()['armed'])
    def test_boolean_permit_cannot_enable_online_reducer(self):
        plan=self.sim.plan('DCE.p2701','1000',stop_points='16',take_profit_points='50')
        with self.assertRaises(TraderError):initial_state(plan,simulation_permit=True)
        permit=self.sim.permit(plan);initial_state(plan,simulation_permit=permit)
        with self.assertRaises(TraderError):initial_state(plan|{'initial_open_quantity':2},simulation_permit=permit)
    def test_reconnect_reconciles_and_resumes_exits_without_rearming(self):
        _,plan=self.filled();self.port.fault=True;self.port.source_epoch+=1;self.sim.tick()
        self.assertFalse(self.port.fault);self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.kernel._committed[plan['position_cycle_id']]['protection']['owned_qty'],1)
    def test_late_discovered_native_fill_does_not_restart_sixty_seconds(self):
        child,plan=self.open();self.kernel.wait_idle();self.port.fill(child)
        self.port.clock.value+=timedelta(seconds=70);self.sim.tick()
        cycle=self.kernel._staged[plan['position_cycle_id']]
        self.assertLessEqual(int(cycle['online_deadline']['due_monotonic_ns']),time.monotonic_ns())
        self.assertTrue(any(o['effect']=='REDUCE' for o in cycle['orders']))
    def test_small_native_clock_lead_never_shortens_hold_below_sixty_seconds(self):
        child,plan=self.open();self.kernel.wait_idle();self.port.fill(child)
        fill=self.port.observed[child]['fills'][0];fill['source_time_ns']=str(int(fill['source_time_ns'])+500_000_001)
        self.sim.tick();deadline=self.kernel._committed[plan['position_cycle_id']]['online_deadline']
        actual_due=datetime.fromisoformat(deadline['due_reference_utc'].replace('Z','+00:00'))
        expected=self.port.clock.now()+timedelta(seconds=60,microseconds=500001)
        self.assertEqual(actual_due,expected)
        self.assertGreaterEqual(int(deadline['due_monotonic_ns'])-int(deadline['first_fill_monotonic_ns']),60_500_000_000)
    def test_restart_preserves_original_deadline_and_does_not_replay_open(self):
        from online_trader.kernel import Kernel
        _,plan=self.filled();cid=plan['position_cycle_id'];deadline=copy.deepcopy(self.kernel._committed[cid]['online_deadline'])
        self.kernel.close()
        self.kernel=Kernel(self.path,self.grant.account_key,port=self.port,simulation_grant=self.grant,risk_budget='1000')
        self.sim=self.kernel.simulation;self.sim.initialize();self.port.clock.value+=timedelta(seconds=61)
        self.port.prices['source_time']='after-restart';self.sim.tick();self.kernel.wait_idle()
        self.assertEqual(self.kernel._committed[cid]['online_deadline'],deadline)
        self.assertEqual(sum(o['effect']=='OPEN' for o in self.port.sent),1)
        self.assertTrue(any(o['effect']=='REDUCE' for o in self.kernel._staged[cid]['orders']))
        self.assertFalse(self.kernel.snapshot()['armed'])
    def test_online_control_policy_is_only_the_committed_execution_scope(self):
        policy='sim-execution-'+self.grant.content_hash[:24]
        self.assertEqual(self.kernel.control._policy(policy,'EXECUTION')['execution_scope'],'ONLINE_SIMULATION')
        for name,kind in ((policy,'AUTHORITY'),('foreign-policy','EXECUTION')):
            with self.subTest(name=name,kind=kind),self.assertRaises(TraderError):self.kernel.control._policy(name,kind)
    def test_foreign_activity_after_open_blocks_reconciliation(self):
        _,plan=self.filled();self.port.native['foreign']={'order_id':'foreign','contract_id':'DCE.p2701','status':'FINISHED'}
        with self.assertRaises(TraderError):self.sim.reconcile(plan['position_cycle_id'])
