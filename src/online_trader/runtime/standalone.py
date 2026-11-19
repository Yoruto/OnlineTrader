"""Headless entry to the current, single-owner simulation execution path."""

import json
import sys

from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes, strict_loads
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
    if args.runtime_action == 'export-handoff':
        from online_trader.runtime.handoff import export_handoff
        store.profile()
        print(json.dumps(export_handoff(store, args.output), ensure_ascii=False))
        return 0
    selected = store.get(store.read()['selected_id'])
    pipe = store.pipe_name(selected)
    if args.runtime_action == 'account':
        from online_trader.transport.runtime_account import RuntimeAccountClient
        value = RuntimeAccountClient(pipe, expected_account_key=selected['account_key']).read()
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value['status'] == 'READY' else 2
    if args.runtime_action == 'history':
        value = _history(args, store.ledger_path(selected), selected['account_key'])
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if args.runtime_action == 'report':
        from online_trader.reporting import read_report
        value = read_report(store.ledger_path(selected), selected['account_key'],
                            checkpoint_event_seq=args.checkpoint_event_seq)
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if args.runtime_action == 'catalog':
        from online_trader.runtime.startup_metadata import read_contract_catalog
        print(json.dumps(read_contract_catalog(store, selected['account_id']), ensure_ascii=False))
        return 0
    if args.runtime_action in ('command', 'receipt'):
        return _operator_request(args, store, selected, pipe)
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
    if args.runtime_action == 'schedule':
        from online_trader.runtime.portable_schedule import schedule_session
        return schedule_session(store, selected, args.operator, _read_token(args))
    profile = store.profile()
    if args.runtime_action == 'quotes':
        from online_trader.runtime.startup_metadata import read_startup_metadata
        contracts = args.symbol or [row['code'] for row in profile.to_record()['settings']['symbols']]
        data = read_startup_metadata(store, selected['account_id'], contracts, quote_preview=True)
        print(json.dumps(data, ensure_ascii=False))
        return 0
    if args.runtime_action == 'inspect':
        print(json.dumps({'account': selected, 'profile_hash': profile.content_hash,
                          'pipe': pipe, 'ledger_exists': store.ledger_path(selected).is_file()}, ensure_ascii=False))
        return 0
    request = build_start_request(store, selected['account_id'], profile, arm=False, manual=args.mode == 'manual')
    return run_session(store, selected, request, use_profile_file=args.use_profile_file)


def _history(args, ledger, account_key):
    if all(value is None for value in (args.from_day, args.to_day, args.section, args.contract, args.cursor)):
        from online_trader.runtime.account_history import account_history
        return account_history(ledger, account_key, before=args.before, limit=5 if args.limit is None else args.limit)
    if args.before is not None:
        raise TraderError('E_HISTORY_CURSOR', 'Date queries use --cursor for pagination')
    from online_trader.runtime.account_day_history import read_account_days
    return read_account_days(ledger, account_key, from_day=args.from_day, to_day=args.to_day,
                             section=args.section or 'coverage', contract=args.contract, cursor=args.cursor,
                             limit=50 if args.limit is None else args.limit)


def run_session(store, selected, request, *, use_profile_file=False, check_scheduled_authorization=None):
    pipe = store.pipe_name(selected)
    directory = store.directory / 'desktop-starts' / request['request_id']
    request_path = publish_new_bytes(canonical_bytes(request), directory / 'request.json')
    output = directory / 'run'
    print(json.dumps({'account_key': selected['account_key'], 'pipe': pipe, 'run': str(output),
                      'entry_authority': 'DISARMED'}, ensure_ascii=False), flush=True)
    from online_trader.runtime.view_process import ViewProcess
    process = ViewProcess(pipe, selected['account_key'], store.directory, store.ledger_path(selected))
    try:
        result = run_confirmed_start(request_path, output, configured_store=store,
                                     trusted_control_pid=process.trusted_pid,
                                     use_portable_profile_file=use_profile_file,
                                     check_scheduled_authorization=check_scheduled_authorization)
    finally:
        process.close()
    failure = output / 'startup-failure.json'
    if result and failure.is_file():
        print(json.dumps({'status': 'STARTUP_FAILED', 'diagnostic': str(failure)}, ensure_ascii=False),
              file=sys.stderr)
    return result


def _operator_request(args, store, selected, pipe):
    from online_trader.runtime.operator_api import OperatorEndpoint, RuntimeCommandClient

    if args.runtime_action == 'receipt':
        endpoint = OperatorEndpoint(pipe, store.directory, store.ledger_path(selected), selected['account_key'])
        result = endpoint.handle({'operation': 'receipt', 'body': {'command_id': args.command_id},
                                  'operator': args.operator, 'token': _read_token(args)})
    else:
        try:
            with args.input.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            body = strict_loads(raw.decode('utf-8-sig'))
        except (OSError, ValueError):
            raise TraderError('E_SCHEMA', 'Cannot read the command JSON file') from None
        result = RuntimeCommandClient(pipe).request('submit', body, operator=args.operator, token=_read_token(args))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _read_token(args):
    from getpass import getpass

    return sys.stdin.readline(258).rstrip('\r\n') if args.token_stdin else getpass('本机操作令牌 ')
