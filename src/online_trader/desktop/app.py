"""Minimal status console; slow IPC stays in a worker and window close is local."""

from __future__ import annotations

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow, QPushButton,
                              QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from online_trader.desktop.client import StatusClient
from online_trader.errors import TraderError


class _Request(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, pipe_name, action, factory):
        super().__init__()
        self.pipe_name, self.action, self.factory = pipe_name, action, factory

    def run(self):
        try:
            client = self.factory(self.pipe_name)
            value = client.snapshot() if self.action == "snapshot" else client.pause()
            if not self.isInterruptionRequested():
                self.result.emit({"action": self.action, "value": value,
                                  "metadata": getattr(client, "last_metadata", None)})
        except TraderError as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(exc.code)
        except Exception:
            if not self.isInterruptionRequested():
                self.failed.emit("E_INTERNAL")


class StatusWindow(QMainWindow):
    def __init__(self, pipe_name: str, *, client_factory=StatusClient):
        super().__init__()
        self.pipe_name, self._factory = pipe_name, client_factory
        self._worker = None
        self._closing = False
        self._stale = True
        self.setWindowTitle("OnlineTrader · G1 状态控制台")
        self.resize(960, 520)
        central = QWidget(self); layout = QVBoxLayout(central); self.setCentralWidget(central)
        self.connection_label = QLabel("未刷新 · 当前状态未知")
        self.account_label = QLabel("账户：未知")
        self.kernel_label = QLabel("内核：未知 · 新仓权限：未知")
        self.funds_label = QLabel("权益：未知 · 可用：未知 · 保证金：未知 · 费用：未知")
        self.notice_label = QLabel("G1 工程控制台；关闭窗口不停止内核或删除持仓保护。")
        self.notice_label.setWordWrap(True)
        for widget in [self.connection_label, self.account_label, self.kernel_label, self.funds_label, self.notice_label]:
            layout.addWidget(widget)
        self.cycles = QTableWidget(0, 7)
        self.cycles.setHorizontalHeaderLabels(["合约", "方向", "周期", "剩余手数", "退出目标", "保护", "数据/持久状态"])
        self.cycles.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cycles.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.cycles)
        controls = QHBoxLayout()
        self.refresh_button = QPushButton("刷新状态")
        self.pause_button = QPushButton("暂停新开仓")
        self.settings_button = QPushButton("DSV参数")
        self.reports_button = QPushButton("盈亏报表")
        self._settings_dialog = None
        self._reports_window = None
        self._account_key = None
        controls.addWidget(self.refresh_button); controls.addWidget(self.pause_button)
        controls.addWidget(self.settings_button); controls.addWidget(self.reports_button); controls.addStretch()
        layout.addLayout(controls)
        self.refresh_button.clicked.connect(self.refresh)
        self.pause_button.clicked.connect(self.pause_entries)
        self.settings_button.clicked.connect(self.open_settings)
        self.reports_button.clicked.connect(self.open_reports)

    def open_reports(self):
        from online_trader.desktop.reports import ReportsWindow
        if self._reports_window is None:
            self._reports_window = ReportsWindow(account_key=self._account_key, pipe_name=self.pipe_name)
        self._reports_window.show()
        self._reports_window.raise_()
        self._reports_window.activateWindow()

    def open_settings(self):
        from online_trader.desktop.settings import SettingsDialog
        if self._settings_dialog is None:
            try:
                self._settings_dialog = SettingsDialog(self)
            except TraderError as error:
                self.notice_label.setText(f"参数基准未通过校验，保留状态控制台。错误代码：{error.code}")
                return
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _start(self, action: str):
        if self._worker is not None or self._closing:
            return
        self.refresh_button.setEnabled(False); self.pause_button.setEnabled(False)
        worker = _Request(self.pipe_name, action, self._factory)
        self._worker = worker
        worker.result.connect(self._result)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        worker.start()

    def refresh(self):
        self._start("snapshot")

    def pause_entries(self):
        self._start("pause")

    def _result(self, result: dict):
        if self._closing:
            return
        if result["action"] == "pause":
            receipt = result["value"]
            self._stale = True
            self.connection_label.setText("控制请求已返回；请刷新当前状态")
            stage = receipt["status"]
            self.notice_label.setText(f"暂停新开仓回执：{stage}。持仓保护继续；请求回执不代表成交。")
            return
        records = result["value"]["records"]
        account = next(record for record in records if record["record_type"] == "ACCOUNT")
        self._account_key = account['record_id']
        self._stale = False
        metadata = result["metadata"] or {}
        self.connection_label.setText(f"已取得完整状态快照 · {metadata.get('received_at_utc', '本次刷新')}")
        self.account_label.setText(f"账户：{account['record_id']} · 启动：{metadata.get('kernel_boot_id', '未知')}")
        self.kernel_label.setText(f"内核：{account['kernel_state']} · 新仓权限：{account['entry_authority']}")
        known = account["funds_quality"] != "UNKNOWN"
        def money(key):
            value = account[key] if known else None
            return "未知" if value is None else str(value) + ("（估算）" if account["funds_quality"] == "ESTIMATED" else "")
        self.funds_label.setText(f"权益：{money('equity')} · 可用：{money('available')} · 保证金：{money('margin')} · 费用：{money('fees')}")
        cycles = [record for record in records if record["record_type"] == "CYCLE"]
        self.cycles.setRowCount(len(cycles))
        for row, record in enumerate(cycles):
            values = [record["contract_id"], record["direction"], record["cycle_state"], str(record["remaining_quantity"]),
                      "尚无目标" if record["target_remaining_quantity"] is None else str(record["target_remaining_quantity"]),
                      record["protection_mode"], record["protection_quality"] + " / " + record["protection_persistence"]]
            for column, value in enumerate(values):
                self.cycles.setItem(row, column, QTableWidgetItem(value))
        self.notice_label.setText("状态来自内核快照。G1 未安装交易 SDK；未知资金不会显示为零。关闭窗口只关闭控制台。")

    def _failed(self, code: str):
        if self._closing:
            return
        self._stale = True
        self.connection_label.setText(f"连接/请求失败 · 显示已过期 · {code}")
        self.notice_label.setText("保留上次可见记录，不能据过期显示判断当前持仓或保护。可刷新重新取得完整快照。")

    def _finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        if self._closing:
            QTimer.singleShot(0, self.close)
        else:
            self.refresh_button.setEnabled(True); self.pause_button.setEnabled(True)

    def closeEvent(self, event):
        if self._worker is not None:
            self._closing = True
            self._worker.requestInterruption()
            self.hide()
            event.ignore()
            return
        event.accept()


def run(pipe_name: str) -> int:
    app = QApplication.instance() or QApplication([])
    window = StatusWindow(pipe_name)
    window.show()
    QTimer.singleShot(0, window.refresh)
    return app.exec()
