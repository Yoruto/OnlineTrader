"""Temporary offline facts exercise the read-only auditor, never an SDK connection."""
from datetime import datetime,timezone,timedelta
from pathlib import Path
import json,sys,tempfile,time
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tests.test_online_simulation import FakeOnlinePort,grant_for
from online_trader.contracts.codec import canonical_bytes
from online_trader.kernel import Kernel
from online_trader.runtime.simulation import SimulationSession
from qa.product_simulation_closure import audit

class FixturePort(FakeOnlinePort):
    def snapshot(self):
        value=super().snapshot()
        for trade in value['trades']:
            native=self.native[trade['order_id']]
            fill=next(f for o in self.observed.values() for f in o['fills'] if f['trade_key']=='kq-'+trade['trade_id'])
            trade.update(direction=native['direction'],offset=native['offset'],source_time_ns=fill['source_time_ns'])
        return value

with tempfile.TemporaryDirectory(prefix='online-trader-offline-auditor-fixture-') as temp:
    root=Path(temp);now=datetime.now(timezone.utc).replace(microsecond=0)
    grant=grant_for(root/'ledger.db',not_before_utc=(now-timedelta(seconds=1)).isoformat().replace('+00:00','Z'),not_after_utc=(now+timedelta(minutes=4)).isoformat().replace('+00:00','Z'))
    (root/'scope.json').write_bytes(canonical_bytes(grant.value));port=FixturePort(grant);port.clock.value=now
    port.clock.monotonic_anchor=time.monotonic();port.poll_updates=lambda seconds:None
    with Kernel(root/'ledger.db',grant.account_key,port=port,simulation_grant=grant,risk_budget='1000') as kernel:
        kernel.simulation.initialize();session=SimulationSession(kernel,root/'run',emit=lambda value:None)
        session.step();port.prices['source_time']='quote-2';session.step();assert len(port.sent)==1
        port.fill(port.sent[0]['child_order_id']);session.step();cycle=next(iter(kernel._staged.values()))
        due=int(cycle['online_deadline']['due_monotonic_ns']);port.clock.value+=timedelta(seconds=61);port.prices['source_time']='quote-3'
        with patch('online_trader.kernel.simulation.time.monotonic_ns',return_value=due):session.step()
        assert len(port.sent)==2
        port.fill(port.sent[1]['child_order_id'],'999');port.prices.update(source_time='quote-4',price='999');session.step()
        assert session.result['status']=='CLOSED'
        session.write('final-relogin.json',{'status':'PASS','native_snapshot':port.snapshot(),'fixture_only':True})
        session.write('final-money-report.json',kernel.money.report())
    report=audit(root);assert report['status']=='PASS' and report['one_minute_reached'] and report['gross_realized_pnl']=='-10'
    assert report['original_ledger_fill_count']==2 and report['native_fill_interval_seconds']=='61'
print(json.dumps({'status':'PASS','scope':'TEMPORARY_OFFLINE_AUDITOR_FIXTURE','broker_connected':False,'real_orders':0}))
