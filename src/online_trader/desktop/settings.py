"""Offline DSV parameter preview. Import/export never applies a trading policy."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextEdit, QVBoxLayout)

from online_trader.errors import TraderError


MAX_IMPORT_BYTES = 1_048_576

_TRADE_LABELS = {
    "max_volume": "策略数量上限（手）", "max_risk_ratio": "风险度参数",
    "max_daily_orders": "每日报单上限", "max_net_loss_stops": "名义净亏止损倍数",
    "close_before_seconds": "收盘退出提前量（秒）", "trailing_activate_ratio": "普通移动激活比例",
    "trailing_pullback_ratio": "普通移动回撤参数", "stop_day_on_trailing": "移动退出后日停",
    "protect_80_enabled": "利润保护", "protect_80_ratio": "保护激活比例",
    "protect_keep_ratio": "保护留利比例", "protect_stop_day": "保护触发后日停",
    "entry_slip_abort": "成交滑点退出", "entry_slip_abort_ratio": "成交滑点退出比例",
    "entry_slip_abort_stop_day": "滑点退出后日停", "half_close_enabled": "半仓减仓",
    "cont_stop_day_stop": "延续开止损后日停", "continuation_entry": "延续开",
    "continuation_ignore_no_open": "延续开豁免开盘等待", "no_chase_after_act": "跑过激活位后禁开",
    "manual_close_day_stop": "人工平仓后日停", "tick_diag": "逐行情诊断",
    "risk_per_trade_ratio": "单笔风险比例", "stale_force_close_seconds": "断流退出参数（秒）",
    "stale_close_day_stop": "断流退出后日停", "aggressive_mode": "激进模式",
    "passive_entry_first": "入场先被动报价", "auto_start_on_launch": "自动启动",
    "chase_slip_ratio": "追价比例参数", "chase_base_now": "追价使用现价基准",
    "min_one_lot_fallback": "保底一手", "stop_cooldown_enabled": "止损冷却",
    "stop_cooldown_minutes": "冷却时间预填值（分钟）", "require_trigger_update": "要求触发价更新",
    "max_symbols_enabled": "品种数限制", "max_symbols_on": "品种数上限预填值",
    "rollover_stop_enabled": "到期停开", "live_protect": "实盘保护",
    "live_max_lots": "实盘手数上限预填值", "live_daily_loss_limit": "日亏上限预填值（元）",
    "live_first_one_lot": "首次一手限制", "half_close_ratio": "全局半仓同名参数原值",
    "manual_tp_drop_ratio": "人工止盈回撤参数原值", "month_stop_days": "月度停开参数原值",
    "night_session_close": "夜盘收盘保存值", "holidays": "节假日保存表",
    "entry_slip_ratio": "入场价差比例参数", "chase_window_minutes": "追价窗口（分钟）",
    "no_chase_tolerance": "追价容差原值", "no_open_before_close_minutes": "收盘前禁开（分钟）",
    "no_open_after_open": "开盘后禁开", "no_open_after_open_minutes": "开盘后禁开（分钟）",
    "open_jump_block_ratio": "首分钟跳空比例参数", "hysteresis_points": "全局滞回原值（价格点）",
    "half_stage2": "二阶段减仓", "half_stage2_trigger_ratio": "二阶段触发预填比例",
    "half_breakeven": "减仓后保本", "total_margin_cap": "总保证金占用参数",
    "position_sync_seconds": "状态同步参数（秒）", "participate_auction": "参与集合竞价",
    "close_price_mode": "收盘报价模式", "cancel_on_stop": "停止时撤单",
    "margin_rate_alert_threshold": "保证金率变化提醒参数", "max_quote_age_seconds": "报价年龄原值（秒）",
    "min_days_to_expiry": "到期前停开参数", "limit_fallback_ratio": "限价后备比例原值",
    "open_gap_max_ratio": "开盘价差比例原值", "entry_confirm_timeout": "入场确认期限原值",
    "risk_reference_equity": "权益参考原值",
}
_SECTION_LABELS = {"trade": "交易参数", "auto_offset": "偏移计算参数", "schedule": "定时参数"}
_OTHER_LABELS = {
    "formula": "公式保存值", "ceiling": "上界原值", "floor": "下界原值", "coef": "品种系数原值",
    "enabled": "启用", "night_start_time": "夜盘启动时间", "day_start_time": "日盘启动时间",
    "catch_up_minutes": "错过时点窗口（分钟）", "name": "品种名称", "code": "保存合约",
    "code_new": "候选换月合约", "open_offset": "开仓", "stop_offset": "止损",
    "profit_offset": "止盈", "order_volume": "手数原值", "half_close_ratio": "半仓同名参数原值",
    "hysteresis_points": "滞回原值",
}
_MISSING = object()
_CSV_FIELDS = {"file": "文件", "header": "列标题", "row": "品种参数",
               **{key: value for key, value in _OTHER_LABELS.items()}}
_CSV_REASONS = {
    "expected_nonnegative_decimal": "必须填写非负小数，不能留空或使用科学计数法",
    "invalid_decimal": "数值格式或精度不受支持",
    "unresolved_explicit_contract": "请填写明确月份的合约，并核对交易所与品种别名",
    "duplicate_contract": "同一合约在文件中重复出现",
    "expected_integer_or_auto": "请填写非负整数或“自动”",
    "unknown_duplicate_or_missing_columns": "存在未知、重复或缺少的必填列",
    "missing_header": "缺少列标题", "column_count": "单元格数量与列标题不一致",
    "symbol_limit": "品种总数超过64个", "invalid_symbol_values": "取值超出范围或格式不正确",
    "empty_symbol_list": "没有可导入的品种", "size_limit": "文件超过256 KiB",
    "malformed_utf8_csv": "请检查UTF-8编码和CSV引号格式", "expected_utf8_csv": "需要UTF-8格式的CSV",
}


def _default_baseline():
    from online_trader.configuration.baseline import default_preview_profile
    return default_preview_profile(), {"rule_set_id": "离线预览"}


def _display(value):
    if value is _MISSING: return "未提供"
    if value is True: return "开启"
    if value is False: return "关闭"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if type(value) in {int, float}:
        text = format(Decimal(str(value)), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _flatten(value, prefix=""):
    if isinstance(value, dict) and value:
        result = {}
        for key, item in value.items(): result.update(_flatten(item, f"{prefix}.{key}" if prefix else key))
        return result
    if isinstance(value, list) and value:
        result = {}
        for index, item in enumerate(value): result.update(_flatten(item, f"{prefix}[{index}]"))
        return result
    return {prefix: value}


def _label(path):
    section, _, tail = path.partition(".")
    key = tail.rsplit(".", 1)[-1]
    if section.startswith("symbols["):
        index = int(section[8:-1]) + 1
        return f"品种第{index}行 · {_OTHER_LABELS.get(key, '已保存参数')}"
    labels = _TRADE_LABELS if section == "trade" else _OTHER_LABELS
    return f"{_SECTION_LABELS.get(section, '参数')} · {labels.get(key, '已保存参数')}"


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().setVisible(False)
    return table


def _cell(table, row, column, text, tooltip=None):
    item = QTableWidgetItem(str(text))
    if tooltip: item.setToolTip(tooltip)
    table.setItem(row, column, item)


class SettingsDialog(QDialog):
    """Offline preview, exclusive exports and explicit local trade-form templates."""
    preview_changed = Signal(object)
    templates_changed = Signal()
    template_directory_changed = Signal(str)

    def __init__(self, parent=None, *, baseline_loader=None, contract_catalog=(), contract_catalog_loader=None):
        super().__init__(parent)
        self._baseline, self._manifest = (baseline_loader or _default_baseline)()
        self._profile = self._baseline
        self._template_store = None
        self._template_snapshot = None
        self._contract_catalog = tuple(dict.fromkeys(contract_catalog or ()))
        self._contract_catalog_loader = contract_catalog_loader
        self._symbol_editor = None
        self.setWindowTitle("OnlineTrader · DSV参数中心")
        self.resize(1160, 760)
        layout = QVBoxLayout(self)
        self.profile_label = QLabel()
        self.reference_label = QLabel("已确认来源：" + self._manifest.get("rule_set_id", "DSV业务基准"))
        self.guard_label = QLabel()
        self.entry_label = QLabel()
        self.notice_label = QLabel("导入仅用于核对预览；不会改变运行参数或持仓计划。导出只包含业务设置。")
        self.notice_label.setWordWrap(True)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        for widget in (self.profile_label, self.reference_label, self.guard_label, self.entry_label,
                       self.notice_label, self.error_label): layout.addWidget(widget)
        self.tabs = QTabWidget()
        self.parameters_table = _table(["参数", "已确认基准", "当前预览", "变化"])
        self.symbols_table = _table(["品种", "保存合约", "开仓", "止损", "止盈",
                                     "手数原值", "半仓参数原值", "滞回原值", "候选换月合约", "核对状态"])
        self.differences_table = _table(["变化项目", "已确认基准", "当前预览"])
        self.diagnostics_text = QTextEdit()
        self.diagnostics_text.setReadOnly(True)
        for widget, title in ((self.parameters_table, "交易与定时参数"), (self.symbols_table, "品种参数"),
                              (self.differences_table, "与基准的差异"), (self.diagnostics_text, "核对提示")):
            self.tabs.addTab(widget, title)
        layout.addWidget(self.tabs)
        symbols_controls = QHBoxLayout()
        self.symbol_mode = QComboBox()
        self.symbol_mode.addItem("替换品种预览", "replace")
        self.symbol_mode.addItem("合并品种预览", "merge")
        self.symbol_import_button = QPushButton("导入品种")
        self.symbol_export_button = QPushButton("导出品种")
        symbols_controls.addWidget(QLabel("品种清单："))
        for widget in (self.symbol_mode, self.symbol_import_button, self.symbol_export_button):
            symbols_controls.addWidget(widget)
        symbols_controls.addStretch(); layout.addLayout(symbols_controls)
        template_controls = QHBoxLayout()
        self.template_directory_button = QPushButton("选择模板目录")
        self.template_reload_button = QPushButton("重新载入")
        self.template_combo = QComboBox(); self.template_combo.setMinimumWidth(170)
        self.template_name = QLineEdit(); self.template_name.setPlaceholderText("新模板名称")
        self.template_apply_button = QPushButton("应用到预览")
        self.template_save_button = QPushButton("另存模板")
        self.template_overwrite_button = QPushButton("覆盖所选")
        self.template_delete_button = QPushButton("删除所选")
        for widget in (self.template_directory_button, self.template_reload_button, self.template_combo,
                       self.template_apply_button, self.template_name, self.template_save_button,
                       self.template_overwrite_button, self.template_delete_button):
            template_controls.addWidget(widget)
        layout.addLayout(template_controls)
        self.template_label = QLabel("尚未选择模板目录。模板只保存DSV的33项交易参数；应用只更新预览。")
        self.template_label.setWordWrap(True); layout.addWidget(self.template_label)
        controls = QHBoxLayout()
        self.import_button = QPushButton("导入参数预览")
        self.import_dsv_button = QPushButton("导入原版 DSV 配置")
        self.export_button = QPushButton("导出业务参数")
        self.reset_button = QPushButton("恢复基准预览")
        self.edit_button = QPushButton("编辑全部业务参数")
        self.calculator_button = QPushButton("离线偏移计算器")
        self.close_button = QPushButton("关闭")
        for button in (self.edit_button, self.calculator_button, self.import_button,
                       self.import_dsv_button, self.export_button, self.reset_button):
            controls.addWidget(button)
        controls.addStretch(); controls.addWidget(self.close_button); layout.addLayout(controls)
        self.import_button.clicked.connect(self._choose_import)
        self.import_dsv_button.clicked.connect(lambda: self._choose_import(source_kind="dsv"))
        self.export_button.clicked.connect(self._choose_export)
        self.reset_button.clicked.connect(self.reset_to_baseline)
        self.edit_button.clicked.connect(self._choose_editor)
        self.calculator_button.clicked.connect(self._choose_calculator)
        self.close_button.clicked.connect(self.close)
        self.symbol_import_button.clicked.connect(self._choose_symbol_import)
        self.symbol_export_button.clicked.connect(self._choose_symbol_export)
        self.template_directory_button.clicked.connect(self._choose_template_directory)
        self.template_reload_button.clicked.connect(self.reload_templates)
        self.template_apply_button.clicked.connect(lambda: self.apply_template(self.template_combo.currentText()))
        self.template_save_button.clicked.connect(lambda: self.save_template(self.template_name.text()))
        self.template_overwrite_button.clicked.connect(self._confirm_template_overwrite)
        self.template_delete_button.clicked.connect(self._confirm_template_delete)
        self._render_template_controls()
        self._render()

    @property
    def profile(self): return self._profile

    def set_reference_profile(self, profile, source):
        self._baseline = self._profile = profile
        self._manifest = {'rule_set_id': source}
        self.reference_label.setText('参数来源：' + source)
        self.error_label.clear()
        self._render()

    def _render(self):
        current = self._profile.to_record()["settings"]
        baseline = self._baseline.to_record()["settings"]
        same = self._profile.content_hash == self._baseline.content_hash
        self.profile_label.setText("已确认DSV基准 · 当前预览一致" if same else "导入参数预览 · 与已确认基准有差异")
        t = current["trade"]
        state = "开启" if t["live_protect"] else "关闭"
        suffix = "（开关已开启；尚未应用运行系统）" if t["live_protect"] else "（未启用）"
        self.guard_label.setText(f"实盘保护：{state}；预填上限：{_display(t['live_max_lots'])}手 / 日亏{_display(t['live_daily_loss_limit'])}元{suffix}")
        self.entry_label.setText("保底一手：" + _display(t["min_one_lot_fallback"]) +
            " · 触发价更新：" + _display(t["require_trigger_update"]) +
            " · 延续开豁免等待：" + _display(t["continuation_ignore_no_open"]))
        flat, base = _flatten(current), _flatten(baseline)
        parameter_paths = sorted(path for path in flat.keys() | base.keys() if not path.startswith("symbols["))
        self.parameters_table.setRowCount(len(parameter_paths))
        for row, path in enumerate(parameter_paths):
            before, after = base.get(path, _MISSING), flat.get(path, _MISSING)
            for column, text in enumerate((_label(path), _display(before), _display(after),
                                             "未变化" if before == after else "已修改")):
                _cell(self.parameters_table, row, column, text, path)
        symbols = current["symbols"]
        self.symbols_table.setRowCount(len(symbols))
        for row, symbol in enumerate(symbols):
            zero = any(Decimal(str(symbol[k])) <= 0 for k in ("open_offset", "stop_offset", "profit_offset"))
            values = [symbol[k] for k in ("name", "code", "open_offset", "stop_offset", "profit_offset",
                                           "order_volume", "half_close_ratio", "hysteresis_points")]
            values += [symbol.get("code_new", "—"), "待核参数：偏移含0" if zero else "参数已保存；合约状态未核验"]
            for column, value in enumerate(values): _cell(self.symbols_table, row, column, _display(value))
        changed = [path for path in sorted(flat.keys() | base.keys()) if base.get(path, _MISSING) != flat.get(path, _MISSING)]
        self.differences_table.setRowCount(len(changed))
        for row, path in enumerate(changed):
            for column, text in enumerate((_label(path), _display(base.get(path, _MISSING)), _display(flat.get(path, _MISSING)))):
                _cell(self.differences_table, row, column, text, path)
        self.tabs.setTabText(2, f"与基准的差异（{len(changed)}）")
        notes = ["这些是已保存的DSV参数；导入预览不批准新参数，也不会启用交易。",
            "合约代码、候选换月字段与偏移值分别保留；本页不证明合约当前可交易或已完成换月。",
            "滞回、报价年龄和权益参考的0原样显示，具体运行含义仍需核对。",
            "全局和品种同名半仓参数分开保留，不推断它们的优先级或分母。"]
        if not t["live_protect"]: notes.append("实盘保护关闭：2手和5000元等栏位是预填值，不能当成已启用硬限。")
        if not t["max_symbols_enabled"]: notes.append(f"品种数限制关闭：预填{_display(t['max_symbols_on'])}不作为已启用上限。")
        for symbol in symbols:
            if any(Decimal(str(symbol[k])) <= 0 for k in ("open_offset", "stop_offset", "profit_offset")):
                notes.append(symbol["name"] + "：偏移含0，保留待核状态，不自动补值。")
        self.diagnostics_text.setPlainText("\n\n".join(notes))
        for table in (self.parameters_table, self.symbols_table, self.differences_table): table.resizeColumnsToContents()
        self.preview_changed.emit(self._profile)

    def _failure(self, code):
        hint = "请重新载入模板后再操作。" if code == "E_TEMPLATE_CONFLICT" else ""
        self.error_label.setText(f"操作未完成，当前预览已保留。错误代码：{code}。{hint}")
        return False

    def import_bytes(self, raw, *, source_kind="preview"):
        from online_trader.configuration.dsv import DsvProfile
        try:
            if source_kind == "dsv": candidate = DsvProfile.from_dsv_json(raw)
            elif source_kind == "settings": candidate = DsvProfile.from_json(raw)
            elif source_kind == "preview": candidate = DsvProfile.from_preview_json(raw)
            else: return self._failure("E_CONFIG_SOURCE")
        except TraderError as error: return self._failure(error.code)
        self._profile = candidate
        self.error_label.clear(); self._render()
        self.notice_label.setText("参数已导入预览。差异见对应页签；运行参数与持仓计划未改变。")
        return True

    def import_file(self, path, *, source_kind="preview"):
        try:
            with Path(path).open("rb") as file: raw = file.read(MAX_IMPORT_BYTES + 1)
            if len(raw) > MAX_IMPORT_BYTES: return self._failure("E_CONFIG_SIZE")
        except OSError: return self._failure("E_CONFIG_IO")
        return self.import_bytes(raw, source_kind=source_kind)

    def export_file(self, path):
        from online_trader.configuration.files import export_profile
        try: export_profile(self._profile, path)
        except TraderError as error: return self._failure(error.code)
        except OSError: return self._failure("E_CONFIG_IO")
        self.error_label.clear()
        self.notice_label.setText("业务参数已导出到新文件；账户、口令和通知密钥不包含在导出中。")
        return True

    def reset_to_baseline(self):
        self._profile = self._baseline
        self.error_label.clear(); self._render()
        self.notice_label.setText("已恢复已确认DSV基准预览；运行参数与持仓计划未改变。")

    def apply_edited_preview(self, candidate, *, expected_hash):
        from online_trader.configuration.dsv import DsvProfile
        if self._profile.content_hash != expected_hash: return self._failure("E_CONFIG_CONFLICT")
        if not isinstance(candidate, DsvProfile): return self._failure("E_CONFIG_EDIT")
        self._profile = candidate; self.error_label.clear(); self._render()
        self.notice_label.setText("编辑或计算结果已更新参数预览。可核对差异后导出业务参数；运行参数与持仓计划未改变。")
        return True

    def _choose_editor(self):
        self.edit_preview()

    def set_contract_catalog(self, contracts):
        self._contract_catalog = tuple(dict.fromkeys(code for code in (contracts or ()) if isinstance(code, str)))
        if self._symbol_editor is not None:
            self._symbol_editor.set_contract_catalog(self._contract_catalog)

    def set_contract_catalog_error(self, code):
        if self._symbol_editor is not None:
            self._symbol_editor.set_contract_catalog_error(code)

    def edit_preview(self, *, symbol_row=None, add_symbol=False):
        if add_symbol or symbol_row is not None:
            from online_trader.desktop.symbol_editor import SymbolEditorDialog
            dialog = SymbolEditorDialog(self._profile, self if self.isVisible() else self.parentWidget(),
                                        symbol_row=None if add_symbol else symbol_row,
                                        contract_catalog=self._contract_catalog,
                                        catalog_loader=self._contract_catalog_loader)
            self._symbol_editor = dialog
        else:
            from online_trader.desktop.editor import ParameterEditorDialog
            dialog = ParameterEditorDialog(self._profile, self)
        try:
            if dialog.exec() == QDialog.Accepted:
                return self.apply_edited_preview(dialog.result_profile, expected_hash=dialog.base_hash)
            return False
        finally:
            if dialog is self._symbol_editor:
                self._symbol_editor = None
            dialog.deleteLater()

    def _choose_calculator(self):
        from online_trader.desktop.calculator import OffsetCalculatorDialog
        dialog = OffsetCalculatorDialog(self._profile, self)
        try:
            if dialog.exec() == QDialog.Accepted:
                self.apply_edited_preview(dialog.result_profile, expected_hash=dialog.base_hash)
        finally: dialog.deleteLater()

    def import_symbols_bytes(self, raw, *, mode="replace"):
        from online_trader.configuration.symbol_csv import import_symbols
        return self._apply_symbol_import(lambda: import_symbols(raw, self._profile, mode=mode), "CSV")

    def _apply_symbol_import(self, load, label):
        from online_trader.configuration.symbol_csv import SymbolCsvError
        try: candidate = load()
        except SymbolCsvError as error:
            field = _CSV_FIELDS.get(error.field, "品种参数")
            reason = _CSV_REASONS.get(error.reason, "未通过校验")
            self.error_label.setText(f"{label}第{error.line}行：{field}{reason}。当前预览已保留。")
            return False
        except TraderError as error: return self._failure(error.code)
        self._profile = candidate
        self.error_label.clear(); self._render()
        self.tabs.setCurrentIndex(1)
        self.notice_label.setText(f"品种{label}已导入预览。运行参数与持仓计划未改变。")
        return True

    def import_symbols_file(self, path, *, mode="replace"):
        if Path(path).suffix.lower() == ".xlsx":
            from online_trader.configuration.symbol_xlsx import load_symbol_workbook
            return self._apply_symbol_import(lambda: load_symbol_workbook(path, self._profile, mode=mode), "Excel")
        from online_trader.configuration.symbol_csv import MAX_CSV_BYTES
        try:
            with Path(path).open("rb") as stream: raw = stream.read(MAX_CSV_BYTES + 1)
        except OSError: return self._failure("E_SYMBOL_CSV_IO")
        return self.import_symbols_bytes(raw, mode=mode)

    def export_symbols_file(self, path):
        from online_trader.configuration.symbol_csv import export_symbols
        from online_trader.configuration.symbol_xlsx import export_symbol_workbook
        excel = Path(path).suffix.lower() == ".xlsx"
        export = export_symbol_workbook if excel else export_symbols
        try: export(self._profile, path)
        except TraderError as error: return self._failure(error.code)
        self.error_label.clear()
        self.notice_label.setText("已导出 DSV 八列 Excel 品种表。" if excel else "已导出 CSV 品种表及候选换月列。")
        return True

    def _render_template_controls(self):
        ready = self._template_snapshot is not None
        selected = ready and bool(self._template_snapshot.templates)
        for button in (self.template_reload_button, self.template_save_button): button.setEnabled(ready)
        for button in (self.template_apply_button, self.template_overwrite_button, self.template_delete_button):
            button.setEnabled(selected)
        self.template_combo.setEnabled(selected)

    def set_template_directory(self, path):
        from online_trader.configuration.templates import TemplateStore
        store = TemplateStore(path)
        try: snapshot = store.read()
        except TraderError as error: return self._failure(error.code)
        self._template_store, self._template_snapshot = store, snapshot
        self._show_templates(); self.error_label.clear()
        self.template_directory_changed.emit(str(Path(path)))
        return True

    def _show_templates(self, selected=None):
        selected = self.template_combo.currentText() if selected is None else selected
        self.template_combo.clear(); self.template_combo.addItems(sorted(self._template_snapshot.templates))
        index = self.template_combo.findText(selected)
        if index >= 0: self.template_combo.setCurrentIndex(index)
        self.template_label.setText(f"模板目录：{self._template_store.directory} · 版本 {self._template_snapshot.revision} · "
                                    "仅33项交易参数，应用只更新预览，品种清单和未收录参数保留。")
        self._render_template_controls()
        self.templates_changed.emit()

    def reload_templates(self):
        if self._template_store is None: return self._failure("E_TEMPLATE_DIRECTORY")
        try: snapshot = self._template_store.read()
        except TraderError as error: return self._failure(error.code)
        self._template_snapshot = snapshot; self._show_templates(); self.error_label.clear()
        return True

    def save_template(self, name, *, overwrite=False):
        if self._template_store is None: return self._failure("E_TEMPLATE_DIRECTORY")
        try:
            snapshot = self._template_store.save(name, self._profile,
                expected_revision=self._template_snapshot.revision, overwrite=overwrite)
        except TraderError as error: return self._failure(error.code)
        self._template_snapshot = snapshot; self._show_templates(name); self.error_label.clear()
        self.notice_label.setText("模板已保存为新版本，旧版本保留。运行参数与持仓计划未改变。")
        return True

    def apply_template(self, name):
        if self._template_store is None: return self._failure("E_TEMPLATE_DIRECTORY")
        try:
            current = self._template_store.read()
            if current.journal_hash != self._template_snapshot.journal_hash:
                return self._failure("E_TEMPLATE_CONFLICT")
            candidate = current.apply(name, self._profile)
        except TraderError as error: return self._failure(error.code)
        self._profile = candidate; self.error_label.clear(); self._render()
        self.notice_label.setText("模板的33项交易参数已填入预览；品种清单保持当前预览。运行参数与持仓计划未改变。")
        return True

    def delete_template(self, name):
        if self._template_store is None: return self._failure("E_TEMPLATE_DIRECTORY")
        try:
            snapshot = self._template_store.delete(name, expected_revision=self._template_snapshot.revision)
        except TraderError as error: return self._failure(error.code)
        self._template_snapshot = snapshot; self._show_templates(); self.error_label.clear()
        self.notice_label.setText("模板已从当前列表删除，历史版本保留。当前预览与持仓计划未改变。")
        return True

    def _choose_symbol_import(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入到品种预览", "", "品种表 (*.xlsx *.csv)")
        if path: self.import_symbols_file(path, mode=self.symbol_mode.currentData())

    def _choose_symbol_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出品种表", "品种列表.xlsx", "Excel (*.xlsx);;CSV (*.csv)")
        if path: self.export_symbols_file(path)

    def _choose_template_directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择本地模板目录")
        if path: self.set_template_directory(path)

    def _confirm_template_overwrite(self):
        name = self.template_combo.currentText()
        if name and QMessageBox.question(self, "覆盖模板", f"用当前预览的33项交易参数覆盖模板“{name}”？旧版本将保留。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.save_template(name, overwrite=True)

    def _confirm_template_delete(self):
        name = self.template_combo.currentText()
        if name and QMessageBox.question(self, "删除模板", f"从当前列表删除模板“{name}”？历史版本和当前预览将保留。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.delete_template(name)

    def _choose_import(self, *, source_kind="preview"):
        title = "选择原版 DSV 配置" if source_kind == "dsv" else "选择业务参数文件"
        path, _ = QFileDialog.getOpenFileName(self, title, "", "JSON参数 (*.json)")
        if path:
            self.import_file(path, source_kind=source_kind)

    def _choose_export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出到新的业务参数文件", "dsv-business-settings.json", "JSON参数 (*.json)")
        if path: self.export_file(path)


def run():
    app = QApplication.instance() or QApplication([])
    window = SettingsDialog()
    window.show()
    return app.exec()
