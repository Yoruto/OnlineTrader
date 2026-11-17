"""Entry shared by the two Windows bootloaders in the portable distribution."""

from importlib.metadata import PackageNotFoundError, version
from contextlib import nullcontext
from pathlib import Path
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
    from online_trader.update_install import managed_bootstrap, managed_root, runtime_lease
    from online_trader.updates import UpdateError

    arguments = sys.argv[1:]
    root = managed_root(sys.executable)
    if Path(sys.executable).stem.casefold() == 'onlinetradercli':
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(encoding='utf-8')
        if arguments[:1] == ['--managed-launch']:
            raise SystemExit(managed_bootstrap(arguments[1:]))
        try:
            with runtime_lease(root) if root else nullcontext():
                raise SystemExit(console_main(arguments))
        except UpdateError as error:
            print(error, file=sys.stderr)
            raise SystemExit(2)
    from online_trader.desktop.entrypoint import main
    if arguments == ['--update-smoke', __version__] and root is None:
        from online_trader.desktop.update_ui import offline_smoke
        raise SystemExit(offline_smoke())
    try:
        with runtime_lease(root) if root else nullcontext():
            raise SystemExit(main())
    except UpdateError:
        raise SystemExit(2)
