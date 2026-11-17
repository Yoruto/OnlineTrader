"""DSV offset calculator with completed daily data and explicit preview apply."""
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QInputDialog, QPushButton, QVBoxLayout)
from online_trader.configuration.editor import form_values, profile_from_form
from online_trader.configuration.offsets import (FORMULAS, DailyCsvError, calculate_offsets,
    load_daily, export_calculation, formula_source)
from online_trader.desktop.settings import _table, _cell
from online_trader.errors import TraderError


class OffsetCalculatorDialog(QDialog):
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.base_profile, self.result_profile = profile, None
        self.base_hash = profile.content_hash
        self.dataset, self.calculation = None, None
        self._rendering = False
        self.setWindowTitle("OnlineTrader · DSV偏移计算器"); self.resize(1240, 780)
        layout = QVBoxLayout(self)
        notice = QLabel("加载离线日线CSV后计算。截止交易日当天及之后的日线不参与；当日振幅留空时按DSV不启用上限。勾选结果并应用只更新参数预览。")
        notice.setWordWrap(True); layout.addWidget(notice)
        controls = QHBoxLayout()
        self.load_button = QPushButton("加载日线CSV")
        self.formula_combo = QComboBox()
        for key, title in FORMULAS.items(): self.formula_combo.addItem(title, key)
        auto = profile.to_record()["settings"]["auto_offset"]
        self.formula_combo.setCurrentIndex(self.formula_combo.findData(auto["formula"]))
        self.ceiling_input = QLineEdit(auto["ceiling"]); self.ceiling_input.setMaximumWidth(85)
        self.floor_input = QLineEdit(auto["floor"]); self.floor_input.setMaximumWidth(85)
        self.date_input = QLineEdit(); self.date_input.setPlaceholderText("YYYY-MM-DD"); self.date_input.setMaximumWidth(130)
        self.calculate_button = QPushButton("计算 / 从缓存重算")
        self.coefficient_button = QPushButton("编辑品种系数")
        for widget in (self.load_button, self.formula_combo, QLabel("截止交易日"), self.date_input,
                       self.coefficient_button, self.calculate_button): controls.addWidget(widget)
        layout.addLayout(controls)
        ratios = QHBoxLayout()
        for widget in (QLabel("当日振幅上限比例（非负）"), self.ceiling_input,
                       QLabel("开仓偏移下限比例（非负）"), self.floor_input): ratios.addWidget(widget)
        ratios.addStretch(); layout.addLayout(ratios)
        self.data_label = QLabel("尚未加载日线。CSV列：series_id,date,high,low,close；具体合约与KQ.m@主连分开提供。")
        self.data_label.setWordWrap(True); layout.addWidget(self.data_label)
        self.error_label = QLabel(); self.error_label.setWordWrap(True); layout.addWidget(self.error_label)
        self.table = _table(["选择 / 品种与合约", "数据序列", "当日振幅（可编辑）", "系数", "当前开/损/盈", "计算开/损/盈", "核对提示"])
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
        layout.addWidget(self.table)
        self.detail_label = QLabel("开仓：先按基准×系数四舍六入五成双取整，再计算振幅上限/下限；止盈独立使用未取整基准×系数×0.9。固定止损按DSV品种表。")
        self.detail_label.setWordWrap(True); layout.addWidget(self.detail_label)
        actions = QHBoxLayout()
        self.select_button = QPushButton("选择全部成功结果"); self.copy_button = QPushButton("复制计算文本")
        self.export_button = QPushButton("导出计算依据")
        self.apply_button = QPushButton("应用所选到预览"); self.cancel_button = QPushButton("关闭计算器")
        for widget in (self.select_button, self.copy_button, self.export_button): actions.addWidget(widget)
        actions.addStretch(); actions.addWidget(self.apply_button); actions.addWidget(self.cancel_button); layout.addLayout(actions)
        self._render_initial()
        self.load_button.clicked.connect(self._choose_daily); self.calculate_button.clicked.connect(self.calculate)
        self.coefficient_button.clicked.connect(self._choose_coefficients)
        self.select_button.clicked.connect(self.select_successful); self.copy_button.clicked.connect(self.copy_text)
        self.export_button.clicked.connect(self._choose_export); self.apply_button.clicked.connect(self.apply_selected)
        self.cancel_button.clicked.connect(self.reject); self.table.itemChanged.connect(self._table_changed)
        for widget in (self.ceiling_input, self.floor_input, self.date_input): widget.textChanged.connect(self.invalidate)
        self.formula_combo.currentIndexChanged.connect(self._formula_changed)
        self.invalidate()

    def set_base_profile(self, profile):
        self.base_profile, self.result_profile = profile, None
        self.base_hash = profile.content_hash
        auto = profile.to_record()['settings']['auto_offset']
        self.formula_combo.blockSignals(True)
        self.formula_combo.setCurrentIndex(self.formula_combo.findData(auto['formula']))
        self.formula_combo.blockSignals(False)
        self._render_initial()
        self.ceiling_input.setText(auto['ceiling'])
        self.floor_input.setText(auto['floor'])
        self.error_label.clear()
        self.invalidate()

    def _render_initial(self):
        self._rendering = True
        symbols = self.base_profile.to_record()["settings"]["symbols"]
        self.table.setRowCount(len(symbols))
        for row, symbol in enumerate(symbols):
            try: _, identity = formula_source(self.formula_combo.currentData(), symbol["name"], symbol["code"])
            except TraderError: identity = "待选择公式"
            for column, value in enumerate((symbol["name"] + " · " + symbol["code"], identity, "", "—",
                    "/".join(symbol[key] for key in ("open_offset", "stop_offset", "profit_offset")), "—", "待计算")):
                _cell(self.table, row, column, value)
                if column != 2: self.table.item(row, column).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.table.resizeColumnsToContents(); self._rendering = False

    def invalidate(self, *_):
        self.calculation = None
        for button in (self.select_button, self.copy_button, self.export_button, self.apply_button): button.setEnabled(False)
        self._rendering = True
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.item(row, 0).setData(Qt.CheckStateRole, None)
            self.table.item(row, 3).setText("—")
            self.table.item(row, 5).setText("待重算"); self.table.item(row, 6).setText("待计算")
        self._rendering = False

    def _formula_changed(self, *_):
        self.invalidate()
        self._rendering = True
        for row, symbol in enumerate(self.base_profile.to_record()["settings"]["symbols"]):
            try: _, identity = formula_source(self.formula_combo.currentData(), symbol["name"], symbol["code"])
            except TraderError: identity = "待选择公式"
            self.table.item(row, 1).setText(identity)
        self._rendering = False

    def _table_changed(self, item):
        if self._rendering: return
        if item.column() == 2: self.invalidate()
        elif item.column() == 0: self.apply_button.setEnabled(bool(self.selected_contracts()))

    def load_file(self, path):
        try: dataset = load_daily(path)
        except DailyCsvError as error:
            self.error_label.setText(f"日线CSV第{error.line}行未通过校验，请核对列名、合约序列、日期顺序和高低收价格。原数据缓存保留。")
            return False
        except TraderError:
            self.error_label.setText("无法读取所选日线文件，原数据缓存保留。"); return False
        self.dataset = dataset; self.invalidate(); summary = dataset.summary()
        self.data_label.setText(f"已缓存{summary['bars']}根日线、{len(summary['series'])}个序列。数据哈希：{summary['data_hash']}")
        self.error_label.clear(); return True


    def calculation_input(self):
        return (self.base_profile, self.formula_combo.currentData(), self.date_input.text().strip(),
                self.ceiling_input.text(), self.floor_input.text(),
                tuple(self.table.item(row, 2).text() for row in range(self.table.rowCount())))

    def calculate_with_market(self, inputs):
        from online_trader.runtime.auto_offsets import dataset_from_rows
        symbols = self.base_profile.to_record()['settings']['symbols']
        try:
            dataset = dataset_from_rows(inputs['daily_rows'])
            ranges = [inputs['realized_ranges'][symbol['code']] for symbol in symbols]
        except (KeyError, TypeError, TraderError):
            self.error_label.setText('行情返回的日线或振幅未通过校验，原数据缓存保留。')
            return False
        self.dataset = dataset
        self.date_input.setText(inputs['as_of_date'])
        self._rendering = True
        try:
            for index, variation in enumerate(ranges):
                self.table.item(index, 2).setText(variation)
        finally:
            self._rendering = False
        summary = dataset.summary()
        self.data_label.setText(f"原生行情 · {summary['bars']} 根已完成日线 · "
                                f"最后完整交易日 {inputs['last_completed_day']} · {summary['data_hash']}")
        return self.calculate()

    def set_coefficients(self, raw):
        values = form_values(self.base_profile); values["auto_offset"]["coef"] = raw
        try: candidate = profile_from_form(self.base_profile, values)
        except TraderError:
            self.error_label.setText("品种系数表未通过校验。请填写品种名到非负系数的JSON映射；原系数保留。"); return False
        self.base_profile = candidate; self.invalidate()
        self.error_label.setText("系数已更新到计算器。请重新计算；应用所选结果时才会更新参数中心预览。")
        return True

    def _choose_coefficients(self):
        raw = json.dumps(self.base_profile.to_record()["settings"]["auto_offset"]["coef"], ensure_ascii=False, indent=2)
        text, accepted = QInputDialog.getMultiLineText(self, "编辑品种系数", "按DSV品种名填写系数；0使用DSV默认系数。", raw)
        if accepted: self.set_coefficients(text)

    def calculate(self):
        self.invalidate()
        if self.dataset is None:
            self.error_label.setText("请先加载离线日线CSV。"); return False
        symbols = self.base_profile.to_record()["settings"]["symbols"]
        ranges = {symbol["code"]: self.table.item(row, 2).text() for row, symbol in enumerate(symbols) if self.table.item(row, 2).text().strip()}
        try:
            result = calculate_offsets(self.base_profile, self.dataset, as_of_date=self.date_input.text(),
                realized_ranges=ranges, formula=self.formula_combo.currentData(), ceiling=self.ceiling_input.text(), floor=self.floor_input.text())
        except TraderError:
            self.error_label.setText("计算未完成。请核对截止交易日、公式和非负数值；旧结果已失效，当前参数预览未改变。"); return False
        self.calculation = result; self._rendering = True
        reasons = {"NO_COMPLETED_SOURCE_BARS": "没有匹配的已完成日线", "INSUFFICIENT_COMPLETED_BARS": "已完成日线数量不足", "CALCULATED_VALUE_OUT_OF_RANGE": "结果超出数值范围"}
        success = False
        for index, row in enumerate(result.to_record()["rows"]):
            if row["status"] == "CALCULATED":
                success = True
                self.table.item(index, 0).setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                self.table.item(index, 0).setCheckState(Qt.Unchecked)
                self.table.item(index, 3).setText(row["coefficient"])
                self.table.item(index, 5).setText("/".join(row[key] for key in ("open", "stop", "profit")))
                notes = [f"基准{row['base']} · {row['completed_bars']}根 · 截至{row['last_completed_date']}"]
                if row["short_window_only"]: notes.append("长窗不足，使用短窗")
                notes.append("振幅约束已计算" if row["cap_used"] else "未启用振幅约束")
                self.table.item(index, 6).setText("；".join(notes))
            else:
                self.table.item(index, 5).setText("—")
                self.table.item(index, 6).setText(reasons.get(row["reason"], "数据或参数未通过计算校验"))
        self._rendering = False
        for button in (self.copy_button, self.export_button): button.setEnabled(True)
        self.select_button.setEnabled(success); self.apply_button.setEnabled(False)
        self.error_label.clear(); self.table.resizeColumnsToContents(); return True

    def selected_contracts(self):
        if self.calculation is None: return []
        return [row["contract_id"] for index, row in enumerate(self.calculation.to_record()["rows"])
                if row["status"] == "CALCULATED" and self.table.item(index, 0).checkState() == Qt.Checked]

    def select_successful(self):
        if self.calculation is None: return
        for index, row in enumerate(self.calculation.to_record()["rows"]):
            if row["status"] == "CALCULATED": self.table.item(index, 0).setCheckState(Qt.Checked)

    def apply_selected(self):
        if self.calculation is None: return False
        try: candidate = self.calculation.apply(self.base_profile, self.selected_contracts())
        except TraderError:
            self.error_label.setText("请选择计算成功的品种；参数变化后需要重新计算。"); return False
        self.result_profile = candidate; self.accept(); return True

    def copy_text(self):
        if self.calculation is None: return False
        QApplication.clipboard().setText(self.calculation.text()); return True

    def export_file(self, path):
        if self.calculation is None: return False
        try: export_calculation(self.calculation, path)
        except TraderError:
            self.error_label.setText("计算依据未导出，请选择新的可写文件。"); return False
        self.error_label.clear(); return True

    def _choose_daily(self):
        path, _ = QFileDialog.getOpenFileName(self, "加载离线日线", "", "日线CSV (*.csv)")
        if path: self.load_file(path)

    def _choose_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出到新的计算依据文件", "offset-calculation.json", "计算依据 (*.json)")
        if path: self.export_file(path)
