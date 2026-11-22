"""Cross-day facts and original-owner close routing, with zero connections."""
from concurrent.futures import Future
from datetime import datetime
import copy
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import strict_loads
from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError
from online_trader.execution.close_routes import close_routes, preview_owned_routes
from online_trader.gateway.native_days import native_day
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.kernel import Kernel
from online_trader.scheduling.calendar import TradingCalendar
from tests import test_online_dsv as fixtures
from tests.test_close_routes import fill


def two_days(code='DCE.p2701'):
    record=fixtures.day_calendar(code).to_record()
    record['coverage_to_utc']='2026-11-11T08:00:00Z'
    record['contracts'][0]['sessions'].append({'session_id':'next-day','trading_day':'2026-11-11',
        'open_utc':'2026-11-11T01:00:00Z','close_utc':'2026-11-11T07:00:00Z',
        'is_opening_session':True,'force_close_at_end':True})
    return TradingCalendar.from_record(record)


def ns(value):return str(int(datetime.fromisoformat(value).timestamp())*1_000_000_000)


class NativeDayProofTests(unittest.TestCase):
    def test_half_open_boundary_preserves_the_last_nanosecond(self):
        calendar=two_days()
        value=str(int(ns('2026-11-10T07:00:00Z'))-1)
        self.assertEqual(native_day(calendar,'DCE.p2701',value)['trading_day'],'2026-11-10')
        with self.assertRaises(TraderError):native_day(calendar,'DCE.p2701',ns('2026-11-10T07:00:00Z'))

    def test_closed_window_missing_contract_and_uncovered_time_have_no_day(self):
        for code,value in (('DCE.p2701','2026-11-10T08:00:00Z'),('DCE.l2701','2026-11-10T01:01:00Z'),
                           ('DCE.p2701','2026-11-12T01:01:00Z')):
            with self.subTest(code=code,value=value),self.assertRaises(TraderError):native_day(two_days(),code,ns(value))

    def test_invalid_timestamp_is_never_replaced_by_requested_day(self):
        for value in (None,True,0,'0','01','-1','1.1','9223372036854775808'):
            with self.subTest(value=value),self.assertRaises(TraderError):native_day(two_days(),'DCE.p2701',value)

    def test_night_day_is_the_explicit_calendar_label(self):
        record=two_days().to_record()
        record['contracts'][0]['sessions'].insert(1,{'session_id':'night','trading_day':'2026-11-11',
            'open_utc':'2026-11-10T13:00:00Z','close_utc':'2026-11-10T15:00:00Z',
            'is_opening_session':True,'force_close_at_end':False})
        proof=native_day(TradingCalendar.from_record(record),'DCE.p2701',ns('2026-11-10T14:00:00Z'))
        self.assertEqual(proof['trading_day'],'2026-11-11');self.assertEqual(proof['session_id'],'night')


