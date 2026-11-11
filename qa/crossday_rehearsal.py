"""Product owner/ledger + Windows IPC with explicit external rollover facts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import online_trader
from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.domain.orders import OrderState
from online_trader.kernel.server import ControlServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.test_crossday_native import NativeRolloverOwnerTests


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    case=NativeRolloverOwnerTests();case.setUp();x=case.x
    x.temp.cleanup();x.temp=SimpleNamespace(name=str(out),cleanup=lambda:None);x.path=out/'ledger.db'
    results=[];failures=[];stop=threading.Event();report={'status':'RUNNING'}
    def write(name,value):(out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    try:
        oid=case.start();x.api.fill(oid,1,1055);x.session.step()
        original=content_hash(x.cycle()['plan']);calls=len(x.api.inserted)
        x.api.fill(oid,1,1055)  # Original-day detail deliberately arrives late.
        case.advance_day();x.api.fill(oid,1,1055);x.session.step()
        assert x.cycle()['protection']['owned_qty']==3
        assert len(x.api.inserted)==calls==1
        assert content_hash(x.cycle()['plan'])==original
        assert x.kernel.recovery_required and not x.kernel._armed
        facts=x.kernel.ledger.query('SELECT trade_key,trading_day,quantity,raw_fact_json FROM fill_facts ORDER BY recorded_event_seq')
        assert [f['trading_day'] for f in facts]==['2026-11-10','2026-11-10','2026-11-11']
        assert all(strict_loads(f['raw_fact_json'])['native_day_evidence']['calendar_hash']==x.calendar.content_hash for f in facts)
        pipe='OnlineTrader.CrossDay.'+uuid.uuid4().hex
        def client():
            try:
                for action in ('workbench-status','status'):
                    process=subprocess.run([sys.executable,'-I','-X','utf8','-m','online_trader',action,'--pipe',pipe],
                        cwd=out,capture_output=True,text=True,encoding='utf-8',timeout=12,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    assert process.returncode==0,process.stderr
                    result=json.loads(process.stdout);results.append({'action':action,'result':result})
                assert results[0]['result']['cycles'][0]['remaining_quantity']==3
                assert results[0]['result']['entry_authority']=='DISARMED'
                assert results[0]['result']['connection']['recovery_required']
            except BaseException as error:failures.append(repr(error))
            finally:stop.set()
        thread=threading.Thread(target=client);thread.start();began=time.monotonic()
        def tick():
            x.session.step()
            if time.monotonic()-began>25:failures.append('Offline cross-day IPC deadline');stop.set()
        try:ControlServer(x.kernel,pipe).serve(stop,owner_tick=tick)
        finally:thread.join(timeout=15)
        assert not failures,failures
        assert len(x.kernel.ledger.query('SELECT trade_key FROM fill_facts'))==3
        assert OrderState.from_dict(x.cycle()['orders'][0]).potential_remaining==2
        package=Path(online_trader.__file__).resolve().parent
        report={'status':'PASS','version':online_trader.__version__,'package_root':str(package),
            'scope':'OFFLINE_EXTERNAL_NATIVE_DAYS_ORIGINAL_OWNER_LEDGER_REAL_WINDOWS_IPC',
            'broker_connections':0,'real_native_orders_sent':0,'development_kernel':x.kernel.development,
            'product_execution_scope':x.cycle()['plan']['execution_scope'],'owned_quantity':3,'pending_open_quantity':2,
            'original_plan_hash':original,'native_fill_days':[f['trading_day'] for f in facts],
            'entry_authority':'DISARMED','recovery_required':True,'crossday_send_enabled':False,
            'recorded_adapter_calls':len(x.api.inserted),'additional_calls_after_rollover':len(x.api.inserted)-calls,
            'source_sha256':{p.relative_to(package).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob('*.py')},
            'sqlite_integrity':x.kernel.ledger.query('PRAGMA integrity_check'),'outcomes':results,
            'money_report':x.kernel.money.report(),'final_snapshot':x.kernel.snapshot()}
        write('native-days.json',facts)
    except BaseException as error:
        report={'status':'FAIL','error':repr(error),'broker_connections':0,'real_native_orders_sent':0}
        raise
    finally:
        case.tearDown();write('result.json',report)
        print(json.dumps({key:report.get(key) for key in ('status','version','owned_quantity','native_fill_days','broker_connections','real_native_orders_sent','error')}))
    return 0


if __name__=='__main__':raise SystemExit(main())
