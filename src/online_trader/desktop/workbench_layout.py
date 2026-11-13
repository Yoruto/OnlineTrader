"""Trading workspace. Widgets consume the accepted owner view."""
from collections import deque
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
import re
from typing import NamedTuple

from PySide6.QtCore import QBuffer, QIODevice, QItemSelectionModel, QPointF, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import load_profile, publish_new_bytes
from online_trader.configuration.rollover import expiry_date, expiry_warning, preview_rollover
from online_trader.contracts.codec import strict_loads
from online_trader.desktop.reports import EquityChart
from online_trader.errors import TraderError


BEIJING = timezone(timedelta(hours=8))
ENTRY_MESSAGES = {
    'GEOMETRY_BLOCK': '几何条件不满足，暂不建仓',
    'WAITING_FRESH_QUOTE': '等待行情',
    'MARKET_PATH_INCOMPLETE': '行情路径待补齐',
    'OPENING_WAIT': '开盘等待',
    'CLOSE_SESSION_NO_OPEN': '收盘前禁开',
    'RECONCILIATION_REQUIRED': '柜台待核对',
    'CONTROL_BARRIER': '开仓权限受限',
    'OBSERVATION_ONLY_BACKLOG': '正在补齐行情',
    'DSV_QUOTE_AGE_EXCEEDED': '行情已滞后，暂停开新仓',
    'TAKE_PROFIT_OUTSIDE_PRICE_LIMIT': '理论止盈超过涨跌停',
    'LAST_NEAR_PRICE_LIMIT': '最新价接近涨跌停',
    'SAME_DIRECTION_WITHIN_ONE_SECOND': '同方向报单间隔不足 1 秒',
}
ACTIVATION_MESSAGES = {
    'LONG:NO_CHASE_AFTER_ACT': '当日行情已跑过多头激活位',
    'SHORT:NO_CHASE_AFTER_ACT': '当日行情已跑过空头激活位',
}
STRATEGY_LOG_REASONS = {'GEOMETRY_BLOCK', 'OPENING_WAIT', 'CLOSE_SESSION_NO_OPEN', 'DSV_QUOTE_AGE_EXCEEDED',
                        'NET_LOSS_LIMIT', 'CANDLE_OPPORTUNITY_CONSUMED', 'OBSERVATION_ONLY_BACKLOG',
                        'LONG:TRIGGER_NOT_REACHED', 'SHORT:TRIGGER_NOT_REACHED', 'DUPLICATE_MARKET_EVENT',
                        *ACTIVATION_MESSAGES}
ORDER_HEADERS = ('报单号','合约','方向','开平','委托量','未成交','委托价','状态','报单时间','消息')
TRADE_HEADERS = ('成交号','合约','方向','开平','手数','成交价','手续费','成交时间')
POSITION_HEADERS = ('合约','方向','持仓量','成交价','止损价','止盈价','实时价格',
                    '盈利价差','持仓盈亏','浮盈比例','保证金','手续费')
SYMBOL_HEADERS = ('品种名', '合约代码', '开仓偏移', '止损偏移', '止盈偏移', '下单手数', '半仓参数', '滞回点')
LOG_EVENT_FILTERS = {
    '开仓': {'open', 'auto'}, '平仓': {'close', 'auto'}, '错误': {'error'}, '手动': {'manual'},
}
LOG_TAGS = {'开仓':'open','平仓':'close','自动':'auto','错误':'error','手动':'manual'}


class WorkbenchLogLine(NamedTuple):
    text: str
    category: str | None = None

TRADE_FIELDS = (
    ('max_volume','最大手数'), ('max_risk_ratio','风险度上限'), ('max_daily_orders','最大报单'),
    ('max_net_loss_stops','净亏倍数'), ('close_before_seconds','收盘秒数'),
    ('trailing_activate_ratio','止盈激活'), ('trailing_pullback_ratio','止盈回调'), ('risk_per_trade_ratio','单笔风险'),
    ('stale_force_close_seconds','断流秒'), ('risk_reference_equity','参考权益'), ('night_session_close','夜盘收盘'),
    ('entry_slip_ratio','滑点比例'), ('chase_window_minutes','开盘追单分'), ('chase_slip_ratio','追单滑点比例'),
    ('manual_tp_drop_ratio','手动止盈下探'), ('total_margin_cap','合计保证金'),
    ('no_open_before_close_minutes','禁开分钟'), ('position_sync_seconds','检测秒'),
    ('no_open_after_open_minutes','开盘后禁开分'), ('max_symbols_on','最大持仓品种'),
    ('stop_cooldown_minutes','冷却分钟'), ('half_close_ratio','半仓比例'),
    ('protect_80_ratio','保护触发比例'), ('protect_keep_ratio','保护留利比例'),
)
TRADE_SWITCHES = (
    ('participate_auction','参与集合竞价'), ('stop_day_on_trailing','移动止盈后日停'),
    ('stale_close_day_stop','断流强平后日停'), ('aggressive_mode','进取策略'),
    ('auto_start_on_launch','打开软件自动启动'), ('passive_entry_first','被动限价优先'),
    ('manual_close_day_stop','手动平仓后停开'), ('min_one_lot_fallback','额度不足至少一手'),
    ('stop_cooldown_enabled','止损后方向冷却'), ('require_trigger_update','要求触发价更新'),
    ('max_symbols_enabled','限制同时持仓品种数'), ('rollover_stop_enabled','到期停开'),
    ('half_stage2','二次半仓'), ('half_breakeven','半仓后保本'), ('continuation_entry','开盘延续开'),
    ('protect_80_enabled','80% 止盈保护'), ('protect_stop_day','触发后停开'), ('half_close_enabled','启用半仓减仓'),
)
STYLE = """
QWidget { color: #172438; }
QWidget#tradingWorkspace, QTabWidget::pane, QGroupBox { background: #f2f5f9; }
QTabWidget::pane { border: 1px solid #d7dfe9; }
QTabBar::tab { background: #e0e6ee; border: 1px solid #aab5c2; padding: 8px 17px; }
QTabBar::tab:selected { background: #235de6; color: white; font-weight: bold; }
QGroupBox { border: 1px solid #d8e1ed; margin-top: 13px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 3px; padding-right: 5px; color: #1746a1; }
QGroupBox QWidget { font-weight: normal; }
QPushButton { background: #e0e7f0; border: 1px solid #dce4ee; padding: 4px 10px; }
QPushButton:hover { background: #d4e1f4; border-color: #a8bdd9; }
QPushButton:disabled { color: #70839b; background: #e7edf4; }
QPushButton#strategyStart, QPushButton#strategyPause { font-weight: bold; padding: 9px 16px; }
QPushButton#strategyStart { background: #e52024; border-color: #e52024; color: white; }
QPushButton#strategyPause { background: #536b89; border-color: #536b89; color: white; }
QPushButton#strategyStart:disabled, QPushButton#strategyPause:disabled { background: #cbd5e3; color: #6c7e96; border-color: #cbd5e3; }
QLineEdit, QSpinBox, QComboBox { background: white; border: 1px solid #c6d4e7; padding: 2px 4px; }
QTableWidget { background: white; alternate-background-color: #f5f8fc; border: 1px solid #a9bbd3; }
QHeaderView::section { background: #1b2638; color: white; border: 0; padding: 7px 4px; font-weight: bold; }
QLabel#brand { color: #16479c; font-weight: bold; padding: 5px 4px; }
QTabWidget#workspaceNavigation::tab-bar { alignment: left; }
QScrollArea#workbenchScroll, QWidget#workbenchPage { background: #f2f5f9; border: 0; }
QLabel#homeMoney { font-size: 22px; font-weight: bold; }
QLabel#homeAccountType { color: #214da5; background: #eaf1ff; border-radius: 4px; padding: 3px 7px; }
QLabel#homeAccountType[live="true"] { color: #7c4c08; background: #fff3df; }
QLabel#homePending { color: #7c4c08; background: #fff3df; border-radius: 6px; padding: 6px 10px; }
QLabel#waiting, QLabel#blocked, QLabel#pending { padding: 7px; }
QLabel#waiting { background: #ecfaf2; color: #195d40; }
QLabel#blocked { background: #fff0f0; color: #9d252c; }
QLabel#pending { background: #fff5e8; color: #884605; }
QPlainTextEdit#runtimeLog { background: #1d202a; color: #e4ebfa; border: 1px solid #394355; font-family: Consolas; }
"""


def row(*widgets):
    layout = QHBoxLayout()
    layout.setSpacing(7)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch()
    return layout


def button(text, callback):
    control = QPushButton(text)
    control.clicked.connect(callback)
    return control


def group(title, parent_layout, *, stretch=0, horizontal=False):
    box = QGroupBox(title)
    layout = QHBoxLayout(box) if horizontal else QVBoxLayout(box)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(6)
    parent_layout.addWidget(box, stretch)
    return layout


def configure_table(table):
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(24)
    table.horizontalHeader().setMinimumSectionSize(45)
    table.horizontalHeader().setStretchLastSection(True)


def table(headers, rows=()):
    grid = QTableWidget(0, len(headers))
    grid.setHorizontalHeaderLabels(headers)
    configure_table(grid)
    grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    populate(grid, rows)
    return grid


def populate(grid, rows):
    grid.setRowCount(len(rows))
    for i, values in enumerate(rows):
        for j, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignCenter)
            item.setToolTip(str(value))
            grid.setItem(i, j, item)


def beijing(stamp):
    return datetime.fromisoformat(stamp).astimezone(BEIJING)


def suggested_lots(symbol, trade, symbol_count, account, market, reference_rule):
    """DSV's symbol-table estimate using displayed settings and proved inputs."""
    equity = Decimal(account.get('balance') or '0')
    if equity <= 0:
        equity = Decimal(trade['risk_reference_equity'])
    if equity <= 0:
        return None, '待连接或设置参考权益'
    stop = Decimal(symbol['stop_offset'])
    multiplier = Decimal(market.get('volume_multiple') or '0') if market.get('fresh') else Decimal(0)
    if multiplier <= 0 and reference_rule is not None:
        multiplier = Decimal(reference_rule.multiplier)
    if stop <= 0 or multiplier <= 0:
        return None, '等待有效止损距离和合约乘数'
    if trade['aggressive_mode']:
        last = Decimal(market.get('last') or '0') if market.get('fresh') else Decimal(0)
        if last <= 0:
            return None, '等待进取模式所需的最新价'
        margin = Decimal(market.get('margin') or '0')
        if margin <= 0:
            margin = last * multiplier * Decimal('0.15')
        cap = min(Decimal(trade['max_risk_ratio']), Decimal(trade['total_margin_cap']), Decimal(1))
        estimate = int(equity * cap / (max(1,symbol_count) * margin))
    else:
        estimate = int(equity * Decimal(trade['risk_per_trade_ratio']) / (stop * multiplier))
    return min(max(0,estimate), trade['max_volume']), '建议值按当前表单和参考数据计算，实际下单仍由原风控决定'


