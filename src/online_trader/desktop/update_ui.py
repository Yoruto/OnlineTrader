"""Managed update controls shared by Legacy and Standalone Runtime workbenches."""

from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QProgressDialog, QPushButton, QVBoxLayout,
)

from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock
from online_trader.update_install import (active_version, pending_candidate, quiescent_installation,
                                          startup_check_and_download, write_update_status)
from online_trader.updates import UpdateError, check_and_download, load_update_source


def offline_smoke():
    from online_trader.desktop.workbench import WorkbenchWindow

    app = QApplication([])
    window = WorkbenchWindow(None)
    window.resize(640, 480)
    window.winId()
    app.processEvents()
    return 0


def run_startup_update_check(root, current):
    """Check and download updates before launching the Runtime/Workbench."""
    root = Path(root)
    if pending_candidate(root, current) is not None or not (root / 'update-source.json').is_file():
        return None
    app = QApplication.instance() or QApplication([])
    dialog = QProgressDialog('正在检查软件更新…', '跳过本次', 0, 0)
    dialog.setWindowTitle('OnlineTrader 自动更新')
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.setMinimumWidth(420)
    dialog.show()

    updates = SimpleQueue()
    finished = SimpleQueue()
    cancelled = Event()
    loop = QEventLoop(dialog)

    def on_progress(stage, version, downloaded, total):
        if cancelled.is_set():
            raise UpdateError('已跳过本次启动更新')
        updates.put((stage, version, downloaded, total))

    def worker():
        try:
            result = startup_check_and_download(root, current, on_progress=on_progress)
        except Exception:
            result = None
        finished.put(result)

    def poll():
        while True:
            try:
                stage, version, downloaded, total = updates.get_nowait()
            except Empty:
                break
            if stage == 'CHECKING':
                dialog.setRange(0, 0)
                dialog.setLabelText('正在检查软件更新…')
            elif stage == 'DOWNLOADING':
                if total > 0:
                    dialog.setRange(0, 1000)
                    dialog.setValue(min(1000, int(downloaded * 1000 / total)))
                    mb = 1024 * 1024
                    dialog.setLabelText(
                        f'正在下载新版本 {version}（{downloaded / mb:.1f} / {total / mb:.1f} MB）…'
                    )
                else:
                    dialog.setRange(0, 0)
                    dialog.setLabelText(f'正在下载新版本 {version}…')
            elif stage == 'VERIFYING':
                dialog.setRange(0, 0)
                dialog.setLabelText(f'正在校验并准备自动安装 {version}…')
        if cancelled.is_set():
            loop.quit()
            return
        try:
            finished.get_nowait()
        except Empty:
            return
        loop.quit()

    def on_cancel():
        cancelled.set()
        loop.quit()

    timer = QTimer(dialog)
    timer.setInterval(50)
    timer.timeout.connect(poll)
    dialog.canceled.connect(on_cancel)
    Thread(target=worker, name='OnlineTraderStartupUpdate', daemon=True).start()
    timer.start()
    loop.exec()
    timer.stop()
    dialog.close()
    app.processEvents()
    return None


