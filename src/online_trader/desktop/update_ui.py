"""Managed workbench update controls and offline startup probe."""

import os
from http.client import HTTPException
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock
from online_trader.update_install import active_version, pending_candidate, settled_accounts
from online_trader.updates import check_and_download, UpdateError


def offline_smoke():
    from online_trader.desktop.workbench import WorkbenchWindow

    app = QApplication([])
    window = WorkbenchWindow(None)
    window.resize(640, 480)
    window.winId()
    app.processEvents()
    return 0


def install_update_menu(window, root, request_restart):
    result = SimpleQueue()
    menu = window.menuBar().addMenu('软件更新')
    action = menu.addAction('检查更新')
    restart = menu.addAction('更新并重启')
    restart.setEnabled(False)
    status = menu.addAction('等待检查')
    status.setEnabled(False)
    timer = QTimer(window)
    timer.setInterval(250)

    def download():
        try:
            with AccountLock('UPDATE-DOWNLOAD:' + str(root).casefold()):
                version = check_and_download(root)
            message = f'{version} 已下载，可以更新并重启' if version else '当前已是所选渠道的最新版本'
        except UpdateError as error:
            message = str(error)
        except (OSError, ValueError, KeyError, TypeError, TraderError, HTTPException):
            message = '检查或下载失败，可以重试'
        result.put(message)

    def poll():
        try:
            message = result.get_nowait()
        except Empty:
            return
        timer.stop()
        status.setText(message)
        window.statusBar().showMessage(message, 10000)
        action.setEnabled(True)
        restart.setEnabled(available_update() is not None)

    def available_update():
        try:
            return pending_candidate(root, active_version(root))
        except (UpdateError, OSError, ValueError, KeyError, TypeError):
            return None

    def restart_now():
        candidate = available_update()
        if candidate is None:
            restart.setEnabled(False)
            return
        answer = QMessageBox.question(window, '更新并重启',
                                      f'安装 {candidate.version} 并重新打开工作台？',
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with settled_accounts():
                request_restart()
        except (UpdateError, OSError, ValueError, KeyError, TypeError, TraderError) as error:
            message = str(error) if isinstance(error, (UpdateError, TraderError)) else '无法核对更新前的账户状态'
            QMessageBox.information(window, '暂缓安装更新', message)

    def start():
        if timer.isActive():
            return
        action.setEnabled(False)
        restart.setEnabled(False)
        status.setText('正在检查或下载更新…')
        timer.start()
        Thread(target=download, daemon=True).start()

    def on_ready():
        marker = os.environ.pop('ONLINETRADER_UPDATE_READY', '')
        if marker:
            target = Path(marker)
            if target.name == 'ready' and target.parent.parent == root and target.parent.name.startswith('.launch-'):
                target.touch()
        window.setEnabled(True)
        if os.environ.pop('ONLINETRADER_UPDATE_DEFERRED', ''):
            window.statusBar().showMessage('暂缓安装更新，已启动当前版本', 10000)
        start()

    action.triggered.connect(start)
    restart.triggered.connect(restart_now)
    timer.timeout.connect(poll)
    window.setEnabled(False)
    QTimer.singleShot(0, window, on_ready)