def native_time(value):
    if value is None or not str(value).isdecimal():
        return '—'
    try:
        return datetime.fromtimestamp(int(value)//1_000_000_000,BEIJING).strftime('%H:%M:%S')
    except (OSError,OverflowError,ValueError):
        return '—'


def display_value(value):
    return '—' if value is None or value=='' else str(value)


def native_side(item):
    direction = item.get('direction')
    if direction not in {'LONG','SHORT'}:
        return {'BUY':'买','SELL':'卖'}.get(direction)
    return '买' if (item['direction']=='LONG')==(item['effect']=='OPEN') else '卖'


def order_cells(item):
    offset = {'CLOSETODAY':'平今','CLOSE':'平仓','CLOSEYESTERDAY':'平昨','OPEN':'开仓'}
    state = {'FILLED':'全部成交','CANCELLED':'已撤单','REJECTED':'已拒绝',
             'NOT_SEEN':'等待回报','ALIVE':'挂单','ACKED':'已报'}
    native_offset = offset.get(item.get('native_offset'),'开仓' if item['effect']=='OPEN' else '平仓')
    status = state.get(item.get('broker_state'),item.get('broker_state'))
    if item.get('send_state')=='UNKNOWN':
        status = '待核对'
    quantity, filled = item.get('original_qty'), item.get('filled_qty')
    return [display_value(value) for value in (
        item.get('native_order_id'),item['contract_id'],native_side(item),native_offset,
        quantity,quantity-filled if quantity is not None and filled is not None else None,item.get('limit_price'),
        status,native_time(item.get('source_time_ns')),item.get('native_message'),
    )]


def trade_cells(item):
    return [display_value(value) for value in (
        item.get('native_trade_id') or item['trade_key'],item['contract_id'],native_side(item),
        '开仓' if item['effect']=='OPEN' else '平仓',item['quantity'],item['price'],
        item.get('commission'),native_time(item.get('source_time_ns')),
    )]


def position_cells(item):
    floating = item.get('floating_profit')
    margin = item.get('margin')
    ratio = f'{Decimal(floating)/Decimal(margin):+.2%}' if floating is not None and margin is not None and Decimal(margin)>0 else None
    return [display_value(value) for value in (
        item['contract_id'],'多' if item['direction']=='LONG' else '空',item['remaining_quantity'],
        item.get('entry_price'),item.get('stop_price'),item.get('take_profit_price'),item.get('last_price'),
        item.get('price_difference'),floating,ratio,margin,item.get('commission'),
    )]


class SampledCandles(QWidget):
    """OHLC of the quotes actually sampled by the workbench, with missing minutes left blank."""

    def __init__(self):
        super().__init__()
        self.history = {}
        self.native_series = {}
        self.code = ''
        self.setMinimumHeight(105)

    def select(self, code):
        self.code = code
        self.update()

    def show_native_bars(self, payload):
        self.native_series = {code:[(row[0],*map(Decimal,row[1:])) for row in bars]
                              for code,bars in payload.items()}
        self.update()

    def record(self, market):
        if not market.get('fresh') or not market.get('source_time_utc'):
            return
        try:
            price = Decimal(market['last'])
            minute = int(beijing(market['source_time_utc']).timestamp()) // 60
        except (TypeError, ValueError, InvalidOperation):
            return
        if not price.is_finite() or price <= 0:
            return
        values = self.history.setdefault(market['contract_id'], deque(maxlen=48))
        if values and values[-1][0] == minute:
            _, first, high, low, _ = values[-1]
            values[-1] = minute, first, max(high, price), min(low, price), price
        elif not values or minute > values[-1][0]:
            values.append((minute, price, price, price, price))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#ffffff'))
        painter.setPen(QColor('#52647d'))
        candles = self.native_series.get(self.code) or self.history.get(self.code)
        if not candles:
            painter.drawText(12, 27, '等待可用行情')
            return
        low = min(item[3] for item in candles)
        high = max(item[2] for item in candles)
        span = high-low or Decimal(1)
        last = candles[-1][0]
        plot = self.rect().adjusted(66, 12, -14, -22)
        painter.drawText(4, 18, str(high))
        painter.drawText(4, plot.bottom(), str(low))
        painter.drawText(plot.right()-38, self.height()-5, datetime.fromtimestamp(last*60, BEIJING).strftime('%H:%M'))
        painter.setPen(QColor('#d7e0eb'))
        painter.drawRect(plot)
        step = plot.width()/48
        def y(price):
            return plot.bottom()-4-(plot.height()-8)*float((price-low)/span)
        for minute, opening, maximum, minimum, closing in candles:
            if last-minute >= 48:
                continue
            x = plot.right()-(last-minute+.5)*step
            color = QColor('#c8323c' if closing >= opening else '#23845b')
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(x, y(maximum)), QPointF(x, y(minimum)))
            painter.setPen(QPen(color, max(2, step*.52)))
            painter.drawLine(QPointF(x, y(opening)), QPointF(x, y(closing)+1))


