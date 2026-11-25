"""Shared owner tick execution and error classification for Standalone Runtime."""

from __future__ import annotations

from datetime import datetime, timezone
import time

from online_trader.configuration.files import publish_runtime_file_bytes
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError, exception_record


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_diag_event(diagnostic_sink, *args, **kwargs):
    if diagnostic_sink is None:
        return
    try:
        diagnostic_sink.record(*args, **kwargs)
    except Exception:
        pass


def _try_write_runtime_file(
    payload_bytes,
    path,
    *,
    error_code,
    event_type,
    loop_state,
    retry_key,
    log_key,
    diagnostic_sink,
    now_mono,
):
    """Best-effort single-attempt write without blocking or aborting the Owner loop."""
    try:
        publish_runtime_file_bytes(
            payload_bytes,
            path,
            error_code=error_code,
            error_message=f"Cannot replace {getattr(path, 'name', 'runtime file')}",
        )
        loop_state[retry_key] = 0.0
        return True
    except (OSError, TraderError) as error:
        loop_state[retry_key] = now_mono + 1.0
        if now_mono >= loop_state.get(log_key, 0.0) + 30.0:
            loop_state[log_key] = now_mono
            _record_diag_event(
                diagnostic_sink,
                event_type,
                component="OWNER",
                severity="WARNING",
                priority="HIGH",
                error_code=error_code,
                payload={
                    "authority_effect": "NONE",
                    "exception_type": type(error).__name__,
                },
            )
        return False


def _flush_pending_runtime_files(status_path, diagnostics_path, loop_state, diagnostic_sink, now_mono):
    pending_diag = loop_state.get("pending_diagnostics_bytes")
    if pending_diag is not None and now_mono >= loop_state.get("diagnostics_retry_at", 0.0):
        if _try_write_runtime_file(
            pending_diag,
            diagnostics_path,
            error_code="E_RUNTIME_DIAGNOSTICS_WRITE",
            event_type="RUNTIME_DIAGNOSTICS_WRITE_FAILED",
            loop_state=loop_state,
            retry_key="diagnostics_retry_at",
            log_key="last_diag_write_error_at",
            diagnostic_sink=diagnostic_sink,
            now_mono=now_mono,
        ):
            loop_state.pop("pending_diagnostics_bytes", None)
    pending_status = loop_state.get("pending_status_bytes")
    if pending_status is not None and now_mono >= loop_state.get("status_retry_at", 0.0):
        if _try_write_runtime_file(
            pending_status,
            status_path,
            error_code="E_RUNTIME_STATUS_WRITE",
            event_type="RUNTIME_STATUS_WRITE_FAILED",
            loop_state=loop_state,
            retry_key="status_retry_at",
            log_key="last_status_write_error_at",
            diagnostic_sink=diagnostic_sink,
            now_mono=now_mono,
        ):
            loop_state["last_status"] = loop_state.pop("pending_status", loop_state.get("last_status"))
            loop_state.pop("pending_status_bytes", None)


def _queue_status_update(current, *, binding, status_path, loop_state, diagnostic_sink, now_mono):
    if current == loop_state.get("last_status") and loop_state.get("pending_status_bytes") is None:
        return
    if current == loop_state.get("pending_status") and loop_state.get("pending_status_bytes") is not None:
        return
    payload = canonical_bytes({
        **current,
        "runtime_boot_id": binding.boot_id,
        "account_key": binding.account_key,
        "execution_core": "DSV_TARGET_EXECUTION",
        "observed_at_utc": _utc_now(),
    })
    loop_state["pending_status"] = current
    loop_state["pending_status_bytes"] = payload
    if now_mono < loop_state.get("status_retry_at", 0.0):
        return
    if _try_write_runtime_file(
        payload,
        status_path,
        error_code="E_RUNTIME_STATUS_WRITE",
        event_type="RUNTIME_STATUS_WRITE_FAILED",
        loop_state=loop_state,
        retry_key="status_retry_at",
        log_key="last_status_write_error_at",
        diagnostic_sink=diagnostic_sink,
        now_mono=now_mono,
    ):
        loop_state["last_status"] = loop_state.pop("pending_status")
        loop_state.pop("pending_status_bytes", None)


_TRANSIENT_TICK_CODES = frozenset({
    "E_DSV_SDK_SYNC", "E_DSV_SYNC", "E_GATEWAY_CONNECT", "E_GATEWAY_SYNC",
    "E_TIMEOUT", "E_PIPE_TIMEOUT",
})
_PERSISTENT_DISARM_CODES = frozenset({
    "E_DSV_NATIVE_FACT", "E_DSV_NATIVE_ID", "E_DSV_SDK_FACT", "E_DSV_OWNERSHIP",
    "E_DSV_SCOPE", "E_DSV_SDK_SCOPE", "E_DSV_ROUND", "E_DSV_ROUND_SEQUENCE",
    "E_DSV_TIME_VERSION", "E_DSV_SDK_CLOCK", "E_DSV_NIGHT_BOUNDARY", "E_CLOCK_INPUT",
})


