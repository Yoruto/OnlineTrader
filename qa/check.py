"""Independent offline check gate, with immutable machine-readable run evidence."""

import ast
import compileall
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sqlite3
import sys
import unittest
import uuid
from online_trader import __version__


ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    output=ROOT/"output/checks"/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    sources=sorted((ROOT/"src").rglob("*.py"))+sorted((ROOT/"src/online_trader/resources").glob("*"))
    before={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources if path.is_file()}
    validation_files=sorted((ROOT/'tests').rglob('*.py'))+sorted((ROOT/'qa').rglob('*.py'))+[
        ROOT/'scripts/check.ps1',ROOT/'pyproject.toml',ROOT/'requirements-core.lock',
        ROOT/'requirements-desktop.lock',ROOT/'requirements-build.lock']+sorted((ROOT/'docs').glob('spec-*.json'))
    validation_before={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in validation_files}
    prohibited={"v7_trader","vnpy","wt9_v7","tqsdk","qa","tests","pickle"}
    dsv_core = ROOT / "src/online_trader/dsv_core"
    violations=[]
    for path in (ROOT/"src").rglob("*.py"):
        tree=ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ""] if isinstance(node,ast.ImportFrom) else []
            for module in modules:
                optional_sdk=(module=='tqsdk' and isinstance(node,ast.ImportFrom)
                    and ((((path.relative_to(ROOT).as_posix()=='src/online_trader/gateway/tqkq.py' and {a.name for a in node.names}=={'TqApi','TqKq','TqAuth'})
                           or (path.relative_to(ROOT).as_posix()=='src/online_trader/gateway/simnow.py' and {a.name for a in node.names}=={'TqApi','TqAccount','TqAuth'}))
                          and any(isinstance(parent,(ast.FunctionDef,ast.AsyncFunctionDef)) and parent.name=='connect' and node in ast.walk(parent) for parent in ast.walk(tree)))
                         or (path.relative_to(ROOT).as_posix()=='src/online_trader/runtime/startup_metadata.py'
                             and node.level==0 and {a.name for a in node.names}=={'TqApi','TqAuth','TqSim'}
                             and any(isinstance(parent,(ast.FunctionDef,ast.AsyncFunctionDef)) and parent.name=='_sdk_factory' and node in ast.walk(parent) for parent in ast.walk(tree)))))
                if module.split(".")[0] in prohibited and not optional_sdk:violations.append(str(path.relative_to(ROOT))+":"+module)
                if (path.is_relative_to(dsv_core) and module.startswith("online_trader.")
                        and not module.startswith(("online_trader.dsv_core.", "online_trader.domain."))):
                    violations.append("dsv-core-dependency:" + str(path.relative_to(ROOT)) + ":" + module)
    manifest=json.loads((ROOT/"docs/spec-source.json").read_text(encoding="utf-8"))
    workbench=json.loads((ROOT/"docs/spec-extension-workbench.json").read_text(encoding="utf-8"))
    extended=workbench.get("extends_copied_resources",{})
    for source,digest in extended.items():
        if manifest["copied_specification_resources"].get(source)!=digest or Path(source).name not in workbench["resources"]:
            violations.append("extension-source-drift:"+source)
    for source,digest in manifest["copied_specification_resources"].items():
        actual=ROOT/"src/online_trader/resources"/Path(source).name
        if source in extended:
            digest=workbench["resources"].get(Path(source).name)
        if hashlib.sha256(actual.read_bytes()).hexdigest()!=digest:violations.append("spec-resource-drift:"+source)
    for manifest_name in ("spec-extension-dsv-protection.json","spec-extension-dsv-execution.json","spec-extension-dsv-online.json","spec-extension-workbench.json"):
        extension=json.loads((ROOT/"docs"/manifest_name).read_text(encoding="utf-8"))
        for name,digest in extension["resources"].items():
            actual=ROOT/"src/online_trader/resources"/name
            if hashlib.sha256(actual.read_bytes()).hexdigest()!=digest:violations.append("extension-resource-drift:"+name)
    compiled=all(compileall.compile_dir(str(ROOT/name),quiet=1) for name in ("src","tests","qa"))
    suite=unittest.defaultTestLoader.discover(str(ROOT/"tests"))
    with (output/"check.log").open("w",encoding="utf-8") as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    after={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources if path.is_file()}
    validation_after={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in validation_files}
    passed=bool(result.testsRun and result.wasSuccessful() and not result.skipped and not result.expectedFailures
                and not result.unexpectedSuccesses and compiled and not violations and before==after and validation_before==validation_after)
    report={"status":"passed" if passed else "failed","product_version":__version__,"scope":"offline native calendar-bound order/fill days, cross-day pending-fact consumption, cycle/target/bucket close reservations and final send checks plus prior accounts/workbench/DSV/G2/Windows IPC; zero SDK connections or real orders; no cross-day online send, online acceptance, performance or durability claim",
        "python":sys.version,"platform":platform.platform(),"sqlite":sqlite3.sqlite_version,
        "dependencies":{n:importlib.metadata.version(n) for n in ["jsonschema","referencing","PySide6"]},
        "tests":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),"skipped":len(result.skipped),
        "compile_passed":compiled,"boundary_violations":violations,"source_stable":before==after,"source_sha256":after,
        "validation_stable":validation_before==validation_after,"validation_sha256":validation_after}
    (output/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"{report['status']}: {result.testsRun} tests; {len(result.failures)} failures; {len(result.errors)} errors; {len(result.skipped)} skipped")
    print(output/"result.json")
    if not passed:
        print((output/"check.log").read_text(encoding="utf-8"))
    return 0 if passed else 1


if __name__=="__main__":raise SystemExit(main())
