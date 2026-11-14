"""Explicit finite simulation startup for the replacement owner."""

from contextlib import ExitStack
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time

from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.dsv_runtime.migration import _COLD_CHECKS, _readonly, _source_digest, _source_schema, read_migration_receipt
from online_trader.dsv_runtime.controller import terminal
from online_trader.dsv_runtime.offline_endpoint import _handle, serve_request, validate_offline_message
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import utc
from online_trader.gateway.simnow import strategy_port
from online_trader.runtime.dsv_simulation import _claim_run_output, load_inputs
from online_trader.runtime.instance import AccountLock
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.reference import LocalWallClock
from online_trader.transport.named_pipe import PipeListener


def sdk_version():
    try:
        installed = version('tqsdk')
    except PackageNotFoundError:
        raise TraderError('E_DEPENDENCY', 'The pinned TqSdk 3.10.4 is required') from None
    if installed != SDK_VERSION:
        raise TraderError('E_DEPENDENCY', 'The replacement requires the pinned TqSdk 3.10.4')
    return installed


def _validate(grant, profile, database):
    if not grant.replacement:
        raise TraderError('E_ONLINE_SCOPE', 'Expected an explicit replacement grant')
    if set(grant.value['contracts']) != {row['code'] for row in profile.to_record()['settings']['symbols']}:
        raise TraderError('E_ONLINE_SCOPE', 'The replacement profile and grant must cover the same contracts')
    legacy = Path(grant.value['ledger_path']).resolve()
    database = Path(database).resolve()
    if (database == legacy or database.parent == legacy.parent or
            database.exists() and legacy.exists() and database.samefile(legacy)):
        raise TraderError('E_DSV_STORE', 'Use a distinct replacement data directory')
    return legacy, database


def _scope(grant, profile):
    return {'account_key': grant.account_key, 'provider': grant.provider, 'trading_day': grant.value['trading_day'],
            'configuration_id': profile.content_hash}


def _validate_source(grant, profile, legacy, database):
    existing = database.exists()
    if existing:
        with _readonly(database) as db:
            imported = read_migration_receipt(db)
            row = db.execute('SELECT payload FROM scope WHERE id=1').fetchone()
            if (imported['target_scope'] != _scope(grant, profile) or row is None or
                    strict_loads(row[0]) != imported['target_scope']):
                raise TraderError('E_DSV_STORE', 'Replacement facts have a different account or configuration')
            binding = db.execute("SELECT payload FROM events WHERE kind='SDK_SCOPE_BOUND' ORDER BY seq LIMIT 1").fetchone()
            if binding is not None and strict_loads(binding[0]) != {
                    'grant_id': grant.value['grant_id'], 'grant_hash': grant.content_hash}:
                raise TraderError('E_DSV_SDK_SCOPE', 'Stored replacement owner belongs to a different grant')
    if not legacy.is_file() or not existing:
        raise TraderError('E_DSV_MIGRATION_SCOPE', 'Prepare a bound, cold-migrated candidate database first')
    with _readonly(legacy) as source:
        _source_schema(source)
        scope = source.execute('SELECT account_key,environment,broker_identity FROM ledger_scope').fetchone()
        if scope is None or tuple(scope) != (grant.account_key, 'ONLINE_SIM', grant.provider):
            raise TraderError('E_DSV_MIGRATION_SCOPE', 'The original archive belongs to a different account')
        if (imported['source_account_hash'] != content_hash(grant.account_key) or
                imported['source_digest'] != _source_digest(source) or
                any(source.execute(query).fetchone() for query in _COLD_CHECKS.values())):
            raise TraderError('E_DSV_MIGRATION_SCOPE', 'The original owner facts differ from the verified cold migration')
    return imported


def prepare(scope, profile_path, calendar_path, database, output):
    grant, profile, calendar = load_inputs(scope, profile_path, calendar_path)
    legacy, database = _validate(grant, profile, database)
    with AccountLock(grant.account_key):
        _validate_source(grant, profile, legacy, database)
    record = {'type': 'DSV_REPLACEMENT_PREPARE/1', 'scope_hash': grant.content_hash,
              'calendar_hash': calendar.content_hash, 'account_key': grant.account_key,
              'database': str(database), 'broker_connected': False, 'entry_authority': 'DISARMED'}
    publish_new_bytes(canonical_bytes(record), Path(output) / 'replacement-preflight.json')
    return record


def _date_resolver(grant, calendar):
    def cabinet_day(previous, now):
        windows = [calendar.query(code, utc(now)) for code in grant.value['contracts']]
        days = {item['trading_day'] for item in windows if item['is_trading']}
        if (not all(item['complete'] for item in windows) or len(days) > 1 or
                any(not grant.permits_day(day) for day in days)):
            raise TraderError('E_DSV_SDK_SYNC', 'Current cabinet day is outside the approved calendar')
        return next(iter(days), previous)
    return cabinet_day


