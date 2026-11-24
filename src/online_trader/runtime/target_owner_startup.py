"""Standalone lifecycle host for the DSV target-execution candidate.

The desktop cutover calls this owner only after compiling the same account,
profile and calendar scope used by the current Runtime.  Construction is
DISARMED and the first complete native frame remains mandatory.
"""

from datetime import datetime, timezone
from pathlib import Path
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


def run_target_owner(*, grant, profile, calendar, credentials, output, pipe,
                     session_storage, runtime_lock, account_lock, clock,
                     trusted_control_pid, history_cache, arm=False,
                     configuration_revision='1', entry_point=None, configured_store=None):
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
        connection = strategy_port(grant).connect(credentials, grant, clock)
        connection.configure_calendar(calendar)
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
        if configured_store is not None:
            owner.on_parameters_saved = configured_store.replace_profile
            owner.on_parameters_restore = configured_store.restore_overrides
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
        history_writer = BrokerHistoryWriter(
            history_cache, binding.account_key, binding.provider,
            on_error=lambda error: publish_replace_bytes(canonical_bytes({
                'runtime_boot_id': binding.boot_id, 'account_key': binding.account_key,
                'execution_core': 'DSV_TARGET_EXECUTION', 'authority_effect': 'NONE',
                'diagnostic': getattr(error, 'code', 'E_HISTORY_CACHE_WRITE'),
                'observed_at_utc': _utc_now(),
            }), diagnostics))
        publish_replace_bytes(canonical_bytes({
            'phase': 'WAITING_FOR_NATIVE_SYNC', 'runtime_boot_id': binding.boot_id,
            'account_key': binding.account_key, 'entry_authority': 'DISARMED',
            'execution_core': 'DSV_TARGET_EXECUTION',
            'observed_at_utc': _utc_now(),
        }), status)
        server = ControlServer(owner, pipe, trusted_client_pid=trusted_control_pid)
        loop_state = {
            'last_status': {'phase': 'WAITING_FOR_NATIVE_SYNC', 'entry_authority': 'DISARMED'},
            'last_history_capture': 0.0,
            'initial_start_pending': bool(arm),
            'initial_arm_generation': getattr(owner, 'authority_generation', 0),
        }

        contracts = tuple(grant.value.get('contracts', ()))

        def current_trading_day(current_day=None, now=None):
            base_day = current_day or owner.snapshot().get('trading_day') or grant.value['trading_day']
            return resolve_active_trading_day(
                calendar, contracts, base_day, now, clock=clock)

        def is_complete():
            return bool(connection.broker_connected and connection.complete())

        market_diagnostics = TargetMarketDiagnostics(
            output, boot_id=binding.boot_id, sdk_version=SDK_VERSION,
        )

        def tick():
            execute_owner_tick(
                owner, binding=binding, current_trading_day=current_trading_day,
                is_complete=is_complete, history_writer=history_writer,
                status_path=status, diagnostics_path=diagnostics,
                loop_state=loop_state, market_diagnostics=market_diagnostics,
            )

        with attach_order_blocking_log(output), market_diagnostics:
            server.serve(stop_event=owner.stop_event, owner_tick=tick, publish_workbench=True)
        shutdown = owner.session.prepare_shutdown()
        publish_replace_bytes(canonical_bytes({
            'phase': 'STOPPED', 'runtime_boot_id': binding.boot_id,
            'account_key': binding.account_key, 'entry_authority': 'DISARMED',
            'execution_core': 'DSV_TARGET_EXECUTION', 'shutdown': shutdown,
            'observed_at_utc': _utc_now(),
        }), status)
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
        raise
    finally:
        if history_writer is not None:
            history_writer.close()
        if owner is not None:
            owner.close()
        elif connection is not None:
            connection.close()
