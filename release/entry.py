"""Entry shared by the two Windows bootloaders in the portable distribution."""

from importlib.metadata import PackageNotFoundError, version
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
    if Path(sys.executable).stem.casefold() == 'onlinetradercli':
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(encoding='utf-8')
        raise SystemExit(console_main(sys.argv[1:]))
    from online_trader.desktop.entrypoint import main
    raise SystemExit(main())
