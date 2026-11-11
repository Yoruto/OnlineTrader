"""Offline scheduling uses explicit calendar/clock facts without inventing quotes."""

import copy
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.scheduler import ScheduledRuntime
from online_trader.scheduling.calendar import TradingCalendar
from qa.recording_port import RecordingPort
from test_dsv_runtime import facts


def utc(value):
    return value.isoformat(timespec='milliseconds').replace('+00:00','Z')


class ManualClock:
    def __init__(self,now='2026-11-09T01:06:00Z'):
        self.wall=datetime.fromisoformat(now)
        self.monotonic_ns=time.monotonic_ns()

    def sample(self):
        return {'now_utc':utc(self.wall),'monotonic_ns':str(self.monotonic_ns)}

    def advance(self,milliseconds):
        self.wall+=timedelta(milliseconds=milliseconds)
        self.monotonic_ns+=milliseconds*1_000_000

    def move_to(self,value):
        moment=datetime.fromisoformat(value)
        self.advance(int((moment-self.wall).total_seconds()*1000))


def calendar(*,coverage_to='2026-11-09T16:00:00Z',complete=True):
    def session(identity,start,end,opening=False,force=False):
        return {'session_id':identity,'trading_day':'2026-11-09','open_utc':start,
                'close_utc':end,'is_opening_session':opening,'force_close_at_end':force}
    windows=[
        session('day-1','2026-11-09T01:00:00Z','2026-11-09T02:15:00Z',True),
        session('day-2','2026-11-09T02:30:00Z','2026-11-09T03:30:00Z'),
        session('day-3','2026-11-09T05:30:00Z','2026-11-09T07:00:00Z',force=True)]
    # Short coverage is an explicit incomplete future rather than a guessed calendar.
    end=datetime.fromisoformat(coverage_to)
    windows=[s for s in windows if datetime.fromisoformat(s['close_utc'])<=end]
    return TradingCalendar.from_record({'schema_version':'TRADING_CALENDAR/1',
        'calendar_id':'calendar-scheduled-tests','calendar_version':'v1','complete':complete,
        'coverage_from_utc':'2026-11-09T00:00:00Z','coverage_to_utc':coverage_to,
        'contracts':[{'contract_id':'DCE.p2701','sessions':windows}]})


class ScheduledRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'ledger.db'
        self.key='DEV:scheduled-'+uuid.uuid4().hex
        self.port=RecordingPort()
        self.kernel=self.new_kernel(self.port)
        self.clock=ManualClock()
        self.driver=None
        self.sequence=0

    def new_kernel(self,port,*,development=True):
        return Kernel(self.path,self.key,development=development,port=port,
                      risk_budget='1000000',tick_size='1',multiplier='10')

    def tearDown(self):
        try:self.kernel.close()
        except TraderError:
            if not self.kernel._failed:raise
        finally:self.temp.cleanup()

    def start(self,*,quantity=3,half=False,custom_calendar=None,trade=None):
        settings=load_confirmed_baseline()[0].to_settings()
        settings['trade'].update(half_close_enabled=half,half_stage2=half,half_breakeven=False)
        if trade:settings['trade'].update(trade)
        next(s for s in settings['symbols'] if s['code']=='DCE.p2701')['order_volume']=quantity
        self.calendar=custom_calendar or calendar()
        self.driver=ScheduledRuntime(self.kernel,self.calendar,clock=self.clock,
                                     profile=DsvProfile.from_settings(settings),interval_ms=1)
        self.driver.activate();self.kernel.wait_idle()
        self.kernel.arm_development();self.kernel.wait_idle()
        self.quote()
        return self.cycle()['orders'][0]['child_order_id']

    def cycle(self):return self.kernel.snapshot()['cycles'][0]

    def quote(self,price='1055',*,advance_ms=0):
        if advance_ms:self.clock.advance(advance_ms)
        event,account,instrument=facts(self.key)
        self.sequence+=1
        at=self.calendar.query('DCE.p2701',self.clock.sample()['now_utc'])
        session=at['session']
        event.update(sequence=self.sequence,source_time_utc=self.clock.sample()['now_utc'],
            session_id=session['session_id'],session_open_utc=session['open_utc'],
            session_close_utc=session['close_utc'],is_opening_session=session['is_opening_session'],
            last=price,bid1=str(int(price)-1),ask1=str(int(price)+1),day_high=str(max(1090,int(price))))
        future=self.driver.on_market(event,account,instrument)
        self.kernel.wait_idle()
        return future.result()

    def holding(self,*,quantity=3,half=False,trade=None):
        child=self.start(quantity=quantity,half=half,trade=trade)
        self.kernel.record_fill(child,'scheduled-open-'+uuid.uuid4().hex,quantity,'1055')
        self.kernel.wait_idle()
        self.quote(advance_ms=1)
        self.kernel.reconcile_development_cycle(self.cycle()['position_cycle_id']);self.kernel.wait_idle()
        return child

    def advance(self,milliseconds):
        self.clock.advance(milliseconds)
        result=self.driver.poll()
        self.kernel.wait_idle()
        return result

    def reductions(self):return [o for o in self.port.orders if o['effect']=='REDUCE']

    def test_no_quote_clock_drives_strict_entry_timeout_without_inventing_a_fill(self):
        self.start()
        self.advance(60000)
        self.assertEqual(self.port.cancellations,[])
        self.advance(1)
        self.assertEqual(len(self.port.cancellations),1)
        self.assertEqual(len(self.port.orders),1)
        self.assertEqual(self.cycle()['protection']['owned_qty'],0)
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,3)

    def test_calendar_bound_entry_wait_includes_the_exact_five_minute_boundary(self):
        self.clock=ManualClock('2026-11-09T01:05:00Z')
        self.calendar=calendar()
        self.driver=ScheduledRuntime(self.kernel,self.calendar,clock=self.clock,interval_ms=1)
        self.driver.activate();self.kernel.wait_idle()
        self.kernel.arm_development();self.kernel.wait_idle()
        self.quote()
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.kernel.snapshot()['cycles'],[])
        self.quote(advance_ms=1)
        self.assertEqual(len(self.port.orders),1)

    def test_cancel_terminal_wait_is_monotonic_and_never_releases_the_unknown_quantity(self):
        self.start();self.advance(60001)
        self.advance(29999)
        self.assertFalse(self.kernel.recovery_required)
        self.advance(1)
        self.assertTrue(self.kernel.recovery_required)
        self.assertEqual(self.cycle()['execution']['last_action'],'WAIT_RECONCILIATION')
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,3)
        self.assertEqual(len(self.port.orders),1)

    def test_clock_pulse_does_not_replay_quote_into_a_second_half_stage(self):
        self.holding(quantity=5,half=True)
        self.quote('1090',advance_ms=1)
        before=copy.deepcopy(self.cycle())
        self.assertEqual(before['protection']['phase2_status'],'NOT_TRIGGERED')
        self.advance(500)
        after=self.cycle()
        self.assertEqual(after['protection']['phase2_status'],'NOT_TRIGGERED')
        self.assertEqual(after['protection']['max_favorable_move'],before['protection']['max_favorable_move'])
        self.assertEqual(after['last_quote']['received_ns'],before['last_quote']['received_ns'])
        self.assertEqual([o['original_qty'] for o in self.reductions()],[2])

    def test_session_end_clock_uses_a_still_fresh_cached_quote_without_refreshing_its_age(self):
        self.holding()
        self.clock.move_to('2026-11-09T06:59:39Z');self.quote()
        before=self.cycle()['last_quote']['received_ns']
        self.advance(1000)
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'],0)
        self.assertEqual(len(self.reductions()),1)
        self.assertEqual(self.reductions()[0]['original_qty'],3)
        self.assertEqual(self.cycle()['last_quote']['received_ns'],before)
        self.assertEqual(self.cycle()['protection']['owned_qty'],3)

    def test_session_end_latches_target_but_stale_quote_cannot_send(self):
        self.holding()
        self.clock.move_to('2026-11-09T06:59:09Z');self.quote()
        self.advance(31000)
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'],0)
        self.assertEqual(self.reductions(),[])
        self.quote(advance_ms=1000)
        self.assertEqual(len(self.reductions()),1)

    def test_data_gap_strict_boundary_requests_takeover_when_the_price_has_expired(self):
        self.holding()
        self.advance(180000)
        self.assertFalse(self.kernel.recovery_required)
        self.advance(1)
        self.assertTrue(self.kernel.recovery_required)
        self.assertIn('ALERT_AND_TAKEOVER',self.cycle()['reasons'])
        self.assertEqual(self.reductions(),[])

    def test_intraday_break_is_neither_a_final_close_nor_accumulated_quote_gap(self):
        self.holding()
        self.clock.move_to('2026-11-09T02:14:59Z');self.quote()
        self.advance(5*60*1000+1000)
        self.assertFalse(self.kernel.recovery_required)
        self.assertIsNone(self.cycle()['protection']['target_remaining_qty'])
        self.assertEqual(self.reductions(),[])
        self.clock.move_to('2026-11-09T02:30:10Z');self.driver.poll();self.kernel.wait_idle()
        self.assertFalse(self.kernel.recovery_required)
        self.clock.move_to('2026-11-09T02:33:00Z');self.driver.poll();self.kernel.wait_idle()
        self.assertFalse(self.kernel.recovery_required)
        self.advance(1)
        self.assertTrue(self.kernel.recovery_required)
        self.assertEqual(self.reductions(),[])

    def test_unknown_calendar_coverage_preserves_a_latched_protection_plan(self):
        self.holding()
        self.quote('1095',advance_ms=1)
        before=self.cycle()['protection']
        self.assertEqual(before['mode'],'PROTECTED')
        self.clock.move_to('2026-11-10T01:00:00Z');self.driver.poll();self.kernel.wait_idle()
        after=self.cycle()['protection']
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(after['mode'],'PROTECTED')
        self.assertEqual(after['plan_id'],before['plan_id'])
        self.assertEqual(after['max_favorable_move'],before['max_favorable_move'])
        self.assertEqual(self.reductions(),[])

    def test_wall_clock_regression_disarms_without_removing_existing_protection(self):
        self.holding();self.quote('1095',advance_ms=1)
        before=self.cycle()['protection']
        self.advance(500)
        self.clock.wall-=timedelta(seconds=2)
        self.clock.monotonic_ns+=1_000_000
        result=self.driver.poll();self.kernel.wait_idle()
        self.assertEqual(result['status'],'CLOCK_FAULT')
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.cycle()['protection']['mode'],'PROTECTED')
        self.assertEqual(self.cycle()['protection']['plan_id'],before['plan_id'])
        self.assertEqual(self.reductions(),[])

    def test_clock_provider_failure_is_a_durable_fault_instead_of_leaving_new_entries_armed(self):
        self.start()
        before=self.cycle()['plan']['plan_id']
        self.assertTrue(self.kernel.snapshot()['armed'])
        def unavailable():raise OSError('offline clock source unavailable')
        self.clock.sample=unavailable
        result=self.driver.poll();self.kernel.wait_idle()
        self.assertEqual(result['status'],'CLOCK_FAULT')
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.cycle()['plan']['plan_id'],before)
        self.assertEqual(self.port.cancellations,[])
        self.assertEqual(len(self.port.orders),1)

    def test_explicit_clock_reset_after_reconciliation_stays_disarmed_and_requires_a_new_quote(self):
        self.holding()
        plan_id=self.cycle()['plan']['plan_id'];cid=self.cycle()['position_cycle_id']
        self.advance(500)
        self.clock.wall-=timedelta(seconds=2);self.clock.monotonic_ns+=1_000_000
        self.driver.poll();self.kernel.wait_idle()
        self.assertTrue(self.kernel.recovery_required)
        self.assertTrue(self.driver.status()['clock_fault'])
        self.clock.wall+=timedelta(seconds=3);self.clock.monotonic_ns+=3_000_000_000
        self.kernel.reconcile_development_cycle(cid);self.kernel.wait_idle()
        self.assertFalse(self.kernel.recovery_required)
        self.driver.reset_clock();self.kernel.wait_idle()
        self.assertIsNone(self.driver.status()['clock_fault'])
        self.assertEqual(self.driver.status()['cached_contracts'],[])
        self.assertFalse(self.kernel.execution.price(self.cycle(),'REDUCE')['allowed'])
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.advance(1)
        self.assertEqual(self.driver.status()['cached_contracts'],[])
        self.assertEqual(self.reductions(),[])
        self.quote(advance_ms=1)
        self.assertEqual(self.driver.status()['cached_contracts'],['DCE.p2701'])
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.cycle()['plan']['plan_id'],plan_id)
        self.assertEqual(self.cycle()['protection']['owned_qty'],3)
        self.assertEqual(len(self.port.orders),1)

    def test_a_durable_clock_fault_survives_restart_with_the_same_calendar(self):
        self.holding()
        plan_id=self.cycle()['plan']['plan_id']
        self.advance(500)
        self.clock.wall-=timedelta(seconds=2);self.clock.monotonic_ns+=1_000_000
        self.driver.poll();self.kernel.wait_idle()
        self.assertTrue(self.driver.status()['clock_fault'])
        self.kernel.close()
        self.port=RecordingPort();self.kernel=self.new_kernel(self.port)
        self.driver=ScheduledRuntime(self.kernel,self.calendar,clock=self.clock,interval_ms=1)
        self.assertTrue(self.driver.status()['clock_fault'])
        self.assertEqual(self.driver.status()['cached_contracts'],[])
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.cycle()['plan']['plan_id'],plan_id)
        self.assertEqual(self.cycle()['protection']['owned_qty'],3)
        self.driver.poll();self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.port.cancellations,[])

    def test_kernel_poll_drives_the_attached_scheduler_without_a_background_thread(self):
        self.start();self.clock.advance(60001)
        self.kernel.poll();self.kernel.wait_idle()
        self.assertEqual(len(self.port.cancellations),1)
        self.assertEqual(len(self.port.orders),1)

    def test_clock_action_waits_for_the_owner_to_accept_its_commit_ack(self):
        self.start();self.clock.advance(60001)
        self.driver.poll()
        self.assertEqual(self.port.cancellations,[])
        self.assertTrue(self.kernel._pending)
        self.kernel._pending[0].future.result(timeout=3)
        self.assertEqual(self.port.cancellations,[])
        self.kernel.wait_idle()
        self.assertEqual(len(self.port.cancellations),1)

    def test_clock_fault_revokes_entry_authority_before_an_already_durable_ack_is_accepted(self):
        self.calendar=calendar()
        self.driver=ScheduledRuntime(self.kernel,self.calendar,clock=self.clock,interval_ms=1)
        self.driver.activate();self.kernel.wait_idle()
        self.kernel.arm_development();self.kernel.wait_idle()
        event,account,instrument=facts(self.key)
        session=self.calendar.query(event['contract_id'],self.clock.sample()['now_utc'])['session']
        event.update(source_time_utc=self.clock.sample()['now_utc'],session_id=session['session_id'],
            session_open_utc=session['open_utc'],session_close_utc=session['close_utc'],
            is_opening_session=session['is_opening_session'])
        self.driver.on_market(event,account,instrument)
        self.assertEqual(self.port.orders,[])
        self.kernel._pending[0].future.result(timeout=3)
        self.clock.wall-=timedelta(seconds=2);self.clock.monotonic_ns+=1_000_000
        self.kernel.poll();self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.cycle()['orders'][0]['send_state'],'DEFINITELY_NOT_SENT')

    def test_calendar_coverage_expiry_blocks_a_durable_entry_before_owner_acceptance(self):
        record={'schema_version':'TRADING_CALENDAR/1','calendar_id':'short-calendar',
            'calendar_version':'v1','complete':True,'coverage_from_utc':'2026-11-09T00:00:00Z',
            'coverage_to_utc':'2026-11-09T01:06:01Z','contracts':[{'contract_id':'DCE.p2701',
            'sessions':[{'session_id':'short-session','trading_day':'2026-11-09',
                'open_utc':'2026-11-09T01:00:00Z','close_utc':'2026-11-09T01:06:01Z',
                'is_opening_session':True,'force_close_at_end':False}]}]}
        self.calendar=TradingCalendar.from_record(record)
        self.driver=ScheduledRuntime(self.kernel,self.calendar,clock=self.clock,interval_ms=1)
        self.driver.activate();self.kernel.wait_idle()
        self.kernel.arm_development();self.kernel.wait_idle()
        event,account,instrument=facts(self.key)
        event.update(source_time_utc=self.clock.sample()['now_utc'],session_id='short-session',
            session_open_utc='2026-11-09T01:00:00Z',session_close_utc='2026-11-09T01:06:01Z',
            is_opening_session=True)
        self.driver.on_market(event,account,instrument)
        self.kernel._pending[0].future.result(timeout=3)
        self.assertEqual(self.port.orders,[])
        self.clock.advance(1001)
        self.kernel.poll();self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.cycle()['orders'][0]['send_state'],'DEFINITELY_NOT_SENT')

    def test_known_intraday_break_blocks_pending_entry_before_owner_ack_acceptance(self):
        self.clock=ManualClock('2026-11-09T02:14:59Z')
        self.calendar=calendar()
        self.driver=ScheduledRuntime(self.kernel,self.calendar,clock=self.clock,interval_ms=1)
        self.driver.activate();self.kernel.wait_idle()
        self.kernel.arm_development();self.kernel.wait_idle()
        event,account,instrument=facts(self.key)
        event.update(source_time_utc=self.clock.sample()['now_utc'],session_id='day-1',
            session_open_utc='2026-11-09T01:00:00Z',session_close_utc='2026-11-09T02:15:00Z',
            is_opening_session=True,candle_id='20261109-0214')
        self.driver.on_market(event,account,instrument)
        self.kernel._pending[0].future.result(timeout=3)
        self.assertEqual(self.port.orders,[])
        self.clock.advance(2000)
        at=self.calendar.query(event['contract_id'],self.clock.sample()['now_utc'])
        self.assertTrue(at['complete']);self.assertFalse(at['is_trading'])
        self.kernel.poll();self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertFalse(self.driver.guard.faulted)
        self.assertEqual(self.cycle()['orders'][0]['send_state'],'DEFINITELY_NOT_SENT')

    def test_clock_transaction_rollback_cannot_dispatch_a_cancel(self):
        self.start();self.clock.advance(60001)
        submit=self.kernel.ledger.submit;injected=[]
        def fail_once(kind,event,payload,mutate=None,**kwargs):
            if not injected:
                injected.append(event)
                def fail(conn,sequence):
                    if mutate:mutate(conn,sequence)
                    raise sqlite3.IntegrityError('scheduled transaction rollback')
                return submit(kind,event,payload,fail,**kwargs)
            return submit(kind,event,payload,mutate,**kwargs)
        self.kernel.ledger.submit=fail_once
        self.driver.poll()
        with self.assertRaises(TraderError):self.kernel.wait_idle()
        self.assertTrue(self.kernel._failed)
        self.assertEqual(self.port.cancellations,[])
        self.assertEqual(len(self.port.orders),1)

    def test_restarted_unknown_order_has_no_restored_fresh_quote_or_replayed_send(self):
        self.port.fail_send=True
        self.start()
        self.assertEqual(self.cycle()['orders'][0]['send_state'],'UNKNOWN')
        self.kernel.close()
        self.port=RecordingPort();self.kernel=self.new_kernel(self.port)
        self.driver=ScheduledRuntime(self.kernel,self.calendar,clock=self.clock,interval_ms=1)
        self.clock.advance(61000);self.driver.poll();self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.port.cancellations,[])
        self.assertTrue(self.kernel.recovery_required)
        self.assertEqual(self.cycle()['orders'][0]['send_state'],'UNKNOWN')

    def test_default_kernel_cannot_turn_calendar_or_clock_into_execution_authority(self):
        self.kernel.close();self.kernel=self.new_kernel(None,development=False)
        with self.assertRaises(TraderError):
            driver=ScheduledRuntime(self.kernel,calendar(),clock=self.clock)
            driver.activate()
        self.assertFalse(self.kernel.snapshot()['armed'])


if __name__=='__main__':unittest.main()
