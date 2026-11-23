"""Controlled full-process shutdown helpers for the Standalone Runtime Workbench."""

from __future__ import annotations

import ctypes
import os
import sys
import time
from uuid import uuid4

from online_trader.errors import TraderError
from online_trader.runtime.instance import runtime_owner_exists
from online_trader.runtime.instance_registry import read_runtime_instance
from online_trader.runtime.operator_api import RuntimeWorkbenchClient
from online_trader.transport.runtime_view import RuntimeViewClient


_WAIT_OBJECT_0 = 0x00000000
_STILL_ACTIVE = 259
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def pid_alive(pid: int | None) -> bool:
    """Return True if the specified process ID is still active on this host."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == 'win32':
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == _STILL_ACTIVE
            return kernel32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_pid_exit(pid: int | None, timeout: float = 15.0) -> bool:
    """Wait up to ``timeout`` seconds for ``pid`` to terminate."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    if not pid_alive(pid):
        return True
    if sys.platform == 'win32':
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return not pid_alive(pid)
        try:
            wait_ms = max(0, int(timeout * 1000))
            if kernel32.WaitForSingleObject(handle, wait_ms) == _WAIT_OBJECT_0:
                return True
            return not pid_alive(pid)
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def matching_runtime_instance(pipe_name: str | None) -> dict | None:
    """Return the active discovery hint matching ``pipe_name`` if present."""
    record = read_runtime_instance()
    if record is None:
        return None
    if pipe_name is not None and record.get('pipe') != pipe_name:
        return None
    return record


def inspect_runtime_for_exit(pipe_name: str) -> tuple[dict | None, int | None, str | None]:
    """Inspect whether the Runtime attached to ``pipe_name`` is still alive and its state."""
    record = matching_runtime_instance(pipe_name)
    pid = record['pid'] if record is not None else None
    if pid is not None and not pid_alive(pid):
        pid = None
    try:
        response = RuntimeViewClient(pipe_name).read()
    except (OSError, TraderError):
        if pid is not None and pid_alive(pid):
            return None, pid, '无法读取后台交易服务状态，进程仍在运行。'
        return None, None, None
    kind = response.get('kind')
    if kind == 'UNAVAILABLE' or not isinstance(response.get('body'), dict):
        if pid is not None and pid_alive(pid):
            return None, pid, '后台交易服务状态不可用，进程仍在运行。'
        return None, None, None
    view = response['body'].get('view')
    if not isinstance(view, dict):
        if pid is not None and pid_alive(pid):
            return None, pid, '后台交易服务返回了无效状态快照，进程仍在运行。'
        return None, None, None
    return view, pid, None


def shutdown_runtime_and_wait(
    pipe_name: str,
    *,
    pid: int | None = None,
    view: dict | None = None,
    timeout: float = 20.0,
) -> tuple[bool, str | None]:
    """Request FLATTEN_THEN_STOP for the bound Runtime and verify Owner + View exit."""
    record = matching_runtime_instance(pipe_name)
    if pid is None and record is not None:
        pid = record.get('pid')
    if view is None:
        view, detected_pid, error = inspect_runtime_for_exit(pipe_name)
        if pid is None:
            pid = detected_pid
        if view is None:
            if pid is None:
                return True, None
            return False, error or f'后台交易服务（PID {pid}）仍在运行。'

    config = view.get('configuration') or {}
    policies = [row['policy_id'] for row in view.get('policies', ()) if row.get('policy_kind') == 'EXECUTION']
    if len(policies) != 1:
        return False, '缺少唯一的执行政策，无法执行安全停机。'
    binding = {
        'account_key': view['account_key'],
        'kernel_boot_id': view['kernel_boot_id'],
        'config_revision': str(config['config_revision']),
    }
    try:
        RuntimeWorkbenchClient(pipe_name).request('submit', {
            'command_id': 'exit-' + uuid4().hex,
            'operation': 'FLATTEN_THEN_STOP',
            'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
            'parameters': {'execution_policy_id': policies[0]},
            'binding': binding,
        })
    except (OSError, TraderError) as error:
        if (pid is None or not pid_alive(pid)) and not runtime_owner_exists():
            return True, None
        code = getattr(error, 'code', 'E_RUNTIME_STOP')
        return False, f'安全停机请求被拒绝或超时：{error}（{code}）'

    deadline = time.monotonic() + max(1.0, timeout)
    if pid is not None:
        remaining = max(0.5, deadline - time.monotonic())
        if not wait_pid_exit(pid, timeout=remaining):
            return False, f'后台交易服务（PID {pid}）在 {int(timeout)} 秒内仍未退出（可能仍在等待平仓或撤单确认）。'

    while time.monotonic() < deadline:
        live = matching_runtime_instance(pipe_name)
        live_pid = live.get('pid') if live is not None else None
        if (live is None or not pid_alive(live_pid)) and not runtime_owner_exists():
            return True, None
        try:
            response = RuntimeViewClient(pipe_name).read()
            if response.get('kind') == 'UNAVAILABLE' and not runtime_owner_exists():
                return True, None
        except (OSError, TraderError):
            if not runtime_owner_exists():
                return True, None
        time.sleep(0.15)

    if runtime_owner_exists():
        return False, '后台交易服务或视图进程仍在运行，请确认所有持仓与撤单已结清。'
    return True, None
