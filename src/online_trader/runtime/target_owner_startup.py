"""Standalone lifecycle host for the DSV target-execution candidate.

The desktop cutover calls this owner only after compiling the same account,
profile and calendar scope used by the current Runtime.  Construction is
DISARMED and the first complete native frame remains mandatory.
"""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from online_trader.contracts.codec import canonical_bytes
from online_trader.configuration.files import publish_new_bytes, publish_replace_bytes
from online_trader.dsv_runtime.order_diagnostics import attach_order_blocking_log
from online_trader.dsv_runtime.standalone_owner import create_standalone_owner
from online_trader.dsv_runtime.target_diagnostics import TargetMarketDiagnostics
from online_trader.errors import TraderError, exception_record
from online_trader.gateway.simnow import strategy_port
from online_trader.kernel.server import ControlServer
from online_trader.runtime.account_cache import BrokerHistoryWriter
from online_trader.runtime.identity import runtime_identity
from online_trader.runtime.owner_tick import classify_owner_tick_error, execute_owner_tick
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.clock import utc_text


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def resolve_active_trading_day(calendar, contracts, current_day, now=None, *, clock=None):
    """Advance the cabinet day when currently active contracts agree on a trading day."""
    if calendar is None or not contracts:
        return current_day
    if now is None and clock is not None:
        capture = getattr(clock, 'capture_live', None) or getattr(clock, 'capture', None)
        sample = capture() if callable(capture) else None
        now_utc = getattr(sample, 'now_utc', None)
    elif isinstance(now, str):
        now_utc = now
    elif now is not None:
        now_utc = utc_text(now)
    else:
        now_utc = None
    if not now_utc:
        return current_day
    windows = [calendar.query(code, now_utc) for code in contracts]
    if any(not window.get('complete') for window in windows):
        return current_day
    active_windows = [
        window for window in windows
        if window.get('is_trading') and window.get('session')
    ]
    if not active_windows:
        return current_day
    active_days = {window['session'].get('trading_day') for window in active_windows}
    if len(active_days) > 1:
        raise TraderError('E_DSV_SCOPE', 'Active contracts disagree on cabinet trading day')
    candidate = next(iter(active_days))
    if candidate and (not current_day or candidate >= current_day):
        return candidate
    return current_day


def _resolve_trusted_pid(trusted_control_pid):
    """Resolve a static or dynamically queried trusted control PID."""
    pid = trusted_control_pid() if callable(trusted_control_pid) else trusted_control_pid
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def _build_close_deadlines(calendar, contracts):
    """Map each trading day with complete contract coverage to its latest forced close."""
    if calendar is None or not contracts:
        return {}
    to_record = getattr(calendar, 'to_record', None)
    if not callable(to_record):
        return {}
    close_schedule = {}
    for row in to_record().get('contracts', ()):
        code = row.get('contract_id')
        if code not in contracts:
            continue
        for session in row.get('sessions', ()):
            if session.get('force_close_at_end'):
                day = session.get('trading_day')
                close_text = session.get('close_utc')
                if not day or not close_text:
                    continue
                close_at = datetime.fromisoformat(close_text.replace('Z', '+00:00'))
                previous = close_schedule.setdefault(day, {}).get(code)
                close_schedule[day][code] = close_at if previous is None else max(previous, close_at)
    return {
        day: max(closes.values())
        for day, closes in close_schedule.items()
        if set(closes) == set(contracts)
    }


def _maybe_request_market_close(*, close_deadlines, clock, loop_state, boot_id, calendar_hash,
                                diagnostic_sink, diagnostic_report_request, now_monotonic=None,
                                allowed_days=None):
    """Trigger one market-close diagnostic report per associated trading day once past its deadline."""
    if diagnostic_report_request is None:
        return False
    check_at = time.monotonic() if now_monotonic is None else float(now_monotonic)
    if check_at - loop_state['diagnostic_close_check_monotonic'] < 10.0:
        return False
    loop_state['diagnostic_close_check_monotonic'] = check_at
    if not close_deadlines:
        return False
    # Compare aware datetimes, never UTC strings (seconds vs microseconds).
    now = clock.now().astimezone(timezone.utc)
    reported = loop_state['diagnostic_reported_close_keys']
    triggered = False
    for day, deadline in sorted(close_deadlines.items()):
        if allowed_days is not None and day not in allowed_days:
            continue
        if now < deadline:
            continue
        close_key = f'{boot_id}:{day}:{calendar_hash}'
        if close_key in reported:
            continue
        reported.add(close_key)
        triggered = True
        if diagnostic_sink is not None:
            diagnostic_sink.record(
                'REPORT_CUT_REQUEST', component='LIFECYCLE', priority='HIGH',
                correlation={'trading_day': day},
                payload={
                    'trigger': 'MARKET_CLOSE',
                    'calendar_hash': calendar_hash,
                    'close_deadline_utc': utc_text(deadline),
                    'observed_at_utc': utc_text(now),
                },
            )
        diagnostic_report_request(report_reason='MARKET_CLOSE', trigger_key=close_key)
    return triggered


