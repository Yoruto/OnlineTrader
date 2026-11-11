"""Owner execution effects use durable intent and explicit anonymous facts."""

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.dsv_strategy import DsvStrategyRuntime
from online_trader.kernel.strategy import StrategyCoordinator
from online_trader.strategy.plan import build_development_plan
from qa.recording_port import RecordingPort
from test_dsv_runtime import facts


class DsvExecutionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'ledger.db'
        self.account='DEV:execution-'+uuid.uuid4().hex
        self.port=RecordingPort()
        self.kernel=self.new_kernel(self.port)

    def new_kernel(self,port):
        return Kernel(self.path,self.account,development=True,port=port,
                      risk_budget='1000000',tick_size='1',multiplier='10')

    def tearDown(self):
        try:self.kernel.close()
        except TraderError:
            if not self.kernel._failed:raise
        finally:self.temp.cleanup()

    def cycle(self):return self.kernel.snapshot()['cycles'][0]

    def child(self,identity):
        return next(o for o in self.cycle()['orders'] if o['child_order_id']==identity)

    def create_cycle(self,quantity=3,*,fill=0,direction='LONG',execution_version='1.1'):
        settings=load_confirmed_baseline()[0].to_settings()
        settings['trade'].update(half_close_enabled=False,half_stage2=False,half_breakeven=False,
                                 protect_80_enabled=False,trailing_activate_ratio=0)
        profile=DsvProfile.from_settings(settings)
        candidate={'account_key':self.account,'contract_id':'DCE.p2701','trading_day':'2026-11-09',
            'profile_hash':profile.content_hash,'direction':direction,'theoretical_price':'1000',
            'limit_price':'1000','open_offset':'55','stop_distance':'16','take_profit_distance':'50'}
        plan=build_development_plan(profile,candidate,account_key=self.account,trading_day='2026-11-09',
            config_revision=str(self.kernel._config_revision),config_hash=self.kernel._configuration['payload_hash'],
            rule_set_id=self.kernel._configuration['rule_set_id'],quantity=quantity,
            plan_id='execution-plan-'+uuid.uuid4().hex,position_cycle_id='execution-cycle-'+uuid.uuid4().hex,
            execution_version=execution_version)
        self.kernel.arm_development();self.kernel.wait_idle()
        context=self.kernel.issue_entry_context('qa',plan['contract_id'])
        child=self.kernel.admit_entry(plan,'execution-decision-'+uuid.uuid4().hex,'1000',
                                     context_id=context['context_id'])
        self.kernel.wait_idle()
        if execution_version=='1.2':
            self.kernel.observe(plan['position_cycle_id'],'1000',bid1='999',ask1='1000');self.kernel.wait_idle()
        if fill:
            self.kernel.record_fill(child,'entry-fill-'+uuid.uuid4().hex,fill,'1000');self.kernel.wait_idle()
        return child,plan

    def event(self,elapsed_ms=0,*,last='1000',bid='999',ask='1000',entry_context=True,**updates):
        sequence=getattr(self,'_clock_sequence',0)+1;self._clock_sequence=sequence
        event={'event_id':'execution-event-'+uuid.uuid4().hex,'source_epoch':1,'sequence':sequence,
            'elapsed_ms':elapsed_ms,'trading_day':'2026-11-09',
            'market':{'complete':True,'fresh':True,'is_trading':True,'quality':'CONTIGUOUS',
                'last':last,'bid1':bid,'ask1':ask,'tick_size':'1','lower_limit':'900','upper_limit':'1100',
                'since_open_ms':300000,'near_open':False}}
        if entry_context:
            context=self.kernel.issue_entry_context('qa',self.cycle()['plan']['contract_id'])
            event.update(context_id=context['context_id'],peer_id='qa')
        event.update(updates)
        return event

    def tick(self,event,*,wait=True):
        result=self.kernel.execution_tick(self.cycle()['position_cycle_id'],event)
        if wait:self.kernel.wait_idle()
        return result

    def start_reprice(self,*,fill=0):
        child,plan=self.create_cycle(fill=fill,execution_version='1.2')
        event=self.event(last='1001',bid='1000',ask='1001')
        self.tick(event)
        self.assertEqual(len(self.port.cancellations),1)
        return child,plan,event

    def create_exit(self,quantity=3):
        child,plan=self.create_cycle(quantity,fill=quantity,execution_version='1.2')
        self.kernel.reconcile_development_cycle(plan['position_cycle_id']);self.kernel.wait_idle()
        self.kernel.observe(plan['position_cycle_id'],'980',bid1='979',ask1='981');self.kernel.wait_idle()
        old=next(o for o in self.port.orders if o['effect']=='REDUCE')['child_order_id']
        return old,plan

    def create_strategy_cycle(self):
        settings=load_confirmed_baseline()[0].to_settings()
        settings['trade'].update(half_close_enabled=False,half_stage2=False,half_breakeven=False,
                                 protect_80_enabled=False,trailing_activate_ratio=0,no_chase_after_act=False)
        next(row for row in settings['symbols'] if row['code']=='DCE.p2701')['order_volume']=3
        runtime=DsvStrategyRuntime(self.kernel,profile=DsvProfile.from_settings(settings))
        runtime.activate();self.kernel.arm_development();self.kernel.wait_idle()
        market,account,instrument=facts(self.account)
        account.update(equity='400000',available='300000')
        future=runtime.on_market(market,account,instrument);self.kernel.wait_idle()
        child=future.result()['child_order_id']
        self.assertIsNotNone(child)
        self.assertEqual(self.cycle()['plan']['schema_version'],'1.2')
        market=market|{'sequence':2,'source_time_utc':'2026-11-09T01:05:01Z',
                       'last':'1056','bid1':'1055','ask1':'1057'}
        runtime.update_facts(market,account,instrument)
        self.tick(self.event(last='1056',bid='1055',ask='1057',entry_context=False))
        self.assertEqual(len(self.port.cancellations),1)
        self.kernel.confirm_terminal(child,'CANCELLED',0);self.kernel.wait_idle()
        return runtime,child,market,account,instrument

    def hold_replacement(self,old,event):
        submit=self.kernel.ledger.submit;held=[]
        def delayed(kind,name,payload,mutate=None,**kwargs):
            actual=submit(kind,name,payload,mutate,**kwargs)
            if kind=='TX_INTENT' and any(o.get('parent_child_order_id')==old
                                         for o in payload.get('cycle',{}).get('orders',[])):
                visible=Future();held.append((actual,visible));return visible
            return actual
        self.kernel.ledger.submit=delayed
        self.tick(event,wait=False)
        self.kernel._pending[0].future.result(timeout=3)
        self.kernel.poll()
        self.assertTrue(held,'The real replacement intent must exist before final-gate changes')
        return held[0]

    def test_final_send_rechecks_new_price_after_the_replacement_intent_has_committed(self):
        old,_,_=self.start_reprice()
        self.kernel.confirm_terminal(old,'CANCELLED',0);self.kernel.wait_idle()
        actual,visible=self.hold_replacement(old,self.event(1000,last='1001',bid='1000',ask='1001'))
        try:
            self.kernel.observe(self.cycle()['position_cycle_id'],'1002',bid1='1001',ask1='1002')
        finally:visible.set_result(actual.result(timeout=3))
        self.kernel.wait_idle()
        self.assertEqual(len(self.port.orders),1)
        self.assertEqual(self.cycle()['orders'][-1]['send_state'],'DEFINITELY_NOT_SENT')

    def test_strategy_replacement_rechecks_account_and_contract_specification_after_intent_ack(self):
        for changed in ('funds','multiplier'):
            with self.subTest(changed=changed):
                self.kernel.close();self.path=Path(self.temp.name)/(changed+'.db')
                self.port=RecordingPort();self.kernel=self.new_kernel(self.port)
                runtime,old,market,account,instrument=self.create_strategy_cycle()
                actual,visible=self.hold_replacement(old,self.event(1000,last='1056',bid='1055',ask='1057',entry_context=False))
                try:
                    account=account|{'revision':2}
                    if changed=='funds':account['available']='0'
                    else:instrument=instrument|{'multiplier':'20'}
                    runtime.update_facts(market,account,instrument)
                finally:visible.set_result(actual.result(timeout=3))
                self.kernel.wait_idle()
                self.assertEqual(len(self.port.orders),1)
                self.assertEqual(self.cycle()['orders'][-1]['send_state'],'DEFINITELY_NOT_SENT')

    def test_replacement_from_the_previous_strategy_epoch_is_not_revived_by_rearming(self):
        _,_,_,_,_=self.create_strategy_cycle()
        coordinator=StrategyCoordinator(self.kernel)
        _,digest=coordinator.checkpoint()
        new_epoch=coordinator.register_shadow('execution-replacement-'+uuid.uuid4().hex,digest)
        barrier,digest=coordinator.checkpoint()
        coordinator.switch(new_epoch,expected_old_epoch=self.kernel.active_epoch,barrier_seq=barrier,state_hash=digest)
        self.kernel.wait_idle();self.kernel.arm_development();self.kernel.wait_idle()
        self.tick(self.event(1000,last='1056',bid='1055',ask='1057',entry_context=False))
        self.assertEqual(len(self.port.orders),1)

    def test_cross_day_execution_fact_is_refused_before_any_effect(self):
        self.create_cycle(execution_version='1.2')
        with self.assertRaises(TraderError) as raised:
            self.tick(self.event(last='1001',bid='1000',ask='1001',trading_day='2026-11-10'))
        self.assertEqual(raised.exception.code,'E_SCOPE')
        self.assertEqual(self.port.cancellations,[])
        self.assertEqual(len(self.port.orders),1)

    def test_reprice_waits_for_terminal_and_partial_fill_details_then_reuses_only_the_remaining_target(self):
        old,_,_=self.start_reprice()
        self.assertFalse(self.child(old)['terminal_confirmed'])
        self.tick(self.event(1000,last='1001',bid='1000',ask='1001'))
        self.assertEqual(len(self.port.orders),1)
        self.assertEqual(len(self.port.cancellations),1)
        self.kernel.record_fill(old,'entry-partial-while-cancelling',1,'1000');self.kernel.wait_idle()
        with self.assertRaises(TraderError):self.kernel.confirm_terminal(old,'CANCELLED',0)
        self.assertEqual(len(self.port.orders),1)
        self.kernel.confirm_terminal(old,'CANCELLED',1);self.kernel.wait_idle()
        self.assertEqual(len(self.port.orders),1)
        self.kernel.arm_development();self.kernel.wait_idle()
        self.tick(self.event(2000,last='1001',bid='1000',ask='1001'))
        self.assertEqual(len(self.port.orders),2)
        replacement=self.port.orders[-1]
        self.assertEqual(replacement['original_qty'],2)
        self.assertEqual(replacement['limit_price'],'1001')
        self.assertEqual(replacement['parent_child_order_id'],old)
        self.assertEqual(replacement['intent_id'],self.child(old)['intent_id'])
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM order_intents WHERE effect='OPEN'")),1)

    def test_repeated_tick_identity_cannot_repeat_cancel_or_advance_a_replacement(self):
        old,_,event=self.start_reprice()
        self.tick(event)
        self.assertEqual(len(self.port.cancellations),1)
        self.kernel.confirm_terminal(old,'CANCELLED',0);self.kernel.wait_idle()
        event=self.event(1000,last='1001',bid='1000',ask='1001')
        self.tick(event)
        self.assertEqual(len(self.port.orders),2)
        self.tick(event)
        self.assertEqual(len(self.port.orders),2)
        self.assertEqual(len(self.port.cancellations),1)

    def test_entry_timeout_is_strict_and_a_partial_cycle_is_not_timed_out_as_empty(self):
        old,plan=self.create_cycle(execution_version='1.2')
        self.tick(self.event(0))
        self.tick(self.event(plan['execution_policy']['entry_confirm_timeout_ms']))
        self.assertEqual(self.port.cancellations,[])
        self.tick(self.event(plan['execution_policy']['entry_confirm_timeout_ms']+1))
        self.assertEqual(len(self.port.cancellations),1)
        self.kernel.confirm_terminal(old,'CANCELLED',0);self.kernel.wait_idle()
        self.tick(self.event(70000))
        self.assertEqual(len(self.port.orders),1)
        # The next isolated file has actual partial fills; its remaining order
        # is not an empty pending entry merely because sixty seconds passed.
        self.kernel.close();self.path=Path(self.temp.name)/'partial-timeout.db'
        self.port=RecordingPort();self.kernel=self.new_kernel(self.port)
        self.create_cycle(fill=1,execution_version='1.2')
        self.tick(self.event(0));self.tick(self.event(60001))
        self.assertEqual(self.port.cancellations,[])
        self.assertEqual(len(self.port.orders),1)

    def test_rejected_or_unrelated_manual_cancellation_does_not_authorize_automatic_open_retry(self):
        for status in ('REJECTED','CANCELLED'):
            with self.subTest(status=status):
                self.kernel.close();self.path=Path(self.temp.name)/(status+'.db')
                self.port=RecordingPort();self.kernel=self.new_kernel(self.port)
                old,_=self.create_cycle(execution_version='1.2')
                self.tick(self.event())
                if status=='CANCELLED':
                    self.kernel.request_cancel(old);self.kernel.wait_idle()
                self.kernel.confirm_terminal(old,status,0);self.kernel.wait_idle()
                self.tick(self.event(1000,last='1001',bid='1000',ask='1001'))
                self.assertEqual(len(self.port.orders),1)

    def test_cancel_unknown_and_unknown_send_do_not_create_another_opening_attempt(self):
        old,_=self.create_cycle(execution_version='1.2')
        def ambiguous(request):
            self.port.cancellations.append(dict(request));raise RuntimeError('Unknown cancel')
        self.port.cancel=ambiguous
        self.tick(self.event(last='1001',bid='1000',ask='1001'))
        self.tick(self.event(1000,last='1002',bid='1001',ask='1002'))
        self.assertEqual(self.child(old)['cancel_status'],'UNKNOWN')
        self.assertEqual(OrderState.from_dict(self.child(old)).potential_remaining,3)
        self.assertEqual(len(self.port.cancellations),1)
        self.assertEqual(len(self.port.orders),1)
        self.kernel.close();self.path=Path(self.temp.name)/'unknown-send.db'
        self.port=RecordingPort();self.port.fail_send=True;self.kernel=self.new_kernel(self.port)
        old,_=self.create_cycle(execution_version='1.2')
        self.assertEqual(self.child(old)['send_state'],'UNKNOWN')
        self.tick(self.event(last='1001',bid='1000',ask='1001'))
        self.assertEqual(len(self.port.orders),1)
        self.assertEqual(self.port.cancellations,[])

    def test_pause_while_replacement_transaction_is_pending_prevents_any_new_open_send(self):
        old,_,_=self.start_reprice()
        self.kernel.confirm_terminal(old,'CANCELLED',0);self.kernel.wait_idle()
        self.tick(self.event(1000,last='1001',bid='1000',ask='1001'),wait=False)
        self.kernel.pause();self.kernel.wait_idle()
        self.assertEqual(len(self.port.orders),1)
        self.assertFalse(self.kernel.snapshot()['armed'])

    def test_close_window_uses_the_same_exit_intent_and_target_through_active_extra_tick_and_limit(self):
        old,_=self.create_exit()
        self.tick(self.event(last='980',bid='979',ask='981',close_due=True,entry_context=False))
        self.tick(self.event(10000,last='980',bid='979',ask='981',close_due=True,entry_context=False))
        self.assertEqual(len(self.port.cancellations),1)
        self.kernel.confirm_terminal(old,'CANCELLED',0);self.kernel.wait_idle()
        self.tick(self.event(10001,last='980',bid='979',ask='981',close_due=True,entry_context=False))
        second=[o for o in self.port.orders if o['effect']=='REDUCE'][-1]
        self.assertEqual(second['limit_price'],'978')
        self.assertEqual(second['intent_id'],self.child(old)['intent_id'])
        self.tick(self.event(15000,last='980',bid='979',ask='981',close_due=True,entry_context=False))
        self.kernel.confirm_terminal(second['child_order_id'],'CANCELLED',0);self.kernel.wait_idle()
        self.tick(self.event(15001,last='980',bid='979',ask='981',close_due=True,entry_context=False))
        reductions=[o for o in self.port.orders if o['effect']=='REDUCE']
        self.assertEqual([o['limit_price'] for o in reductions],['979','978','900'])
        self.assertTrue(all(o['original_qty']==3 for o in reductions))
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'],0)
        self.assertEqual(self.cycle()['protection']['exit_filled_qty'],0)

    def test_late_exit_after_inconsistent_terminal_is_preserved_and_stops_the_replacement_chain(self):
        old,_=self.create_exit()
        self.tick(self.event(last='980',bid='978',ask='980',entry_context=False))
        self.kernel.confirm_terminal(old,'CANCELLED',0);self.kernel.wait_idle()
        self.tick(self.event(1000,last='980',bid='978',ask='980',entry_context=False))
        replacement=[o for o in self.port.orders if o['effect']=='REDUCE'][-1]
        self.assertNotEqual(replacement['child_order_id'],old)
        self.kernel.record_fill(old,'contradictory-old-exit-fill',1,'979');self.kernel.wait_idle()
        self.assertEqual(self.cycle()['protection']['owned_qty'],2)
        self.assertTrue(self.kernel.snapshot()['recovery_required'])
        self.assertIsNotNone(self.child(replacement['child_order_id'])['cancel_status'])
        self.assertEqual(len([o for o in self.port.orders if o['effect']=='REDUCE']),2)
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM fill_facts WHERE trade_key='contradictory-old-exit-fill'")),1)

    def test_unknown_execution_state_survives_restart_without_replaying_send_or_cancel(self):
        old,_=self.create_cycle(execution_version='1.2')
        def ambiguous(request):
            self.port.cancellations.append(dict(request));raise RuntimeError('Unknown cancel')
        self.port.cancel=ambiguous
        self.tick(self.event(last='1001',bid='1000',ask='1001'))
        original_plan=self.cycle()['plan']
        self.kernel.close();self.port=RecordingPort();self.kernel=self.new_kernel(self.port)
        self.assertEqual(self.cycle()['plan'],original_plan)
        self.assertEqual(self.child(old)['cancel_status'],'UNKNOWN')
        self.assertGreater(OrderState.from_dict(self.child(old)).potential_remaining,0)
        try:self.tick(self.event(1000,last='1002',bid='1001',ask='1002'))
        except TraderError as error:self.assertIn(error.code,{'E_NOT_READY','E_FACT_BARRIER','E_EPOCH_REVOKED'})
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.port.cancellations,[])
        self.assertFalse(self.kernel.snapshot()['armed'])

    def test_replacement_rollback_or_wrong_ack_never_calls_the_sender(self):
        for mode in ('rollback','wrong-ack'):
            with self.subTest(mode=mode):
                try:self.kernel.close()
                except TraderError:
                    if not self.kernel._failed:raise
                self.path=Path(self.temp.name)/('replacement-'+mode+'.db')
                self.port=RecordingPort();self.kernel=self.new_kernel(self.port)
                old,_,_=self.start_reprice()
                self.kernel.confirm_terminal(old,'CANCELLED',0);self.kernel.wait_idle()
                submit=self.kernel.ledger.submit
                def inject(kind,event,payload,mutate=None,**kwargs):
                    new_child=any(o.get('parent_child_order_id')==old for o in payload.get('cycle',{}).get('orders',[]))
                    if kind=='TX_INTENT' and new_child:
                        if mode=='rollback':
                            def rollback(conn,seq):
                                mutate(conn,seq)
                                raise sqlite3.OperationalError('Injected replacement intent rollback')
                            return submit(kind,event,payload,rollback,**kwargs)
                        actual=submit(kind,event,payload,mutate,**kwargs)
                        ack=actual.result(timeout=3);wrong=Future()
                        wrong.set_result(replace(ack,kernel_boot_id='former-boot'))
                        return wrong
                    return submit(kind,event,payload,mutate,**kwargs)
                self.kernel.ledger.submit=inject
                self.tick(self.event(1000,last='1001',bid='1000',ask='1001'),wait=False)
                with self.assertRaises(TraderError):self.kernel.wait_idle()
                self.assertEqual(len(self.port.orders),1)
                self.assertTrue(self.kernel.snapshot()['storage_failed'])

    def test_cancel_is_committed_before_call_return_never_releases_remaining_quantity(self):
        child,_=self.create_cycle()
        self.kernel.request_cancel(child)
        self.kernel._pending[0].future.result(timeout=3)
        self.assertEqual(self.port.cancellations,[])
        self.assertEqual(self.kernel.ledger.query('SELECT status FROM cancel_actions'),[{'status':'SEND_POSSIBLE'}])
        self.kernel.wait_idle()
        order=self.child(child)
        self.assertEqual(order['cancel_status'],'CALL_RETURNED')
        self.assertFalse(order['terminal_confirmed'])
        self.assertEqual(OrderState.from_dict(order).potential_remaining,3)
        self.assertEqual(len(self.port.cancellations),1)
        self.kernel.request_cancel(child);self.kernel.wait_idle()
        self.assertEqual(len(self.port.cancellations),1)

    def test_cancel_unknown_keeps_the_old_order_reserved_without_another_cancel_call(self):
        child,_=self.create_cycle()
        def ambiguous(request):
            self.port.cancellations.append(dict(request))
            raise RuntimeError('Explicit anonymous cancellation result is unknown')
        self.port.cancel=ambiguous
        self.kernel.request_cancel(child);self.kernel.wait_idle()
        order=self.child(child)
        self.assertEqual(order['cancel_status'],'UNKNOWN')
        self.assertFalse(order['terminal_confirmed'])
        self.assertEqual(OrderState.from_dict(order).potential_remaining,3)
        self.kernel.request_cancel(child);self.kernel.wait_idle()
        self.assertEqual(len(self.port.cancellations),1)
        self.assertEqual(len(self.port.orders),1)

    def test_fill_while_cancel_ack_is_pending_preserves_the_remainder_until_terminal_evidence(self):
        child,_=self.create_cycle()
        self.kernel.request_cancel(child)
        self.kernel.record_fill(child,'fill-during-cancel',1,'1000')
        self.kernel.wait_idle()
        order=self.child(child)
        self.assertEqual(order['filled_qty'],1)
        self.assertEqual(OrderState.from_dict(order).potential_remaining,2)
        self.assertFalse(order['terminal_confirmed'])
        with self.assertRaises(TraderError) as raised:self.kernel.confirm_terminal(child,'CANCELLED',0)
        self.assertEqual(raised.exception.code,'E_FACT_BARRIER')
        self.assertEqual(OrderState.from_dict(self.child(child)).potential_remaining,2)
        self.kernel.confirm_terminal(child,'CANCELLED',1);self.kernel.wait_idle()
        self.assertEqual(OrderState.from_dict(self.child(child)).potential_remaining,0)
        self.assertEqual(len(self.port.orders),1)

    def test_cancel_rollback_or_wrong_ack_cannot_call_the_port(self):
        for mode in ('rollback','wrong-ack'):
            with self.subTest(mode=mode):
                try:self.kernel.close()
                except TraderError:
                    if not self.kernel._failed:raise
                self.path=Path(self.temp.name)/(mode+'.db')
                self.port=RecordingPort();self.kernel=self.new_kernel(self.port)
                child,_=self.create_cycle()
                if mode=='rollback':
                    submit=self.kernel.ledger.submit
                    def fail(kind,event,payload,mutate=None,**kwargs):
                        if event=='CANCEL_PREPARED':
                            def rollback(conn,seq):
                                mutate(conn,seq)
                                raise sqlite3.OperationalError('Injected cancel intent rollback')
                            return submit(kind,event,payload,rollback,**kwargs)
                        return submit(kind,event,payload,mutate,**kwargs)
                    self.kernel.ledger.submit=fail
                self.kernel.request_cancel(child)
                if mode=='wrong-ack':
                    pending=self.kernel._pending[0];ack=pending.future.result(timeout=3)
                    pending.future=Future();pending.future.set_result(replace(ack,kernel_boot_id='former-boot'))
                with self.assertRaises(TraderError):self.kernel.wait_idle()
                self.assertEqual(self.port.cancellations,[])
                self.assertEqual(len(self.port.orders),1)
                self.assertTrue(self.kernel.snapshot()['storage_failed'])
                if mode=='rollback':self.assertEqual(self.kernel.ledger.query('SELECT * FROM cancel_actions'),[])


if __name__=='__main__':unittest.main()