class CycleBucketAllocationTests(unittest.TestCase):
    def review(self,*,target=2,orders=None,fills=None,cycle='today'):
        facts=fills or [fill('old','2026-11-09',3)|{'position_cycle_id':'old'},
                        fill('new','2026-11-10',2,seq=2)|{'position_cycle_id':'today'}]
        return close_routes(contract_id='SHFE.rb2701',direction='LONG',current_day='2026-11-10',
            fills=facts,native={'today':2,'historical':3,'total':5},orders=orders or [],
            target_quantity=target,position_cycle_id=cycle)

    def order(self,*,cycle='today',offset='CLOSETODAY',qty=1,**changes):
        return {'position_cycle_id':cycle,'contract_id':'SHFE.rb2701','direction':'LONG',
            'original_qty':qty,'filled_qty':0,'send_state':'UNKNOWN','terminal_confirmed':False,
            'reported_fill_qty':None,'native_offset':offset,'trading_day':'2026-11-10'}|changes

    def test_selected_cycle_cannot_spend_another_cycles_historical_lots(self):
        result=self.review()
        self.assertEqual(result['routes'],[{'native_offset':'CLOSETODAY','quantity':2}])
        self.assertEqual(result['cycle_owned'],{'today':2,'historical':0})

    def test_target_already_covered_by_pending_close_creates_no_extra_route(self):
        self.assertEqual(self.review(target=1,orders=[self.order()])['routes'],[])

    def test_other_cycle_pending_does_not_reduce_the_selected_cycles_target(self):
        result=self.review(orders=[self.order(cycle='old',offset='CLOSE',qty=2)])
        self.assertEqual(result['routes'],[{'native_offset':'CLOSETODAY','quantity':2}])

    def test_selected_target_must_not_exceed_its_own_quantity(self):
        with self.assertRaises(TraderError):self.review(target=3)

    def test_fill_cannot_close_another_cycles_lots(self):
        facts=[fill('old','2026-11-09',4)|{'position_cycle_id':'old'},
               fill('new','2026-11-10',2,seq=2)|{'position_cycle_id':'today'},
               fill('close','2026-11-10',1,effect='REDUCE',offset='CLOSE',seq=3)|{'position_cycle_id':'today'}]
        with self.assertRaises(TraderError):self.review(fills=facts)

    def test_reduction_cannot_consume_a_later_open_even_on_combined_exchange(self):
        facts=[fill('later','2026-11-10',3)|{'contract_id':'DCE.p2701'},
               fill('close','2026-11-09',1,effect='REDUCE',offset='CLOSE',seq=2)|{'contract_id':'DCE.p2701'}]
        with self.assertRaises(TraderError):close_routes(contract_id='DCE.p2701',direction='LONG',current_day='2026-11-10',
            fills=facts,native={'today':2,'historical':0,'total':2},orders=[],target_quantity=2)

    def test_unknown_with_matching_terminal_flags_still_reserves_until_proved(self):
        order=self.order(terminal_confirmed=True,reported_fill_qty=0)
        self.assertEqual(self.review(target=1,orders=[order])['routes'],[])
        state=OrderState('child','intent','attempt','REDUCE',1,'1000',send_state='UNKNOWN',
                         terminal_confirmed=True,reported_fill_qty=0)
        self.assertEqual(state.potential_remaining,1)

    def test_combined_close_consumes_native_today_before_historical_not_cost_fifo(self):
        facts=[fill('old','2026-11-09',3),fill('today','2026-11-10',2,seq=2),
               fill('close','2026-11-10',2,effect='REDUCE',offset='CLOSE',seq=3)]
        facts=[f|{'contract_id':'DCE.p2701'} for f in facts]
        review=close_routes(contract_id='DCE.p2701',direction='LONG',current_day='2026-11-10',
            fills=facts,native={'today':0,'historical':3,'total':3},orders=[],target_quantity=3)
        self.assertEqual(review['routes'],[{'native_offset':'CLOSE','quantity':3}])

    def test_combined_close_cannot_spend_another_cycles_today_lots(self):
        facts=[fill('old','2026-11-09',3)|{'contract_id':'DCE.p2701','position_cycle_id':'old'},
               fill('new','2026-11-10',2,seq=2)|{'contract_id':'DCE.p2701','position_cycle_id':'today'}]
        review=close_routes(contract_id='DCE.p2701',direction='LONG',current_day='2026-11-10',
            fills=facts,native={'today':2,'historical':3,'total':5},orders=[],target_quantity=3,position_cycle_id='old')
        self.assertEqual(review['routes'],[])

    def test_combined_close_historical_first_deducts_historical_and_protects_other_cycles_historical_lots(self):
        for code in ('DCE.p2701', 'CZCE.OI701'):
            with self.subTest(code=code):
                facts = [
                    fill('old', '2026-11-09', 3) | {'contract_id': code, 'position_cycle_id': 'old'},
                    fill('new', '2026-11-10', 2, seq=2) | {'contract_id': code, 'position_cycle_id': 'today'},
                ]
                # Under historical_first_on_combined (pinned TqSdk 3.10.4 / DCE / CZCE):
                # 1) Closing the historical cycle 'old' is safe because CLOSE deducts historical lots first.
                old_review = close_routes(
                    contract_id=code, direction='LONG', current_day='2026-11-10',
                    fills=facts, native={'today': 2, 'historical': 3, 'total': 5},
                    orders=[], target_quantity=3, position_cycle_id='old',
                    historical_first_on_combined=True)
                self.assertEqual(old_review['routes'], [{'native_offset': 'CLOSE', 'quantity': 3}])
                self.assertEqual(old_review['routing_reasons'], [])

                # 2) Closing the today cycle 'today' while 'old' still holds historical lots
                # is blocked because CLOSE would spend 'old' cycle's historical lots first.
                today_review = close_routes(
                    contract_id=code, direction='LONG', current_day='2026-11-10',
                    fills=facts, native={'today': 2, 'historical': 3, 'total': 5},
                    orders=[], target_quantity=2, position_cycle_id='today',
                    historical_first_on_combined=True)
                self.assertEqual(today_review['routes'], [])
                self.assertIn('COMBINED_CLOSE_WOULD_SPEND_OTHER_CYCLE_HISTORICAL', today_review['routing_reasons'])

                # 3) After 2 lots of 'old' close on CLOSE, historical shrinks to 1 and today stays 2.
                after_partial = facts + [
                    fill('close-old', '2026-11-10', 2, effect='REDUCE', offset='CLOSE', seq=3)
                    | {'contract_id': code, 'position_cycle_id': 'old'}
                ]
                partial_review = close_routes(
                    contract_id=code, direction='LONG', current_day='2026-11-10',
                    fills=after_partial, native={'today': 2, 'historical': 1, 'total': 3},
                    orders=[], target_quantity=1, position_cycle_id='old',
                    historical_first_on_combined=True)
                self.assertEqual(partial_review['owned_today'], 2)
                self.assertEqual(partial_review['owned_historical'], 1)
                self.assertEqual(partial_review['routes'], [{'native_offset': 'CLOSE', 'quantity': 1}])


