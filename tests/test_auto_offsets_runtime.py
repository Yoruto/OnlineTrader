"""Native daily facts and the real owner chain; no SDK import or connection."""
import copy
import csv
from datetime import date,datetime,timedelta,timezone
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.offsets import DailyDataset,dsv_startup_profile
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.gateway.daily_facts import NativeDailyFeed
from online_trader.gateway.simnow import SimNowDsvPort
from online_trader.kernel import Kernel
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.auto_offsets import compile_preparation,prepare_connected,cache_path,load_preparation,verify_preparation
from online_trader.runtime.comparison import verify_dsv_profile,read_records
from online_trader.runtime.dsv_simulation import DsvSimulationSession,prepare,run
from online_trader.scheduling.calendar import TradingCalendar
from qa.dsv_native_facts import FactClock,NativeFactApi,NativeRows
from tests.test_online_dsv import scope_for,day_calendar


def daily_rows(*,last='2026-11-09',current='2026-11-10'):
    rows=[];last=date.fromisoformat(last)
    for i in range(30):
        day=last-timedelta(days=29-i)
        at=datetime.combine(day,datetime.min.time(),tzinfo=timezone(timedelta(hours=8)))
        rows.append({'id':i,'datetime':int(at.timestamp())*1_000_000_000,'high':2000.,'low':1000.,'close':1500.})
    if current:
        at=datetime.fromisoformat(current+'T00:00:00+08:00')
        rows.append({'id':30,'datetime':int(at.timestamp())*1_000_000_000,'high':99999.,'low':1.,'close':1000.})
    return rows


class DailyApi(NativeFactApi):
    def __init__(self,clock,code,identity):
        super().__init__(clock,(code,));self.daily={identity:NativeRows()};self.daily[identity].rows=daily_rows();self.daily_calls=[]
    def get_kline_serial(self,identity,duration_seconds,**kwargs):
        self.daily_calls.append((identity,duration_seconds,kwargs));return self.daily[identity]


class AutoOffsetRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.kernel=None;self.session=None
        self.clock=FactClock();self.code='SHFE.ag2702';self.calendar=day_calendar(self.code)
        settings=load_confirmed_baseline()[0].to_settings()
        settings['trade'].update(no_chase_after_act=False,entry_slip_abort=False,half_close_enabled=False,protect_80_enabled=False,
                                 trailing_activate_ratio=0)
        symbol=copy.deepcopy(settings['symbols'][0]);symbol.update(name='白银',code=self.code);settings['symbols']=[symbol]
        symbol.update(open_offset=0,stop_offset=0,profit_offset=0,order_volume=5)
        self.profile=DsvProfile.from_settings(settings)
        self.source=scope_for(self.root/'ledger.db',self.profile,self.calendar,schema_version='SIMNOW_DSV_AUTO/1',
            contracts=[self.code],trading_days=['2026-11-10'],resume_entries_on_day_change=False,
            auto_offsets={'schema_version':'DSV_START_OFFSETS/1','as_of_date':'2026-11-10','last_completed_day':'2026-11-09'})
        self.api=DailyApi(self.clock,self.code,self.code)
        self.port=SimNowDsvPort(self.api,self.api._account,self.source,self.clock);self.port.configure_calendar(self.calendar)

    def tearDown(self):
        if self.session:self.session.close()
        if self.kernel:self.kernel.close()
        self.temp.cleanup()

    def calculate(self):
        self.record,self.derived,self.effective=prepare_connected(self.source,self.profile,self.port)
        return self.record

    def owner(self):
        self.calculate()
        self.kernel=Kernel(self.root/'ledger.db',self.derived.account_key,port=self.port,simulation_grant=self.derived,risk_budget='1000000')
        self.kernel.simulation.initialize();self.kernel._auto_source_profile=self.profile
        self.kernel.simulation.capture('ONLINE_AUTO_OFFSETS_APPLIED',{'preparation':self.record})
        self.session=DsvSimulationSession(self.kernel,self.root/'session',profile=self.effective,calendar=self.calendar,
            clock=self.clock,emit=lambda r:None)
        self.api.tick(self.code,at='2026-11-10T01:00:00Z',last=1100,high=1120)
        self.api.tick(self.code,last=1100,high=1120);self.session.prime()

    def test_silver_calculates_all_three_offsets_and_uses_fixed_stop_15(self):
        record=self.calculate();row=next(r for r in record['calculation']['rows'] if r['contract_id']==self.code)
        self.assertEqual((row['open'],row['stop'],row['profit']),('140','15','315'))
        self.assertEqual(row['completed_bars'],30);self.assertEqual(row['last_completed_date'],'2026-11-09')
        self.assertEqual(self.api.daily_calls,[(self.code,86400,{'data_length':120})])
        self.assertEqual(self.api.inserted,[]);self.assertFalse(self.port.broker_connected)

    def test_source_scope_cannot_attach_an_execution_owner_before_calculation(self):
        with self.assertRaises(TraderError) as error:
            Kernel(self.root/'ledger.db',self.source.account_key,port=self.port,simulation_grant=self.source,risk_budget='1000000')
        self.assertEqual(error.exception.code,'E_AUTO_OFFSETS');self.assertFalse((self.root/'ledger.db').exists())

    def test_derived_scope_preserves_every_original_limit(self):
        self.calculate();changed={k for k in self.source.value if self.source.value[k]!=self.derived.value.get(k)}
        self.assertEqual(changed,{'schema_version','auto_offsets','grant_id','profile_hash','authorization_reference'})
        before=self.profile.to_record()['settings'];after=self.effective.to_record()['settings']
        self.assertEqual(before['trade'],after['trade']);self.assertEqual(before['auto_offset'],after['auto_offset'])
        self.assertEqual(before['schedule'],after['schedule'])
        self.assertEqual(next(s for s in before['symbols'] if s['code']==self.code)['stop_offset'],'0')

    def test_calculated_settings_export_original_dsv_eight_column_csv(self):
        self.owner();path=self.session.comparison.directory/'dsv-calculated-symbols.csv'
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream);rows=list(reader);self.assertEqual(len(reader.fieldnames),8)
        self.assertEqual(rows[0]['止损偏移'],'15');self.assertEqual(rows[0]['合约代码'],self.code)

    def test_computed_profile_reaches_original_oms_and_original_protection(self):
        self.owner();self.kernel.simulation.arm();self.kernel.wait_idle()
        self.clock.advance(1000);self.api.tick(self.code,last=1140,high=1140);self.session.step()
        self.assertEqual(len(self.api.inserted),1,self.session.last_decisions)
        cycle=next(iter(self.kernel._staged.values()));self.assertEqual(cycle['plan']['fixed_stop_loss']['distance_points'],'15')
        order=self.api.inserted[-1];self.api.fill(order['order_id'],5,1141);self.session.step()
        frozen=copy.deepcopy(cycle['plan'])
        self.clock.advance(1000);self.api.tick(self.code,last=1120,high=1140);self.session.step()
        self.assertEqual(len(self.api.inserted),2);self.assertEqual(self.api.inserted[-1]['offset'],'CLOSETODAY')
        self.assertEqual(next(iter(self.kernel._staged.values()))['plan'],frozen)

    def test_missing_current_label_or_missing_completed_day_blocks_calculation(self):
        self.api.daily[self.code].rows=daily_rows(current=None)
        with self.assertRaises(TraderError):NativeDailyFeed(self.port,self.profile).snapshot()
        self.api.daily[self.code].rows=daily_rows(last='2026-11-08')
        with self.assertRaises(TraderError):NativeDailyFeed(self.port,self.profile).snapshot()
        self.assertEqual(self.api.inserted,[])

    def test_only_unfinished_data_is_never_a_completed_fallback(self):
        rows=daily_rows()[-1:];self.api.daily[self.code].rows=rows
        with self.assertRaises(TraderError):NativeDailyFeed(self.port,self.profile).snapshot()
        self.assertFalse(cache_path(self.source).exists())

    def test_duplicate_daily_date_is_a_fact_barrier(self):
        self.api.daily[self.code].rows.insert(1,copy.deepcopy(self.api.daily[self.code].rows[0]))
        with self.assertRaises(TraderError) as error:NativeDailyFeed(self.port,self.profile).snapshot()
        self.assertEqual(error.exception.code,'E_FACT_BARRIER')

    def test_missing_middle_daily_index_is_not_treated_as_complete_history(self):
        self.api.daily[self.code].rows.pop(5)
        with self.assertRaises(TraderError) as error:NativeDailyFeed(self.port,self.profile).snapshot()
        self.assertEqual(error.exception.code,'E_FACT_BARRIER')

    def test_bitumen_default_formula_uses_main_daily_series(self):
        value=self.profile.to_settings();value['symbols'][0].update(code='SHFE.bu2701',name='沥青')
        profile=DsvProfile.from_settings(value);calendar=day_calendar('SHFE.bu2701');scope=self.source.value
        scope.update(contracts=['SHFE.bu2701'],profile_hash=profile.content_hash,calendar_hash=calendar.content_hash)
        grant=DsvSimulationGrant.parse(canonical_bytes(scope));api=DailyApi(self.clock,'SHFE.bu2701','KQ.m@SHFE.bu')
        port=SimNowDsvPort(api,api._account,grant,self.clock);port.configure_calendar(calendar)
        dataset,_,_=NativeDailyFeed(port,profile).snapshot()
        self.assertEqual({r['series_id'] for r in dataset.rows},{'KQ.m@SHFE.bu'})
        self.assertEqual(api.daily_calls,[('KQ.m@SHFE.bu',86400,{'data_length':120})])

    def test_night_daily_label_uses_next_cabinet_day_and_excludes_its_unfinished_bar(self):
        clock=FactClock('2026-11-10T13:06:00Z')
        value=self.calendar.to_record();value.update(coverage_from_utc='2026-11-10T12:00:00Z',coverage_to_utc='2026-11-11T08:00:00Z')
        value['contracts'][0]['sessions']=[{'session_id':'night','trading_day':'2026-11-11',
            'open_utc':'2026-11-10T13:00:00Z','close_utc':'2026-11-10T18:30:00Z',
            'is_opening_session':True,'force_close_at_end':True}]
        calendar=TradingCalendar.from_record(value);scope=self.source.value
        scope.update(trading_day='2026-11-11',trading_days=['2026-11-11'],calendar_hash=calendar.content_hash,
            not_before_utc='2026-11-10T12:55:00Z',not_after_utc='2026-11-10T18:31:00Z',
            auto_offsets={'schema_version':'DSV_START_OFFSETS/1','as_of_date':'2026-11-11','last_completed_day':'2026-11-10'})
        grant=DsvSimulationGrant.parse(canonical_bytes(scope));api=DailyApi(clock,self.code,self.code)
        api.daily[self.code].rows=daily_rows(last='2026-11-10',current='2026-11-11');api.tick(self.code)
        port=SimNowDsvPort(api,api._account,grant,clock);port.configure_calendar(calendar)
        dataset,ranges,evidence=NativeDailyFeed(port,self.profile).snapshot()
        record,_,_=compile_preparation(grant,self.profile,dataset,ranges,evidence)
        row=record['calculation']['rows'][0]
        self.assertEqual(row['last_completed_date'],'2026-11-10');self.assertEqual(row['base'],'1000')

    def test_original_ledger_restores_calculation_when_auxiliary_cache_is_lost(self):
        self.owner();cache_path(self.source).unlink()
        restored,grant,profile=load_preparation(self.source,self.profile)
        self.assertEqual(restored,self.record);self.assertEqual(grant.content_hash,self.derived.content_hash)
        self.assertEqual(profile.content_hash,self.effective.content_hash)
        self.assertEqual(len(self.api.daily_calls),1);self.assertEqual(self.api.inserted,[])

    def test_held_restart_keeps_original_plan_and_does_not_recalculate_new_daily_values(self):
        self.owner();self.kernel.simulation.arm();self.kernel.wait_idle()
        self.clock.advance(1000);self.api.tick(self.code,last=1140,high=1140);self.session.step()
        entry=self.api.inserted[-1];self.api.fill(entry['order_id'],5,1141);self.session.step()
        original=copy.deepcopy(next(iter(self.kernel._staged.values()))['plan'])
        self.session.close();self.session=None;self.kernel.close();self.kernel=None
        cache_path(self.source).unlink();self.api.daily[self.code].rows=[]
        record,grant,effective=load_preparation(self.source,self.profile)
        self.port=SimNowDsvPort(self.api,self.api._account,grant,self.clock);self.port.configure_calendar(self.calendar)
        self.kernel=Kernel(self.root/'ledger.db',grant.account_key,port=self.port,simulation_grant=grant,risk_budget='1000000')
        self.kernel.simulation.initialize();self.kernel._auto_source_profile=self.profile
        self.session=DsvSimulationSession(self.kernel,self.root/'restart',profile=effective,calendar=self.calendar,
            clock=self.clock,emit=lambda r:None)
        self.session.prime();self.session.step()
        cycle=next(iter(self.kernel._staged.values()))
        self.assertEqual(cycle['plan'],original);self.assertEqual(cycle['protection']['owned_qty'],5)
        self.assertEqual(len(self.api.inserted),1);self.assertEqual(len(self.api.daily_calls),1)
        self.assertFalse(self.kernel._armed)

    def test_tampered_calculation_cannot_change_effective_parameters(self):
        self.calculate();bad=copy.deepcopy(self.record);bad['effective_profile']['settings']['symbols'][-1]['stop_offset']='999'
        with self.assertRaises(TraderError):verify_preparation(self.source,self.profile,bad)

    def test_dsv_saved_source_is_distinct_from_verified_effective_values(self):
        self.calculate();path=self.root/'dsv-config.json';path.write_text(self.profile.to_json(),encoding='utf-8')
        verify_dsv_profile(path,self.effective,source_profile=self.profile)
        with self.assertRaises(TraderError):verify_dsv_profile(path,self.effective)
        path.write_text(self.effective.to_json(),encoding='utf-8');verify_dsv_profile(path,self.effective,source_profile=self.profile)

    def test_dsv_startup_zero_repair_and_calculator_are_distinct(self):
        repaired=dsv_startup_profile(self.profile).to_record()['settings']['symbols'][0]
        self.assertEqual([repaired[k] for k in ('open_offset','stop_offset','profit_offset')],['100','15','60'])
        self.calculate();path=self.root/'dsv-config.json';path.write_text(dsv_startup_profile(self.profile).to_json(),encoding='utf-8')
        verify_dsv_profile(path,self.effective,source_profile=self.profile)

    def test_comparison_waits_for_matching_effective_dsv_parameters_before_arm(self):
        self.owner();path=self.root/'dsv-config.json';path.write_text(self.profile.to_json(),encoding='utf-8')
        self.kernel._comparison_profile_source=path
        with self.assertRaises(TraderError) as error:self.kernel.simulation.arm()
        self.assertEqual(error.exception.code,'E_NOT_READY');self.assertTrue(self.kernel._auto_parameter_wait)
        path.write_text(self.effective.to_json(),encoding='utf-8');self.kernel.simulation.arm();self.kernel.wait_idle()
        self.assertFalse(self.kernel._auto_parameter_wait);self.assertTrue(self.kernel._armed)

    def test_offline_prepare_reports_pending_data_and_fixed_silver_stop_without_connection(self):
        for name,value in [('scope',self.source.value),('profile',self.profile.to_record()),('calendar',self.calendar.to_record())]:
            (self.root/(name+'.json')).write_bytes(canonical_bytes(value))
        with patch.object(SimNowDsvPort,'connect',side_effect=AssertionError('Connection forbidden')):
            result=prepare(self.root/'scope.json',self.root/'profile.json',self.root/'calendar.json',self.root/'prepared')
        self.assertEqual(result['automatic_offsets']['fixed_stops'],{self.code:'15'})
        self.assertEqual(result['automatic_offsets']['status'],'AWAITING_DAILY_HISTORY');self.assertFalse(result['broker_connected'])

    def test_explicit_run_bootstraps_daily_calculation_before_owner_without_second_connection(self):
        for name,value in [('scope',self.source.value),('profile',self.profile.to_record()),('calendar',self.calendar.to_record())]:
            (self.root/(name+'.json')).write_bytes(canonical_bytes(value))
        captured=[]
        class Server:
            def __init__(self,kernel,pipe):captured.append(kernel)
            def serve(self,stop,**kwargs):stop.set()
        with (patch('online_trader.runtime.dsv_simulation.measure_reference',return_value=self.clock),
              patch('online_trader.runtime.dsv_simulation.clock_observation',return_value={'system_aligned':True}),
              patch.object(SimNowDsvPort,'connect',return_value=self.port) as connect,
              patch('online_trader.kernel.server.ControlServer',Server)):
            run(self.root/'scope.json','NEVER_READ',self.root/'profile.json',self.root/'calendar.json',self.root/'run',pipe='offline-auto')
        self.assertEqual(connect.call_count,1);self.assertEqual(self.api.inserted,[])
        self.assertEqual(captured[0].simulation.grant.value['schema_version'],'SIMNOW_DSV_SIM/1')
        self.assertTrue(captured[0].ledger is not None);self.assertTrue(cache_path(self.source).exists())


if __name__=='__main__':unittest.main()
