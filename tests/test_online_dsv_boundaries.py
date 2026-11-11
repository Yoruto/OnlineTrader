from contextlib import redirect_stdout
from concurrent.futures import Future
from io import StringIO
from pathlib import Path
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from online_trader.cli import main
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.kernel import Kernel
from online_trader.online_scope import DsvSimulationGrant,SimulationGrant
from online_trader.protection.reducer import DsvSimulationPlanPermit,initial_state
from online_trader.runtime.dsv_simulation import prepare,run
from qa.dsv_native_facts import FactClock,NativeFactApi
from tests import test_online_dsv as fixtures
from tests.test_online_simulation import grant_for


class DsvScopeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.profile=load_confirmed_baseline()[0];self.calendar=fixtures.day_calendar()
        self.grant=fixtures.scope_for(self.root/'ledger.db',self.profile,self.calendar)
        for name,value in (('scope',self.grant.value),('profile',self.profile.to_record()),('calendar',self.calendar.to_record())):
            (self.root/(name+'.json')).write_bytes(canonical_bytes(value))

    def test_manual_scope_cannot_be_reinterpreted_as_strategy_scope(self):
        manual=grant_for(self.root/'manual/ledger.db')
        with self.assertRaises(TraderError):DsvSimulationGrant.parse(canonical_bytes(manual.value))
        with self.assertRaises(TraderError):SimulationGrant.parse(canonical_bytes(self.grant.value))
        api=NativeFactApi(FactClock())
        with self.assertRaises(TraderError):TqKqDsvPort(api,api._account,manual,FactClock())

    def test_missing_implicit_or_expanded_scope_fields_are_refused(self):
        for changes in ({'opening_attempts':True},{'opening_attempts':0},{'max_order_quantity':0},
                        {'max_order_quantity':False},{'risk_budget':'0'},{'profile_hash':'x'*64},
                        {'calendar_hash':'x'*64},{'credentials':'never-export'},
                        {'not_after_utc':'2026-11-12T07:00:00Z'},{'contracts':['CFFEX.IF2612']}):
            with self.subTest(changes=changes),self.assertRaises(TraderError):
                DsvSimulationGrant.parse(canonical_bytes(self.grant.value|changes))

    def test_offline_prepare_does_not_read_credentials_connect_or_arm(self):
        with patch('online_trader.runtime.dsv_simulation.measure_reference') as ntp,patch.object(TqKqDsvPort,'connect') as connect,redirect_stdout(StringIO()) as stdout:
            code=main(['dsv','prepare','--scope',str(self.root/'scope.json'),'--profile',str(self.root/'profile.json'),
                       '--calendar',str(self.root/'calendar.json'),'--output',str(self.root/'preparation')])
        self.assertEqual(code,0);connect.assert_not_called();ntp.assert_not_called()
        result=json.loads(stdout.getvalue());self.assertFalse(result['broker_connected'])
        self.assertFalse(result['account_state_checked']);self.assertEqual(result['entry_authority'],'DISARMED')
        self.assertFalse((self.root/'ledger.db').exists())

    def test_wrong_profile_is_refused_before_reference_or_account_connection(self):
        value=self.grant.value|{'profile_hash':'0'*64};(self.root/'scope.json').write_bytes(canonical_bytes(value))
        with patch('online_trader.runtime.dsv_simulation.measure_reference') as ntp,patch.object(TqKqDsvPort,'connect') as connect:
            with self.assertRaises(TraderError):run(self.root/'scope.json','unused',self.root/'profile.json',self.root/'calendar.json',self.root/'out')
        ntp.assert_not_called();connect.assert_not_called()

    def test_missing_force_close_calendar_is_not_replaced_by_invented_default(self):
        value=self.calendar.to_record();value['contracts'][0]['sessions'][0]['force_close_at_end']=False
        from online_trader.scheduling.calendar import TradingCalendar
        calendar=TradingCalendar.from_record(value)
        grant=fixtures.scope_for(self.root/'ledger.db',self.profile,calendar)
        (self.root/'scope.json').write_bytes(canonical_bytes(grant.value));(self.root/'calendar.json').write_bytes(canonical_bytes(value))
        with self.assertRaises(TraderError):prepare(self.root/'scope.json',self.root/'profile.json',self.root/'calendar.json',self.root/'out')

    def test_kernel_cannot_loosen_the_original_risk_budget(self):
        clock=FactClock();api=NativeFactApi(clock);port=TqKqDsvPort(api,api._account,self.grant,clock)
        port.configure_calendar(self.calendar)
        with self.assertRaises(TraderError):Kernel(self.root/'ledger.db',self.grant.account_key,port=port,
                                                  simulation_grant=self.grant,risk_budget='1000001')
        self.assertFalse((self.root/'ledger.db').exists())

    def test_returned_grant_value_cannot_mutate_its_immutable_limits(self):
        value=self.grant.value;value['opening_attempts']=1000
        self.assertEqual(self.grant.value['opening_attempts'],20)

    def test_requesting_subscription_before_open_is_not_proof_it_was_ready(self):
        from online_trader.gateway.dsv_facts import DsvFactFeed
        clock=FactClock('2026-11-10T00:59:00Z');api=NativeFactApi(clock)
        port=TqKqDsvPort(api,api._account,self.grant,clock);port.configure_calendar(self.calendar)
        self.assertEqual(port.tick_records('DCE.p2701'),[])
        clock.advance(420000);api.tick(last=1055)
        feed=DsvFactFeed(port,self.profile,self.calendar)
        self.assertFalse(feed.batches()[0][1][0]['path_complete'])


class DsvPlanBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.OnlineDsvTests();self.case.setUp()
    def tearDown(self):self.case.tearDown()

    def test_plan_requires_exact_typed_scope_permit_and_original_hash(self):
        x=self.case;x.start();x.quote();plan=x.cycle()['plan'];permit=x.kernel.simulation.permit(plan)
        for invalid in (None,True):
            with self.subTest(invalid=invalid),self.assertRaises(TraderError):initial_state(plan,simulation_permit=invalid)
        changed=copy.deepcopy(plan);changed['fixed_stop_loss']['distance_points']='17'
        with self.assertRaises(TraderError):initial_state(changed,simulation_permit=permit)
        with self.assertRaises(TraderError):DsvSimulationPlanPermit.issue(plan,grant_for(Path(x.temp.name)/'manual/ledger.db'))

    def test_restored_scope_identity_cannot_silently_change_its_contents(self):
        x=self.case;x.entered();x.session.close();x.kernel.close()
        changed=DsvSimulationGrant.parse(canonical_bytes(x.grant.value|{'opening_attempts':21}))
        port=TqKqDsvPort(x.api,x.api._account,changed,x.clock);port.configure_calendar(x.calendar)
        with self.assertRaises(TraderError):Kernel(x.path,changed.account_key,port=port,simulation_grant=changed,risk_budget='1000000')

    def test_entry_intent_waits_for_matching_ack_before_any_native_call(self):
        x=self.case;x.start();x.clock.advance(1000);x.api.tick(last=1055)
        event=x.session.feed.batches()[0][1][-1];account,instrument=x.session.feed.facts(event,x.kernel)
        future=x.session.driver.on_market(event,account,instrument)
        pending=x.kernel._pending[0];ack=pending.future.result(timeout=3);held=Future();pending.future=held
        x.kernel.poll();self.assertEqual(x.api.inserted,[]);self.assertFalse(future.done())
        held.set_result(ack);x.kernel.wait_idle();self.assertEqual(len(x.api.inserted),1)

    def test_contract_quantity_cap_refuses_instead_of_silently_clamping_dsv_size(self):
        x=self.case;x.start(grant_changes={'max_order_quantity':2});x.quote()
        self.assertEqual(x.api.inserted,[]);self.assertEqual(x.kernel._staged,{})
        self.assertIn('PLAN_APPROVAL_REQUIRED',x.session.last_decisions['DCE.p2701']['reasons'])