class NativeRolloverOwnerTests(unittest.TestCase):
    def setUp(self):
        self.x=fixtures.OnlineDsvTests();self.x.setUp()

    def tearDown(self):self.x.tearDown()

    def start(self,*,code='DCE.p2701',quantity=5,unknown=False,calendar=None):
        calendar=calendar or two_days(code)
        with patch.object(fixtures,'day_calendar',return_value=calendar):
            self.x.start(code=code,quantity=quantity,trade={'half_close_enabled':False})
        if unknown:
            with patch.object(self.x.api,'insert_order',side_effect=OSError('offline ambiguous native call')):self.x.quote()
            return self.x.port.order_id(self.x.cycle()['orders'][0]['child_order_id'])
        self.x.quote(1079 if code.startswith('SHFE.') else 1055)
        return self.x.api.inserted[0]['order_id']

    def advance_day(self):
        x=self.x
        x.clock.advance(int((datetime.fromisoformat('2026-11-11T01:06:00Z')-x.clock.now()).total_seconds()*1000))
        for p in x.api.positions.values():
            p.pos_long_his+=p.pos_long_today;p.pos_long_today=0
            p.pos_short_his+=p.pos_short_today;p.pos_short_today=0

    def test_delayed_prior_day_fill_is_committed_under_original_day_after_rollover(self):
        x=self.x;oid=self.start();x.api.fill(oid,2,1055)
        plan=copy.deepcopy(x.cycle()['plan']);self.advance_day();x.session.step()
        self.assertEqual(x.cycle()['protection']['owned_qty'],2)
        facts=x.kernel.ledger.query('SELECT trading_day,raw_fact_json FROM fill_facts')
        self.assertEqual(facts[0]['trading_day'],'2026-11-10')
        self.assertEqual(strict_loads(facts[0]['raw_fact_json'])['native_day_evidence']['calendar_hash'],x.calendar.content_hash)
        self.assertEqual(x.cycle()['plan'],plan);self.assertFalse(x.kernel.snapshot()['armed'])
        self.assertTrue(x.kernel.recovery_required);self.assertEqual(len(x.api.inserted),1)
        x.session.step();self.assertEqual(len(x.kernel.ledger.query('SELECT * FROM fill_facts')),1)

    def test_current_day_late_fill_does_not_inherit_original_grant_day(self):
        x=self.x;oid=self.start();self.advance_day();x.api.fill(oid,2,1055);x.session.step()
        self.assertEqual(x.kernel.ledger.query('SELECT trading_day FROM fill_facts')[0]['trading_day'],'2026-11-11')
        self.assertEqual(x.cycle()['protection']['owned_qty'],2)
        self.assertEqual(x.cycle()['plan']['trading_day'],'2026-11-10')
        self.assertEqual(len(x.api.inserted),1);self.assertTrue(x.kernel.recovery_required)

    def test_crossday_terminal_releases_only_after_detailed_fill_is_accepted(self):
        x=self.x;oid=self.start();x.api.fill(oid,2,1055);x.api.terminal(oid)
        self.advance_day();x.session.step()
        order=x.cycle()['orders'][0]
        self.assertTrue(order['terminal_confirmed']);self.assertEqual(order['filled_qty'],2)
        self.assertEqual(OrderState.from_dict(order).potential_remaining,0)
        self.assertTrue(x.kernel.recovery_required)

    def test_missing_fill_day_retains_native_evidence_without_inventing_a_fill(self):
        x=self.x;oid=self.start();x.api.fill(oid,2,1055)
        next(iter(x.api.trades.values())).trade_date_time=int(ns('2026-11-10T08:00:00Z'))
        self.advance_day();x.session.step()
        self.assertEqual(x.kernel.ledger.query('SELECT * FROM fill_facts'),[])
        self.assertEqual(OrderState.from_dict(x.cycle()['orders'][0]).potential_remaining,5)
        events=x.kernel.ledger.query("SELECT payload_json FROM event_journal WHERE event_type='ONLINE_ORDER_OBSERVATIONS'")
        retained=[f for row in events for o in strict_loads(row['payload_json'])['data']['orders'] for f in o['fills']]
        self.assertEqual(retained[-1]['native_day_error'],'E_CALENDAR_INCOMPLETE')
        self.assertTrue(x.kernel.recovery_required)

    def test_changed_original_order_day_is_not_silently_accepted(self):
        x=self.x;oid=self.start();x.api.fill(oid,1,1055)
        x.api.orders[oid].insert_date_time=int(ns('2026-11-11T01:01:00Z'))
        self.advance_day();x.session.step()
        self.assertEqual(x.kernel.ledger.query('SELECT * FROM fill_facts'),[])
        self.assertEqual(len(x.api.inserted),1)

    def test_original_fills_remain_deduplicated_after_owner_restart(self):
        x=self.x;oid=self.start();x.api.fill(oid,2,1055);self.advance_day();x.session.step()
        x.session.close();x.session=None;x.kernel.close()
        port=TqKqDsvPort(x.api,x.api._account,x.grant,x.clock);port.configure_calendar(x.calendar)
        x.kernel=Kernel(x.path,x.grant.account_key,port=port,simulation_grant=x.grant,risk_budget='1000000')
        x.kernel.simulation.observe_native_orders();x.kernel.wait_idle()
        self.assertEqual(x.cycle()['protection']['owned_qty'],2)
        self.assertEqual(len(x.kernel.ledger.query('SELECT * FROM fill_facts')),1)
        self.assertEqual(len(x.api.inserted),1);self.assertFalse(x.kernel.snapshot()['armed'])

    def test_missing_original_order_on_new_day_keeps_unknown_and_does_not_resend(self):
        x=self.x;self.start(unknown=True);order=x.cycle()['orders'][0];x.api.orders.clear()
        self.advance_day();x.session.step()
        self.assertEqual(OrderState.from_dict(x.cycle()['orders'][0]).potential_remaining,order['original_qty'])
        self.assertEqual(len(x.api.inserted),0);self.assertTrue(x.kernel.recovery_required)
        self.assertEqual(x.cycle()['orders'][0]['send_state'],'UNKNOWN')

    def test_fill_on_a_day_before_original_order_stays_pending(self):
        record=two_days().to_record();record['coverage_from_utc']='2026-11-09T00:00:00Z'
        record['contracts'][0]['sessions'].insert(0,{'session_id':'previous','trading_day':'2026-11-09',
            'open_utc':'2026-11-09T01:00:00Z','close_utc':'2026-11-09T07:00:00Z',
            'is_opening_session':True,'force_close_at_end':True})
        x=self.x;oid=self.start(calendar=TradingCalendar.from_record(record));x.api.fill(oid,1,1055)
        next(iter(x.api.trades.values())).trade_date_time=int(ns('2026-11-09T01:01:00Z'))
        x.session.step()
        self.assertEqual(x.kernel.ledger.query('SELECT * FROM fill_facts'),[])
        self.assertEqual(OrderState.from_dict(x.cycle()['orders'][0]).potential_remaining,5)
        self.assertTrue(x.kernel.recovery_required)

    def test_same_day_fill_time_is_not_rejected_by_an_unproved_cross_clock_ordering_rule(self):
        x=self.x;oid=self.start();x.api.fill(oid,1,1055)
        next(iter(x.api.trades.values())).trade_date_time=int(ns('2026-11-10T01:06:00Z'))
        x.session.step()
        self.assertEqual(x.cycle()['protection']['owned_qty'],1)
        self.assertFalse(x.kernel._failed)

    def test_native_order_timestamp_cannot_change_inside_the_same_cabinet_day(self):
        x=self.x;oid=self.start();x.api.fill(oid,1,1055);x.session.step()
        x.api.orders[oid].insert_date_time-=1000
        x.session.step()
        self.assertTrue(x.kernel.recovery_required);self.assertFalse(x.kernel.snapshot()['armed'])
        self.assertEqual(x.cycle()['protection']['owned_qty'],1)
        self.assertEqual(len(x.api.inserted),1)

    def test_future_native_order_time_does_not_prove_zero_fill_terminal(self):
        x=self.x;oid=self.start();x.api.orders[oid].insert_date_time=int(ns('2026-11-10T01:10:00Z'))
        x.api.terminal(oid);x.session.step()
        self.assertEqual(OrderState.from_dict(x.cycle()['orders'][0]).potential_remaining,5)
        self.assertFalse(x.cycle()['orders'][0]['terminal_confirmed']);self.assertTrue(x.kernel.recovery_required)

    def test_future_native_fill_is_not_accepted_using_a_valid_calendar_label(self):
        x=self.x;oid=self.start();x.api.fill(oid,1,1055)
        next(iter(x.api.trades.values())).trade_date_time=int(ns('2026-11-10T01:10:00Z'))
        x.session.step()
        self.assertEqual(x.kernel.ledger.query('SELECT * FROM fill_facts'),[])
        self.assertTrue(x.kernel.recovery_required)

    def test_proved_day_fill_is_retained_when_arrival_follows_a_different_day_fill(self):
        x=self.x;oid=self.start();x.api.fill(oid,1,1055);x.session.step()
        self.advance_day();x.api.fill(oid,1,1055);x.session.step()
        days=[r['trading_day'] for r in x.kernel.ledger.query('SELECT trading_day FROM fill_facts ORDER BY recorded_event_seq')]
        self.assertEqual(days,['2026-11-10','2026-11-11']);self.assertEqual(x.cycle()['protection']['owned_qty'],2)
        self.assertEqual(x.kernel.money.report()['summary']['entry_quantity'],2)

    def test_native_bucket_change_between_intent_and_ack_blocks_send(self):
        x=self.x;oid=self.start(code='SHFE.bu2701');x.api.fill(oid,5,1079);x.session.step()
        k=x.kernel;k.manual_close(x.cycle()['position_cycle_id'],5)
        pending=k._pending[0];ack=pending.future.result(timeout=3);held=Future();pending.future=held
        k.poll();self.assertEqual(len(x.api.inserted),1)
        held.set_result(ack);k.poll()
        exit_pending=k._pending[0];exit_ack=exit_pending.future.result(timeout=3);exit_held=Future();exit_pending.future=exit_held
        p=x.api.positions[x.code];p.pos_long_today=4;p.pos_long_his=1
        exit_held.set_result(exit_ack);k.wait_idle()
        self.assertEqual(len(x.api.inserted),1)
        self.assertEqual(x.cycle()['orders'][-1]['send_state'],'DEFINITELY_NOT_SENT')
        self.assertEqual(x.cycle()['protection']['owned_qty'],5)

    def test_close_route_and_day_are_durable_on_original_child(self):
        x=self.x;oid=self.start(code='SHFE.bu2701');x.api.fill(oid,5,1079);x.session.step()
        x.kernel.manual_close(x.cycle()['position_cycle_id'],2);x.kernel.wait_idle()
        order=x.cycle()['orders'][-1]
        self.assertEqual(order['native_trading_day'],'2026-11-10')
        self.assertEqual(order['native_offset'],'CLOSETODAY')
        self.assertEqual(order['close_route']['quantity'],2)
        self.assertEqual(order['close_route']['position_cycle_id'],x.cycle()['position_cycle_id'])
        self.assertEqual(x.api.inserted[-1]['volume'],2)
        review=preview_owned_routes(x.kernel,x.cycle()['position_cycle_id'],2)
        self.assertEqual(review['routes'],[])
