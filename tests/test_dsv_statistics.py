"""Source-derived statistics do not mistake quantity, targets or cash for points."""

import copy
from decimal import Decimal
import unittest
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.strategy.statistics import initial_state, apply_event, rollover_day, cooldown_gate
from online_trader.strategy.stops import evaluate_stops


class DsvStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.profile=load_confirmed_baseline()[0]
        self.state=initial_state('DEV:stats','DCE.p2701','2026-11-09')
        self.ordinal=0

    def profile_with(self, **trade):
        settings=self.profile.to_settings();settings['trade'].update(trade)
        return DsvProfile.from_settings(settings)

    def entry(self, *, profile=None, direction='LONG', kind='NORMAL', requested=5, confirmed=1, **changes):
        profile=profile or self.profile;self.ordinal+=1
        row=next(r for r in profile.to_record()['settings']['symbols'] if r['code']=='DCE.p2701')
        event={'type':'ENTRY_CONFIRMED','event_id':f'entry-event-{self.ordinal}',
            'evidence_id':f'entry-fill-{self.ordinal}','account_key':'DEV:stats','contract_id':'DCE.p2701',
            'trading_day':self.state['trading_day'],'position_cycle_id':f'cycle-{self.ordinal}',
            'plan_id':f'plan-{self.ordinal}','plan_hash':f'{self.ordinal:064x}',
            'profile_hash':profile.content_hash,'direction':direction,'entry_kind':kind,
            'theoretical_entry_price':'1000','stop_distance':row['stop_offset'],
            'take_profit_distance':row['profit_offset'],'requested_quantity':requested,'confirmed_quantity':confirmed}
        event.update(changes);return event

    def close(self, entry, reason='STOP_LOSS', *, price='980', slip=None, **changes):
        event={k:copy.deepcopy(v) for k,v in entry.items() if k not in {'requested_quantity','confirmed_quantity'}}
        event.update(type='CYCLE_CLOSED',event_id='close-'+entry['event_id'],evidence_id='flat-'+entry['evidence_id'],
            flat_confirmed=True,orders_terminal=True,exit_reason=reason,nominal_exit_price=price,
            adverse_slippage_points=slip,closed_at_utc='2026-11-09T02:01:00Z',exit_triggered_at_utc=None)
        event.update(changes);return event

    def apply(self,event,profile=None):
        result=apply_event(profile or self.profile,self.state,event);self.state=result['state'];return result

    def cycle(self, reason='STOP_LOSS', *, profile=None,direction='LONG',price='980',kind='NORMAL',**changes):
        entry=self.entry(profile=profile,direction=direction,kind=kind)
        self.apply(entry,profile)
        return self.apply(self.close(entry,reason,price=price,**changes),profile)

    def test_first_confirmed_entry_counts_one_action_and_requested_target_volume(self):
        result=self.apply(self.entry(requested=7,confirmed=1))
        self.assertEqual((self.state['trade_count'],self.state['total_volume']),(1,7))
        self.assertEqual(self.state['daily_pnl_points'],'0')
        self.assertEqual(self.state['net_loss_points'],'0')
        self.assertIn('SOURCE_TOTAL_VOLUME_IS_REQUESTED_TARGET_NOT_CONFIRMED_FILL_VOLUME',result['source_notes'])

    def test_stop_counts_fixed_s_while_daily_points_use_trigger_quote_without_quantity_weight(self):
        self.cycle(price='975')
        self.assertEqual((self.state['daily_pnl_points'],self.state['net_loss_points']),('-25','16'))
        self.assertEqual((self.state['trade_count'],self.state['stop_count'],self.state['stop_streak']),(2,1,1))
        self.assertEqual(self.state['total_volume'],5)

    def test_take_profit_uses_fixed_t_for_net_loss_and_actual_nominal_quote_for_daily(self):
        self.cycle('TAKE_PROFIT',price='1063')
        self.assertEqual((self.state['daily_pnl_points'],self.state['net_loss_points']),('63','-50'))
        self.assertEqual((self.state['profit_count'],self.state['stop_count'],self.state['trade_count']),(1,0,2))

    def test_trailing_and_protected_floor_subtract_their_nominal_pf_not_fixed_t(self):
        self.cycle('TRAILING_STOP',price='1037')
        self.cycle('PROTECTED_FLOOR',price='1025')
        self.assertEqual((self.state['daily_pnl_points'],self.state['net_loss_points']),('62','-62'))
        self.assertEqual(self.state['profit_count'],2)

    def test_break_even_retains_source_stop_tag_s_charge_even_at_zero_points(self):
        self.cycle('BREAK_EVEN',price='1000')
        self.assertEqual((self.state['daily_pnl_points'],self.state['net_loss_points']),('0','16'))
        self.assertEqual((self.state['stop_count'],self.state['stop_streak']),(1,1))

    def test_short_stop_and_profit_mirror_theoretical_anchor(self):
        self.cycle(direction='SHORT',price='1025')
        self.cycle('TRAILING_STOP',direction='SHORT',price='963')
        self.assertEqual((self.state['daily_pnl_points'],self.state['net_loss_points']),('12','-21'))

    def test_profit_offsets_later_stops_without_clamping_nominal_loss_to_zero(self):
        self.cycle('TAKE_PROFIT',price='1050')
        for _ in range(3):self.cycle()
        self.assertEqual(self.state['net_loss_points'],'-2')
        self.assertEqual(self.state['stop_streak'],3)

    def test_nominal_stop_reducer_receives_source_points_as_is_and_uses_equality(self):
        self.cycle();self.cycle()
        result=evaluate_stops(self.profile,'DCE.p2701','2026-11-09',
            net_loss_points=self.state['net_loss_points'],stop_streak=self.state['stop_streak'],
            continuation_stopped=False,day_stopped=False,day_stopped_reason=None,stop_days=[],month_stop=None,
            trading_days=['2026-11-04','2026-11-05','2026-11-06','2026-11-09'])
        self.assertEqual(self.state['net_loss_points'],'32')
        self.assertFalse(result['allow_new_entries'])
        self.assertIn('NET_LOSS_LIMIT',result['reasons'])

    def test_session_close_changes_points_but_not_close_count_profit_or_streak(self):
        self.cycle();entry=self.entry();self.apply(entry)
        before=copy.deepcopy(self.state)
        self.apply(self.close(entry,'SESSION_CLOSE',price='1011'))
        self.assertEqual(self.state['net_loss_points'],'5')
        self.assertEqual(self.state['daily_pnl_points'],'-9')
        for key in ('trade_count','stop_count','profit_count','stop_streak'):
            self.assertEqual(self.state[key],before[key])

    def test_calendar_exit_without_price_finishes_after_native_flat_without_inventing_points(self):
        self.cycle(price='975')
        entry = self.entry()
        self.apply(entry)
        closed = self.close(entry, 'SESSION_CLOSE', price=None)
        with self.assertRaises(TraderError):
            self.apply(closed)
        closed['unpriced_calendar_exit_id'] = 'e' * 64
        proof = self.apply(closed)
        self.assertEqual(self.state['daily_pnl_points'], '-25')
        self.assertEqual(self.state['net_loss_points'], '16')
        self.assertEqual(self.state['trade_count'], 3)
        self.assertIsNone(proof['receipt']['daily_pnl_delta_points'])
        self.assertIsNone(proof['receipt']['net_loss_delta_points'])
        self.assertIn('SESSION_CLOSE_REFERENCE_UNAVAILABLE', proof['source_notes'])
        self.assertTrue(self.apply(closed)['duplicate'])

    def test_slippage_only_changes_net_loss_and_gap_has_zero_delta(self):
        for reason,slip in (('ADVERSE_SLIPPAGE','7.25'),('DATA_GAP_EXIT',None)):
            entry=self.entry();self.apply(entry);before=copy.deepcopy(self.state)
            self.apply(self.close(entry,reason,price=None,slip=slip))
            for key in ('daily_pnl_points','trade_count','stop_count','profit_count','stop_streak'):
                self.assertEqual(self.state[key],before[key])
        self.assertEqual(self.state['net_loss_points'],'7.25')

    def test_manual_sign_and_unit_repair_preserves_source_action_counts(self):
        self.cycle();entry=self.entry();self.apply(entry)
        result=self.apply(self.close(entry,'MANUAL_EXIT',price='1012'))
        self.assertEqual((self.state['daily_pnl_points'],self.state['net_loss_points']),('-8','4'))
        self.assertEqual((self.state['trade_count'],self.state['stop_count'],self.state['profit_count'],self.state['stop_streak']),(4,2,0,1))
        self.assertIn('MANUAL_NOMINAL_SIGN_AND_UNIT_REPAIRED',result['source_notes'])
        self.assertNotIn('realized_currency',self.state)

    def test_continuation_stop_with_flag_stops_all_new_entries_for_day(self):
        self.cycle(kind='CONTINUATION')
        self.assertTrue(self.state['continuation_stopped'])
        self.assertTrue(self.state['day_stopped'])
        self.assertEqual(self.state['day_stopped_reason'],'延续开止损后当日停开')

    def test_disabled_continuation_day_stop_keeps_only_the_streak_fact(self):
        self.cycle(profile=self.profile_with(cont_stop_day_stop=False),kind='CONTINUATION')
        self.assertFalse(self.state['day_stopped']);self.assertFalse(self.state['continuation_stopped'])
        self.assertEqual(self.state['stop_streak'],1)

    def test_same_day_new_profile_cannot_reset_old_counts_and_points(self):
        self.cycle()
        settings=self.profile.to_settings()
        next(row for row in settings['symbols'] if row['code']=='DCE.p2701')['stop_offset']=20
        changed=DsvProfile.from_settings(settings)
        self.cycle(profile=changed)
        self.assertEqual((self.state['net_loss_points'],self.state['trade_count'],self.state['total_volume']),('36',4,10))

    def test_exact_and_renamed_evidence_retries_are_one_economic_event(self):
        event=self.entry();first=self.apply(event);before=copy.deepcopy(self.state)
        for retried in (event,event|{'event_id':'retry-entry','evidence_id':'new-query-proof'}):
            result=self.apply(retried)
            self.assertTrue(result['duplicate']);self.assertEqual(self.state,before)
            self.assertEqual(result['receipt'],first['receipt'])

    def test_changed_cycle_phase_content_conflicts_and_does_not_mutate_state(self):
        entry=self.entry();self.apply(entry);closed=self.close(entry);self.apply(closed)
        before=copy.deepcopy(self.state)
        with self.assertRaises(TraderError) as caught:self.apply(closed|{'nominal_exit_price':'979'})
        self.assertEqual(caught.exception.code,'E_DUPLICATE_CONFLICT');self.assertEqual(self.state,before)

    def test_scope_profile_and_original_plan_binding_are_strict(self):
        entry=self.entry();self.apply(entry)
        for changed in ({'account_key':'DEV:other'},{'trading_day':'2026-11-10'},
                        {'profile_hash':'f'*64},{'plan_id':'different-plan'}):
            with self.subTest(changed=changed),self.assertRaises(TraderError):
                self.apply(self.close(entry)|changed)

    def test_flat_and_terminal_flags_cannot_be_replaced_by_send_return(self):
        entry=self.entry();self.apply(entry);before=copy.deepcopy(self.state)
        for changed in ({'flat_confirmed':False},{'orders_terminal':False}):
            with self.subTest(changed=changed),self.assertRaises(TraderError) as caught:
                self.apply(self.close(entry)|changed)
            self.assertEqual(caught.exception.code,'E_FACT_BARRIER')
        self.assertEqual(self.state,before)

    def test_close_requires_original_entry_confirmation(self):
        with self.assertRaises(TraderError) as caught:self.apply(self.close(self.entry()))
        self.assertEqual(caught.exception.code,'E_FACT_BARRIER')

    def test_no_fill_terminal_cycle_does_not_count_and_cannot_gain_an_unreviewed_late_entry(self):
        entry=self.entry();result=self.apply(self.close(entry,'NO_FILL',price=None))
        self.assertEqual((self.state['trade_count'],self.state['total_volume'],self.state['net_loss_points']),(0,0,'0'))
        self.assertIn('NO_CONFIRMED_ENTRY_NO_STATISTICS_DELTA',result['source_notes'])
        with self.assertRaises(TraderError):self.apply(entry)

    def test_cooldown_disabled_preserves_saved_minutes_without_enabling_it(self):
        self.cycle()
        self.assertEqual(self.state['cooldown_until'],{'LONG':None,'SHORT':None})
        self.assertFalse(cooldown_gate(self.state,direction='LONG',now_utc='2026-11-09T02:01:00Z')['blocked'])

    def test_cooldown_uses_trigger_clock_is_directional_and_includes_no_extra_boundary_tick(self):
        profile=self.profile_with(stop_cooldown_enabled=True)
        self.cycle(profile=profile,exit_triggered_at_utc='2026-11-09T02:00:00Z')
        self.assertEqual(self.state['cooldown_until']['LONG'],'2026-11-09T02:05:00.000000Z')
        self.assertTrue(cooldown_gate(self.state,direction='LONG',now_utc='2026-11-09T02:04:59.999999Z')['blocked'])
        self.assertFalse(cooldown_gate(self.state,direction='LONG',now_utc='2026-11-09T02:05:00Z')['blocked'])
        self.assertFalse(cooldown_gate(self.state,direction='SHORT',now_utc='2026-11-09T02:01:00Z')['blocked'])

    def test_new_opposite_stop_replaces_the_source_single_direction_cooldown(self):
        profile=self.profile_with(stop_cooldown_enabled=True)
        self.cycle(profile=profile)
        self.cycle(profile=profile,direction='SHORT',price='1020')
        self.assertIsNone(self.state['cooldown_until']['LONG'])
        self.assertEqual(self.state['cooldown_direction'],'SHORT')

    def test_monotonic_elapsed_can_expire_the_original_cooldown_without_wall_clock_inference(self):
        self.cycle(profile=self.profile_with(stop_cooldown_enabled=True))
        self.assertTrue(cooldown_gate(self.state,direction='LONG',now_utc='2026-11-09T01:00:00Z',elapsed_ms=299999)['blocked'])
        ready=cooldown_gate(self.state,direction='LONG',now_utc='2026-11-09T01:00:00Z',elapsed_ms=300000)
        self.assertFalse(ready['blocked']);self.assertEqual(ready['clock_basis'],'MONOTONIC_ELAPSED')

    def test_fractional_duration_rounds_utc_up_instead_of_expiring_early(self):
        profile=self.profile_with(stop_cooldown_enabled=True,stop_cooldown_minutes=Decimal('0.000000000001'))
        result=self.cycle(profile=profile)
        self.assertIn('COOLDOWN_UTC_DEADLINE_ROUNDED_UP_TO_MICROSECOND',result['source_notes'])
        self.assertTrue(cooldown_gate(self.state,direction='LONG',now_utc='2026-11-09T02:01:00Z')['blocked'])
        self.assertFalse(cooldown_gate(self.state,direction='LONG',now_utc='2026-11-09T02:01:00.000001Z')['blocked'])

    def test_explicit_day_rollover_resets_daily_counts_but_preserves_original_cooldown(self):
        self.cycle(profile=self.profile_with(stop_cooldown_enabled=True),kind='CONTINUATION')
        before=copy.deepcopy(self.state);rolled=rollover_day(self.state,'2026-11-10')
        self.assertEqual(rolled['cooldown_until'],before['cooldown_until'])
        self.assertEqual((rolled['trade_count'],rolled['stop_streak'],rolled['net_loss_points']),(0,0,'0'))
        self.assertFalse(rolled['day_stopped']);self.assertEqual(rolled['receipts'],{})
        self.assertEqual(rollover_day(self.state,'2026-11-09'),self.state)
        self.assertEqual(self.state,before)
        with self.assertRaises(TraderError):rollover_day(self.state,'2026-11-08')

    def test_utc_close_date_does_not_silently_change_the_cabinet_day(self):
        self.cycle(closed_at_utc='2026-11-10T00:01:00Z')
        self.assertEqual(self.state['trading_day'],'2026-11-09')

    def test_float_unknown_fields_and_impossible_trigger_order_are_refused(self):
        entry=self.entry()
        for changed in ({'requested_quantity':True},{'confirmed_quantity':0},{'confirmed_quantity':6},
                        {'theoretical_entry_price':1000.0},{'broker_account_password':'must-not-be-copied'}):
            with self.subTest(changed=changed),self.assertRaises(TraderError):self.apply(entry|changed)
        self.apply(entry)
        with self.assertRaises(TraderError):
            self.apply(self.close(entry,exit_triggered_at_utc='2026-11-09T02:02:00Z'))

    def test_receipt_capacity_never_evicts_old_identity_and_retries_still_work(self):
        with patch('online_trader.strategy.statistics.MAX_RECEIPTS',2):
            entry=self.entry();self.apply(entry);closed=self.close(entry);self.apply(closed)
            self.assertTrue(self.apply(closed)['duplicate'])
            with self.assertRaises(TraderError) as caught:self.apply(self.entry())
            self.assertEqual(caught.exception.code,'E_QUEUE_OVERFLOW')

    def test_pure_results_are_deep_copies_and_ot_cj_safe(self):
        event=self.entry();before=copy.deepcopy(self.state);result=apply_event(self.profile,self.state,event)
        self.assertEqual(self.state,before)
        event['requested_quantity']=99
        self.assertEqual(result['receipt']['requested_quantity'],5)
        canonical_bytes(result)


if __name__=='__main__':unittest.main()
