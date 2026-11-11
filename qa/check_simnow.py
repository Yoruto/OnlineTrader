"""Necessary gateway/comparison/startup checks only; not the complete suite."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT))


def files():
    paths=[p for folder in ('src','tests','qa','scripts') for p in (ROOT/folder).rglob('*')
           if p.is_file() and p.suffix in {'.py','.ps1','.json','.sql'} and '__pycache__' not in p.parts]
    paths.append(ROOT/'pyproject.toml')
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--auto-offsets',action='store_true');args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False);before=files()
    for name in before:
        if name.endswith('.py'):ast.parse((ROOT/name).read_text(encoding='utf-8'))
    names=['tests.test_simnow_comparison',
        'tests.test_online_dsv_boundaries.DsvScopeBoundaryTests.test_manual_scope_cannot_be_reinterpreted_as_strategy_scope',
        'tests.test_online_dsv_boundaries.DsvPlanBoundaryTests.test_entry_intent_waits_for_matching_ack_before_any_native_call',
        'tests.test_online_dsv.OnlineDsvTests.test_final_relogin_closes_sender_and_checks_fresh_full_owned_facts',
        'tests.test_online_dsv_multi.MultiContractDsvTests.test_night_session_uses_explicit_next_cabinet_day_instead_of_utc_date',
        'tests.test_dsv_day_execution.DsvDayExecutionTests.test_unknown_missing_original_id_does_not_replay_or_allow_recovery',
        'tests.test_dsv_day_execution.DsvDayExecutionTests.test_mixed_today_historical_executes_distinct_children_on_one_original_target',
        'tests.test_simulation_recovery.RecoveryTests.test_zero_fill_cancel_keeps_reservation_until_matching_terminal']
    if args.auto_offsets:names.insert(0,'tests.test_auto_offsets_runtime')
    suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    with (args.output/'checks.log').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    after=files()
    from online_trader import __version__
    value={'status':'passed' if result.wasSuccessful() and before==after else 'failed','version':__version__,
        'scope':'Targeted SimNow gateway, clock, records and affected original-owner invariants only',
        'automatic_offsets_included':args.auto_offsets,
        'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'source_stable':before==after,'source_and_validation_sha256':after,
        'actual_sdk_modules_loaded':[name for name in sys.modules if name=='tqsdk' or name.startswith('tqsdk.')],
        'account_connections':0,'ntp_requests':0,'real_native_orders':0,'full_suite_run':False,'wheel_built':False}
    (args.output/'result.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in value.items() if k!='source_and_validation_sha256'},ensure_ascii=False,indent=2))
    return 0 if value['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
