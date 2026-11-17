"""Locate the already installed desktop execution environment, without installing.

The source workbench and the optional SDK live in separate environments. Source
launches use the same reviewed checkout through its existing isolated runner;
the stale package installed in the SDK environment is never selected instead.
"""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

import online_trader
from online_trader import __version__
from online_trader.errors import TraderError
from online_trader.runtime.executable import bundled_cli


@dataclass(frozen=True)
class DesktopRuntime:
    executable: Path
    source_runner: Path | None = None
    package_file: Path | None = None
    frozen: bool = False

    @property
    def kind(self):
        return 'bundled' if self.frozen else 'source' if self.source_runner is not None else 'installed'

    def command(self, *arguments):
        prefix = [str(self.executable)]
        if not self.frozen:
            prefix.extend(['-I', '-X', 'utf8'])
            prefix.extend([str(self.source_runner)] if self.source_runner is not None else ['-m', 'online_trader'])
        return [*prefix, *(str(value) for value in arguments)]

    def probe(self):
        """Verify exactly the source/package the child will use; never imports SDK."""
        if not self.executable.is_file():
            raise TraderError('E_DEPENDENCY', '模拟连接组件不存在，请检查应用安装目录。')
        if self.frozen:
            arguments = [str(self.executable), '--verify-runtime', __version__]
        else:
            statements = ['import sys, struct, runpy, pathlib, importlib.metadata as metadata']
            if self.source_runner is not None:
                if not self.source_runner.is_file():
                    raise TraderError('E_DEPENDENCY', '源码启动入口不存在，请检查项目目录。')
                # run_name intentionally differs from __main__: imports only,
                # with the runner's real sys.path selection and no CLI action.
                statements.append(f"runpy.run_path({str(self.source_runner)!r}, run_name='online_trader_desktop_probe')")
            statements.extend([
                'import online_trader',
                "assert sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8",
                f'assert online_trader.__version__ == {__version__!r}',
                "assert metadata.version('tqsdk') == '3.10.4'",
            ])
            if self.package_file is not None:
                statements.append(f'assert pathlib.Path(online_trader.__file__).resolve() == pathlib.Path({str(self.package_file)!r}).resolve()')
            arguments = [str(self.executable), '-I', '-X', 'utf8', '-c', ';'.join(statements)]
        try:
            result = subprocess.run(arguments, capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise TraderError('E_DEPENDENCY', '模拟连接组件检查未完成，请稍后重试。') from None
        if result.returncode:
            # Do not echo a child interpreter's arbitrary stderr into the UI.
            raise TraderError('E_DEPENDENCY', '模拟连接组件与当前程序版本不匹配，请检查应用环境。')
        return self


def resolve_environment():
    """Choose an existing interpreter/CLI using only this product's paths."""
    packaged = bundled_cli()
    if packaged is not None:
        runtime = DesktopRuntime(Path(packaged).resolve(), frozen=True)
    else:
        package = Path(online_trader.__file__).resolve()
        root = package.parents[2]
        runner = root / 'scripts' / 'run_online_trader.py'
        if package.parent.parent.name == 'src' and (root / 'pyproject.toml').is_file():
            interpreter = root / 'output' / 'online' / 'sdk-venv' / 'Scripts' / 'python.exe'
            runtime = DesktopRuntime(interpreter, runner, package)
        else:
            interpreter = Path(sys.executable).resolve()
            if interpreter.name.lower() == 'pythonw.exe':
                interpreter = interpreter.with_name('python.exe')
            runtime = DesktopRuntime(interpreter, package_file=package)
    if not runtime.executable.is_file() or (runtime.source_runner is not None and not runtime.source_runner.is_file()):
        raise TraderError('E_DEPENDENCY', '模拟连接组件未就绪，请检查当前应用环境。')
    return runtime



def fetch_offset_inputs(store, account_id, profile, formula, as_of_date):
    """Run the read-only market request in the installed SDK environment."""
    from online_trader.configuration.files import export_profile
    from online_trader.marketdata import bounded_json

    runtime = resolve_environment().probe()
    with TemporaryDirectory(prefix='onlinetrader-offset-') as directory:
        request, response = Path(directory)/'profile.json', Path(directory)/'inputs.json'
        export_profile(profile, request)
        command = runtime.command('data', 'offset-inputs', '--store', store.directory, '--account-id',
                                  account_id, '--profile', request, '--output', response, '--formula', formula)
        if as_of_date:
            command.extend(('--as-of-date', as_of_date))
        try:
            result = subprocess.run(command, capture_output=True, timeout=90,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise TraderError('E_OFFSET_INPUTS', '联网取数未完成。') from None
        if result.returncode:
            code = re.findall(rb'^([A-Z][A-Z0-9_]{1,49}): ', result.stderr, re.MULTILINE)
            if code and code[-1] == b'E_OFFSET_QUOTE_TIME' and response.is_file():
                quality = bounded_json(response)
                if quality.get('status') == 'QUOTE_TIME_REJECTED':
                    return quality
            raise TraderError(code[-1].decode() if code else 'E_OFFSET_INPUTS', '联网取数未完成。')
        return bounded_json(response)

def launch_bootstrap(request_path, output, *, runtime=None):
    """Start one explicitly confirmed desktop request in its existing SDK.

    The request contains account references and business parameters, never a
    credential value. The bootstrap itself owns creation of the new output
    directory, so startup logs live beside it and cannot be overwritten.
    """
    request = Path(request_path).resolve()
    directory = Path(output).resolve()
    if not request.is_file() or directory.exists():
        raise TraderError('E_STARTUP', '本次启动记录无效或运行目录已存在，请重新发起启动。')
    selected = (runtime or resolve_environment()).probe()
    arguments = selected.command('desktop-start', '--request', request, '--output', directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    log_path = directory.with_name(directory.name + '.executor.log')
    try:
        with log_path.open('xb') as stream:
            child = subprocess.Popen(arguments, cwd=directory.parent, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        raise TraderError('E_STARTUP', '无法启动模拟连接进程，请重新发起启动。') from None
    return {'status': 'PROCESS_LAUNCHED_NOT_CONNECTED', 'pid': child.pid,
            'output': str(directory), 'log_path': str(log_path), 'runtime_kind': selected.kind,
            'broker_connected': False}


def process_running(pid):
    """Read a child PID's process state; unavailable evidence remains unknown.

    This never signals or terminates a process. A vanished PID is a proved
    exit, whereas access denied or a failed state query cannot prove an exit.
    """
    if type(pid) is not int or pid <= 0 or sys.platform != 'win32':
        return None
    try:
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False if ctypes.get_last_error() == 87 else None  # ERROR_INVALID_PARAMETER: no such PID
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            api.CloseHandle(handle)
    except (AttributeError, OSError):
        return None
