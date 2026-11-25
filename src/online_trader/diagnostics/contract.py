"""Versioned, bounded diagnostic event contract."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import time

from online_trader.contracts.codec import canonical_bytes
from online_trader.diagnostics.catalog import is_valid_error_code, user_code_for_error
from online_trader.diagnostics.redaction import sanitize_diagnostic_mapping


EVENT_SCHEMA = "ONLINE_TRADER_DIAGNOSTIC_EVENT/1"
DIAGNOSTIC_PROTOCOL = "1"
SEVERITIES = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z", re.ASCII)
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z", re.ASCII)
_MAX_EVENT_BYTES = 64 * 1024
_CORRELATION_ALLOWLIST = frozenset({
    "market_fact_id", "strategy_decision_id", "decision_id", "execution_intent_id", "intent_id",
    "position_cycle_id", "position_id", "child_order_id", "attempt_id", "effect_id",
    "broker_order_id", "native_order_id", "trade_id", "native_trade_id",
    "position_revision", "execution_journal_event_seq", "broker_snapshot_id",
    "history_snapshot_seq", "command_id", "snapshot_id", "runtime_snapshot_id",
    "workbench_apply_id", "trace_id", "contract_id", "candle_id", "trading_day",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity(value, name, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"invalid diagnostic {name}")
    return value


def _mapping(value, name, *, max_items=32, allowlist=None):
    if value is None:
        return {}
    if type(value) is not dict or len(value) > max_items:
        raise ValueError(f"invalid diagnostic {name}")
    return sanitize_diagnostic_mapping(value, allowed_keys=allowlist)


def validate_event_header(
    *,
    process_role: str,
    component: str,
    event_type: str,
    severity: str = "INFO",
    reason_code: str | None = None,
    error_code: str | None = None,
    correlation: dict | None = None,
    payload: dict | None = None,
) -> tuple[str, str, str, str]:
    """Perform cheap caller-thread validation without JSON serialization."""
    role = process_role.upper()
    component_name = component.upper()
    event_name = event_type.upper()
    if any(_NAME.fullmatch(value) is None for value in (role, component_name, event_name)):
        raise ValueError("invalid diagnostic event name")
    severity_name = severity.upper()
    if severity_name not in SEVERITIES:
        raise ValueError("invalid diagnostic severity")
    if reason_code is not None and (not isinstance(reason_code, str) or _NAME.fullmatch(reason_code) is None):
        raise ValueError("invalid diagnostic reason code")
    if error_code is not None and not is_valid_error_code(error_code):
        raise ValueError("invalid diagnostic error code")
    for mapping, label in ((correlation, "correlation"), (payload, "payload")):
        if mapping is not None and (type(mapping) is not dict or len(mapping) > 32):
            raise ValueError(f"invalid diagnostic {label}")
    return role, component_name, event_name, severity_name


def encode_diagnostic_event(**kwargs) -> bytes:
    """Build and canonically encode one diagnostic NDJSON line in one pass."""
    raw = canonical_bytes(diagnostic_event(**kwargs))
    if len(raw) > _MAX_EVENT_BYTES:
        raise ValueError("diagnostic event is too large")
    return raw + b"\n"


def diagnostic_event(
    *,
    stream_id: str,
    event_seq: int,
    process_role: str,
    process_instance_id: str,
    component: str,
    event_type: str,
    severity: str = "INFO",
    runtime_boot_id: str | None = None,
    account_pseudonym: str | None = None,
    configuration_revision: str | None = None,
    reason_code: str | None = None,
    error_code: str | None = None,
    observed_at_utc: str | None = None,
    monotonic_ns: str | None = None,
    correlation: dict | None = None,
    payload: dict | None = None,
) -> dict:
    """Build one small immutable evidence envelope."""
    if type(event_seq) is not int or event_seq <= 0:
        raise ValueError("invalid diagnostic event sequence")
    role = process_role.upper()
    component_name = component.upper()
    event_name = event_type.upper()
    if any(_NAME.fullmatch(value) is None for value in (role, component_name, event_name)):
        raise ValueError("invalid diagnostic event name")
    severity = severity.upper()
    if severity not in SEVERITIES:
        raise ValueError("invalid diagnostic severity")
    if reason_code is not None and (not isinstance(reason_code, str) or _NAME.fullmatch(reason_code) is None):
        raise ValueError("invalid diagnostic reason code")
    if error_code is not None and not is_valid_error_code(error_code):
        raise ValueError("invalid diagnostic error code")
    safe_correlation = _mapping(correlation, "correlation", allowlist=_CORRELATION_ALLOWLIST)
    safe_payload = _mapping(payload, "payload")
    user_code = user_code_for_error(error_code)
    if user_code is not None and "user_diagnostic_code" not in safe_payload:
        safe_payload["user_diagnostic_code"] = user_code
    record = {
        "schema_version": EVENT_SCHEMA,
        "stream_id": _identity(stream_id, "stream id"),
        "event_seq": str(event_seq),
        "observed_at_utc": observed_at_utc if isinstance(observed_at_utc, str) else utc_now(),
        "monotonic_ns": monotonic_ns if isinstance(monotonic_ns, str) else str(time.monotonic_ns()),
        "process_role": role,
        "process_instance_id": _identity(process_instance_id, "process instance"),
        "runtime_boot_id": _identity(runtime_boot_id, "runtime boot", optional=True),
        "account_pseudonym": _identity(account_pseudonym, "account pseudonym", optional=True),
        "configuration_revision": _identity(
            configuration_revision, "configuration revision", optional=True
        ),
        "component": component_name,
        "event_type": event_name,
        "reason_code": reason_code,
        "error_code": error_code,
        "severity": severity,
        "correlation": safe_correlation,
        "payload": safe_payload,
        "diagnostic_protocol": DIAGNOSTIC_PROTOCOL,
    }
    if len(canonical_bytes(record)) > _MAX_EVENT_BYTES:
        raise ValueError("diagnostic event is too large")
    return record
