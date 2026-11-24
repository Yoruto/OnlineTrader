"""QA-only Desktop Runtime Host with real ViewProcess, Workbench publication and isolated config.

Reuses StandaloneDsvOwner, DsvSession, ControlServer and ViewProcess in an
isolated child process without adding test flags to production ``runtime run``.
"""

from __future__ import annotations

from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
from queue import Full
import tempfile
from threading import Condition, Lock, Thread
import time
from uuid import uuid4

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.standalone_owner import StandaloneDsvOwner
from online_trader.kernel.server import ControlServer
from online_trader.runtime.session_storage import RuntimeSessionStorage
from online_trader.runtime.view_process import ViewProcess
from qa.target_owner.offline_runtime import DAY, profile
from qa.target_owner.process_runtime import (
    OwnerProcessError,
    PROTOCOL,
    _MAX_FACT_FRAME_BYTES,
    _ObservedAdapter,
    _ObservedFrames,
    _error_record,
    _json_copy,
)


def _desktop_host_main(
    runtime_root_str: str,
    pipe_name: str,
    run_id: str,
    control,
    profile_record: dict[str, object] | None = None,
    trading_day: str = DAY,
) -> None:
    """Spawn target for QA Desktop Runtime Host (Owner + ControlServer + ViewProcess)."""
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
        encoded = json.dumps(
            packet, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str
        ).encode("utf-8")
        if len(encoded) > 4 * _MAX_FACT_FRAME_BYTES:
            raise ValueError("QA desktop host event exceeds size limit")
        control.send(json.loads(encoded.decode("utf-8")))

    try:
        runtime_root = Path(runtime_root_str).resolve()
        runtime_root.mkdir(parents=True, exist_ok=True)
        temp_dir = runtime_root / "runtime-temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(temp_dir)

        frames = _ObservedFrames()
        account = "DEV:desktop-qa-" + uuid4().hex
        settings = DsvProfile.from_record(profile_record) if profile_record is not None else profile()
        param_path = runtime_root / "OnlineTrader-parameters.json"
        param_path.write_bytes(canonical_bytes(settings.to_record()))
        history_path = runtime_root / "history-cache.db"
        shutdown = {"ready": False}

        with RuntimeSessionStorage() as storage:
            boot_id, ledger = storage.boot_id, storage.ledger
            binding = StandaloneOwnerBinding(boot_id, account, "OFFLINE", object(), object())
            adapter = _ObservedAdapter(frames, emit)
            connection = adapter.connection
            session = DsvSession(
                storage.ledger,
                account_key=account,
                provider="OFFLINE",
                trading_day=trading_day,
                profile=settings,
                port=adapter,
                _owner_binding=binding,
            )
            session.disconnect("QA_DESKTOP_HOST_CREATED_DISARMED")
            owner = StandaloneDsvOwner(
                session,
                adapter,
                connection,
                settings,
                binding,
                configuration_revision="1",
            )
            owner.run_directory = str(runtime_root)

            def on_save(candidate: DsvProfile) -> bytes:
                previous = param_path.read_bytes()
                param_path.write_bytes(canonical_bytes(candidate.to_record()))
                return previous

            def on_restore(previous: bytes) -> None:
                param_path.write_bytes(previous)

            owner.on_parameters_saved = on_save
            owner.on_parameters_restore = on_restore

            view_process = ViewProcess(pipe_name, account, runtime_root, storage.ledger, history_path)
            try:
                view_deadline = time.monotonic() + 5.0
                while view_process.trusted_pid() <= 0 and time.monotonic() < view_deadline:
                    time.sleep(0.05)
                server = ControlServer(owner, pipe_name, trusted_client_pid=view_process.trusted_pid)
                last_input_sequence = 0
                seen_frame_ids: set[str] = set()
                ready_sent = False

                def owner_tick() -> None:
                    nonlocal last_input_sequence, shutdown_cause, ready_sent
                    if not ready_sent:
                        emit(
                            "OWNER_READY",
                            runtime_boot_id=boot_id,
                            account_key=account,
                            execution_core="DSV_TARGET_EXECUTION",
                            configuration_revision="1",
                            profile_hash=settings.content_hash,
                            trading_day=trading_day,
                            source="QA_DESKTOP_HOST",
                            real_broker_connection=False,
                            real_order_submission=False,
                            product_pipe=pipe_name,
                            journal_scope="TEMPORARY_CURRENT_BOOT",
                            entry_authority="DISARMED",
                            view_pid=view_process.trusted_pid(),
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
                        if kind == "SET_ADAPTER_FAULT":
                            adapter.fail_send = bool(message.get("fail_send", False))
                            emit("ADAPTER_FAULT_UPDATED", fail_send=adapter.fail_send)
                            continue
                        if kind != "FACT_FRAME":
                            emit("HARNESS_MESSAGE_REJECTED", reason_code="UNSUPPORTED_MESSAGE_TYPE")
                            continue
                        frame = message.get("frame")
                        if not isinstance(frame, dict):
                            emit("FACT_FRAME_REJECTED", reason_code="FRAME_NOT_OBJECT")
                            continue
                        frame_id = frame.get("id")
                        sequence = frame.get("sequence")
                        if (
                            not isinstance(frame_id, str)
                            or not frame_id
                            or len(frame_id) > 128
                            or type(sequence) is not int
                            or sequence <= last_input_sequence
                            or frame_id in seen_frame_ids
                        ):
                            emit(
                                "FACT_FRAME_REJECTED",
                                frame_id=frame_id,
                                reason_code="FRAME_ID_OR_SEQUENCE_INVALID",
                            )
                            continue
                        try:
                            clean_frame = _json_copy(frame)
                            frames.put_nowait(clean_frame)
                        except (TypeError, ValueError):
                            emit(
                                "FACT_FRAME_REJECTED",
                                frame_id=frame_id,
                                reason_code="FRAME_NOT_JSON_OR_TOO_LARGE",
                            )
                            continue
                        except Full:
                            emit(
                                "FACT_FRAME_REJECTED",
                                frame_id=frame_id,
                                reason_code="FRAME_QUEUE_FULL",
                            )
                            continue
                        last_input_sequence = sequence
                        seen_frame_ids.add(frame_id)
                        emit("FACT_FRAME_ACCEPTED", frame_id=frame_id, input_sequence=sequence)

                    frames.last_popped = None
                    receipt = owner.step(trading_day=trading_day, complete=connection.complete)
                    applied = frames.last_popped
                    if applied is not None:
                        emit(
                            "FACT_FRAME_APPLIED",
                            frame_id=applied.get("id"),
                            input_sequence=applied.get("sequence"),
                            application_result=(receipt.get("type") if isinstance(receipt, dict) else None),
                        )

                try:
                    server.serve(stop_event=owner.stop_event, owner_tick=owner_tick, publish_workbench=True)
                    shutdown = owner.session.prepare_shutdown()
                finally:
                    owner.close()
            finally:
                view_process.close()

        emit(
            "OWNER_STOPPED",
            runtime_boot_id=boot_id,
            shutdown_cause=shutdown_cause or "PRODUCT_OWNER_STOP_EVENT",
            shutdown_ready=bool(shutdown.get("ready")),
            ledger_removed=not Path(ledger).exists(),
        )
    except BaseException as error:
        try:
            emit(
                "CHILD_ERROR",
                runtime_boot_id=boot_id,
                shutdown_cause=shutdown_cause,
                error=_error_record(error),
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        try:
            control.close()
        except OSError:
            pass


class QADesktopHost:
    """Manages an isolated QA Desktop Runtime Host (Owner + ControlServer + ViewProcess)."""

    def __init__(
        self,
        root: str | Path,
        *,
        startup_timeout: float = 15.0,
        shutdown_timeout: float = 15.0,
        profile_record: dict[str, object] | None = None,
        trading_day: str = DAY,
    ) -> None:
        self.root = Path(root).resolve()
        self.pipe = "online-trader-desktop-qa-" + uuid4().hex
        self.run_id = "qa-desktop-host-" + uuid4().hex
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.profile_record = deepcopy(profile_record) if profile_record is not None else None
        self.trading_day = trading_day
        self.boot_id: str | None = None
        self.account_key: str | None = None
        self.owner_pid: int | None = None
        self.view_pid: int | None = None
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
        return not self._closed and self._process is not None and self._process.is_alive()

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        with self._condition:
            return tuple(deepcopy(self._events))

    @property
    def event_cursor(self) -> int:
        with self._condition:
            return len(self._events)

    def start(self) -> "QADesktopHost":
        if self._started:
            raise OwnerProcessError("QA Desktop Host already started")
        if os.name != "nt":
            raise OwnerProcessError("QA Desktop Host requires Windows Named Pipes")
        self.root.mkdir(parents=True, exist_ok=True)
        self._input_log = (self.root / "inputs.jsonl").open("w", encoding="utf-8")
        self._event_log = (self.root / "observed-events.jsonl").open("w", encoding="utf-8")
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_desktop_host_main,
            name="online-trader-qa-desktop-host",
            args=(str(self.root), self.pipe, self.run_id, child, self.profile_record, self.trading_day),
            daemon=True,
        )
        process.start()
        child.close()
        self._process = process
        self._control = parent
        self._started = True
        self._reader = Thread(target=self._read_events, name="qa-desktop-host-events", daemon=True)
        self._reader.start()
        try:
            ready = self.wait_for_event("OWNER_READY", timeout=self.startup_timeout, after=0)
        except OwnerProcessError:
            self.close(force=True)
            raise
        self.boot_id = str(ready["runtime_boot_id"])
        self.account_key = str(ready["account_key"])
        self.owner_pid = int(ready["owner_pid"])
        self.view_pid = ready.get("view_pid")  # type: ignore[assignment]
        return self

    def wait_for_event(
        self,
        event_type: str,
        *,
        timeout: float = 5.0,
        after: int = 0,
        predicate=None,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self._events[after:]:
                    if event.get("event_type") == event_type and (predicate is None or predicate(event)):
                        return deepcopy(event)
                    if event.get("event_type") == "CHILD_ERROR":
                        raise OwnerProcessError(f"QA Desktop Host failed: {event.get('error')}")
                if (
                    self._process is not None
                    and not self._process.is_alive()
                    and (self._reader is None or not self._reader.is_alive())
                ):
                    raise OwnerProcessError(f"QA Desktop Host exited with code {self._process.exitcode}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OwnerProcessError(f"Timed out waiting for QA Desktop Host event {event_type}")
                self._condition.wait(min(remaining, 0.25))

    def send_fact_frame(self, frame: dict[str, object], *, timeout: float = 5.0) -> dict[str, object]:
        if not self.is_alive or self._control is None:
            raise OwnerProcessError("QA Desktop Host is not running")
        clean_frame = _json_copy(frame)
        assert isinstance(clean_frame, dict)
        frame_id = str(clean_frame["id"])
        sequence = int(clean_frame["sequence"])  # type: ignore[arg-type]
        cursor = self.event_cursor
        with self._input_lock:
            if self._tearing_down:
                raise OwnerProcessError("Cannot submit frame during teardown")
            self._input_seq += 1
            with self._condition:
                self._pending_frames.add(frame_id)
            self._write_jsonl(
                self._input_log,
                {
                    "input_seq": self._input_seq,
                    "event_type": "FAKE_BROKER_FACT_FRAME",
                    "run_id": self.run_id,
                    "frame_id": frame_id,
                    "input_sequence": sequence,
                    "monotonic_ns": time.monotonic_ns(),
                    "frame": clean_frame,
                },
            )
            with self._send_lock:
                self._control.send({"type": "FACT_FRAME", "frame": clean_frame})
        return self.wait_for_event(
            "FACT_FRAME_ACCEPTED",
            timeout=timeout,
            after=cursor,
            predicate=lambda ev: ev.get("frame_id") == frame_id,
        )

    def set_adapter_fault(self, *, fail_send: bool = False, timeout: float = 5.0) -> dict[str, object]:
        if not self.is_alive or self._control is None:
            raise OwnerProcessError("QA Desktop Host is not running")
        cursor = self.event_cursor
        with self._input_lock:
            with self._send_lock:
                self._control.send({"type": "SET_ADAPTER_FAULT", "fail_send": bool(fail_send)})
        return self.wait_for_event("ADAPTER_FAULT_UPDATED", timeout=timeout, after=cursor)

    def wait_for_frame_applied(self, frame_id: str, *, timeout: float = 5.0, after: int = 0) -> dict[str, object]:
        return self.wait_for_event(
            "FACT_FRAME_APPLIED",
            timeout=timeout,
            after=after,
            predicate=lambda event: event.get("frame_id") == frame_id,
        )

    def wait_for_exit(self, *, timeout: float = 10.0) -> int | None:
        if self._process is None:
            return None
        self._process.join(timeout)
        if self._process.is_alive():
            raise OwnerProcessError(f"QA Desktop Host pid={self._process.pid} did not exit within {timeout}s")
        return self._process.exitcode

    def teardown(self, *, timeout: float | None = None) -> dict[str, object] | None:
        if not self.is_alive or self._control is None:
            return None
        deadline = time.monotonic() + (timeout or self.shutdown_timeout)
        with self._input_lock:
            self._tearing_down = True
        cursor = self.event_cursor
        request_id = uuid4().hex
        with self._send_lock:
            self._control.send({"type": "HARNESS_TEARDOWN", "request_id": request_id})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OwnerProcessError("Timed out before QA Desktop Host teardown completed")
        return self.wait_for_event("OWNER_STOPPED", timeout=remaining, after=cursor)

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
                process.terminate()
                process.join(3)
                if process.is_alive():
                    process.kill()
                    process.join(2)
        if process is not None:
            process.join(0.2)
            if not process.is_alive():
                process.close()
                self._process = None
        if self._reader is not None:
            self._reader.join(2)
        if self._control is not None:
            try:
                self._control.close()
            except OSError:
                pass
            self._control = None
        for stream in (self._input_log, self._event_log):
            if stream is not None and not stream.closed:
                stream.close()
        self._closed = True

    def __enter__(self) -> "QADesktopHost":
        return self.start()

    def __exit__(self, _exc_type, _exc_val, _tb) -> None:
        self.close()

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
            with self._event_log_lock:
                with self._condition:
                    self._observed_order += 1
                    tagged = dict(event, observed_order=self._observed_order)
                    self._events.append(tagged)
                    if tagged.get("event_type") in {"FACT_FRAME_APPLIED", "FACT_FRAME_REJECTED"}:
                        self._pending_frames.discard(str(tagged.get("frame_id")))
                    self._condition.notify_all()
                self._write_jsonl(self._event_log, tagged)
        with self._condition:
            self._condition.notify_all()

    @staticmethod
    def _write_jsonl(stream, value: dict[str, object]) -> None:
        if stream is None or stream.closed:
            return
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