def _status(path, value):
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.sdk-status-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(canonical_bytes(value))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(scope, credentials, profile_path, calendar_path, database, output, *, pipe):
    grant, profile, calendar = load_inputs(scope, profile_path, calendar_path)
    legacy, database = _validate(grant, profile, database)
    output = Path(output).resolve()
    date_resolver = _date_resolver(grant, calendar)
    wall = LocalWallClock()
    with ExitStack() as stack:
        lock = stack.enter_context(AccountLock(grant.account_key))
        imported = _validate_source(grant, profile, legacy, database)
        installed = sdk_version()
        now = wall.now()
        date_resolver(grant.value['trading_day'], now)
        if now < datetime.fromisoformat(grant.value['not_before_utc']):
            raise TraderError('E_ONLINE_SCOPE', 'The replacement scope has not started')
        with _readonly(database) as db:
            saved = strict_loads(db.execute('SELECT payload FROM checkpoint WHERE id=1').fetchone()[0])
            recovering = any(row['position'] or row.get('detached_positions') or row.get('native_conflicts')
                             for row in saved['symbols'].values())
            recovering |= any(not terminal(order) for order in saved['orders'].values())
        if not grant.in_window(now) and not recovering:
            raise TraderError('E_ONLINE_SCOPE', 'The replacement scope has expired without pending responsibilities')
        _claim_run_output(output, grant)
        status_path = output / 'status.json'
        _status(status_path, {'phase': 'STARTING', 'entry_authority': 'DISARMED', 'account_key': grant.account_key})
        try:
            with ExitStack() as owner:
                connection = strategy_port(grant).connect(credentials, grant, wall)
                owner.callback(connection.close)
                if (getattr(connection, 'grant', None) != grant or getattr(connection, 'provider', None) != grant.provider
                        or not connection.broker_connected or connection.read_only):
                    raise TraderError('E_DSV_SDK_SCOPE', 'Connected SDK provider and account differ from the startup grant')
                connection.configure_calendar(calendar)
                initial_snapshot = connection.snapshot()
                if initial_snapshot.get('account_key') != grant.account_key or (
                        initial_snapshot.get('trading_day') is not None
                        and not grant.permits_day(initial_snapshot['trading_day'])):
                    raise TraderError('E_DSV_SDK_SCOPE', 'Initial SDK snapshot differs from the approved account or day')
                adapter = DsvSdkAdapter(connection.api, connection.account, account_key=grant.account_key, profile=profile,
                                        calendar=calendar, sdk_version=installed,
                                        clock=lambda: (wall.now(), time.monotonic_ns() // 1_000_000), _connection=connection,
                                        _legacy_native_orders=imported.get('legacy_native_orders'))
                session = owner.enter_context(DsvSession(
                    database, profile=profile, port=adapter, _execution_lock=lock, account_key=grant.account_key,
                    provider=grant.provider, trading_day=grant.value['trading_day']))
                session.disconnect('SDK_OWNER_STARTED')
                session.disable_entries_on_restart()
                listener = owner.enter_context(PipeListener(pipe, validator=validate_offline_message))

                def complete():
                    return connection.broker_connected and connection.complete()
                return _serve(session, adapter, listener, date_resolver, status_path, complete)
        except Exception as error:
            try:
                _status(status_path, {'phase': 'NEEDS_REVIEW', 'entry_authority': 'DISARMED',
                                      'account_key': grant.account_key, 'error_code': getattr(error, 'code', 'E_DSV_SDK_RUNTIME')})
            except OSError:
                pass
            if isinstance(error, TraderError):
                raise
            raise TraderError('E_DSV_SDK_RUNTIME', 'Replacement startup or cleanup failed') from None


def _serve(session, adapter, listener, day, status_path, complete):
    previous = None
    stopping = False

    def on_request(request):
        nonlocal stopping
        if request['op'] == 'shutdown':
            snapshot = session.snapshot()
            result = session.prepare_shutdown()
            can_exit = result['ready'] and adapter.flat_confirmed and snapshot['synchronized'] and not snapshot['running']
            if not can_exit:
                raise TraderError('E_DSV_SDK_SHUTDOWN', 'Stop and verify scope-flat orders and trades before shutting down')
            stopping = True
            return result
        return _handle(session, request, sdk=adapter)

    while not stopping:
        error_code = None
        try:
            adapter.step(session, trading_day=day, complete=complete)
        except KeyboardInterrupt:
            adapter.command(session, {'id': 'sdk-console-stop', 'action': 'STOP'})
            continue
        except (TraderError, OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            if session._failed:
                raise
            error_code = getattr(error, 'code', 'E_DSV_SDK_RUNTIME')
            time.sleep(0.1)
        state = session.snapshot()
        payload = {'phase': 'READY' if state['synchronized'] else 'WAITING_FOR_NATIVE_SYNC',
                   'account_key': state['scope']['account_key'], 'trading_day': state['trading_day'],
                   'entry_authority': 'RUNNING' if state['running'] else 'DISARMED', 'error_code': error_code}
        if payload != previous:
            try:
                _status(status_path, payload)
            except OSError:
                pass
            previous = payload
        serve_request(listener, on_request, timeout=0.025)
    _status(status_path, payload | {'phase': 'STOPPED', 'entry_authority': 'DISARMED'})
    return 0