def classify_owner_tick_error(error, *, storage_failed=False):
    """Classify an owner tick error into TRANSIENT_DISCONNECT, PERSISTENT_DISARM, or FAIL_STOP."""
    if storage_failed:
        return "FAIL_STOP"
    if isinstance(error, OSError):
        return "TRANSIENT_DISCONNECT"
    if isinstance(error, TraderError):
        if error.code in {"E_DSV_STORE", "E_DSV_OWNER", "E_DSV_SDK_OWNER", "E_STORAGE"}:
            return "FAIL_STOP"
        if error.code in _TRANSIENT_TICK_CODES:
            return "TRANSIENT_DISCONNECT"
        if error.code in _PERSISTENT_DISARM_CODES:
            return "PERSISTENT_DISARM"
    return "FAIL_STOP"


def execute_owner_tick(
    owner,
    *,
    binding,
    current_trading_day,
    is_complete,
    history_writer,
    status_path,
    diagnostics_path,
    loop_state,
    market_diagnostics=None,
    diagnostic_sink=None,
    sleep_fn=time.sleep,
):
    """Execute one Target Owner step, preserving pending auto-arm across transient errors."""
    loop_started_ns = time.monotonic_ns()
    now_mono = time.monotonic()
    _flush_pending_runtime_files(
        status_path, diagnostics_path, loop_state, diagnostic_sink, now_mono,
    )
    if getattr(owner, 'maintenance', None) is not None and loop_state.get('initial_start_pending'):
        loop_state['initial_start_pending'] = False
    setter = getattr(owner, 'set_initial_start_pending', None)
    if callable(setter):
        setter(bool(loop_state.get('initial_start_pending')))
    pumper = getattr(owner, "pump_fault_hold", None)
    cur_state = owner.snapshot()
    if (
        callable(pumper)
        and cur_state.get("owner_fault_code")
        and not cur_state.get("synchronized")
        and loop_state.get("consecutive_persistent_faults", 0) >= 1
        and now_mono < loop_state.get("fault_retry_at", 0.0)
    ):
        try:
            pumper(trading_day=current_trading_day, complete=is_complete)
        except Exception:
            sleep_fn(0.05)
        return
    try:
        receipt = owner.step(trading_day=current_trading_day, complete=is_complete)
        loop_state["consecutive_persistent_faults"] = 0
        loop_state["fault_retry_at"] = 0.0
        state = owner.snapshot()
        if loop_state.get("initial_start_pending"):
            expected_gen = loop_state.get("initial_arm_generation", 0)
            if getattr(owner, "authority_generation", 0) != expected_gen or getattr(owner, "maintenance", None) is not None:
                loop_state["initial_start_pending"] = False
            elif state["synchronized"] and not state.get("owner_fault_code"):
                owner.start_confirmed()
                loop_state["initial_start_pending"] = False
                state = owner.snapshot()
        if callable(setter):
            setter(bool(loop_state.get("initial_start_pending")))
        now = time.monotonic()
        if history_writer is not None and now - loop_state.get("last_history_capture", 0.0) >= 10:
            snapshot = owner.native_account_snapshot()
            if snapshot is not None:
                history_writer.offer(snapshot)
            loop_state["last_history_capture"] = now
    except Exception as error:
        category = classify_owner_tick_error(
            error, storage_failed=bool(getattr(owner.session, "_failed", False))
        )
        if category == "FAIL_STOP":
            raise
        disarm = category == "PERSISTENT_DISARM"
        if disarm:
            loop_state["initial_start_pending"] = False
        code = getattr(error, "code", "E_DSV_SDK_RUNTIME")
        detail = str(error)[:240]
        prior_state = owner.snapshot()
        already_disarmed = bool(
            disarm
            and not prior_state.get("synchronized")
            and not prior_state.get("running")
            and prior_state.get("owner_fault_code") == code
        )
        if not already_disarmed:
            owner.disconnect_for_error(
                "PERSISTENT_OWNER_FACT_CONFLICT" if disarm else "TRANSIENT_OWNER_TICK_ERROR",
                disarm=disarm,
                fault_code=code if disarm else None,
            )
        if disarm:
            setter = getattr(owner, "set_owner_fault_detail", None)
            if callable(setter):
                setter(detail)
            loop_state["consecutive_persistent_faults"] = loop_state.get("consecutive_persistent_faults", 0) + 1
            loop_state["fault_retry_at"] = time.monotonic() + 2.0
        else:
            loop_state["consecutive_persistent_faults"] = 0
        state = owner.snapshot()
        if market_diagnostics is not None:
            now_ns = time.monotonic_ns()
            market_diagnostics.observe_tick(
                owner=owner,
                owner_loop_ns=now_ns - loop_started_ns,
                synchronized=bool(state.get("synchronized")),
                disconnected=True,
                error_code=code,
                now_ns=now_ns,
            )
        authority_effect = (
            "DISARMED_RECOVERY_REQUIRED"
            if disarm
            else "SYNCHRONIZATION_LOST_AUTHORITY_PRESERVED"
            if state["running"]
            else "SYNCHRONIZATION_LOST_DISARMED"
        )
        fail_mono = time.monotonic()
        should_emit_diag = (
            not already_disarmed
            or fail_mono >= loop_state.get("last_fault_diag_at", 0.0) + 30.0
        )
        if should_emit_diag:
            loop_state["last_fault_diag_at"] = fail_mono
            _record_diag_event(
                diagnostic_sink,
                'OWNER_TICK_FAILED', component='OWNER', severity='ERROR', priority='CRITICAL',
                error_code=code if isinstance(code, str) and code.startswith('E_') else 'E_DSV_SDK_RUNTIME',
                reason_code=category,
                payload={
                    'authority_effect': authority_effect,
                    'entry_authority': 'RUNNING' if state['running'] else 'DISARMED',
                    'exception_type': type(error).__name__,
                    'detail': detail,
                },
            )
            diag_bytes = canonical_bytes({
                "runtime_boot_id": binding.boot_id,
                "account_key": binding.account_key,
                "execution_core": "DSV_TARGET_EXECUTION",
                "authority_effect": authority_effect,
                "error_category": category,
                "entry_authority": "RUNNING" if state["running"] else "DISARMED",
                "diagnostic": code,
                "detail": detail,
                "observed_at_utc": _utc_now(),
                "exception": exception_record(error),
            })
            if _try_write_runtime_file(
                diag_bytes,
                diagnostics_path,
                error_code="E_RUNTIME_DIAGNOSTICS_WRITE",
                event_type="RUNTIME_DIAGNOSTICS_WRITE_FAILED",
                loop_state=loop_state,
                retry_key="diagnostics_retry_at",
                log_key="last_diag_write_error_at",
                diagnostic_sink=diagnostic_sink,
                now_mono=fail_mono,
            ):
                loop_state.pop("pending_diagnostics_bytes", None)
            else:
                loop_state["pending_diagnostics_bytes"] = diag_bytes
        current = {
            "phase": "NEEDS_REVIEW" if disarm else "WAITING_FOR_NATIVE_SYNC",
            "entry_authority": "RUNNING" if state["running"] else "DISARMED",
        }
        if current != loop_state.get("last_emitted_status"):
            loop_state["last_emitted_status"] = current
        _queue_status_update(
            current,
            binding=binding,
            status_path=status_path,
            loop_state=loop_state,
            diagnostic_sink=diagnostic_sink,
            now_mono=fail_mono,
        )
        sleep_fn(0.1 if not disarm else 0.05)
        return
    if market_diagnostics is not None:
        now_ns = time.monotonic_ns()
        disconnected = bool(
            receipt is not None and receipt.get("type") == "DISCONNECTED"
        )
        market_diagnostics.observe_tick(
            owner=owner,
            owner_loop_ns=now_ns - loop_started_ns,
            synchronized=bool(state.get("synchronized")),
            disconnected=disconnected,
            now_ns=now_ns,
        )
    if receipt is not None:
        current = {
            "phase": (
                "NEEDS_REVIEW"
                if state.get("owner_fault_code")
                else "READY"
                if state["synchronized"]
                else "WAITING_FOR_NATIVE_SYNC"
            ),
            "entry_authority": "RUNNING" if state["running"] else "DISARMED",
        }
        if current != loop_state.get("last_emitted_status"):
            loop_state["last_emitted_status"] = current
            _record_diag_event(
                diagnostic_sink,
                'OWNER_STATE_CHANGED', component='OWNER', priority='HIGH',
                reason_code=current['phase'],
                payload={'entry_authority': current['entry_authority']},
            )
        _queue_status_update(
            current,
            binding=binding,
            status_path=status_path,
            loop_state=loop_state,
            diagnostic_sink=diagnostic_sink,
            now_mono=time.monotonic(),
        )
