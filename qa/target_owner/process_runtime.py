"""QA-only Standalone Target Owner hosted in an isolated child process.

The parent injects offline fact frames over a private multiprocessing pipe and
observes adapter send/cancel calls as events. Product commands and projections
remain on the existing ControlServer Named Pipe. This module must never be
imported by the production Runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from queue import Full, Queue
import tempfile
from threading import Condition, Lock, Thread
import time
import traceback
from uuid import uuid4

from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.standalone_owner import StandaloneDsvOwner
from online_trader.kernel.server import ControlServer
from online_trader.runtime.session_storage import RuntimeSessionStorage
from qa.target_owner.offline_runtime import DAY, OfflineAdapter, profile
from qa.recording_port import RecordingPort


PROTOCOL = "TARGET_OWNER_QA_PROCESS/1"
_MAX_FACT_FRAME_BYTES = 1_048_576


class OwnerProcessError(RuntimeError):
    """Raised when the isolated QA Owner cannot start or stop cleanly."""


def _json_copy(value: object, *, limit: int = _MAX_FACT_FRAME_BYTES) -> object:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("QA process message exceeds its size limit")
    return json.loads(encoded.decode("utf-8"))


def _error_record(error: BaseException) -> dict[str, object]:
    frames = traceback.extract_tb(error.__traceback__)
    safe_frames = [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in frames[-8:]
    ]
    message_hash = hashlib.sha256(str(error).encode("utf-8", "replace")).hexdigest()
    return {
        "error_type": type(error).__name__,
        "error_code": getattr(error, "code", "E_QA_OWNER_PROCESS"),
        "frames": safe_frames,
        "message_sha256": message_hash,
    }


class _ObservedFrames(Queue):
    """Thread-local frame queue that lets the Owner host record consumption."""

    def __init__(self):
        super().__init__(maxsize=128)
        self.last_popped = None

    def get_nowait(self):
        value = super().get_nowait()
        self.last_popped = value
        return value


class _ObservedAdapter(OfflineAdapter):
    def __init__(self, frames: _ObservedFrames, emit: Callable[..., None]):
        super().__init__(frames)
        self._emit = emit

    def send(self, request):
        copied = deepcopy(request)
        try:
            result = RecordingPort.send(self, copied)
        except BaseException as error:
            self._emit("OUTBOUND_SEND", request=copied, adapter_result="ERROR",
                       adapter_error_type=type(error).__name__)
            raise
        self._emit("OUTBOUND_SEND", request=copied, adapter_result="RETURNED")
        return result

    def cancel(self, request):
        copied = deepcopy(request)
        try:
            result = RecordingPort.cancel(self, copied)
        except BaseException as error:
            self._emit("OUTBOUND_CANCEL", request=copied, adapter_result="ERROR",
                       adapter_error_type=type(error).__name__)
            raise
        self._emit("OUTBOUND_CANCEL", request=copied, adapter_result="RETURNED")
        return result


def _owner_process_main(storage_root: str, pipe_name: str, run_id: str, control) -> None:
    """Spawn target. All owner construction and calls stay on this process thread."""
    event_seq = 0
    shutdown_cause = None
    boot_id = None
    ledger = None

    def emit(event_type: str, **fields) -> None:
        nonlocal event_seq
        event_seq += 1
        packet = {
            "protocol": PROTOCOL,
            "run_id": run_id,
            "event_seq": event_seq,
            "event_type": event_type,
            "owner_pid": os.getpid(),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"), default=str).encode("utf-8")
        if len(encoded) > 4 * _MAX_FACT_FRAME_BYTES:
            raise ValueError("QA owner event exceeds its size limit")
        control.send(json.loads(encoded.decode("utf-8")))

    try:
        runtime_root = Path(storage_root).resolve()
        runtime_root.mkdir(parents=True, exist_ok=True)
        # RuntimeSessionStorage uses TemporaryDirectory. Point it inside this
        # run's dedicated root so a forced process kill cannot leak global temp data.
        tempfile.tempdir = str(runtime_root)
        frames = _ObservedFrames()
        account = "DEV:target-owner-" + uuid4().hex
        settings = profile()
        connection = None
        owner = None

        with RuntimeSessionStorage() as storage:
            boot_id, ledger = storage.boot_id, storage.ledger
            binding = StandaloneOwnerBinding(boot_id, account, "OFFLINE", object(), object())
            adapter = _ObservedAdapter(frames, emit)
            connection = adapter.connection
            session = DsvSession(
                storage.ledger, account_key=account, provider="OFFLINE",
                trading_day=DAY, profile=settings, port=adapter,
                _owner_binding=binding)
            session.disconnect("QA_OWNER_CREATED_DISARMED")
            owner = StandaloneDsvOwner(
                session, adapter, connection, settings, binding,
                configuration_revision="1")
            server = ControlServer(owner, pipe_name)
            last_input_sequence = 0
            seen_frame_ids: set[str] = set()
            ready_sent = False

            def owner_tick():
                nonlocal last_input_sequence, shutdown_cause, ready_sent
                if not ready_sent:
                    emit(
                        "OWNER_READY",
                        runtime_boot_id=boot_id,
                        account_key=account,
                        execution_core="DSV_TARGET_EXECUTION",
                        configuration_revision="1",
                        profile_hash=settings.content_hash,
                        trading_day=DAY,
                        source="QA_OFFLINE_FACT_ADAPTER",
                        real_broker_connection=False,
                        real_order_submission=False,
                        product_pipe=pipe_name,
                        journal_scope="TEMPORARY_CURRENT_BOOT",
                        entry_authority="DISARMED",
                    )
                    ready_sent = True
                while control.poll(0):
                    try:
                        message = control.recv()
                    except EOFError:
                        shutdown_cause = "PARENT_DISCONNECTED"
                        owner.stop_event.set()
                        return
                    if not isinstance(message, dict):
                        emit("HARNESS_MESSAGE_REJECTED", reason_code="MESSAGE_NOT_OBJECT")
                        continue
                    kind = message.get("type")
                    if kind == "HARNESS_TEARDOWN":
                        shutdown_cause = "HARNESS_TEARDOWN"
                        emit("HARNESS_TEARDOWN_ACCEPTED", request_id=message.get("request_id"))
                        owner.stop_event.set()
                        return
                    if kind != "FACT_FRAME":
                        emit("HARNESS_MESSAGE_REJECTED", reason_code="UNSUPPORTED_MESSAGE_TYPE")
                        continue
                    frame = message.get("frame")
                    if not isinstance(frame, dict):
                        emit("FACT_FRAME_REJECTED", reason_code="FRAME_NOT_OBJECT")
                        continue
                    frame_id = frame.get("id")
                    sequence = frame.get("sequence")
                    if (not isinstance(frame_id, str) or not frame_id or len(frame_id) > 128
                            or type(sequence) is not int or sequence <= last_input_sequence
                            or frame_id in seen_frame_ids):
                        emit("FACT_FRAME_REJECTED", frame_id=frame_id,
                             reason_code="FRAME_ID_OR_SEQUENCE_INVALID")
                        continue
                    try:
                        clean_frame = _json_copy(frame)
                        frames.put_nowait(clean_frame)
                    except (TypeError, ValueError):
                        emit("FACT_FRAME_REJECTED", frame_id=frame_id,
                             reason_code="FRAME_NOT_JSON_OR_TOO_LARGE")
                        continue
                    except Full:
                        emit("FACT_FRAME_REJECTED", frame_id=frame_id,
                             reason_code="FRAME_QUEUE_FULL")
                        continue
                    last_input_sequence = sequence
                    seen_frame_ids.add(frame_id)
                    emit("FACT_FRAME_ACCEPTED", frame_id=frame_id, input_sequence=sequence)

                frames.last_popped = None
                receipt = owner.step(trading_day=DAY, complete=connection.complete)
                applied = frames.last_popped
                if applied is not None:
                    emit(
                        "FACT_FRAME_APPLIED",
                        frame_id=applied.get("id"),
                        input_sequence=applied.get("sequence"),
                        application_result=(receipt.get("type") if isinstance(receipt, dict) else None),
                    )

            try:
                server.serve(stop_event=owner.stop_event, owner_tick=owner_tick,
                             publish_workbench=False)
                shutdown = owner.session.prepare_shutdown()
            finally:
                owner.close()
                owner = None

        emit(
            "OWNER_STOPPED",
            runtime_boot_id=boot_id,
            shutdown_cause=shutdown_cause or "PRODUCT_OWNER_STOP_EVENT",
            shutdown_ready=bool(shutdown.get("ready")),
            ledger_removed=not Path(ledger).exists(),
        )
    except BaseException as error:
        try:
            emit("CHILD_ERROR", runtime_boot_id=boot_id, shutdown_cause=shutdown_cause,
                 error=_error_record(error))
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        try:
            control.close()
        except OSError:
            pass


class OfflineTargetOwnerProcess:
    """Parent-side process manager and QA fact channel.

    The class exposes a product pipe name and event cursor, never a child Owner
    object. Callers should inspect behavior through a product StatusClient.
    """

    def __init__(self, root: str | Path, *, startup_timeout: float = 10.0,
                 shutdown_timeout: float = 10.0):
        self.root = Path(root).resolve()
        self.runtime_root = self.root / "runtime-temp"
        self.pipe = "online-trader-target-process-qa-" + uuid4().hex
        self.run_id = "qa-process-" + uuid4().hex
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.boot_id = None
        self.account_key = None
        self.owner_pid = None
        self._context = multiprocessing.get_context("spawn")
        self._process = None
        self._control = None
        self._reader = None
        self._events: list[dict[str, object]] = []
        self._condition = Condition()
        self._event_log_lock = Lock()
        self._send_lock = Lock()
        self._input_lock = Lock()
        self._input_seq = 0
        self._observed_order = 0
        self._pending_frames: set[str] = set()
        self._tearing_down = False
        self._input_log = None
        self._event_log = None
        self._started = False
        self._closed = False

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        with self._condition:
            return tuple(deepcopy(self._events))

    @property
    def event_cursor(self) -> int:
        with self._condition:
            return len(self._events)

    def start(self) -> "OfflineTargetOwnerProcess":
        if self._started:
            raise OwnerProcessError("Harness process already started")
        if os.name != "nt":
            raise OwnerProcessError("Standalone Target Owner process harness requires Windows Named Pipes")
        self.root.mkdir(parents=True, exist_ok=False)
        self.runtime_root.mkdir()
        self._input_log = (self.root / "inputs.jsonl").open("x", encoding="utf-8")
        self._event_log = (self.root / "observed-events.jsonl").open("x", encoding="utf-8")
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_owner_process_main,
            name="online-trader-target-owner-qa",
            args=(str(self.runtime_root), self.pipe, self.run_id, child),
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            self._close_logs()
            raise
        child.close()
        self._process = process
        self._control = parent
        self._started = True
        self._reader = Thread(target=self._read_events, name="target-owner-qa-events", daemon=True)
        self._reader.start()
        try:
            ready = self.wait_for_event("OWNER_READY", timeout=self.startup_timeout, after=0)
        except OwnerProcessError:
            self.close(force=True)
            raise
        self.boot_id = ready["runtime_boot_id"]
        self.account_key = ready["account_key"]
        self.owner_pid = ready["owner_pid"]
        metadata = {
            "protocol": PROTOCOL,
            "run_id": self.run_id,
            "parent_pid": os.getpid(),
            "owner_pid": self.owner_pid,
            "runtime_boot_id": self.boot_id,
            "configuration_revision": ready["configuration_revision"],
            "profile_hash": ready["profile_hash"],
            "trading_day": ready["trading_day"],
            "source": ready["source"],
            "product_pipe": self.pipe,
            "platform": os.name,
            "runtime_root": "PER_RUN_TEMPORARY",
            "real_broker_connection": False,
            "real_order_submission": False,
        }
        try:
            (self.root / "process.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except BaseException:
            self.close(force=True)
            raise
        return self

    def wait_for_event(self, event_type: str, *, timeout: float = 5.0, after: int = 0,
                       predicate: Callable[[dict[str, object]], bool] | None = None) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self._events[after:]:
                    if event.get("event_type") == event_type and (predicate is None or predicate(event)):
                        return deepcopy(event)
                    if event.get("event_type") == "CHILD_ERROR":
                        raise OwnerProcessError(f"Owner child failed: {event.get('error')}")
                if (self._process is not None and not self._process.is_alive()
                        and (self._reader is None or not self._reader.is_alive())):
                    raise OwnerProcessError(f"Owner child exited with code {self._process.exitcode}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OwnerProcessError(f"Timed out waiting for Owner event {event_type}")
                self._condition.wait(min(remaining, 0.25))

    def send_fact_frame(self, frame: dict[str, object], *, timeout: float = 5.0) -> dict[str, object]:
        if not self.is_alive or self._control is None:
            raise OwnerProcessError("Owner child is not running")
        clean_frame = _json_copy(frame)
        if not isinstance(clean_frame, dict):
            raise ValueError("Fact frame must be an object")
        frame_id = clean_frame.get("id")
        sequence = clean_frame.get("sequence")
        if not isinstance(frame_id, str) or type(sequence) is not int:
            raise ValueError("Fact frame requires a string id and integer sequence")
        cursor = self.event_cursor
        input_record = {
            "input_seq": self._input_seq + 1,
            "event_type": "FAKE_BROKER_FACT_FRAME",
            "run_id": self.run_id,
            "frame_id": frame_id,
            "input_sequence": sequence,
            "monotonic_ns": time.monotonic_ns(),
            "frame": clean_frame,
        }
        with self._input_lock:
            if self._tearing_down:
                raise OwnerProcessError("Cannot submit a fact frame during teardown")
            self._input_seq += 1
            input_record["input_seq"] = self._input_seq
            with self._condition:
                self._pending_frames.add(frame_id)
            self._write_jsonl(self._input_log, input_record)
            with self._send_lock:
                self._control.send({"type": "FACT_FRAME", "frame": clean_frame})
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self._events[cursor:]:
                    if event.get("frame_id") != frame_id:
                        continue
                    if event.get("event_type") == "FACT_FRAME_ACCEPTED":
                        return deepcopy(event)
                    if event.get("event_type") == "FACT_FRAME_REJECTED":
                        raise OwnerProcessError(
                            f"Fact frame rejected: {event.get('reason_code', 'UNKNOWN')}"
                        )
                if (self._process is not None and not self._process.is_alive()
                        and (self._reader is None or not self._reader.is_alive())):
                    raise OwnerProcessError(f"Owner child exited with code {self._process.exitcode}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OwnerProcessError(f"Timed out waiting for fact frame {frame_id}")
                self._condition.wait(min(remaining, 0.25))

    def wait_for_frame_applied(self, frame_id: str, *, timeout: float = 5.0,
                               after: int = 0) -> dict[str, object]:
        return self.wait_for_event(
            "FACT_FRAME_APPLIED", timeout=timeout, after=after,
            predicate=lambda event: event.get("frame_id") == frame_id)

    def teardown(self, *, timeout: float | None = None) -> dict[str, object] | None:
        if not self.is_alive or self._control is None:
            return None
        deadline = time.monotonic() + (timeout or self.shutdown_timeout)
        with self._input_lock:
            self._tearing_down = True
        with self._condition:
            pending_ids = list(self._pending_frames)
        for frame_id in pending_ids:
            with self._condition:
                pending = frame_id in self._pending_frames
            if pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OwnerProcessError("Cannot teardown with unapplied fact frames")
                self.wait_for_frame_applied(frame_id, timeout=remaining)
        cursor = self.event_cursor
        request_id = uuid4().hex
        with self._input_lock:
            self._input_seq += 1
            self._write_jsonl(self._input_log, {
                "input_seq": self._input_seq,
                "event_type": "HARNESS_TEARDOWN",
                "run_id": self.run_id,
                "request_id": request_id,
                "monotonic_ns": time.monotonic_ns(),
            })
            with self._send_lock:
                self._control.send({"type": "HARNESS_TEARDOWN", "request_id": request_id})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OwnerProcessError("Timed out before Owner teardown completed")
        return self.wait_for_event("OWNER_STOPPED", timeout=remaining, after=cursor)

    def terminate(self, *, timeout: float = 5.0) -> int | None:
        """Force-stop the single Owner process and record it as abnormal teardown."""
        process = self._process
        if process is None:
            return None
        if process.is_alive():
            process.terminate()
            process.join(timeout)
        if process.is_alive():
            process.kill()
            process.join(timeout)
        if process.is_alive():
            raise OwnerProcessError("Owner child survived terminate and kill")
        exit_code = process.exitcode
        self._record_parent_event({
            "protocol": PROTOCOL,
            "run_id": self.run_id,
            "event_type": "OWNER_PROCESS_FORCIBLY_TERMINATED",
            "owner_pid": process.pid,
            "exit_code": exit_code,
            "source": "HARNESS_PARENT",
            "monotonic_ns": time.monotonic_ns(),
        })
        return exit_code

    def close(self, *, force: bool = False) -> None:
        if self._closed:
            return
        process = self._process
        if process is not None and process.is_alive():
            if not force:
                try:
                    self.teardown(timeout=self.shutdown_timeout)
                except (OwnerProcessError, BrokenPipeError, EOFError, OSError):
                    force = True
            if process.is_alive() and force:
                self.terminate()
        if process is not None:
            process.join(0.1)
        if self._reader is not None:
            self._reader.join(2)
        if self._control is not None:
            try:
                self._control.close()
            except OSError:
                pass
            self._control = None
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(1)
        if process is not None and not process.is_alive():
            with self._condition:
                has_terminal = any(event.get("event_type") in {
                    "OWNER_STOPPED", "OWNER_PROCESS_FORCIBLY_TERMINATED", "OWNER_PROCESS_EXITED"
                } for event in self._events)
            if not has_terminal:
                self._record_parent_event({
                    "protocol": PROTOCOL,
                    "run_id": self.run_id,
                    "event_type": "OWNER_PROCESS_EXITED",
                    "owner_pid": process.pid,
                    "exit_code": process.exitcode,
                    "source": "HARNESS_PARENT",
                    "monotonic_ns": time.monotonic_ns(),
                })
        if process is not None and not process.is_alive():
            process.close()
        self._close_logs()
        self._closed = True

    def _read_events(self) -> None:
        control = self._control
        if control is None:
            return
        while True:
            try:
                if not control.poll(0.25):
                    if self._process is not None and not self._process.is_alive():
                        break
                    continue
                event = control.recv()
            except (EOFError, OSError):
                break
            if not isinstance(event, dict):
                continue
            self._record_event(event)
            with self._condition:
                if event.get("event_type") in {"FACT_FRAME_APPLIED", "FACT_FRAME_REJECTED"}:
                    self._pending_frames.discard(str(event.get("frame_id")))
                self._condition.notify_all()
        with self._condition:
            self._condition.notify_all()

    @staticmethod
    def _write_jsonl(stream, value: dict[str, object]) -> None:
        if stream is None or stream.closed:
            return
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()

    def _record_parent_event(self, event: dict[str, object]) -> None:
        self._record_event(event)

    def _record_event(self, event: dict[str, object]) -> None:
        with self._event_log_lock:
            with self._condition:
                self._observed_order += 1
                event = dict(event, observed_order=self._observed_order)
                self._events.append(event)
                self._condition.notify_all()
            self._write_jsonl(self._event_log, event)

    def _close_logs(self) -> None:
        for stream in (self._input_log, self._event_log):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self) -> "OfflineTargetOwnerProcess":
        return self.start()

    def __exit__(self, *_args) -> None:
        self.close()
