"""Windows overlapped named pipes with explicit local-user ACL and peer checks.

This module authenticates the Windows SID/PID, not application roles. The dispatcher
must separately bind registered instances and capabilities. All messages are OTIP
bytes; no Python object serialization or arbitrary RPC is accepted.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import math
import os
import re
import threading
import time

from online_trader.contracts.codec import FrameDecoder, encode_frame
from online_trader.contracts.validation import validate_message
from online_trader.errors import TraderError

_WINDOWS = os.name == "nt"
_INVALID = ctypes.c_void_p(-1).value
_IO_PENDING = 997
_NO_DATA = 232
_PIPE_CONNECTED = 535
_TIMEOUT = 258
_MAX_TIMEOUT = 30.0


if _WINDOWS:
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _a = ctypes.WinDLL("advapi32", use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", w.LPVOID), ("Attributes", w.DWORD)]

    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    def _signature(lib, name, restype, *args):
        func = getattr(lib, name); func.restype = restype; func.argtypes = list(args)
        return func

    _signature(_k, "CloseHandle", w.BOOL, w.HANDLE)
    _signature(_k, "LocalFree", w.HANDLE, w.HANDLE)
    _signature(_k, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    _signature(_k, "CreateEventW", w.HANDLE, w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR)
    _signature(_k, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
    _signature(_k, "GetOverlappedResult", w.BOOL, w.HANDLE, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(w.DWORD), w.BOOL)
    _signature(_k, "CancelIoEx", w.BOOL, w.HANDLE, ctypes.POINTER(_OVERLAPPED))
    _signature(_k, "CreateNamedPipeW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.POINTER(_SECURITY_ATTRIBUTES))
    _signature(_k, "ConnectNamedPipe", w.BOOL, w.HANDLE, ctypes.POINTER(_OVERLAPPED))
    _signature(_k, "DisconnectNamedPipe", w.BOOL, w.HANDLE)
    _signature(_k, "GetNamedPipeClientProcessId", w.BOOL, w.HANDLE, ctypes.POINTER(w.ULONG))
    _signature(_k, "GetNamedPipeServerProcessId", w.BOOL, w.HANDLE, ctypes.POINTER(w.ULONG))
    _signature(_k, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
    _signature(_k, "WaitNamedPipeW", w.BOOL, w.LPCWSTR, w.DWORD)
    _signature(_k, "ReadFile", w.BOOL, w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(_OVERLAPPED))
    _signature(_k, "WriteFile", w.BOOL, w.HANDLE, w.LPCVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(_OVERLAPPED))
    _signature(_a, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
    _signature(_a, "GetTokenInformation", w.BOOL, w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD))
    _signature(_a, "ConvertSidToStringSidW", w.BOOL, w.LPVOID, ctypes.POINTER(w.LPWSTR))
    _signature(_a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL, w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD))


def _require_windows() -> None:
    if not _WINDOWS:
        raise TraderError("E_UNSUPPORTED", "Windows named pipes require Windows")


def _error(code: str = "E_PIPE", message: str = "Local pipe operation failed") -> TraderError:
    # Preserve only a numeric OS error; never echo names or message contents.
    return TraderError(code, f"{message} (winerror={ctypes.get_last_error()})")


def _deadline(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= _MAX_TIMEOUT:
        raise TraderError("E_SCHEMA", "Pipe timeout must be between zero and thirty seconds")
    return time.monotonic() + timeout


def _milliseconds(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TraderError("E_TIMEOUT", "Local pipe deadline expired")
    return max(1, math.ceil(remaining * 1000))


def _pipe_path(name: str) -> str:
    if type(name) is not str:
        raise TraderError("E_SCHEMA", "Invalid local pipe instance name")
    if name.startswith("\\\\.\\pipe\\"):
        name = name[len("\\\\.\\pipe\\"):]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", name):
        raise TraderError("E_SCHEMA", "Invalid local pipe instance name")
    return "\\\\.\\pipe\\" + name


def process_sid(pid: int) -> str:
    """Get an actual process token SID; the caller supplies no asserted identity."""
    _require_windows()
    process = _k.OpenProcess(0x1000, False, pid)
    if not process:
        raise _error("E_AUTH", "Cannot inspect peer process")
    token = w.HANDLE()
    try:
        if not _a.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise _error("E_AUTH", "Cannot inspect peer token")
        needed = w.DWORD()
        _a.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise _error("E_AUTH", "Cannot inspect peer SID")
        buffer = ctypes.create_string_buffer(needed.value)
        if not _a.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise _error("E_AUTH", "Cannot inspect peer SID")
        sid = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents.User.Sid
        result = w.LPWSTR()
        if not _a.ConvertSidToStringSidW(sid, ctypes.byref(result)):
            raise _error("E_AUTH", "Cannot encode peer SID")
        try:
            return result.value
        finally:
            _k.LocalFree(ctypes.cast(result, w.HANDLE))
    finally:
        if token:
            _k.CloseHandle(token)
        _k.CloseHandle(process)


def current_sid() -> str:
    return process_sid(os.getpid())


def _peer(handle: int, server_side: bool) -> tuple[int, str]:
    pid = w.ULONG()
    func = _k.GetNamedPipeClientProcessId if server_side else _k.GetNamedPipeServerProcessId
    if not func(handle, ctypes.byref(pid)):
        raise _error("E_AUTH", "Cannot authenticate pipe peer")
    return pid.value, process_sid(pid.value)


def _await(handle: int, overlapped: _OVERLAPPED, deadline: float) -> int:
    try:
        milliseconds = _milliseconds(deadline)
    except TraderError:
        _k.CancelIoEx(handle, ctypes.byref(overlapped))
        _k.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(w.DWORD()), True)
        raise
    wait = _k.WaitForSingleObject(overlapped.hEvent, milliseconds)
    if wait == _TIMEOUT:
        _k.CancelIoEx(handle, ctypes.byref(overlapped))
        # OVERLAPPED and its buffer must remain alive until cancellation completes.
        _k.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(w.DWORD()), True)
        raise TraderError("E_TIMEOUT", "Local pipe deadline expired")
    if wait != 0:
        _k.CancelIoEx(handle, ctypes.byref(overlapped))
        _k.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(w.DWORD()), True)
        raise _error("E_PIPE_CLOSED", "Pipe wait failed")
    transferred = w.DWORD()
    if not _k.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(transferred), False):
        raise _error("E_PIPE_CLOSED", "Pipe operation did not complete")
    return transferred.value


def _operation(handle: int, operation: str, data: bytes | None, deadline: float) -> bytes | int:
    _milliseconds(deadline)
    event = _k.CreateEventW(None, True, False, None)
    if not event:
        raise _error()
    overlap = _OVERLAPPED(hEvent=event)
    transferred = w.DWORD()
    buffer = ctypes.create_string_buffer(65536) if data is None else ctypes.create_string_buffer(data)
    try:
        if operation == "read":
            success = _k.ReadFile(handle, buffer, 65536, ctypes.byref(transferred), ctypes.byref(overlap))
        else:
            success = _k.WriteFile(handle, buffer, len(data), ctypes.byref(transferred), ctypes.byref(overlap))
        if not success:
            if ctypes.get_last_error() != _IO_PENDING:
                raise _error("E_PIPE_CLOSED", "Pipe peer disconnected")
            transferred.value = _await(handle, overlap, deadline)
        if operation == "read":
            if not transferred.value:
                raise TraderError("E_PIPE_CLOSED", "Pipe peer disconnected")
            return buffer.raw[:transferred.value]
        return transferred.value
    finally:
        _k.CloseHandle(event)


class PipeConnection:
    def __init__(self, handle: int, *, server_side: bool, allowed_sid: str,
                 expected_peer_pid: int | None = None, validator=validate_message) -> None:
        self._handle = handle
        self._server_side = server_side
        self._closed = False
        self._close_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._receive_lock = threading.Lock()
        self._decoder = FrameDecoder()
        self._ready: deque[dict] = deque()
        self._validator = validator
        try:
            self.peer_pid, self.peer_sid = _peer(handle, server_side)
            allowed = {allowed_sid, "S-1-5-18"} if server_side else {allowed_sid}
            if self.peer_sid not in allowed or (expected_peer_pid is not None and self.peer_pid != expected_peer_pid):
                raise TraderError("E_AUTH", "Pipe peer identity does not match the registered instance")
        except Exception:
            self.close()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    def send(self, message: dict, *, timeout: float = 5.0) -> None:
        self._validator(message)
        frame = encode_frame(message)
        deadline = _deadline(timeout)
        with self._send_lock:
            if self._closed:
                raise TraderError("E_PIPE_CLOSED", "Pipe is closed")
            offset = 0
            try:
                while offset < len(frame):
                    count = _operation(self._handle, "write", frame[offset:], deadline)
                    if not count:
                        raise TraderError("E_PIPE_CLOSED", "Pipe write made no progress")
                    offset += count
            except TraderError:
                self.close()
                raise

    def recv(self, *, timeout: float = 5.0) -> dict:
        deadline = _deadline(timeout)
        with self._receive_lock:
            if self._closed:
                raise TraderError("E_PIPE_CLOSED", "Pipe is closed")
            while not self._ready:
                try:
                    chunk = _operation(self._handle, "read", None, deadline)
                    self._ready.extend(self._decoder.feed(chunk))
                except TraderError as exc:
                    # An idle timeout can be polled; a partial frame must not survive its deadline.
                    if exc.code != "E_TIMEOUT" or self._decoder.pending_bytes:
                        self.close()
                    raise
            message = self._ready.popleft()
            try:
                self._validator(message)
            except TraderError:
                self.close()
                raise
            return message

    def close(self) -> None:
        with self._close_lock:
            if not self._closed:
                self._closed = True
                _k.CancelIoEx(self._handle, None)
                _k.CloseHandle(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class PipeListener:
    """One accept at a time, with a reserved server instance between accepts."""

    def __init__(self, name: str, *, validator=validate_message) -> None:
        _require_windows()
        self.name = _pipe_path(name)
        self.owner_sid = current_sid()
        self.security_sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{self.owner_sid})"
        self._closed = False
        self._accept_lock = threading.Lock()
        self._validator = validator
        self._handle = self._create(first=True)

    def _create(self, *, first: bool) -> int:
        descriptor = w.LPVOID()
        if not _a.ConvertStringSecurityDescriptorToSecurityDescriptorW(self.security_sddl, 1, ctypes.byref(descriptor), None):
            raise _error("E_AUTH", "Cannot create restricted pipe descriptor")
        attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False)
        try:
            # OVERLAPPED | FIRST_PIPE_INSTANCE; BYTE | REJECT_REMOTE_CLIENTS.
            flags = 0x00000003 | 0x40000000 | (0x00080000 if first else 0)
            handle = _k.CreateNamedPipeW(self.name, flags, 0x00000008, 32, 65536, 65536, 5000, ctypes.byref(attributes))
            if handle == _INVALID:
                raise _error("E_PIPE_IN_USE", "Cannot create exclusive local pipe")
            return handle
        finally:
            _k.LocalFree(descriptor)

    def accept(self, *, timeout: float = 5.0) -> PipeConnection:
        deadline = _deadline(timeout)
        with self._accept_lock:
            if self._closed:
                raise TraderError("E_PIPE_CLOSED", "Listener is closed")
            handle = self._handle
            event = _k.CreateEventW(None, True, False, None)
            if not event:
                raise _error()
            overlap = _OVERLAPPED(hEvent=event)
            try:
                while not _k.ConnectNamedPipe(handle, ctypes.byref(overlap)):
                    error = ctypes.get_last_error()
                    if error == _IO_PENDING:
                        _await(handle, overlap, deadline)
                        break
                    if error == _PIPE_CONNECTED:
                        break
                    if error != _NO_DATA:
                        raise _error("E_PIPE_CLOSED", "Cannot accept pipe client")
                    # A client can open and close the reserved instance before
                    # the next accept. Windows requires a disconnect before reuse.
                    if not _k.DisconnectNamedPipe(handle):
                        raise _error("E_PIPE_CLOSED", "Cannot release the previous pipe client")
                    _milliseconds(deadline)
                connection = PipeConnection(handle, server_side=True, allowed_sid=self.owner_sid,
                                            validator=self._validator)
                try:
                    self._handle = self._create(first=False)
                except Exception:
                    connection.close()
                    self._closed = True
                    raise
                return connection
            except TraderError as exc:
                if exc.code == "E_AUTH":
                    # A rejected connected instance has been closed; reserve another name instance.
                    self._handle = self._create(first=False)
                raise
            finally:
                _k.CloseHandle(event)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _k.CancelIoEx(self._handle, None)
            _k.CloseHandle(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def connect(name: str, *, timeout: float = 5.0, expected_server_pid: int | None = None,
            expected_server_sid: str | None = None, validator=validate_message) -> PipeConnection:
    _require_windows()
    path = _pipe_path(name)
    deadline = _deadline(timeout)
    while True:
        handle = _k.CreateFileW(path, 0xC0000000, 0, None, 3, 0x40000000, None)
        if handle != _INVALID:
            return PipeConnection(handle, server_side=False, allowed_sid=expected_server_sid or current_sid(),
                                  expected_peer_pid=expected_server_pid, validator=validator)
        error = ctypes.get_last_error()
        if error == 231:
            if not _k.WaitNamedPipeW(path, _milliseconds(deadline)):
                if ctypes.get_last_error() == 121:
                    raise TraderError("E_TIMEOUT", "Pipe connection deadline expired")
                raise _error("E_PIPE_CLOSED", "Cannot wait for local pipe")
        elif error in {2, 3}:
            _milliseconds(deadline)
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        else:
            raise _error("E_AUTH" if error == 5 else "E_PIPE", "Cannot open local pipe")


class CancellablePipeClient:
    """Own at most one client connection and cancel its blocking I/O on close."""

    def __init__(self) -> None:
        self._client_lock = threading.Lock()
        self._client_connection = None
        self._client_closed = False

    @contextmanager
    def open_connection(self, name: str, *, timeout: float, validator):
        with self._client_lock:
            if self._client_closed:
                raise TraderError("E_PIPE_CLOSED", "Pipe client is closed")
        connection = connect(name, timeout=timeout, validator=validator)
        with self._client_lock:
            if self._client_closed:
                connection.close()
                raise TraderError("E_PIPE_CLOSED", "Pipe client is closed")
            if self._client_connection is not None:
                connection.close()
                raise TraderError("E_BUSY", "Pipe client already has an active request")
            self._client_connection = connection
        try:
            yield connection
        finally:
            with self._client_lock:
                if self._client_connection is connection:
                    self._client_connection = None
            connection.close()

    def close(self) -> None:
        with self._client_lock:
            self._client_closed = True
            connection = self._client_connection
        if connection is not None:
            connection.close()
