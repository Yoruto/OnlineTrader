"""Attach the desktop to a Runtime, starting a disarmed owner when needed."""

import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QProgressDialog

from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError
from online_trader.runtime.executable import bundled_cli
from online_trader.runtime.portable_config import PortableAccountStore
from online_trader.runtime.instance_registry import read_runtime_instance
from online_trader.transport.runtime_view import RuntimeViewClient


_CONNECTION_ERRORS = frozenset({'E_PIPE', 'E_PIPE_CLOSED', 'E_TIMEOUT'})
_PHASE_LABELS = {
    'STARTUP_CHECKING': '正在核对本机 Runtime、账号与启动请求…',
    'STARTUP_READING_MARKET': '正在连接行情并核对交易日与合约…',
    'STARTUP_PREPARED': '正在连接模拟柜台并同步账户状态…',
    'STARTUP_RECOVERING': '正在从柜台同步当前账户状态，新增开仓保持关闭…',
}
_FAILURE_HINTS = {
    'E_RUNTIME_CONFIG': 'Runtime 配置不完整或格式无效，请核对交付目录中的运行配置。',
    'E_STORAGE': '本次 Runtime 的临时存储或历史缓存不可用，请查看启动诊断。',
    'E_SCOPE': '账号、provider、配置或启动范围不一致，Runtime 已拒绝启动。',
    'E_INSTANCE_EXISTS': '这台电脑已有一个 Runtime，但在等待期内没有取得它的工作台接口；请查看已运行的 Runtime。其他电脑不受此限制。',
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
        if root is not None:
            stable = root / 'runtime'
            if (stable / 'runtime.json').is_file():
                return stable
            if (root / 'OnlineTrader.exe').is_file():
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


def _discover_runtime():
    """Return a verified host Runtime endpoint, never a registry claim alone."""
    record = read_runtime_instance()
    if record is None:
        return None
    return record if _probe(record['pipe'], record['account_key']) is not None else None


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
    command = [*_cli_command(), 'runtime', 'run', '--root', str(runtime_root), '--mode', mode,
               '--desktop-bootstrap']
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


class BootstrapWorker(QThread):
    """Perform Runtime discovery and bounded startup without blocking Qt."""

    phase_changed = Signal(str, str, str)
    ready = Signal(str)
    failed = Signal(object)
    cancelled = Signal()

    def __init__(self, root, *, account=None, mode='strategy', timeout=90,
                 attach_policy='attach-any', expected_account_key=None, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        self.account = account
        self.mode = mode
        self.timeout = timeout
        self.attach_policy = attach_policy
        self.expected_account_key = expected_account_key

    def _phase(self, state, message, selected=None):
        detail = '' if selected is None else f"{selected['alias']} · {selected['masked_login']}"
        self.phase_changed.emit(state, message, detail)

    def run(self):
        try:
            self._phase('DISCOVERING', '正在读取交易服务权威状态…')
            runtime_root = self.root.resolve()
            store = PortableAccountStore(runtime_root, self.account)
            store.profile()
            selected = store.get(store.read()['selected_id'])
            pipe = store.pipe_name(selected)
            if self.isInterruptionRequested():
                self.cancelled.emit()
                return

            self._phase('ATTACHING', '正在附着已有 Runtime；不会改变当前权限…', selected)
            expected_key = self.expected_account_key or selected['account_key']
            discovered = _discover_runtime()
            if discovered is not None:
                if self.attach_policy == 'attach-exact' and discovered.get('account_key') != expected_key:
                    raise TraderError(
                        'E_INSTANCE_EXISTS',
                        '当前本机仍有其他账号的 Runtime 正在运行，请先完成安全退出后再切换。',
                    )
                attached = {'alias': discovered['account_alias'],
                            'masked_login': discovered['masked_login']}
                self._phase('READY', '已取得本机唯一 Runtime 基线。', attached)
                self.ready.emit(discovered['pipe'])
                return
            if _probe(pipe, expected_key if self.attach_policy == 'attach-exact' else selected['account_key']) is not None:
                self._phase('READY', '已取得现有 Runtime 基线。', selected)
                self.ready.emit(pipe)
                return
            if self.isInterruptionRequested():
                self.cancelled.emit()
                return

            from online_trader.runtime.instance import runtime_owner_exists
            if runtime_owner_exists():
                if self.attach_policy == 'attach-exact':
                    raise TraderError(
                        'E_INSTANCE_EXISTS',
                        '原 Runtime 进程尚未完全释放运行锁，请稍后重试。',
                    )
                process = None
                stdout_path = None
                stderr_path = runtime_root / 'logs' / 'desktop-bootstrap'
                self._phase('WAITING_BASELINE', '本机 Runtime 正在启动，等待其工作台接口…', selected)
            else:
                self._phase('LAUNCHING', '未发现可附着 Runtime，正在启动 DISARMED Owner…', selected)
                process, stdout_path, stderr_path = _launch_owner(runtime_root, self.mode, self.account)
            deadline = time.monotonic() + self.timeout
            run_directory = None
            exited_at = None
            instance_failure = None
            last_phase = None
            self._phase('WAITING_BASELINE', '正在等待 Runtime 建立可归属的状态基线…', selected)
            while time.monotonic() < deadline:
                if self.isInterruptionRequested():
                    self._phase('CANCELLED_UI', '桌面已取消等待；已启动的 Owner 保持独立运行。', selected)
                    self.cancelled.emit()
                    return
                discovered = _discover_runtime()
                if discovered is not None and (
                    self.attach_policy != 'attach-exact' or discovered.get('account_key') == expected_key
                ):
                    attached = {'alias': discovered['account_alias'],
                                'masked_login': discovered['masked_login']}
                    self._phase('READY', 'Runtime 已就绪，正在打开唯一交易工作台。', attached)
                    self.ready.emit(discovered['pipe'])
                    return
                value = _probe(pipe, expected_key if self.attach_policy == 'attach-exact' else selected['account_key'])
                if value is not None:
                    self._phase('READY', 'Runtime 已就绪，正在打开交易工作台。', selected)
                    self.ready.emit(pipe)
                    return
                run_directory = run_directory or (_run_directory(runtime_root, stdout_path)
                                                  if stdout_path is not None else None)
                record, diagnostic = _startup_record(run_directory)
                if record is not None:
                    phase = record.get('phase') or record.get('status')
                    if phase == 'STARTUP_FAILED':
                        if record.get('error_code') == 'E_INSTANCE_EXISTS':
                            instance_failure = (record, diagnostic)
                            self._phase(
                                'WAITING_BASELINE',
                                '该账号已有执行 Owner，正在等待它的工作台接口；不会启动第二个 Owner…',
                                selected,
                            )
                            self.msleep(200)
                            continue
                        raise _failure_error(record, diagnostic, stderr_path)
                    if phase != last_phase and phase in _PHASE_LABELS:
                        last_phase = phase
                        self._phase('WAITING_BASELINE', _PHASE_LABELS[phase], selected)
                if process is not None and process.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    # A concurrent launcher may have won the account lock. Its
                    # E_INSTANCE_EXISTS record keeps this worker on attach-only
                    # waiting until the original deadline.
                    if time.monotonic() - exited_at >= 3 and instance_failure is None:
                        record, diagnostic = _startup_record(run_directory)
                        raise _failure_error(record, diagnostic, stderr_path)
                self.msleep(200)
            if instance_failure is not None:
                raise _failure_error(instance_failure[0], instance_failure[1], stderr_path)
            raise TraderError(
                'E_RUNTIME_START_TIMEOUT',
                '等待 Trading Runtime 就绪超时。启动进程可能仍在继续；为避免形成第二个 Owner，未自动重启或终止它。'
                f'\n\n错误码：E_RUNTIME_START_TIMEOUT\n诊断：{stderr_path}',
            )
        except Exception as error:
            failure = error if isinstance(error, TraderError) else TraderError(
                'E_RUNTIME_START', 'Trading Runtime 启动检查失败，请查看 bootstrap 日志')
            self.phase_changed.emit('FAILED', str(failure), '')
            self.failed.emit(failure)


class BootstrapController(QObject):
    """Own the bootstrap UI and worker, but never the Runtime Owner lifetime."""

    ready = Signal(str)
    failed = Signal(object)
    cancelled = Signal()
    state_changed = Signal(str)

    def __init__(self, root, *, account=None, mode='strategy', timeout=90,
                 attach_policy='attach-any', expected_account_key=None, parent=None):
        super().__init__(parent)
        self.progress = QProgressDialog('', '', 0, 0)
        self.progress.setWindowTitle('OnlineTrader · 启动交易服务')
        self.progress.setCancelButtonText('取消等待')
        self.progress.setMinimumDuration(0)
        self.progress.setAutoClose(False)
        self.progress.setAutoReset(False)
        self.worker = BootstrapWorker(
            root, account=account, mode=mode, timeout=timeout,
            attach_policy=attach_policy, expected_account_key=expected_account_key,
            parent=self)
        self._terminal = False
        self._cancel_requested = False
        self.worker.phase_changed.connect(self._phase_changed)
        self.worker.ready.connect(self._ready)
        self.worker.failed.connect(self._failed)
        self.worker.cancelled.connect(self._cancelled)
        self.progress.canceled.connect(self.cancel)

    def start(self):
        if self.worker.isRunning():
            return
        self._terminal = False
        self._cancel_requested = False
        self.progress.setCancelButtonText('取消等待')
        self.progress.setLabelText('正在读取交易服务权威状态…\n新启动保持禁开')
        self.progress.show()
        self.worker.start()

    def cancel(self):
        if self._terminal or self._cancel_requested:
            return
        self._cancel_requested = True
        self.progress.setCancelButton(None)
        self.progress.setLabelText(
            '正在取消桌面等待…\n已启动的 Runtime Owner 将保持独立运行')
        if self.worker.isRunning():
            self.worker.requestInterruption()
        else:
            self._cancelled()

    def shutdown(self):
        if self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.wait()
        self._terminal = True
        self.progress.close()

    def _phase_changed(self, state, message, detail):
        self.state_changed.emit(state)
        suffix = f'\n{detail}' if detail else ''
        safety = '' if state in {'READY', 'FAILED', 'CANCELLED_UI'} else '\n新增开仓保持关闭'
        self.progress.setLabelText(message + suffix + safety)

    def _ready(self, pipe):
        if self._terminal:
            return
        if self._cancel_requested:
            self._cancelled()
            return
        self._terminal = True
        self.progress.close()
        self.ready.emit(pipe)

    def _failed(self, error):
        if self._terminal:
            return
        self._terminal = True
        self.progress.close()
        self.failed.emit(error)

    def _cancelled(self):
        if self._terminal:
            return
        self._terminal = True
        self.progress.close()
        self.cancelled.emit()
