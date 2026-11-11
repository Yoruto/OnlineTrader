"""Calendar policy boundaries across entry, reference resolution and recesses."""

import copy
import unittest
import uuid

import test_scheduled_runtime as fixtures
from online_trader.errors import TraderError
from online_trader.kernel.scheduler import ScheduledRuntime
from online_trader.scheduling.calendar import TradingCalendar
from qa.recording_port import RecordingPort
from test_dsv_runtime import facts


def night_and_day(*,day='2026-11-09'):
    def session(identity,opened,closed):
        return {'session_id':identity,'trading_day':day,'open_utc':opened,'close_utc':closed,
                'is_opening_session':True,'force_close_at_end':True}
    return TradingCalendar.from_record({'schema_version':'TRADING_CALENDAR/1',
        'calendar_id':'calendar-boundary-night','calendar_version':'v1','complete':True,
        'coverage_from_utc':'2026-11-08T00:00:00Z','coverage_to_utc':'2026-11-10T16:00:00Z',
        'contracts':[{'contract_id':'DCE.p2701','sessions':[
            session('night','2026-11-08T13:00:00Z','2026-11-08T15:00:00Z'),
            session('day','2026-11-09T01:00:00Z','2026-11-09T07:00:00Z')]}]})


class ScheduleBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.owner=fixtures.ScheduledRuntimeTests();self.owner.setUp()
        self.addCleanup(self.owner.tearDown)

    def test_last_ten_minutes_before_recess_do_not_become_the_day_close_entry_block(self):
        owner=self.owner;owner.clock=fixtures.ManualClock('2026-11-09T02:05:00Z')
        owner.start()
        self.assertEqual(len(owner.port.orders),1)
        candidate=owner.cycle()['strategy_metadata']['candidate']
        self.assertEqual(candidate['session_id'],'day-1')
        cached=owner.driver._quotes['DCE.p2701']['event']
        self.assertFalse(cached['session_policy']['force_close_at_end'])
        self.assertEqual(cached['session_policy']['entry_close_utc'],'2026-11-09T07:00:00Z')

    def test_recess_endpoint_is_not_a_forced_exit_even_inside_the_twenty_second_window(self):
        owner=self.owner;owner.holding()
        owner.clock.move_to('2026-11-09T02:14:40Z');owner.quote()
        owner.advance(1000)
        self.assertIsNone(owner.cycle()['protection']['target_remaining_qty'])
        self.assertIsNone(owner.cycle().get('execution',{}).get('close_start_ms'))
        self.assertEqual(owner.reductions(),[])

    def test_night_entry_honors_the_explicit_day_instead_of_inventing_one_from_wall_date(self):
        owner=self.owner;owner.clock=fixtures.ManualClock('2026-11-08T13:06:00Z')
        calendar=night_and_day(day='2026-11-10')
        owner.driver=ScheduledRuntime(owner.kernel,calendar,clock=owner.clock,interval_ms=1)
        owner.driver.activate();owner.kernel.wait_idle();owner.kernel.arm_development();owner.kernel.wait_idle()
        event,account,instrument=facts(owner.key)
        session=calendar.query('DCE.p2701',owner.clock.sample()['now_utc'])['session']
        event.update(trading_day=session['trading_day'],source_time_utc=owner.clock.sample()['now_utc'],
            session_id=session['session_id'],session_open_utc=session['open_utc'],session_close_utc=session['close_utc'],
            is_opening_session=True)
        account['trading_day']=instrument['trading_day']=session['trading_day']
        future=owner.driver.on_market(event,account,instrument);owner.kernel.wait_idle();future.result()
        self.assertEqual(owner.cycle()['plan']['trading_day'],'2026-11-10')
        self.assertEqual(len(owner.port.orders),1)

    def test_incomplete_calendar_cannot_turn_a_quote_into_proven_closed_or_active_time(self):
        owner=self.owner;unknown=fixtures.calendar(complete=False)
        owner.driver=ScheduledRuntime(owner.kernel,unknown,clock=owner.clock,interval_ms=1)
        owner.driver.activate();owner.kernel.wait_idle();owner.kernel.arm_development();owner.kernel.wait_idle()
        event,account,instrument=facts(owner.key)
        event['source_time_utc']=owner.clock.sample()['now_utc']
        with self.assertRaises(TraderError) as raised:owner.driver.on_market(event,account,instrument)
        self.assertEqual(raised.exception.code,'E_CALENDAR_INCOMPLETE')
        self.assertIsNone(unknown.query('DCE.p2701',event['source_time_utc'])['is_trading'])
        self.assertEqual(owner.port.orders,[])

    def test_existing_bound_calendar_cannot_be_replaced_by_a_new_calendar_without_plan_migration(self):
        owner=self.owner;owner.holding();before=copy.deepcopy(owner.cycle());cid=before['position_cycle_id']
        owner.kernel.close();owner.port=RecordingPort();owner.kernel=owner.new_kernel(owner.port)
        changed=fixtures.calendar().to_record();changed['calendar_version']='v2'
        changed['contracts'][0]['sessions'][-1]['close_utc']='2026-11-09T06:30:00Z'
        owner.driver=ScheduledRuntime(owner.kernel,TradingCalendar.from_record(changed),clock=owner.clock,interval_ms=1)
        owner.kernel.reconcile_development_cycle(cid);owner.kernel.wait_idle()
        owner.driver.activate();owner.kernel.wait_idle()
        with self.assertRaises(TraderError):owner.driver.bind_existing_cycle(cid)
        self.assertEqual(owner.cycle()['schedule_binding'],before['schedule_binding'])
        self.assertEqual(owner.cycle()['plan'],before['plan'])
        self.assertEqual(owner.cycle()['protection']['owned_qty'],3)
        self.assertEqual(owner.port.orders,[])

    def test_new_session_quote_cannot_postpone_an_original_missed_forced_exit_deadline(self):
        owner=self.owner;owner.clock=fixtures.ManualClock('2026-11-08T13:06:00Z')
        child=owner.start(custom_calendar=night_and_day())
        owner.kernel.record_fill(child,'boundary-night-fill-'+uuid.uuid4().hex,3,'1055');owner.kernel.wait_idle()
        owner.quote(advance_ms=1)
        owner.kernel.reconcile_development_cycle(owner.cycle()['position_cycle_id']);owner.kernel.wait_idle()
        original_plan=owner.cycle()['plan']
        # Simulate no owner pulse until the next declared session, then deliver
        # a fresh quote first. Its new endpoint must not erase the old deadline.
        owner.clock.move_to('2026-11-09T01:06:00Z');owner.quote()
        self.assertEqual(owner.cycle()['protection']['target_remaining_qty'],0)
        self.assertEqual(owner.cycle()['plan'],original_plan)
        self.assertEqual(owner.cycle()['protection']['owned_qty'],3)
        self.assertEqual(owner.cycle()['execution']['contexts']['REDUCE']['mode'],'MARKET')


if __name__=='__main__':unittest.main()
