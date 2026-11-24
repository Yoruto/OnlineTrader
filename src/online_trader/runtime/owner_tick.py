"""Shared owner tick execution and error classification for Standalone Runtime."""

from __future__ import annotations

from datetime import datetime, timezone
import time

from online_trader.configuration.files import publish_replace_bytes
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError, exception_record


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    sleep_fn=time.sleep,
):
    """Execute one Target Owner step, preserving pending auto-arm across transient errors."""
    loop_started_ns = time.monotonic_ns()
    try:
        receipt = owner.step(trading_day=current_trading_day, complete=is_complete)
        state = owner.snapshot()
        if loop_state.get("initial_start_pending"):
            expected_gen = loop_state.get("initial_arm_generation", 0)
            if getattr(owner, "authority_generation", 0) != expected_gen:
                loop_state["initial_start_pending"] = False
            elif state["synchronized"] and not state.get("owner_fault_code"):
                owner.start_confirmed()
                loop_state["initial_start_pending"] = False
                state = owner.snapshot()
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
        owner.disconnect_for_error(
            "PERSISTENT_OWNER_FACT_CONFLICT" if disarm else "TRANSIENT_OWNER_TICK_ERROR",
            disarm=disarm,
            fault_code=code if disarm else None,
        )
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
        publish_replace_bytes(
            canonical_bytes({
                "runtime_boot_id": binding.boot_id,
                "account_key": binding.account_key,
                "execution_core": "DSV_TARGET_EXECUTION",
                "authority_effect": authority_effect,
                "error_category": category,
                "entry_authority": "RUNNING" if state["running"] else "DISARMED",
                "diagnostic": code,
                "observed_at_utc": _utc_now(),
                "exception": exception_record(error),
            }),
            diagnostics_path,
        )
        current = {
            "phase": "NEEDS_REVIEW" if disarm else "WAITING_FOR_NATIVE_SYNC",
            "entry_authority": "RUNNING" if state["running"] else "DISARMED",
        }
        if current != loop_state.get("last_status"):
            publish_replace_bytes(
                canonical_bytes({
                    **current,
                    "runtime_boot_id": binding.boot_id,
                    "account_key": binding.account_key,
                    "execution_core": "DSV_TARGET_EXECUTION",
                    "observed_at_utc": _utc_now(),
                }),
                status_path,
            )
            loop_state["last_status"] = current
        sleep_fn(0.1)
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
        if current == loop_state.get("last_status"):
            return
        publish_replace_bytes(
            canonical_bytes({
                **current,
                "runtime_boot_id": binding.boot_id,
                "account_key": binding.account_key,
                "execution_core": "DSV_TARGET_EXECUTION",
                "observed_at_utc": _utc_now(),
            }),
            status_path,
        )
        loop_state["last_status"] = current
