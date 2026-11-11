"""Statistics stay behind real owner barriers at clock and execution edges."""

import copy
from concurrent.futures import Future
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError
from online_trader.scheduling.calendar import TradingCalendar
from qa.recording_port import RecordingPort
import test_dsv_execution_runtime as execution_fixtures
import test_scheduled_runtime as scheduled_fixtures
import test_statistics_runtime as statistics_fixtures


CONTRACT=statistics_fixtures.CONTRACT


class StatisticsEdgeTests(unittest.TestCase):
    def setUp(self):
        self.env=statistics_fixtures.StatisticsRuntimeTests();self.env.setUp()

    def tearDown(self):self.env.tearDown()

    @staticmethod
    def stop_facts():
        return {'net_loss_points':'32','stop_streak':2,'continuation_stopped':False,
            'day_stopped':False,'day_stopped_reason':None,'stop_days':[],
            'month_stop':None,'trading_days':['2026-11-09']}

    def test_close_pending_matching_ack_cannot_be_reported_as_already_reconciled(self):
        t=self.env;t.start();t.open();t.exit_fill();hold,held=t._held_close()
        with hold:
            t.close(wait=False)
            with self.assertRaises(TraderError) as error:t.close(wait=False)
            self.assertEqual(error.exception.code,'E_FACT_BARRIER')
        self.assertEqual(t.field('stop_count'),0)
        held[0][0].set_result(held[0][1]);t.kernel.wait_idle()
        self.assertEqual(t.field('stop_count'),1)

    def test_confirmed_flat_waiting_for_close_reconciliation_blocks_next_trading_day(self):
        t=self.env;t.start(max_net_loss_stops=100);t.open();t.exit_fill()
        sent=len(t.port.orders);t.kernel.arm_development();t.kernel.wait_idle()
        report=t.observe(day='2026-11-10',at='2026-11-10T01:06:00Z')
        self.assertIsNone(report['child_order_id'],report)
        self.assertIn('STATISTICS_CLOSE_RECONCILIATION_REQUIRED',report['reasons'])
        self.assertEqual(len(t.port.orders),sent)
        self.assertEqual(t.field('stop_count',day='2026-11-09'),0)

    def test_late_open_after_no_fill_close_is_durable_but_requires_review_before_reaccounting(self):
        t=self.env;t.start();child=t.open(fill=0)
        t.kernel.confirm_terminal(child,'CANCELLED',0);t.kernel.wait_idle();t.close()
        before=copy.deepcopy(t.state())
        self.assertEqual(before['trade_count'],0)
        self.assertTrue(t.kernel.record_fill(child,'late-after-no-fill',1,'1055'));t.kernel.wait_idle()
        cycle=t.cycle()
        self.assertTrue(cycle['statistics_review_required'])
        self.assertTrue(t.kernel.recovery_required)
        self.assertEqual(cycle['protection']['owned_qty'],1)
        self.assertEqual(t.state(),before)
        rows=t.kernel.ledger.query("SELECT quantity,price FROM fill_facts WHERE trade_key='late-after-no-fill'")
        self.assertEqual(rows,[{'quantity':1,'price':'1055'}])
        with self.assertRaises(TraderError):t.kernel.arm_development()
        t.kernel.close();t.port=RecordingPort();t.kernel=t.new_kernel()
        self.assertTrue(t.cycle()['statistics_review_required'])
        self.assertTrue(t.kernel.recovery_required)
        self.assertEqual(t.state(),before)
        self.assertEqual(t.port.orders,[])

    def test_derived_statistics_failure_cannot_discard_a_confirmed_owned_fill(self):
        t=self.env;t.start();child=t.open(fill=0)
        before=copy.deepcopy(t.snap())
        with patch('online_trader.kernel.statistics.apply_event',
                side_effect=TraderError('E_QUEUE_OVERFLOW','Derived statistics receipt capacity is full')):
            self.assertTrue(t.kernel.record_fill(child,'fill-survives-statistics-overflow',1,'1055'))
            t.kernel.wait_idle()
        cycle=t.cycle()
        self.assertEqual(cycle['protection']['owned_qty'],1)
        self.assertEqual(cycle['protection']['entry_filled_qty'],1)
        self.assertTrue(cycle['statistics_review_required'])
        self.assertEqual(cycle['statistics_error'],'E_QUEUE_OVERFLOW')
        self.assertTrue(t.kernel.recovery_required)
        self.assertEqual(t.snap(),before)
        rows=t.kernel.ledger.query("SELECT quantity,price FROM fill_facts WHERE trade_key='fill-survives-statistics-overflow'")
        self.assertEqual(rows,[{'quantity':1,'price':'1055'}])
        with self.assertRaises(TraderError):t.kernel.arm_development()
        t.observe('1035',at=t.day+'T01:06:01Z')
        self.assertEqual(t.cycle()['protection']['target_remaining_qty'],0)
        self.assertTrue(t.port.cancellations)
        t.kernel.confirm_terminal(child,'CANCELLED',1);t.kernel.wait_idle()
        reductions=[o for o in t.port.orders if o['effect']=='REDUCE']
        self.assertEqual(len(reductions),1)
        self.assertEqual(reductions[0]['original_qty'],1)
        t.kernel.record_fill(reductions[0]['child_order_id'],'protected-after-statistics-overflow',1,'1034')
        t.kernel.wait_idle()
        self.assertEqual(t.cycle()['protection']['owned_qty'],0)
        with self.assertRaises(TraderError) as error:t.close()
        self.assertEqual(error.exception.code,'E_FACT_BARRIER')
        self.assertTrue(t.cycle()['statistics_review_required'])
        self.assertEqual(t.snap(),before)

    def test_initial_final_gate_rechecks_a_new_explicit_stop_before_sending(self):
        t=self.env;t.start();t.kernel.arm_development();t.kernel.wait_idle()
        event,account,instrument=statistics_fixtures.facts(t.key)
        submit=t.kernel.ledger.submit;held=[]
        def intercept(kind,name,payload,mutate=None,**kwargs):
            actual=submit(kind,name,payload,mutate,**kwargs)
            if kind=='TX_INTENT' and payload.get('cycle',{}).get('entry_origin')=='STRATEGY':
                actual.result(timeout=3);visible=Future();held.append((visible,actual.result()));return visible
            return actual
        with patch.object(t.kernel.ledger,'submit',side_effect=intercept):
            result=t.runtime.on_market(event,account,instrument)
        self.assertEqual(len(held),1)
        self.assertEqual(t.port.orders,[])
        account=account|{'revision':2,'strategy_stop_facts':self.stop_facts()}
        t.runtime.update_facts(event,account,instrument)
        held[0][0].set_result(held[0][1]);t.kernel.wait_idle()
        result.result()
        self.assertEqual(t.port.orders,[])
        cycle=next(iter(t.kernel._committed.values()))
        self.assertEqual(cycle['orders'][0]['send_state'],'DEFINITELY_NOT_SENT')

    def test_older_fill_ack_cannot_replace_a_newer_explicit_trading_calendar(self):
        t=self.env;t.start();child=t.open(fill=0)
        old_days=t.kernel.statistics._days(CONTRACT)
        submit=t.kernel.ledger.submit;held=[]
        def intercept(kind,name,payload,mutate=None,**kwargs):
            actual=submit(kind,name,payload,mutate,**kwargs)
            if kind=='TX_FILL':
                actual.result(timeout=3);visible=Future();held.append((visible,actual.result()));return visible
            return actual
        with patch.object(t.kernel.ledger,'submit',side_effect=intercept):
            self.assertTrue(t.kernel.record_fill(child,'entry-calendar-held-ack',1,'1055'))
        self.assertEqual(len(held),1)
        self.assertIsNone(t.snap())
        saved=t.runtime._latest[(CONTRACT,t.day)]
        account=copy.deepcopy(saved['account']);account['revision']+=1
        latest_days=old_days+['2026-11-10']
        account['strategy_stop_facts']['trading_days']=latest_days
        t.runtime.update_facts(saved['event'],account,saved['instrument'])
        self.assertEqual(t.kernel.statistics._days(CONTRACT),latest_days)
        held[0][0].set_result(held[0][1]);t.kernel.wait_idle()
        self.assertEqual(t.kernel.statistics._days(CONTRACT),latest_days)
        self.assertEqual(t.field('trade_count'),1)
        self.assertEqual(t.field('total_volume'),3)
        self.assertEqual(t.snap()['actual_confirmed_entry_quantity'],1)

    def test_existing_owner_receipt_rejects_changed_confirmed_statistics_content(self):
        t=self.env;t.start();t.open();before=copy.deepcopy(t.snap())
        cycle=copy.deepcopy(t.cycle());owner=t.kernel.statistics
        event=owner._event(cycle,t.profile,'ENTRY_CONFIRMED','changed-confirmation-evidence')
        event.update(requested_quantity=3,confirmed_quantity=2)
        with self.assertRaises(TraderError) as error:owner._prepare(cycle,t.profile,event)
        self.assertEqual(error.exception.code,'E_DUPLICATE_CONFLICT')
        self.assertEqual(t.snap(),before)
        self.assertEqual(t.cycle()['protection']['owned_qty'],3)

    def test_direct_observe_starts_cooldown_at_current_owner_utc_instead_of_old_entry_time(self):
        t=self.env;t.start(max_net_loss_stops=100,stop_cooldown_enabled=True,stop_cooldown_minutes=5)
        t.open();self.assertEqual(t.last_event['source_time_utc'],t.day+'T01:06:00Z')
        triggered=t.day+'T01:07:00Z'
        with patch('online_trader.kernel.statistics.datetime',wraps=datetime) as owner_clock:
            owner_clock.now.return_value=datetime.fromisoformat(triggered)
            t.kernel.observe(t.cid,'1035',bid1='1034',ask1='1036');t.kernel.wait_idle()
        self.assertEqual(datetime.fromisoformat(t.cycle()['nominal_exit']['trigger_clock']['now_utc']),
            datetime.fromisoformat(triggered))
        order=next(o for o in t.cycle()['orders'] if o['effect']=='REDUCE')
        t.kernel.record_fill(order['child_order_id'],'direct-current-owner-stop',3,'1034');t.kernel.wait_idle()
        t.kernel.reconcile_development_cycle(t.cid,close=True,statistics_at_utc=triggered);t.kernel.wait_idle()
        self.assertEqual(datetime.fromisoformat(t.state()['cooldown_until']['LONG']),
            datetime.fromisoformat(t.day+'T01:12:00Z'))

    def test_reprice_final_gate_and_later_append_recheck_explicit_stops(self):
        e=execution_fixtures.DsvExecutionRuntimeTests();e.setUp()
        try:
            runtime,old,market,account,instrument=e.create_strategy_cycle()
            actual,visible=e.hold_replacement(old,e.event(1000,last='1056',bid='1055',ask='1057',entry_context=False))
            runtime.update_facts(market,account|{'revision':2,'strategy_stop_facts':self.stop_facts()},instrument)
            visible.set_result(actual.result(timeout=3));e.kernel.wait_idle()
            self.assertEqual(len(e.port.orders),1)
            self.assertEqual(e.cycle()['orders'][-1]['send_state'],'DEFINITELY_NOT_SENT')
            before=len(e.cycle()['orders'])
            e.tick(e.event(2000,last='1056',bid='1055',ask='1057',entry_context=False))
            self.assertEqual(len(e.port.orders),1)
            self.assertEqual(len(e.cycle()['orders']),before)
        finally:e.tearDown()

    def test_final_and_replacement_gates_use_local_stop_totals_despite_external_zero_loss(self):
        t=self.env;t.start(max_net_loss_stops=1);t.stop_cycle()
        cycle=copy.deepcopy(t.cycle());candidate=cycle['strategy_metadata']['candidate']
        event=copy.deepcopy(t.last_event);event.update(sequence=t.sequence+1,
            source_time_utc=t.day+'T01:06:02Z',last='1055',bid1='1054',ask1='1056',
            candle_id=t.day.replace('-','')+'-0106')
        saved=t.runtime._latest[(CONTRACT,t.day)]
        account=copy.deepcopy(saved['account']);account['revision']+=1
        self.assertEqual(account['strategy_stop_facts']['net_loss_points'],'0')
        t.runtime.update_facts(event,account,saved['instrument'])
        t.kernel.arm_development();t.kernel.wait_idle()
        self.assertFalse(t.snap()['stop_result']['allow_new_entries'])
        prior=t.runtime.strategy.initial_state(t.key,CONTRACT,t.day)
        expected=t.runtime.strategy.evaluate(prior,event)['candidates']
        self.assertTrue(any(c['decision_key']==candidate['decision_key'] for c in expected))
        self.assertTrue(t.runtime._risk(candidate,t.runtime._latest[(CONTRACT,t.day)])['allowed'])
        self.assertFalse(t.runtime._final_gate(candidate,prior,3,cycle['instrument']))
        order=OrderState('local-stop-child',cycle['orders'][0]['intent_id'],'local-stop-attempt',
            'OPEN',3,'1056',kernel_boot_id=t.kernel.boot_id)
        self.assertIsNone(t.runtime.prepare_replacement(cycle,order))

    def test_session_exit_books_quote_points_without_normal_exit_counts(self):
        t=self.env;t.start();t.open()
        t.observe('1065',at=t.day+'T06:59:41Z')
        order=next(o for o in t.cycle()['orders'] if o['effect']=='REDUCE')
        t.kernel.record_fill(order['child_order_id'],'session-statistics-flat',3,'1064');t.kernel.wait_idle();t.close()
        self.assertEqual(t.field('daily_pnl_points'),'10')
        self.assertEqual(t.field('net_loss_points'),'-10')
        self.assertEqual(t.field('trade_count'),1)
        self.assertEqual(t.field('stop_count'),0)
        self.assertEqual(t.field('profit_count'),0)
        self.assertEqual(t.field('stop_streak'),0)

    def test_protected_floor_books_theoretical_floor_points_instead_of_quote_or_fill(self):
        t=self.env;t.start(protect_80_enabled=True,protect_80_ratio=0.8,
            protect_keep_ratio=0.5,protect_stop_day=False)
        t.open();t.observe('1095',at=t.day+'T01:06:01Z')
        self.assertEqual(t.cycle()['protection']['mode'],'PROTECTED')
        t.exit_fill('1070',actual_price='1069');t.close()
        self.assertEqual(t.cycle()['nominal_exit']['exit_reason'],'PROTECTED_FLOOR')
        self.assertEqual(t.cycle()['nominal_exit']['nominal_exit_price'],'1080')
        self.assertEqual(t.field('daily_pnl_points'),'25')
        self.assertEqual(t.field('net_loss_points'),'-25')
        self.assertEqual(t.field('trade_count'),2)
        self.assertEqual(t.field('profit_count'),1)
        self.assertEqual(t.field('stop_count'),0)

    def test_adverse_slippage_adds_only_nominal_loss_without_normal_exit_counters(self):
        t=self.env;t.start(entry_slip_abort=True,entry_slip_abort_ratio=0.4,
            entry_slip_abort_stop_day=False)
        child=t.open(fill=0)
        t.kernel.record_fill(child,'adverse-entry-confirmed',3,'1065',
            observed_at_utc=t.last_event['source_time_utc']);t.kernel.wait_idle()
        self.assertEqual(t.cycle()['nominal_exit']['exit_reason'],'ADVERSE_SLIPPAGE')
        self.assertEqual(t.cycle()['nominal_exit']['adverse_slippage_points'],'10')
        t.exit_fill('1060',actual_price='1040');t.close()
        self.assertEqual(t.field('net_loss_points'),'10')
        self.assertEqual(t.field('daily_pnl_points'),'0')
        self.assertEqual(t.field('trade_count'),1)
        self.assertEqual(t.field('stop_count'),0)
        self.assertEqual(t.field('profit_count'),0)
        self.assertEqual(t.field('stop_streak'),0)

    def test_data_gap_confirmed_flat_does_not_add_nominal_points_or_exit_counters(self):
        t=self.env;t.start(stale_force_close_seconds=10,stale_close_day_stop=True)
        t.open();t.observe('1065',at=t.day+'T01:06:01Z')
        plan=t.cycle()['plan']
        t.kernel.observe_development_event(t.cid,{'type':'DATA_GAP','active_trading':True,
            'gap_ms':10001,'session_elapsed_ms':300000,'policy_authorized':True,
            'policy_id':plan['data_gap_exit']['data_quality_policy_id'],'legal_price_available':True},
            observed_at_utc=t.last_event['source_time_utc'])
        t.kernel.wait_idle()
        self.assertEqual(t.cycle()['nominal_exit']['exit_reason'],'DATA_GAP_EXIT')
        self.assertFalse(t.cycle()['protection']['block_new_for_trading_day'])
        t.exit_fill('1065',actual_price='1060');t.close()
        self.assertTrue(t.cycle()['protection']['block_new_for_trading_day'])
        self.assertEqual(t.field('net_loss_points'),'0')
        self.assertEqual(t.field('daily_pnl_points'),'0')
        self.assertEqual(t.field('trade_count'),1)
        self.assertEqual(t.field('stop_count'),0)
        self.assertEqual(t.field('profit_count'),0)
        self.assertEqual(t.field('stop_streak'),0)

    def _scheduled_stop(self,*,minutes=5,two_days=False):
        e=scheduled_fixtures.ScheduledRuntimeTests();e.setUp()
        calendar=scheduled_fixtures.calendar()
        if two_days:
            record=calendar.to_record();record['coverage_to_utc']='2026-11-10T16:00:00Z'
            sessions=record['contracts'][0]['sessions']
            for original in list(sessions):
                item=copy.deepcopy(original);item['session_id']='next-'+item['session_id'];item['trading_day']='2026-11-10'
                for field in ('open_utc','close_utc'):item[field]=item[field].replace('2026-11-09','2026-11-10')
                sessions.append(item)
            calendar=TradingCalendar.from_record(record)
        try:
            child=e.start(custom_calendar=calendar,trade={'half_close_enabled':False,'half_stage2':False,
                'half_breakeven':False,'protect_80_enabled':False,'trailing_activate_ratio':0,
                'stop_cooldown_enabled':True,'stop_cooldown_minutes':minutes,'max_net_loss_stops':100,
                'month_stop_days':0,'no_chase_after_act':False})
            e.kernel.record_fill(child,'monotonic-statistics-entry',3,'1055');e.kernel.wait_idle()
            e.quote('1035',advance_ms=1)
            cycle=e.cycle();order=next(o for o in cycle['orders'] if o['effect']=='REDUCE')
            e.kernel.record_fill(order['child_order_id'],'monotonic-statistics-exit',3,'1034');e.kernel.wait_idle()
            e.kernel.reconcile_development_cycle(cycle['position_cycle_id'],close=True,
                statistics_at_utc=e.clock.sample()['now_utc']);e.kernel.wait_idle()
            return e
        except BaseException:e.tearDown();raise

    def test_scheduler_same_boot_cooldown_prefers_monotonic_time_at_exact_boundary(self):
        e=self._scheduled_stop()
        try:
            e.clock.advance(299999);e.clock.wall+=timedelta(milliseconds=500);e.driver.preflight()
            reasons=e.kernel.statistics.entry_reasons(CONTRACT,'2026-11-09',profile=e.driver.strategy.profile,
                direction='LONG',now_utc=e.clock.sample()['now_utc'])
            self.assertIn('STOP_COOLDOWN',reasons)
            e.clock.advance(1);e.driver.preflight()
            reasons=e.kernel.statistics.entry_reasons(CONTRACT,'2026-11-09',profile=e.driver.strategy.profile,
                direction='LONG',now_utc=e.clock.sample()['now_utc'])
            self.assertNotIn('STOP_COOLDOWN',reasons)
            self.assertFalse(e.driver._fault)
        finally:e.tearDown()

    def test_scheduler_cooldown_survives_explicit_trading_day_rollover(self):
        e=self._scheduled_stop(minutes=1440,two_days=True)
        try:
            e.clock.move_to('2026-11-10T01:06:00Z');e.driver.preflight()
            reasons=e.kernel.statistics.entry_reasons(CONTRACT,'2026-11-10',profile=e.driver.strategy.profile,
                direction='LONG',now_utc=e.clock.sample()['now_utc'])
            self.assertIn('STOP_COOLDOWN',reasons)
            reverse=e.kernel.statistics.entry_reasons(CONTRACT,'2026-11-10',profile=e.driver.strategy.profile,
                direction='SHORT',now_utc=e.clock.sample()['now_utc'])
            self.assertNotIn('STOP_COOLDOWN',reverse)
            e.clock.advance(1);e.driver.preflight()
            reasons=e.kernel.statistics.entry_reasons(CONTRACT,'2026-11-10',profile=e.driver.strategy.profile,
                direction='LONG',now_utc=e.clock.sample()['now_utc'])
            self.assertNotIn('STOP_COOLDOWN',reasons)
        finally:e.tearDown()


if __name__=='__main__':unittest.main()
