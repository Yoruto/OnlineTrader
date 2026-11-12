"""Read-only money reports from a consistent, account-scoped local ledger view."""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PySide6.QtCore import QPointF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                              QLineEdit, QMainWindow, QPushButton, QTableWidget,
                              QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from online_trader.desktop.client import StatusClient
from online_trader.errors import TraderError


_TOTALS = (("gross_realized_pnl", "已实现毛收益"), ("confirmed_fees", "确认费用小计"), ("fees", "费用合计"),
           ("estimated_fees", "估算费用"), ("net_realized_pnl", "已实现净收益"),
           ("unrealized_pnl", "未平浮盈亏"), ("entry_quantity", "开仓成交量"),
           ("exit_quantity", "平仓成交量"), ("fill_count", "成交笔数"))
_TABLES = (
    ("by_contract", "按品种", (("contract_id", "合约"),) + _TOTALS),
    ("by_day", "每日盈亏", (("trading_day", "交易日"),) + _TOTALS),
    ("cycles", "周期", (("position_cycle_id", "周期"), ("contract_id", "合约"),
       ("direction", "方向"), ("trading_day", "入场交易日"),
       ("remaining_quantity", "剩余量"), ("closed", "已平")) + _TOTALS),
    ("trades", "成交明细", (("trading_day", "交易日"), ("observed_at_utc", "观察时间 UTC"),
       ("contract_id", "合约"), ("direction", "方向"), ("effect", "开平"),
       ("quantity", "成交量"), ("price", "实际成交价"), ("realized_pnl", "FIFO毛收益"),
       ("fee", "手续费"), ("fee_quality", "费用质量"),
       ("directional_slippage_points", "方向化入场滑点"), ("trade_key", "成交身份"))),
    ("equity", "权益记录", (("trading_day", "交易日"), ("observed_at_utc", "观察时间 UTC"),
       ("complete", "资金来源完整"),
       ("balance", "权益"), ("pre_balance", "昨结权益"), ("available", "可用"),
       ("margin", "保证金"), ("commission", "账户手续费"), ("today_pnl", "DSV当日盈亏"),
       ("net_cash_flow", "净出入金"), ("trading_pnl", "已扣出入金盈亏"))),
    ("nominal", "名义风控", (("contract_id", "合约"), ("trading_day", "交易日"),
       ("daily_pnl_points", "名义收益点数"), ("net_loss_points", "净亏点数"),
       ("trade_count", "源计次"), ("stop_count", "源止损计次"),
       ("profit_count", "源止盈计次"), ("stop_streak", "连续止损"))),
)
_TRANSLATIONS = {"LONG": "多", "SHORT": "空", "OPEN": "开", "REDUCE": "平",
                 "CONFIRMED": "确认", "ESTIMATED": "估算", "UNKNOWN": "未知"}
_DISPLAY_LIMIT = 5000


def _display(value, *, estimated=False):
    if value is None:
        return "未知"
    if type(value) is bool:
        return "是" if value else "否"
    result = _TRANSLATIONS.get(str(value), str(value))
    return "≈" + result if estimated else result


