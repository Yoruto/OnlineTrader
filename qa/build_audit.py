"""Offline immutable wheel/clean-install audit against this exact full check."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',type=Path,required=True);args=parser.parse_args()
    from online_trader import __version__
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+__version__.split('.')[-1]+'-'+uuid.uuid4().hex[:8]
    out=ROOT/'output/artifacts'/name;out.mkdir(parents=True);report={'status':'RUNNING','version':__version__,'commands':[]}
    def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    def run(label,argv,cwd=ROOT):
        result=subprocess.run([str(a) for a in argv],cwd=cwd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180,
            env=os.environ|{'PYTHONUTF8':'1','PIP_NO_INDEX':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (out/(label+'.log')).write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        report['commands'].append({'name':label,'argv':[str(a) for a in argv],'cwd':str(cwd),'returncode':result.returncode})
        if result.returncode:raise RuntimeError(label+' failed')
        return result
    try:
        check=json.loads(args.check.read_text(encoding='utf-8'))
        assert check['status']=='passed' and check['product_version']==__version__ and check['source_stable'] and check['validation_stable']
        assert check['failures']==check['errors']==check['skipped']==0
        for p,sha in check['source_sha256'].items():assert digest(ROOT/p)==sha
        for p,sha in check['validation_sha256'].items():assert digest(ROOT/p)==sha
        product={p.relative_to(ROOT/'src').as_posix():p.read_bytes() for p in (ROOT/'src/online_trader').rglob('*')
            if p.is_file() and (p.suffix=='.py' or p.parent.name=='resources' and p.suffix in {'.json','.sql'})}
        manifest=[{'path':p,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)} for p,raw in sorted(product.items())]
        write(out/'source-manifest.json',manifest)
        stage=out/'build-source';stage.mkdir();shutil.copyfile(ROOT/'pyproject.toml',stage/'pyproject.toml')
        for name,raw in product.items():
            path=stage/'src'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        dist=ROOT/'dist'/out.name;dist.mkdir(parents=True)
        run('build',[sys.executable,'-m','pip','wheel','--no-deps','--no-build-isolation','.', '-w',dist],cwd=stage)
        wheel=dist/('online_trader-'+__version__+'-py3-none-any.whl');assert wheel.is_file()
        with zipfile.ZipFile(wheel) as archive:
            entries=archive.namelist();assert len(entries)==len(set(entries))
            packaged={name:archive.read(name) for name in entries if name.startswith('online_trader/')}
            assert packaged==product
            assert all(n.startswith('online_trader/') or n.startswith('online_trader-'+__version__+'.dist-info/') for n in entries)
        clean=out/'smoke-venv';run('create-clean-venv',[sys.executable,'-I','-m','venv',clean]);python=clean/'Scripts/python.exe'
        run('install-offline',[python,'-I','-m','pip','install','--no-index','--find-links',ROOT/'output/core-wheelhouse',wheel])
        run('pip-check',[python,'-I','-m','pip','check'])
        smoke_source=ROOT/'output/artifacts/20261110T080156Z-dev17-final-c9fd8967/smoke-source.py'
        # Re-execute the established business assertions against the new wheel;
        # the old artifact and its result remain untouched and are not reused.
        fixture=smoke_source.read_text(encoding='utf-8').replace('0.1.0.dev17',__version__)
        # DSV's ordinary entry wait includes the exact five-minute boundary.
        # Keep the positive smoke quote in the same candle, one second later.
        boundary='2026-11-09T01:05:00Z'
        assert fixture.count(boundary)==1
        smoke=out/'installed-smoke.py'
        smoke.write_text(fixture.replace(boundary,'2026-11-09T01:05:01Z'),encoding='utf-8')
        run('installed-smoke',[python,'-I',smoke],cwd=out)
        run('installed-dsv-ipc',[python,'-I',ROOT/'qa/dsv_session_rehearsal.py','--output',out/'installed-dsv-ipc'],cwd=out)
        run('installed-workbench-ipc',[python,'-I',ROOT/'qa/workbench_rehearsal.py','--output',out/'installed-workbench-ipc'],cwd=out)
        run('installed-crossday-ipc',[python,'-I',ROOT/'qa/crossday_rehearsal.py','--output',out/'installed-crossday-ipc'],cwd=out)
        install=clean/'Lib/site-packages'
        assert all((install/name).read_bytes()==raw for name,raw in product.items())
        assert not (install/'tqsdk').exists()
        for p,sha in check['source_sha256'].items():assert digest(ROOT/p)==sha
        for p,sha in check['validation_sha256'].items():assert digest(ROOT/p)==sha
        report.update(status='PASS',wheel_path=str(wheel),wheel_sha256=digest(wheel),source_python_count=sum(n.endswith('.py') for n in product),
            source_resource_count=sum(not n.endswith('.py') for n in product),source_wheel_installed_bytes_match=True,
            complete_check={'path':str(args.check.resolve()),'sha256':digest(args.check),'tests':check['tests']},
            source_sha256=check['source_sha256'],validation_sha256=check['validation_sha256'],
            isolated_smoke_template_sha256=digest(smoke_source),installed_smoke_sha256=digest(smoke),core_sdk_absent=True,
            broker_connections=0,real_orders=0,visible_online_acceptance='NOT_RUN')
    except BaseException as error:report.update(status='FAIL',error=str(error),traceback=traceback.format_exc())
    finally:
        write(out/'wheel-audit.json',report)
        print(json.dumps({k:report.get(k) for k in ('status','wheel_path','wheel_sha256','source_python_count','source_resource_count','error')}|{'audit':str(out/'wheel-audit.json')},ensure_ascii=False))
    return 0 if report['status']=='PASS' else 2


if __name__=='__main__':raise SystemExit(main())
