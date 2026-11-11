"""Focused original-owner multi-day execution regressions; no SDK connection."""
from datetime import datetime
from pathlib import Path
import copy
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes
from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.kernel import Kernel
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.dsv_simulation import DsvSimulationSession
from tests import test_online_dsv as fixtures
from tests.test_crossday_native import two_days


class DsvDayExecutionTests(unittest.TestCase):
    def setUp(self):self.x=fixtures.OnlineDsvTests();self.x.setUp()
    def tearDown(self):self.x.tearDown()

    def start(self,*,quantity=5,resume=False,code='SHFE.bu2701',short=False):
        x=self.x;calendar=two_days(code);original=fixtures.scope_for
        def scope(path,profile,cal,**values):
            return original(path,profile,cal,**(values|{'schema_version':'KQ_DSV_SIM/2',
                'trading_days':['2026-11-10','2026-11-11'],'resume_entries_on_day_change':resume,
                'not_after_utc':'2026-11-11T07:00:00Z'}))
        with patch.object(fixtures,'day_calendar',return_value=calendar),patch.object(fixtures,'scope_for',side_effect=scope):
            x.start(code=code,quantity=quantity,trade={'half_close_enabled':False})
        self.price=1011 if short else 1079 if code.startswith('SHFE') else 1055
        x.quote(self.price)
        self.oid=x.api.inserted[0]['order_id'];return x

    def next_day(self):
        x=self.x;now=datetime.fromisoformat('2026-11-11T01:06:00Z')
        x.clock.advance(int((now-x.clock.now()).total_seconds()*1000))
        p=x.api.positions[x.code]
        for side in ('long','short'):
            setattr(p,'pos_'+side+'_his',getattr(p,'pos_'+side+'_his')+getattr(p,'pos_'+side+'_today'))
            setattr(p,'pos_'+side+'_today',0)
        x.api.tick(x.code,last=self.price)

    def assert_live(self):
        self.assertFalse(self.x.kernel._failed,self.x.kernel._errors)
        self.assertNotIn('DSV_BLOCKED',self.x.session.phase,self.x.session.phase)

    def test_mixed_today_historical_executes_distinct_children_on_one_original_target(self):
        x=self.start();x.api.fill(self.oid,3,1079);x.session.step();plan=copy.deepcopy(x.cycle()['plan'])
        self.next_day();x.api.fill(self.oid,2,1079);x.session.step();self.assert_live()
        calls=[c for c in x.api.inserted if c['offset']!='OPEN']
        self.assertEqual([(c['offset'],c['volume']) for c in calls],[('CLOSETODAY',2),('CLOSE',3)],x.session.phase)
        self.assertEqual(x.cycle()['protection']['target_remaining_qty'],0)
        self.assertEqual(x.cycle()['plan'],plan)
        for call in calls:x.api.fill(call['order_id'],call['volume'],1079)
        x.session.step();self.assert_live()
        self.assertEqual(x.cycle()['lifecycle'],'CLOSED');self.assertEqual(x.cycle()['protection']['owned_qty'],0)
        self.assertEqual(x.kernel.money.report()['summary']['exit_quantity'],5)

    def test_current_day_collection_can_drop_certified_terminal_original_order(self):
        x=self.start();x.api.fill(self.oid,5,1079);x.session.step()
        self.assertIsNotNone(x.cycle().get('online_reconciliation'))
        self.next_day();x.api.orders.pop(self.oid);x.api.trades.clear()
        x.session.step();self.assert_live()
        self.assertEqual(x.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(x.api.inserted[-1]['volume'],5)
        self.assertTrue(x.cycle()['online_reconciliation'].get('history_sources'))

    def test_prior_day_working_remainder_waits_for_cancel_terminal_and_retained_details(self):
        x=self.start();x.api.fill(self.oid,3,1079);x.session.step();self.next_day()
        x.session.step();self.assertIn(self.oid,x.api.cancelled)
        self.assertEqual(len(x.api.inserted),1)
        self.assertEqual(OrderState.from_dict(x.cycle()['orders'][0]).potential_remaining,2)
        # Simulate the SDK clearing the old day detail collection. Original
        # accepted fill receipts plus exact current native terminal still agree.
        x.api.trades.clear();x.api.orders[self.oid].trade_records.clear();x.api.terminal(self.oid)
        x.session.step();self.assert_live()
        self.assertEqual(x.api.inserted[-1]['offset'],'CLOSE');self.assertEqual(x.api.inserted[-1]['volume'],3)
        self.assertEqual(x.cycle()['protection']['entry_filled_qty'],3)

    def test_unknown_missing_original_id_does_not_replay_or_allow_recovery(self):
        x=self.start();order=x.cycle()['orders'][0]
        # Original native send has no definitive result and disappears from
        # the new day's collection; a local clock/date never proves it absent.
        cycle=copy.deepcopy(x.cycle());cycle['orders'][0]['send_state']='UNKNOWN';cycle['revision']+=1
        x.kernel._stage_cycle(cycle,'TX_ORDER','OFFLINE_UNKNOWN_FACT');x.kernel.wait_idle()
        self.next_day();x.api.orders.clear();x.session.step()
        self.assertEqual(len(x.api.inserted),1)
        self.assertEqual(OrderState.from_dict(x.cycle()['orders'][0]).potential_remaining,order['original_qty'])
        self.assertFalse(x.kernel._armed);self.assertTrue(x.kernel.recovery_required)

    def test_paused_day_rollover_does_not_resume_entries_even_with_explicit_resume_policy(self):
        x=self.start(resume=True);x.api.fill(self.oid,5,1079);x.session.step()
        x.kernel.pause();x.kernel.wait_idle();self.next_day();x.session.step();self.assert_live()
        self.assertFalse(x.kernel._armed)
        self.assertEqual(x.api.inserted[-1]['offset'],'CLOSE')

    def test_version_one_cannot_gain_multi_day_fields(self):
        x=self.start();value=x.grant.value
        with self.assertRaises(TraderError):DsvSimulationGrant.parse(canonical_bytes(value|{'schema_version':'KQ_DSV_SIM/1'}))

    def test_multiday_scope_requires_explicit_days_and_boolean_continuation(self):
        x=self.start();value=x.grant.value
        for changes in ({'trading_days':[]},{'trading_days':['2026-11-11','2026-11-10']},
                        {'trading_days':['2026-11-10','2026-11-10']},{'trading_days':['2026-11-09']},
                        {'resume_entries_on_day_change':1},{'resume_entries_on_day_change':None}):
            with self.subTest(changes=changes),self.assertRaises(TraderError):DsvSimulationGrant.parse(canonical_bytes(value|changes))

    def test_operator_pause_while_old_cancel_is_pending_prevents_next_day_auto_arm(self):
        x=self.start(resume=True);x.api.fill(self.oid,3,1079);x.session.step();self.next_day();x.session.step()
        x.kernel.pause();x.kernel.wait_idle();x.api.terminal(self.oid);x.session.step();self.assert_live()
        self.assertFalse(x.kernel._armed);self.assertEqual(x.api.inserted[-1]['offset'],'CLOSE')

    def test_old_close_today_is_cancelled_then_replaced_as_historical_on_original_intent(self):
        x=self.start();x.api.fill(self.oid,5,1079);x.session.step()
        x.kernel.manual_close(x.cycle()['position_cycle_id'],2);x.kernel.wait_idle()
        old=copy.deepcopy(x.cycle()['orders'][-1]);old_oid=x.api.inserted[-1]['order_id']
        self.assertEqual(old['native_offset'],'CLOSETODAY')
        self.next_day();x.session.step();self.assertIn(old_oid,x.api.cancelled)
        self.assertEqual(len(x.api.inserted),2)
        x.api.terminal(old_oid);x.session.step();self.assert_live()
        new=x.cycle()['orders'][-1]
        self.assertEqual(new['native_offset'],'CLOSE');self.assertEqual(new['native_trading_day'],'2026-11-11')
        self.assertEqual(new['intent_id'],old['intent_id']);self.assertEqual(new['parent_child_order_id'],old['child_order_id'])
        self.assertEqual(new['ordinal'],old['ordinal']+1)

    def test_multi_day_restart_keeps_partial_exit_reservations_without_replaying_children(self):
        x=self.start();x.api.fill(self.oid,3,1079);x.session.step();self.next_day();x.api.fill(self.oid,2,1079);x.session.step()
        calls=list(x.api.inserted);self.assertEqual(len(calls),3)
        historical=next(c for c in calls if c['offset']=='CLOSE');today=next(c for c in calls if c['offset']=='CLOSETODAY')
        x.api.fill(historical['order_id'],1,1079);x.session.step()
        plan=copy.deepcopy(x.cycle()['plan']);target=x.cycle()['protection']['target_remaining_qty']
        x.session.close();x.kernel.close()
        x.port=TqKqDsvPort(x.api,x.api._account,x.grant,x.clock);x.port.configure_calendar(x.calendar)
        x.kernel=Kernel(x.path,x.grant.account_key,port=x.port,simulation_grant=x.grant,risk_budget='1000000')
        x.kernel.simulation.initialize()
        x.session=DsvSimulationSession(x.kernel,Path(x.temp.name)/'new-owner',profile=x.profile,calendar=x.calendar,clock=x.clock,emit=lambda e:None)
        x.session.step();self.assert_live()
        self.assertEqual(len(x.api.inserted),3);self.assertEqual(x.cycle()['plan'],plan)
        self.assertEqual(x.cycle()['protection']['target_remaining_qty'],target)
        self.assertEqual(sum(OrderState.from_dict(o).potential_remaining for o in x.cycle()['orders'] if o['effect']=='REDUCE'),4)
        x.api.fill(historical['order_id'],2,1079);x.api.fill(today['order_id'],2,1079);x.session.step();self.assert_live()
        self.assertEqual(x.cycle()['lifecycle'],'CLOSED');self.assertEqual(len(x.api.inserted),3)

    def test_explicit_continuation_can_enter_next_day_after_original_cycle_is_closed(self):
        x=self.start(resume=True);x.api.fill(self.oid,5,1079);x.session.step();self.next_day();x.session.step();self.assert_live()
        call=x.api.inserted[-1];x.api.fill(call['order_id'],call['volume'],1079);x.session.step();self.assert_live()
        self.assertEqual(x.cycle()['lifecycle'],'CLOSED');self.assertTrue(x.kernel._armed)
        x.quote(1079,milliseconds=60000)
        self.assertEqual(x.api.inserted[-1]['offset'],'OPEN')
        cycles=list(x.kernel._staged.values());self.assertEqual(cycles[-1]['plan']['trading_day'],'2026-11-11')
        self.assertEqual(cycles[-1]['orders'][0]['native_trading_day'],'2026-11-11')

    def test_combined_exchange_mixed_lots_exit_through_one_native_close(self):
        x=self.start(code='DCE.p2701');x.api.fill(self.oid,3,self.price);x.session.step();self.next_day()
        x.api.fill(self.oid,2,self.price);x.session.step();self.assert_live()
        self.assertEqual([(c['offset'],c['volume']) for c in x.api.inserted[1:]],[('CLOSE',5)])
        x.api.fill(x.api.inserted[-1]['order_id'],5,self.price);x.session.step();self.assert_live()
        self.assertEqual(x.cycle()['lifecycle'],'CLOSED')

    def test_short_historical_exit_uses_buy_close_on_original_short_side(self):
        x=self.start(short=True);x.api.fill(self.oid,5,self.price);x.session.step();self.next_day();x.session.step();self.assert_live()
        call=x.api.inserted[-1];self.assertEqual((call['direction'],call['offset'],call['volume']),('BUY','CLOSE',5))
        x.api.fill(call['order_id'],5,self.price);x.session.step();self.assert_live()
        self.assertEqual(x.cycle()['lifecycle'],'CLOSED')

    def test_reconnect_after_rollover_requires_current_epoch_reconciliation_without_replaying_exits(self):
        x=self.start(resume=True);x.api.fill(self.oid,5,1079);x.session.step();self.next_day();x.session.step();self.assert_live()
        count=len(x.api.inserted);x.api._data['notify']['down']={'code':2019112910};x.session.step()
        self.assertFalse(x.kernel._armed);self.assertTrue(x.kernel.recovery_required)
        x.api._data['notify']['up']={'code':2019112902};x.session.step();self.assert_live()
        self.assertFalse(x.port.fault);self.assertFalse(x.kernel._armed)
        self.assertEqual(len(x.api.inserted),count)
        self.assertEqual(x.kernel.simulation.days.ready_epoch,x.port.source_epoch)
