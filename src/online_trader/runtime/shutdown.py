"""Controlled full-process shutdown helpers for the Standalone Runtime Workbench."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
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


class _FileTime(ctypes.Structure):
    _fields_ = [('dwLowDateTime', ctypes.c_uint32), ('dwHighDateTime', ctypes.c_uint32)]


def _process_creation_time(pid: int | None) -> int | None:
    """Return the Windows process creation timestamp (100ns ticks) to guard against PID reuse."""
    if sys.platform != 'win32' or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel_t, user_t = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        if kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel_t), ctypes.byref(user_t),
        ):
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        return None
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int | None, *, expected_started_ns: int | None = None) -> bool:
    """Return True if the specified process ID is still active (and matches creation time if known)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == 'win32':
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            if expected_started_ns is not None:
                created, exited, kernel_t, user_t = _FileTime(), _FileTime(), _FileTime(), _FileTime()
                if kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel_t), ctypes.byref(user_t),
                ):
                    actual = (created.dwHighDateTime << 32) | created.dwLowDateTime
                    if actual != expected_started_ns:
                        return False
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


def wait_pid_exit(
    pid: int | None,
    timeout: float = 15.0,
    *,
    expected_started_ns: int | None = None,
) -> bool:
    """Wait up to ``timeout`` seconds for ``pid`` to terminate."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    if not pid_alive(pid, expected_started_ns=expected_started_ns):
        return True
    if sys.platform == 'win32':
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return not pid_alive(pid, expected_started_ns=expected_started_ns)
        try:
            if expected_started_ns is not None:
                actual = _process_creation_time(pid)
                if actual is not None and actual != expected_started_ns:
                    return True
            wait_ms = max(0, int(timeout * 1000))
            if kernel32.WaitForSingleObject(handle, wait_ms) == _WAIT_OBJECT_0:
                return True
            return not pid_alive(pid, expected_started_ns=expected_started_ns)
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


def _wait_runtime_exit(pipe_name: str, *, pid: int | None = None, timeout: float = 20.0) -> tuple[bool, str | None]:
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

    return _wait_runtime_exit(pipe_name, pid=pid, timeout=timeout)


def _read_run_json(run_dir: str | None, filename: str) -> dict | None:
    if not isinstance(run_dir, str) or not run_dir:
        return None
    path = Path(run_dir) / filename
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _read_maintenance_completion(run_dir: str | None) -> dict | None:
    return _read_run_json(run_dir, 'maintenance-completion.json')


def _is_valid_maintenance_completion(
    completion: dict | None,
    *,
    command_id: str,
    boot_id: str | None,
    account_key: str | None,
) -> bool:
    if not isinstance(completion, dict):
        return False
    if completion.get('schema_version') != 'DSV_MAINTENANCE_COMPLETION/1':
        return False
    if completion.get('command_id') != command_id:
        return False
    if boot_id and completion.get('runtime_boot_id') != boot_id:
        return False
    if account_key and completion.get('account_key') != account_key:
        return False
    if completion.get('state') != 'COMPLETED':
        return False
    responsibility = completion.get('responsibility')
    return isinstance(responsibility, dict) and responsibility.get('maintenance_allowed') is True


def _format_aborted_message(blockers) -> str:
    items = [str(item) for item in (blockers or ()) if item]
    suffix = f"（{'、'.join(items)}）" if items else ''
    return f'维护停机期间检测到晚到成交或状态变化，已中止停机并继续保持当前账户保护{suffix}。'


def _wait_maintenance_exit(
    pipe_name: str,
    *,
    pid: int | None,
    expected_started_ns: int | None,
    command_id: str,
    boot_id: str | None,
    run_dir: str | None,
    timeout: float,
) -> tuple[bool, str | None]:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if pid is not None and pid_alive(pid, expected_started_ns=expected_started_ns):
            wait_pid_exit(pid, timeout=min(0.15, max(0.05, deadline - time.monotonic())),
                          expected_started_ns=expected_started_ns)
        failure = _read_run_json(run_dir, 'runtime-failure.json')
        if failure is not None and (not boot_id or failure.get('runtime_boot_id') == boot_id):
            code = failure.get('error_code') or 'E_RUNTIME_FAILURE'
            return False, f'维护停机期间后台交易服务异常退出（{code}），未确认安全停机证明，已阻止继续切换或更新。'
        live = matching_runtime_instance(pipe_name)
        live_pid = live.get('pid') if live is not None else None
        pid_done = pid is None or not pid_alive(pid, expected_started_ns=expected_started_ns)
        live_done = live is None or not pid_alive(live_pid)
        if pid_done and live_done and not runtime_owner_exists():
            return True, None
        try:
            response = RuntimeViewClient(pipe_name).read()
            body = response.get('body') if isinstance(response, dict) else None
            view = body.get('view') if isinstance(body, dict) else None
            if isinstance(view, dict):
                comp = view.get('maintenance_completion')
                if (isinstance(comp, dict) and comp.get('command_id') == command_id
                        and comp.get('state') == 'ABORTED'):
                    return False, _format_aborted_message(comp.get('blockers'))
            if response.get('kind') == 'UNAVAILABLE' and pid_done and not runtime_owner_exists():
                return True, None
        except (OSError, TraderError):
            if pid_done and not runtime_owner_exists():
                return True, None
        time.sleep(0.05)

    completion = _read_maintenance_completion(run_dir)
    if completion and completion.get('command_id') == command_id and completion.get('state') == 'ABORTED':
        return False, _format_aborted_message((completion.get('responsibility') or {}).get('blockers'))
    if pid is not None and pid_alive(pid, expected_started_ns=expected_started_ns):
        return False, f'后台交易服务（PID {pid}）在 {int(timeout)} 秒内仍未退出，未确认安全停机。'
    if runtime_owner_exists():
        return False, '后台交易服务或视图进程仍在运行，未确认安全停机。'
    return False, '等待维护停机超时，未能确认安全停机完成。'


def maintenance_stop_runtime_and_wait(
    pipe_name: str,
    *,
    reason: str,
    pid: int | None = None,
    view: dict | None = None,
    timeout: float = 20.0,
) -> tuple[bool, str | None]:
    """Request non-flattening SAFE_STOP_FOR_MAINTENANCE and verify controlled exit."""
    if reason not in {'SWITCH_ACCOUNT', 'UPDATE', 'RESTART_CONFIG'}:
        return False, '维护停机原因无效。'
    record = matching_runtime_instance(pipe_name)
    if pid is None and record is not None:
        pid = record.get('pid')
    if view is None:
        view, detected_pid, error = inspect_runtime_for_exit(pipe_name)
        if pid is None:
            pid = detected_pid
        if view is None:
            if pid is None and not runtime_owner_exists() and matching_runtime_instance(pipe_name) is None:
                return True, None
            return False, error or '后台交易服务仍在运行或状态不可用，无法确认无责任安全停机。'

    config = view.get('configuration') or {}
    run_dir = config.get('run_directory')
    expected_boot_id = view.get('kernel_boot_id')
    expected_account_key = view.get('account_key')
    expected_started_ns = _process_creation_time(pid)
    command_id = 'maint-' + uuid4().hex
    binding = {
        'account_key': expected_account_key,
        'kernel_boot_id': expected_boot_id,
        'config_revision': str(config['config_revision']),
    }
    try:
        RuntimeWorkbenchClient(pipe_name).request('submit', {
            'command_id': command_id,
            'operation': 'SAFE_STOP_FOR_MAINTENANCE',
            'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
            'parameters': {'reason': reason},
            'binding': binding,
        })
    except (OSError, TraderError) as error:
        code = getattr(error, 'code', 'E_RUNTIME_MAINTENANCE')
        if code == 'E_NOT_READY':
            return False, f'无法进入维护停机：{error}（{code}）'
        exited, _ = _wait_runtime_exit(pipe_name, pid=pid, timeout=min(timeout, 5.0))
        completion = _read_maintenance_completion(run_dir)
        if exited and _is_valid_maintenance_completion(
            completion, command_id=command_id, boot_id=expected_boot_id, account_key=expected_account_key,
        ):
            return True, None
        return False, f'维护停机请求中断或未完成安全校验：{error}（{code}）'

    ok, wait_error = _wait_maintenance_exit(
        pipe_name,
        pid=pid,
        expected_started_ns=expected_started_ns,
        command_id=command_id,
        boot_id=expected_boot_id,
        run_dir=run_dir,
        timeout=timeout,
    )
    if not ok:
        return False, wait_error
    completion = _read_maintenance_completion(run_dir)
    if not _is_valid_maintenance_completion(
        completion, command_id=command_id, boot_id=expected_boot_id, account_key=expected_account_key,
    ):
        return False, '未能确认无责任受控停机证明（可能发生异常退出），已阻止继续切换或更新。'
    return True, None
