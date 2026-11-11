"""All four business sections edited in an isolated, validated modal form."""
import re
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget)
from online_trader.configuration.editor import EditError, form_values, profile_from_form
from online_trader.configuration.symbol_csv import FIELDS, HEADERS
from online_trader.configuration.offsets import FORMULAS
from online_trader.desktop.settings import _TRADE_LABELS, _OTHER_LABELS, _table, _cell


class ParameterEditorDialog(QDialog):
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.base_profile, self.result_profile = profile, None
        self.base_hash = profile.content_hash
        self.fields = {}; self._form_rows = []
        self.setWindowTitle("OnlineTrader · 编辑业务参数预览"); self.resize(1120, 820)
        layout = QVBoxLayout(self)
        notice = QLabel("全部编辑先留在本窗口。校验通过后才更新参数中心预览；取消会放弃本次编辑。交易比例使用0–1小数，偏移单位为价格点，手数0/“自动”保留自动含义。")
        notice.setWordWrap(True); layout.addWidget(notice)
        self.error_label = QLabel(); self.error_label.setWordWrap(True); layout.addWidget(self.error_label)
        self.search = QLineEdit(); self.search.setPlaceholderText("筛选参数名称"); layout.addWidget(self.search)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs)
        values = form_values(profile)
        for section, title in (("trade", "交易与风控"), ("auto_offset", "偏移计算设置"), ("schedule", "定时设置")):
            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            body = QWidget(); form = QFormLayout(body)
            for key, value in sorted(values[section].items()):
                path = section + "." + key
                label = (_TRADE_LABELS if section == "trade" else _OTHER_LABELS).get(key, key)
                if section == "auto_offset": label = {"formula": "偏移候选公式", "ceiling": "当日振幅上限比例（非负）", "floor": "开仓偏移下限比例（非负）", "coef": "品种系数表（按品种名）"}[key]
                if section == "trade" and (key.endswith("_ratio") or key == "total_margin_cap"): label += "（0–1）"
                if type(value) is bool:
                    widget = QCheckBox("开启"); widget.setChecked(value)
                elif key in {"formula", "close_price_mode"}:
                    widget = QComboBox()
                    choices = FORMULAS if key == "formula" else {item: item for item in ("对手价", "排队价", "最新价")}
                    for identity, choice_title in choices.items(): widget.addItem(choice_title, identity)
                    if widget.findData(value) < 0: widget.addItem("保存值：" + value, value)
                    widget.setCurrentIndex(widget.findData(value))
                elif key in {"holidays", "coef"}:
                    widget = QPlainTextEdit(value); widget.setMaximumHeight(130)
                    widget.setToolTip("JSON日期数组，例如[\"2026-11-10\"]" if key == "holidays" else 'JSON品种系数表，例如{"棕榈油":"0.45"}；0按DSV回退默认系数')
                else:
                    widget = QLineEdit(value)
                    if "time" in key or key == "night_session_close": widget.setPlaceholderText("HH:MM")
                self.fields[path] = widget
                form.addRow(label, widget); self._form_rows.append((form, form.rowCount() - 1, label + " " + key))
            scroll.setWidget(body); self.tabs.addTab(scroll, title)
        symbols_body = QWidget(); symbols_layout = QVBoxLayout(symbols_body)
        self.symbols_table = _table(HEADERS)
        self.symbols_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
        self.symbols_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.symbols_table.setRowCount(len(values["symbols"]))
        for row, symbol in enumerate(values["symbols"]):
            for column, field in enumerate(FIELDS): _cell(self.symbols_table, row, column, symbol[field])
        self.symbols_table.resizeColumnsToContents(); symbols_layout.addWidget(self.symbols_table)
        controls = QHBoxLayout()
        self.add_button = QPushButton("新增品种"); self.remove_button = QPushButton("删除选中行")
        self.batch_field = QComboBox()
        for field, title in zip(FIELDS[2:8], HEADERS[2:8]): self.batch_field.addItem(title, field)
        self.batch_value = QLineEdit(); self.batch_value.setPlaceholderText("批量值")
        self.batch_button = QPushButton("设置所选行")
        for widget in (self.add_button, self.remove_button, self.batch_field, self.batch_value, self.batch_button): controls.addWidget(widget)
        symbols_layout.addLayout(controls); self.tabs.addTab(symbols_body, "品种与批量编辑")
        buttons = QHBoxLayout(); buttons.addStretch()
        self.apply_button = QPushButton("校验并更新预览"); self.cancel_button = QPushButton("取消编辑")
        buttons.addWidget(self.apply_button); buttons.addWidget(self.cancel_button); layout.addLayout(buttons)
        self.apply_button.clicked.connect(self.validate_preview); self.cancel_button.clicked.connect(self.reject)
        self.add_button.clicked.connect(self.add_symbol); self.remove_button.clicked.connect(self.remove_selected)
        self.batch_button.clicked.connect(self.batch_selected); self.search.textChanged.connect(self._filter)

    def _filter(self, text):
        for form, row, label in self._form_rows: form.setRowVisible(row, text.casefold() in label.casefold())

    def values(self):
        result = {section: {} for section in ("trade", "auto_offset", "schedule")}
        for path, widget in self.fields.items():
            section, key = path.split(".", 1)
            result[section][key] = (widget.isChecked() if isinstance(widget, QCheckBox) else
                widget.currentData() if isinstance(widget, QComboBox) else
                widget.toPlainText() if isinstance(widget, QPlainTextEdit) else widget.text())
        result["symbols"] = [{field: self.symbols_table.item(row, column).text() if self.symbols_table.item(row, column) else ""
            for column, field in enumerate(FIELDS)} for row in range(self.symbols_table.rowCount())]
        return result

    def _failure(self, error):
        label = "品种清单"
        if "." in error.path and not error.path.startswith("symbols"):
            section, key = error.path.split(".", 1)
            label = (_TRADE_LABELS if section == "trade" else _OTHER_LABELS).get(key, "业务参数")
        match = re.fullmatch(r"symbols\[([0-9]+)\](?:\.(.+))?", error.path)
        if match:
            row = int(match[1]); field = match[2]
            label = f"品种第{row + 1}行 · {_OTHER_LABELS.get(field, '参数')}"
            self.tabs.setCurrentIndex(3)
            self.symbols_table.setCurrentCell(row, FIELDS.index(field) if field in FIELDS else 0)
        self.error_label.setText(f"参数未通过校验：{label}。请核对范围、单位和格式；本次编辑仍保留，参数中心预览未改变。")
        widget = self.fields.get(error.path)
        if widget is not None:
            self.search.clear()
            self.tabs.setCurrentIndex(("trade", "auto_offset", "schedule").index(error.path.split(".")[0]))
            widget.setFocus()
            self.tabs.currentWidget().ensureWidgetVisible(widget)
        return False

    def validate_preview(self):
        try: candidate = profile_from_form(self.base_profile, self.values())
        except EditError as error: return self._failure(error)
        self.result_profile = candidate; self.error_label.clear(); self.accept()
        return True

    def add_symbol(self):
        row = self.symbols_table.rowCount()
        if row >= 64:
            self.error_label.setText("最多编辑64个品种。"); return False
        self.symbols_table.insertRow(row)
        for column, value in enumerate(("", "", "0", "0", "0", "自动", "0.5", "0", "")): _cell(self.symbols_table, row, column, value)
        self.symbols_table.selectRow(row); self.symbols_table.scrollToBottom()
        return True

    def remove_selected(self):
        for row in sorted({item.row() for item in self.symbols_table.selectionModel().selectedRows()}, reverse=True): self.symbols_table.removeRow(row)

    def batch_selected(self):
        rows = sorted({item.row() for item in self.symbols_table.selectionModel().selectedRows()})
        if not rows:
            self.error_label.setText("请先选中需要批量修改的品种行。"); return False
        field, value = self.batch_field.currentData(), self.batch_value.text()
        values = self.values()
        for row in rows: values["symbols"][row][field] = value
        try: profile_from_form(self.base_profile, values)
        except EditError as error: return self._failure(error)
        for row in rows: _cell(self.symbols_table, row, FIELDS.index(field), value)
        self.error_label.clear()
        return True
