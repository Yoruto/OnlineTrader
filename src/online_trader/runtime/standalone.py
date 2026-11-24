"""Headless entry to the current, single-owner simulation execution path."""

import json
import os
import sys

from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.runtime.desktop_bootstrap import build_start_request, run_confirmed_start
from online_trader.runtime.portable_config import PortableAccountStore


def run_cli(args):
    if args.runtime_action == 'schema':
        from importlib.resources import files
        print(files('online_trader').joinpath('resources/runtime-ipc.schema.json').read_text(encoding='utf-8'), end='')
        return 0
    if args.runtime_action == 'account' and not args.attached:
        from online_trader.runtime.account_query import read_current_account
        print(json.dumps(read_current_account(args.root, account=args.account), ensure_ascii=False))
        return 0
    store = PortableAccountStore(args.root, getattr(args, 'account', None))
    selected = store.get(store.read()['selected_id'])
    pipe = store.pipe_name(selected)
    if args.runtime_action == 'account':
        from online_trader.transport.runtime_account import RuntimeAccountClient
        value = RuntimeAccountClient(pipe, expected_account_key=selected['account_key']).read()
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value['status'] == 'READY' else 2
    if args.runtime_action == 'history':
        value = _history(args, store.history_path(selected), selected['account_key'])
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if args.runtime_action == 'report':
        from online_trader.runtime.account_cache import broker_report
        value = broker_report(store.history_path(selected), selected['account_key'])
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if args.runtime_action == 'catalog':
        from online_trader.runtime.startup_metadata import read_contract_catalog
        print(json.dumps(read_contract_catalog(store, selected['account_id']), ensure_ascii=False))
        return 0
    if args.runtime_action in ('status', 'watch'):
        from online_trader.transport.runtime_view import RuntimeViewClient
        client = RuntimeViewClient(pipe)
        if args.runtime_action == 'status':
            value = client.read()
            print(json.dumps(value, ensure_ascii=False))
            return 2 if value['kind'] == 'UNAVAILABLE' else 0
        try:
            for message in client.subscribe():
                print(json.dumps(message, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            pass
        return 0
    profile = store.profile()
    if args.runtime_action == 'quotes':
        from online_trader.runtime.startup_metadata import read_startup_metadata
        contracts = args.symbol or [row['code'] for row in profile.to_record()['settings']['symbols']]
        data = read_startup_metadata(store, selected['account_id'], contracts, quote_preview=True)
        print(json.dumps(data, ensure_ascii=False))
        return 0
    if args.runtime_action == 'inspect':
        print(json.dumps({'account': selected, 'profile_hash': profile.content_hash,
                          'pipe': pipe, 'history_cache_exists': store.history_path(selected).is_file(),
                          'archived_legacy_ledger_exists': store.ledger_path(selected).is_file()}, ensure_ascii=False))
        return 0
    if getattr(args, 'mode', 'strategy') != 'strategy':
        raise TraderError('E_ONLINE_SCOPE', 'Standalone target execution requires a strategy runtime scope')
    request = build_start_request(store, selected['account_id'], profile, arm=False, manual=False)
    return run_session(
        store, selected, request,
        use_profile_file=args.use_profile_file,
        workbench_expected=bool(getattr(args, 'desktop_bootstrap', False)),
    )


def _history(args, ledger, account_key):
    if all(value is None for value in (args.from_day, args.to_day, args.section, args.contract, args.cursor)):
        from online_trader.runtime.account_cache import recent_history
        return recent_history(ledger, account_key, before=args.before, limit=5 if args.limit is None else args.limit)
    if args.before is not None:
        raise TraderError('E_HISTORY_CURSOR', 'Date queries use --cursor for pagination')
    from online_trader.runtime.account_cache import read_account_days
    return read_account_days(ledger, account_key, from_day=args.from_day, to_day=args.to_day,
                             section=args.section or 'coverage', contract=args.contract, cursor=args.cursor,
                             limit=50 if args.limit is None else args.limit)


def run_session(store, selected, request, *, use_profile_file=False, workbench_expected=False):
    pipe = store.pipe_name(selected)
    directory = store.directory / 'desktop-starts' / request['request_id']
    request_path = publish_new_bytes(canonical_bytes(request), directory / 'request.json')
    output = directory / 'run'
    print(json.dumps({'account_key': selected['account_key'], 'pipe': pipe, 'run': str(output),
                      'entry_authority': 'DISARMED'}, ensure_ascii=False), flush=True)
    from online_trader.runtime.instance import RuntimeLock
    from online_trader.runtime.instance_registry import RuntimeInstanceRegistration
    from online_trader.runtime.session_storage import RuntimeSessionStorage
    from online_trader.runtime.view_process import ViewProcess
    result = 2
    runtime_boot_id = None
    runtime_process_instance_id = None
    report_reason = 'STARTUP_FAILED'
    with RuntimeLock() as runtime_lock, RuntimeSessionStorage() as session:
        from online_trader.diagnostics import account_pseudonym, open_sink
        runtime_boot_id = session.boot_id
        runtime_process_instance_id = f'{runtime_boot_id}-{os.getpid()}'
        try:
            pseudonym = account_pseudonym(store.directory, selected['account_key'])
        except Exception:
            pseudonym = None
        diagnostics = open_sink(
            store.directory / 'diagnostics',
            process_role='RUNTIME_OWNER',
            process_instance_id=runtime_process_instance_id,
            stream_group='runtime',
            runtime_boot_id=session.boot_id,
            account_pseudonym=pseudonym,
            configuration_revision='1',
        )
        diagnostics.record(
            'RUNTIME_STARTED', component='LIFECYCLE', priority='HIGH',
            payload={'entry_authority': 'DISARMED', 'execution_core': 'DSV_TARGET_EXECUTION'},
        )
        def request_market_close_report(*, report_reason, trigger_key):
            from online_trader.diagnostics.reporter import request_report_async
            return request_report_async(
                store.directory,
                report_reason=report_reason,
                runtime_boot_id=runtime_boot_id,
                clean_shutdown=False,
                runtime_process_instance_id=runtime_process_instance_id,
                trigger_key=trigger_key,
                runtime_view_expected=True,
                workbench_expected=workbench_expected,
            )
        process = None
        try:
            process = ViewProcess(pipe, selected['account_key'], store.directory, session.ledger,
                                  store.history_path(selected))
            with RuntimeInstanceRegistration(store, selected, pipe):
                result = run_confirmed_start(request_path, output, configured_store=store,
                                             trusted_control_pid=process.trusted_pid,
                                             use_portable_profile_file=use_profile_file,
                                             execution_ledger=session.ledger,
                                             history_cache=store.history_path(selected),
                                             runtime_lock=runtime_lock,
                                             runtime_boot_id=session.boot_id,
                                             session_storage=session,
                                             diagnostic_sink=diagnostics,
                                             diagnostic_report_request=request_market_close_report)
        finally:
            if process is not None:
                process.close()
            report_reason = 'CONTROLLED_EXIT' if result == 0 else 'STARTUP_OR_RUNTIME_FAILURE'
            try:
                result_code = result if type(result) is int and result >= 0 else 2
                diagnostics.record(
                    'RUNTIME_STOPPED' if result == 0 else 'RUNTIME_FAILED',
                    component='LIFECYCLE', severity='INFO' if result == 0 else 'ERROR',
                    priority='CRITICAL',
                    error_code=None if result == 0 else 'E_RUNTIME_SESSION',
                    payload={'result_code': result_code, 'entry_authority': 'DISARMED'},
                )
            finally:
                diagnostics.finalize(
                    clean_shutdown=result == 0, snapshot_available=result == 0, timeout=1.0)
    if runtime_process_instance_id is not None:
        try:
            from online_trader.diagnostics.reporter import create_report_request, launch_reporter
            report_request = create_report_request(
                store.directory,
                report_reason=report_reason,
                runtime_boot_id=runtime_boot_id,
                clean_shutdown=result == 0,
                runtime_process_instance_id=runtime_process_instance_id,
                workbench_expected=workbench_expected,
            )
            launch_reporter(store.directory, report_request)
        except Exception:
            pass
    failure = output / 'startup-failure.json'
    if result and failure.is_file():
        print(json.dumps({'status': 'STARTUP_FAILED', 'diagnostic': str(failure)}, ensure_ascii=False),
              file=sys.stderr)
    return result
