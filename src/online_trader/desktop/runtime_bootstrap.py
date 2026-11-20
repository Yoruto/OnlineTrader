"""Attach the desktop to a Runtime, starting a disarmed owner when needed."""

import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from PySide6.QtWidgets import QApplication, QProgressDialog

from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError
from online_trader.runtime.executable import bundled_cli
from online_trader.runtime.portable_config import PortableAccountStore
from online_trader.transport.runtime_view import RuntimeViewClient


_CONNECTION_ERRORS = frozenset({'E_PIPE', 'E_PIPE_CLOSED', 'E_TIMEOUT'})
_PHASE_LABELS = {
    'STARTUP_CHECKING': '正在核对账号、账本与启动请求…',
    'STARTUP_READING_MARKET': '正在连接行情并核对交易日与合约…',
    'STARTUP_PREPARED': '正在连接模拟柜台并同步账户状态…',
    'STARTUP_RECOVERING': '正在恢复原持仓与保护责任，新增开仓保持关闭…',
}
_FAILURE_HINTS = {
    'E_RUNTIME_HANDOFF': '检测到同一账号已有另一份桌面执行账本。为避免形成两个交易账本，Runtime 已拒绝启动；请先完成受控的一致性交接。',
    'E_RUNTIME_CONFIG': 'Runtime 配置不完整或格式无效，请核对交付目录中的运行配置。',
    'E_STORAGE': '执行账本缺失、不可读或身份不完整，Runtime 未启动且没有重建账本。',
    'E_SCOPE': '账号、provider、账本或启动范围不一致，Runtime 已拒绝启动。',
    'E_INSTANCE_EXISTS': '该账号已有执行 Owner，但在等待期内没有取得它的工作台接口；请查看原 Runtime 状态，禁止重复启动第二个 Owner。',
    'E_GATEWAY_CONNECT': '模拟账号或行情连接失败，请核对网络与账号配置。',
}
_launched_processes = []


def bundled_runtime_root():
    """Return an adjacent configured Runtime for a frozen desktop, if present."""
    if not getattr(sys, 'frozen', False):
        return None
    directory = Path(sys.executable).resolve().parent
    try:
        from online_trader.update_install import managed_root
        root = managed_root(sys.executable)
        if root is not None and (root / 'OnlineTrader.exe').is_file():
            directory = root
    except (OSError, ValueError, KeyError, TypeError):
        pass
    candidate = directory / 'runtime'
    return candidate if (candidate / 'runtime.json').is_file() else None


def _cli_command():
    packaged = bundled_cli()
    if packaged is not None:
        if not packaged.is_file():
            raise TraderError('E_DEPENDENCY', 'OnlineTraderCLI.exe is missing beside the desktop application')
        return [str(packaged)]
    executable = Path(sys.executable).resolve()
    if executable.name.casefold() == 'pythonw.exe':
        console = executable.with_name('python.exe')
        if console.is_file():
            executable = console
    return [str(executable), '-X', 'utf8', '-m', 'online_trader']


def _probe(pipe, expected_account_key):
    try:
        value = RuntimeViewClient(pipe).read()
    except (OSError, TraderError) as error:
        code = getattr(error, 'code', 'E_PIPE')
        if code not in _CONNECTION_ERRORS:
            raise
        return None
    if value.get('kind') == 'UNAVAILABLE':
        return None
    try:
        account_key = value['body']['view']['account_key']
    except (KeyError, TypeError):
        raise TraderError('E_RUNTIME_VIEW', 'Runtime did not provide an attributable account baseline') from None
    if value.get('kind') != 'BASELINE' or account_key != expected_account_key:
        raise TraderError('E_SCOPE', 'Runtime baseline belongs to a different account')
    return value


