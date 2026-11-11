"""DSV nominal statistics use owned, committed fills and reconciled closure."""

import copy
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.dsv_strategy import DsvStrategyRuntime
from qa.recording_port import RecordingPort
from test_dsv_runtime import facts


CONTRACT='DCE.p2701'
DAYS=['2026-11-04','2026-11-05','2026-11-06','2026-11-09','2026-11-10','2026-12-01']


class StatisticsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.db'
        self.key='DEV:statistics-'+uuid.uuid4().hex;self.port=RecordingPort()
        self.kernel=self.new_kernel();self.runtime=None;self.sequence=0;self.revision=0;self.minute=5
        self.day='2026-11-09';self.cid=None;self.last_event=None

    def new_kernel(self):
        return Kernel(self.path,self.key,development=True,port=self.port,risk_budget='1000000',
                      tick_size='1',multiplier='10')

    def tearDown(self):
        try:self.kernel.close()
        except TraderError:
            if not self.kernel._failed:raise
        finally:self.temp.cleanup()

    def start(self,*,quantity=3,stop=16,**trade):
        settings=load_confirmed_baseline()[0].to_settings()
        settings['trade'].update(half_close_enabled=False,half_stage2=False,half_breakeven=False,
            protect_80_enabled=False,trailing_activate_ratio=0,no_chase_after_act=False,month_stop_days=0)
        settings['trade'].update(trade)
        row=next(s for s in settings['symbols'] if s['code']==CONTRACT)
        row.update(order_volume=quantity,stop_offset=stop)
        self.profile=DsvProfile.from_settings(settings)
        self.runtime=DsvStrategyRuntime(self.kernel,profile=self.profile)
        self.runtime.activate();self.kernel.wait_idle()

    def cycle(self):
        return next(c for c in self.kernel._committed.values() if c['position_cycle_id']==self.cid)

    def snap(self,day=None):return self.kernel.statistics.snapshot(CONTRACT,day or self.day)

    def state(self,day=None):
        snapshot=self.snap(day)
        return {} if snapshot is None else snapshot.get('state',{})

    def field(self,name,day=None):return self.state(day).get(name,0 if name not in ('daily_pnl_points','net_loss_points') else '0')

    def observe(self,price='1055',*,day=None,at=None,candle=None):
        self.day=day or self.day;self.sequence+=1;self.revision+=1
        event,account,instrument=facts(self.key)
        moment=at or f'{self.day}T01:{self.minute:02d}:00Z'
        event.update(trading_day=self.day,session_id='statistics-'+self.day.replace('-',''),
            source_time_utc=moment,session_open_utc=self.day+'T01:00:00Z',
            session_close_utc=self.day+'T07:00:00Z',sequence=self.sequence,
            last=price,bid1=str(int(price)-1),ask1=str(int(price)+1),
            day_high=str(max(1090,int(price))),day_low=str(min(1000,int(price))),
            candle_id=candle or self.day.replace('-','')+'-'+moment[11:16].replace(':',''))
        account.update(trading_day=self.day,revision=self.revision)
        account['strategy_stop_facts']={'net_loss_points':'0','stop_streak':0,'continuation_stopped':False,
            'day_stopped':False,'day_stopped_reason':None,'stop_days':[],'month_stop':None,
            'trading_days':[d for d in DAYS if d<=self.day]}
        instrument['trading_day']=self.day
        self.last_event=event
        result=self.runtime.on_market(event,account,instrument);self.kernel.wait_idle()
        return result.result()

    def open(self,*,fill=3,day=None,at=None):
        self.kernel.arm_development();self.kernel.wait_idle()
        self.minute+=1
        report=self.observe(day=day,at=at)
        child=report['child_order_id'];self.assertIsNotNone(child,report)
        self.cid=next(c['position_cycle_id'] for c in self.kernel._committed.values()
            if any(o['child_order_id']==child for o in c['orders']))
        if fill:
            self.kernel.record_fill(child,'statistics-entry-'+uuid.uuid4().hex,fill,'1055');self.kernel.wait_idle()
        return child

    def exit_fill(self,price='1035',*,actual_price=None):
        moment=datetime.fromisoformat(self.last_event['source_time_utc'])+timedelta(seconds=1)
        self.observe(price,at=moment.isoformat().replace('+00:00','Z'))
        order=next(o for o in reversed(self.cycle()['orders']) if o['effect']=='REDUCE' and not o['terminal_confirmed'])
        self.kernel.record_fill(order['child_order_id'],'statistics-exit-'+uuid.uuid4().hex,
            order['original_qty'],actual_price or str(int(price)-1));self.kernel.wait_idle()
        return order

    def close(self,*,wait=True):
        result=self.kernel.reconcile_development_cycle(self.cid,close=True,
            statistics_at_utc=self.last_event['source_time_utc'])
        if wait:self.kernel.wait_idle()
        return result

    def stop_cycle(self,**open_args):
        self.open(**open_args);self.exit_fill();self.close()

    def test_entry_confirmation_counts_once_and_keeps_requested_volume_distinct_from_actual_quantity(self):
        self.start();child=self.open(fill=1)
        self.assertEqual(self.field('trade_count'),1)
        self.assertEqual(self.field('total_volume'),3)
        self.assertEqual(self.snap()['actual_confirmed_entry_quantity'],1)
        self.kernel.record_fill(child,'entry-second-detail',2,'1055');self.kernel.wait_idle()
        self.assertEqual(self.field('trade_count'),1)
        self.assertEqual(self.field('total_volume'),3)
        self.assertEqual(self.snap()['actual_confirmed_entry_quantity'],3)

    def test_target_send_and_partial_exit_do_not_book_a_completed_stop(self):
        self.start();self.open()
        self.observe('1035',at=self.day+'T01:06:01Z')
        order=next(o for o in self.cycle()['orders'] if o['effect']=='REDUCE')
        self.assertEqual(self.field('stop_count'),0)
        self.kernel.record_fill(order['child_order_id'],'partial-exit-only',1,'1034');self.kernel.wait_idle()
        with self.assertRaises(TraderError):self.close()
        self.assertEqual(self.field('stop_count'),0)
        self.assertEqual(self.field('net_loss_points'),'0')
        self.assertEqual(self.field('trade_count'),1)

    def test_flat_without_reconciliation_does_not_book_the_exit(self):
        self.start();self.open();self.exit_fill()
        self.assertEqual(self.cycle()['protection']['owned_qty'],0)
        self.assertEqual(self.cycle()['lifecycle'],'FLAT_PENDING_CONFIRM')
        self.assertEqual(self.field('stop_count'),0)
        self.assertEqual(self.field('trade_count'),1)
        self.close()
        self.assertEqual(self.field('stop_count'),1)

    def test_stop_uses_source_S_but_daily_points_use_frozen_trigger_pf_without_quantity_weighting(self):
        self.start();self.open();self.exit_fill('1035',actual_price='1034');self.close()
        self.assertEqual(self.field('net_loss_points'),'16')
        self.assertEqual(self.field('daily_pnl_points'),'-20')
        self.assertEqual(self.field('stop_streak'),1)
        self.assertEqual(self.field('stop_count'),1)
        self.assertEqual(self.field('trade_count'),2)
        self.assertEqual(self.snap()['actual_confirmed_exit_quantity'],3)

    def test_take_profit_uses_T_and_decision_pf_instead_of_fill_price_or_currency(self):
        self.start();self.open();self.exit_fill('1110',actual_price='1100');self.close()
        self.assertEqual(self.field('net_loss_points'),'-50')
        self.assertEqual(self.field('daily_pnl_points'),'55')
        self.assertEqual(self.field('profit_count'),1)
        self.assertEqual(self.field('stop_count'),0)
        self.assertEqual(self.field('stop_streak'),0)

    def test_first_full_exit_cause_is_not_replaced_by_a_later_take_profit_quote(self):
        self.start();self.open()
        self.observe('1035',at=self.day+'T01:06:01Z')
        order=next(o for o in self.cycle()['orders'] if o['effect']=='REDUCE')
        self.observe('1120',at=self.day+'T01:06:02Z')
        self.kernel.record_fill(order['child_order_id'],'old-stop-detail',3,'1034');self.kernel.wait_idle();self.close()
        self.assertEqual(self.field('net_loss_points'),'16')
        self.assertEqual(self.field('daily_pnl_points'),'-20')
        self.assertEqual(self.field('stop_count'),1)
        self.assertEqual(self.field('profit_count'),0)

    def test_half_reduction_does_not_add_a_nominal_trade_or_weight_the_final_nominal_profit(self):
        self.start(quantity=5,half_close_enabled=True)
        self.open(fill=5)
        self.kernel.reconcile_development_cycle(self.cid);self.kernel.wait_idle()
        self.observe('1075',at=self.day+'T01:06:01Z')
        half=next(o for o in self.cycle()['orders'] if o['effect']=='REDUCE')
        self.assertEqual(half['original_qty'],2)
        self.kernel.record_fill(half['child_order_id'],'half-fill',2,'1074');self.kernel.wait_idle()
        self.assertEqual(self.field('trade_count'),1)
        self.assertEqual(self.field('net_loss_points'),'0')
        self.exit_fill('1105',actual_price='1104');self.close()
        self.assertEqual(self.field('daily_pnl_points'),'50')
        self.assertEqual(self.field('net_loss_points'),'-50')
        self.assertEqual(self.field('trade_count'),2)
        self.assertEqual(self.snap()['actual_confirmed_exit_quantity'],5)

    def test_manual_exit_uses_owned_fill_points_and_cannot_mix_cash_into_the_nominal_ledger(self):
        self.start();self.open();self.observe('1065',at=self.day+'T01:06:01Z')
        self.kernel.manual_close(self.cid,3,statistics_at_utc=self.last_event['source_time_utc']);self.kernel.wait_idle()
        order=next(o for o in self.cycle()['orders'] if o['effect']=='REDUCE')
        self.kernel.record_fill(order['child_order_id'],'manual-owned-exit',3,'1064');self.kernel.wait_idle();self.close()
        self.assertEqual(self.field('daily_pnl_points'),'9')
        self.assertEqual(self.field('net_loss_points'),'-9')
        self.assertEqual(self.field('trade_count'),2)
        self.assertEqual(self.field('stop_count'),1)
        self.assertEqual(self.field('profit_count'),0)

    def test_unknown_exit_cannot_be_closed_or_release_a_nominal_statistics_receipt(self):
        self.start();self.open();self.port.fail_send=True
        self.observe('1035',at=self.day+'T01:06:01Z')
        self.assertTrue(any(o['send_state']=='UNKNOWN' for o in self.cycle()['orders']))
        with self.assertRaises(TraderError):self.close()
        self.assertEqual(self.field('trade_count'),1)
        self.assertEqual(self.field('stop_count'),0)

    def _held_close(self):
        submit=self.kernel.ledger.submit;held=[]
        def intercept(kind,event,payload,mutate=None,**kwargs):
            actual=submit(kind,event,payload,mutate,**kwargs)
            if payload.get('cycle',{}).get('lifecycle')=='CLOSED':
                actual.result(timeout=3);replacement=Future();held.append((replacement,actual.result()));return replacement
            return actual
        return patch.object(self.kernel.ledger,'submit',side_effect=intercept),held

    def test_close_statistics_wait_for_owner_acceptance_of_the_matching_commit_ack(self):
        self.start();self.open();self.exit_fill()
        hold,held=self._held_close()
        with hold:self.close(wait=False)
        self.assertEqual(len(held),1)
        self.assertEqual(self.field('stop_count'),0)
        held[0][0].set_result(held[0][1]);self.kernel.wait_idle()
        self.assertEqual(self.field('stop_count'),1)
        self.assertEqual(self.field('trade_count'),2)

    def test_wrong_close_ack_failstops_without_publishing_the_statistics(self):
        self.start();self.open();self.exit_fill()
        hold,held=self._held_close()
        with hold:self.close(wait=False)
        held[0][0].set_result(replace(held[0][1],kernel_boot_id='foreign-boot'))
        with self.assertRaises(TraderError):self.kernel.wait_idle()
        self.assertTrue(self.kernel._failed)
        self.assertEqual(self.field('stop_count'),0)
        self.assertEqual(self.field('trade_count'),1)
        sent=len(self.port.orders)
        with self.assertRaises(TraderError):self.kernel.arm_development()
        self.assertEqual(len(self.port.orders),sent)

    def test_closure_and_nominal_updates_roll_back_together(self):
        self.start();self.open();self.exit_fill();submit=self.kernel.ledger.submit
        def fail(kind,event,payload,mutate=None,**kwargs):
            if payload.get('cycle',{}).get('lifecycle')=='CLOSED':
                original=mutate
                def mutation(conn,seq):
                    if original:original(conn,seq)
                    conn.execute('INSERT INTO statistics_missing_table VALUES (1)')
                mutate=mutation
            return submit(kind,event,payload,mutate,**kwargs)
        with patch.object(self.kernel.ledger,'submit',side_effect=fail):
            self.close(wait=False)
            with self.assertRaises(TraderError):self.kernel.wait_idle()
        self.assertTrue(self.kernel._failed)
        self.assertEqual(self.field('stop_count'),0)
        self.assertEqual(self.field('trade_count'),1)
        sent=len(self.port.orders)
        with self.assertRaises(TraderError):self.kernel.arm_development()
        self.assertEqual(len(self.port.orders),sent)

    def test_duplicate_close_and_restart_do_not_add_another_receipt(self):
        self.start();self.stop_cycle();before=copy.deepcopy(self.snap())
        self.close();self.assertEqual(self.snap(),before)
        self.kernel.close();self.port=RecordingPort();self.kernel=self.new_kernel()
        self.assertEqual(self.snap(),before)
        self.assertEqual(self.port.orders,[])

    def test_local_nominal_day_stop_cannot_be_replaced_with_external_zero_loss(self):
        self.start();self.stop_cycle();self.stop_cycle()
        self.assertEqual(self.field('net_loss_points'),'32')
        self.assertFalse(self.snap()['stop_result']['allow_new_entries'])
        count=len(self.port.orders)
        self.kernel.arm_development();self.kernel.wait_idle();self.minute+=1
        report=self.observe()
        self.assertIsNone(report['child_order_id'],report)
        self.assertEqual(len(self.port.orders),count)
        self.assertFalse(self.snap()['stop_result']['allow_new_entries'])

    def test_stop_streak_gate_applies_to_continuation_without_disabling_ordinary_entries(self):
        self.start(max_net_loss_stops=100);self.stop_cycle();self.stop_cycle()
        result=self.snap()['stop_result']
        self.assertEqual(self.field('stop_streak'),2)
        self.assertTrue(result['allow_new_entries']);self.assertFalse(result['allow_continuation'])
        self.open(fill=0)
        self.assertEqual(self.field('trade_count'),4)

    def test_profile_change_cannot_create_a_new_daily_statistics_bucket(self):
        self.start(max_net_loss_stops=100);self.stop_cycle();before=copy.deepcopy(self.state())
        self.kernel.close();self.port=RecordingPort();self.kernel=self.new_kernel()
        self.kernel.reconcile_development_cycle(self.cid,close=True);self.kernel.wait_idle()
        self.start(stop=20,max_net_loss_stops=100)
        self.assertEqual(self.field('net_loss_points'),before['net_loss_points'])
        self.assertEqual(self.field('stop_count'),before['stop_count'])
        self.assertEqual(self.field('trade_count'),before['trade_count'])

    def test_directional_cooldown_survives_restart_and_allows_its_exact_deadline(self):
        self.start(max_net_loss_stops=100,stop_cooldown_enabled=True,stop_cooldown_minutes=5)
        self.stop_cycle()
        deadline=self.state()['cooldown_until']['LONG']
        self.assertEqual(datetime.fromisoformat(deadline),datetime.fromisoformat(self.day+'T01:11:01Z'))
        self.kernel.close();self.port=RecordingPort();self.kernel=self.new_kernel()
        self.kernel.reconcile_development_cycle(self.cid,close=True);self.kernel.wait_idle()
        self.runtime=DsvStrategyRuntime(self.kernel,profile=self.profile);self.runtime.activate();self.kernel.wait_idle()
        self.assertEqual(self.state()['cooldown_until']['LONG'],deadline)
        self.kernel.arm_development();self.kernel.wait_idle()
        report=self.observe(at=self.day+'T01:07:00Z')
        self.assertIsNone(report['child_order_id'],report)
        self.kernel.arm_development();self.kernel.wait_idle()
        report=self.observe(at=deadline)
        self.assertIsNotNone(report['child_order_id'],report)

    def test_disabled_cooldown_keeps_the_saved_minutes_dormant(self):
        self.start(max_net_loss_stops=100,stop_cooldown_enabled=False,stop_cooldown_minutes=5)
        self.stop_cycle()
        self.assertIsNone(self.state()['cooldown_until']['LONG'])
        self.kernel.arm_development();self.kernel.wait_idle()
        report=self.observe(at=self.day+'T01:07:00Z')
        self.assertIsNotNone(report['child_order_id'],report)

    def test_explicit_source_cooldown_reaches_its_deadline_without_realtime_waiting(self):
        self.start(max_net_loss_stops=100,stop_cooldown_enabled=True,stop_cooldown_minutes=5)
        self.stop_cycle()
        self.kernel.arm_development();self.kernel.wait_idle()
        blocked=self.observe(at=self.day+'T01:07:00Z')
        self.assertIsNone(blocked['child_order_id'],blocked)
        self.kernel.arm_development();self.kernel.wait_idle()
        allowed=self.observe(at=self.state()['cooldown_until']['LONG'])
        self.assertIsNotNone(allowed['child_order_id'],allowed)

    def test_consecutive_explicit_stopped_trading_days_lock_the_month_across_day_change(self):
        self.start(max_net_loss_stops=1,month_stop_days=4)
        for day in DAYS[:4]:
            self.stop_cycle(day=day)
        self.assertEqual(self.snap()['stop_result']['state']['month_stop'],'2026-11')
        count=len(self.port.orders);self.kernel.arm_development();self.kernel.wait_idle();self.minute+=1
        report=self.observe(day='2026-11-10')
        self.assertIsNone(report['child_order_id'],report)
        self.assertEqual(len(self.port.orders),count)


if __name__=='__main__':unittest.main()