class WorkbenchLayout:
    def __init__(self, window, *, dsv_config=None, require_reviewed_profile=False):
        self.window = window
        self.buttons = window.command_buttons
        self.tables = window.tables
        self.paired = window.paired_launcher
        self.settings = QSettings('OnlineTrader', 'Workbench')
        self.dsv_config = Path(dsv_config) if dsv_config is not None and not self.paired else None
        self.require_reviewed_profile = require_reviewed_profile
        self.dsv_profile_loaded = False
        self.profile_source = '本次绑定参数' if self.paired else '历史 DSV 基准（2026-11-09）'
        if self.paired:
            self.profile = DsvProfile.from_record(strict_loads(self.paired.paths[1].read_bytes()))
        else:
            self.profile = load_confirmed_baseline()[0]
            if self.dsv_config is not None:
                try:
                    self.profile = load_profile(self.dsv_config, dsv=True)
                    self.profile_source = '本机 DSV 已保存参数'
                    self.dsv_profile_loaded = True
                except TraderError:
                    self.profile_source = 'DSV 配置读取失败 · 显示历史基准'
            elif require_reviewed_profile:
                self.profile_source = '未找到本机 DSV 配置 · 显示历史基准'
        self.symbols = self.profile.to_record()['settings']['symbols']
        self.names = {item['code']: item['name'] for item in self.symbols}
        self.activity = deque(maxlen=200)
        self.last_state = None
        self.last_log_text = None
        self.visible_log_lines = []
        self.log_clear_offsets = {}
        self.balance_history = deque(maxlen=8640)
        self.last_balance_slot = None
        self.chart_owner = None
        self.rendered_view = None
        self.last_reasons = {}
        self.manual_price_scope = None
        self.fund_values = {}
        self.home_updated_at = None

        original = window.takeCentralWidget()
        for widget in (window.connection_label, window.account_label, window.kernel_label,
                       window.funds_label, window.notice_label, window.cycles, window.refresh_button,
                       window.pause_button, window.settings_button, window.reports_button):
            widget.setParent(window)
        original.deleteLater()
        panel = QWidget()
        panel.setObjectName('tradingWorkspace')
        panel.setStyleSheet(STYLE)
        window.setCentralWidget(panel)
        self.panel = panel
        shell = QVBoxLayout(panel)
        shell.setContentsMargins(8, 4, 8, 5)
        shell.setSpacing(6)

        self.tabs = window.tabs = QTabWidget()
        self.tabs.setObjectName('workspaceNavigation')
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.ElideNone)
        self.buttons['arm'].setObjectName('strategyStart')
        window.pause_button.setObjectName('strategyPause')
        window.pause_button.setText('停止策略 (F6)')
        QShortcut(QKeySequence('F5'), panel, self.buttons['arm'].click)
        QShortcut(QKeySequence('F6'), panel, window.pause_button.click)
        self.toolbar(shell)
        self.home_tab = self.add_page(self.tabs, '策略主控', self.home_page)
        self.monitor_tab = self.add_page(self.tabs, '实时监控', self.monitor_page, scroll=True)
        self.add_page(self.tabs, '挂单汇总', self.orders_page)
        self.add_page(self.tabs, '手动下单', self.manual_page, scroll=True)
        self.log_tab = self.add_page(self.tabs, '运行日志', self.logs_page)
        self.add_page(self.tabs, '品种信息', self.instruments_page)
        self.add_page(self.tabs, '偏移计算', self.offset_page, scroll=True)
        self.add_page(self.tabs, '系统设置', self.system_page, scroll=True)
        self.account_tab = self.add_page(self.tabs, '账号信息', self.account_page, scroll=True)
        self.parameter_tab = self.add_page(self.tabs, '交易参数', self.strategy_page, scroll=True)
        self.render_parameters(self.profile)
        shell.addWidget(self.tabs, 1)
        window.notice_label.setText('未连接 · 等待启动')
        window.notice_label.setWordWrap(True)
        shell.addWidget(window.notice_label)
        window.notice_label.hide()
        self.font_size.valueChanged.connect(self.apply_font)
        self.apply_font(self.font_size.value())
        if self.settings.value('template_directory','',str):
            self.parameter_dialog()
        self.tabs.currentChanged.connect(self.page_changed)
        self.render_home()
        self.log_timer = QTimer(window)
        self.log_timer.setInterval(1800)
        self.log_timer.timeout.connect(self.update_logs)
        self.log_timer.start()
        for grid in (*self.tables.values(), window.cycles):
            configure_table(grid)

    @staticmethod
    def add_page(tabs, title, build, *, scroll=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(8)
        build(layout)
        if scroll:
            area = QScrollArea()
            area.setObjectName('workbenchScroll')
            area.setWidgetResizable(True)
            page.setObjectName('workbenchPage')
            area.setWidget(page)
            page = area
        tabs.addTab(page, title)
        return page

    def page_changed(self, _):
        self.window.notice_label.setVisible(self.tabs.currentWidget() is not self.home_tab)
        self.update_logs()

    def show_details(self, page):
        self.tabs.setCurrentWidget(page)

    def show_account_settings(self):
        self.reload_accounts()
        self.tabs.setCurrentWidget(self.account_tab)

    def toolbar(self, shell):
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 5, 0, 4)
        brand = QLabel('OnlineTrader')
        brand.setObjectName('brand')
        bar.addWidget(brand)
        bar.addStretch(1)
        bar.addWidget(self.buttons['arm'])
        bar.addWidget(self.window.pause_button)
        bar.addStretch(1)
        bar.addSpacing(8)
        bar.addWidget(QLabel('字号：'))
        self.font_size = QSpinBox()
        self.font_size.setRange(8, 13)
        self.font_size.setValue(self.settings.value('font_size', 9, int))
        bar.addWidget(self.font_size)
        bar.addWidget(button('快捷键', self.shortcuts))
        bar.addWidget(button('按屏幕自适应', self.window.showMaximized))
        shell.addLayout(bar)

    def apply_font(self, size):
        font = self.panel.font()
        font.setFamilies(['Microsoft YaHei UI', 'Segoe UI'])
        font.setPointSize(size)
        self.panel.setFont(font)
        self.settings.setValue('font_size', size)

    def shortcuts(self):
        QMessageBox.information(self.window, '快捷键', 'F5 启动策略或打开模拟连接\nF6 停止策略')

    def open_parameters(self):
        self.parameter_dialog()
        self.window.open_settings()

    def parameter_dialog(self):
        from online_trader.desktop.settings import SettingsDialog
        if self.window._settings_dialog is None:
            dialog = SettingsDialog(self.window, baseline_loader=lambda: (
                self.profile, {'rule_set_id': self.profile_source}))
            self.window._settings_dialog = dialog
            dialog.preview_changed.connect(self.render_parameters)
            dialog.templates_changed.connect(self.reload_template_picker)
            dialog.template_directory_changed.connect(lambda path:self.settings.setValue('template_directory',path))
            directory = self.settings.value('template_directory','',str)
            if directory:
                dialog.set_template_directory(directory)
        return self.window._settings_dialog

    def reload_template_picker(self):
        source = self.window._settings_dialog.template_combo
        self.template_picker.clear()
        self.template_picker.addItems([source.itemText(index) for index in range(source.count())])
        self.template_picker.setCurrentIndex(source.currentIndex())

    def choose_template_store(self, dialog):
        directory = self.settings.value('template_directory','',str)
        if directory and dialog.set_template_directory(directory):
            return True
        directory = QFileDialog.getExistingDirectory(self.window,'选择本地模板目录')
        return bool(directory and dialog.set_template_directory(directory))

    def apply_selected_template(self):
        dialog = self.parameter_dialog()
        if self.template_picker.currentIndex()<0:
            if not self.choose_template_store(dialog) or self.template_picker.currentIndex()<0:
                return
        if not dialog.apply_template(self.template_picker.currentText()):
            QMessageBox.warning(self.window,'应用模板',dialog.error_label.text())

    def save_parameter_template(self):
        dialog = self.parameter_dialog()
        if not dialog.template_save_button.isEnabled() and not self.choose_template_store(dialog):
            return
        name, accepted = QInputDialog.getText(self.window,'保存为模板','模板名称')
        if accepted and not dialog.save_template(name):
            QMessageBox.warning(self.window,'保存为模板',dialog.error_label.text())

    def delete_selected_template(self):
        dialog = self.parameter_dialog()
        name = self.template_picker.currentText()
        if name and QMessageBox.question(self.window,'删除模板',f'从当前列表删除“{name}”？',
                                        QMessageBox.Yes | QMessageBox.No,QMessageBox.No)==QMessageBox.Yes:
            if not dialog.delete_template(name):
                QMessageBox.warning(self.window,'删除模板',dialog.error_label.text())

    def home_page(self, layout):
        status = group('策略状态', layout)
        self.home_state = QLabel()
        self.home_state.setStyleSheet('font-weight: bold; color: #1746a1;')
        self.home_connection = QLabel()
        self.home_connection.setWordWrap(True)
        status_line = QHBoxLayout()
        status_line.addWidget(self.home_state)
        status_line.addWidget(self.home_connection, 1)
        status.addLayout(status_line)
        self.home_pending = QLabel()
        self.home_pending.setObjectName('homePending')
        self.home_pending.setTextFormat(Qt.PlainText)
        self.home_pending.setWordWrap(True)
        layout.addWidget(self.home_pending)

        self.symbols_page(layout)

        positions = group('策略持仓', layout)
        self.home_position_title = QLabel('策略持仓')
        position_heading = row(self.home_position_title)
        position_heading.addWidget(button('查看明细', lambda: self.show_details(self.monitor_tab)))
        positions.addLayout(position_heading)
        self.home_positions = table(('合约', '方向', '手数', '浮盈亏', '持仓状态'))
        positions.addWidget(self.home_positions)
        self.home_empty_positions = QLabel()
        positions.addWidget(self.home_empty_positions)

        self.home_event = QLabel()
        self.home_event.setTextFormat(Qt.PlainText)
        self.home_event.setWordWrap(True)
        self.home_event.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.home_updated = QLabel()
        footer = QHBoxLayout()
        footer.addWidget(self.home_event, 1)
        footer.addWidget(self.home_updated)
        layout.addLayout(footer)

    def account_page(self, layout):
        w = self.window
        account = group('账号信息', layout)
        self.home_account_type = QLabel()
        self.home_account_type.setObjectName('homeAccountType')
        self.home_account = QLabel()
        self.home_account.setTextFormat(Qt.PlainText)
        account.addLayout(row(self.home_account_type, self.home_account))
        self.account_picker = QComboBox()
        self.account_picker.setMinimumWidth(300)
        self.account_switch = button('切换账号', w.select_account)
        account.addLayout(row(QLabel('模拟盘账号：'), self.account_picker, self.account_switch,
                              w.accounts_button))
        account.addWidget(QLabel('模拟盘初次默认使用 SimNow；账号及口令在「账户管理」中保存。'))
        account.addLayout(row(w.session_button, w.refresh_button, w.attach_button))
        account.addWidget(w.connection_label)
        account.addWidget(w.connection_detail)
        account.addWidget(w.kernel_label)
        account.addWidget(w.account_label)
        self.reload_accounts()

        funds = group('账号资金', layout, horizontal=True)
        self.home_money = {}
        for key, name, note in (
                ('today_pnl', '今日权益变动', '相比上一交易日结算'),
                ('float_profit', '当前浮动盈亏', '当前账户持仓'),
                ('available', '可用资金', '保证金占用 —')):
            card = group(name, funds, stretch=1)
            value = QLabel('—')
            value.setObjectName('homeMoney')
            caption = QLabel(note)
            card.addWidget(value)
            card.addWidget(caption)
            self.home_money[key] = value, caption
        if self.paired:
            comparison = group('夜盘对照', layout)
            comparison.addWidget(QLabel('当前连接范围已绑定 SimNow 账号'))
            comparison.addWidget(button('查看运行日志', lambda: self.show_details(self.log_tab)))
        layout.addStretch()

    def reload_accounts(self):
        picker = self.account_picker
        picker.clear()
        store = self.window.store
        state = store.read() if store is not None else None
        rows = state.get('accounts', []) if isinstance(state, dict) else []
        selected = self.window.selected_account
        for account in sorted(rows, key=lambda item: item['provider'] != 'TQ_SIMNOW'):
            provider = 'SimNow' if account['provider'] == 'TQ_SIMNOW' else '快期模拟'
            picker.addItem(f"{provider} · {account['alias']} · {account['masked_login']}", account['account_id'])
        index = picker.findData(selected['account_id']) if selected else -1
        if index >= 0:
            picker.setCurrentIndex(index)
        elif not rows:
            picker.addItem('SimNow（已绑定）' if self.paired else '请先添加模拟账号', None)
        ready = self.paired is None and not self.window._startup_in_progress
        enabled = bool(rows) and ready
        picker.setEnabled(enabled)
        self.account_switch.setEnabled(enabled)
        self.window.accounts_button.setEnabled(store is not None and ready)
        self.window.attach_button.setEnabled(store is not None and ready)

    def reset_account_view(self):
        """Discard the previous account's display facts before requesting a new view."""
        self.rendered_view = None
        self.fund_values.clear()
        self.home_updated_at = None
        self.chart_owner = None
        self.last_state = None
        self.last_reasons.clear()
        self.balance_history.clear()
        self.last_balance_slot = None
        self.equity.set_rows([])
        self.candles.history.clear()
        self.candles.native_series.clear()
        self.candles.select('')
        self.candle_contract.clear()
        self.manual_code.clear()
        self.manual_price_scope = None
        self.manual_status.setText('等待所选账号同步')
        self.manual_funds.setText('资金未知')
        for label, title in self.fund_metrics.values():
            label.setText(f'{title}   —')
        for label in self.status_labels:
            label.setText('等待同步')
        self.quote_status.setText('行情 · 等待所选账号同步')
        self.instrument_grid.setRowCount(0)
        self.instrument_rules.set_markets(self.names, {})
        self.render_session_hours({})
        self.render_rollover_status({})
        self.activity.clear()
        self.visible_log_lines.clear()
        self.last_log_text = None
        self.log_text.clear()
        self.log_count.setText('0 行')
        self.render_equity_chart()
        self.render_home()

    @staticmethod
    def fill_home_table(grid, rows):
        populate(grid, rows)
        row_height = grid.verticalHeader().defaultSectionSize()
        grid.setFixedHeight(grid.horizontalHeader().sizeHint().height() + min(6, len(rows)) * row_height + 2)

    def render_home(self):
        w = self.window
        view = w.view
        connection = view['connection'] if view else {}
        config = view['configuration'] if view else {}
        connected = connection.get('broker_connected', False)
        synced = connection.get('synchronized', False)
        stale = w._stale and view is not None
        truncated = bool(view and view['truncated'])
        fault = any(connection.get(key) for key in ('fault', 'storage_failed', 'clock_fault'))
        available = bool(view and connected and synced and not (stale or fault or truncated))
        armed = bool(view and view['entry_authority'] == 'ARMED')

        account_type, account_login = w.display_account()
        self.home_account_type.setText(account_type)
        live = account_type == '正常账户'
        if self.home_account_type.property('live') != live:
            self.home_account_type.setProperty('live', live)
            self.home_account_type.style().unpolish(self.home_account_type)
            self.home_account_type.style().polish(self.home_account_type)
        self.home_account_type.setVisible(bool(account_type))
        self.home_account.setText('当前账号 ' + account_login)
        if view is None:
            w.account_label.setText('账户 · ' + account_login)

        cycles = [cycle for cycle in (view or {}).get('cycles', []) if cycle['remaining_quantity'] > 0]
        quantity = sum(cycle['remaining_quantity'] for cycle in cycles)
        if stale or fault or truncated:
            state = '状态待核对'
            description = '上次状态已过期' if stale else '柜台或执行器状态待核对'
            if truncated:
                description = '状态数据不完整，请在实时监控中核对'
        elif not connected:
            state, description = '尚未连接', '连接设置位于「账号信息」'
        elif not synced or connection.get('recovery_required'):
            state, description = '正在核对', '账户已连接 · 等待柜台或持仓核对'
        elif armed and config.get('manual_workbench'):
            state, description = '人工开仓已启用', '账户已连接'
        elif armed:
            count = len(config.get('scoped_contracts') or view['markets'])
            state, description = '策略运行中', f'账户已连接 · 正在监控 {count} 个合约'
        else:
            state = '已停止新开仓'
            description = f'仍有 {quantity} 手策略持仓 · 保护状态见下方' if quantity else '账户已连接 · 等待启动'
        self.home_state.setText(state)
        self.home_connection.setText(description)

        funds = self.fund_values if available else {}
        for key, (value, caption) in self.home_money.items():
            amount = funds.get(key)
            value.setText(self.yuan(amount))
            if key == 'available':
                margin = funds.get('margin')
                caption.setText(f'保证金占用 {self.yuan(margin)}')
            else:
                color = '#a22c37' if amount and amount.startswith('+') else '#07634d' if amount and amount.startswith('-') else '#172438'
                value.setStyleSheet(f'color: {color};')

        pending, positions = self.home_position_rows(view, cycles, available)
        if w.home_issue:
            pending.insert(0, w.home_issue)
        self.home_pending.setVisible(bool(pending))
        self.home_pending.setText('待处理  ·  ' + '；'.join(pending[:2]) +
                                  (f'；另有 {len(pending) - 2} 项' if len(pending) > 2 else '') if pending else '')
        self.home_pending.setToolTip('\n'.join(pending))
        self.home_position_title.setText(f"策略持仓  {'上次 ' if not available else ''}{quantity} 手" if quantity else '策略持仓')
        self.fill_home_table(self.home_positions, positions)
        self.home_positions.setVisible(bool(positions))
        self.home_empty_positions.setVisible(not positions)
        self.home_empty_positions.setText('当前无策略持仓' if available else '持仓待同步')
        for index, position in enumerate(positions):
            color = {'+': '#a22c37', '-': '#07634d'}.get(position[3][:1])
            if color:
                self.home_positions.item(index, 3).setForeground(QColor(color))
            status = position[4]
            status_color = '#07634d' if status == '保护中' else '#7c4c08'
            self.home_positions.item(index, 4).setForeground(QColor(status_color))

        symbols = config.get('strategy_symbols')
        if symbols is None and config.get('scoped_contracts'):
            symbols = [{'code': code, 'name': self.names.get(code, code)} for code in config['scoped_contracts']]
            source = '执行品种 · 参数未同步'
        elif symbols is None:
            symbols = self.symbols
            source = '已绑定参数' if self.paired else '等待执行器 · ' + self.profile_source
        else:
            source = '上次执行参数' if stale else '执行参数'
        rows = [[s['name'], s['code'], *(display_value(s.get(key)) for key in ('open_offset', 'stop_offset', 'profit_offset')),
                 ('自动' if not s['order_volume'] else s['order_volume']) if 'order_volume' in s else '—',
                 display_value(s.get('half_close_ratio')), display_value(s.get('hysteresis_points'))] for s in symbols]
        self.home_symbol_source.setText(source)
        self.fill_home_table(self.home_symbols, rows)

        trades = (view or {}).get('trades', [])
        latest = max((trade for trade in trades if trade.get('source_time_ns')),
                     key=lambda trade: int(trade['source_time_ns']), default=None)
        event = ''
        if latest:
            effect = '开仓' if latest['effect'] == 'OPEN' else '平仓'
            event = f"{native_time(latest['source_time_ns'])}  {latest['contract_id']} {effect}成交 {latest['quantity']} 手"
        self.home_event.setText(w.home_feedback or event)
        updated = self.home_updated_at if view is not None else None
        self.home_updated.setText(('最后更新 ' if stale else '状态更新 ') + updated if updated else '')

    @staticmethod
    def yuan(amount):
        if amount is None:
            return '—'
        return f'{amount[0]}¥{amount[1:]}' if amount[:1] in ('+', '-') else f'¥{amount}'

    def home_position_rows(self, view, cycles, available):
        if view is None:
            return [], []
        connection = view['connection']
        pending = []
        by_cycle = {}
        failed_exits = {block.get('position_cycle_id') for block in view['blocks']
                        if block.get('active') and block['reason_code'] == 'EXIT_REDUCTION_TERMINAL_UNFILLED'}
        if available:
            if connection.get('recovery_required'):
                pending.append('柜台持仓待核对')
            if view['configuration'].get('stop_cancel_pending'):
                pending.append('停止时撤单待确认')
            for order in view['orders']:
                code = order['contract_id']
                quantity = order.get('potential_remaining', 0)
                if order.get('send_state') == 'UNKNOWN' or order.get('cancel_status') == 'UNKNOWN':
                    message = '委托结果待核对'
                elif order.get('cancel_status') in {'SEND_POSSIBLE', 'CALL_RETURNED'} and not order.get('terminal_confirmed'):
                    message = '撤单待确认'
                elif quantity and order.get('broker_state') in {'ACKED', 'ALIVE'}:
                    effect = '开仓' if order['effect'] == 'OPEN' else '平仓'
                    message = f'{effect}委托 {quantity} 手待成交'
                elif quantity:
                    message = '委托待确认'
                else:
                    continue
                item = f'{code} {message}'
                if item not in pending:
                    pending.append(item)
                if order['effect'] == 'REDUCE' and order.get('position_cycle_id'):
                    by_cycle.setdefault(order['position_cycle_id'], []).append(message)
            if view.get('external_positions'):
                pending.append('柜台持仓或委托待核对')
        grouped = {}
        for cycle in cycles:
            cid, code = cycle['position_cycle_id'], cycle['contract_id']
            floating = cycle.get('floating_profit') if available else None
            if not available:
                status = '待核对'
            elif cycle.get('management_state') in {'TAKEN_OVER', 'TAKEOVER_PENDING'}:
                status = '已交接' if cycle['management_state'] == 'TAKEN_OVER' else '交接待确认'
                if status == '交接待确认':
                    pending.append(f'{code} {status}')
            elif cid in failed_exits:
                status = '平仓待核对'
                pending.append(f'{code} 平仓剩余目标待核对')
            elif cycle.get('exit_pending'):
                messages = by_cycle.get(cid, [])
                status = '平仓待成交' if messages and all('待成交' in item for item in messages) else '平仓待确认'
                if not messages:
                    pending.append(f'{code} {status}')
            elif (cycle.get('quality') != 'CONTIGUOUS' or connection.get('recovery_required')
                  or (cycle.get('close_route_review') or {}).get('status') == 'BLOCKED'):
                status = '保护待核对'
                pending.append(f'{code} {status}')
            else:
                status = '保护中'
            grouped.setdefault((code, cycle['direction']), []).append((cycle['remaining_quantity'], floating, status))

        names = self.names | {item['code']: item['name'] for item in view['configuration'].get('strategy_symbols', [])}
        status_order = ('待核对', '平仓待核对', '平仓待确认', '交接待确认', '保护待核对', '平仓待成交', '已交接', '保护中')
        rows = []
        for (code, direction), items in grouped.items():
            name = names.get(code)
            quantity = sum(item[0] for item in items)
            floating = sum(Decimal(item[1]) for item in items) if all(item[1] is not None for item in items) else None
            status = min((item[2] for item in items), key=status_order.index)
            rows.append((f'{name}  {code}' if name else code, '多' if direction == 'LONG' else '空',
                         f'{quantity} 手', self.yuan(f'{floating:+,.2f}' if floating is not None else None), status))
        return list(dict.fromkeys(pending)), rows

    def strategy_page(self, layout):
        templates = group('参数模板', layout)
        self.template_picker = QComboBox()
        self.template_picker.setPlaceholderText('选择模板')
        self.template_picker.setMinimumWidth(200)
        templates.addLayout(row(QLabel('参数模板：'), self.template_picker,
                                button('应用', self.apply_selected_template),
                                button('保存为模板', self.save_parameter_template),
                                button('删除模板', self.delete_selected_template)))
        settings = self.profile.to_record()['settings']
        fields = group('交易参数', layout)
        self.parameter_state = QLabel(self.profile_source)
        fields.addLayout(row(self.parameter_state,button('编辑交易参数',lambda:self.parameter_dialog().edit_preview())))
        self.parameter_fields = {}
        grid = QGridLayout()
        for index, (key, label) in enumerate(TRADE_FIELDS):
            field = self.read_value(settings['trade'][key])
            self.parameter_fields[key] = field
            grid.addLayout(row(QLabel(label),field), index//6, index%6)
        fields.addLayout(grid)
        switches = QGridLayout()
        self.parameter_switches = {}
        for index, (key, label) in enumerate(TRADE_SWITCHES):
            check = QCheckBox(label)
            check.setChecked(settings['trade'][key])
            check.setAttribute(Qt.WA_TransparentForMouseEvents)
            check.setFocusPolicy(Qt.NoFocus)
            self.parameter_switches[key] = check
            switches.addWidget(check, index//4, index%4)
        fields.addLayout(switches)
        schedule = group('DSV 定时参数', fields)
        data = settings['schedule']
        self.schedule_enabled = QLabel('启用' if data['enabled'] else '关闭')
        self.schedule_fields = {key:self.read_value(data[key]) for key in ('night_start_time','day_start_time','catch_up_minutes')}
        schedule.addLayout(row(self.schedule_enabled,QLabel('夜盘'),self.schedule_fields['night_start_time'],
            QLabel('日盘'),self.schedule_fields['day_start_time'],QLabel('补启动分钟'),self.schedule_fields['catch_up_minutes']))

        fields.addLayout(row(button('参数中心', self.open_parameters),
                             button('保存预览参数', self.export_parameters_preview)))
        layout.addStretch()

    def symbols_page(self, layout):
        contracts = group('品种列表', layout, stretch=1)
        self.symbol_preview_state = QLabel()
        contracts.addLayout(row(self.symbol_preview_state,
                               button('查看执行参数', self.show_execution_parameters)))
        if not self.paired:
            actions = row(button('重读 DSV 参数', lambda: self.reload_dsv_parameters()),
                          button('选择 DSV 配置', self.choose_dsv_parameters))
            actions.addStretch(1)
            contracts.addLayout(actions)
        self.symbol_grid = table(['品种名','合约代码','主力合约','换月提醒（到期日）','候选换月','开仓偏移','止损偏移','止盈偏移',
                                  '下单手数','半仓参数','滞回点','建议手数'])
        self.symbol_grid.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        contracts.addWidget(self.symbol_grid)
        self.symbol_grid.itemDoubleClicked.connect(lambda _: self.edit_selected_symbol())
        actions = row(button('添加品种',self.add_symbol),
                      button('编辑选中',self.edit_selected_symbol),
                      button('删除选中',self.remove_selected_symbols),
                      button('导入Excel',self.import_symbols_preview),
                      button('导出Excel',self.export_symbols_preview),
                      button('清空列表',lambda:self.remove_selected_symbols(all_symbols=True)),
                      button('参数中心',self.open_parameters),
                      button('换月预览',self.stage_selected_rollover))
        actions.addWidget(button('保存预览参数',self.export_parameters_preview))
        contracts.addLayout(actions)

    def render_parameters(self, profile):
        settings = profile.to_record()['settings']
        self.parameter_state.setText(self.profile_source)
        if profile.content_hash!=self.profile.content_hash:
            self.parameter_state.setText('本地参数预览 · 用于下次启动')
        if self.dsv_config is not None:
            self.parameter_state.setToolTip(str(self.dsv_config))
            self.symbol_preview_state.setToolTip(str(self.dsv_config))
        self.symbol_preview_state.setText(self.parameter_state.text() + ' · 编辑预览不改变当前运行')
        for key,field in self.parameter_fields.items():
            field.setText(str(settings['trade'][key]))
        for key,check in self.parameter_switches.items():
            check.setChecked(settings['trade'][key])
        self.schedule_enabled.setText('启用' if settings['schedule']['enabled'] else '关闭')
        for key,field in self.schedule_fields.items():
            field.setText(str(settings['schedule'][key]))
        populate(self.symbol_grid,[[s['name'],s['code'],'—','到期日未取到（可不换月）',s.get('code_new') or '—',s['open_offset'],s['stop_offset'],
            s['profit_offset'],s['order_volume'] or '自动',s['half_close_ratio'],s['hysteresis_points'],'—']
            for s in settings['symbols']])
        self.render_rollover_status({item['contract_id']:item
                                     for item in (self.rendered_view or {}).get('markets',[])})

    def choose_dsv_parameters(self):
        path, _ = QFileDialog.getOpenFileName(self.window, '选择 DSV config.json',
                                              str(self.dsv_config or ''), 'JSON (*.json)')
        if path and self.reload_dsv_parameters(Path(path)):
            self.settings.setValue('dsv_config_path', str(self.dsv_config))

    def reload_dsv_parameters(self, path=None):
        source = path or self.dsv_config
        if source is None:
            self.window.show_home_feedback('请先选择 DSV 的 config.json。')
            return False
        try:
            profile = load_profile(source, dsv=True)
        except TraderError:
            self.window.show_home_feedback('读取 DSV 配置失败，请检查文件后重试。')
            return False
        dialog = self.window._settings_dialog
        if (dialog is not None and dialog.profile.content_hash != self.profile.content_hash
                and QMessageBox.question(self.window, '重读 DSV 参数', '用 DSV 已保存参数替换当前未导出的编辑？',
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes):
            return False
        self.dsv_config = source
        self.set_dsv_profile(profile)
        self.window.show_home_feedback('已读取 DSV 交易、定时、偏移计算及品种参数。')
        return True

    def set_dsv_profile(self, profile):
        self.dsv_profile_loaded = True
        self.profile_source = '本机 DSV 已保存参数'
        self.profile = profile
        self.symbols = profile.to_record()['settings']['symbols']
        self.names = {symbol['code']: symbol['name'] for symbol in self.symbols}
        dialog = self.window._settings_dialog
        if dialog is None:
            self.render_parameters(profile)
        else:
            dialog.set_reference_profile(profile, self.profile_source)
        config = (self.rendered_view or {}).get('configuration', {})
        contracts = config.get('scoped_contracts') or list(self.names)
        for picker, values in ((self.candle_contract, contracts),
                               (self.order_contract, ['全部品种', *contracts])):
            selected = picker.currentText()
            picker.clear()
            picker.addItems(values)
            if selected in values:
                picker.setCurrentText(selected)
        if self.window.view is None:
            self.manual_code.clear()
            self.manual_code.addItems(self.names)
        self.instrument_rules.set_markets(self.names, {
            item['contract_id']: item for item in (self.rendered_view or {}).get('markets', [])})
        self.calculator.set_base_profile(profile)
        self.offset_preview.setText('计算结果在此核对')

    def checked_start_profile(self):
        dialog = self.window._settings_dialog
        profile = dialog.profile if dialog is not None else self.profile
        edited = profile.content_hash != self.profile.content_hash
        if self.dsv_config is not None:
            try:
                current = load_profile(self.dsv_config, dsv=True)
            except TraderError as error:
                raise TraderError('E_DSV_PROFILE', 'DSV 配置读取失败，请检查文件或选择正确的 DSV 配置。') from error
            if current.content_hash != self.profile.content_hash or not self.dsv_profile_loaded:
                if not edited:
                    self.set_dsv_profile(current)
                raise TraderError('E_DSV_PROFILE_CHANGED', 'DSV 参数已变化，请核对品种和交易参数后重新启动。已有本地编辑时，请先重读 DSV 参数。')
        elif self.require_reviewed_profile and not self.paired and not edited:
            raise TraderError('E_DSV_PROFILE', '请先选择本机 DSV 配置，或在参数中心导入已核对的业务参数。')
        return profile

    def show_execution_parameters(self):
        self.show_details(self.monitor_tab)
        self.monitor_details.setCurrentWidget(self.execution_parameters)

    def render_rollover_status(self, markets):
        profile = self.window._settings_dialog.profile if self.window._settings_dialog is not None else self.profile
        settings = profile.to_record()['settings']
        symbols = settings['symbols']
        account = (self.rendered_view or {}).get('account_metrics') or {}
        from online_trader.configuration.instrument_rules import product_code
        for index, symbol in enumerate(symbols):
            market = markets.get(symbol['code'],{})
            facts = market.get('rollover') or {}
            today = expiry_date(facts.get('as_of_date')) or datetime.now(BEIJING).date()
            warning, urgent = expiry_warning(facts.get('expiry_date'),today)
            candidate = facts.get('candidate')
            basis = facts.get('candidate_basis')
            if candidate:
                hints = {'MAIN':'非主力','VOLUME':'成交量领先','OPEN_INTEREST':'持仓量领先','DUAL':'持仓和成交量双超'}
                warning += f'  ·  {hints.get(basis,"候选")} {candidate}'
            saved = symbol.get('code_new')
            if saved == symbol['code']:
                saved = None
            values = (facts.get('main_contract') or '—',warning,saved or candidate or '—')
            for column, value in zip((2,3,4),values):
                item = self.symbol_grid.item(index,column)
                item.setText(value)
                tooltip = value
                if column == 2 and facts.get('main_expiry_date'):
                    tooltip += '  ·  主力到期日 ' + facts['main_expiry_date']
                item.setToolTip(tooltip)
                item.setForeground(QColor('#a52718') if column==3 and urgent else QColor('#172438'))
            rule_panel = getattr(self, 'instrument_rules', None)
            rule = rule_panel.rules.get(product_code(symbol['code'])) if rule_panel is not None else None
            estimate, note = suggested_lots(symbol, settings['trade'], len(symbols), account, market, rule)
            item = self.symbol_grid.item(index,11)
            item.setText(f'{estimate} 手' if estimate is not None else '—')
            item.setToolTip(note)

    def stage_selected_rollover(self):
        selected = self.symbol_grid.selectionModel().selectedRows()
        if len(selected) != 1:
            QMessageBox.information(self.window,'换月预览','请先选中一个品种。')
            return
        dialog = self.parameter_dialog()
        profile = dialog.profile
        symbol = profile.to_record()['settings']['symbols'][selected[0].row()]
        code = symbol['code']
        saved = symbol.get('code_new')
        if saved == code:
            saved = None
        def live_candidate():
            view = self.rendered_view or {}
            if self.window._stale or view.get('truncated') or view.get('connection',{}).get('fault'):
                return None
            market = next((item for item in view.get('markets',[]) if item['contract_id']==code),{})
            return (market.get('rollover') or {}).get('candidate')

        target = saved or live_candidate()
        if target is None:
            QMessageBox.information(self.window,'换月预览','行情暂未给出换月候选，可先在品种编辑中填写目标合约。')
            return
        try:
            candidate = preview_rollover(profile,code,target)
        except TraderError as error:
            QMessageBox.warning(self.window,'换月预览',str(error))
            return
        if QMessageBox.question(self.window,'换月预览',
                f'将 {symbol["name"]} 的草稿合约从 {code} 改为 {target}？\n'
                '保存预览后，用新参数和新合约日历审查下一次运行范围。当前执行器仍管理原合约。',
                QMessageBox.Yes | QMessageBox.No,QMessageBox.No) != QMessageBox.Yes:
            return
        if (not saved and live_candidate()!=target) or not dialog.apply_edited_preview(candidate,expected_hash=profile.content_hash):
            QMessageBox.warning(self.window,'换月预览','候选或参数预览已变化，请重新核对。')

    def add_symbol(self):
        if self.parameter_dialog().edit_preview(add_symbol=True):
            self.symbol_grid.selectRow(self.symbol_grid.rowCount() - 1)
            self.symbol_grid.scrollToBottom()
            self.window.show_home_feedback('品种已添加到列表。点击“保存预览参数”可保存到文件。')

    def edit_selected_symbol(self):
        selected = self.symbol_grid.selectionModel().selectedRows()
        if len(selected)==1:
            index = selected[0].row()
            if self.parameter_dialog().edit_preview(symbol_row=index):
                self.symbol_grid.selectRow(index)
                self.window.show_home_feedback('品种修改已保存到列表。点击“保存预览参数”可保存到文件。')
        else:
            self.window.show_home_feedback('请先选中一个品种，再点击“编辑选中”；也可以双击品种。')

    def remove_selected_symbols(self, *, all_symbols=False):
        dialog = self.parameter_dialog()
        settings = dialog.profile.to_settings()
        selected = (set(range(len(settings['symbols']))) if all_symbols else
                    {item.row() for item in self.symbol_grid.selectionModel().selectedRows()})
        if not selected or QMessageBox.question(self.window,'编辑品种预览',
                f'从参数预览移除 {len(selected)} 个品种？',
                QMessageBox.Yes | QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:
            return
        settings['symbols'] = [symbol for index,symbol in enumerate(settings['symbols']) if index not in selected]
        dialog.apply_edited_preview(DsvProfile.from_settings(settings),expected_hash=dialog.profile.content_hash)

    def import_symbols_preview(self):
        dialog = self.parameter_dialog()
        path,_ = QFileDialog.getOpenFileName(self.window,'导入品种预览','','品种表 (*.xlsx *.csv)')
        if path and not dialog.import_symbols_file(path):
            QMessageBox.warning(self.window,'导入品种',dialog.error_label.text())

    def export_symbols_preview(self):
        dialog = self.parameter_dialog()
        path,_ = QFileDialog.getSaveFileName(self.window,'导出品种预览','品种列表.xlsx','Excel (*.xlsx);;CSV (*.csv)')
        if path and not dialog.export_symbols_file(path):
            QMessageBox.warning(self.window,'导出品种',dialog.error_label.text())

    def export_parameters_preview(self):
        dialog = self.parameter_dialog()
        path,_ = QFileDialog.getSaveFileName(self.window,'保存业务参数预览','dsv-business-settings.json','JSON (*.json)')
        if path:
            if not dialog.export_file(path):
                QMessageBox.warning(self.window,'保存业务参数预览',dialog.error_label.text())
            else:
                self.window.show_home_feedback('参数已保存：' + Path(path).name)

    @staticmethod
    def read_value(value):
        field = QLineEdit(str(value))
        field.setReadOnly(True)
        field.setMaximumWidth(72)
        return field

    def monitor_page(self, layout):
        w = self.window
        account = group('账户实时信息', layout)
        w.funds_label.hide()
        metrics = QGridLayout()
        self.fund_metrics = {}
        fields = (
            ('equity','账户权益'), ('available','可用资金'), ('margin','当前保证金'),
            ('margin_ratio','资金使用率'), ('risk_ratio','风险度'), ('local_time','本机时间'),
            ('quote_delay','行情延迟'), ('close_profit','平仓盈亏'), ('position_profit','持仓盈亏'),
            ('today_pnl','当日累计'), ('fees','手续费'), ('today_ratio','盈亏比例'),
            ('opening_orders','开仓委托'),
        )
        for index,(key,title) in enumerate(fields):
            metric = QLabel(f'{title}   —')
            metric.setStyleSheet('color: #19498c;')
            metrics.addWidget(metric,index//7,index%7)
            self.fund_metrics[key] = (metric,title)
        account.addLayout(metrics)
        self.quote_status = QLabel('行情 · 等待首份快照')
        account.addWidget(self.quote_status)
        states = group('品种状态', layout, horizontal=True)
        self.status_labels = []
        for name, title in (('waiting','尚可开仓'),('blocked','当日已停止开仓'),('pending','平仓待确认')):
            box = group(title, states, stretch=1)
            label = QLabel('等待同步')
            label.setObjectName(name)
            label.setWordWrap(True)
            label.setMinimumHeight(62)
            label.setAlignment(Qt.AlignTop)
            if name == 'waiting':
                label.setToolTip('品种尚未触发当日停开。每笔开仓仍需满足当前信号和风控。')
            box.addWidget(label)
            self.status_labels.append(label)
        details = QTabWidget()
        w.cycles.setColumnCount(len(POSITION_HEADERS))
        w.cycles.setHorizontalHeaderLabels(POSITION_HEADERS)
        w.cycles.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        details.addTab(w.cycles, '实时持仓')
        execution = QWidget()
        self.execution_parameters = execution
        execution_layout = QVBoxLayout(execution)
        self.home_symbol_source = QLabel()
        execution_layout.addWidget(self.home_symbol_source)
        self.home_symbols = table(SYMBOL_HEADERS)
        execution_layout.addWidget(self.home_symbols)
        execution_layout.addStretch()
        details.addTab(execution, '执行参数')
        details.addTab(self.market_panel(), '实时行情')
        details.addTab(self.tables['protection'], '保护明细')
        details.addTab(self.external_panel(), '柜台持仓差异')
        details.addTab(self.tables['blocks'], '阻挡详情')
        positions = group('实时持仓', layout, stretch=3)
        positions.addWidget(details)
        positions.addLayout(row(self.buttons['close'], self.buttons['flatten']))
        self.monitor_details = details

        self.charts = QWidget()
        charts = QHBoxLayout(self.charts)
        charts.setContentsMargins(0, 0, 0, 0)
        equity = group('权益曲线', charts, stretch=6)
        self.equity_panel = equity.parentWidget()
        self.equity_mode = QComboBox()
        self.equity_mode.addItems(['权益曲线','收益曲线'])
        self.equity_from = QComboBox()
        self.equity_to = QComboBox()
        self.equity_mode.currentIndexChanged.connect(self.render_equity_chart)
        equity.addLayout(row(QLabel('曲线'),self.equity_mode,QLabel('起'),self.equity_from,
                             QLabel('止'),self.equity_to,button('应用',self.render_equity_chart)))
        equity.addLayout(row(w.reports_button,button('导出 PNG',self.export_equity_png),
                             button('导出 CSV',self.export_equity_csv)))
        self.equity = EquityChart()
        self.equity.setMinimumHeight(105)
        equity.addWidget(self.equity)
        prices = group('K线图', charts, stretch=5)
        self.candle_panel = prices.parentWidget()
        self.candle_contract = QComboBox()
        self.candle_contract.addItems(self.names)
        self.candles = SampledCandles()
        self.candle_source = QLabel()
        self.candle_contract.currentTextChanged.connect(self.select_candles)
        self.select_candles(self.candle_contract.currentText())
        prices.addLayout(row(QLabel('选择品种'),self.candle_contract,self.candle_source))
        prices.addWidget(self.candles)
        layout.addWidget(self.charts, 2)

    def select_candles(self, code):
        self.candles.select(code)
        self.candle_source.setText('原生 1 分钟' if code in self.candles.native_series else '工作台采样')

    def market_panel(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addLayout(row(self.window.filter_input,self.buttons['symbol_enable'],self.buttons['symbol_pause']))
        layout.addWidget(self.tables['markets'])
        return page

    def external_panel(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.tables['external_positions'])
        layout.addLayout(row(self.buttons['adopt'],self.buttons['lot_review'],self.buttons['reconcile']))
        return page

    def orders_page(self, layout):
        filters = group('筛选', layout)
        self.order_contract = QComboBox()
        self.order_contract.addItems(['全部品种', *self.names])
        self.order_effect = QComboBox()
        self.order_effect.addItems(['全部开平','开仓','平仓'])
        self.order_side = QComboBox()
        self.order_side.addItems(['全部方向','买','卖'])
        self.order_count = QLabel('等待柜台同步')
        self.order_contract.currentTextChanged.connect(self.filter_orders)
        self.order_effect.currentTextChanged.connect(self.filter_orders)
        self.order_side.currentTextChanged.connect(self.filter_orders)
        controls = row(QLabel('品种'),self.order_contract,QLabel('方向'),self.order_side,QLabel('开平'),self.order_effect)
        controls.addWidget(self.order_count)
        controls.addWidget(button('刷新', self.window.refresh))
        filters.addLayout(controls)
        orders = group('当日挂单 / 委托', layout, stretch=1)
        orders.addWidget(self.tables['orders'])
        self.tables['orders'].setColumnCount(len(ORDER_HEADERS))
        self.tables['orders'].setHorizontalHeaderLabels(ORDER_HEADERS)
        orders.addLayout(row(self.buttons['cancel'],self.buttons['cancel_entries'],self.buttons['cancel_account']))
        fills = group('当日成交明细', layout, stretch=1)
        fills.addWidget(self.tables['trades'])
        self.tables['trades'].setColumnCount(len(TRADE_HEADERS))
        self.tables['trades'].setHorizontalHeaderLabels(TRADE_HEADERS)

    @staticmethod
    def current_trading_day(view):
        day = (view.get('account_metrics') or {}).get('trading_day')
        if day is not None:
            return day
        days = {item['trading_day'] for item in view['markets'] if item.get('fresh') and item.get('trading_day')}
        return next(iter(days)) if len(days)==1 else None

    def filter_orders(self, *_):
        if self.rendered_view is None:
            return
        chosen = self.order_contract.currentText()
        effect = {'开仓':'OPEN','平仓':'REDUCE'}.get(self.order_effect.currentText())
        side = self.order_side.currentText()
        view = self.rendered_view
        current = self.current_trading_day(view)
        def visible(item, day):
            return (current is None or item.get(day)==current) and (
                chosen == '全部品种' or item.get('contract_id') == chosen) and (
                effect is None or item.get('effect') == effect) and (side=='全部方向' or side==native_side(item))
        counts = []
        for name, day in (('orders','native_trading_day'),('trades','trading_day')):
            matches = 0
            for index, item in enumerate(view[name]):
                keep = visible(item, day)
                self.tables[name].setRowHidden(index, not keep)
                matches += keep
            counts.append(matches)
        self.order_count.setText(f'委托 {counts[0]} 笔   /   成交 {counts[1]} 笔' if current else '等待柜台交易日')

    def manual_page(self, layout):
        account = group('连接', layout)
        self.manual_status = QLabel('等待账户同步')
        self.manual_close_price_mode = QComboBox()
        self.manual_close_price_mode.addItems(['对手价','排队价','最新价'])
        account.addLayout(row(self.manual_status,button('模拟连接设置',self.window.open_session),
                              QLabel('平仓价格'),self.manual_close_price_mode))
        columns = QHBoxLayout()
        order = group('下单', columns, stretch=5)
        self.manual_code = QComboBox()
        self.manual_code.addItems(self.names)
        self.manual_code.currentTextChanged.connect(self.render_manual)
        self.manual_side = QComboBox()
        self.manual_side.addItems(['买开','卖开'])
        self.manual_quantity = QSpinBox()
        self.manual_quantity.setRange(1, 2147483647)
        self.manual_price = QLineEdit()
        self.manual_price.setPlaceholderText('0 = 最新价')
        self.submit_button = button('提交开仓',self.submit)
        self.latest_button = button('用最新价',self.fill_latest)
        self.quick_sizes = []
        order.addWidget(QLabel('快捷开仓'))
        for side, label in enumerate(('买开','卖开')):
            controls = []
            for qty in (1, 2, 5, 10, 20):
                control = button(f'{label}{qty}',lambda checked=False,s=side,q=qty:self.quick_open(s,q))
                controls.append(control)
                self.quick_sizes.append(control)
            order.addLayout(row(*controls))
        for label, control in (('合约',self.manual_code),('方向',self.manual_side),
                               ('手数',self.manual_quantity),('价格',self.manual_price)):
            control.setMinimumWidth(120)
            order.addLayout(row(QLabel(label),control))
        order.addLayout(row(self.submit_button,self.latest_button))
        self.manual_position = QLabel('选中持仓：无')
        self.manual_close_buttons = {}
        for qty,label in ((None,'全部平仓'),(1,'平1手'),(5,'平5手')):
            self.manual_close_buttons[qty] = button(label,lambda checked=False,q=qty:self.close_manual_position(q))
        self.manual_close_all = button('平所有持仓',lambda:self.window.manual_close_all())
        order.addWidget(self.manual_position)
        order.addLayout(row(*self.manual_close_buttons.values(),self.manual_close_all))
        if not self.paired:
            order.addLayout(row(self.buttons['buy'],self.buttons['sell']))
        self.manual_notice = QLabel('人工交易需要独立人工范围')
        order.addWidget(self.manual_notice)
        order.addStretch()
        quote = group('实时行情（当前选中合约）', columns, stretch=4)
        self.manual_quote = QLabel('等待行情')
        self.manual_quote.setAlignment(Qt.AlignTop)
        quote.addWidget(self.manual_quote)
        funds = group('账户', columns, stretch=3)
        self.manual_funds = QLabel('等待同步')
        self.manual_funds.setWordWrap(True)
        self.manual_funds.setAlignment(Qt.AlignTop)
        funds.addWidget(self.manual_funds)
        layout.addLayout(columns, 3)
        summary = group('当日委托、成交与持仓', layout, stretch=4)
        manual_tabs = QTabWidget()
        self.manual_tables = {}
        for key, title, headers in (
            ('orders','当日委托',ORDER_HEADERS),
            ('trades','当日成交',TRADE_HEADERS),
            ('cycles','当前持仓（可选中平仓）',POSITION_HEADERS),
        ):
            grid = table(headers)
            self.manual_tables[key] = grid
            manual_tabs.addTab(grid,title)
        self.manual_controls_enabled = False
        self.manual_cancel = button('撤单',self.cancel_manual_order)
        self.manual_cancel_account = button('撤全部挂单',self.window.cancel_account)
        for key in ('cycles','orders'):
            self.manual_tables[key].itemSelectionChanged.connect(self.refresh_manual_selection)
        summary.addWidget(manual_tabs)
        summary.addLayout(row(self.manual_cancel,self.manual_cancel_account))

    def update_manual_controls(self, enabled):
        self.manual_controls_enabled = enabled
        view = self.window.view
        ready = bool(enabled and view and view['configuration'].get('manual_workbench'))
        self.submit_button.setEnabled(ready)
        self.latest_button.setEnabled(ready)
        for control in self.quick_sizes:
            control.setEnabled(ready)
        self.refresh_manual_selection()
        self.manual_notice.setText('提交前确认合约、方向、手数和限价' if ready else '人工开仓需要已同步的独立人工范围')

    def quick_open(self, side, qty):
        self.manual_side.setCurrentIndex(side)
        self.manual_quantity.setValue(qty)
        self.window.submit_manual(self.manual_code.currentText(),('LONG','SHORT')[side],qty,'OPPONENT')

    def selected_manual_ids(self, key):
        return [index.data(Qt.UserRole) for index in self.manual_tables[key].selectionModel().selectedRows()
                if index.data(Qt.UserRole) is not None]

    def refresh_manual_selection(self):
        view = self.rendered_view or {}
        owned = [item for item in view.get('cycles',[]) if item['remaining_quantity']>0 and
                 item.get('management_state') not in {'TAKEOVER_PENDING','TAKEN_OVER'}]
        selected = self.selected_manual_ids('cycles')
        position = next((item for item in owned if len(selected)==1 and item['position_cycle_id']==selected[0]),None)
        available = position['remaining_quantity'] if position else 0
        self.manual_position.setText(f"选中持仓：{position['contract_id']} {available} 手" if position else '选中持仓：无')
        for qty,control in self.manual_close_buttons.items():
            control.setEnabled(self.manual_controls_enabled and available>0 and (qty is None or qty<=available))
        configuration = view.get('configuration',{})
        mode = configuration.get('manual_close_price_mode')
        self.manual_close_all.setEnabled(self.manual_controls_enabled and mode is not None and bool(owned))
        self.manual_close_price_mode.setEnabled(self.manual_controls_enabled and mode is not None)
        selected = self.selected_manual_ids('orders')
        active = {item['child_order_id'] for item in view.get('orders',[]) if item.get('potential_remaining')}
        self.manual_cancel.setEnabled(self.manual_controls_enabled and bool(selected) and set(selected)<=active)
        cancel_review = configuration.get('native_cancel_review') or {}
        self.manual_cancel_account.setEnabled(self.manual_controls_enabled and bool(cancel_review.get('active_count')))

    def close_manual_position(self, quantity):
        selected = self.selected_manual_ids('cycles')
        if len(selected)==1:
            self.window.close_manual_cycle(selected[0],quantity)

    def cancel_manual_order(self):
        selected = self.selected_manual_ids('orders')
        if selected:
            self.window.cancel_manual_children(selected)

    def render_manual_tables(self, view):
        day = self.current_trading_day(view)
        orders = [item for item in view['orders'] if day is not None and item.get('native_trading_day')==day]
        trades = [item for item in view['trades'] if day is not None and item.get('trading_day')==day]
        positions = [item for item in view['cycles'] if item['remaining_quantity']>0]
        for name,items,cells,identity in (
                ('orders',orders,order_cells,'child_order_id'),
                ('trades',trades,trade_cells,'trade_key'),
                ('cycles',positions,position_cells,'position_cycle_id')):
            grid = self.manual_tables[name]
            selected = set(self.selected_manual_ids(name))
            grid.clearSelection()
            populate(grid,[cells(item) for item in items])
            for index,item in enumerate(items):
                grid.item(index,0).setData(Qt.UserRole,item[identity])
                if item[identity] in selected:
                    grid.selectionModel().select(grid.model().index(index,0),
                        QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.refresh_manual_selection()

    def render_positions(self, view):
        positions = {item['position_cycle_id']:item for item in view['cycles']}
        grid = self.window.cycles
        for row,cycle_id in enumerate(getattr(self.window,'_base_cycle_ids',[])):
            item = positions.get(cycle_id)
            visible = bool(item and item['remaining_quantity'])
            grid.setRowHidden(row,not visible)
            if visible:
                for column,value in enumerate(position_cells(item)):
                    cell = QTableWidgetItem(value)
                    cell.setTextAlignment(Qt.AlignCenter)
                    grid.setItem(row,column,cell)

    def submit(self):
        self.window.submit_manual(self.manual_code.currentText(),
            'LONG' if self.manual_side.currentIndex()==0 else 'SHORT',
            self.manual_quantity.value(),self.manual_price.text().strip())

    def fill_latest(self):
        market = self.current_market()
        if market.get('fresh') and market.get('last'):
            self.manual_price.setText(market['last'])

    def current_market(self):
        return next((item for item in (self.rendered_view or {}).get('markets',[])
                     if item['contract_id']==self.manual_code.currentText()),{})

    def render_manual(self, *_):
        if self.rendered_view is None:
            return
        quote = self.current_market()
        stamp = quote.get('source_time_utc')
        rows = [
            (('合约',self.manual_code.currentText()),('最新价',quote.get('last'))),
            (('买一',quote.get('bid1')),('买一量',quote.get('bid_volume1'))),
            (('卖一',quote.get('ask1')),('卖一量',quote.get('ask_volume1'))),
            (('开盘',quote.get('open')),('最高',quote.get('day_high'))),
            (('最低',quote.get('day_low')),('昨结',quote.get('pre_settlement'))),
            (('今结',quote.get('settlement')),('成交量',quote.get('volume'))),
            (('持仓量',quote.get('open_interest')),('涨停',quote.get('upper_limit'))),
            (('跌停',quote.get('lower_limit')),('保证金/手',quote.get('margin'))),
            (('手续费/手',quote.get('commission')),('最小变动',quote.get('price_tick'))),
            (('行情时间',beijing(stamp).strftime('%H:%M:%S') if stamp else None),),
        ]
        self.manual_quote.setText('\n'.join('     '.join(f'{label}   {display_value(value)}'
                                          for label,value in line) for line in rows))
        account = self.rendered_view.get('account_metrics')
        if account:
            fields = [('账户权益','balance'),('可用资金','available'),('占用保证金','margin'),
                      ('风险度','risk_ratio'),('平仓盈亏','close_profit'),('持仓盈亏','position_profit'),
                      ('手续费','commission'),('昨结权益','pre_balance')]
            values = {key:account.get(key) for _,key in fields}
            risk = values['risk_ratio']
            if risk is not None:
                values['risk_ratio'] = f'{Decimal(risk):.2%}'
            self.manual_funds.setText('\n'.join(f'{label}   {display_value(values[key])}' for label,key in fields))

    def logs_page(self, layout):
        self.log_filter = QComboBox()
        self.log_filter.addItems(['全部','执行器','工作台','DSV 对照'])
        self.log_filter.currentTextChanged.connect(self.update_logs)
        self.log_event_filter = QComboBox()
        self.log_event_filter.addItems(['全部事件', *LOG_EVENT_FILTERS])
        self.log_event_filter.currentTextChanged.connect(self.update_logs)
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText('搜索已加载日志')
        self.log_search.textChanged.connect(self.update_logs)
        self.log_count = QLabel('0 行')
        tools = row(button('复制全部',self.copy_log),button('导出Excel',self.export_log),
                    button('清空',self.clear_log),button('导出日报',self.open_daily_report),self.log_filter,
                    self.log_event_filter,self.log_search)
        tools.addWidget(self.log_count)
        layout.addLayout(tools)
        logs = group('运行日志',layout,stretch=1)
        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName('runtimeLog')
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        logs.addWidget(self.log_text)
        recovery = QTabWidget()
        recovery.addTab(self.tables['commands'],'命令回执')
        recovery.addTab(QWidget(),'原单核对')
        recovery.setMaximumHeight(150)
        panel = QVBoxLayout(recovery.widget(1))
        panel.addLayout(row(self.buttons['order_archive'],self.buttons['historical_orders']))
        panel.addStretch()
        layout.addLayout(row(self.buttons['receipt']))
        layout.addWidget(recovery)

    def copy_log(self):
        if self.visible_log_lines:
            QApplication.clipboard().setText('\n'.join(self.visible_log_lines))

    def export_log(self):
        if not self.visible_log_lines:
            QMessageBox.information(self.window,'导出日志','当前筛选下没有日志可导出')
            return
        name,_ = QFileDialog.getSaveFileName(self.window,'导出运行日志','运行日志.xlsx','Excel (*.xlsx);;文本 (*.txt)')
        if not name:
            return
        try:
            if Path(name).suffix.lower() == '.txt':
                payload = ('\n'.join(self.visible_log_lines) + '\n').encode('utf-8')
            else:
                from online_trader.reconciliation_export import tabular_workbook
                rows = [('序号','时间','类型','内容')]
                for index,line in enumerate(self.visible_log_lines,1):
                    match = re.match(r'^\[(\d\d:\d\d:\d\d)\] \[([^\]]+)\]\s*(.*)$',line)
                    rows.append((index,*match.groups()) if match else (index,'','',line))
                payload = tabular_workbook([('运行日志',rows,[6,11,10,110])])
            publish_new_bytes(payload,name)
        except TraderError as error:
            QMessageBox.warning(self.window,'导出日志',str(error))

    def clear_log(self):
        if QMessageBox.question(self.window,'清空运行日志','清空当前显示的运行日志？') != QMessageBox.Yes:
            return
        run = self.latest_run()
        if run is not None:
            paths = [run/'executor.log',run/'startup-failure.json',run/'strategy-log.jsonl',
                     *run.glob('comparison-*/events.jsonl')]
            self.log_clear_offsets.clear()
            for path in paths:
                try:
                    self.log_clear_offsets[path] = path.stat().st_size
                except OSError:
                    continue
        self.activity.clear()
        self.update_logs()

    def open_daily_report(self):
        self.window.open_reports()
        reports = self.window._reports_window
        if reports._worker is not None:
            return
        if self.paired is not None:
            reports.path_edit.setText(str(self.paired.packet/'ledger.db'))
        elif self.window.store is not None and self.window.selected_account is not None:
            reports.path_edit.setText(str(self.window.store.ledger_path(self.window.selected_account)))
        reports.request_daily_export()

    def latest_run(self):
        if self.paired is None:
            dialog = self.window._session_dialog
            selected = self.window.selected_account
            if dialog is not None and selected is not None and dialog.account_id == selected['account_id']:
                return dialog.run_directory
            return None
        runs = self.paired.packet/'runs'
        log = max(runs.glob('*/executor.log'), key=lambda path:path.parent.name, default=None)
        return log.parent if log is not None else None

    def json_tail(self, path, size):
        try:
            with path.open('rb') as stream:
                end = stream.seek(0,2)
                cleared = self.log_clear_offsets.get(path,0)
                if cleared > end:
                    cleared = 0
                offset = max(cleared,end-size)
                stream.seek(offset)
                lines = stream.read(size).splitlines()
        except OSError:
            return []
        result = []
        for line in (lines[1:] if offset > cleared else lines)[-800:]:
            if not line.startswith(b'{'):
                continue
            try:
                record = strict_loads(line)
            except TraderError:
                continue
            if isinstance(record,dict):
                result.append(record)
        return result

    def executor_log(self, run):
        phases = []
        log = run/'executor.log'
        if not log.exists():
            log = run.with_name(run.name + '.executor.log')
        for record in self.json_tail(log,96_000):
            if record.get('phase'):
                stamp = record.get('observed_at_utc')
                at = beijing(stamp).strftime('%H:%M:%S') if stamp else '—'
                phases.append(WorkbenchLogLine(f"[{at}] [执行器] {record['phase']} {record.get('error_code','')}".rstrip(),
                                               'error' if record.get('error_code') else None))
        for record in self.json_tail(run/'startup-failure.json',8192):
            phases.append(WorkbenchLogLine(f"[执行器] {record.get('status','启动失败')} · {record.get('error_code','—')}", 'error'))
        for record in self.json_tail(run/'strategy-log.jsonl',360_000):
            stamp, tag, message = (record.get(key) for key in ('observed_at_utc','tag','message'))
            if not all(isinstance(value,str) and value for value in (stamp,tag,message)):
                continue
            try:
                at = beijing(stamp).strftime('%H:%M:%S')
            except ValueError:
                continue
            phases.append(WorkbenchLogLine(f'[{at}] [{tag}] {message}', LOG_TAGS.get(tag)))
        return phases

    def comparison_logs(self, run):
        path = next(run.glob('comparison-*/events.jsonl'),None)
        product, dsv = [], []
        if path is None:
            return product, dsv
        for record in self.json_tail(path,360_000):
            event = record.get('data',{})
            if record.get('kind')=='DSV_LOG':
                stamp = event.get('source_time_utc')
                if stamp:
                    at = beijing(stamp).strftime('%H:%M:%S')
                    kind = event.get('kind')
                    category = event.get('operator_tag') or {'ENTRY_ORDER':'open','ENTRY_FILL':'open','EXIT_DECISION':'close'}.get(kind)
                    dsv.append(WorkbenchLogLine(f"[{at}] [DSV] {event.get('contract_id','—')} · {kind or ''} {event.get('reason','')}".rstrip(), category))
            elif record.get('kind')=='PRODUCT_COMMITTED_EVENT':
                label, category = {'ENTRY_PREPARED':('开仓意图','open'), 'EXIT_PREPARED':('平仓意图','close'),
                                   'FILL_CONFIRMED':('柜台成交',None), 'SEND_ABORTED':('发送取消','error'),
                                   'EXECUTION_CANCEL_TIMEOUT':('撤单待核对','error')}.get(event.get('event_type'), (None,None))
                if label is None:
                    continue
                payload = event.get('payload',{})
                data = payload.get('data',payload)
                cycle = data.get('cycle',{})
                plan = cycle.get('plan',{})
                fill = data.get('fill')
                child_id = None
                detail = ''
                if fill:
                    effect = '开仓' if fill.get('effect')=='OPEN' else '平仓'
                    if category is None:
                        category = 'open' if fill.get('effect')=='OPEN' else 'close'
                    child_id = fill.get('child_order_id')
                    detail = f"{effect} {fill.get('quantity','—')} 手 @ {fill.get('price','—')}"
                elif cycle.get('orders'):
                    order = cycle['orders'][-1]
                    child_id = order.get('child_order_id')
                    detail = f"{order.get('original_qty','—')} 手 @ {order.get('limit_price','—')}"
                stamp = (fill or {}).get('observed_at_utc') or (cycle.get('money_quote') or {}).get('observed_at_utc')
                at = beijing(stamp).strftime('%H:%M:%S') if stamp else '—'
                manual_children = (cycle.get('execution') or {}).get('manual_reduction_children',{})
                if category=='open' and cycle.get('entry_origin')=='MANUAL':
                    category = 'manual'
                elif category=='close' and (child_id in manual_children
                        or (cycle.get('nominal_exit') or {}).get('exit_reason')=='MANUAL_EXIT'):
                    category = 'manual'
                product.append(WorkbenchLogLine(f"[{at}] [执行器] {label} {plan.get('contract_id','—')} {detail}".rstrip(), category))
        return product, dsv

    def update_logs(self, *_):
        if self.tabs.currentWidget() is not self.log_tab:
            return
        mode = self.log_filter.currentIndex()
        run = self.latest_run()
        lines = []
        product, dsv = self.comparison_logs(run) if run and mode in (0,1,3) else ([],[])
        if run and mode in (0,1):lines.extend(self.executor_log(run) + product)
        if mode in (0,2):lines.extend(self.activity)
        if mode in (0,3):lines.extend(dsv)
        allowed = LOG_EVENT_FILTERS.get(self.log_event_filter.currentText())
        query = self.log_search.text().strip().casefold()
        self.visible_log_lines = [line.text for line in lines if (allowed is None or line.category in allowed)
                                  and query in line.text.casefold()]
        content = '\n'.join(self.visible_log_lines) or '当前没有可显示的运行记录'
        self.log_count.setText(f'{len(self.visible_log_lines)} 行')
        if content != self.last_log_text:
            self.last_log_text = content
            scrollbar = self.log_text.verticalScrollBar()
            position = scrollbar.value()
            follow = position >= scrollbar.maximum()-4
            self.log_text.setPlainText(content)
            scrollbar.setValue(scrollbar.maximum() if follow else position)

    def instruments_page(self, layout):
        cols = QHBoxLayout()
        hours = group('品种交易时间（北京时间）',cols,stretch=5)
        self.calendar_preview = None
        if self.paired is not None:
            from online_trader.scheduling.calendar import TradingCalendar
            calendar = TradingCalendar.from_record(strict_loads(self.paired.paths[2].read_bytes()))
            scope = strict_loads(self.paired.paths[0].read_bytes())
            day = scope['trading_day']
            self.calendar_preview = {'trading_day':day,
                'intervals':{code:calendar.session_times(code,day) for code in scope['contracts']}}
        self.session_grid = table(['品种','合约','日盘','夜盘'])
        self.calendar_status = QLabel()
        self.render_session_hours({})
        for column in (0,1,3):
            self.session_grid.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeToContents)
        self.session_grid.horizontalHeader().setStretchLastSection(False)
        hours.addWidget(self.calendar_status)
        hours.addWidget(self.session_grid)
        if self.paired is None:
            hours.addLayout(row(self.window.data_button))
        instruments = group('行情和合约状态',cols,stretch=4)
        self.instrument_grid = table(['品种','合约','交易日','最新价','行情状态'])
        instruments.addWidget(self.instrument_grid)
        from online_trader.desktop.instrument_rules import InstrumentRulesPanel
        self.instrument_rules = InstrumentRulesPanel(self.settings, self.add_activity)
        self.instrument_rules.set_markets(self.names, {})
        cols.addWidget(self.instrument_rules, 6)
        layout.addLayout(cols,1)

    def render_session_hours(self, view):
        active = view.get('configuration',{}).get('schedule_display')
        schedule = active or self.calendar_preview
        rows = []
        if schedule:
            label = '执行器已绑定' if active else '待启动日历预览'
            self.calendar_status.setText(f"{label} · 柜台交易日 {schedule['trading_day']}" +
                                         (' · 时段已截断' if schedule.get('truncated') else ''))
            for code, intervals in schedule['intervals'].items():
                day, night = [], []
                for opened, closed in intervals:
                    start, end = (beijing(value).strftime('%H:%M') for value in (opened,closed))
                    (night if start>='20:00' or start<'06:00' else day).append(f'{start}–{end}')
                rows.append((self.names.get(code,code),code,' / '.join(day) or '—',' / '.join(night) or '—'))
        else:
            self.calendar_status.setText('等待执行器确认交易日及绑定日历')
        current = getattr(self,'session_rows',None)
        if rows!=current:
            populate(self.session_grid,rows)
            self.session_rows=rows

    def offset_page(self, layout):
        from online_trader.desktop.calculator import OffsetCalculatorDialog
        calculator = OffsetCalculatorDialog(self.profile,self.window)
        calculator.setWindowFlags(Qt.Widget)
        calculator.cancel_button.hide()
        calculator.apply_button.setText('应用所选到本地预览')
        self.offset_preview = QLabel('计算结果在此核对')
        def applied():
            candidate = calculator.result_profile
            if candidate is not None:
                self.offset_preview.setText('已生成参数预览 · '+candidate.content_hash[:12])
            calculator.show()
        calculator.accepted.connect(applied)
        layout.addWidget(calculator,1)
        layout.addWidget(self.offset_preview)
        self.calculator = calculator

    def system_page(self, layout):
        sound_settings = getattr(self.window,'create_sound_settings',None)
        if sound_settings is not None:
            layout.addWidget(sound_settings())
        appearance = group('界面偏好', layout)
        appearance.addWidget(self.window._tray.preference_checkbox())
        for title,key,panel in (('显示 K 线图','show_candles',self.candle_panel),
                                ('显示权益曲线','show_equity',self.equity_panel)):
            chart = QCheckBox(title)
            chart.setChecked(self.settings.value(key,self.settings.value('show_charts',True,bool),bool))
            panel.setVisible(chart.isChecked())
            chart.toggled.connect(panel.setVisible)
            chart.toggled.connect(lambda visible,k=key:self.settings.setValue(k,visible))
            appearance.addWidget(chart)
        layout.addStretch()

    def add_activity(self, message, *, category=None):
        stamp = datetime.now(BEIJING).strftime('%H:%M:%S')
        self.activity.append(WorkbenchLogLine(f'[{stamp}] [工作台] {message}', category))

    def record_balance(self, account, view):
        now = datetime.now(BEIJING)
        slot = int(now.timestamp())//5
        day = self.current_trading_day(view)
        if day is None or slot == self.last_balance_slot:
            return
        self.last_balance_slot = slot
        native = view.get('account_metrics') or {}
        value = native.get('balance')
        if value is None and account.get('funds_quality') in {'KNOWN','ESTIMATED'}:
            value = account.get('equity')
        if value is not None and Decimal(value)<=0:
            value = None
        self.balance_history.append({'trading_day':day,'balance':value,'observed_at_utc':now.isoformat()})

    def equity_rows(self):
        view = self.rendered_view or {}
        daily = view.get('equity_history',[])
        days = set(item['trading_day'] for item in daily) | {item['trading_day'] for item in self.balance_history}
        entries = ['全部',*sorted(days)]
        for combo in (self.equity_from,self.equity_to):
            if [combo.itemText(index) for index in range(combo.count())]!=entries:
                selected = combo.currentText()
                combo.clear()
                combo.addItems(entries)
                combo.setCurrentText(selected if selected in entries else '全部')
        start,end = (combo.currentText() for combo in (self.equity_from,self.equity_to))
        selected_days = {day for day in days if (start=='全部' or day>=start) and (end=='全部' or day<=end)}
        rows = [item for item in daily if item['trading_day'] in selected_days]
        return rows,selected_days

    def render_equity_chart(self, *_):
        daily,days = self.equity_rows()
        current = self.current_trading_day(self.rendered_view) if self.rendered_view is not None else None
        intraday = [item for item in self.balance_history if item['trading_day']==current] if current in days else []
        has_current_samples = any(item['balance'] is not None for item in intraday)
        rows = [item for item in daily if not has_current_samples or item['trading_day']!=current]
        if has_current_samples:
            rows.extend(intraday)
        if self.equity_mode.currentText()=='收益曲线':
            anchor_rows = (self.rendered_view or {}).get('equity_history') or rows
            anchor = next((Decimal(item['balance']) for item in anchor_rows if item.get('balance') is not None),None)
            if anchor is not None:
                rows = [item|{'balance':str(Decimal(item['balance'])-anchor) if item.get('balance') is not None else None}
                        for item in rows]
            self.equity.heading = '权益变化（相对首个已记录权益）'
        else:
            self.equity.heading = '账户权益（柜台已记录）'
        self.equity.set_rows(rows)

    def export_equity_png(self):
        if not self.equity.rows:
            return
        path,_ = QFileDialog.getSaveFileName(self.window,'导出曲线 PNG','权益曲线.png','PNG (*.png)')
        if not path:
            return
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if self.equity.grab().save(buffer,'PNG'):
            self.write_equity_export(bytes(buffer.data()),path)

    def export_equity_csv(self):
        daily,_ = self.equity_rows()
        if not daily:
            return
        path,_ = QFileDialog.getSaveFileName(self.window,'导出每日权益 CSV','每日权益.csv','CSV (*.csv)')
        if not path:
            return
        stream = StringIO()
        writer = csv.writer(stream)
        writer.writerow(('日期','最近记录权益','当日盈亏','累计盈亏','累计收益率','记录时间（北京时间）'))
        first = Decimal(daily[0]['balance'])
        for row in daily:
            equity = Decimal(row['balance'])
            previous = row.get('pre_balance')
            today = equity-Decimal(previous) if previous is not None and Decimal(previous)>0 else ''
            writer.writerow((row['trading_day'],equity,today,equity-first,f'{equity/first-1:.2%}',
                             beijing(row['observed_at_utc']).isoformat()))
        self.write_equity_export(stream.getvalue().encode('utf-8-sig'),path)

    def write_equity_export(self, raw, path):
        try:
            publish_new_bytes(raw,path)
        except (TraderError,OSError):
            QMessageBox.warning(self.window,'导出权益','文件未保存。请选择可写的新文件名。')

    def render_funds(self, account, view, markets):
        known = account.get('funds_quality') not in ('UNKNOWN',None)
        values = {key:account.get(key) if known else None for key in ('equity','available','margin','fees')}
        native = view.get('account_metrics')
        if native:
            values.update({key:native.get(key) for key in
                           ('available','margin','risk_ratio','close_profit','position_profit','float_profit','opening_orders')})
            values['equity'] = native.get('balance')
            values['fees'] = native.get('commission')
            pre_balance = native.get('pre_balance')
            if pre_balance is not None and values['equity'] is not None:
                previous = Decimal(pre_balance)
                today = Decimal(values['equity']) - previous
                values['today_pnl'] = f'{today:+,.2f}'
                if previous > 0:
                    values['today_ratio'] = f'{today/previous:+.2%}'
        if values.get('margin') is not None and values.get('equity') is not None:
            equity = Decimal(values['equity'])
            values['margin_ratio'] = f"{(Decimal(values['margin'])/equity if equity>0 else Decimal(0)):.2%}"
        for key in ('equity','available','margin','close_profit','position_profit','float_profit','fees'):
            if values.get(key) is not None:
                sign = '+' if key in {'close_profit','position_profit','float_profit'} else ''
                values[key] = format(Decimal(values[key]), f'{sign},.2f')
        if values.get('risk_ratio') is not None:
            values['risk_ratio'] = f"{Decimal(values['risk_ratio']):.2%}"
        now = datetime.now(BEIJING)
        values['local_time'] = now.strftime('%H:%M:%S')
        delays = [(now-beijing(market['source_time_utc'])).total_seconds()
                  for market in markets.values() if market.get('fresh') and market.get('source_time_utc')]
        if delays and all(delay >= 0 for delay in delays):
            values['quote_delay'] = f'{max(delays):.1f} 秒'
        self.fund_values = values
        for key, (metric,title) in self.fund_metrics.items():
            value = values.get(key)
            metric.setText(f'{title}   {value if value is not None else "—"}')

    def render(self, view):
        self.rendered_view = view
        self.render_session_hours(view)
        populate(self.tables['orders'],[order_cells(item) for item in view['orders']])
        populate(self.tables['trades'],[trade_cells(item) for item in view['trades']])
        self.filter_orders()
        self.render_positions(view)
        self.manual_status.setText(self.window.connection_detail.text())
        self.manual_funds.setText(self.window.funds_label.text().replace(' · ','\n'))
        mode = view['configuration'].get('manual_close_price_mode')
        price_scope = (view['account_key'],view['kernel_boot_id'],mode)
        if self.manual_price_scope!=price_scope:
            default_index = self.manual_close_price_mode.findText(mode) if isinstance(mode,str) else -1
            self.manual_close_price_mode.setCurrentIndex(max(default_index,0))
            self.manual_price_scope = price_scope
        markets = {item['contract_id']:item for item in view['markets']}
        self.render_rollover_status(markets)
        codes = view['configuration'].get('scoped_contracts') or list(self.names)
        if [self.manual_code.itemText(i) for i in range(self.manual_code.count())]!=codes:
            selected = self.manual_code.currentText()
            self.manual_code.clear()
            self.manual_code.addItems(codes)
            if selected in codes:
                self.manual_code.setCurrentText(selected)
        if [self.candle_contract.itemText(i) for i in range(self.candle_contract.count())]!=codes:
            selected = self.candle_contract.currentText()
            self.candle_contract.clear()
            self.candle_contract.addItems(codes)
            if selected in codes:
                self.candle_contract.setCurrentText(selected)
        owner = view['account_key']
        if owner != self.chart_owner:
            self.chart_owner = owner
            self.balance_history.clear()
            self.last_balance_slot = None
            self.candles.history.clear()
            self.last_reasons.clear()
        account = getattr(self.window,'_last_account_record',{})
        self.render_funds(account,view,markets)
        self.home_updated_at = datetime.now(BEIJING).strftime('%H:%M:%S')
        self.render_home()
        self.record_balance(account,view)
        self.render_equity_chart()
        self.candles.show_native_bars(view.get('minute_bars',{}))
        self.select_candles(self.candle_contract.currentText())
        run = self.latest_run()
        has_strategy_log = run is not None and (run/'strategy-log.jsonl').is_file()
        waiting, blocked, pending = [], [], []
        quote_states = {}
        for code in codes:
            market = markets.get(code,{})
            name = f'{self.names.get(code,code)} ({code})'
            reasons = market.get('entry_reasons',[])
            quote_states[code] = ('新鲜' if market.get('fresh') else
                                  '行情已滞后' if market.get('source_time_utc') else '等待行情')
            activity_reasons = [reason for reason in reasons if not has_strategy_log or reason not in STRATEGY_LOG_REASONS]
            if activity_reasons != self.last_reasons.get(code):
                self.last_reasons[code] = activity_reasons
                if activity_reasons:
                    self.add_activity(f'{code} · {" / ".join(activity_reasons)}')
            stop_reason = market.get('day_stop_reason')
            if stop_reason:
                blocked.append(f'● {name}  {stop_reason}')
            else:
                if market.get('enabled') is False:
                    status = '品种已手动停用'
                elif not market.get('fresh'):
                    status = quote_states[code]
                elif view['entry_authority'] != 'ARMED':
                    status = '策略未启动'
                else:
                    primary = next((ENTRY_MESSAGES[reason] for reason in reasons if reason in ENTRY_MESSAGES),None)
                    details = [ACTIVATION_MESSAGES[reason] for reason in reasons if reason in ACTIVATION_MESSAGES]
                    status = ' · '.join([primary,*details] if primary else details) or ('等待信号' if reasons else '可开仓')
                waiting.append(f'● {name}  {status}')
            if market:
                self.candles.record(market)
        failed_exits = {block['position_cycle_id'] for block in view.get('blocks',())
                        if block.get('active') and block.get('reason_code')=='EXIT_REDUCTION_TERMINAL_UNFILLED'}
        for cycle in view['cycles']:
            target = cycle.get('target_remaining_quantity')
            if cycle.get('exit_pending',target is not None and target<cycle['remaining_quantity']):
                status = ' · 委托已终态，剩余目标待核对' if cycle['position_cycle_id'] in failed_exits else ''
                pending.append(f"● {cycle['contract_id']}  剩余 {cycle['remaining_quantity']} 手{status}")
        for label, lines in zip(self.status_labels,(waiting,blocked,pending)):
            label.setText('\n'.join(lines) or '（无）')
        self.quote_status.setText(f"行情 · {sum(bool(item.get('fresh')) for item in markets.values())} / {len(codes)} 合约有新鲜报价")
        populate(self.instrument_grid,[[self.names.get(code,code),code,markets.get(code,{}).get('trading_day','—'),
             display_value(markets.get(code,{}).get('last')),quote_states[code]] for code in codes])
        self.instrument_rules.set_markets({code:self.names.get(code,code) for code in codes}, markets)
        self.render_manual_tables(view)
        self.render_manual()
        state = (view['kernel_boot_id'],view['entry_authority'],view['connection']['fault'],view['connection']['synchronized'])
        if state != self.last_state:
            self.last_state = state
            self.add_activity(self.window.connection_detail.text())
        self.update_logs()
