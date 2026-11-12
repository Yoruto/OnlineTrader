"""Select the console executable shipped beside the frozen workbench."""

from pathlib import Path
import sys


def bundled_cli():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).with_name('OnlineTraderCLI.exe')
    return None
