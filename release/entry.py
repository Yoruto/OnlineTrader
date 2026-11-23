"""Entry shared by the two Windows bootloaders in the portable distribution."""

from importlib.metadata import PackageNotFoundError, version
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import sys

from online_trader import __version__


def console_main(argv):
    if argv[:1] == ['--verify-runtime']:
        try:
            return 0 if argv[1:] == [__version__] and version('tqsdk') == '3.10.4' else 2
        except PackageNotFoundError:
            return 2
    if argv[:1] == ['--strategy-worker']:
        from online_trader.strategy.worker import main
        return main(argv[1:])
    from online_trader.cli import main
    return main(argv)


if __name__ == '__main__':
    from online_trader.errors import TraderError
    from online_trader.update_install import (RESTART_FOR_UPDATE, active_version, assert_payload_managed_launch,
                                               launch_managed, managed_bootstrap, managed_root,
                                               program_directory, runtime_lease)
    from online_trader.updates import UpdateError

    arguments = sys.argv[1:]
    root = managed_root(sys.executable)
    executable = Path(sys.executable).resolve()
    portable_entry = root == executable.parent
    environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    if executable.stem.casefold() == 'onlinetradercli':
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(encoding='utf-8')
        if arguments[:1] == ['--managed-launch']:
            raise SystemExit(managed_bootstrap(arguments[1:]))
        if arguments == ['--restart-update']:
            raise SystemExit(managed_bootstrap([], restarting=True))
        if arguments[:1] == ['--verify-runtime']:
            raise SystemExit(console_main(arguments))
        try:
            assert_payload_managed_launch(executable, arguments)
            with runtime_lease(root) if root else nullcontext():
                if portable_entry and (root / 'active.json').exists():
                    selected = program_directory(root, active_version(root))
                    if selected != root:
                        raise SystemExit(subprocess.call([str(selected / executable.name), *arguments],
                                                         env=environment))
                raise SystemExit(console_main(arguments))
        except (UpdateError, TraderError, OSError, ValueError, KeyError, TypeError) as error:
            print(error, file=sys.stderr)
            raise SystemExit(2)
    from online_trader.desktop.entrypoint import main
    if arguments == ['--update-smoke', __version__]:
        from online_trader.desktop.update_ui import offline_smoke
        raise SystemExit(offline_smoke())
    try:
        assert_payload_managed_launch(executable, arguments)
        ready = Path(os.environ.get('ONLINETRADER_UPDATE_READY', '')).resolve()
        is_child = ready.name == 'ready' and ready.parent.parent == root and ready.parent.name.startswith('.launch-')
        if portable_entry and not is_child and arguments in ([], ['--autostart']):
            try:
                raise SystemExit(launch_managed(root, arguments, portable_version=__version__))
            except TraderError as error:
                if error.code == 'E_INSTANCE_EXISTS':
                    raise SystemExit(0)
                raise
        with runtime_lease(root) if root else nullcontext():
            if portable_entry and not is_child and (root / 'active.json').exists():
                selected = program_directory(root, active_version(root))
                if selected != root:
                    raise SystemExit(subprocess.call([str(selected / executable.name), *arguments],
                                                     env=environment))
            result = main()
        if result == RESTART_FOR_UPDATE and root is not None:
            subprocess.Popen([str(executable.with_name('OnlineTraderCLI.exe')), '--restart-update'],
                             cwd=executable.parent, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
            result = 0
        raise SystemExit(result)
    except (UpdateError, TraderError, OSError, ValueError, KeyError, TypeError) as error:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, 'OnlineTrader 启动失败', str(error))
        raise SystemExit(2)
