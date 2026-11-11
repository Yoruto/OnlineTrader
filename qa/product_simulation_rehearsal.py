"""Installed-wheel IPC rehearsal with external offline facts; never connects an SDK."""
from datetime import timedelta
from pathlib import Path
import argparse,importlib.metadata,json,subprocess,sys,threading,time,uuid
import online_trader

INSTALLED=Path(online_trader.__file__).resolve().parent
ROOT=Path(__file__).resolve().parents[1]
assert sys.flags.isolated and (INSTALLED.parent.name=='site-packages' or INSTALLED==ROOT/'src/online_trader')
sys.path.insert(0,str(ROOT)) # Test fixtures stay outside the installed production package.
from tests.test_online_simulation import FakeOnlinePort,grant_for
from online_trader.kernel import Kernel
from online_trader.kernel.server import ControlServer
from online_trader.runtime.simulation import SimulationSession

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output=args.output;output.mkdir(parents=True)
    grant=grant_for(output/'ledger.db');port=FakeOnlinePort(grant)
    port.clock.value-=timedelta(minutes=1);port.clock.monotonic_anchor=time.monotonic();port.poll_updates=lambda seconds:time.sleep(min(seconds,0.01))
    pipe='OnlineTrader.Rehearsal.'+uuid.uuid4().hex;results={};failures=[]
    with Kernel(grant.value['ledger_path'],grant.account_key,port=port,simulation_grant=grant,risk_budget='1000') as kernel:
        kernel.simulation.initialize();session=SimulationSession(kernel,output/'session',emit=lambda value:None)
        server=ControlServer(kernel,pipe)
        def client():
            try:
                time.sleep(.3)
                for command in ('status','pause'):
                    run=subprocess.run([sys.executable,'-I','-X','utf8','-m','online_trader',command,'--pipe',pipe],capture_output=True,text=True,encoding='utf-8',timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    (output/(command+'.stdout.json')).write_text(run.stdout,encoding='utf-8');(output/(command+'.stderr.txt')).write_text(run.stderr,encoding='utf-8')
                    assert run.returncode==0,(command,run.returncode)
                    results[command]=json.loads(run.stdout)
            except BaseException as error:failures.append(repr(error));session.stop.set()
        worker=threading.Thread(target=client);worker.start()
        started=time.monotonic()
        def tick():
            session.step()
            if time.monotonic()-started>20:failures.append('rehearsal timeout');session.stop.set()
        server.serve(session.stop,owner_tick=tick);worker.join(timeout=20)
        assert not failures,failures
        assert session.result['status']=='PAUSED_BEFORE_OPEN',session.result
        assert not port.sent and not kernel._staged
        assert Path(online_trader.__file__).resolve().parent==INSTALLED
        report={'status':'PASS','scope':'OFFLINE_EXTERNAL_FACTS_'+('INSTALLED_WHEEL' if INSTALLED.parent.name=='site-packages' else 'SOURCE')+'_IPC','broker_connected':False,
            'version':online_trader.__version__,'installed_root':str(INSTALLED),
            'status_and_pause_cross_process':True,'pause_prevents_rearm':True,'native_orders_sent':0,
            'durable_operator_commands':len(kernel.ledger.query('SELECT * FROM operator_commands')),
            'final_snapshot':kernel.snapshot(),'session_result':session.result}
        (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:report[k] for k in ('status','scope','version','pause_prevents_rearm','native_orders_sent','durable_operator_commands')}))

if __name__=='__main__':main()
