"""Non-blocking, bounded, per-process diagnostic segment writer."""

from __future__ import annotations

from collections import deque
import hashlib
import os
from pathlib import Path
from threading import Condition, Event, Lock, Thread
import time

from online_trader import __version__
from online_trader.configuration.files import publish_replace_bytes
from online_trader.contracts.codec import canonical_bytes
from online_trader.diagnostics.contract import encode_diagnostic_event, utc_now, validate_event_header


_DEFAULT_QUEUE = 2048
_DEFAULT_SEGMENT_BYTES = 2 * 1024 * 1024
_DEFAULT_STREAM_BYTES = 64 * 1024 * 1024
_UNSET = object()
_HIGH_PRIORITIES = frozenset({"HIGH", "CRITICAL"})


def _freeze_mapping(mapping):
    if not mapping:
        return None
    frozen = {}
    for key, value in mapping.items():
        if isinstance(value, (list, tuple)):
            frozen[key] = tuple(value[:16])
        else:
            frozen[key] = value
    return frozen


class NullDiagnosticSink:
    """No-op fallback used whenever diagnostics cannot initialize."""

    process_instance_id = None

    def set_configuration_revision(self, revision):
        return None

    def record(self, *_, **__):
        return False

    def finalize(self, *_, **__):
        return {
            "persisted": 0,
            "enqueued": 0,
            "dropped_by_priority": {},
            "write_failures": 1,
            "last_persisted_seq": None,
            "retention_dropped_segments": 0,
            "retention_dropped_events": 0,
            "segments": [],
        }

    close = finalize


