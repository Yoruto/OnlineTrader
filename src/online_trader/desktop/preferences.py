"""Storage for workbench preferences."""

from pathlib import Path
import sys

from PySide6.QtCore import QSettings, QStandardPaths


def workbench_preferences():
    if getattr(sys, 'frozen', False):
        from online_trader.update_install import managed_root
        root = managed_root(sys.executable)
        if root is not None:
            return QSettings(str(root / 'OnlineTrader.ini'), QSettings.Format.IniFormat)
        path = Path(sys.executable).with_name('OnlineTrader.ini')
        return QSettings(str(path), QSettings.Format.IniFormat)
    return QSettings('OnlineTrader', 'Workbench')


def workbench_pending_lock_path():
    """Return one cross-process lock path for the shared pending registry."""
    if getattr(sys, 'frozen', False):
        from online_trader.update_install import managed_root
        root = managed_root(sys.executable)
        directory = root if root is not None else Path(sys.executable).parent
    else:
        directory = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
    if not str(directory):
        raise OSError('Workbench local data directory is unavailable')
    directory.mkdir(parents=True, exist_ok=True)
    return directory / 'runtime-pending-receipts.lock'