def run_target_owner(*, grant, profile, calendar, credentials, output, pipe,
                     session_storage, runtime_lock, account_lock, clock,
                     trusted_control_pid, history_cache, arm=False,
                     configuration_revision='1', entry_point=None, configured_store=None,
                     diagnostic_sink=None, diagnostic_report_request=None):
    """Run the single target owner on the calling Runtime thread."""
    if grant.replacement or grant.manual_workbench:
        raise TraderError('E_ONLINE_SCOPE', 'Standalone target execution requires a strategy desktop scope')
    if Path(grant.value['ledger_path']).resolve() != session_storage.ledger:
        raise TraderError('E_DSV_SCOPE', 'Target execution must use this boot temporary Journal')
    binding = session_storage.owner_binding(
        runtime_lock, account_lock, account_key=grant.account_key, provider=grant.provider)
    output = Path(output).resolve()
    connection = None
    owner = None
    history_writer = None
    status = output / 'target-owner-status.json'
    diagnostics = output / 'target-owner-diagnostics.json'
    try:
        if diagnostic_sink is not None:
            diagnostic_sink.record(
                'OWNER_CONNECTING', component='CONNECTION', priority='HIGH',
                payload={'provider': grant.provider, 'entry_authority': 'DISARMED'},
            )
        connection = strategy_port(grant).connect(credentials, grant, clock)
        connection.configure_calendar(calendar)
        if diagnostic_sink is not None:
            diagnostic_sink.record(
                'OWNER_CONNECTED', component='CONNECTION', priority='HIGH',
                payload={'provider': grant.provider, 'entry_authority': 'DISARMED'},
            )
        if grant.automatic_offsets:
            from online_trader.runtime.auto_offsets import load_preparation, prepare_connected
            restored = load_preparation(grant, profile)
            if restored is not None:
                _, grant, profile = restored
            else:
                preparation, grant, profile = prepare_connected(grant, profile, connection)
                publish_replace_bytes(canonical_bytes({
                    'status': 'CALCULATED_BEFORE_TARGET_OWNER',
                    'preparation_hash': preparation['preparation_hash'],
                    'effective_profile_hash': profile.content_hash,
                    'offset_changes': preparation['offset_changes'],
                    'entry_authority': 'DISARMED',
                }), output / 'auto-offsets-completed.json')
        owner = create_standalone_owner(
            session_storage.ledger, binding=binding, connection=connection,
            profile=profile, calendar=calendar, clock=clock,
            configuration_revision=configuration_revision)
        if diagnostic_sink is not None:
            trackers = (
                getattr(owner.session, '_diagnostics', None),
                getattr(getattr(owner.session, '_controller', None), '_diagnostics', None),
                getattr(owner.adapter, '_diagnostics', None),
            )
            for tracker in trackers:
                bind = getattr(tracker, 'bind_evidence_sink', None)
                if callable(bind):
                    bind(diagnostic_sink)
        if configured_store is not None:
            owner.on_parameters_saved = configured_store.replace_profile
            owner.on_parameters_restore = configured_store.restore_overrides
            repo = configured_store.config_repository() if hasattr(configured_store, 'config_repository') else None
            if repo is not None:
                owner.on_parameters_marker_write = (
                    lambda *, old_hash, new_hash, apply_id=None, _repo=repo: _repo.write_pending_apply_marker(
                        account_key=binding.account_key,
                        boot_id=binding.boot_id,
                        old_hash=old_hash,
                        new_hash=new_hash,
                        apply_id=apply_id,
                    )
                )
                owner.on_parameters_marker_clear = repo.clear_pending_apply_marker
                try:
                    pending_marker = repo.read_pending_apply_marker()
                except TraderError as error:
                    if diagnostic_sink is not None:
                        diagnostic_sink.record(
                            'CONFIG_APPLY_MARKER_CORRUPTED', component='CONFIG', severity='ERROR',
                            priority='HIGH', error_code=error.code,
                            payload={'authority_effect': 'DISARMED'},
                        )
                    raise
                if pending_marker is not None and diagnostic_sink is not None:
                    matched_state = (
                        'NEW_HASH_PRESENT' if pending_marker.get('new_hash') == profile.content_hash
                        else 'OLD_HASH_PRESENT' if pending_marker.get('old_hash') == profile.content_hash
                        else 'HASH_CONFLICT'
                    )
                    diagnostic_sink.record(
                        'CONFIG_APPLY_MARKER_PENDING', component='CONFIG', severity='WARNING',
                        priority='HIGH',
                        payload={
                            'authority_effect': 'DISARMED',
                            'marker_state': matched_state,
                            'apply_id': pending_marker.get('apply_id'),
                        },
                    )
        resolved_pid = _resolve_trusted_pid(trusted_control_pid)
        resolved_entry_point = entry_point or (
            'online-trader-desktop'
            if resolved_pid is not None and resolved_pid > 0
            else 'runtime run'
        )
        identity = runtime_identity(
            resolved_entry_point,
            'DSV_TARGET_EXECUTION',
            getattr(clock, 'epoch', '0'),
            getattr(connection, 'source_epoch', '0'),
            binding.boot_id,
            calendar=calendar,
            profile=profile,
            grant=grant,
        )
        owner.session.runtime_identity = identity
        try:
            publish_new_bytes(canonical_bytes(identity), output / 'runtime-identity.json')
        except (OSError, TraderError):
            pass
        run_identity = {
            'schema_version': 'DSV_RUN_IDENTITY/1',
            'account_key': binding.account_key,
            'kernel_boot_id': binding.boot_id,
            'scope_hash': grant.content_hash,
            'run_directory': str(output),
        }
        try:
            publish_new_bytes(canonical_bytes(run_identity), output / 'run-identity.json')
        except (OSError, TraderError):
            owner.run_directory = None
        else:
            owner.run_directory = str(output)
        def history_error(error):
            publish_replace_bytes(canonical_bytes({
                'runtime_boot_id': binding.boot_id, 'account_key': binding.account_key,
                'execution_core': 'DSV_TARGET_EXECUTION', 'authority_effect': 'NONE',
                'diagnostic': getattr(error, 'code', 'E_HISTORY_CACHE_WRITE'),
                'observed_at_utc': _utc_now(),
            }), diagnostics)
            if diagnostic_sink is not None:
                code = getattr(error, 'code', 'E_HISTORY_CACHE_WRITE')
                diagnostic_sink.record(
                    'HISTORY_CACHE_WRITE_FAILED', component='HISTORY', severity='WARNING',
                    priority='HIGH',
                    error_code=code if isinstance(code, str) and code.startswith('E_') else 'E_HISTORY_CACHE_WRITE',
                    payload={'authority_effect': 'NONE', 'exception_type': type(error).__name__},
                )

        try:
            history_writer = BrokerHistoryWriter(
                history_cache, binding.account_key, binding.provider,
                on_error=history_error)
        except (sqlite3.Error, OSError, TraderError) as error:
            history_writer = None
            history_error(error)
            if diagnostic_sink is not None:
                code = getattr(error, 'code', 'E_HISTORY_CACHE_INIT')
                diagnostic_sink.record(
                    'HISTORY_CACHE_UNAVAILABLE', component='HISTORY', severity='WARNING',
                    priority='HIGH',
                    error_code=code if isinstance(code, str) and code.startswith('E_') else 'E_HISTORY_CACHE_INIT',
                    payload={'authority_effect': 'NONE', 'exception_type': type(error).__name__},
                )
        publish_replace_bytes(canonical_bytes({
            'phase': 'WAITING_FOR_NATIVE_SYNC', 'runtime_boot_id': binding.boot_id,
            'account_key': binding.account_key, 'entry_authority': 'DISARMED',
            'execution_core': 'DSV_TARGET_EXECUTION',
            'observed_at_utc': _utc_now(),
        }), status)
        server = ControlServer(
            owner, pipe, trusted_client_pid=trusted_control_pid,
            diagnostic_sink=diagnostic_sink,
        )
        loop_state = {
            'last_status': {'phase': 'WAITING_FOR_NATIVE_SYNC', 'entry_authority': 'DISARMED'},
            'last_history_capture': 0.0,
            'initial_start_pending': bool(arm),
            'initial_arm_generation': getattr(owner, 'authority_generation', 0),
            'diagnostic_reported_close_keys': set(),
            'diagnostic_close_check_monotonic': 0.0,
            'diagnostic_close_failure_recorded': False,
            'diagnostic_observed_days': {grant.value['trading_day']},
        }

        contracts = tuple(grant.value.get('contracts', ()))
        close_deadlines = _build_close_deadlines(calendar, contracts)

        def current_trading_day(current_day=None, now=None):
            base_day = current_day or owner.snapshot().get('trading_day') or grant.value['trading_day']
            return resolve_active_trading_day(
                calendar, contracts, base_day, now, clock=clock)

        def is_complete():
            return bool(connection.broker_connected and connection.complete())

        market_diagnostics = TargetMarketDiagnostics(
            output, boot_id=binding.boot_id, sdk_version=SDK_VERSION,
            evidence_sink=diagnostic_sink,
        )

        def tick():
            execute_owner_tick(
                owner, binding=binding, current_trading_day=current_trading_day,
                is_complete=is_complete, history_writer=history_writer,
                status_path=status, diagnostics_path=diagnostics,
                loop_state=loop_state, market_diagnostics=market_diagnostics,
                diagnostic_sink=diagnostic_sink,
            )
            if diagnostic_report_request is None:
                return
            try:
                active_day = owner.snapshot().get('trading_day')
                if active_day:
                    loop_state['diagnostic_observed_days'].add(active_day)
                _maybe_request_market_close(
                    close_deadlines=close_deadlines,
                    clock=clock,
                    loop_state=loop_state,
                    boot_id=binding.boot_id,
                    calendar_hash=calendar.content_hash,
                    diagnostic_sink=diagnostic_sink,
                    diagnostic_report_request=diagnostic_report_request,
                    allowed_days=loop_state['diagnostic_observed_days'],
                )
            except Exception as error:
                if diagnostic_sink is not None and not loop_state['diagnostic_close_failure_recorded']:
                    loop_state['diagnostic_close_failure_recorded'] = True
                    diagnostic_sink.record(
                        'REPORT_CUT_FAILED', component='LIFECYCLE', severity='WARNING',
                        priority='HIGH', error_code='E_DIAGNOSTIC_REPORTER',
                        payload={'exception_type': type(error).__name__},
                    )

        with attach_order_blocking_log(output), market_diagnostics:
            server.serve(stop_event=owner.stop_event, owner_tick=tick, publish_workbench=True)
        shutdown = owner.session.prepare_shutdown()
        maintenance_completion = getattr(owner, 'maintenance_completion', None)
        if maintenance_completion is not None:
            try:
                publish_replace_bytes(
                    canonical_bytes(maintenance_completion),
                    output / 'maintenance-completion.json',
                )
            except (OSError, TraderError):
                pass
        if diagnostic_sink is not None:
            final_state = owner.snapshot()
            journal_sequence = final_state.get('sequence')
            diagnostic_sink.record(
                'EXECUTION_JOURNAL_CUTOFF', component='STORAGE', priority='CRITICAL',
                correlation={
                    'execution_journal_event_seq': (
                        str(journal_sequence) if journal_sequence is not None else 'UNKNOWN'
                    ),
                },
                payload={
                    'clean_shutdown': True,
                    'shutdown_status': shutdown.get('status'),
                    'entry_authority': 'DISARMED',
                    'maintenance_state': (
                        None if maintenance_completion is None else maintenance_completion.get('state')
                    ),
                },
            )
        stopped_payload = {
            'phase': 'STOPPED', 'runtime_boot_id': binding.boot_id,
            'account_key': binding.account_key, 'entry_authority': 'DISARMED',
            'execution_core': 'DSV_TARGET_EXECUTION', 'shutdown': shutdown,
            'observed_at_utc': _utc_now(),
        }
        if maintenance_completion is not None:
            stopped_payload['maintenance_completion'] = maintenance_completion
        publish_replace_bytes(canonical_bytes(stopped_payload), status)
        if diagnostic_sink is not None:
            diagnostic_sink.record(
                'OWNER_STOPPED', component='LIFECYCLE', priority='CRITICAL',
                payload={'entry_authority': 'DISARMED', 'shutdown_status': shutdown.get('status')},
            )
        return 0
    except Exception as error:
        publish_replace_bytes(canonical_bytes({
            'phase': 'NEEDS_REVIEW', 'account_key': grant.account_key,
            'runtime_boot_id': binding.boot_id,
            'entry_authority': 'DISARMED', 'execution_core': 'DSV_TARGET_EXECUTION',
            'error_code': getattr(error, 'code', 'E_DSV_SDK_RUNTIME'),
            'observed_at_utc': _utc_now(),
            'exception': exception_record(error),
        }), status)
        if diagnostic_sink is not None:
            code = getattr(error, 'code', 'E_DSV_SDK_RUNTIME')
            diagnostic_sink.record(
                'OWNER_FAILED', component='LIFECYCLE', severity='ERROR', priority='CRITICAL',
                error_code=code if isinstance(code, str) and code.startswith('E_') else 'E_DSV_SDK_RUNTIME',
                payload={'entry_authority': 'DISARMED', 'exception_type': type(error).__name__},
            )
        raise
    finally:
        if history_writer is not None:
            closed_cleanly = history_writer.close()
            if closed_cleanly is False and diagnostic_sink is not None:
                diagnostic_sink.record(
                    'HISTORY_CACHE_CLOSE_PARTIAL', component='HISTORY', severity='WARNING',
                    priority='HIGH',
                    payload={'authority_effect': 'NONE', 'coverage_status': 'PARTIAL'},
                )
        if owner is not None:
            owner.close()
        elif connection is not None:
            connection.close()
