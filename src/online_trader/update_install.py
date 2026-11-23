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
from online_trader.configuration.files import publish_replace_bytes
from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock, RuntimeLock
from online_trader.updates import (Candidate, MAX_BUNDLE, PAYLOAD_MARKER, SHA256, UpdateError, atomic_json,
                                   extract_bundle, verify_payload_marker, version_key)


RESTART_FOR_UPDATE = 36


def assert_payload_managed_launch(executable, arguments=()):
    """Refuse standalone execution of an update payload outside a configured root."""
    exe = Path(executable).resolve()
    marker = exe.parent / PAYLOAD_MARKER
    if not marker.is_file():
        return
    try:
        metadata = verify_payload_marker(exe.parent)
    except UpdateError as error:
        raise TraderError('E_RUNTIME_CONFIG', str(error)) from None
    argv = list(arguments or ())
    if exe.stem.casefold() == 'onlinetradercli' and argv[:1] == ['--verify-runtime']:
        return
    if exe.stem.casefold() == 'onlinetrader' and argv[:1] == ['--update-smoke']:
        return
    root = managed_root(exe)
    expected = (root / 'versions' / metadata['version']).resolve() if root is not None else None
    if (root is None or exe.parent != expected
            or not (root / 'update-source.json').is_file()
            or not (root / 'runtime' / 'runtime.json').is_file()):
        raise TraderError(
            'E_RUNTIME_CONFIG',
            '此程序为受控更新载荷，不能单独启动；请从已安装的 OnlineTrader 配置根目录启动。',
        )


def managed_root(executable):
    directory = Path(executable).resolve().parent
    if directory.parent.name == 'versions':
        root = directory.parent.parent
        if (root / 'update-source.json').is_file():
            return root
    if (directory / 'update-source.json').is_file() and (directory / 'OnlineTrader.exe').is_file():
        return directory
    return None


def program_directory(root, version):
    version_key(version)
    if (root / 'OnlineTrader.exe').is_file():
        source = json.loads((root / 'update-source.json').read_text(encoding='utf-8-sig'))
        if source.get('portable_version') == version:
            return root
    return root / 'versions' / version


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


def _managed_runtime_store(root, current):
    """Return the stable portable Runtime store without relocating trading state."""
    from online_trader.runtime.portable_config import PortableAccountStore

    stable = (Path(root).resolve() / 'runtime').resolve()
    if (stable / 'runtime.json').is_file():
        return PortableAccountStore(stable)
    adjacent = (program_directory(root, current) / 'runtime').resolve()
    if adjacent != stable and (adjacent / 'runtime.json').is_file():
        raise UpdateError('Runtime 配置仍位于版本目录，暂不自动更新；请先完成受控配置迁移')
    return None


def _account_stores(root):
    stores = [AccountStore(Path(os.environ['LOCALAPPDATA']) / 'OnlineTrader' / 'accounts')]
    if root is not None:
        runtime_store = _managed_runtime_store(root, active_version(root))
        if runtime_store is not None:
            stores.append(runtime_store)
    return stores


@contextmanager
def quiescent_installation(root=None):
    """Hold process and account identities without consulting historical ledgers."""
    try:
        with ExitStack() as locks:
            if root is not None:
                locks.enter_context(RuntimeLock())
            locked = set()
            for store in _account_stores(root):
                locks.enter_context(AccountLock('CATALOGUE:' + str(store.directory).casefold()))
                rows = store.read()['accounts']
                for row in rows:
                    if row['account_key'] not in locked:
                        locks.enter_context(AccountLock(row['account_key']))
                        locked.add(row['account_key'])
            yield
    except TraderError as error:
        if error.code == 'E_INSTANCE_EXISTS':
            raise UpdateError('Trading Runtime 仍在运行；请先停止策略并等待账户安全退出') from None
        raise UpdateError(str(error)) from None


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


def pending_candidate(root, current):
    cache = root / 'updates'
    pending = cache / 'pending.json'
    if not pending.exists():
        return None
    candidate = Candidate(**json.loads(pending.read_text(encoding='utf-8')))
    if (version_key(candidate.version) <= version_key(current)
            or (cache / f'{candidate.version}.failed').exists()):
        return None
    if (type(candidate.size) is not int or not 0 < candidate.size <= MAX_BUNDLE
            or not isinstance(candidate.sha256, str) or not SHA256.fullmatch(candidate.sha256)):
        raise UpdateError('待安装更新信息无效')
    return candidate


