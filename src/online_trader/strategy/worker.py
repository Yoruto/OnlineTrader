"""Read-only G1 SHADOW process. It cannot submit a proposal or alter protection."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from online_trader import __version__
from online_trader.contracts.codec import content_hash
from online_trader.errors import TraderError
from online_trader.transport.status_client import StatusClient

_READ_ONLY_CAPABILITIES = frozenset({"READ_STATUS", "READ_STREAM", "READ_DIAGNOSTIC", "SHADOW_COMPARE"})


class ShadowStatusClient(StatusClient):
    def __init__(self, pipe_name: str, instance_id: str, **kwargs) -> None:
        super().__init__(pipe_name, **kwargs)
        if type(instance_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", instance_id):
            raise TraderError("E_SCHEMA", "Invalid registered worker identity")
        self.instance_id = instance_id

    def _hello(self, connection, deadline: float) -> dict:
        request = self._message("Hello", {
            "client_instance_id": self.instance_id, "requested_role": "SHADOW", "supported_versions": ["1.0"],
            "requested_account_key": None, "client_build_id": "online-trader-" + __version__,
        })
        connection.send(request, timeout=self._remaining(deadline))
        welcome = self._receive(connection, deadline, request=request, expected_type="Welcome")
        capabilities = set(welcome["body"]["capabilities"])
        if (any(welcome[key] is None for key in ("connection_id", "kernel_boot_id", "account_key")) or
                welcome["body"]["granted_role"] != "SHADOW" or "READ_STATUS" not in capabilities or
                not capabilities <= _READ_ONLY_CAPABILITIES):
            raise TraderError("E_AUTH", "Worker was not granted a read-only SHADOW session")
        return welcome

    def pause(self) -> dict:
        raise TraderError("E_AUTH", "SHADOW workers have no operator command capability")


class ParentWatch:
    """Hold the original Windows process object so PID reuse cannot revive a parent."""

    def __init__(self, parent_pid: int) -> None:
        if type(parent_pid) is not int or parent_pid <= 0:
            raise TraderError("E_SCHEMA", "Invalid parent process identity")
        self.parent_pid = parent_pid
        self._handle = None
        if os.name == "nt":
            self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self._kernel.OpenProcess.restype = wintypes.HANDLE
            self._kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self._kernel.WaitForSingleObject.restype = wintypes.DWORD
            self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self._kernel.CloseHandle.restype = wintypes.BOOL
            self._handle = self._kernel.OpenProcess(0x00100000, False, parent_pid)

    def is_alive(self) -> bool:
        if os.name == "nt":
            return bool(self._handle) and self._kernel.WaitForSingleObject(self._handle, 0) == 258
        return os.getppid() == self.parent_pid

    def wait(self, seconds: float) -> bool:
        if os.name == "nt":
            return bool(self._handle) and self._kernel.WaitForSingleObject(self._handle, max(1, int(seconds * 1000))) == 258
        time.sleep(seconds)
        return self.is_alive()

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _report(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n"); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_worker(pipe_name: str, instance_id: str, parent_pid: int, report_file: str | Path, *,
               interval_ms: int = 250, client_factory=ShadowStatusClient, watch_factory=ParentWatch,
               max_iterations: int | None = None) -> int:
    if type(interval_ms) is not int or not 100 <= interval_ms <= 10000:
        raise TraderError("E_SCHEMA", "Invalid worker observation interval")
    if max_iterations is not None and (type(max_iterations) is not int or max_iterations < 1):
        raise TraderError("E_SCHEMA", "Invalid development iteration limit")
    client = client_factory(pipe_name, instance_id, timeout=2.0, expected_server_pid=parent_pid)
    watch = watch_factory(parent_pid)
    failures = iterations = 0
    try:
        while watch.is_alive():
            try:
                snapshot = client.snapshot()
                if not watch.is_alive():
                    return 0
                metadata = client.last_metadata
                if metadata is None:
                    raise TraderError("E_SNAPSHOT", "Worker received no checked snapshot boundary")
                _report(Path(report_file), {
                    "pid": os.getpid(), "parent_pid": parent_pid,
                    "kernel_boot_id": metadata["kernel_boot_id"], "durable_seq": metadata["durable_seq"],
                    "snapshot_hash": content_hash(snapshot), "records_count": len(snapshot["records"]),
                })
                failures = 0
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    return 0
            except TraderError as exc:
                failures += 1
                print("SHADOW status unavailable: " + exc.code, file=sys.stderr, flush=True)
                # A bounded startup retry lets the parent register the newly returned PID.
                if failures >= 20:
                    return 2
            except OSError:
                print("SHADOW report unavailable: E_STORAGE", file=sys.stderr, flush=True)
                return 2
            if not watch.wait(interval_ms / 1000):
                break
        return 0
    finally:
        watch.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only OnlineTrader G1 SHADOW process")
    parser.add_argument("--pipe", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--report-file", required=True)
    args = parser.parse_args(argv)
    try:
        return run_worker(args.pipe, args.instance_id, args.parent_pid, args.report_file)
    except TraderError as exc:
        print("SHADOW refused: " + exc.code, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
