"""Modern grouped, directly-editable trade parameters panel for OnlineTrader workbench."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from online_trader.configuration.dsv import DsvProfile
from online_trader.errors import TraderError


_TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
_CLOSE_PRICE_MODES = ('对手价', '最新价', '涨跌停价', '市价')
_RESTART_TRADE_KEYS = frozenset({'night_session_close', 'holidays'})


class _ValidationError(ValueError):
    def __init__(self, key: str, message: str):
        super().__init__(message)
        self.key = key


class _SectionCard(QFrame):
    """Clean white card with optional collapse toggle."""

    def __init__(self, title: str, subtitle: str = '', *, collapsible: bool = False, collapsed: bool = False):
        super().__init__()
        self.setObjectName('paramSectionCard')
        self._collapsible = collapsible
        self._collapsed = collapsed
        self._title = title

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        if collapsible:
            self._toggle = QToolButton()
            self._toggle.setObjectName('paramSectionToggle')
            self._toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            self._toggle.setArrowType(Qt.RightArrow if collapsed else Qt.DownArrow)
            self._toggle.setText(title)
            self._toggle.setCheckable(True)
            self._toggle.setChecked(not collapsed)
            self._toggle.toggled.connect(self._on_toggled)
            header.addWidget(self._toggle)
        else:
            self._toggle = None
            title_label = QLabel(title)
            title_label.setObjectName('paramSectionTitle')
            header.addWidget(title_label)

        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName('paramSectionSubtitle')
            header.addWidget(sub)
        header.addStretch(1)
        outer.addLayout(header)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 2, 0, 0)
        self.body_layout.setSpacing(10)
        self.body.setVisible(not collapsed)
        outer.addWidget(self.body)

    def _on_toggled(self, expanded: bool):
        self._collapsed = not expanded
        if self._toggle is not None:
            self._toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.body.setVisible(expanded)

    def ensure_expanded(self):
        if self._collapsible and self._collapsed and self._toggle is not None:
            self._toggle.setChecked(True)


class TradeParametersPanel(QWidget):
    """Directly-editable, grouped trade parameters panel with live dirty tracking."""

    save_requested = Signal()
    revert_requested = Signal()
    apply_template_requested = Signal()
    save_template_requested = Signal()
    delete_template_requested = Signal()
    open_center_requested = Signal()
    import_dsv_requested = Signal()
    export_file_requested = Signal()
    timed_start_requested = Signal()

    def __init__(self, *, runtime_mode: bool = True, show_timed_start: bool = False, parent=None):
        super().__init__(parent)
        self._runtime_mode = runtime_mode
        self._show_timed_start = show_timed_start
        self._loading = False
        self._active_profile: DsvProfile | None = None
        self._base_profile: DsvProfile | None = None
        self._profile_source = ''
        self._saved_next_boot_hash: str | None = None
        self._live_save = True

        self.parameter_fields: dict[str, QWidget] = {}
        self.parameter_switches: dict[str, QCheckBox] = {}
        self.schedule_fields: dict[str, QLineEdit] = {}
        self.schedule_enabled_check: QCheckBox | None = None
        self.timed_start_button: QPushButton | None = None

        self._field_types: dict[str, str] = {}
        self._field_labels: dict[str, str] = {}
        self._field_cards: dict[str, _SectionCard] = {}
        self._switch_dependents: dict[str, list[QWidget]] = {}

        self._build_ui()

    def _build_ui(self):
        self.setObjectName('tradeParametersRoot')
        self.setStyleSheet("""
            QFrame#paramTopBar, QFrame#paramBottomBar {
                background: #ffffff;
                border: 1px solid #d8e2ef;
                border-radius: 8px;
            }
            QFrame#paramSectionCard {
                background: #ffffff;
                border: 1px solid #d8e2ef;
                border-radius: 8px;
            }
            QLabel#paramSectionTitle {
                color: #111c2e;
                font-size: 14px;
                font-weight: 700;
                padding-left: 6px;
                border-left: 3px solid #2b7bf4;
            }
            QToolButton#paramSectionToggle {
                color: #111c2e;
                font-size: 14px;
                font-weight: 700;
                border: 0;
                background: transparent;
                padding: 2px 4px;
            }
            QToolButton#paramSectionToggle:hover {
                color: #2b7bf4;
            }
            QLabel#paramSectionSubtitle {
                color: #66778d;
                font-size: 12px;
            }
            QLabel#paramFieldLabel {
                color: #26364d;
                font-size: 13px;
            }
            QLabel#paramUnitLabel {
                color: #66778d;
                font-size: 12px;
            }
            QLabel#paramRestartTag {
                color: #9a6700;
                background: #fff8c5;
                border: 1px solid #e6d178;
                border-radius: 4px;
                padding: 1px 5px;
                font-size: 11px;
            }
            QLineEdit#paramInput, QComboBox#paramCombo {
                background: #ffffff;
                border: 1px solid #c9d5e6;
                border-radius: 5px;
                padding: 4px 8px;
                min-height: 24px;
                color: #162236;
            }
            QLineEdit#paramInput:focus, QComboBox#paramCombo:focus {
                border: 1px solid #2b7bf4;
            }
            QLineEdit#paramInput:disabled, QComboBox#paramCombo:disabled {
                background: #f3f6fa;
                color: #94a3b8;
                border-color: #e2e8f0;
            }
            QLineEdit#paramInput[invalid="true"] {
                border: 1.5px solid #d92d20;
                background: #fff5f5;
            }
            QCheckBox#paramSwitch {
                color: #162236;
                font-size: 13px;
                spacing: 6px;
            }
            QCheckBox#paramSwitch:disabled {
                color: #94a3b8;
            }
            QPushButton#paramPrimaryBtn {
                background: #2b7bf4;
                color: #ffffff;
                border: 1px solid #1f6ae0;
                border-radius: 6px;
                padding: 6px 16px;
                font-weight: 600;
            }
            QPushButton#paramPrimaryBtn:hover {
                background: #1f6ae0;
            }
            QPushButton#paramPrimaryBtn:disabled {
                background: #cbd5e1;
                border-color: #cbd5e1;
                color: #ffffff;
            }
            QPushButton#paramSecondaryBtn {
                background: #ffffff;
                color: #26364d;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 5px 12px;
            }
            QPushButton#paramSecondaryBtn:hover {
                background: #f1f5f9;
            }
            QPushButton#paramSecondaryBtn:disabled {
                color: #94a3b8;
                background: #f8fafc;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # --- Top bar: status + template picker ---
        top_bar = QFrame()
        top_bar.setObjectName('paramTopBar')
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(14, 10, 14, 10)
        top_layout.setSpacing(8)

        self.parameter_state = QLabel('当前运行配置')
        self.parameter_state.setStyleSheet('color: #1e3a5f; font-weight: 600;')
        top_layout.addWidget(self.parameter_state)
        top_layout.addStretch(1)

        top_layout.addWidget(QLabel('参数模板：'))
        self.template_picker = QComboBox()
        self.template_picker.setPlaceholderText('选择模板')
        self.template_picker.setMinimumWidth(180)
        top_layout.addWidget(self.template_picker)

        apply_tpl = QPushButton('应用模板')
        apply_tpl.setObjectName('paramSecondaryBtn')
        apply_tpl.clicked.connect(self.apply_template_requested.emit)
        top_layout.addWidget(apply_tpl)

        save_tpl = QPushButton('存为模板')
        save_tpl.setObjectName('paramSecondaryBtn')
        save_tpl.clicked.connect(self.save_template_requested.emit)
        top_layout.addWidget(save_tpl)

        del_tpl = QPushButton('删除')
        del_tpl.setObjectName('paramSecondaryBtn')
        del_tpl.clicked.connect(self.delete_template_requested.emit)
        top_layout.addWidget(del_tpl)

        root.addWidget(top_bar)

        # --- Scrollable cards ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName('workbenchScroll')

        content = QWidget()
        content.setObjectName('workspacePage')
        cards_layout = QVBoxLayout(content)
        cards_layout.setContentsMargins(0, 0, 6, 4)
        cards_layout.setSpacing(12)

        self._build_risk_card(cards_layout)
        self._build_entry_window_card(cards_layout)
        self._build_protection_card(cards_layout)
        self._build_execution_card(cards_layout)
        self._build_advanced_card(cards_layout)
        cards_layout.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        # --- Sticky bottom action bar ---
        bottom_bar = QFrame()
        bottom_bar.setObjectName('paramBottomBar')
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(14, 10, 14, 10)
        bottom_layout.setSpacing(10)

        status_col = QVBoxLayout()
        status_col.setContentsMargins(0, 0, 0, 0)
        status_col.setSpacing(2)
        self.dirty_label = QLabel('当前无未保存修改')
        self.dirty_label.setStyleSheet('color: #66778d; font-size: 12px;')
        self.feedback_label = QLabel('')
        self.feedback_label.setWordWrap(True)
        self.feedback_label.hide()
        status_col.addWidget(self.dirty_label)
        status_col.addWidget(self.feedback_label)
        bottom_layout.addLayout(status_col, 1)

        self.revert_button = QPushButton('撤销修改')
        self.revert_button.setObjectName('paramSecondaryBtn')
        self.revert_button.clicked.connect(self.revert_requested.emit)
        bottom_layout.addWidget(self.revert_button)

        more_btn = QPushButton('更多工具 ▾')
        more_btn.setObjectName('paramSecondaryBtn')
        more_menu = QMenu(more_btn)
        more_menu.addAction('基准对比与高级管理（参数中心）', self.open_center_requested.emit)
        more_menu.addAction('导入原版 DSV 配置文件…', self.import_dsv_requested.emit)
        more_menu.addAction('导出当前业务参数…', self.export_file_requested.emit)
        more_btn.setMenu(more_menu)
        bottom_layout.addWidget(more_btn)

        self.save_button = QPushButton('保存并生效')
        self.save_button.setObjectName('paramPrimaryBtn')
        self.save_button.clicked.connect(self.save_requested.emit)
        bottom_layout.addWidget(self.save_button)

        root.addWidget(bottom_bar)

    # --- Widget builders ---
    def _make_input(
        self,
        key: str,
        label: str,
        kind: str,
        card: _SectionCard,
        *,
        unit: str = '',
        tooltip: str = '',
        width: int = 110,
        restart_only: bool = False,
    ) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        lbl = QLabel(label)
        lbl.setObjectName('paramFieldLabel')
        lbl.setMinimumWidth(116)
        if tooltip:
            lbl.setToolTip(tooltip)
        row.addWidget(lbl)

        if kind == 'close_mode':
            widget: QWidget = QComboBox()
            widget.setObjectName('paramCombo')
            widget.setEditable(True)
            widget.addItems(_CLOSE_PRICE_MODES)
            widget.setFixedWidth(max(width, 110))
            widget.currentTextChanged.connect(lambda _text: self._on_value_changed(key))
        else:
            widget = QLineEdit()
            widget.setObjectName('paramInput')
            widget.setFixedWidth(width)
            widget.textChanged.connect(lambda _text: self._on_value_changed(key))
        if tooltip:
            widget.setToolTip(tooltip)
        row.addWidget(widget)

        if unit:
            unit_lbl = QLabel(unit)
            unit_lbl.setObjectName('paramUnitLabel')
            row.addWidget(unit_lbl)
        if restart_only:
            tag = QLabel('需重启')
            tag.setObjectName('paramRestartTag')
            tag.setToolTip('修改此项后需要重启后台服务才能生效')
            row.addWidget(tag)
        row.addStretch(1)

        self.parameter_fields[key] = widget
        self._field_types[key] = kind
        self._field_labels[key] = label
        self._field_cards[key] = card
        return holder

    def _make_switch(
        self,
        key: str,
        label: str,
        card: _SectionCard,
        *,
        tooltip: str = '',
    ) -> QCheckBox:
        check = QCheckBox(label)
        check.setObjectName('paramSwitch')
        if tooltip:
            check.setToolTip(tooltip)
        check.toggled.connect(lambda checked, k=key: self._on_switch_toggled(k, checked))
        self.parameter_switches[key] = check
        self._field_types[key] = 'bool'
        self._field_labels[key] = label
        self._field_cards[key] = card
        return check

    def _link_dependent(self, switch_key: str, *widgets: QWidget):
        self._switch_dependents.setdefault(switch_key, []).extend(widgets)

    def _make_switch_with_inline(
        self,
        switch_key: str,
        switch_label: str,
        card: _SectionCard,
        *,
        switch_tip: str = '',
        inline_specs: tuple = (),
    ) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        check = self._make_switch(switch_key, switch_label, card, tooltip=switch_tip)
        row.addWidget(check)

        for spec in inline_specs:
            if spec[0] == 'switch':
                _, sub_key, sub_label, sub_tip = spec
                sub_chk = self._make_switch(sub_key, sub_label, card, tooltip=sub_tip)
                row.addWidget(sub_chk)
                self._link_dependent(switch_key, sub_chk)
            else:
                _, sub_key, sub_label, sub_kind, sub_unit, sub_tip, sub_width = spec
                if sub_label:
                    lbl = QLabel(sub_label)
                    lbl.setObjectName('paramUnitLabel')
                    row.addWidget(lbl)
                edit = QLineEdit()
                edit.setObjectName('paramInput')
                edit.setFixedWidth(sub_width)
                if sub_tip:
                    edit.setToolTip(sub_tip)
                edit.textChanged.connect(lambda _t, k=sub_key: self._on_value_changed(k))
                self.parameter_fields[sub_key] = edit
                self._field_types[sub_key] = sub_kind
                self._field_labels[sub_key] = sub_label or switch_label
                self._field_cards[sub_key] = card
                row.addWidget(edit)
                self._link_dependent(switch_key, edit)
                if sub_unit:
                    u = QLabel(sub_unit)
                    u.setObjectName('paramUnitLabel')
                    row.addWidget(u)
        row.addStretch(1)
        return holder

    def _add_two_col_grid(self, card: _SectionCard, widgets: list[QWidget]):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        for idx, widget in enumerate(widgets):
            grid.addWidget(widget, idx // 2, idx % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        card.body_layout.addLayout(grid)

    # --- 5 Semantic Cards ---
    def _build_risk_card(self, parent_layout: QVBoxLayout):
        card = _SectionCard('1. 资金与风控', '控制手数上限、账户风险度、单笔风险及亏损停开')
        self._add_two_col_grid(card, [
            self._make_input('risk_per_trade_ratio', '单笔风险比例', 'ratio', card,
                             tooltip='按权益自动计算手数时的单笔风险占比（如 0.002 = 0.2%）'),
            self._make_input('max_volume', '单品种最大手数', 'int', card, unit='手',
                             tooltip='单次开仓允许的最大手数上限'),
            self._make_input('risk_reference_equity', '参考权益', 'decimal', card, unit='元',
                             tooltip='0 表示使用实时账户权益，大于 0 时按此固定权益算量'),
            self._make_input('max_risk_ratio', '柜台风险度上限', 'ratio', card,
                             tooltip='账户风险度达到该比例时禁止继续开新仓（如 0.85）'),
            self._make_input('total_margin_cap', '总保证金占用上限', 'ratio', card,
                             tooltip='所有持仓保证金占账户权益上限（如 0.7）'),
            self._make_input('max_daily_orders', '每日最大报单数', 'int', card, unit='笔',
                             tooltip='每品种每日最多允许的策略开仓次数'),
            self._make_input('max_net_loss_stops', '日停净亏倍数', 'int', card, unit='倍止损',
                             tooltip='当日净亏达到该倍数的单笔止损额时触发日停（0 表示不限）'),
            self._make_input('month_stop_days', '月度连续亏损日停', 'int', card, unit='天',
                             tooltip='连续亏损达到该交易日数后触发月停'),
            self._make_switch_with_inline(
                'max_symbols_enabled', '限制同时持仓品种数', card,
                switch_tip='启用后，同时持仓品种达到上限时不再开新品种',
                inline_specs=(('input', 'max_symbols_on', '最多', 'int', '个', '允许同时持仓的品种上限', 64),),
            ),
            self._make_switch_with_inline(
                'rollover_stop_enabled', '合约到期前停开', card,
                switch_tip='临近交割到期前自动禁止新开仓',
                inline_specs=(('input', 'min_days_to_expiry', '提前', 'int', '天', '距到期少于等于该天数时停开', 64),),
            ),
            self._make_switch('min_one_lot_fallback', '额度不足时保底开 1 手', card,
                              tooltip='自动算量不足 1 手但资金足够时，保底按 1 手开仓'),
        ])
        parent_layout.addWidget(card)

    def _build_entry_window_card(self, parent_layout: QVBoxLayout):
        card = _SectionCard('2. 开仓与时间窗口', '控制集合竞价、开盘等待、收盘强平及冷却规则')
        self._add_two_col_grid(card, [
            self._make_switch_with_inline(
                'no_open_after_open', '开盘后禁开', card,
                switch_tip='开启后，开盘前 N 分钟内不进行普通开仓；关闭后进入连续交易即可开仓',
                inline_specs=(('input', 'no_open_after_open_minutes', '等待', 'decimal', '分钟', '开盘后等待不开仓的分钟数', 64),),
            ),
            self._make_switch('participate_auction', '参与集合竞价', card,
                              tooltip='仅作用于 08:55–08:59 / 20:55–20:59 集合竞价窗口；不影响连续交易首分钟'),
            self._make_input('no_open_before_close_minutes', '收盘前禁开', 'decimal', card, unit='分钟',
                             tooltip='临近收盘前该分钟数内禁止新开仓'),
            self._make_input('close_before_seconds', '收盘强平提前量', 'int', card, unit='秒',
                             tooltip='距收盘剩余该秒数时启动自动收盘平仓'),
            self._make_input('night_session_close', '夜盘收盘时间', 'time', card,
                             tooltip='格式 HH:MM（如 23:00），修改后需重启后台服务生效', restart_only=True),
            self._make_input('close_price_mode', '收盘报价模式', 'close_mode', card,
                             tooltip='收盘或平仓时使用的默认报价方式（如 对手价）'),
            self._make_switch_with_inline(
                'stop_cooldown_enabled', '止损后同向冷却', card,
                switch_tip='止损平仓后，在冷却期内禁止同方向再次开仓',
                inline_specs=(('input', 'stop_cooldown_minutes', '冷却', 'decimal', '分钟', '止损后的同向冷却分钟数', 64),),
            ),
            self._make_switch('require_trigger_update', '止损后要求触发价更新', card,
                              tooltip='同向止损后，若触发价未变化则跳过本次开仓'),
            self._make_switch('cancel_on_stop', '停止策略时自动撤单', card,
                              tooltip='点击“停止策略 (F6)”时自动撤销策略活动挂单'),
        ])
        card.body_layout.addWidget(self._make_switch_with_inline(
            'continuation_entry', '启用早盘延续开', card,
            switch_tip='利用夜盘区间与早盘跳空进行延续方向入场',
            inline_specs=(
                ('switch', 'continuation_ignore_no_open', '豁免开盘等待', '延续开不受“开盘后禁开”分钟数限制'),
                ('switch', 'cont_stop_day_stop', '延续开止损后日停', '延续开触发止损后当日不再开新仓'),
            ),
        ))
        parent_layout.addWidget(card)

    def _build_protection_card(self, parent_layout: QVBoxLayout):
        card = _SectionCard('3. 止盈、止损与利润保护', '80% 利润保护、移动回撤止盈与半仓减仓规则')
        card.body_layout.addWidget(self._make_switch_with_inline(
            'protect_80_enabled', '启用 80% 利润保护', card,
            switch_tip='浮盈达到激活比例后锁定最低留利比例',
            inline_specs=(
                ('input', 'protect_80_ratio', '激活比例', 'ratio', '', '浮盈达到止盈目标的该比例时激活保护（如 0.8）', 68),
                ('input', 'protect_keep_ratio', '留利比例', 'ratio', '', '激活保护后至少保留的利润比例（如 0.5）', 68),
                ('switch', 'protect_stop_day', '触发保护后日停', '保护平仓触发后当日停止开新仓'),
            ),
        ))
        self._add_two_col_grid(card, [
            self._make_input('trailing_activate_ratio', '移动止盈激活比例', 'ratio', card,
                             tooltip='浮盈达到止盈目标的该比例时激活移动回撤止盈'),
            self._make_input('trailing_pullback_ratio', '移动止盈回撤比例', 'ratio', card,
                             tooltip='从最高浮盈回撤达到该比例时触发移动止盈'),
            self._make_switch('stop_day_on_trailing', '移动止盈后日停', card,
                              tooltip='触发移动止盈平仓后当日不再开新仓'),
            self._make_switch('manual_close_day_stop', '手动平仓后日停', card,
                              tooltip='手动平仓后当日不再自动开新仓'),
            self._make_input('manual_tp_drop_ratio', '手动止盈下探比例', 'ratio', card,
                             tooltip='手动止盈时的回撤容忍比例'),
        ])
        card.body_layout.addWidget(self._make_switch_with_inline(
            'half_close_enabled', '启用半仓减仓', card,
            switch_tip='盈利到达指定比例时先平掉一半仓位',
            inline_specs=(
                ('input', 'half_close_ratio', '减仓比例', 'ratio', '', '全局半仓减仓比例（如 0.5）', 68),
                ('switch', 'half_breakeven', '半仓后保本', '半仓减仓成交后将止损推进至保本位'),
                ('switch', 'half_stage2', '二次半仓', '启用第二档半仓减仓'),
                ('input', 'half_stage2_trigger_ratio', '二档触发比例', 'ratio', '', '二阶段半仓触发比例（如 0.6）', 68),
            ),
        ))
        self._link_dependent('half_stage2', self.parameter_fields['half_stage2_trigger_ratio'])
        parent_layout.addWidget(card)

    def _build_execution_card(self, parent_layout: QVBoxLayout):
        card = _SectionCard('4. 滑点与执行偏好', '控制追价上限、报价策略与成交滑点保护')
        self._add_two_col_grid(card, [
            self._make_input('entry_slip_ratio', '普通入场滑点比例', 'ratio', card,
                             tooltip='普通时段允许偏离触发价的最大比例（如 0.03）'),
            self._make_input('chase_window_minutes', '开盘追单窗口', 'decimal', card, unit='分钟',
                             tooltip='开盘后该分钟数内使用开盘追单滑点比例'),
            self._make_input('chase_slip_ratio', '开盘追单滑点比例', 'ratio', card,
                             tooltip='开盘追单窗口内允许的最大滑点比例（如 0.05）'),
            self._make_switch('no_chase_after_act', '越过激活位后不追价', card,
                              tooltip='行情已经越过止盈激活位时，放弃本次追单开仓'),
            self._make_switch('passive_entry_first', '被动限价优先', card,
                              tooltip='开仓优先挂理论价等待成交，而非直接对手价追单'),
            self._make_switch('aggressive_mode', '激进模式', card,
                              tooltip='启用激进手数/执行模式'),
            self._make_switch('chase_base_now', '追价以现价为基准', card,
                              tooltip='计算追价上限时以现价为基准'),
        ])
        card.body_layout.addWidget(self._make_switch_with_inline(
            'entry_slip_abort', '成交滑点过大自动退出', card,
            switch_tip='开仓成交价偏离理论价过大时触发保护性退出',
            inline_specs=(
                ('input', 'entry_slip_abort_ratio', '滑点退出比例', 'ratio', '', '允许的最大成交滑点占止损比例（如 0.4）', 68),
                ('switch', 'entry_slip_abort_stop_day', '滑点退出后日停', '因滑点退出后当日不再开新仓'),
            ),
        ))
        parent_layout.addWidget(card)

    def _build_advanced_card(self, parent_layout: QVBoxLayout):
        card = _SectionCard(
            '5. 高级与系统参数（低频项）',
            '断流保护、首分钟微观规则、实盘保护预填与节假日',
            collapsible=True,
            collapsed=True,
        )
        self._advanced_card = card
        widgets = [
            self._make_input('stale_force_close_seconds', '行情断流强平超时', 'decimal', card, unit='秒',
                             tooltip='交易时段内行情长时间无更新触发保护平仓的秒数'),
            self._make_switch('stale_close_day_stop', '断流强平后日停', card,
                              tooltip='触发断流强平后当日不再开新仓'),
            self._make_input('position_sync_seconds', '持仓同步检测周期', 'decimal', card, unit='秒'),
            self._make_input('max_quote_age_seconds', '最大行情年龄', 'decimal', card, unit='秒',
                             tooltip='超过该秒数的过旧行情不用于开仓（0 表示不限）'),
            self._make_input('open_jump_block_ratio', '首分钟跳空日停比例', 'ratio', card,
                             tooltip='开盘首分钟偏离触发线达到止盈距离的该比例时触发当日停开'),
            self._make_input('hysteresis_points', '全局滞回点数', 'decimal', card, unit='点',
                             tooltip='0 表示自动使用品种止损偏移作为滞回距离'),
            self._make_input('no_chase_tolerance', '追价容差', 'decimal', card),
            self._make_input('limit_fallback_ratio', '限价后备比例', 'ratio', card),
            self._make_input('open_gap_max_ratio', '开盘价差比例', 'ratio', card),
            self._make_input('entry_confirm_timeout', '入场确认超时', 'int', card, unit='秒'),
            self._make_input('margin_rate_alert_threshold', '保证金率变动提醒', 'decimal', card),
            self._make_switch('tick_diag', '记录逐行情诊断日志', card,
                              tooltip='仅排障时开启，会显著增加日志量'),
        ]
        if not self._runtime_mode:
            widgets.append(self._make_switch('auto_start_on_launch', '打开软件自动启动', card))
        self._add_two_col_grid(card, widgets)

        card.body_layout.addWidget(self._make_switch_with_inline(
            'live_protect', '实盘保护（预填）', card,
            switch_tip='实盘保护开关及预填上限',
            inline_specs=(
                ('input', 'live_max_lots', '手数上限', 'int', '手', '', 64),
                ('input', 'live_daily_loss_limit', '日亏上限', 'decimal', '元', '', 84),
                ('switch', 'live_first_one_lot', '首次一手限制', ''),
            ),
        ))

        holidays_holder = self._make_input(
            'holidays', '自定义节假日', 'holidays', card,
            tooltip='多个日期用英文逗号分隔，格式 YYYY-MM-DD（如 2026-10-01,2026-10-02）',
            width=360,
            restart_only=True,
        )
        card.body_layout.addWidget(holidays_holder)

        if not self._runtime_mode:
            sched_row = QWidget()
            sl = QHBoxLayout(sched_row)
            sl.setContentsMargins(0, 4, 0, 0)
            sl.setSpacing(8)
            self.schedule_enabled_check = QCheckBox('启用定时启动')
            self.schedule_enabled_check.setObjectName('paramSwitch')
            self.schedule_enabled_check.toggled.connect(lambda _: self._refresh_dirty_state())
            sl.addWidget(self.schedule_enabled_check)
            for key, label, width in (
                ('night_start_time', '夜盘', 72),
                ('day_start_time', '日盘', 72),
                ('catch_up_minutes', '补启动(分)', 64),
            ):
                sl.addWidget(QLabel(label))
                edit = QLineEdit()
                edit.setObjectName('paramInput')
                edit.setFixedWidth(width)
                edit.textChanged.connect(lambda _t: self._refresh_dirty_state())
                self.schedule_fields[key] = edit
                sl.addWidget(edit)
            if self._show_timed_start:
                self.timed_start_button = QPushButton('安排定时启动')
                self.timed_start_button.setObjectName('paramSecondaryBtn')
                self.timed_start_button.clicked.connect(self.timed_start_requested.emit)
                sl.addWidget(self.timed_start_button)
            sl.addStretch(1)
            card.body_layout.addWidget(sched_row)

        parent_layout.addWidget(card)

    # --- Data binding & validation ---
    @staticmethod
    def _format_value(value, kind: str) -> str:
        if kind == 'holidays':
            return ', '.join(str(item) for item in (value or ()))
        return str(value)

    def load_profile(
        self,
        profile: DsvProfile,
        *,
        active_profile: DsvProfile | None = None,
        profile_source: str | None = None,
        saved_next_boot_hash: str | None = None,
        live_save: bool | None = None,
    ):
        self._base_profile = profile
        if active_profile is not None:
            self._active_profile = active_profile
        elif self._active_profile is None:
            self._active_profile = profile
        if profile_source is not None:
            self._profile_source = profile_source
        if saved_next_boot_hash is not None or active_profile is not None:
            self._saved_next_boot_hash = saved_next_boot_hash
        if live_save is not None:
            self._live_save = live_save

        settings = profile.to_record()['settings']
        trade = settings['trade']
        self._loading = True
        try:
            for key, widget in self.parameter_fields.items():
                if key not in trade:
                    continue
                text = self._format_value(trade[key], self._field_types[key])
                if isinstance(widget, QComboBox):
                    widget.setCurrentText(text)
                else:
                    widget.setText(text)
                    self._set_invalid(widget, False)
            for key, check in self.parameter_switches.items():
                if key in trade:
                    check.setChecked(bool(trade[key]))
            if self.schedule_enabled_check is not None:
                self.schedule_enabled_check.setChecked(bool(settings['schedule']['enabled']))
            for key, field in self.schedule_fields.items():
                field.setText(str(settings['schedule'][key]))
                self._set_invalid(field, False)
            self._apply_all_dependents()
        finally:
            self._loading = False
        self._refresh_dirty_state()

    def _apply_all_dependents(self):
        for sw_key, dependents in self._switch_dependents.items():
            switch = self.parameter_switches.get(sw_key)
            if switch is None:
                continue
            enabled = switch.isChecked()
            for widget in dependents:
                widget.setEnabled(enabled)
        if 'half_close_enabled' in self.parameter_switches and 'half_stage2' in self.parameter_switches:
            stage2_ready = (
                self.parameter_switches['half_close_enabled'].isChecked()
                and self.parameter_switches['half_stage2'].isChecked()
            )
            self.parameter_fields['half_stage2_trigger_ratio'].setEnabled(stage2_ready)

    def _on_switch_toggled(self, _key: str, _checked: bool):
        if self._loading:
            return
        self._apply_all_dependents()
        self._refresh_dirty_state()

    def _on_value_changed(self, key: str):
        if self._loading:
            return
        widget = self.parameter_fields.get(key)
        if isinstance(widget, QLineEdit):
            self._set_invalid(widget, False)
        self._refresh_dirty_state()

    @staticmethod
    def _set_invalid(widget: QLineEdit, invalid: bool):
        if bool(widget.property('invalid')) != invalid:
            widget.setProperty('invalid', invalid)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _parse_widget(self, key: str):
        kind = self._field_types[key]
        label = self._field_labels[key]
        widget = self.parameter_fields[key]
        raw = widget.currentText().strip() if isinstance(widget, QComboBox) else widget.text().strip()
        if kind == 'int':
            try:
                value = int(raw)
            except ValueError:
                raise _ValidationError(key, f'「{label}」必须为非负整数') from None
            if value < 0:
                raise _ValidationError(key, f'「{label}」不能为负数')
            return value
        if kind in {'decimal', 'ratio'}:
            try:
                dec = Decimal(raw)
            except (InvalidOperation, ValueError):
                raise _ValidationError(key, f'「{label}」必须为有效数字') from None
            if not dec.is_finite() or dec < 0:
                raise _ValidationError(key, f'「{label}」必须为非负数字')
            if kind == 'ratio' and dec > 1:
                raise _ValidationError(key, f'「{label}」为比例值，必须在 0 到 1 之间')
            return format(dec.normalize(), 'f') if dec != 0 else '0'
        if kind == 'time':
            if not _TIME_RE.fullmatch(raw):
                raise _ValidationError(key, f'「{label}」格式必须为 HH:MM（例如 23:00）')
            return raw
        if kind == 'close_mode':
            if not raw or len(raw) > 24:
                raise _ValidationError(key, f'「{label}」不能为空且不超过 24 个字符')
            return raw
        if kind == 'holidays':
            if not raw:
                return []
            items = [token.strip() for token in raw.replace('，', ',').split(',') if token.strip()]
            seen = []
            for item in items:
                try:
                    if date.fromisoformat(item).isoformat() != item:
                        raise ValueError
                except ValueError:
                    raise _ValidationError(key, f'「{label}」中的日期 “{item}” 格式无效，应为 YYYY-MM-DD') from None
                if item not in seen:
                    seen.append(item)
            return seen
        return raw

    def build_profile(self, base_profile: DsvProfile | None = None) -> DsvProfile:
        source = base_profile or self._base_profile or self._active_profile
        if source is None:
            raise TraderError('E_CONFIG', '尚未加载策略参数')
        record = copy.deepcopy(source.to_record())
        trade = record['settings']['trade']
        for key in self.parameter_fields:
            if key in trade:
                trade[key] = self._parse_widget(key)
        for key, check in self.parameter_switches.items():
            if key in trade:
                trade[key] = bool(check.isChecked())
        if self.schedule_enabled_check is not None:
            record['settings']['schedule']['enabled'] = bool(self.schedule_enabled_check.isChecked())
        for key, field in self.schedule_fields.items():
            text = field.text().strip()
            if key == 'catch_up_minutes':
                try:
                    val = int(text)
                    if val < 0:
                        raise ValueError
                except ValueError:
                    raise _ValidationError(key, '「补启动(分)」必须为非负整数') from None
                record['settings']['schedule'][key] = val
            else:
                if not _TIME_RE.fullmatch(text):
                    raise _ValidationError(key, f'定时参数「{key}」格式必须为 HH:MM')
                record['settings']['schedule'][key] = text
        return DsvProfile.from_record(record)

    def try_build_profile(self, base_profile: DsvProfile | None = None) -> DsvProfile | None:
        try:
            candidate = self.build_profile(base_profile)
            self.set_feedback('')
            return candidate
        except _ValidationError as err:
            self.set_feedback(str(err), error=True)
            card = self._field_cards.get(err.key)
            if card is not None:
                card.ensure_expanded()
            widget = self.parameter_fields.get(err.key) or self.schedule_fields.get(err.key)
            if isinstance(widget, QLineEdit):
                self._set_invalid(widget, True)
                widget.setFocus()
                widget.selectAll()
            elif widget is not None:
                widget.setFocus()
            return None
        except TraderError as err:
            self.set_feedback(f'参数校验未通过：{err}', error=True)
            return None

    def _compute_changed_keys(self) -> tuple[list[str], bool]:
        if self._active_profile is None:
            return [], False
        active_settings = self._active_profile.to_record()['settings']
        active_trade = active_settings['trade']
        changed: list[str] = []
        needs_restart = False
        for key, widget in self.parameter_fields.items():
            if key not in active_trade:
                continue
            cur = widget.currentText().strip() if isinstance(widget, QComboBox) else widget.text().strip()
            base_text = self._format_value(active_trade[key], self._field_types[key])
            if cur != base_text:
                changed.append(key)
                if key in _RESTART_TRADE_KEYS:
                    needs_restart = True
        for key, check in self.parameter_switches.items():
            if key in active_trade and bool(check.isChecked()) != bool(active_trade[key]):
                changed.append(key)
        if self.schedule_enabled_check is not None:
            if bool(self.schedule_enabled_check.isChecked()) != bool(active_settings['schedule']['enabled']):
                changed.append('schedule.enabled')
                needs_restart = True
        for key, field in self.schedule_fields.items():
            if field.text().strip() != str(active_settings['schedule'][key]):
                changed.append(f'schedule.{key}')
                needs_restart = True
        # Also detect symbol changes staged in the Monitor Contracts tab
        if self._base_profile is not None:
            if self._base_profile.to_record()['settings']['symbols'] != active_settings['symbols']:
                changed.append('symbols')
        return changed, needs_restart

    def _refresh_dirty_state(self):
        changed, needs_restart = self._compute_changed_keys()
        if not changed:
            self.dirty_label.setText('当前无未保存修改')
            self.dirty_label.setStyleSheet('color: #66778d; font-size: 12px;')
            self.revert_button.setEnabled(False)
            self.save_button.setText('保存并生效' if self._live_save else '保存参数')
        else:
            suffix = '（含需重启生效项）' if needs_restart else ('（保存后立即热生效）' if self._live_save else '（保存后下次启动生效）')
            self.dirty_label.setText(f'● 已修改 {len(changed)} 项{suffix}')
            self.dirty_label.setStyleSheet('color: #075de5; font-weight: 600; font-size: 12px;')
            self.revert_button.setEnabled(True)
            self.save_button.setText(f'保存并生效 ({len(changed)})' if self._live_save else f'保存参数 ({len(changed)})')

    def set_feedback(self, message: str, *, error: bool = False):
        self.feedback_label.setText(message)
        self.feedback_label.setStyleSheet(
            'color: #b42318; font-weight: 600; font-size: 12px;' if error
            else 'color: #08764d; font-weight: 600; font-size: 12px;' if message else ''
        )
        self.feedback_label.setVisible(bool(message))

    def has_unsaved_changes(self) -> bool:
        changed, _ = self._compute_changed_keys()
        return bool(changed)
