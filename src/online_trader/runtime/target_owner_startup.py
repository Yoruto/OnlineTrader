"""Standalone lifecycle host for the DSV target-execution candidate.

The desktop cutover calls this owner only after compiling the same account,
profile and calendar scope used by the current Runtime.  Construction is
DISARMED and the first complete native frame remains mandatory.
"""

from pathlib import Path
import time

from online_trader.contracts.codec import canonical_bytes
from online_trader.configuration.files import publish_replace_bytes
from online_trader.dsv_runtime.standalone_owner import create_standalone_owner
from online_trader.errors import TraderError, exception_record
from online_trader.gateway.simnow import strategy_port
from online_trader.kernel.server import ControlServer
from online_trader.runtime.account_cache import BrokerHistoryWriter


def run_target_owner(*, grant, profile, calendar, credentials, output, pipe,
                     session_storage, runtime_lock, account_lock, clock,
                     trusted_control_pid, history_cache, arm=False,
                     configuration_revision='1'):
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
        history_writer = BrokerHistoryWriter(
            history_cache, binding.account_key, binding.provider,
            on_error=lambda error: publish_replace_bytes(canonical_bytes({
                'runtime_boot_id': binding.boot_id, 'account_key': binding.account_key,
                'execution_core': 'DSV_TARGET_EXECUTION', 'authority_effect': 'NONE',
                'diagnostic': getattr(error, 'code', 'E_HISTORY_CACHE_WRITE'),
            }), diagnostics))
        publish_replace_bytes(canonical_bytes({
            'phase': 'WAITING_FOR_NATIVE_SYNC', 'runtime_boot_id': binding.boot_id,
            'account_key': binding.account_key, 'entry_authority': 'DISARMED',
            'execution_core': 'DSV_TARGET_EXECUTION',
        }), status)
        server = ControlServer(owner, pipe, trusted_client_pid=trusted_control_pid)
        last_status = {'phase': 'WAITING_FOR_NATIVE_SYNC', 'entry_authority': 'DISARMED'}
        last_history_capture = 0.0
        initial_start_pending = bool(arm)

        def tick():
            nonlocal last_status, last_history_capture, initial_start_pending
            receipt = owner.step(trading_day=grant.value['trading_day'], complete=connection.complete)
            state = owner.snapshot()
            if initial_start_pending and state['synchronized']:
                owner.start_confirmed()
                initial_start_pending = False
                state = owner.snapshot()
            now = time.monotonic()
            if now - last_history_capture >= 10:
                snapshot = owner.native_account_snapshot()
                if snapshot is not None:
                    history_writer.offer(snapshot)
                last_history_capture = now
            if receipt is not None:
                current = {
                    'phase': 'READY' if state['synchronized'] else 'WAITING_FOR_NATIVE_SYNC',
                    'entry_authority': 'RUNNING' if state['running'] else 'DISARMED',
                }
                if current == last_status:
                    return
                publish_replace_bytes(canonical_bytes({
                    **current,
                    'runtime_boot_id': binding.boot_id, 'account_key': binding.account_key,
                    'execution_core': 'DSV_TARGET_EXECUTION',
                }), status)
                last_status = current

        server.serve(stop_event=owner.stop_event, owner_tick=tick, publish_workbench=True)
        shutdown = owner.session.prepare_shutdown()
        publish_replace_bytes(canonical_bytes({
            'phase': 'STOPPED', 'runtime_boot_id': binding.boot_id,
            'account_key': binding.account_key, 'entry_authority': 'DISARMED',
            'execution_core': 'DSV_TARGET_EXECUTION', 'shutdown': shutdown,
        }), status)
        return 0
    except Exception as error:
        publish_replace_bytes(canonical_bytes({
            'phase': 'NEEDS_REVIEW', 'account_key': grant.account_key,
            'entry_authority': 'DISARMED', 'execution_core': 'DSV_TARGET_EXECUTION',
            'error_code': getattr(error, 'code', 'E_DSV_SDK_RUNTIME'),
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
