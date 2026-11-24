"""Versioned, bounded diagnostic event contract."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import time

from online_trader.contracts.codec import canonical_bytes


EVENT_SCHEMA = "ONLINE_TRADER_DIAGNOSTIC_EVENT/1"
DIAGNOSTIC_PROTOCOL = "1"
SEVERITIES = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z", re.ASCII)
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z", re.ASCII)
_MAX_EVENT_BYTES = 64 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity(value, name, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"invalid diagnostic {name}")
    return value


def _mapping(value, name, *, max_items=32):
    if value is None:
        return {}
    if type(value) is not dict or len(value) > max_items:
        raise ValueError(f"invalid diagnostic {name}")
    # canonical_bytes supplies the recursive scalar/type/depth validation.
    raw = canonical_bytes(value)
    if len(raw) > _MAX_EVENT_BYTES // 2:
        raise ValueError(f"diagnostic {name} is too large")
    return value


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
    for value, name in ((reason_code, "reason code"), (error_code, "error code")):
        if value is not None and (not isinstance(value, str) or _NAME.fullmatch(value) is None):
            raise ValueError(f"invalid diagnostic {name}")
    record = {
        "schema_version": EVENT_SCHEMA,
        "stream_id": _identity(stream_id, "stream id"),
        "event_seq": str(event_seq),
        "observed_at_utc": utc_now(),
        "monotonic_ns": str(time.monotonic_ns()),
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
        "correlation": _mapping(correlation, "correlation"),
        "payload": _mapping(payload, "payload"),
        "diagnostic_protocol": DIAGNOSTIC_PROTOCOL,
    }
    if len(canonical_bytes(record)) > _MAX_EVENT_BYTES:
        raise ValueError("diagnostic event is too large")
    return record
