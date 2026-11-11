"""Original product owner + separate visible workbench/CLI, external facts only."""
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
from online_trader.kernel.server import ControlServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests import test_online_dsv as fixtures


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--visible',action='store_true');args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);case=fixtures.OnlineDsvTests();case.setUp();case.temp.cleanup()
    case.temp=SimpleNamespace(name=str(out),cleanup=lambda:None);case.path=out/'ledger.db'
    stop=threading.Event();results=[];failures=[];reduced=False
    try:
        case.entered();case.quote(1086);case.finish_last(1086);assert case.cycle()['protection']['owned_qty']==3
        pipe='OnlineTrader.Workbench.'+uuid.uuid4().hex;server=ControlServer(case.kernel,pipe)
        def client():
            try:
                if args.visible:
                    command=[sys.executable,'-X','utf8','-m','qa.workbench_client','--pipe',pipe,'--output',str(out)]
                    env=os.environ|{'QT_QPA_PLATFORM':'windows','PYTHONUTF8':'1'}
                    process=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=25,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    (out/'desktop.log').write_text(process.stdout+'\n'+process.stderr,encoding='utf-8');assert process.returncode==0,process.stderr
                else:
                    for action in ('workbench-status','pause','workbench-status'):
                        if len(results)==2:
                            deadline=time.monotonic()+10
                            while not (out/'reduced.json').exists():
                                assert time.monotonic()<deadline;time.sleep(.02)
                        process=subprocess.run([sys.executable,'-I','-X','utf8','-m','online_trader',action,'--pipe',pipe],cwd=out,
                            capture_output=True,text=True,encoding='utf-8',timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        assert process.returncode==0,process.stderr;data=json.loads(process.stdout);results.append({'action':action,'result':data})
                    assert results[0]['result']['cycles'][0]['remaining_quantity']==3
                    assert results[2]['result']['cycles'][0]['remaining_quantity']==1
                    assert results[2]['result']['cycles'][0]['break_even_status']=='ACTIVE'
            except BaseException as error:failures.append(str(error))
            finally:stop.set()
        worker=threading.Thread(target=client);worker.start();began=time.monotonic()
        def tick():
            nonlocal reduced
            case.session.step()
            if not case.kernel._armed and not reduced:
                case.quote(1096,high=1100);case.finish_last(1096);reduced=True
                (out/'reduced.json').write_text('{"quantity":1}',encoding='utf-8')
            if time.monotonic()-began>28:failures.append('Owner deadline');stop.set()
        try:server.serve(stop,owner_tick=tick)
        finally:worker.join(timeout=30)
        assert not failures,failures
        assert case.cycle()['protection']['owned_qty']==1 and case.cycle()['protection']['break_even_status']=='ACTIVE'
        product=Path(online_trader.__file__).parent
        report={'status':'PASS','version':online_trader.__version__,'scope':'OFFLINE_EXTERNAL_NATIVE_FACTS_REAL_WORKBENCH_IPC',
            'broker_connected':False,'real_native_orders_sent':0,'development_kernel':case.kernel.development,
            'execution_scope':case.cycle()['plan']['execution_scope'],'visible_desktop':args.visible,'final_owned_quantity':1,
            'gui_close_kept_owner_protection':True if args.visible else None,
            'client_disconnect_kept_owner_protection':True,'results':results,'final_snapshot':case.kernel.snapshot(),
            'source_sha256':{str(p.relative_to(product)):hashlib.sha256(p.read_bytes()).hexdigest() for p in product.rglob('*.py')}}
        (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:report[k] for k in ('status','version','scope','visible_desktop','broker_connected','final_owned_quantity')}));return 0
    finally:case.tearDown()


if __name__=='__main__':raise SystemExit(main())
