"""Storage for workbench preferences."""

from pathlib import Path
import sys

from PySide6.QtCore import QSettings


def workbench_preferences():
    if getattr(sys, 'frozen', False):
        from online_trader.update_install import managed_root
        root = managed_root(sys.executable)
        if root is not None:
            return QSettings(str(root / 'OnlineTrader.ini'), QSettings.Format.IniFormat)
        path = Path(sys.executable).with_name('OnlineTrader.ini')
        return QSettings(str(path), QSettings.Format.IniFormat)
    return QSettings('OnlineTrader', 'Workbench')