class EquityChart(QWidget):
    """Small native chart: unknown observations break lines rather than become zero."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.heading = "账户权益记录（缺失点留空；非本地成交收益曲线）"
        self.setMinimumHeight(125)
        self.setMaximumHeight(170)

    def set_rows(self, rows):
        self.rows = copy.deepcopy(rows)
        self.update()

    def values(self):
        values = []
        for row in self.rows:
            try:
                value = Decimal(row["balance"]) if row.get("balance") is not None else None
                values.append(value if value is None or value.is_finite() else None)
            except (InvalidOperation, TypeError, ValueError):
                values.append(None)
        return values

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f6f8fb"))
        painter.setPen(QColor("#4a5568"))
        painter.drawText(12, 20, self.heading)
        values = self.values()
        known = [value for value in values if value is not None]
        if not known:
            painter.drawText(12, 54, "尚无可用权益记录")
            return
        low, high = min(known), max(known)
        spread = high-low or Decimal(1)
        left, top, width, height = 92, 33, max(self.width()-112, 1), max(self.height()-53, 1)
        painter.drawText(8, top+8, str(high))
        painter.drawText(8, top+height, str(low))
        painter.setPen(QPen(QColor("#d8dee8"), 1))
        painter.drawLine(left, top+height, left+width, top+height)
        previous = None
        painter.setPen(QPen(QColor("#2765ae"), 2))
        for index, value in enumerate(values):
            if value is None:
                previous = None
                continue
            x = left + (width*index/max(len(values)-1, 1))
            y = top + height*(1-float((value-low)/spread))
            point = QPointF(x, y)
            if previous is not None:
                painter.drawLine(previous, point)
            painter.drawEllipse(point, 2.5, 2.5)
            previous = point


class _ReportTask(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, *, action, reader=None, exporter=None, path=None, account_key=None,
                 pipe_name=None, report=None, destination=None, table=None, format=None):
        super().__init__()
        self.action, self.reader, self.exporter = action, reader, exporter
        self.path, self.account_key, self.pipe_name = path, account_key, pipe_name
        self.report, self.destination, self.table, self.format = report, destination, table, format

    def run(self):
        try:
            if self.action == "load":
                metadata = None
                account_key = self.account_key
                if self.pipe_name:
                    client = StatusClient(self.pipe_name)
                    projection = client.snapshot()
                    account = next(r for r in projection["records"] if r["record_type"] == "ACCOUNT")
                    if account_key and account["record_id"] != account_key:
                        raise TraderError("E_SCOPE", "Report account differs from the kernel account")
                    account_key = account["record_id"]
                    metadata = client.last_metadata
                    if (not isinstance(metadata, dict) or metadata.get("account_key") != account_key
                            or not metadata.get("durable_seq") or not metadata.get("received_at_utc")
                            or not metadata.get("kernel_boot_id")):
                        raise TraderError("E_FACT_BARRIER", "Report requires an accepted owner snapshot boundary")
                if self.reader is None:
                    from online_trader.reporting import read_report
                    reader = read_report
                else:
                    reader = self.reader
                options = {} if metadata is None else {
                    "checkpoint_event_seq": metadata["durable_seq"], "as_of_utc": metadata["received_at_utc"],
                    "expected_kernel_boot_id": metadata["kernel_boot_id"]}
                report = reader(self.path, account_key, **options)
                if (not isinstance(report, dict) or report.get("schema_version") != "MONEY_REPORT/1"
                        or report.get("account_key") != account_key
                        or not isinstance(report.get("summary"), dict)
                        or any(not isinstance(report.get(key), list) for key, _, _ in _TABLES)):
                    raise TraderError("E_REPORT", "Invalid money report result")
                if metadata and str(report.get("checkpoint_event_seq")) != str(metadata["durable_seq"]):
                    raise TraderError("E_FACT_BARRIER", "Report is outside the accepted owner snapshot")
                result = {"action": "load", "report": report, "metadata": metadata}
            elif self.format == "daily_xlsx":
                from online_trader.reporting import export_daily_report
                target = export_daily_report(self.report, self.destination, self.table)
                result = {"action":"export","destination":str(target)}
            else:
                if self.exporter is None:
                    from online_trader.reporting import export_report
                    exporter = export_report
                else:
                    exporter = self.exporter
                target = exporter(self.report, self.destination, table=self.table, format=self.format)
                result = {"action": "export", "destination": str(target)}
            if not self.isInterruptionRequested():
                self.result.emit(result)
        except TraderError as error:
            if not self.isInterruptionRequested():
                self.failed.emit(error.code)
        except Exception:
            if not self.isInterruptionRequested():
                self.failed.emit("E_INTERNAL")


class ReportsWindow(QMainWindow):
    def __init__(self, ledger_path=None, account_key=None, *, pipe_name=None, reader=None, exporter=None):
        super().__init__()
        self.pipe_name, self._reader, self._exporter = pipe_name, reader, exporter
        self._worker = None
        self._task_action = None
        self._daily_after_load = False
        self._closing = False
        self._stale = True
        self._report = None
        self.setWindowTitle("OnlineTrader · 资金盈亏与报表")
        self.resize(1240, 780)
        central = QWidget(self)
        layout = QVBoxLayout(central)
        self.setCentralWidget(central)
        inputs = QHBoxLayout()
        self.path_edit = QLineEdit("" if ledger_path is None else str(ledger_path))
        self.path_edit.setPlaceholderText("选择本产品的 ledger.db")
        self.choose_button = QPushButton("选择账本")
        self.account_edit = QLineEdit(account_key or "")
        self.account_edit.setPlaceholderText("账户档案标识，例如 DEV:console")
        self.account_edit.setMaximumWidth(290)
        self.refresh_button = QPushButton("刷新报表")
        inputs.addWidget(self.path_edit, 1)
        inputs.addWidget(self.choose_button)
        inputs.addWidget(self.account_edit)
        inputs.addWidget(self.refresh_button)
        layout.addLayout(inputs)
        self.status_label = QLabel("请选择账本并刷新 · 当前资金未知")
        self.account_label = QLabel("账户快照：权益 未知 · 可用 未知 · 保证金 未知 · 当日盈亏 未知")
        self.local_label = QLabel("本地成交：已实现毛收益 未知 · 确认手续费 未知 · 已实现净收益 未知")
        self.local_label.setWordWrap(True)
        self.scope_label = QLabel("离线账本 / 开发事实；名义风控点数与人民币资金分列。")
        self.scope_label.setWordWrap(True)
        self.quality_label = QLabel("资金和费用未取得时显示未知。")
        self.quality_label.setWordWrap(True)
        for widget in (self.status_label, self.account_label, self.local_label, self.scope_label, self.quality_label):
            layout.addWidget(widget)
        self.chart = EquityChart()
        layout.addWidget(self.chart)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("明细交易日筛选："))
        self.from_day = QComboBox(); self.to_day = QComboBox()
        for box in (self.from_day, self.to_day):box.addItem("全部交易日", None)
        filters.addWidget(self.from_day); filters.addWidget(QLabel("至")); filters.addWidget(self.to_day)
        filters.addStretch()
        self.csv_button = QPushButton("导出当前表 CSV")
        self.json_button = QPushButton("导出完整报告 JSON")
        self.daily_button = QPushButton("导出日报 Excel")
        self.reconciliation_button = QPushButton('成交对照/导入费用')
        self._reconciliation_window = None
        filters.addWidget(self.csv_button); filters.addWidget(self.json_button)
        filters.addWidget(self.daily_button)
        filters.addWidget(self.reconciliation_button)
        layout.addLayout(filters)
        self.tabs = QTabWidget()
        self.tables = {}
        for key, title, columns in _TABLES:
            table = QTableWidget(0, len(columns))
            table.setHorizontalHeaderLabels([label for _, label in columns])
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.horizontalHeader().setStretchLastSection(True)
            for column, (field, _) in enumerate(columns):
                if field == 'observed_at_utc':table.setColumnWidth(column, 185)
                elif field in {'position_cycle_id', 'trade_key'}:table.setColumnWidth(column, 195)
                elif field == 'directional_slippage_points':table.setColumnWidth(column, 145)
            self.tables[key] = table
            self.tabs.addTab(table, title)
        layout.addWidget(self.tabs, 1)
        self.rows_label = QLabel("尚无报表。筛选只影响带交易日的明细；顶部与按品种汇总保持完整账本范围。")
        self.rows_label.setWordWrap(True)
        layout.addWidget(self.rows_label)
        self.choose_button.clicked.connect(self.choose_ledger)
        self.refresh_button.clicked.connect(self.refresh)
        self.csv_button.clicked.connect(lambda: self.export("csv"))
        self.json_button.clicked.connect(lambda: self.export("json"))
        self.daily_button.clicked.connect(self.export_daily)
        self.reconciliation_button.clicked.connect(self.open_reconciliation)
        self.from_day.currentIndexChanged.connect(self._render_tables)
        self.to_day.currentIndexChanged.connect(self._render_tables)
        self.tabs.currentChanged.connect(self._render_tables)
        self.path_edit.textEdited.connect(self._source_changed)
        self.account_edit.textEdited.connect(self._source_changed)
        self._enabled(True)

    def _enabled(self, enabled):
        for widget in (self.path_edit, self.account_edit, self.choose_button, self.refresh_button,
                       self.from_day, self.to_day):widget.setEnabled(enabled)
        self.reconciliation_button.setEnabled(enabled)
        for button in (self.csv_button, self.json_button):button.setEnabled(enabled and self._report is not None)
        self.daily_button.setEnabled(enabled and self._report is not None and not self._stale)

    def _source_changed(self, _value=None):
        self._stale = True
        self.status_label.setText("来源已更改 · 当前显示已过期，请刷新核对新账本")

    def open_reconciliation(self):
        from online_trader.desktop.reconciliation import ReconciliationWindow
        if self._reconciliation_window is None:
            self._reconciliation_window = ReconciliationWindow(
                self.path_edit.text().strip() or None,self.account_edit.text().strip() or None,pipe_name=self.pipe_name)
        self._reconciliation_window.show()
        self._reconciliation_window.raise_()
        self._reconciliation_window.activateWindow()

    def choose_ledger(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 OnlineTrader 账本", "", "SQLite账本 (*.db);;所有文件 (*)")
        if path:
            self.path_edit.setText(path)
            self._source_changed()

    def _start(self, worker):
        if self._worker is not None or self._closing:return
        self._enabled(False)
        self._worker = worker
        self._task_action = worker.action
        worker.result.connect(self._result)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        worker.start()

    def refresh(self):
        if self._worker is not None or self._closing:return
        path, account = self.path_edit.text().strip(), self.account_edit.text().strip()
        if not path or (not account and not self.pipe_name):
            self._stale = True
            self.status_label.setText("请先选择本产品账本并填写账户档案标识")
            return
        self.status_label.setText("正在读取完整账本快照…")
        self._start(_ReportTask(action="load", reader=self._reader, path=Path(path),
                                account_key=account or None, pipe_name=self.pipe_name))

    def _result(self, result):
        if self._closing:return
        if result["action"] == "export":
            self.status_label.setText("已导出：" + result["destination"] + (" · 数据已过期" if self._stale else ""))
            return
        self._report = copy.deepcopy(result["report"])
        report = self._report
        self._stale = False
        self.account_edit.setText(report["account_key"])
        metadata = result.get("metadata")
        boundary = "内核已接受快照" if metadata else "离线账本已提交快照"
        self.status_label.setText(f"{boundary} · 事件 {report['checkpoint_event_seq']} · 账户 {report['account_key']}")
        equity = report.get("account") or (report["equity"][-1] if report["equity"] else {})
        incomplete = equity.get('complete') is False
        self.account_label.setText(("账户快照（资金来源不完整）：" if incomplete else "账户快照：") + " · ".join(label + " " + _display(equity.get(key)) for key, label in (
            ("balance", "权益"), ("available", "可用"), ("margin", "保证金"), ("today_pnl", "DSV当日盈亏"))))
        summary = report["summary"]
        self.local_label.setText("本地成交（CNY）：" + " · ".join(label + " " + _display(summary.get(key), estimated=estimated)
            for key, label, estimated in (("gross_realized_pnl", "已实现毛收益", False),
                ("confirmed_fees", "确认费用小计", False), ("fees", "费用合计", False), ("estimated_fees", "估算费用", True),
                ("net_realized_pnl", "已实现净收益", False),
                ("estimated_net_realized_pnl", "估算净收益", True), ("unrealized_pnl", "未平浮盈亏", False))))
        basis = {"CONFIRMED_FILL_FIFO": "按确认顺序的先进先出成交成本"}.get(report.get('accounting_basis'), '未知成本口径')
        self.scope_label.setText(f"范围：本地自有成交 · {basis} · "
            "账户权益来自独立资金快照，名义点数分列；当前未连接柜台。" +
            (f" 数据问题 {len(report.get('issues', []))} 项。" if report.get("issues") else ""))
        quality = []
        if incomplete:quality.append("资金来源不完整，已知单项仅供核对")
        if summary.get('fees') is None:quality.append("成交费用未全部确认，净收益保留未知")
        if summary.get('gross_realized_pnl') is None:quality.append("成交成本或乘数证据不足，毛收益保留未知")
        if summary.get('unrealized_pnl') is None:quality.append("未平头寸缺少有效估值，浮盈亏保留未知")
        if equity.get('today_pnl') is None:quality.append("账户快照不全，资金当日盈亏保留未知")
        issue_codes = {item.get('code') if isinstance(item, dict) else str(item) for item in report.get('issues', [])}
        if 'FEE_INPUT_INVALID' in issue_codes:quality.append("费用输入无效，按缺失费用处理")
        if issue_codes & {'RISK_MONEY_FIELD_INVALID', 'ACCOUNT_MONEY_FIELD_INVALID'}:
            quality.append("资金来源字段无效，相关金额按未知处理")
        self.quality_label.setText("；".join(quality) if quality else "本页金额依已确认来源计算；账户资金与自有成交分别展示。")
        self.chart.set_rows(report["equity"])
        days = sorted({str(row["trading_day"]) for key, _, _ in _TABLES for row in report[key] if row.get("trading_day")})
        for box in (self.from_day, self.to_day):
            chosen = box.currentData()
            box.blockSignals(True);box.clear();box.addItem("全部交易日", None)
            for day in days:box.addItem(day, day)
            index = box.findData(chosen)
            box.setCurrentIndex(max(index, 0));box.blockSignals(False)
        self._render_tables()

    def _filtered_rows(self, key):
        if self._report is None:return []
        start, end = self.from_day.currentData(), self.to_day.currentData()
        if start and end and start > end and key != 'by_contract':return []
        rows = []
        for original in self._report[key]:
            row = original.get("state", original) if key == "nominal" else original
            day = row.get("trading_day")
            if day and ((start and day < start) or (end and day > end)):continue
            rows.append(row)
        return rows

    def _render_tables(self, *_args):
        if self._report is None:return
        for key, _, columns in _TABLES:
            rows = self._filtered_rows(key)
            table = self.tables[key]
            table.setRowCount(min(len(rows), _DISPLAY_LIMIT))
            for index, row in enumerate(rows[:_DISPLAY_LIMIT]):
                for column, (field, _) in enumerate(columns):
                    value = row.get(field)
                    estimated = field in {"fee", "estimated_fees"} and (field == "estimated_fees" or row.get("fee_quality") == "ESTIMATED")
                    item = QTableWidgetItem(_display(value, estimated=estimated))
                    item.setToolTip(item.text())
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter if isinstance(value, (str, int)) and field not in
                        {"contract_id", "position_cycle_id", "trade_key", "observed_at_utc", "trading_day", "direction", "effect", "fee_quality"} else Qt.AlignLeft | Qt.AlignVCenter)
                    table.setItem(index, column, item)
        current = _TABLES[self.tabs.currentIndex()][0]
        rows = self._filtered_rows(current)
        self.rows_label.setText(f"当前表 {len(rows)} 行，显示 {min(len(rows), _DISPLAY_LIMIT)} 行；CSV导出包含筛选后的全部行。"
            "日期筛选只影响带交易日明细；顶部、按品种汇总和JSON保持完整账本范围。")

    def request_daily_export(self):
        self._daily_after_load = bool(self.path_edit.text().strip())
        if self._daily_after_load:
            self.refresh()
        else:
            self.status_label.setText("请选择账本并刷新，然后导出所选交易日的日报")

    def export_daily(self):
        if self._report is None or self._stale or self._worker is not None or self._closing:
            return
        days = sorted({row.get('trading_day') for key in ('by_day','trades','nominal','equity')
                       for row in self._report[key] if row.get('trading_day')}, reverse=True)
        if not days:
            self.status_label.setText("当前账本没有可导出的交易日报")
            return
        day, ok = QInputDialog.getItem(self, "导出日报", "柜台交易日", days, 0, False)
        if not ok:
            return
        target, _ = QFileDialog.getSaveFileName(self, "保存日报", f"日报_{day}.xlsx", "Excel (*.xlsx)")
        if target:
            self._start(_ReportTask(action="export", report=copy.deepcopy(self._report),
                        destination=Path(target), table=day, format="daily_xlsx"))

    def export(self, format):
        if self._report is None or self._worker is not None or self._closing:return
        title = "导出当前表 CSV" if format == "csv" else "导出完整资金报告 JSON"
        target, _ = QFileDialog.getSaveFileName(self, title, "", "CSV (*.csv)" if format == "csv" else "JSON (*.json)")
        if not target:return
        table = _TABLES[self.tabs.currentIndex()][0]
        report = copy.deepcopy(self._report)
        if format == "csv":report[table] = copy.deepcopy(self._filtered_rows(table))
        self._start(_ReportTask(action="export", exporter=self._exporter, report=report,
            destination=Path(target), table=table, format=format))

    def _failed(self, code):
        if self._closing:return
        if self._task_action == "load":
            self._stale = True
            self._daily_after_load = False
            self.status_label.setText(f"报表读取失败 · 显示已过期 · {code}；保留上次完整记录")
        else:
            self.status_label.setText(f"导出失败 · {code}；已有文件不会被覆盖")

    def _finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:worker.deleteLater()
        self._task_action = None
        if self._closing:QTimer.singleShot(0, self.close)
        else:
            self._enabled(True)
            if self._daily_after_load and not self._stale:
                self._daily_after_load = False
                QTimer.singleShot(0, self.export_daily)

    def closeEvent(self, event):
        if self._worker is not None:
            self._closing = True
            self._worker.requestInterruption()
            self.hide();event.ignore()
            return
        event.accept()

    def showEvent(self, event):
        if self._worker is None:self._closing = False
        super().showEvent(event)


def run(ledger_path=None, account_key=None, *, pipe_name=None):
    app = QApplication.instance() or QApplication([])
    window = ReportsWindow(ledger_path, account_key, pipe_name=pipe_name)
    window.show()
    if ledger_path is not None and (account_key is not None or pipe_name):QTimer.singleShot(0, window.refresh)
    return app.exec()