class DiagnosticSink:
    """Assign sequence numbers before a non-blocking enqueue and write complete NDJSON lines."""

    def __init__(
        self,
        diagnostics_root,
        *,
        process_role: str,
        process_instance_id: str,
        stream_group: str,
        runtime_boot_id: str | None = None,
        account_pseudonym: str | None = None,
        configuration_revision: str | None = None,
        max_queue: int = _DEFAULT_QUEUE,
        segment_bytes: int = _DEFAULT_SEGMENT_BYTES,
        max_stream_bytes: int = _DEFAULT_STREAM_BYTES,
    ):
        if type(max_queue) is not int or not 16 <= max_queue <= 65536:
            raise ValueError("invalid diagnostic queue bound")
        if type(segment_bytes) is not int or not 64 * 1024 <= segment_bytes <= 32 * 1024 * 1024:
            raise ValueError("invalid diagnostic segment bound")
        if (type(max_stream_bytes) is not int
                or not segment_bytes <= max_stream_bytes <= 512 * 1024 * 1024):
            raise ValueError("invalid diagnostic stream bound")
        if stream_group not in {"runtime", "workbench", "runtime-view"}:
            raise ValueError("invalid diagnostic stream group")
        self.root = Path(diagnostics_root).resolve()
        self.process_role = process_role.upper()
        self.process_instance_id = process_instance_id
        self.runtime_boot_id = runtime_boot_id
        self.account_pseudonym = account_pseudonym
        self.configuration_revision = configuration_revision
        self.stream_id = f"{stream_group}:{process_instance_id}:events"
        self.directory = self.root / stream_group / process_instance_id
        self._max_queue = max_queue
        self._reserved_priority = max(4, min(max_queue // 4, 256))
        self._queue: deque = deque()
        self._closing = Event()
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._sequence = 0
        self._segment_bytes = segment_bytes
        self._max_stream_bytes = max_stream_bytes
        self._segment_number = 0
        self._stream = None
        self._stream_path = None
        self._stream_hash = None
        self._stream_size = 0
        self._stream_first_seq = None
        self._stream_last_seq = None
        self._segments = []
        self._health = {
            "enqueued": 0,
            "persisted": 0,
            "dropped_by_priority": {},
            "write_failures": 0,
            "last_persisted_seq": None,
            "retention_dropped_segments": 0,
            "retention_dropped_events": 0,
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        publish_replace_bytes(canonical_bytes({
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_STREAM/1",
            "diagnostic_protocol": "1",
            "application_version": __version__,
            "stream_id": self.stream_id,
            "process_role": self.process_role,
            "process_instance_id": self.process_instance_id,
            "runtime_boot_id": self.runtime_boot_id,
            "account_pseudonym": self.account_pseudonym,
            "configuration_revision": self.configuration_revision,
            "created_at_utc": utc_now(),
        }), self.directory / "metadata.json")
        self._writer = Thread(
            target=self._write_loop,
            name=f"OnlineTrader-diagnostics-{stream_group}",
            daemon=True,
        )
        self._writer.start()

    def set_configuration_revision(self, revision: str | None):
        with self._lock:
            self.configuration_revision = revision

    def record(
        self,
        event_type,
        *,
        component="RUNTIME",
        severity="INFO",
        priority="NORMAL",
        reason_code=None,
        error_code=None,
        correlation=None,
        payload=None,
        runtime_boot_id=_UNSET,
        account_pseudonym=_UNSET,
        configuration_revision=_UNSET,
    ):
        if self._closing.is_set():
            return False
        priority = str(priority).upper()
        if priority not in {"LOW", "NORMAL", "HIGH", "CRITICAL"}:
            priority = "NORMAL"
        try:
            role, component_name, event_name, severity_name = validate_event_header(
                process_role=self.process_role,
                component=component,
                event_type=event_type,
                severity=severity,
                reason_code=reason_code,
                error_code=error_code,
                correlation=correlation,
                payload=payload,
            )
            frozen_correlation = _freeze_mapping(correlation)
            frozen_payload = _freeze_mapping(payload)
            observed = utc_now()
            mono_ns = str(time.monotonic_ns())
        except Exception:
            with self._lock:
                self._health["write_failures"] += 1
            return False
        with self._condition:
            if self._closing.is_set():
                return False
            self._sequence += 1
            sequence = self._sequence
            limit = (
                self._max_queue
                if priority in _HIGH_PRIORITIES
                else self._max_queue - self._reserved_priority
            )
            if len(self._queue) >= limit:
                dropped = self._health["dropped_by_priority"]
                dropped[priority] = dropped.get(priority, 0) + 1
                return False
            boot = self.runtime_boot_id if runtime_boot_id is _UNSET else runtime_boot_id
            acct = self.account_pseudonym if account_pseudonym is _UNSET else account_pseudonym
            cfg_rev = (
                self.configuration_revision
                if configuration_revision is _UNSET
                else configuration_revision
            )
            self._queue.append((
                sequence,
                {
                    "stream_id": self.stream_id,
                    "event_seq": sequence,
                    "process_role": role,
                    "process_instance_id": self.process_instance_id,
                    "runtime_boot_id": boot,
                    "account_pseudonym": acct,
                    "configuration_revision": cfg_rev,
                    "component": component_name,
                    "event_type": event_name,
                    "severity": severity_name,
                    "reason_code": reason_code,
                    "error_code": error_code,
                    "observed_at_utc": observed,
                    "monotonic_ns": mono_ns,
                    "correlation": frozen_correlation,
                    "payload": frozen_payload,
                },
            ))
            self._health["enqueued"] += 1
            self._condition.notify()
            return True

    def _open_segment(self):
        self._segment_number += 1
        self._stream_path = self.directory / f"events-{self._segment_number:04}.ndjson"
        self._stream = self._stream_path.open("xb")
        self._stream_hash = hashlib.sha256()
        self._stream_size = 0
        self._stream_first_seq = None
        self._stream_last_seq = None

    def _close_segment(self):
        stream, path = self._stream, self._stream_path
        if stream is None or path is None:
            return
        try:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            metadata = {
                "schema_version": "ONLINE_TRADER_DIAGNOSTIC_SEGMENT/1",
                "stream_id": self.stream_id,
                "file": path.name,
                "first_event_seq": None if self._stream_first_seq is None else str(self._stream_first_seq),
                "last_event_seq": None if self._stream_last_seq is None else str(self._stream_last_seq),
                "bytes": self._stream_size,
                "sha256": self._stream_hash.hexdigest(),
                "closed_at_utc": utc_now(),
            }
            publish_replace_bytes(canonical_bytes(metadata), path.with_suffix(".segment.json"))
            self._segments.append(metadata)
            self._prune_closed_segments()
            self._publish_health(active=True)
        except Exception:
            self._health["write_failures"] += 1
            try:
                stream.close()
            except Exception:
                pass
        finally:
            self._stream = None
            self._stream_path = None
            self._stream_hash = None

    def _prune_closed_segments(self):
        """Prune only this writer's oldest closed segments; never touch another process."""
        total = sum(int(row.get("bytes") or 0) for row in self._segments)
        while total > self._max_stream_bytes and len(self._segments) > 1:
            oldest = self._segments[0]
            path = self.directory / oldest["file"]
            metadata = path.with_suffix(".segment.json")
            try:
                path.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
            except OSError:
                self._health["write_failures"] += 1
                break
            self._segments.pop(0)
            total = max(0, total - int(oldest.get("bytes") or 0))
            first = oldest.get("first_event_seq")
            last = oldest.get("last_event_seq")
            count = 0
            try:
                if first is not None and last is not None:
                    count = max(0, int(last) - int(first) + 1)
            except (TypeError, ValueError):
                count = 0
            self._health["retention_dropped_segments"] += 1
            self._health["retention_dropped_events"] += count

    def _publish_health(
        self,
        *,
        active,
        clean_shutdown=False,
        snapshot_available=False,
        writer_timeout=False,
    ):
        status = dict(self._health) | {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_HEALTH/1",
            "stream_id": self.stream_id,
            "active": bool(active),
            "clean_shutdown": bool(clean_shutdown),
            "snapshot_available": bool(snapshot_available),
            "writer_timeout": bool(writer_timeout),
            "finalized_at_utc": None if active else utc_now(),
            "segments": list(self._segments),
        }
        publish_replace_bytes(canonical_bytes(status), self.directory / "health.json")
        return status

    def _write_loop(self):
        while True:
            with self._condition:
                while not self._queue and not self._closing.is_set():
                    self._condition.wait(timeout=0.1)
                if not self._queue and self._closing.is_set():
                    break
                sequence, kwargs = self._queue.popleft()
            try:
                line = encode_diagnostic_event(**kwargs)
                if self._stream is None:
                    self._open_segment()
                if self._stream_size and self._stream_size + len(line) > self._segment_bytes:
                    self._close_segment()
                    self._open_segment()
                self._stream.write(line)
                self._stream_hash.update(line)
                self._stream_size += len(line)
                self._stream_first_seq = sequence if self._stream_first_seq is None else self._stream_first_seq
                self._stream_last_seq = sequence
                with self._lock:
                    self._health["persisted"] += 1
                    self._health["last_persisted_seq"] = str(sequence)
            except Exception:
                with self._lock:
                    self._health["write_failures"] += 1
        self._close_segment()

    def finalize(self, *, clean_shutdown=False, snapshot_available=False, timeout=1.0):
        if not self._closing.is_set():
            self.record(
                "STREAM_FINALIZING",
                component="LIFECYCLE",
                priority="HIGH",
                payload={
                    "clean_shutdown": bool(clean_shutdown),
                    "snapshot_available": bool(snapshot_available),
                },
            )
            with self._condition:
                self._closing.set()
                self._condition.notify_all()
            self._writer.join(timeout=max(0.0, float(timeout)))
            if self._writer.is_alive():
                with self._lock:
                    self._health["write_failures"] += 1
        writer_timeout = self._writer.is_alive()
        try:
            with self._lock:
                status = self._publish_health(
                    active=writer_timeout,
                    clean_shutdown=clean_shutdown,
                    snapshot_available=snapshot_available,
                    writer_timeout=writer_timeout,
                )
        except Exception:
            with self._lock:
                status = dict(self._health)
                status["write_failures"] += 1
        return status

    close = finalize


def open_sink(*args, **kwargs):
    """Open a sink while preserving diagnostics as a best-effort side channel."""
    try:
        return DiagnosticSink(*args, **kwargs)
    except Exception:
        return NullDiagnosticSink()