class ManagedUpdateController(QObject):
    """Download in the UI process; leave all installation authority to the launcher."""

    changed = Signal(object)

    def __init__(self, window, root, request_restart):
        super().__init__(window)
        self.window = window
        self.root = Path(root).resolve()
        self.request_restart = request_restart
        self.results = SimpleQueue()
        self.running = False
        self.state = {}
        self.deferred_notice = None
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._poll)
        self._refresh_local_state()
        QTimer.singleShot(0, self.start_on_open)

    def _source(self):
        return load_update_source(self.root)['channel']

    def _candidate(self):
        return pending_candidate(self.root, active_version(self.root))

    def _refresh_local_state(self):
        try:
            current = active_version(self.root)
            channel = self._source()
            candidate = self._candidate()
            if candidate is None:
                self._set_state('READY', '启动后将自动检查更新', current=current, channel=channel)
            else:
                self._set_state('DOWNLOADED', f'{candidate.version} 已下载，等待安全安装',
                                current=current, channel=channel, candidate=candidate.version)
        except (OSError, ValueError, KeyError, TypeError, UpdateError):
            self._set_state('FAILED', '更新配置或本地状态无效')

    def _set_state(self, stage, message, *, current=None, channel=None, candidate=None, persist=False):
        current = current or self.state.get('current')
        channel = channel or self.state.get('channel')
        observed = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        self.state = {'stage': stage, 'message': message, 'current': current,
                      'channel': channel, 'candidate': candidate, 'observed_at_utc': observed}
        self.window.statusBar().showMessage(message, 10000)
        self.changed.emit(self.state.copy())
        if persist:
            status = write_update_status(
                self.root, stage, message,
                current=current, channel=channel, candidate=candidate, observed_at_utc=observed,
            )
            self.state = {key: value for key, value in status.items() if key != 'schema_version'}

    def _has_candidate(self):
        try:
            return self._candidate() is not None
        except (OSError, ValueError, KeyError, TypeError, UpdateError):
            return False

    def start_on_open(self):
        marker = os.environ.pop('ONLINETRADER_UPDATE_READY', '')
        if marker:
            try:
                target = Path(marker).resolve()
                if (target.name == 'ready' and target.parent.parent == self.root
                        and target.parent.name.startswith('.launch-')):
                    target.touch()
            except OSError:
                pass
        if os.environ.pop('ONLINETRADER_UPDATE_DEFERRED', ''):
            reason = os.environ.pop('ONLINETRADER_UPDATE_DEFERRED_REASON', '').strip()
            self.deferred_notice = reason or '上次安装条件不足，已继续使用当前版本'
            self._set_state('DEFERRED', self.deferred_notice, persist=True)
        self.check_now()

    def check_now(self):
        if self.running:
            return
        self.running = True
        checking = '正在后台检查并下载更新…'
        if self.deferred_notice:
            checking = f'{self.deferred_notice}；{checking}'
        self._set_state('CHECKING', checking, persist=True)
        self.timer.start()

        def download():
            try:
                with AccountLock('UPDATE-DOWNLOAD:' + str(self.root).casefold()):
                    version = check_and_download(self.root)
                if version:
                    result = ('DOWNLOADED', f'{version} 已下载，等待安全安装', version)
                else:
                    result = ('CURRENT', '当前已是所选渠道的最新版本', None)
            except UpdateError as error:
                result = ('FAILED', str(error), None)
            except (OSError, ValueError, KeyError, TypeError, TraderError, HTTPException):
                result = ('FAILED', '检查或下载失败，可以稍后重试', None)
            self.results.put(result)

        Thread(target=download, name='OnlineTraderUpdateDownload', daemon=True).start()

    def _poll(self):
        try:
            stage, message, candidate = self.results.get_nowait()
        except Empty:
            return
        self.timer.stop()
        self.running = False
        if self.deferred_notice:
            message = f'{message}；上次安装已暂缓：{self.deferred_notice}'
            self.deferred_notice = None
        self._set_state(stage, message, candidate=candidate, persist=True)

    def restart_now(self):
        candidate = self._candidate() if self._has_candidate() else None
        if candidate is None:
            return
        answer = QMessageBox.question(
            self.window, '安全更新并重启',
            f'安装 {candidate.version} 并重新打开工作台？\n\n'
            '只有 Trading Runtime 与账户进程已退出、未占用运行锁时才会安装。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with quiescent_installation(self.root):
                self.request_restart()
        except (UpdateError, OSError, ValueError, KeyError, TypeError, TraderError) as error:
            message = str(error) if isinstance(error, (UpdateError, TraderError)) else '无法核对更新前的账户状态'
            self._set_state('DEFERRED', message, candidate=candidate.version, persist=True)
            QMessageBox.information(self.window, '暂缓安装更新', message)

    def build_page(self, layout):
        version = QGroupBox('版本与更新渠道')
        version_layout = QVBoxLayout(version)
        self.version_label = QLabel()
        self.version_label.setWordWrap(True)
        version_layout.addWidget(self.version_label)
        layout.addWidget(version)

        actions = QGroupBox('软件更新')
        action_layout = QVBoxLayout(actions)
        self.page_status = QLabel()
        self.page_status.setWordWrap(True)
        action_layout.addWidget(self.page_status)
        buttons = QHBoxLayout()
        self.page_check = QPushButton('立即检查更新')
        self.page_restart = QPushButton('安全更新并重启')
        self.page_check.clicked.connect(lambda: self.check_now())
        self.page_restart.clicked.connect(lambda: self.restart_now())
        buttons.addWidget(self.page_check)
        buttons.addWidget(self.page_restart)
        buttons.addStretch(1)
        action_layout.addLayout(buttons)
        note = QLabel('启动工作台后会自动检查并下载更新。下载不会中断行情、订单或持仓保护；'
                      '安装前会再次核对 Runtime 已退出且账户责任已结清。')
        note.setWordWrap(True)
        action_layout.addWidget(note)
        layout.addWidget(actions)
        layout.addStretch(1)

        def render(state):
            channel = {'preview': '预览', 'stable': '稳定'}.get(state.get('channel'), '未知')
            self.version_label.setText(
                f"当前版本：{state.get('current') or '未知'}    更新渠道：{channel}"
                + (f"    待安装：{state['candidate']}" if state.get('candidate') else ''))
            self.page_status.setText(state.get('message') or '等待检查')
            self.page_check.setEnabled(not self.running)
            self.page_restart.setEnabled(not self.running and self._has_candidate())

        self.changed.connect(render)
        render(self.state)


def install_update_menu(window, root, request_restart):
    """Compatibility entry for callers that construct the update controller directly."""
    return ManagedUpdateController(window, root, request_restart)
