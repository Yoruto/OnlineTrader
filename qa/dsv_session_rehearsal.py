"""Installed/source product DSV + IPC, using external facts and zero connections."""
from pathlib import Path
from types import SimpleNamespace
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import online_trader

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from tests.test_online_dsv import OnlineDsvTests
from online_trader.kernel.server import ControlServer


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    case=OnlineDsvTests();case.setUp();case.temp.cleanup()
    case.temp=SimpleNamespace(name=str(out),cleanup=lambda:None);case.path=out/'ledger.db'
    stop=threading.Event();paused=threading.Event();reduced=threading.Event();outcomes=[];failures=[]
    installed=Path(online_trader.__file__).resolve().parent
    try:
        case.entered();case.quote(1086);case.finish_last(1086)
        assert case.cycle()['protection']['owned_qty']==3
        pipe='OnlineTrader.Dsv.Rehearsal.'+uuid.uuid4().hex;server=ControlServer(case.kernel,pipe)
        for name,value in (('scope',case.grant.value),('profile',case.profile.to_record()),('calendar',case.calendar.to_record())):
            (out/('offline-'+name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        def client():
            try:
                for index,command in enumerate(('status','pause','status')):
                    if index==2:assert reduced.wait(10),'Second-stage facts never committed'
                    run=subprocess.run([sys.executable,'-I','-X','utf8','-m','online_trader',command,'--pipe',pipe],
                        cwd=out,capture_output=True,encoding='utf-8',timeout=12,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    row={'command':command,'returncode':run.returncode,'stdout':run.stdout,'stderr':run.stderr};outcomes.append(row)
                    assert run.returncode==0,row
                    data=json.loads(run.stdout)
                    if command=='status':
                        cycle=next(r for r in data['records'] if r['record_type']=='CYCLE')
                        assert cycle['remaining_quantity']==(3 if index==0 else 1),data
                    else:
                        assert data['status']=='SUCCEEDED',data;paused.set()
            except BaseException as error:failures.append(repr(error))
            finally:stop.set()
        worker=threading.Thread(target=client);worker.start();began=time.monotonic()
        def tick():
            case.session.step()
            if paused.is_set() and not reduced.is_set():
                case.quote(1096,high=1100);case.finish_last(1096)
                assert case.cycle()['protection']['owned_qty']==1
                reduced.set()
            if time.monotonic()-began>22:failures.append('DSV rehearsal deadline');stop.set()
        try:server.serve(stop,owner_tick=tick)
        finally:worker.join(timeout=15)
        assert not failures,failures
        assert not case.kernel._armed and case.cycle()['protection']['owned_qty']==1
        assert case.cycle()['protection']['phase2_status']=='DONE'
        assert case.cycle()['protection']['break_even_status']=='ACTIVE'
        report={'status':'PASS','scope':'OFFLINE_EXTERNAL_NATIVE_FACTS_PRODUCT_DSV_REAL_WINDOWS_IPC',
            'version':online_trader.__version__,'installed_root':str(installed),'broker_connected':False,'real_native_orders_sent':0,
            'product_execution_scope':case.cycle()['plan']['execution_scope'],'development_kernel':case.kernel.development,
            'source_sha256':{str(p.relative_to(installed)):hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in installed.rglob('*.py')},
            'recorded_adapter_calls':[{k:call[k] for k in ('symbol','direction','offset','volume','limit_price','order_id')} for call in case.api.inserted],
            'outcomes':outcomes,'pause_preserves_advanced_protection':True,'final_owned_quantity':1,
            'final_snapshot':case.kernel.snapshot(),'money_report':case.kernel.money.report(),
            'sqlite_integrity':case.kernel.ledger.query('PRAGMA integrity_check')}
        (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({k:report[k] for k in ('status','version','scope','product_execution_scope','broker_connected','real_native_orders_sent','final_owned_quantity')}))
        return 0
    finally:case.tearDown()


if __name__=='__main__':raise SystemExit(main())
