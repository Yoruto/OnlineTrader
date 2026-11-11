from datetime import datetime,timezone,timedelta
from pathlib import Path
import copy,json,tempfile,time,unittest,uuid
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes,content_hash
from online_trader.contracts.validation import validate_plan
from online_trader.errors import TraderError
from online_trader.gateway.ports import OnlineSimulationPort
from online_trader.gateway.tqkq import TqKqPort
from online_trader.kernel import Kernel
from online_trader.online_scope import SimulationGrant


class FakeClock:
    def __init__(self):self.value=datetime(2026,11,10,5,30,1,tzinfo=timezone.utc)
    def now(self):return self.value

def grant_for(path,**changes):
    value={'schema_version':'KQ_MANUAL_SIM/1','grant_id':'afternoon-'+uuid.uuid4().hex,'account_fingerprint':uuid.uuid4().hex[:16],
        'ledger_path':str(path.resolve()),'contracts':['DCE.p2701'],'trading_day':'2026-11-10','not_before_utc':'2026-11-10T05:30:00Z',
        'not_after_utc':'2026-11-10T05:35:00Z','quantity':1,'opening_attempts':1,'hold_seconds':60,'authorization_reference':'user-afternoon-test-20261110'}
    value.update(changes);return SimulationGrant.parse(canonical_bytes(value))


class FakeOnlinePort(OnlineSimulationPort):
    def __init__(self,grant):
        self.grant=grant;self.clock=FakeClock();self.gateway_identity='TQKQ:'+grant.value['account_fingerprint']
        self.fault=False;self.block_send=False;self.good_quote=True;self.synced=True;self.sent=[];self.cancelled=[];self.native={};self.observed={};self.long=0;self.short=0
        self.prices={'price':'1000','bid1':'999','ask1':'1000','source_time':'quote-1','tick_size':'1','multiplier':'10'}
    order_id=staticmethod(TqKqPort.order_id)
    def complete(self):return self.synced
    def fresh(self,code):return self.good_quote
    def position(self,code):return {'long':self.long,'short':self.short}
    def active_orders(self,code):return [o for o in self.native.values() if o['status']=='ALIVE']
    def market(self,code):return copy.deepcopy(self.prices)
    def instrument(self,code):return {'tick_size':'1','multiplier':'10','currency':'CNY'}
    def snapshot(self):
        return {'snapshot_id':'snapshot-'+uuid.uuid4().hex,'account_key':self.grant.account_key,'gateway_identity':self.gateway_identity,'source_epoch':self.source_epoch,
            'trading_day':'2026-11-10','complete':self.synced,'funds':{'balance':'1000000','available':'1000000','margin':'0'},
            'positions':[] if not (self.long or self.short) else [{'contract_id':'DCE.p2701','long':self.long,'short':self.short}],
            'orders':copy.deepcopy(list(self.native.values())),
            'trades':[{'trade_id':f['trade_key'][3:],'order_id':o['order_id'],'contract_id':o['contract_id'],
                       'direction':self.native[o['order_id']]['direction'],'offset':self.native[o['order_id']]['offset'],
                       'quantity':f['quantity'],'price':f['price'],'source_time_ns':f['source_time_ns']} for o in self.observed.values() for f in o['fills']]}
    def validate_send(self,request):
        if self.block_send or not self.good_quote or self.fault or not self.synced:raise TraderError('E_GATEWAY_SCOPE','Blocked fake gate')
    def send(self,request):
        self.validate_send(request)
        durable=self.kernel.ledger.query('SELECT COUNT(*) AS n FROM child_orders WHERE child_order_id=?',(request['child_order_id'],))[0]['n']
        assert durable==1,'Native call before durable child'
        self.sent.append(copy.deepcopy(request));child=request['child_order_id'];oid=self.order_id(child)
        self.native[oid]={'order_id':oid,'contract_id':request['contract_id'],'status':'ALIVE','quantity':1,'remaining':1,'direction':request['direction'],'offset':'OPEN' if request['effect']=='OPEN' else 'CLOSE','limit_price':request['limit_price']}
        self.observed[child]={'child_order_id':child,'order_id':oid,'contract_id':request['contract_id'],'status':'ALIVE','reported_fill_qty':0,'remaining':1,'fills':[]}
    def cancel(self,request):self.cancelled.append(request['child_order_id'])
    def observations(self):return copy.deepcopy(list(self.observed.values()))
    def fill(self,child,price='1000'):
        o=self.observed[child];o.update(status='FINISHED',reported_fill_qty=1,remaining=0)
        o['fills']=[{'trade_key':'kq-trade-'+uuid.uuid4().hex,'quantity':1,'price':price,'source_time_ns':str(int(self.clock.now().timestamp())*1_000_000_000)}]
        self.native[self.order_id(child)].update(status='FINISHED',remaining=0)
        self.long+=1 if self.native[self.order_id(child)]['offset']=='OPEN' else -1
    def terminal(self,child):
        self.observed[child]['status']='FINISHED';self.native[self.order_id(child)]['status']='FINISHED'


class OnlineSimulationFixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.db';self.grant=grant_for(self.path);self.port=FakeOnlinePort(self.grant)
        self.kernel=Kernel(self.path,self.grant.account_key,port=self.port,simulation_grant=self.grant,risk_budget='1000',tick_size='1',multiplier='10')
        self.sim=self.kernel.simulation;self.sim.initialize()
    def tearDown(self):
        self.kernel.close();self.temp.cleanup()
    def arm(self):self.sim.arm();self.kernel.wait_idle()
    def open(self):
        self.arm();plan=self.sim.plan('DCE.p2701','1000',stop_points='16',take_profit_points='50');validate_plan(plan)
        context=self.kernel.issue_entry_context('operator',plan['contract_id'])
        child=self.kernel.admit_entry(plan,'online-'+uuid.uuid4().hex,'1000',context_id=context['context_id'],peer_id='operator')
        return child,plan
    def filled(self):
        child,plan=self.open();self.kernel.wait_idle();self.port.fill(child);self.sim.tick();return child,plan

class OnlineSimulationKernelTests(OnlineSimulationFixture,unittest.TestCase):
    def test_online_environment_and_default_disarmed_are_explicit(self):
        snapshot=self.kernel.snapshot();self.assertFalse(snapshot['armed']);self.assertFalse(snapshot['development_session'])
        self.assertEqual(snapshot['execution_scope'],'ONLINE_SIMULATION')
        self.assertEqual(self.kernel.ledger.query('SELECT broker_identity,environment FROM ledger_scope')[0],{'broker_identity':'TQKQ','environment':'ONLINE_SIM'})
        with self.assertRaises(TraderError):self.kernel.arm_development()

    def test_exact_grant_configuration_manifest_and_money_snapshot_are_committed(self):
        manifest=self.kernel.ledger.query('SELECT manifest_json FROM approval_manifests WHERE approval_record_id=?',(self.grant.value['grant_id'],))
        self.assertEqual(json.loads(manifest[0]['manifest_json']),self.grant.value)
        self.assertEqual(self.kernel.money.account_projection()['equity'],'1000000')

    def test_online_native_send_waits_for_matching_owner_commit_ack(self):
        child,plan=self.open();self.assertEqual(self.port.sent,[])
        self.kernel.wait_idle();self.assertEqual(len(self.port.sent),1);self.assertEqual(self.port.sent[0]['child_order_id'],child)
        self.assertEqual(self.port.sent[0]['contract_id'],'DCE.p2701');self.assertEqual(self.port.sent[0]['direction'],'BUY')
        self.assertEqual(plan['execution_scope'],'ONLINE_SIMULATION')

    def test_public_fill_quote_and_terminal_paths_cannot_forge_online_callbacks(self):
        child,plan=self.open();self.kernel.wait_idle()
        for call in (lambda:self.kernel.record_fill(child,'fake',1,'1000'),lambda:self.kernel.observe(plan['position_cycle_id'],'1000'),lambda:self.kernel.confirm_terminal(child,'FILLED',0)):
            with self.assertRaises(TraderError):call()
        self.assertEqual(self.kernel.ledger.query('SELECT * FROM fill_facts'),[])

    def test_real_adapter_callbacks_reach_original_fill_and_money_projections(self):
        child,plan=self.filled()
        rows=self.kernel.ledger.query('SELECT gateway_identity,quantity,price FROM fill_facts')
        self.assertEqual(rows,[{'gateway_identity':self.port.gateway_identity,'quantity':1,'price':'1000'}])
        self.assertEqual(self.kernel.snapshot()['cycles'][0]['protection']['owned_qty'],1)
        self.assertEqual(self.kernel.money.report()['summary']['entry_quantity'],1)

    def test_first_fill_commits_deadline_in_same_original_fill_transaction(self):
        child,plan=self.filled();cycle=self.kernel._committed[plan['position_cycle_id']]
        deadline=cycle['online_deadline'];self.assertEqual(int(deadline['due_monotonic_ns'])-int(deadline['first_fill_monotonic_ns']),60_000_000_000)
        row=self.kernel.ledger.query("SELECT payload_json FROM event_journal WHERE event_type='FILL_CONFIRMED'")[0]
        self.assertIn('online_deadline',row['payload_json'])
        self.sim.tick();self.assertEqual(self.kernel._committed[plan['position_cycle_id']]['online_deadline'],deadline)

    def test_due_exit_uses_same_protection_remaining_target_and_original_writer(self):
        child,plan=self.filled();cid=plan['position_cycle_id'];due=int(self.kernel._committed[cid]['online_deadline']['due_monotonic_ns'])
        self.port.prices['source_time']='quote-2'
        with patch('online_trader.kernel.simulation.time.monotonic_ns',return_value=due):self.sim.tick()
        exits=[o for o in self.kernel._staged[cid]['orders'] if o['effect']=='REDUCE'];self.assertEqual(len(exits),1)
        self.kernel.wait_idle();self.assertEqual(len(self.port.sent),2)
        self.port.fill(exits[0]['child_order_id'],'999');self.sim.tick()
        self.assertEqual(self.kernel._committed[cid]['lifecycle'],'CLOSED')
        self.assertEqual(self.kernel.money.report()['summary']['gross_realized_pnl'],'-10')
        self.assertEqual(self.kernel.ledger.query("SELECT remaining_qty FROM position_cycles WHERE position_cycle_id=?",(cid,))[0]['remaining_qty'],0)

    def test_online_reconciliation_has_real_gateway_snapshot_parts(self):
        _,plan=self.filled()
        row=self.kernel.ledger.query('SELECT gateway_identity FROM reconciliation_runs')[0]
        self.assertEqual(row['gateway_identity'],self.port.gateway_identity)
        parts=self.kernel.ledger.query('SELECT part_kind,completion_evidence_json,snapshot_json FROM reconciliation_parts')
        self.assertEqual({row['part_kind'] for row in parts},{'FUNDS','POSITIONS','ORDERS','TRADES'})
        self.assertTrue(all(json.loads(row['completion_evidence_json'])['kind']=='TQKQ_FULL_SYNC_SNAPSHOT' for row in parts))

    def test_second_opening_cannot_reuse_grant_even_after_first_is_cancelled(self):
        child,plan=self.open();self.kernel.wait_idle();self.port.terminal(child);self.sim.tick()
        with self.assertRaises(TraderError):self.sim.arm()
        with self.assertRaises(TraderError):self.sim.validate_entry(self.sim.plan('DCE.p2701','1000',stop_points='16',take_profit_points='50'))

    def test_quote_changes_before_commit_prevent_native_send(self):
        child,plan=self.open();self.port.block_send=True;self.kernel.wait_idle()
        self.assertEqual(self.port.sent,[])
        order=self.kernel._staged[plan['position_cycle_id']]['orders'][0]
        self.assertEqual(order['send_state'],'DEFINITELY_NOT_SENT')

    def test_unknown_native_send_is_not_repeated_and_disarms_durably(self):
        self.arm();calls=[]
        def uncertain(request):calls.append(request);raise OSError('injected uncertainty')
        self.port.send=uncertain
        plan=self.sim.plan('DCE.p2701','1000',stop_points='16',take_profit_points='50')
        context=self.kernel.issue_entry_context('operator','DCE.p2701')
        self.kernel.admit_entry(plan,'uncertain','1000',context_id=context['context_id'],peer_id='operator');self.kernel.wait_idle()
        self.assertEqual(len(calls),1);self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertTrue(self.kernel._has_unknown())
        self.assertEqual(self.kernel.ledger.query('SELECT new_open_status FROM authority')[0]['new_open_status'],'DISARMED')

    def test_disconnect_latches_pause_without_deleting_protection(self):
        _,plan=self.filled();self.port.fault=True;self.sim.tick()
        self.assertFalse(self.kernel.snapshot()['armed']);self.assertEqual(self.kernel._committed[plan['position_cycle_id']]['protection']['owned_qty'],1)

    def test_wrong_plan_scope_quantity_direction_and_advanced_modes_rejected(self):
        plan=self.sim.plan('DCE.p2701','1000',stop_points='16',take_profit_points='50')
        for update in ({'execution_scope':'DEVELOPMENT'},{'initial_open_quantity':2},{'direction':'SHORT'},{'approval_record_id':'different'},{'partial_reduction':{'enabled':True}}):
            with self.subTest(update=update),self.assertRaises(TraderError):self.sim.validate_entry(plan|update)

    def test_before_or_after_explicit_window_cannot_arm(self):
        for day in (datetime(2026,11,10,5,29,tzinfo=timezone.utc),datetime(2026,11,10,5,36,tzinfo=timezone.utc)):
            self.port.clock.value=day
            with self.assertRaises(TraderError):self.sim.arm()

    def test_existing_foreign_position_or_order_blocks_entry(self):
        plan=self.sim.plan('DCE.p2701','1000',stop_points='16',take_profit_points='50')
        self.port.long=1
        with self.assertRaises(TraderError):self.sim.validate_entry(plan)
        self.port.long=0;self.port.native['foreign']={'status':'ALIVE'}
        with self.assertRaises(TraderError):self.sim.validate_entry(plan)

    def test_incomplete_or_wrong_account_snapshot_never_reconciles(self):
        _,plan=self.filled();self.port.synced=False
        with self.assertRaises(TraderError):self.sim.reconcile(plan['position_cycle_id'])

    def test_online_port_cannot_be_attached_through_development(self):
        with self.assertRaises(TraderError):Kernel(Path(self.temp.name)/'other.db','DEV:online-bypass',development=True,port=self.port)
