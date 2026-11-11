"""Actual product DSV simulation chain with externally injected native facts."""
from pathlib import Path
import copy
import json
import tempfile
import unittest
import uuid
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.kernel import Kernel
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.dsv_simulation import DsvSimulationSession
from online_trader.scheduling.calendar import TradingCalendar
from qa.dsv_native_facts import FactClock,NativeFactApi


def scope_for(path,profile,calendar,**changes):
    value={'schema_version':'KQ_DSV_SIM/1','grant_id':'offline-dsv-'+uuid.uuid4().hex,'account_fingerprint':uuid.uuid4().hex[:16],
        'ledger_path':str(path.resolve()),'contracts':['DCE.p2701'],'trading_day':'2026-11-10',
        'not_before_utc':'2026-11-10T01:00:00Z','not_after_utc':'2026-11-10T07:00:00Z',
        'max_order_quantity':10,'opening_attempts':20,'risk_budget':'1000000','profile_hash':profile.content_hash,
        'calendar_hash':calendar.content_hash,'authorization_reference':'OFFLINE_INJECTED_FACTS_NO_ACCOUNT_AUTHORITY'}
    value.update(changes);return DsvSimulationGrant.parse(canonical_bytes(value))


def day_calendar(code='DCE.p2701'):
    return TradingCalendar.from_record({'schema_version':'TRADING_CALENDAR/1','calendar_id':'offline-dsv-day','calendar_version':'1',
        'complete':True,'coverage_from_utc':'2026-11-10T00:00:00Z','coverage_to_utc':'2026-11-10T08:00:00Z',
        'contracts':[{'contract_id':code,'sessions':[{'session_id':'day','trading_day':'2026-11-10',
            'open_utc':'2026-11-10T01:00:00Z','close_utc':'2026-11-10T07:00:00Z','is_opening_session':True,'force_close_at_end':True}]}]})


class OnlineDsvTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.db';self.kernel=None;self.session=None
        self.clock=FactClock();self.calendar=day_calendar()

    def tearDown(self):
        if self.session:self.session.close()
        if self.kernel:
            try:self.kernel.close()
            except TraderError:
                if not self.kernel._failed:raise
        self.temp.cleanup()

    def start(self,*,trade=None,quantity=5,grant_changes=None,history=True,code='DCE.p2701',max_quote_age_ms=30_000):
        self.code=code;self.calendar=day_calendar(code)
        settings=load_confirmed_baseline()[0].to_settings()
        settings['trade'].update(protect_80_enabled=False,half_close_enabled=True,half_stage2=True,half_breakeven=True,
                                 trailing_activate_ratio=0,entry_slip_abort=False,no_chase_after_act=False)
        if trade:settings['trade'].update(trade)
        next(s for s in settings['symbols'] if s['code']==code)['order_volume']=quantity
        self.profile=DsvProfile.from_settings(settings)
        self.grant=scope_for(self.path,self.profile,self.calendar,**({'contracts':[code]}|(grant_changes or {})))
        self.api=NativeFactApi(self.clock,(code,));self.port=TqKqDsvPort(self.api,self.api._account,self.grant,self.clock)
        self.port.configure_calendar(self.calendar)
        self.kernel=Kernel(self.path,self.grant.account_key,port=self.port,simulation_grant=self.grant,
                           risk_budget='1000000',max_quote_age_ms=max_quote_age_ms)
        self.kernel.simulation.initialize()
        self.session=DsvSimulationSession(self.kernel,Path(self.temp.name)/'session',profile=self.profile,
                                           calendar=self.calendar,clock=self.clock,emit=lambda event:None)
        if history:self.api.tick(code,at='2026-11-10T01:00:00Z',last=1040)
        self.api.tick(code,last=1040)
        self.session.prime();self.kernel.simulation.arm();self.kernel.wait_idle()

    def quote(self,price=1055,*,milliseconds=1000,**values):
        self.clock.advance(milliseconds);self.api.tick(self.code,last=price,**values);self.session.step()
        self.assertNotIn('DSV_BLOCKED',self.session.phase or '',self.session.phase)

    def cycle(self):return next(iter(self.kernel._staged.values()))

    def entered(self,**options):
        self.start(**options);self.quote()
        self.assertEqual(len(self.api.inserted),1,self.session.last_decisions)
        self.api.fill(self.api.inserted[0]['order_id'],self.api.inserted[0]['volume'],1055)
        self.session.step();self.assertEqual(self.cycle()['protection']['owned_qty'],self.api.inserted[0]['volume'])
        return self.cycle()

    def finish_last(self,price):
        order=self.api.inserted[-1];self.api.fill(order['order_id'],order['volume'],price);self.session.step()

    def restart(self,*,history=True):
        original_api=self.api;self.session.close();self.kernel.close()
        api=NativeFactApi(self.clock)
        for name in ('orders','trades','positions','quotes','serials'):setattr(api,name,copy.deepcopy(getattr(original_api,name)))
        if not history:
            for serial in api.serials.values():serial.rows=serial.rows[-1:]
        self.api=api;self.port=TqKqDsvPort(api,api._account,self.grant,self.clock);self.port.configure_calendar(self.calendar)
        self.kernel=Kernel(self.path,self.grant.account_key,port=self.port,simulation_grant=self.grant,risk_budget='1000000')
        self.kernel.simulation.initialize()
        self.session=DsvSimulationSession(self.kernel,Path(self.temp.name)/('restart-'+uuid.uuid4().hex),
            profile=self.profile,calendar=self.calendar,clock=self.clock,emit=lambda event:None)
        self.session.prime();self.session.step()

    def test_full_online_scope_signal_reaches_original_oms_with_no_development_label(self):
        self.start();self.quote()
        self.assertEqual(len(self.api.inserted),1,self.session.last_decisions)
        cycle=self.cycle();self.assertEqual(cycle['entry_origin'],'STRATEGY')
        self.assertEqual(cycle['plan']['schema_version'],'1.3')
        self.assertEqual(cycle['plan']['execution_scope'],'ONLINE_SIMULATION')
        self.assertFalse(self.kernel.development);self.assertEqual(cycle['orders'][0]['original_qty'],5)
        self.assertEqual(self.kernel.ledger.query('SELECT origin FROM order_intents')[0]['origin'],'STRATEGY')

    def test_slow_startup_replays_new_ticks_before_registering_a_fresh_quote(self):
        prime_path = DsvSimulationSession._prime_path

        def slow_initial_replay(session, code, events, *, extend=False):
            tail = prime_path(session, code, events, extend=extend)
            if not extend:
                self.clock.advance(12000)
                poll = session.port.poll_updates
                polls = 0

                def delayed_updates(seconds):
                    nonlocal polls
                    polls += 1
                    if polls == 2:
                        self.api.tick(code, at='2026-11-10T01:06:06Z', last=1040)
                        self.api.tick(code, last=1041)
                    return poll(seconds)

                session.port.poll_updates = delayed_updates
            return tail

        with patch.object(DsvSimulationSession, '_prime_path', slow_initial_replay):
            self.start(max_quote_age_ms=5000)
        self.assertEqual(self.api.inserted, [])
        key = self.code, self.grant.value['trading_day']
        strategy = self.session.driver.strategy
        self.assertEqual(strategy._latest[key]['event']['last'], '1041')
        self.assertEqual(strategy._states[key]['last_sequence'], 4)
        self.assertFalse(strategy._states[key]['coverage_lost'])
        observed = self.kernel.ledger.query('SELECT observed_from_utc FROM observation_windows')
        self.assertTrue(observed[0]['observed_from_utc'].startswith('2026-11-10T01:00:00'))

    def test_multilot_half_stage_two_and_confirmed_reduction_breakeven(self):
        self.start();self.quote();cycle=self.cycle();oid=self.port.order_id(cycle['orders'][0]['child_order_id'])
        self.api.fill(oid,5,1055);self.session.step()
        self.quote(1086,high=1090)
        self.assertEqual(len(self.api.inserted),2,self.cycle())
        self.assertEqual(self.api.inserted[-1]['volume'],2)
        self.api.fill(self.api.inserted[-1]['order_id'],2,1086);self.session.step()
        self.assertEqual(self.cycle()['protection']['break_even_status'],'ACTIVE')
        self.quote(1096,high=1100)
        self.assertEqual(self.api.inserted[-1]['volume'],2)
        self.finish_last(1096)
        self.assertEqual(self.cycle()['protection']['owned_qty'],1)
        self.quote(1055,high=1100)
        self.assertEqual(self.api.inserted[-1]['volume'],1)
        self.finish_last(1055)
        self.assertEqual(self.cycle()['lifecycle'],'CLOSED')
        self.assertEqual(self.kernel.money.report()['summary']['exit_quantity'],5)

    def test_fixed_stop_protects_first_partial_before_entry_terminal(self):
        self.start();self.quote();self.api.fill(self.api.inserted[0]['order_id'],1,1055)
        self.session.step();self.quote(1038)
        self.assertEqual(self.api.inserted[-1]['volume'],1)
        self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'],0)
        self.assertTrue(self.api.cancelled)
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,4)

    def test_fixed_take_profit_closes_full_owned_quantity(self):
        self.entered(trade={'half_close_enabled':False});self.quote(1106,high=1110)
        self.assertEqual(self.api.inserted[-1]['volume'],5)
        self.finish_last(1106);self.assertEqual(self.cycle()['lifecycle'],'CLOSED')
        self.assertIn('TAKE_PROFIT',self.cycle()['protection']['exit_reasons'])

    def test_eighty_percent_protection_suppresses_half_then_exits_at_floor(self):
        self.entered(trade={'protect_80_enabled':True})
        self.quote(1096,high=1100)
        self.assertEqual(self.cycle()['protection']['mode'],'PROTECTED')
        self.assertEqual(len(self.api.inserted),1)
        self.quote(1079,high=1100)
        self.assertEqual(self.api.inserted[-1]['volume'],5)
        self.finish_last(1079)
        self.assertIn('PROTECTED_FLOOR',self.cycle()['protection']['exit_reasons'])

    def test_independent_trailing_uses_original_peak_and_saved_drawdown(self):
        self.entered(trade={'half_close_enabled':False,'trailing_activate_ratio':0.5,'trailing_pullback_ratio':0.2})
        self.quote(1086);self.assertEqual(len(self.api.inserted),1)
        self.quote(1075)
        self.assertEqual(self.api.inserted[-1]['volume'],5)
        self.assertIn('TRAILING_STOP',self.cycle()['protection']['exit_reasons'])

    def test_adverse_slippage_routes_to_same_original_exit(self):
        self.start(trade={'entry_slip_abort':True});self.quote()
        self.api.fill(self.api.inserted[0]['order_id'],5,1080);self.session.step()
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'],0)
        self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(self.api.inserted[-1]['volume'],5)

    def test_duplicate_native_quote_never_consumes_second_half_stage(self):
        self.entered();self.quote(1096,high=1100);self.finish_last(1096)
        count=len(self.api.inserted)
        self.session.step();self.session.step()
        self.assertEqual(len(self.api.inserted),count)
        self.assertEqual(self.cycle()['protection']['owned_qty'],3)

    def test_half_waits_for_cancel_terminal_and_late_entry_details(self):
        self.start();self.quote();oid=self.api.inserted[0]['order_id']
        self.api.fill(oid,2,1055);self.session.step();self.quote(1086)
        self.assertEqual(len(self.api.inserted),1)
        self.assertEqual(self.cycle()['protection']['phase1_status'],'WAITING_ENTRY_TERMINAL')
        self.api.fill(oid,1,1055);self.api.terminal(oid);self.session.step()
        self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(self.api.inserted[-1]['volume'],2)
        self.assertEqual(self.cycle()['protection']['q0'],3)
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,0)

    def test_zero_fill_cancel_closes_without_replacing_or_renewing_scope(self):
        self.start(grant_changes={'opening_attempts':1});self.quote();child=self.cycle()['orders'][0]['child_order_id']
        self.kernel.request_cancel(child);self.kernel.wait_idle()
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,5)
        self.api.terminal(self.port.order_id(child));self.session.step()
        self.assertEqual(self.cycle()['lifecycle'],'CLOSED')
        self.assertEqual(self.kernel.ledger.query('SELECT * FROM fill_facts'),[])
        with self.assertRaises(TraderError):self.kernel.simulation.arm()
        self.quote(milliseconds=60000);self.assertEqual(len(self.api.inserted),1)

    def test_unknown_send_found_alive_is_proved_without_resending(self):
        self.start();original=self.api.insert_order
        def uncertain(**values):original(**values);raise OSError('response lost')
        with patch.object(self.api,'insert_order',side_effect=uncertain):self.quote()
        self.session.step()
        self.assertFalse(self.kernel._has_unknown());self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(len(self.api.inserted),1)
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,5)
        self.assertEqual(self.cycle()['orders'][0]['broker_state'],'WORKING')

    def test_unknown_send_absent_from_native_snapshot_keeps_all_quantity(self):
        self.start()
        with patch.object(self.api,'insert_order',side_effect=OSError('uncertain')):self.quote()
        self.session.step();self.assertTrue(self.kernel._has_unknown())
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,5)
        self.assertEqual(self.api.inserted,[])
        with self.assertRaises(TraderError):self.kernel.simulation.arm()

    def test_disconnect_reconnect_preserves_protection_without_rearming(self):
        self.entered();plan=copy.deepcopy(self.cycle()['plan'])
        self.api._data['notify']['disconnect']={'code':2019112910};self.session.step()
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.api._data['notify']['reconnect']={'code':2019112902};self.session.step()
        self.assertFalse(self.kernel.snapshot()['armed']);self.assertEqual(self.cycle()['plan'],plan)
        self.quote(1038);self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(self.api.inserted[-1]['volume'],5)

    def test_calendar_close_is_clock_driven_with_original_price_escalation(self):
        self.entered(trade={'half_close_enabled':False})
        target=__import__('datetime').datetime.fromisoformat('2026-11-10T06:59:39Z')
        self.quote(1055,milliseconds=int((target-self.clock.now()).total_seconds()*1000))
        self.assertEqual(len(self.api.inserted),1)
        observed=self.cycle()['protection']['last_observed_event_seq']
        self.clock.advance(1000);self.session.step()
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'],0)
        self.assertIn('SESSION_CLOSE',self.cycle()['reasons'])
        self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(self.cycle()['protection']['last_observed_event_seq'],observed)

    def test_stale_quote_gap_retains_owned_quantity_and_requests_reconciliation(self):
        self.entered();self.clock.advance(181000);self.session.step()
        self.assertEqual(self.cycle()['protection']['owned_qty'],5)
        self.assertTrue(self.kernel.recovery_required)
        self.assertIn('ALERT_AND_TAKEOVER',self.cycle()['reasons'])
        self.assertEqual(len(self.api.inserted),1)

    def test_pause_keeps_half_protection_and_never_adds_old_manual_deadline(self):
        self.entered();self.kernel.pause();self.kernel.wait_idle();self.quote(1086)
        self.assertEqual(self.api.inserted[-1]['volume'],2)
        self.assertNotIn('online_deadline',self.cycle())
        self.assertFalse(self.kernel.snapshot()['armed'])

    def test_held_restart_with_new_adapter_keeps_original_plan_and_exit_target(self):
        self.entered();self.quote(1086);self.finish_last(1086)
        plan=copy.deepcopy(self.cycle()['plan']);state=copy.deepcopy(self.cycle()['protection']);boot=self.kernel.boot_id
        self.restart()
        self.assertNotEqual(self.kernel.boot_id,boot);self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.api.inserted,[]);self.assertEqual(self.cycle()['plan'],plan)
        for field in ('owned_qty','max_favorable_move','phase1_status','phase1_target','break_even_status'):
            self.assertEqual(self.cycle()['protection'][field],state[field])
        self.quote(1055);self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(self.api.inserted[-1]['volume'],3)

    def test_incomplete_entry_history_after_restart_does_not_disable_owned_stop(self):
        self.entered();self.restart(history=False)
        self.quote(1038)
        self.assertIn('MARKET_PATH_INCOMPLETE',self.session.last_decisions['DCE.p2701']['reasons'])
        self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.assertEqual(self.api.inserted[-1]['volume'],5)
        self.assertFalse(self.kernel.snapshot()['armed'])

    def test_unknown_absent_attempt_survives_new_adapter_and_is_never_replayed(self):
        self.start()
        with patch.object(self.api,'insert_order',side_effect=OSError('uncertain')):self.quote()
        self.restart();self.assertTrue(self.kernel._has_unknown())
        self.assertEqual(self.api.inserted,[])
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,5)

    def test_late_join_without_a_complete_native_path_cannot_open(self):
        self.start(history=False);self.quote()
        self.assertEqual(self.api.inserted,[])
        self.assertIn('MARKET_PATH_INCOMPLETE',self.session.last_decisions['DCE.p2701']['reasons'])

    def test_native_tick_gap_blocks_entries_without_inventing_missing_ticks(self):
        self.start();self.quote(index=100)
        self.assertEqual(self.api.inserted,[])
        self.assertIn('MARKET_PATH_INCOMPLETE',self.session.last_decisions['DCE.p2701']['reasons'])

    def test_foreign_position_change_before_ack_prevents_native_send(self):
        self.start();self.clock.advance(1000);self.api.tick(last=1055)
        event=self.session.feed.batches()[0][1][-1]
        account,instrument=self.session.feed.facts(event,self.kernel)
        future=self.session.driver.on_market(event,account,instrument)
        self.kernel._pending[0].future.result(timeout=3)
        self.assertEqual(self.api.inserted,[])
        self.api.positions['DCE.p2701'].pos_long=1
        self.kernel.wait_idle();future.result()
        self.assertEqual(self.api.inserted,[])
        self.assertEqual(self.cycle()['orders'][0]['send_state'],'DEFINITELY_NOT_SENT')
        self.assertEqual(self.api.positions['DCE.p2701'].pos_long,1)

    def test_owner_rejects_wrong_commit_ack_before_native_send(self):
        from concurrent.futures import Future
        from dataclasses import replace
        self.start();self.clock.advance(1000);self.api.tick(last=1055)
        event=self.session.feed.batches()[0][1][-1];account,instrument=self.session.feed.facts(event,self.kernel)
        self.session.driver.on_market(event,account,instrument)
        pending=self.kernel._pending[0];ack=pending.future.result(timeout=3)
        held=Future();held.set_result(replace(ack,state_revision=ack.state_revision+1));pending.future=held
        self.kernel.poll();self.assertTrue(self.kernel._failed);self.assertEqual(self.api.inserted,[])

    def test_startup_arm_request_is_cancelled_by_an_operator_pause(self):
        self.start();self.kernel.pause();self.kernel.wait_idle()
        self.session.request_initial_arm();self.kernel.pause();self.kernel.wait_idle();self.quote()
        self.assertEqual(self.api.inserted,[]);self.assertFalse(self.kernel.snapshot()['armed'])

    def test_report_money_revisions_keep_native_snapshot_source_separate_from_quote_time(self):
        self.entered(trade={'half_close_enabled':False});self.quote(1106,high=1110);self.finish_last(1106)
        self.assertIsNone(self.kernel.money.error)
        report=self.kernel.money.report();self.assertEqual(report['summary']['entry_quantity'],5)
        self.assertEqual(report['summary']['exit_quantity'],5)
        self.assertIsNone(report['summary']['net_realized_pnl'])

    def test_short_strategy_and_owned_buy_close_use_the_same_native_chain(self):
        self.start();self.quote(1035)
        self.assertEqual(self.api.inserted[0]['direction'],'SELL')
        self.api.fill(self.api.inserted[0]['order_id'],5,1035);self.session.step()
        self.quote(1052)
        self.assertEqual(self.api.inserted[-1]['direction'],'BUY')
        self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.finish_last(1052);self.assertEqual(self.cycle()['lifecycle'],'CLOSED')

    def test_protected_day_stop_survives_full_close_and_next_candle(self):
        self.entered(trade={'protect_80_enabled':True,'protect_stop_day':True})
        self.quote(1096,high=1100);self.quote(1079,high=1100);self.finish_last(1079)
        self.assertTrue(self.cycle()['protection']['block_new_for_trading_day'])
        self.quote(1055,milliseconds=60000,high=1100)
        self.assertEqual(sum(o['offset']=='OPEN' for o in self.api.inserted),1)
        self.assertIn('PROTECTION_DAY_STOP',self.session.last_decisions['DCE.p2701']['reasons'])

    def test_second_native_cycle_does_not_reclassify_original_owned_orders_as_foreign(self):
        self.entered(trade={'half_close_enabled':False});self.quote(1106,high=1110);self.finish_last(1106)
        self.assertEqual(self.cycle()['lifecycle'],'CLOSED')
        self.quote(1055,milliseconds=60000,high=1090)
        self.assertEqual(sum(o['offset']=='OPEN' for o in self.api.inserted),2,self.session.last_decisions)
        self.api.fill(self.api.inserted[-1]['order_id'],5,1055);self.session.step()
        cycles=list(self.kernel._staged.values())
        self.assertEqual([c['protection']['owned_qty'] for c in cycles],[0,5])
        self.assertIsNone(self.kernel.money.error)

    def test_shfe_same_day_close_offset_is_committed_before_native_send(self):
        self.start(code='SHFE.bu2701');self.quote(1079)
        self.api.fill(self.api.inserted[0]['order_id'],5,1079);self.session.step()
        self.quote(1066)
        self.assertEqual(self.api.inserted[-1]['offset'],'CLOSETODAY')
        self.assertEqual(self.kernel.ledger.query("SELECT offset_kind FROM child_orders WHERE offset_kind!='OPEN'")[0]['offset_kind'],'CLOSE_TODAY')
        self.finish_last(1066);self.assertEqual(self.cycle()['lifecycle'],'CLOSED')
        self.assertEqual(self.kernel.ledger.query("SELECT offset_kind FROM fill_facts WHERE side='SELL'")[0]['offset_kind'],'CLOSE_TODAY')

    def test_czce_owned_reduction_uses_close_and_exact_callback_offset(self):
        self.start(code='CZCE.OI701');self.quote(1058)
        self.api.fill(self.api.inserted[0]['order_id'],5,1058);self.session.step();self.quote(1042)
        self.assertEqual(self.api.inserted[-1]['offset'],'CLOSE')
        self.finish_last(1042);self.assertEqual(self.cycle()['lifecycle'],'CLOSED')

    def test_shfe_historical_position_cannot_be_claimed_as_today_owned_quantity(self):
        self.start(code='SHFE.bu2701');self.quote(1079)
        self.api.fill(self.api.inserted[0]['order_id'],5,1079);self.session.step()
        position=self.api.positions['SHFE.bu2701'];position.pos_long_today=4;position.pos_long_his=1
        self.quote(1066)
        self.assertEqual(len(self.api.inserted),1)
        self.assertEqual(self.cycle()['protection']['owned_qty'],5)

    def test_active_foreign_order_blocks_owned_reduction_without_cancelling_it(self):
        from types import SimpleNamespace as NS
        self.entered();self.api.orders['foreign']=NS(exchange_id='DCE',instrument_id='p2701',direction='SELL',offset='CLOSE',
            volume_orign=1,volume_left=1,status='ALIVE',limit_price=1054.,trade_records={})
        self.quote(1038)
        self.assertEqual(len(self.api.inserted),1)
        self.assertEqual(self.api.orders['foreign'].status,'ALIVE')
        self.assertNotIn('foreign',self.api.cancelled)

    def test_cancel_timeout_requires_terminal_proof_and_cannot_be_cleared_by_alive_snapshot(self):
        self.start();self.quote();self.quote(1056)
        self.assertTrue(self.api.cancelled)
        self.quote(1056,milliseconds=31000)
        self.assertTrue(self.kernel.recovery_required)
        self.assertIn('CANCEL_TERMINAL_TIMEOUT',self.cycle()['execution']['last_reasons'])
        self.session.step();self.assertTrue(self.kernel.recovery_required)
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,5)
        self.api.terminal(self.api.inserted[0]['order_id']);self.session.step()
        self.assertFalse(self.kernel.recovery_required);self.assertFalse(self.kernel._armed)
        self.assertEqual(self.cycle()['lifecycle'],'CLOSED');self.assertEqual(len(self.api.inserted),1)

    def test_automatic_sizing_is_applied_before_the_explicit_grant_quantity_gate(self):
        self.start(quantity=0,grant_changes={'max_order_quantity':30});self.quote()
        report=self.session.last_decisions['DCE.p2701']
        self.assertEqual(report['status'],'ENTRY_COMMITTED')
        self.assertEqual(self.api.inserted[0]['volume'],report['risk']['quantity'])
        self.assertGreater(self.api.inserted[0]['volume'],1)

    def test_explicit_operator_arm_uses_the_original_strategy_policy(self):
        from types import SimpleNamespace as NS
        from tests.test_control import ControlTests
        self.start();self.kernel.pause();self.kernel.wait_idle()
        params={'rule_set_id':self.kernel._configuration['rule_set_id'],'config_revision':str(self.kernel._config_revision),
                'authority_policy_id':'sim-authority-'+self.grant.content_hash[:24]}
        message=ControlTests.message(NS(kernel=self.kernel,account=self.kernel.account_key),'ARM_ENTRIES',params)
        future=self.kernel.execute_command(message,'desktop');self.kernel.wait_idle()
        self.assertIn(future.result()['status'],('COMMITTED','SUCCEEDED'))
        receipt=self.kernel.control.get_status(message['body']['command_id'],message['body']['payload_hash']).result()
        self.assertEqual(receipt['status'],'SUCCEEDED');self.assertTrue(self.kernel._armed)
        self.quote();self.assertEqual(len(self.api.inserted),1)

    def test_late_fill_after_zero_fill_close_preserves_fact_and_requires_statistics_review(self):
        self.start();self.quote();oid=self.api.inserted[0]['order_id']
        self.api.terminal(oid);self.session.step();self.assertEqual(self.cycle()['lifecycle'],'CLOSED')
        original_close=self.cycle()['statistics_close_id']
        self.api.fill(oid,1,1055);self.session.step()
        self.assertTrue(self.cycle()['statistics_review_required'])
        self.assertEqual(self.cycle()['statistics_close_id'],original_close)
        self.assertEqual(self.cycle()['protection']['owned_qty'],1)
        self.quote(1038);self.assertEqual(self.api.inserted[-1]['volume'],1)
        self.finish_last(1038)
        self.assertEqual(self.cycle()['protection']['owned_qty'],0)
        self.assertEqual(self.cycle()['statistics_close_id'],original_close)
        self.assertNotEqual(self.cycle()['lifecycle'],'CLOSED')

    def test_accepted_native_tick_cannot_be_rewritten_or_used_to_refresh_old_price(self):
        self.start();self.api.serials['DCE.p2701'].rows[-1]['last_price']=1055.
        self.session.step()
        self.assertEqual(self.api.inserted,[]);self.assertFalse(self.kernel._armed)
        self.assertEqual(self.session.phase,'DSV_BLOCKED_E_DUPLICATE_CONFLICT')
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM event_journal WHERE event_type='ONLINE_DSV_FACT_BARRIER'")),1)

    def test_injected_native_api_does_not_claim_a_real_broker_connection(self):
        self.start();self.assertFalse(self.session.driver.strategy.status()['broker_connected'])
        self.assertFalse(self.port.broker_connected)

    def test_final_relogin_closes_sender_and_checks_fresh_full_owned_facts(self):
        from online_trader.runtime.dsv_simulation import final_relogin
        self.entered(trade={'half_close_enabled':False});self.quote(1106,high=1110);self.finish_last(1106)
        reader_api=NativeFactApi(self.clock)
        for name in ('orders','trades','positions','quotes','serials'):setattr(reader_api,name,copy.deepcopy(getattr(self.api,name)))
        reader=TqKqDsvPort(reader_api,reader_api._account,self.grant,self.clock)
        with patch.object(TqKqDsvPort,'connect',return_value=reader) as connect:
            self.assertTrue(final_relogin(self.kernel,self.session,'unused-offline-credentials'))
        self.assertTrue(connect.call_args.kwargs['read_only']);self.assertTrue(self.port._closed)
        result=json.loads((self.session.output/'final-relogin.json').read_text(encoding='utf-8'))
        self.assertTrue(result['test_owned_flat']);self.assertFalse(result['whole_account_flat_claimed'])
        self.assertEqual(reader_api.inserted,[]);self.assertEqual(reader_api.closed,1)

    def test_final_full_snapshot_rejects_same_volume_wrong_trade_price(self):
        from online_trader.runtime.dsv_simulation import verify_closed_snapshot
        self.entered(trade={'half_close_enabled':False});self.quote(1106,high=1110);self.finish_last(1106)
        value=self.port.snapshot();value['trades'][0]['price']='1057'
        with self.assertRaises(TraderError):verify_closed_snapshot(self.kernel,value)

    def test_zero_fill_reprice_waits_for_terminal_and_keeps_original_intent_open(self):
        self.start();self.quote();first=self.cycle()['orders'][0]
        self.quote(1056);self.assertEqual(len(self.api.inserted),1)
        self.assertEqual(OrderState.from_dict(self.cycle()['orders'][0]).potential_remaining,5)
        self.api.terminal(self.port.order_id(first['child_order_id']));self.session.step()
        self.assertNotEqual(self.cycle()['lifecycle'],'CLOSED');self.assertNotIn('statistics_close_id',self.cycle())
        self.quote(1056)
        self.assertEqual(len(self.api.inserted),2)
        second=self.cycle()['orders'][-1]
        self.assertEqual(second['intent_id'],first['intent_id']);self.assertEqual(second['ordinal'],2)
        self.assertEqual(second['parent_child_order_id'],first['child_order_id'])
        self.api.fill(self.api.inserted[-1]['order_id'],5,1056);self.session.step()
        self.assertEqual(self.cycle()['protection']['owned_qty'],5)
        self.assertFalse(self.cycle().get('statistics_review_required',False));self.assertIsNone(self.kernel.money.error)
