"""Non-blocking, bounded, per-process diagnostic segment writer."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time

from online_trader.configuration.files import publish_replace_bytes
from online_trader.contracts.codec import canonical_bytes
from online_trader.diagnostics.contract import diagnostic_event, utc_now


_DEFAULT_QUEUE = 2048
_DEFAULT_SEGMENT_BYTES = 2 * 1024 * 1024


class NullDiagnosticSink:
    """No-op fallback used whenever diagnostics cannot initialize."""

    process_instance_id = None

    def record(self, *_, **__):
        return False

    def finalize(self, *_, **__):
        return {
            "persisted": 0,
            "enqueued": 0,
            "dropped_by_priority": {},
            "write_failures": 1,
            "last_persisted_seq": None,
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
    ):
        if type(max_queue) is not int or not 16 <= max_queue <= 65536:
            raise ValueError("invalid diagnostic queue bound")
        if type(segment_bytes) is not int or not 64 * 1024 <= segment_bytes <= 32 * 1024 * 1024:
            raise ValueError("invalid diagnostic segment bound")
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
        self._queue: Queue = Queue(maxsize=max_queue)
        self._closing = Event()
        self._lock = Lock()
        self._sequence = 0
        self._segment_bytes = segment_bytes
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
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        publish_replace_bytes(canonical_bytes({
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_STREAM/1",
            "diagnostic_protocol": "1",
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
        runtime_boot_id=None,
        account_pseudonym=None,
        configuration_revision=None,
    ):
        if self._closing.is_set():
            return False
        priority = str(priority).upper()
        if priority not in {"LOW", "NORMAL", "HIGH", "CRITICAL"}:
            priority = "NORMAL"
        try:
            with self._lock:
                self._sequence += 1
                sequence = self._sequence
            record = diagnostic_event(
                stream_id=self.stream_id,
                event_seq=sequence,
                process_role=self.process_role,
                process_instance_id=self.process_instance_id,
                runtime_boot_id=runtime_boot_id or self.runtime_boot_id,
                account_pseudonym=account_pseudonym or self.account_pseudonym,
                configuration_revision=configuration_revision or self.configuration_revision,
                component=component,
                event_type=event_type,
                severity=severity,
                reason_code=reason_code,
                error_code=error_code,
                correlation=correlation,
                payload=payload,
            )
            line = canonical_bytes(record) + b"\n"
        except Exception:
            self._health["write_failures"] += 1
            return False
        try:
            self._queue.put_nowait((sequence, line))
            self._health["enqueued"] += 1
            return True
        except Full:
            dropped = self._health["dropped_by_priority"]
            dropped[priority] = dropped.get(priority, 0) + 1
            return False

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

    def _write_loop(self):
        while not self._closing.is_set() or not self._queue.empty():
            try:
                sequence, line = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
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
                self._health["persisted"] += 1
                self._health["last_persisted_seq"] = str(sequence)
            except Exception:
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
            self._closing.set()
            self._writer.join(timeout=max(0.0, float(timeout)))
            if self._writer.is_alive():
                self._health["write_failures"] += 1
        writer_timeout = self._writer.is_alive()
        status = dict(self._health) | {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_HEALTH/1",
            "stream_id": self.stream_id,
            "clean_shutdown": bool(clean_shutdown),
            "snapshot_available": bool(snapshot_available),
            "writer_timeout": writer_timeout,
            "finalized_at_utc": utc_now(),
            "segments": list(self._segments),
        }
        try:
            publish_replace_bytes(canonical_bytes(status), self.directory / "health.json")
        except Exception:
            status["write_failures"] += 1
        return status

    close = finalize


def open_sink(*args, **kwargs):
    """Open a sink while preserving diagnostics as a best-effort side channel."""
    try:
        return DiagnosticSink(*args, **kwargs)
    except Exception:
        return NullDiagnosticSink()
