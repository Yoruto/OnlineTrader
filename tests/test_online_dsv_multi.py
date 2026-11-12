from pathlib import Path
import copy
import tempfile
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.kernel import Kernel
from online_trader.runtime.dsv_simulation import DsvSimulationSession
from online_trader.scheduling.calendar import TradingCalendar
from qa.dsv_native_facts import FactClock,NativeFactApi
from tests.test_online_dsv import day_calendar,scope_for


class MultiContractDsvTests(unittest.TestCase):
    def test_scope_finishes_in_covered_post_close_interval(self):
        clock=FactClock('2026-11-10T06:59:59Z');calendar=day_calendar()
        profile=load_confirmed_baseline()[0]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            grant=scope_for(root/'ledger.db',profile,calendar,schema_version='KQ_DSV_SIM/2',
                trading_days=['2026-11-10'],resume_entries_on_day_change=False)
            api=NativeFactApi(clock);port=TqKqDsvPort(api,api._account,grant,clock)
            port.configure_calendar(calendar)
            with Kernel(root/'ledger.db',grant.account_key,port=port,simulation_grant=grant,risk_budget='1000000') as kernel:
                kernel.simulation.initialize()
                session=DsvSimulationSession(kernel,root/'session',profile=profile,calendar=calendar,clock=clock,emit=lambda value:None)
                try:
                    session.prime();clock.advance(2000);session.step()
                    self.assertTrue(session.stop.is_set())
                    self.assertEqual(session.result['status'],'DSV_SCOPE_FINISHED')
                    self.assertEqual(port.snapshot()['trading_day'],'2026-11-10')
                    self.assertEqual(api.inserted,[])
                    clock.advance(3_600_000)
                    self.assertFalse(port.snapshot()['complete'])
                finally:session.close()

    def test_two_contracts_share_owner_reservations_and_keep_exits_separate(self):
        codes=('DCE.p2701','DCE.l2701');clock=FactClock()
        record=day_calendar().to_record();row=record['contracts'][0]
        record['contracts']=[copy.deepcopy(row)|{'contract_id':code} for code in codes]
        calendar=TradingCalendar.from_record(record)
        settings=load_confirmed_baseline()[0].to_settings()
        for row in settings['symbols']:
            if row['code'] in codes:row['order_volume']=2
        profile=DsvProfile.from_settings(settings)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);grant=scope_for(root/'ledger.db',profile,calendar,contracts=list(codes))
            api=NativeFactApi(clock,codes);port=TqKqDsvPort(api,api._account,grant,clock);port.configure_calendar(calendar)
            with Kernel(root/'ledger.db',grant.account_key,port=port,simulation_grant=grant,risk_budget='1000000') as kernel:
                kernel.simulation.initialize()
                session=DsvSimulationSession(kernel,root/'session',profile=profile,calendar=calendar,clock=clock,emit=lambda value:None)
                try:
                    for code in codes:
                        api.tick(code,at='2026-11-10T01:00:00Z',last=1040);api.tick(code,last=1040)
                    session.prime();kernel.simulation.arm();kernel.wait_idle();clock.advance(1000)
                    api.tick(codes[0],last=1055);api.tick(codes[1],last=1079);session.step()
                    self.assertEqual([o['symbol'] for o in api.inserted],list(codes),session.last_decisions)
                    for call in list(api.inserted):api.fill(call['order_id'],call['volume'],1055 if call['symbol']==codes[0] else 1079)
                    session.step();self.assertEqual([c['protection']['owned_qty'] for c in kernel._staged.values()],[2,2])
                    clock.advance(1000);api.tick(codes[0],last=1038);session.step()
                    self.assertEqual(api.inserted[-1]['symbol'],codes[0]);self.assertEqual(api.inserted[-1]['offset'],'CLOSE')
                    self.assertEqual(api.positions[codes[1]].pos_long,2);self.assertIsNone(kernel.money.error)
                finally:session.close()

    def test_night_session_uses_explicit_next_cabinet_day_instead_of_utc_date(self):
        clock=FactClock('2026-11-10T13:06:00Z');record=day_calendar().to_record()
        record.update(coverage_from_utc='2026-11-10T12:00:00Z',coverage_to_utc='2026-11-11T08:00:00Z')
        record['contracts'][0]['sessions']=[{'session_id':'night','trading_day':'2026-11-11','open_utc':'2026-11-10T13:00:00Z',
            'close_utc':'2026-11-10T15:00:00Z','is_opening_session':True,'force_close_at_end':True}]
        calendar=TradingCalendar.from_record(record)
        settings=load_confirmed_baseline()[0].to_settings();settings['symbols'][0]['order_volume']=2
        profile=DsvProfile.from_settings(settings)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);grant=scope_for(root/'ledger.db',profile,calendar,trading_day='2026-11-11',
                not_before_utc='2026-11-10T13:00:00Z',not_after_utc='2026-11-10T15:00:00Z')
            api=NativeFactApi(clock);port=TqKqDsvPort(api,api._account,grant,clock);port.configure_calendar(calendar)
            with Kernel(root/'ledger.db',grant.account_key,port=port,simulation_grant=grant,risk_budget='1000000') as kernel:
                kernel.simulation.initialize()
                session=DsvSimulationSession(kernel,root/'session',profile=profile,calendar=calendar,clock=clock,emit=lambda value:None)
                try:
                    api.tick(at='2026-11-10T13:00:00Z',last=1040);api.tick(last=1040);session.prime()
                    kernel.simulation.arm();kernel.wait_idle();clock.advance(1000);api.tick(last=1055);session.step()
                    self.assertEqual(len(api.inserted),1,session.last_decisions)
                    self.assertEqual(next(iter(kernel._staged.values()))['plan']['trading_day'],'2026-11-11')
                    self.assertEqual(port.snapshot()['trading_day'],'2026-11-11')
                finally:session.close()