def _read_small_json(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            return None
        value = strict_loads(raw.decode('utf-8-sig'))
        return value if type(value) is dict else None
    except (OSError, TraderError, ValueError, UnicodeError):
        return None


def _read_first_json(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.readline(65_537)
        if len(raw) > 65_536:
            return None
        value = strict_loads(raw.decode('utf-8-sig'))
        return value if type(value) is dict else None
    except (OSError, TraderError, ValueError, UnicodeError):
        return None


def _run_directory(runtime_root, stdout_path):
    value = _read_first_json(stdout_path)
    if value is None:
        return None
    raw = value.get('run')
    if type(raw) is not str:
        return None
    path = Path(raw).resolve()
    starts = (runtime_root / 'desktop-starts').resolve()
    return path if path.is_relative_to(starts) else None


def _startup_record(run_directory):
    if run_directory is None:
        return None, None
    failure = run_directory / 'startup-failure.json'
    if failure.is_file():
        return _read_small_json(failure), failure
    phases = list(run_directory.glob('phase-*.json'))
    latest = max(phases, key=lambda item: item.stat().st_mtime_ns, default=None)
    return (_read_small_json(latest), latest) if latest is not None else (None, None)


def _launch_owner(runtime_root, mode, account=None):
    command = [*_cli_command(), 'runtime', 'run', '--root', str(runtime_root), '--mode', mode]
    if account is not None:
        command.extend(('--account', account))
    logs = runtime_root / 'logs' / 'desktop-bootstrap'
    stdout = None
    stderr = None
    try:
        logs.mkdir(parents=True, exist_ok=True)
        launch_id = 'desktop-' + uuid4().hex
        stdout_path = logs / (launch_id + '.stdout.log')
        stderr_path = logs / (launch_id + '.stderr.log')
        stdout = stdout_path.open('xb')
        stderr = stderr_path.open('xb')
    except OSError:
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()
        raise TraderError('E_STORAGE', 'Cannot create the Runtime desktop bootstrap log') from None
    environment = os.environ.copy()
    if getattr(sys, 'frozen', False):
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    try:
        process = subprocess.Popen(
            command, cwd=runtime_root, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            env=environment, close_fds=True, creationflags=flags,
        )
    except OSError:
        raise TraderError('E_DEPENDENCY', 'Cannot launch the configured Trading Runtime') from None
    finally:
        stdout.close()
        stderr.close()
    _launched_processes.append(process)
    return process, stdout_path, stderr_path


def _failure_error(record, diagnostic, stderr_path):
    code = record.get('error_code', 'E_RUNTIME_START_EXITED') if record else 'E_RUNTIME_START_EXITED'
    message = _FAILURE_HINTS.get(code) or (record.get('message') if record else None)
    if not message:
        message = 'Trading Runtime 启动进程已退出，但没有建立可用的工作台接口。'
    evidence = diagnostic or stderr_path
    return TraderError(code, f'{message}\n\n错误码：{code}\n诊断：{evidence}')


def ensure_runtime(root, *, account=None, mode='strategy', timeout=90):
    """Return a ready Runtime pipe, starting one without binding it to Qt lifetime."""
    runtime_root = Path(root).resolve()
    store = PortableAccountStore(runtime_root, account)
    store.profile()
    store.runtime_control()
    selected = store.get(store.read()['selected_id'])
    pipe = store.pipe_name(selected)

    progress = QProgressDialog('', '', 0, 0)
    progress.setWindowTitle('OnlineTrader · 启动交易服务')
    progress.setCancelButton(None)
    progress.setMinimumDuration(0)
    progress.setLabelText(
        f"正在读取交易服务权威状态…\n{selected['alias']} · {selected['masked_login']}\n"
        "已有 Runtime 保留当前权限；新启动保持禁开"
    )
    progress.show()
    QApplication.processEvents()
    try:
        if _probe(pipe, selected['account_key']) is not None:
            return pipe

        process, stdout_path, stderr_path = _launch_owner(runtime_root, mode, account)
        deadline = time.monotonic() + timeout
        run_directory = None
        exited_at = None
        instance_failure = None
        last_phase = None
        while time.monotonic() < deadline:
            QApplication.processEvents()
            value = _probe(pipe, selected['account_key'])
            if value is not None:
                return pipe
            run_directory = run_directory or _run_directory(runtime_root, stdout_path)
            record, diagnostic = _startup_record(run_directory)
            if record is not None:
                phase = record.get('phase') or record.get('status')
                if phase == 'STARTUP_FAILED':
                    if record.get('error_code') == 'E_INSTANCE_EXISTS':
                        instance_failure = (record, diagnostic)
                        progress.setLabelText(
                            f"该账号已有执行 Owner，正在等待它的工作台接口…\n"
                            f"{selected['alias']} · {selected['masked_login']}\n不会启动第二个 Owner"
                        )
                        time.sleep(.2)
                        continue
                    raise _failure_error(record, diagnostic, stderr_path)
                if phase != last_phase and phase in _PHASE_LABELS:
                    last_phase = phase
                    progress.setLabelText(
                        f"{_PHASE_LABELS[phase]}\n{selected['alias']} · {selected['masked_login']}\n新增开仓保持关闭"
                    )
            exit_code = process.poll()
            if exit_code is not None:
                exited_at = exited_at or time.monotonic()
                # A concurrent launcher may have won the account lock. Its
                # structured E_INSTANCE_EXISTS record is handled above by
                # waiting for that original Runtime until the main deadline.
                if time.monotonic() - exited_at >= 3 and instance_failure is None:
                    record, diagnostic = _startup_record(run_directory)
                    raise _failure_error(record, diagnostic, stderr_path)
            time.sleep(.2)
        if instance_failure is not None:
            raise _failure_error(instance_failure[0], instance_failure[1], stderr_path)
        raise TraderError(
            'E_RUNTIME_START_TIMEOUT',
            '等待 Trading Runtime 就绪超时。启动进程可能仍在继续；为避免形成第二个 Owner，未自动重启或终止它。'
            f'\n\n错误码：E_RUNTIME_START_TIMEOUT\n诊断：{stderr_path}',
        )
    finally:
        progress.close()
