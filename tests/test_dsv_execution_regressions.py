"""Owner regressions found while reviewing the DSV execution chain."""

import copy
from concurrent.futures import Future
import unittest
from unittest.mock import patch

import test_dsv_execution_runtime as fixtures
import test_dsv_runtime as strategy_fixtures
from online_trader.errors import TraderError
from online_trader.domain.orders import OrderState
from online_trader.kernel.dsv_strategy import DsvStrategyRuntime
from online_trader.kernel.strategy import StrategyCoordinator
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile


class DsvExecutionReviewTests(unittest.TestCase):
    def setUp(self):
        self.owner=fixtures.DsvExecutionRuntimeTests()
        self.owner.setUp()
        self.addCleanup(self.owner.tearDown)

    def test_near_open_uses_active_price_without_the_ordinary_cap(self):
        owner=self.owner
        owner.create_cycle(execution_version='1.2')
        event=owner.event(last='1004',bid='1003',ask='1004')
        event['market'].update(since_open_ms=1000,near_open=True)
        owner.tick(event)
        cycle=owner.cycle()
        self.assertEqual(cycle['execution']['contexts']['OPEN']['mode'],'ACTIVE')
        self.assertEqual(owner.kernel.execution.price(cycle,'OPEN')['price'],'1004')

    def test_formal_passive_entry_waits_three_seconds_then_caps_and_replaces_after_terminal(self):
        owner=self.owner;settings=load_confirmed_baseline()[0].to_settings()
        settings['trade']['passive_entry_first']=True
        runtime=DsvStrategyRuntime(owner.kernel,profile=DsvProfile.from_settings(settings))
        runtime.activate();owner.kernel.arm_development();owner.kernel.wait_idle()
        market,account,instrument=strategy_fixtures.facts(owner.account)
        def observe(sequence,seconds):
            event=market|{'sequence':sequence,'source_time_utc':f'2026-11-09T01:05:0{seconds}Z'}
            f=runtime.on_market(event,account,instrument);owner.kernel.wait_idle();return f.result()
        observe(1,0)
        first=owner.port.orders[0]
        self.assertEqual(first['limit_price'],'1055')
        observe(2,2)
        self.assertEqual(owner.port.cancellations,[])
        observe(3,3)
        self.assertEqual(len(owner.port.cancellations),1)
        self.assertEqual(len(owner.port.orders),1)
        owner.kernel.confirm_terminal(first['child_order_id'],'CANCELLED',0);owner.kernel.wait_idle()
        observe(4,4)
        self.assertEqual([o['limit_price'] for o in owner.port.orders],['1055','1056'])
        self.assertEqual(owner.port.orders[-1]['parent_child_order_id'],first['child_order_id'])

    def test_initial_active_context_does_not_gain_an_ordinary_cap_after_open_window(self):
        owner=self.owner
        owner.create_cycle(execution_version='1.2')
        first=owner.event(last='1004',bid='1003',ask='1004')
        first['market'].update(since_open_ms=1000,near_open=True)
        owner.tick(first)
        follow=owner.event(61_000,last='1004',bid='1003',ask='1004')
        follow['market'].update(since_open_ms=62_000,near_open=False)
        owner.tick(follow)
        cycle=owner.cycle()
        self.assertEqual(cycle['execution']['contexts']['OPEN']['mode'],'ACTIVE')
        self.assertEqual(owner.kernel.execution.price(cycle,'OPEN')['price'],'1004')

    def test_delayed_first_execution_tick_cannot_restart_the_original_cap_ttl(self):
        owner=self.owner
        owner.create_cycle(quantity=3,fill=1,execution_version='1.2')
        owner.tick(owner.event(90001,last='1004',bid='1003',ask='1004'))
        cycle=owner.cycle()
        self.assertEqual(cycle['execution']['context_ms']['OPEN'],0)
        self.assertEqual(owner.kernel.execution.price(cycle,'OPEN')['mode'],'ACTIVE')
        self.assertEqual(owner.kernel.execution.price(cycle,'OPEN')['price'],'1004')

    def test_rejection_of_an_older_reduction_chain_is_not_bypassed_by_new_exit_intent(self):
        owner=self.owner
        _,plan=owner.create_cycle(quantity=5,fill=5,execution_version='1.2')
        owner.tick(owner.event(entry_context=False))
        cid=plan['position_cycle_id']
        owner.kernel.manual_close(cid,2);owner.kernel.wait_idle()
        first=next(o for o in owner.port.orders if o['effect']=='REDUCE')
        owner.kernel.manual_close(cid,4);owner.kernel.wait_idle()
        self.assertEqual([o['original_qty'] for o in owner.port.orders if o['effect']=='REDUCE'],[2,2])
        owner.kernel.confirm_terminal(first['child_order_id'],'REJECTED',0)
        owner.kernel.wait_idle()
        self.assertEqual(len([o for o in owner.port.orders if o['effect']=='REDUCE']),2)
        owner.tick(owner.event(1000,entry_context=False))
        self.assertEqual(len([o for o in owner.port.orders if o['effect']=='REDUCE']),2)

    def test_execution_tick_cannot_replace_the_original_instrument_tick(self):
        owner=self.owner
        owner.create_cycle(quantity=3,fill=3,execution_version='1.2')
        event=owner.event(entry_context=False)
        event['market']['tick_size']='0.5'
        before=owner.kernel.ledger.query('SELECT COUNT(*) AS n FROM order_intents')[0]['n']
        with self.assertRaises(TraderError) as raised:owner.tick(event)
        self.assertEqual(raised.exception.code,'E_SCOPE')
        self.assertEqual(owner.kernel.ledger.query('SELECT COUNT(*) AS n FROM order_intents')[0]['n'],before)
        self.assertEqual(len(owner.port.orders),1)
        self.assertNotIn('execution',owner.cycle())

    def test_definitely_not_sent_exit_waits_for_a_new_explicit_fact_instead_of_retrying_its_own_abort(self):
        owner=self.owner
        _,plan=owner.create_cycle(quantity=3,fill=3,execution_version='1.2')
        cid=plan['position_cycle_id'];submit=owner.kernel.ledger.submit;held=[]
        def hold_exit(kind,event,payload,mutate=None,**kwargs):
            actual=submit(kind,event,payload,mutate,**kwargs)
            if event=='EXIT_PREPARED' and not held:
                delayed=Future();held.append((delayed,actual));return delayed
            return actual
        owner.kernel.ledger.submit=hold_exit
        try:
            owner.tick(owner.event(last='980',bid='979',ask='981',entry_context=False),wait=False)
            owner.kernel._pending[0].future.result(timeout=3)
            owner.kernel._poll_commits()
            self.assertEqual(len(held),1)
            owner.kernel.observe(cid,'980',bid1='978',ask1='981')
            held[0][0].set_result(held[0][1].result(timeout=3))
            owner.kernel.wait_idle()
            reductions=[o for o in owner.cycle()['orders'] if o['effect']=='REDUCE']
            self.assertEqual(len(reductions),1)
            self.assertEqual(reductions[0]['send_state'],'DEFINITELY_NOT_SENT')
            self.assertEqual(len(owner.port.orders),1)
            owner.tick(owner.event(1000,last='980',bid='978',ask='981',entry_context=False))
            self.assertEqual(len([o for o in owner.port.orders if o['effect']=='REDUCE']),1)
        finally:
            owner.kernel.ledger.submit=submit
            for delayed,actual in held:
                if not delayed.done():delayed.set_result(actual.result(timeout=3))

    def test_execution_clock_or_context_alone_changes_the_handoff_hash(self):
        owner=self.owner
        _,plan=owner.create_cycle(execution_version='1.2')
        owner.tick(owner.event())
        coordinator=StrategyCoordinator(owner.kernel)
        before=coordinator.checkpoint()
        cid=plan['position_cycle_id'];original=copy.deepcopy(owner.kernel._committed[cid])
        # Isolate the hash contract from normal revision/order/protection changes.
        # These temporary snapshots are never persisted or used for dispatch.
        for field in ('clock','context'):
            changed=copy.deepcopy(original)
            if field=='clock':changed['execution']['elapsed_ms']+=1
            else:changed['execution']['contexts']['OPEN']['row']['cap']='1001'
            with patch.dict(owner.kernel._committed,{cid:changed}):
                actual=coordinator.checkpoint()
            self.assertEqual(actual[0],before[0])
            self.assertNotEqual(actual[1],before[1])

    def test_first_formal_entry_applies_the_source_cap_after_the_favorable_last_exemption(self):
        owner=self.owner
        runtime=DsvStrategyRuntime(owner.kernel)
        market,account,instrument=strategy_fixtures.facts(owner.account)
        market.update(last='1055',ask1='1060',bid1='1054')
        runtime.activate();owner.kernel.arm_development();owner.kernel.wait_idle()
        future=runtime.on_market(market,account,instrument);owner.kernel.wait_idle()
        self.assertEqual(future.result()['status'],'ENTRY_COMMITTED')
        self.assertEqual(len(owner.port.orders),1)
        # LAST at A passes the source's double-price chase exemption; the
        # callback must still cap ASK at A+round(55*0.03)=1057 on the first send.
        self.assertEqual(owner.port.orders[0]['limit_price'],'1057')

    def test_cancel_terminal_wait_at_thirty_seconds_requires_reconciliation_without_releasing_quantity(self):
        owner=self.owner
        old,_,_=owner.start_reprice()
        owner.tick(owner.event(29_999,last='1001',bid='1000',ask='1001'))
        self.assertFalse(owner.kernel.snapshot()['recovery_required'])
        self.assertEqual(OrderState.from_dict(owner.child(old)).potential_remaining,3)
        self.assertFalse(owner.child(old)['terminal_confirmed'])
        owner.tick(owner.event(30_000,last='1001',bid='1000',ask='1001'))
        cycle=owner.cycle()
        self.assertTrue(owner.kernel.snapshot()['recovery_required'])
        self.assertFalse(owner.kernel.snapshot()['armed'])
        self.assertEqual(cycle['execution']['last_action'],'WAIT_RECONCILIATION')
        self.assertEqual(cycle['execution']['last_reasons'],['CANCEL_TERMINAL_TIMEOUT'])
        self.assertEqual(OrderState.from_dict(owner.child(old)).potential_remaining,3)
        self.assertFalse(owner.child(old)['terminal_confirmed'])
        self.assertEqual(len(owner.port.orders),1)
        self.assertEqual(len(owner.port.cancellations),1)


if __name__=='__main__':unittest.main()
