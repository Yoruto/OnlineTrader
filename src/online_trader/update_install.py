"""Managed installation launcher and serialized version switch."""

from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

from online_trader.accounts import AccountStore
from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock
from online_trader.updates import (Candidate, MAX_BUNDLE, SHA256, UpdateError, atomic_json,
                                   extract_bundle, version_key)


def managed_root(executable):
    directory = Path(executable).resolve().parent
    if directory.parent.name == 'versions':
        root = directory.parent.parent
        if (root / 'update-source.json').is_file():
            return root
    return None


@contextmanager
def runtime_lease(root, *, exclusive=False):
    """Windows denies an exclusive open as long as any managed process is using this installation."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(root / 'runtime.lock'), 0x80000000, 0 if exclusive else 1,
                                None, 4, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise UpdateError('程序仍在运行或更新锁不可用')
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def settled_accounts():
    store = AccountStore(Path(os.environ['LOCALAPPDATA']) / 'OnlineTrader' / 'accounts')
    with ExitStack() as locks:
        locks.enter_context(AccountLock('CATALOGUE:' + str(store.directory).casefold()))
        rows = store.read()['accounts']
        known = {store.ledger_path(row).resolve() for row in rows}
        data = store.directory / 'data'
        for name in ('ledger.db', 'ledger.db-wal', 'ledger.db-shm', 'ledger.db-journal'):
            for path in data.rglob(name):
                ledger = path.with_name('ledger.db').resolve()
                if ledger not in known or not ledger.is_file():
                    raise UpdateError('发现未归属或缺少主文件的账本，暂缓安装')
        for row in rows:
            locks.enter_context(AccountLock(row['account_key']))
            store._settled(row, for_update=True)
        yield


def active_version(root):
    version = json.loads((root / 'active.json').read_text(encoding='utf-8-sig'))['version']
    version_key(version)
    return version


def _offline_check(directory, version):
    environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    result = subprocess.run([str(directory / 'OnlineTraderCLI.exe'), '--verify-runtime', version],
                            cwd=directory, env=environment, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise UpdateError('新版内置运行检查失败')
    result = subprocess.run([str(directory / 'OnlineTrader.exe'), '--update-smoke', version],
                            cwd=directory, env=environment, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise UpdateError('新版工作台启动检查失败')


def _tree_hashes(directory):
    files = {}
    for path in directory.rglob('*'):
        if path.is_symlink() or path.is_junction():
            raise UpdateError('版本目录包含链接')
        if path.is_file():
            with path.open('rb') as stream:
                files[path.relative_to(directory)] = hashlib.file_digest(stream, 'sha256').digest()
    return files


def apply_pending(root, current):
    cache = root / 'updates'
    pending = cache / 'pending.json'
    if not pending.exists():
        return current
    candidate = Candidate(**json.loads(pending.read_text(encoding='utf-8')))
    if (version_key(candidate.version) <= version_key(current)
            or (cache / f'{candidate.version}.failed').exists()):
        return current
    if (type(candidate.size) is not int or not 0 < candidate.size <= MAX_BUNDLE
            or not isinstance(candidate.sha256, str) or not SHA256.fullmatch(candidate.sha256)):
        raise UpdateError('待安装更新信息无效')
    archive = cache / f'{candidate.version}-{candidate.sha256}.zip'
    with runtime_lease(root, exclusive=True), settled_accounts():
        try:
            with archive.open('rb') as stream:
                if (archive.stat().st_size != candidate.size
                        or hashlib.file_digest(stream, 'sha256').hexdigest() != candidate.sha256):
                    raise UpdateError('安装前的更新文件校验失败')
            versions = root / 'versions'
            target = versions / candidate.version
            with tempfile.TemporaryDirectory(dir=versions, prefix='.staging-') as scratch:
                staged = Path(scratch) / 'bundle'
                staged.mkdir()
                extract_bundle(archive, staged)
                _offline_check(staged, candidate.version)
                if target.exists():
                    if target.is_symlink() or target.is_junction() or _tree_hashes(target) != _tree_hashes(staged):
                        raise UpdateError('目标版本目录已有不同制品')
                else:
                    staged.rename(target)
            atomic_json(root / 'active.json', {'version': candidate.version, 'previous': current})
        except (UpdateError, OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile):
            (cache / f'{candidate.version}.failed').touch()
            raise
    return candidate.version


def launch_managed(root, arguments):
    if arguments not in ([], ['--autostart']):
        return 2
    with AccountLock('UPDATE-LAUNCHER:' + str(root).casefold()):
        current = active_version(root)
        deferred = False
        try:
            selected = apply_pending(root, current)
        except (UpdateError, TraderError, OSError, ValueError, KeyError, TypeError,
                subprocess.SubprocessError, zipfile.BadZipFile):
            selected = current
            deferred = True
        state = json.loads((root / 'active.json').read_text(encoding='utf-8-sig'))
        previous = state.get('previous')
        executable = root / 'versions' / selected / 'OnlineTrader.exe'
        # Only early startup failures before the workbench becomes usable trigger rollback.
        with tempfile.TemporaryDirectory(dir=root, prefix='.launch-') as session:
            ready = Path(session) / 'ready'
            environment = dict(os.environ, ONLINETRADER_UPDATE_READY=str(ready))
            if deferred:
                environment['ONLINETRADER_UPDATE_DEFERRED'] = '1'
            # A frozen bootstrap is a separate PyInstaller application.
            environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            try:
                with subprocess.Popen([str(executable), *arguments], cwd=executable.parent,
                                      env=environment) as process:
                    if previous:
                        deadline = time.monotonic() + 60
                        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.2)
                        if ready.exists():
                            atomic_json(root / 'active.json', {'version': selected})
                            previous = None
                        elif process.poll() is None:
                            # This isolated workbench has not accepted user operations yet.
                            process.terminate()
                    result = process.wait()
            except OSError:
                result = 2
            if not ready.exists() and previous:
                version_key(previous)
                with runtime_lease(root, exclusive=True):
                    atomic_json(root / 'active.json', {'version': previous})
                    (root / 'updates' / f'{selected}.failed').touch()
                fallback = root / 'versions' / previous / 'OnlineTrader.exe'
                environment.pop('ONLINETRADER_UPDATE_READY')
                return subprocess.call([str(fallback), *arguments], cwd=fallback.parent,
                                       env=environment)
            return result


def managed_bootstrap(arguments):
    directory = Path(sys.executable).resolve().parent
    if directory.name != 'bootstrap' or not (directory.parent / 'update-source.json').is_file():
        return 2
    try:
        return launch_managed(directory.parent, arguments)
    except TraderError as error:
        if error.code == 'E_INSTANCE_EXISTS':
            return 0
        print(error)
        return 2
    except (OSError, ValueError, KeyError, TypeError, UpdateError) as error:
        print(error)
        return 2
