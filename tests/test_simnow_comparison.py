"""Targeted native-fact checks; never imports the real SDK or connects."""
import copy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes,strict_loads
from online_trader.errors import TraderError
from online_trader.gateway.simnow import SimNowDsvPort,account_fingerprint,credentials,strategy_port
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.comparison import parse_dsv_line,DsvLogTail,build_report,read_records,verify_dsv_profile
from online_trader.runtime.dsv_simulation import prepare
from online_trader.scheduling.reference import ReferenceClock,clock_observation
from qa.dsv_native_facts import FactClock,NativeFactApi
from tests import test_online_dsv as fixture


def simnow_scope(path,profile,calendar,**changes):
    changes.update(schema_version='SIMNOW_DSV_SIM/1',trading_days=['2026-11-10'],resume_entries_on_day_change=False)
    return fixture_original_scope(path,profile,calendar,**changes)


fixture_original_scope=fixture.scope_for


class SimNowComparisonTests(unittest.TestCase):
    def setUp(self):self.x=fixture.OnlineDsvTests();self.x.setUp()
    def tearDown(self):self.x.tearDown()
    def start(self,**options):
        with patch.object(fixture,'scope_for',simnow_scope),patch.object(fixture,'TqKqDsvPort',SimNowDsvPort):
            self.x.start(**options)

    def read_strategy_log(self):
        path = self.x.session.comparison.directory.parent / 'strategy-log.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def test_strategy_log_throttles_geometry_trigger_and_activation_in_dsv_order(self):
        self.start(trade={'no_chase_after_act': True, 'trailing_activate_ratio': 0.8})
        x = self.x
        self.assertEqual(self.read_strategy_log(), [])
        x.quote(price=1080, high=1300, milliseconds=60_000)
        first = x.clock.now()
        rows = self.read_strategy_log()
        self.assertEqual([row['kind'] for row in rows], ['GEOMETRY_BLOCK', 'TRIGGER_HINT', 'LONG:NO_CHASE_AFTER_ACT'])
        self.assertEqual([row['tag'] for row in rows], ['提示', '提示', '跳过'])
        self.assertIn('已跑过多头激活位', rows[-1]['message'])
        self.assertTrue(all(row['contract_id'] == x.code and row['trading_day'] == '2026-11-10' for row in rows))

        x.quote(price=1080, high=1300, milliseconds=60_000)
        self.assertEqual(self.read_strategy_log(), rows)
        x.quote(price=1081, high=1300, milliseconds=1)
        self.assertEqual([row['kind'] for row in self.read_strategy_log()[3:]], ['TRIGGER_HINT'])
        x.quote(price=1082, high=1300, milliseconds=59_999)
        self.assertEqual(len(self.read_strategy_log()), 4)
        x.quote(price=1083, high=1300, milliseconds=1)
        self.assertEqual([row['kind'] for row in self.read_strategy_log()[3:]], ['TRIGGER_HINT', 'TRIGGER_HINT'])

        x.quote(price=1083, high=1300, milliseconds=479_998)
        self.assertEqual(len(self.read_strategy_log()), 5)
        x.quote(price=1083, high=1300, milliseconds=1)
        rows = self.read_strategy_log()
        self.assertEqual([row['kind'] for row in rows[5:]], ['LONG:NO_CHASE_AFTER_ACT'])
        self.assertEqual(datetime.fromisoformat(rows[-1]['observed_at_utc']) - first, timedelta(seconds=600))
        self.assertEqual(sum(row['kind'] == 'GEOMETRY_BLOCK' for row in rows), 1)
        self.assertEqual(x.api.inserted, [])

    def test_strategy_log_suppresses_backlog_and_keeps_raw_comparison(self):
        self.start(trade={'no_chase_after_act': True, 'trailing_activate_ratio': 0.8})
        x = self.x
        x.clock.advance(1000)
        x.api.tick(x.code, last=1080, high=1300)
        x.quote(price=1040, high=1300)

        rows = self.read_strategy_log()
        self.assertEqual([row['kind'] for row in rows], ['GEOMETRY_BLOCK', 'TRIGGER_HINT', 'SHORT:NO_CHASE_AFTER_ACT'])
        self.assertIn('已跑过空头激活位', rows[-1]['message'])
        self.assertTrue(all(datetime.fromisoformat(row['observed_at_utc']) == x.clock.now() for row in rows))
        decisions = [row['data'] for row in read_records(x.session.comparison.path) if row['kind'] == 'PRODUCT_MARKET_DECISION']
        self.assertEqual([row['event']['last'] for row in decisions[-2:]], ['1080', '1040'])
        self.assertIn('OBSERVATION_ONLY_BACKLOG', decisions[-2]['decision']['reasons'])
        self.assertEqual(x.api.inserted, [])

    def test_strategy_log_prioritizes_timing_and_throttles_close_at_five_minutes(self):
        self.x.clock = FactClock('2026-11-10T01:04:59Z')
        self.start(trade={'no_open_after_open': True, 'no_open_after_open_minutes': 5,
                          'no_open_before_close_minutes': 10, 'no_chase_after_act': True,
                          'trailing_activate_ratio': 0.8})
        x = self.x
        x.quote(price=1080, high=1300)
        self.assertEqual([row['kind'] for row in self.read_strategy_log()], ['OPENING_WAIT'])
        x.quote(price=1080, high=1300, milliseconds=1)
        self.assertEqual([row['kind'] for row in self.read_strategy_log()[1:]],
                         ['GEOMETRY_BLOCK', 'TRIGGER_HINT', 'LONG:NO_CHASE_AFTER_ACT'])
        until_close = datetime.fromisoformat('2026-11-10T06:50:00Z') - x.clock.now()
        x.quote(price=1080, high=1300, milliseconds=round(until_close.total_seconds() * 1000))
        self.assertEqual([row['kind'] for row in self.read_strategy_log()[4:]], ['CLOSE_SESSION_NO_OPEN'])
        x.quote(price=1080, high=1300, milliseconds=299_999)
        self.assertEqual(len(self.read_strategy_log()), 5)
        x.quote(price=1080, high=1300, milliseconds=1)
        self.assertEqual([row['kind'] for row in self.read_strategy_log()[4:]], ['CLOSE_SESSION_NO_OPEN'] * 2)
        self.assertEqual(x.api.inserted, [])

    def test_scope_and_adapter_identities_cannot_cross_kq_and_simnow(self):
        self.start();x=self.x
        self.assertTrue(x.grant.account_key.startswith('SIM:SIMNOW:'))
        self.assertTrue(x.port.gateway_identity.startswith('TQ_SIMNOW:'))
        self.assertIs(strategy_port(x.grant),SimNowDsvPort)
        self.assertEqual(x.kernel.simulation.authority_policy()['provider'],'TQ_SIMNOW')
        self.assertEqual(x.kernel.ledger.query('SELECT broker_identity FROM ledger_scope')[0]['broker_identity'],'TQ_SIMNOW')
        with self.assertRaises(TraderError):TqKqDsvPort(x.api,x.api._account,x.grant,x.clock)
        with patch('online_trader.gateway.tqkq.credentials',side_effect=AssertionError('must not read')):
            with self.assertRaises(TraderError):TqKqDsvPort.connect('never-read',x.grant,x.clock)

    def test_connect_uses_only_literal_simnow_and_separate_market_credentials(self):
        self.start();x=self.x
        class Account:
            def __init__(self,*args,**kwargs):self.args=args;self.kwargs=kwargs;self._broker_id=args[0]
        calls=[]
        def api_factory(**kwargs):calls.append(kwargs);return NativeFactApi(x.clock,(x.code,))
        stub=SimpleNamespace(TqApi=api_factory,TqAccount=Account,TqAuth=lambda *v:v)
        raw={'simnow_username':'FACT_ACCOUNT','simnow_password':'FACT_PASSWORD','kq_username':'FACT_MARKET','kq_password':'FACT_MARKET_PASSWORD'}
        value=x.grant.value;value['account_fingerprint']=account_fingerprint(raw['simnow_username'])
        grant=DsvSimulationGrant.parse(canonical_bytes(value))
        with (patch.dict(sys.modules,{'tqsdk':stub}),patch('online_trader.gateway.simnow.credentials',return_value=raw),
              patch('online_trader.gateway.simnow.version',return_value='3.10.4')):
            port=SimNowDsvPort.connect('not-read',grant,x.clock,read_only=True)
        self.assertEqual(calls[0]['account'].args,('simnow','FACT_ACCOUNT','FACT_PASSWORD'))
        self.assertEqual(calls[0]['account'].kwargs,{})
        self.assertEqual(calls[0]['auth'],('FACT_MARKET','FACT_MARKET_PASSWORD'))
        self.assertFalse(calls[0]['debug']);self.assertTrue(port.read_only);port.close()

    def test_prepare_never_reads_credentials_imports_sdk_or_samples_ntp(self):
        self.start();x=self.x;root=Path(x.temp.name)
        for name,value in [('scope',x.grant.value),('profile',x.profile.to_record()),('calendar',x.calendar.to_record())]:
            (root/(name+'.json')).write_bytes(canonical_bytes(value))
        with (patch('online_trader.runtime.dsv_simulation.measure_reference',side_effect=AssertionError('NTP forbidden')),
              patch('online_trader.gateway.simnow.credentials',side_effect=AssertionError('credential forbidden'))):
            result=prepare(root/'scope.json',root/'profile.json',root/'calendar.json',root/'prepare')
        self.assertEqual(result['provider'],'TQ_SIMNOW');self.assertFalse(result['broker_connected'])

    def test_original_commit_precedes_simnow_send_and_native_fills_close_original_cycle(self):
        self.start(trade={'half_close_enabled':False,'protect_80_enabled':False});x=self.x
        insert=x.api.insert_order
        def check(**values):
            self.assertTrue(x.kernel.ledger.query("SELECT event_seq FROM event_journal WHERE event_type='ENTRY_PREPARED'"))
            return insert(**values)
        x.api.insert_order=check;x.quote()
        order=x.api.inserted[0];x.api.fill(order['order_id'],order['volume'],1055);x.session.step()
        self.assertTrue(all(f['trade_key'].startswith('sn-') for o in x.kernel.simulation._observed_orders.values() for f in o['fills']))
        x.quote(1030);self.assertEqual(len(x.api.inserted),2)
        x.finish_last(1030);self.assertEqual(x.cycle()['lifecycle'],'CLOSED')
        records=list(read_records(x.session.comparison.path))
        self.assertTrue(any(r['kind']=='PRODUCT_MARKET_DECISION' for r in records))
        self.assertTrue(any(r['kind']=='PRODUCT_COMMITTED_EVENT' and r['data']['event_type']=='ORDER_TERMINAL' for r in records))
        from online_trader.runtime.dsv_simulation import final_relogin
        reader_api=NativeFactApi(x.clock)
        for name in ('orders','trades','positions','quotes','serials'):setattr(reader_api,name,copy.deepcopy(getattr(x.api,name)))
        reader=SimNowDsvPort(reader_api,reader_api._account,x.grant,x.clock)
        with patch.object(SimNowDsvPort,'connect',return_value=reader) as connect:
            self.assertTrue(final_relogin(x.kernel,x.session,'never-read'))
        self.assertTrue(connect.call_args.kwargs['read_only']);self.assertEqual(reader_api.inserted,[])

    def test_clock_gate_blocks_new_entries_but_retains_owned_exit(self):
        self.start(trade={'half_close_enabled':False,'protect_80_enabled':False});x=self.x
        x.quote();order=x.api.inserted[0];x.api.fill(order['order_id'],order['volume'],1055);x.session.step()
        x.port.entry_clock_ready=lambda:False
        with self.assertRaises(TraderError):x.kernel.simulation.arm()
        x.quote(1030);self.assertFalse(x.kernel._armed)
        self.assertEqual(len(x.api.inserted),2);self.assertEqual(x.api.inserted[-1]['offset'],'CLOSE')

    def test_comparison_disk_error_pauses_entry_and_still_sends_original_exit(self):
        self.start(trade={'half_close_enabled':False,'protect_80_enabled':False});x=self.x
        x.quote();order=x.api.inserted[0];x.api.fill(order['order_id'],order['volume'],1055);x.session.step()
        with patch.object(x.session.comparison,'write',side_effect=OSError('offline injected disk failure')):x.quote(1030)
        self.assertEqual(len(x.api.inserted),2);self.assertFalse(x.kernel._armed)
        self.assertEqual(x.session.comparison_error,'E_COMPARISON_WRITE')
        with self.assertRaises(TraderError):x.kernel.simulation.arm()

    def test_status_file_failure_does_not_interrupt_owned_exit(self):
        self.start(trade={'half_close_enabled':False,'protect_80_enabled':False});x=self.x
        x.quote();order=x.api.inserted[0];x.api.fill(order['order_id'],order['volume'],1055);x.session.step()
        x.session.phase=None
        with patch.object(x.session,'write',side_effect=OSError('injected output failure')):x.quote(1030)
        self.assertEqual(len(x.api.inserted),2);self.assertFalse(x.kernel._armed)

    def test_native_csv_includes_exit_totals_without_importing_foreign_trades(self):
        from online_trader.runtime.comparison import native_totals
        self.start();x=self.x;path=Path(x.temp.name)/'dsv-trades.csv'
        path.write_text('account_key,environment,trading_day,trade_key,contract_id,side,effect,quantity,price\n'
            'DSV:FACT,ONLINE_SIM,2026-11-10,entry,DCE.p2701,BUY,OPEN,5,1055\n'
            'DSV:FACT,ONLINE_SIM,2026-11-10,exit,DCE.p2701,SELL,REDUCE,5,1030\n',encoding='utf-8')
        product=[{'kind':kind,'trading_day':'2026-11-10','contract_id':x.code,'direction':'LONG','quantity':5,'price':price}
                 for kind,price in [('ENTRY_FILL','1055'),('EXIT_FILL','1031')]]
        result=native_totals(path,'DSV:FACT',x.session.comparison.manifest,product)
        self.assertEqual(len(result['totals']),2)
        self.assertTrue(all(r['quantity_equal'] for r in result['totals']))
        self.assertEqual(next(r for r in result['totals'] if r['kind']=='EXIT_FILL')['price_difference'],'1')
        self.assertFalse(result['ownership_or_fill_time_pairing_claimed']);self.assertEqual(len(x.api.inserted),0)
        with self.assertRaises(TraderError):native_totals(path,'OTHER',x.session.comparison.manifest,product)

    def test_reference_offset_detects_windows_clock_change(self):
        with patch('online_trader.scheduling.reference.time.monotonic',return_value=100),patch('online_trader.scheduling.reference.time.time',return_value=1000):
            clock=ReferenceClock(1000.5,100,0.5,())
            self.assertTrue(clock_observation(clock)['system_aligned'])
            with patch('online_trader.scheduling.reference.time.time',return_value=998):
                observation=clock_observation(clock)
                self.assertFalse(observation['system_aligned']);self.assertEqual(observation['reference_minus_system_ms'],'2500.0')

    def test_dsv_last_timestamp_and_exit_decision_are_not_native_fills(self):
        directions={}
        order=parse_dsv_line('[09:45:58] [09:46:00] [执行] [开short下单] 沥青(SHFE.bu2701) 理论开仓:4270.0 手数:16（限价上限 4268.0）',
                             '2026-11-10',directions=directions)
        self.assertEqual(order['source_time_utc'],'2026-11-10T01:46:00Z');self.assertEqual(order['limit_price'],'4268')
        fill=parse_dsv_line('[09:45:58] [09:46:00] [成交] 沥青(SHFE.bu2701) 开仓 已成交 16 手，真实均价 4268.0',
                            '2026-11-10',directions=directions)
        self.assertEqual(fill['direction'],'SHORT');self.assertFalse(fill['native_identity_available'])
        close=parse_dsv_line('[09:48:54] [自动] [平仓] 沥青(SHFE.bu2701) 空头止损 数量:16 出场:4282.0','2026-11-10')
        self.assertEqual(close['kind'],'EXIT_DECISION');self.assertFalse(close['fill_confirmed'])
        self.assertEqual(close['operator_tag'],'auto')

    def test_log_tail_preserves_partial_lines_and_never_writes_source(self):
        root=Path(self.x.temp.name);log=root/'trade_2026-11-10.log'
        first='[09:06:01] [执行] [开long下单] 棕榈油(DCE.p2701) 理论开仓:1055 手数:5\n'.encode()
        log.write_bytes(first[:-3]);tail=DsvLogTail(root);self.assertEqual(tail.poll(),[])
        with log.open('ab') as stream:stream.write(first[-3:])
        before=log.read_bytes();self.assertEqual(len(tail.poll()),1);self.assertEqual(tail.poll(),[])
        self.assertEqual(before,log.read_bytes())

    def test_dsv_same_log_midnight_rollover_keeps_actual_civil_date_distinct_from_cabinet_day(self):
        root=Path(self.x.temp.name);log=root/'trade_2026-11-10.log'
        log.write_text('[23:59:59] [提示] 白银(SHFE.ag2702) 现价 1000\n'
                       '[00:00:01] [执行] [开long下单] 白银(SHFE.ag2702) 理论开仓:1000 手数:1\n',encoding='utf-8')
        rows=DsvLogTail(root).poll()
        self.assertEqual(rows[-1]['source_time_utc'],'2026-11-10T16:00:01Z')
        self.assertEqual(rows[-1]['midnight_rollovers_inferred'],1)
        self.assertFalse(rows[-1]['cabinet_day_inferred_from_filename'])

    def test_dsv_trading_day_filename_uses_explicit_start_date_across_weekend_and_midnight(self):
        root=Path(self.x.temp.name);log=root/'trade_2026-11-16.log'
        log.write_text('交易日: 2026-11-16\n启动时间: 2026-11-13 20:59:04\n'
                       '[21:05:00] [执行] [开short下单] 沥青(SHFE.bu2701) 理论开仓:4302 手数:16\n'
                       '[22:59:40] [自动] [平仓] [夜盘收盘平仓] 沥青(SHFE.bu2701) short 手数:16 价格:4295.0\n'
                       '[00:00:11] [提示] 白银(SHFE.ag2702) 现价 12552\n'
                       '启动时间: 2026-11-16 08:55:00\n'
                       '[09:05:00] [提示] 白银(SHFE.ag2702) 现价 12553\n',encoding='utf-8')
        first,close,midnight,day=DsvLogTail(root).poll()
        self.assertEqual(first['source_time_utc'],'2026-11-13T13:05:00Z')
        self.assertEqual(first['civil_date_source'],'DSV_START_HEADER')
        self.assertEqual((close['quantity'],close['reference_price'],close['direction']),(16,'4295','SHORT'))
        self.assertFalse(close['fill_confirmed'])
        self.assertEqual(midnight['source_time_utc'],'2026-11-13T16:00:11Z')
        self.assertEqual(day['source_time_utc'],'2026-11-16T01:05:00Z')

    def test_report_pairs_unique_orders_and_fills_and_exposes_both_sides(self):
        self.start();x=self.x;x.quote();order=x.api.inserted[0]
        x.api.fill(order['order_id'],order['volume'],1055);x.session.step()
        directions={}
        for text in ('[09:06:01] [执行] [开long下单] 棕榈油(DCE.p2701) 理论开仓:1055 手数:5（限价上限 1055）',
                     '[09:06:01] [成交] 棕榈油(DCE.p2701) 开仓 已成交 5 手，真实均价 1055（理论开仓价 1055）'):
            row=parse_dsv_line(text,'2026-11-10',directions=directions);row['trading_day']='2026-11-10'
            x.session.comparison.write('DSV_LOG',row)
        result=build_report(x.session.comparison.directory/'manifest.json',Path(x.temp.name)/'report')
        matches=[r for r in result['rows'] if r['status']=='MATCHED']
        self.assertEqual(len(matches),2,result['rows']);self.assertTrue(all(r['quantity_equal'] for r in matches))
        self.assertEqual(next(r for r in matches if r['kind']=='ENTRY_FILL')['price_difference'],'0')
        self.assertEqual(next(r for r in matches if r['kind']=='ENTRY_ORDER')['price_difference'],str(int(order['limit_price'])-1055))
        self.assertFalse(result['automatic_correctness_verdict'])

    def test_comparison_receipt_without_original_fill_commit_is_not_counted_as_a_confirmed_fill(self):
        from online_trader.runtime.comparison import _product_events
        self.start();x=self.x;x.quote();order=x.api.inserted[0]
        x.api.fill(order['order_id'],order['volume'],1055);x.session.step()
        records=list(read_records(x.session.comparison.path))
        self.assertTrue(any(r['kind']=='ENTRY_FILL' for r in _product_events(records)))
        without_ack=[r for r in records if r['kind']!='PRODUCT_COMMITTED_EVENT' or r['data']['event_type']!='FILL_CONFIRMED']
        self.assertFalse(any(r['kind']=='ENTRY_FILL' for r in _product_events(without_ack)))

    def test_changed_dsv_business_settings_are_rejected(self):
        self.start();x=self.x;path=Path(x.temp.name)/'business.json'
        value=x.profile.to_settings();path.write_text(json.dumps(value),encoding='utf-8')
        self.assertEqual(verify_dsv_profile(path,x.profile).content_hash,x.profile.content_hash)
        value['symbols'][0]['open_offset']+=1;path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(TraderError):verify_dsv_profile(path,x.profile)

    def test_scope_fingerprint_mismatch_stops_before_sdk_import(self):
        self.start();x=self.x
        with patch('online_trader.gateway.simnow.credentials',return_value={'simnow_username':'wrong'}):
            with self.assertRaises(TraderError) as error:SimNowDsvPort.connect('not-read',x.grant,x.clock)
        self.assertEqual(error.exception.code,'E_SCOPE')


if __name__=='__main__':unittest.main()