def apply_pending(root, current):
    candidate = pending_candidate(root, current)
    if candidate is None:
        return current
    cache = root / 'updates'
    archive = cache / f'{candidate.version}-{candidate.sha256}.zip'
    runtime_store = None
    defaults_path = None
    defaults_before = None
    defaults_replaced = False
    with runtime_lease(root, exclusive=True), quiescent_installation(root):
        try:
            with archive.open('rb') as stream:
                if (archive.stat().st_size != candidate.size
                        or hashlib.file_digest(stream, 'sha256').hexdigest() != candidate.sha256):
                    raise UpdateError('安装前的更新文件校验失败')
            versions = root / 'versions'
            versions.mkdir(exist_ok=True)
            target = versions / candidate.version
            with tempfile.TemporaryDirectory(dir=versions, prefix='.staging-') as scratch:
                staged = Path(scratch) / 'bundle'
                staged.mkdir()
                extract_bundle(archive, staged, expected_version=candidate.version)
                _offline_check(staged, candidate.version)
                if target.exists():
                    if target.is_symlink() or target.is_junction() or _tree_hashes(target) != _tree_hashes(staged):
                        raise UpdateError('目标版本目录已有不同制品')
                else:
                    staged.rename(target)
            runtime_store = _managed_runtime_store(root, current)
            candidate_defaults = next(
                (path for path in (target / 'runtime' / 'OnlineTrader-parameters.json',
                                   target / 'OnlineTrader-parameters.json')
                 if path.is_file()),
                None,
            )
            if runtime_store is not None and candidate_defaults is not None:
                runtime_store.profile_with_defaults(candidate_defaults)
                defaults_path = runtime_store.directory / 'OnlineTrader-parameters.json'
                defaults_before = defaults_path.read_bytes()
                publish_replace_bytes(candidate_defaults.read_bytes(), defaults_path)
                defaults_replaced = True
            atomic_json(root / 'active.json', {'version': candidate.version, 'previous': current})
        except (UpdateError, TraderError, OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile):
            if defaults_replaced:
                try:
                    publish_replace_bytes(defaults_before, defaults_path)
                except TraderError:
                    raise UpdateError('更新失败，且无法恢复上一版 Runtime 默认配置') from None
            (cache / f'{candidate.version}.failed').touch()
            raise
    return candidate.version


@contextmanager
def launcher_lock(root, *, wait=False):
    deadline = time.monotonic() + (15 if wait else 0)
    with ExitStack() as locks:
        while True:
            try:
                locks.enter_context(AccountLock('UPDATE-LAUNCHER:' + str(root).casefold()))
            except TraderError as error:
                if error.code != 'E_INSTANCE_EXISTS' or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
            else:
                break
        yield


def launch_managed(root, arguments, *, wait_for_previous=False, portable_version=None):
    if arguments not in ([], ['--autostart']):
        return 2
    with launcher_lock(root, wait=wait_for_previous):
        if portable_version is not None:
            if program_directory(root, portable_version) != root:
                raise UpdateError('便携版程序与更新配置版本不符')
            if not (root / 'active.json').exists():
                atomic_json(root / 'active.json', {'version': portable_version})
        current = active_version(root)
        deferred = False
        deferred_reason = ''
        try:
            selected = apply_pending(root, current)
        except (UpdateError, TraderError, OSError, ValueError, KeyError, TypeError,
                subprocess.SubprocessError, zipfile.BadZipFile) as error:
            selected = current
            deferred = True
            if isinstance(error, (UpdateError, TraderError)):
                deferred_reason = str(error).strip()[:512]
            else:
                deferred_reason = '安装前检查或版本切换失败，已继续使用当前版本'
        state = json.loads((root / 'active.json').read_text(encoding='utf-8-sig'))
        previous = state.get('previous')
        executable = program_directory(root, selected) / 'OnlineTrader.exe'
        # Only early startup failures before the workbench becomes usable trigger rollback.
        with tempfile.TemporaryDirectory(dir=root, prefix='.launch-') as session:
            ready = Path(session) / 'ready'
            environment = dict(os.environ, ONLINETRADER_UPDATE_READY=str(ready))
            if deferred:
                environment['ONLINETRADER_UPDATE_DEFERRED'] = '1'
                environment['ONLINETRADER_UPDATE_DEFERRED_REASON'] = deferred_reason
            # A frozen bootstrap is a separate PyInstaller application.
            environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            try:
                with subprocess.Popen([str(executable), *arguments], cwd=executable.parent,
                                      env=environment) as process:
                    if previous:
                        deadline = time.monotonic() + 120
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
                fallback = program_directory(root, previous) / 'OnlineTrader.exe'
                return subprocess.call([str(fallback), *arguments], cwd=fallback.parent,
                                       env=environment)
            return result


def managed_bootstrap(arguments, *, restarting=False):
    directory = Path(sys.executable).resolve().parent
    root = managed_root(sys.executable) if restarting else directory.parent
    if root is None or (not restarting and (directory.name != 'bootstrap'
                                            or not (root / 'update-source.json').is_file())):
        return 2
    try:
        return launch_managed(root, arguments, wait_for_previous=restarting)
    except TraderError as error:
        if error.code == 'E_INSTANCE_EXISTS':
            return 0
        print(error)
        return 2
    except (OSError, ValueError, KeyError, TypeError, UpdateError) as error:
        print(error)
        return 2
