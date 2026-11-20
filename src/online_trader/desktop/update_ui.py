"""Managed update controls shared by Legacy and Standalone Runtime workbenches."""

from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock
from online_trader.update_install import active_version, pending_candidate, quiescent_installation
from online_trader.updates import UpdateError, atomic_json, check_and_download


def offline_smoke():
    from online_trader.desktop.workbench import WorkbenchWindow

    app = QApplication([])
    window = WorkbenchWindow(None)
    window.resize(640, 480)
    window.winId()
    app.processEvents()
    return 0


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
        with (self.root / 'update-source.json').open('rb') as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise UpdateError('更新来源配置过大')
        value = json.loads(raw.decode('utf-8-sig'))
        channel = value.get('channel') if isinstance(value, dict) else None
        if channel not in {'preview', 'stable'}:
            raise UpdateError('更新渠道无效')
        return channel

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
            try:
                cache = self.root / 'updates'
                cache.mkdir(exist_ok=True)
                atomic_json(cache / 'status.json', {'schema_version': 'ONLINE_TRADER_UPDATE_STATUS/1',
                    **self.state})
            except OSError:
                pass

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
            '只有 Runtime 已安全退出、账户无持仓和未决委托时才会安装。',
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
