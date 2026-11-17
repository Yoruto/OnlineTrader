"""Trading workspace. Widgets consume the accepted owner view."""
from collections import deque
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from io import StringIO
from pathlib import Path
import re
import time
from typing import NamedTuple

from PySide6.QtCore import QBuffer, QIODevice, QItemSelectionModel, QPointF, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSplitter, QSizePolicy, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTabBar, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import load_profile, publish_new_bytes
from online_trader.configuration.rollover import expiry_date, expiry_warning, future_product, preview_rollover
from online_trader.configuration.symbol_csv import ALIASES, PRODUCT_NAMES
from online_trader.contracts.codec import strict_loads
from online_trader.desktop.reports import EquityChart
from online_trader.desktop.runtime_logs import (
    JsonLogTail, comparison_file, executor_log_path, matches_operation_log_recovery, matches_run_identity,
    operation_log_message, resolve_run,
)
from online_trader.errors import TraderError
from online_trader.runtime.comparison import dsv_log_category
from online_trader.runtime.strategy_log import BLOCK_MESSAGES, ENTRY_FILTERS


BEIJING = timezone(timedelta(hours=8))
ENTRY_MESSAGES = {
    'GEOMETRY_BLOCK': '几何条件不满足，暂不建仓',
    'WAITING_FRESH_QUOTE': '等待行情',
    'MARKET_PATH_INCOMPLETE': '行情路径待补齐',
    'OPENING_WAIT': '开盘等待',
    'AUCTION_DISABLED': '集合竞价期间暂停普通开仓',
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
QUIET_ENTRY_REASONS = {'CANDLE_OPPORTUNITY_CONSUMED', 'OBSERVATION_ONLY_BACKLOG',
                       'LONG:TRIGGER_NOT_REACHED', 'SHORT:TRIGGER_NOT_REACHED', 'DUPLICATE_MARKET_EVENT'}
ORDER_HEADERS = ('报单号','合约','方向','开平','委托量','未成交','委托价','状态','报单时间','消息')
TRADE_HEADERS = ('成交号','合约','方向','开平','手数','成交价','手续费','成交时间')
POSITION_HEADERS = ('合约','方向','持仓量','成交价','止损价','止盈价','实时价格',
                    '盈利价差','持仓盈亏','浮盈比例','保证金','手续费')
SYMBOL_HEADERS = ('品种名', '合约代码', '开仓偏移', '止损偏移', '止盈偏移', '下单手数', '半仓参数', '滞回点')
LOG_EVENT_FILTERS = {
    '开仓': {'open', 'auto'}, '平仓': {'close', 'auto', 'half', 'close_win', 'close_loss'},
    '错误': {'error'}, '手动': {'manual'},
}
LOG_TAGS = {'开仓':'open','平仓':'close','自动':'auto','错误':'error','手动':'manual',
            '半仓':'half','风控':'error','警告':'error'}
LOG_PHASES = {
    'DSV_PRIMED_DISARMED': '策略已就绪，等待启动或同步',
    'DSV_OBSERVING': '策略正在监控所选合约',
    'DSV_MANAGING_OWNED': '继续管理已有持仓',
    'DSV_WAITING_GATEWAY_SYNC': '行情或交易连接中断，等待重新同步',
    'DSV_DAY_CHANGE_ORIGINAL_EXITS_NEED_RECONCILIATION': '柜台换日，等待核对原委托及持仓',
    'DSV_STOPPING': '已停止开仓，继续保护并等待核对',
    'DSV_STOPPED': '策略已停止',
    'DSV_CLOCK_FAULT': 'Windows 时间待核对，暂停开仓和定时调度',
    'DSV_CLOCK_PROTECTION_BLOCKED': '本地自动保护待恢复，保留原持仓退出目标',
    'DSV_ENTRY_RECOVERY_PENDING': '等待柜台核对和实时行情，开仓权限暂挂',
    'CLOCK_REFRESH_RETRY_PROTECTION_ACTIVE': '参考时钟暂不可用，继续保护已有持仓',
    'DSV_CONTROLLER_INTERRUPTED_ORIGINAL_PROTECTION_CONTINUES': '控制连接中断，继续管理原持仓',
}

CLOCK_PROTECTION_REASONS = {
    'CLOCK_PROTECTION_MONOTONIC_UNPROVED': '连续的单调时间证据缺失',
    'CLOCK_PROTECTION_CONNECTION_UNPROVED': '原行情或柜台连接待核对',
    'CLOCK_PROTECTION_SESSION_UNPROVED': '原盘次或柜台交易日待确认',
    'RECONCILIATION_REQUIRED': '持仓及原委托待核对',
    'E_UNKNOWN_SEND': '报单或撤单结果待确认',
    'E_NATIVE_QUOTE_NOT_CURRENT': '等待同盘次有效的原生报价',
    'E_OWNERSHIP': '柜台持仓、委托或今昨仓待核对',
    'E_FACT_BARRIER': '柜台委托及逐笔成交待核对',
}


class WorkbenchLogLine(NamedTuple):
    text: str
    category: str | None = None
    timestamp: datetime | None = None


def log_time(value):
    try:
        moment = beijing(value)
    except (TypeError, ValueError, OverflowError):
        return '—', None
    return moment.strftime('%H:%M:%S'), moment


def display_quote_delay(value):
    if value is None:
        return '--（需启动策略）'
    lag = Decimal(value)
    if lag > 120:
        return '--（休市/无行情）'
    status = '正常' if lag <= 3 else '偏慢' if lag <= 10 else '很慢/疑似断流'
    return f'{lag:.1f} 秒（{status}）'

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
QWidget { color: #162236; }
QWidget#tradingWorkspace { background: #f4f7fb; }
QWidget#workspacePage, QScrollArea#workbenchScroll, QTabWidget::pane {
    background: #fafcff; border: 0;
}
QFrame#workspaceTop { background: #fafdff; border-bottom: 1px solid #d7e1ef; }
QLabel#brand { color: #111d30; font-size: 22px; font-weight: 700; padding-right: 10px; }
QLabel#topStatus { font-size: 13px; color: #314057; padding-right: 7px; }
QLabel#workspaceTitle { font-size: 26px; font-weight: 700; color: #101a2b; padding: 1px 0 5px; }

QGroupBox {
    background: #ffffff; border: 1px solid #d8e2ef; border-radius: 8px;
    margin-top: 13px; font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; color: #2d435e; }
QGroupBox QWidget { font-weight: normal; }
QGroupBox#workspaceCard {
    background: #ffffff; margin-top: 0; padding-top: 24px;
}
QGroupBox#workspaceCard::title {
    subcontrol-origin: padding; top: 10px; left: 12px; padding: 0 0 0 8px;
    border-left: 3px solid #2b7bf4; color: #111c2e; font-size: 15px; font-weight: 700;
}
QFrame#workspaceFunds {
    background: #f7faff; border: 1px solid #d8e3f2; border-radius: 8px;
}
QLabel#fundMetric { color: #172438; font-size: 14px; font-weight: 600; padding: 5px 8px; }
QLabel#fundMetric[gain="true"] { color: #bd263e; }
QLabel#fundMetric[loss="true"] { color: #08764d; }
QFrame#fundDivider { background: #d5e0ef; }
QSplitter::handle { background: transparent; }

QTabBar::tab {
    background: #eff4fa; color: #43546b; border: 1px solid #dee7f2;
    padding: 7px 13px;
}
QTabBar::tab:selected { background: #e4efff; color: #075de5; font-weight: 600; }
QTabWidget#workspaceNavigation::pane {
    background: #fafcff; border: 1px solid #e3eaf4; border-radius: 9px;
}
QTabWidget#workspaceSection::pane { border-top: 1px solid #e4ebf4; }
QTabWidget#workspaceSection > QTabBar::tab {
    background: transparent; color: #34465e; border: 0; border-bottom: 2px solid transparent;
    padding: 8px 15px;
}
QTabWidget#workspaceSection > QTabBar::tab:hover { background: #eef5ff; }
QTabWidget#workspaceSection > QTabBar::tab:selected {
    color: #075de5; border-bottom-color: #0963f7; font-weight: 700;
}

QPushButton {
    background: #ffffff; color: #253750; border: 1px solid #d4e0ef;
    border-radius: 6px; padding: 5px 10px;
}
QPushButton:hover { background: #eef5ff; border-color: #a7c8fa; }
QPushButton:pressed { background: #dceaff; }
QPushButton:focus { border-color: #4388f5; }
QPushButton:disabled { color: #6a7b91; background: #f0f3f8; border-color: #e1e8f1; }
QPushButton#strategyStart, QPushButton#strategyPause { font-weight: 700; padding: 7px 15px; }
QPushButton#strategyStart { background: #0963f7; color: white; border-color: #0963f7; }
QPushButton#strategyStart:hover { background: #074fc8; border-color: #074fc8; }
QPushButton#strategyPause { background: #fff7f2; border-color: #e8a082; color: #a03816; }
QPushButton#strategyPause:hover { background: #ffece0; border-color: #d47550; }
QPushButton#strategyStart:disabled, QPushButton#strategyPause:disabled {
    background: #ebf0f7; color: #657891; border-color: #dce5f0;
}
QPushButton#workspaceAccount {
    background: #e8f1ff; border-color: #d5e5ff; color: #1459b9; font-weight: 600;
}
QPushButton#workspaceAccount:hover { background: #dceaff; }

QLineEdit, QSpinBox, QComboBox {
    background: #ffffff; border: 1px solid #d3dfed; border-radius: 5px; padding: 3px 6px;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #4388f5; }
QTableWidget {
    background: #ffffff; alternate-background-color: #f9fbfe;
    border: 1px solid #e0e8f3; border-radius: 6px;
    selection-background-color: #dceaff; selection-color: #172438;
    outline: 0;
}
QTableWidget::item { padding: 3px 7px; border-bottom: 1px solid #edf2f8; }
QTableWidget::item:selected, QTableWidget::item:selected:!active {
    background: #dceaff; color: #172438; border-bottom: 1px solid #d5e6ff;
}
QHeaderView::section {
    background: #eff4fa; color: #33455d; border: 0;
    border-bottom: 1px solid #e0e8f3; padding: 6px 5px; font-weight: 600;
}
QTableWidget#dashboardGrid { border: 0; }
QLabel#decisionStatus {
    background: #e7f1ff; color: #084caf; border-radius: 6px;
    font-size: 16px; font-weight: 700; padding: 9px 11px;
}
QLabel#workspaceDetail { color: #26384f; padding: 2px 3px; }
QFrame#decisionField { background: #f4f8fd; border-radius: 3px; }
QLabel#decisionValue { font-weight: 600; color: #152640; }
QLabel#decisionReason {
    background: #f1f7ff; border: 1px solid #dceafe; border-radius: 6px;
    padding: 8px 10px; color: #2a4363;
}
QLabel#chartTitle { font-size: 15px; font-weight: 700; }
QLabel#chartSource, QLabel#workspaceNote { color: #5b6d84; }
QLabel#homeMoney { font-size: 22px; font-weight: 700; }
QLabel#homeAccountType { color: #214da5; background: #eaf1ff; border-radius: 4px; padding: 3px 7px; }
QLabel#homeAccountType[live="true"] { color: #7c4c08; background: #fff3df; }
QLabel#homePending {
    color: #8b4e0e; background: #fff6e4; border: 1px solid #f3dfb8;
    border-radius: 6px; padding: 5px 9px;
}
QLabel#waiting, QLabel#blocked, QLabel#pending { padding: 7px; }
QLabel#waiting { background: #ecfaf2; color: #195d40; }
QLabel#blocked { background: #fff0f0; color: #9d252c; }
QLabel#pending { background: #fff5e8; color: #884605; }
QPlainTextEdit#runtimeLog {
    background: #1d202a; color: #e4ebfa; border: 1px solid #394355;
    border-radius: 6px; font-family: Consolas;
}
"""


def detail_markup(lines):
    cells = []
    for index, line in enumerate(lines):
        label, separator, value = line.partition('    ')
        background = '#f5f8fd' if index % 2 == 0 else '#ffffff'
        row = (f'<td>{escape(label)}</td><td><b>{escape(value)}</b></td>' if separator
               else f'<td colspan="2"><b>{escape(label)}</b></td>')
        cells.append(f'<tr bgcolor="{background}">{row}</tr>')
    return '<table width="100%" cellpadding="4" cellspacing="0">' + ''.join(cells) + '</table>'


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
    if isinstance(parent_layout, QSplitter):
        parent_layout.addWidget(box)
        parent_layout.setStretchFactor(parent_layout.indexOf(box), stretch)
    else:
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


class MinuteCandles(QWidget):
    """Display the most recent 60 native one-minute candles in source order."""

    def __init__(self):
        super().__init__()
        self.native_series = {}
        self.trigger_lines = {}
        self.code = ''
        self.setMinimumHeight(105)

    def select(self, code):
        self.code = code
        self.update()

    def show_native_bars(self, payload):
        self.native_series = {code:[(row[0],*map(Decimal,row[1:])) for row in bars]
                              for code,bars in payload.items()}
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#ffffff'))
        painter.setPen(QColor('#52647d'))
        candles = self.native_series.get(self.code)
        if not candles:
            painter.drawText(12, 27, '等待原生分钟 K 线')
            return
        trigger_prices = [Decimal(value) for value in self.trigger_lines.values()]
        low = min([item[3] for item in candles] + trigger_prices)
        high = max([item[2] for item in candles] + trigger_prices)
        span = high-low
        plot = self.rect().adjusted(10, 15, -80, -24)
        painter.setPen(QColor('#d7e0eb'))
        painter.drawRect(plot)
        step = plot.width()/max(len(candles), 8)

        def y(price):
            return (plot.bottom()-8-(plot.height()-16)*float((price-low)/span)
                    if span else float(plot.center().y()))

        for index in range(5):
            level = high - (high-low)*index/4
            position = y(level)
            painter.setPen(QColor('#eff3f8'))
            painter.drawLine(QPointF(plot.left(), position), QPointF(plot.right(), position))
            painter.setPen(QColor('#52647d'))
            painter.drawText(QPointF(plot.right()+9, position+4),
                             format(level.quantize(Decimal('0.01')).normalize(), 'f'))
            if not span:
                break
        for index in range(0, len(candles), max(1, (len(candles)+4)//5)):
            x = plot.left()+(index+.5)*step
            stamp = datetime.fromtimestamp(candles[index][0]*60, BEIJING).strftime('%H:%M')
            painter.drawText(QPointF(max(plot.left(), min(x-18, plot.right()-36)), self.height()-5), stamp)
        for side, value in self.trigger_lines.items():
            position = y(Decimal(value))
            color = QColor('#0963f7' if side == 'LONG' else '#8a52b8')
            painter.setPen(QPen(color, 1, Qt.DashLine))
            painter.drawLine(QPointF(plot.left(), position), QPointF(plot.right(), position))
            tag = QRectF(plot.right()+4, position-8, 70, 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(tag, 3, 3)
            painter.setPen(QColor('#ffffff'))
            painter.drawText(tag, Qt.AlignCenter, str(value))
            painter.setPen(color)
            label_y = position + 15 if position < plot.top() + 18 else position - 4
            painter.drawText(QPointF(plot.left()+8, label_y), '多头触发' if side == 'LONG' else '空头触发')
        for index, (_, opening, maximum, minimum, closing) in enumerate(candles):
            x = plot.left() + (index+.5)*step
            color = QColor('#c8323c' if closing >= opening else '#23845b')
            painter.setPen(QPen(color, 1))
            painter.drawLine(QPointF(x, y(maximum)), QPointF(x, y(minimum)))
            painter.setPen(QPen(color, min(12, max(2, step*.52))))
            painter.drawLine(QPointF(x, y(opening)), QPointF(x, y(closing)+1))


class StatusBadge(QStyledItemDelegate):
    """Draw text and color together without changing the table's data or selection."""

    colors = {
        '保护中': ('#dff4e6', '#107041'),
        '价格保护中': ('#dff4e6', '#107041'),
        '持仓中': ('#dff4e6', '#107041'),
        '等待触发': ('#e1edff', '#0758c9'),
        '行情滞后': ('#fff1d3', '#945215'),
        '平仓待确认': ('#fff1d3', '#945215'),
        '保护待核对': ('#fff1d3', '#945215'),
        '当日停开': ('#fff1d3', '#945215'),
    }

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if index.data(Qt.DisplayRole) in self.colors:
            size.setWidth(size.width() + 12)
        return size

    def paint(self, painter, option, index):
        text = index.data(Qt.DisplayRole)
        colors = self.colors.get(text)
        if not colors or option.fontMetrics.horizontalAdvance(text) + 16 > option.rect.width():
            super().paint(painter, option, index)
            return
        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ''
        option.widget.style().drawControl(QStyle.CE_ItemViewItem, background, painter, option.widget)
        width = option.fontMetrics.horizontalAdvance(text) + 16
        badge = QRectF(option.rect.center().x() - width / 2, option.rect.center().y() - 9, width, 19)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colors[0]))
        painter.drawRoundedRect(badge, 9, 9)
        painter.setPen(QColor(colors[1]))
        painter.drawText(badge, Qt.AlignCenter, text)
        painter.restore()


class WorkspaceNavigation(QTabBar):
    """West placement with horizontal labels and native tab keyboard navigation."""

    icons = ('\ue7f4', '\ue8a5', '\ue9e9', '\ue9d5', '\ue713')

    def __init__(self, parent):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.icon_family = next((family for family in ('Segoe Fluent Icons', 'Segoe MDL2 Assets')
                                 if family in QFontDatabase.families()), None)

    def tabSizeHint(self, index):
        return QSize(155, 47)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        hover = self.tabAt(self.mapFromGlobal(self.cursor().pos())) if self.underMouse() else -1
        for index in range(self.count()):
            rect = self.tabRect(index).adjusted(1, 2, -9, -2)
            selected = index == self.currentIndex()
            if selected or index == hover:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor('#dceaff' if selected else '#eaf2ff'))
                painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QColor('#075de5' if selected else '#28394e'))
            if self.icon_family:
                icon_font = QFont(self.icon_family)
                icon_font.setPixelSize(17)
                painter.setFont(icon_font)
                painter.drawText(rect.adjusted(12, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, self.icons[index])
            label_font = QFont(self.font())
            label_font.setBold(selected)
            painter.setFont(label_font)
            painter.drawText(rect.adjusted(38 if self.icon_family else 14, 0, -6, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, self.tabText(index))
            if selected and self.hasFocus():
                painter.setPen(QPen(QColor('#75a9f6'), 1, Qt.DotLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)



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
        self.preview_symbols = self.symbols
        self.names = {item['code']: item['name'] for item in self.symbols}
        self.activity = deque(maxlen=200)
        self.last_state = None
        self.last_log_text = None
        self.visible_log_lines = []
        self.log_tails = {}
        self.log_binding = None
        self.log_resolution_at = 0.0
        self.log_directory = None
        self.log_comparison = None
        self.balance_history = deque(maxlen=8640)
        self.last_balance_slot = None
        self.chart_owner = None
        self.rendered_view = None
        self.last_reasons = {}
        self.dashboard_alerts = {}
        self.dashboard_alert_owner = None
        self.manual_price_scope = None
        self.manual_catalog = []
        self.fund_values = {}
        self.home_updated_at = None
        self.quote_reference = {}
        self.quote_worker = None

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
        shell.setContentsMargins(8, 0, 10, 6)
        shell.setSpacing(7)

        self.tabs = window.tabs = QTabWidget()
        self.tabs.setObjectName('workspaceNavigation')
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setTabBar(WorkspaceNavigation(self.tabs))
        self.tabs.setTabPosition(QTabWidget.West)
        self.tabs.setElideMode(Qt.ElideNone)
        self.buttons['arm'].setObjectName('strategyStart')
        window.pause_button.setObjectName('strategyPause')
        window.pause_button.setText('停止策略 (F6)')
        QShortcut(QKeySequence('F5'), panel, self.buttons['arm'].click)
        QShortcut(QKeySequence('F6'), panel, window.pause_button.click)
        self.toolbar(shell)
        self.sections = {}
        self.home_tab, self.home_sections = self.section('交易工作台')
        self.market_tab = self.add_page(self.home_sections, '行情监控', self.home_page)
        self.position_tab = self.add_page(self.home_sections, '策略持仓', self.position_page)

        _, self.order_sections = self.section('订单与成交')
        self.order_tab = self.add_page(self.order_sections, '当前委托', self.orders_page)
        self.add_page(self.order_sections, '成交记录', self.fills_page)
        self.history_tab = self.add_page(self.order_sections, '历史订单', self.history_page)
        self.add_page(self.order_sections, '对账与报表', self.reports_page, scroll=True)
        self.manual_tab = self.add_page(self.order_sections, '手动下单', self.manual_page, scroll=True)

        self.parameter_tab, self.parameter_sections = self.section('策略参数')
        self.symbol_tab = self.add_page(self.parameter_sections, '监控合约', self.symbol_parameters_page, scroll=True)
        self.add_page(self.parameter_sections, '交易参数与模板', self.strategy_page, scroll=True)
        self.add_page(self.parameter_sections, '偏移计算', self.offset_page, scroll=True)

        _, self.diagnostic_sections = self.section('日志与诊断')
        self.log_tab = self.add_page(self.diagnostic_sections, '运行事件', self.logs_page)
        self.monitor_tab = self.add_page(self.diagnostic_sections, '连接与行情', self.monitor_page, scroll=True)
        self.add_page(self.diagnostic_sections, '命令回执', self.receipts_page)

        _, self.account_sections = self.section('账户与设置')
        self.account_tab = self.add_page(self.account_sections, '账户', self.account_page, scroll=True)
        self.add_page(self.account_sections, '系统设置', self.system_page, scroll=True)
        self.add_page(self.account_sections, '品种信息', self.instruments_page, scroll=True)
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
        self.diagnostic_sections.currentChanged.connect(self.update_logs)
        self.render_home()
        self.workspace_clock = QLabel()
        window.statusBar().setStyleSheet(
            'QStatusBar { background:#f5f8fc; border-top:1px solid #dbe5f2; }'
            'QStatusBar QLabel { color:#485b75; padding:2px 7px; }'
            'QStatusBar::item { border:0; }')
        window.statusBar().addWidget(self.workspace_clock)
        self.update_workspace_clock()
        self.log_timer = QTimer(window)
        self.log_timer.setInterval(400)
        self.log_timer.timeout.connect(self.update_logs)
        self.log_timer.timeout.connect(self.update_workspace_clock)
        self.log_timer.start()
        for grid in (*self.tables.values(), window.cycles):
            configure_table(grid)

    def section(self, title):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(15, 11, 15, 12)
        layout.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName('workspaceTitle')
        layout.addWidget(heading)
        if not self.sections:
            self.home_header(layout)
        tabs = QTabWidget()
        tabs.setObjectName('workspaceSection')
        layout.addWidget(tabs, 1)
        self.tabs.addTab(page, title)
        self.sections[page] = tabs
        return page, tabs

    @staticmethod
    def add_page(tabs, title, build, *, scroll=False):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 9, 0, 0)
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
        for section, tabs in self.sections.items():
            if tabs.indexOf(page) >= 0:
                self.tabs.setCurrentWidget(section)
                tabs.setCurrentWidget(page)
                return
        self.tabs.setCurrentWidget(page)

    def show_account_settings(self):
        self.reload_accounts()
        self.show_details(self.account_tab)

    def update_workspace_clock(self):
        view = self.window.view
        trading_day = self.current_trading_day(view) if view and not self.window._stale and not view['truncated'] else None
        self.workspace_clock.setText(f"北京时间 {datetime.now(BEIJING):%H:%M:%S}    交易日 {trading_day or '—'}")

    def toolbar(self, shell):
        header = QFrame(objectName='workspaceTop')
        bar = QHBoxLayout(header)
        bar.setContentsMargins(9, 5, 9, 5)
        bar.setSpacing(8)
        brand = QLabel('OnlineTrader', objectName='brand')
        bar.addWidget(brand)
        self.toolbar_account = QPushButton('账户 · —', objectName='workspaceAccount')
        self.toolbar_account.clicked.connect(self.show_account_settings)
        bar.addWidget(self.toolbar_account)
        self.toolbar_status = []
        for _ in range(4):
            dot = QFrame()
            dot.setFixedSize(8, 8)
            label = QLabel(objectName='topStatus')
            label.setTextFormat(Qt.PlainText)
            bar.addWidget(dot)
            bar.addWidget(label)
            self.toolbar_status.append((dot, label))
        bar.addStretch(1)
        bar.addWidget(button('快捷键', self.shortcuts))
        shell.addWidget(header)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 2, 0)
        actions.addStretch(1)
        actions.addWidget(self.buttons['arm'])
        actions.addWidget(self.window.pause_button)
        shell.addLayout(actions)

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

    def home_header(self, layout):
        frame = QFrame(objectName='workspaceFunds')
        frame.setAccessibleName('资金概览')
        funds = QHBoxLayout(frame)
        funds.setContentsMargins(5, 5, 5, 5)
        funds.setSpacing(2)
        layout.addWidget(frame)
        self.dashboard_money = {}
        for key, caption in (
            ('equity', '账户权益'), ('available', '可用资金'),
            ('today_pnl', '当日权益变动'), ('float_profit', '浮动盈亏'),
        ):
            if self.dashboard_money:
                divider = QFrame(objectName='fundDivider')
                divider.setFixedSize(1, 20)
                funds.addWidget(divider)
            value = QLabel(f'{caption}   —', objectName='fundMetric')
            value.setAlignment(Qt.AlignCenter)
            funds.addWidget(value, 1)
            self.dashboard_money[key] = value, caption
        self.home_pending = QLabel(objectName='homePending')
        self.home_pending.setTextFormat(Qt.PlainText)
        self.home_pending.setWordWrap(True)
        layout.addWidget(self.home_pending)

    def home_page(self, layout):
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(10)
        layout.addWidget(split, 1)
        market, detail = QWidget(), QSplitter(Qt.Vertical)
        detail.setChildrenCollapsible(False)
        detail.setHandleWidth(8)
        split.addWidget(market)
        split.addWidget(detail)
        split.setSizes([950, 440])
        market_layout = QVBoxLayout(market)
        market_layout.setContentsMargins(0, 0, 0, 0)
        listing = group('监控合约', market_layout)
        listing.parentWidget().setObjectName('workspaceCard')
        self.dashboard_markets = table(('合约', '最新价', '当前关注', '持仓', '状态'))
        self.dashboard_markets.setObjectName('dashboardGrid')
        self.dashboard_markets.verticalHeader().setDefaultSectionSize(29)
        for column in (0, 2):
            self.dashboard_markets.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.dashboard_markets.setItemDelegateForColumn(4, StatusBadge(self.dashboard_markets))
        self.dashboard_markets.setSelectionMode(QAbstractItemView.SingleSelection)
        self.dashboard_market_rows = {}
        listing.addWidget(self.dashboard_markets)
        prices = group('行情', market_layout, stretch=1)
        prices.parentWidget().setObjectName('workspaceCard')
        self.candle_panel = prices.parentWidget()
        self.candle_header = QLabel(objectName='chartTitle')
        self.candle_source = QLabel(objectName='chartSource')
        self.candle_contract = QComboBox(prices.parentWidget())
        self.candle_contract.hide()
        self.candle_contract.currentTextChanged.connect(self.select_candles)
        self.candles = MinuteCandles()
        self.candles.setMinimumHeight(180)
        chart_heading = row(self.candle_header)
        chart_heading.addWidget(self.candle_source)
        prices.addLayout(chart_heading)
        prices.addWidget(self.candles, 1)
        explanation = group('策略解释', detail)
        self.explanation_card = explanation.parentWidget()
        self.explanation_card.setObjectName('workspaceCard')
        self.decision_status = QLabel('等待行情', objectName='decisionStatus')
        self.decision_status.setTextFormat(Qt.PlainText)
        self.decision_status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.decision_status.setMinimumHeight(38)
        explanation.addWidget(self.decision_status)
        self.decision_fields = {}
        for field in ('多头触发', '空头触发', '距离最近触发', '最近判断', '最后报价'):
            band = QFrame(objectName='decisionField')
            line = QHBoxLayout(band)
            line.setContentsMargins(8, 2, 8, 2)
            label = QLabel(field)
            value = QLabel('—', objectName='decisionValue')
            value.setTextFormat(Qt.PlainText)
            line.addWidget(label, 1)
            line.addWidget(value, 1)
            explanation.addWidget(band)
            self.decision_fields[field] = band, value
        self.decision_reason = QLabel(objectName='decisionReason')
        self.decision_reason.setTextFormat(Qt.RichText)
        self.decision_reason.setWordWrap(True)
        self.decision_reason.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        explanation.addWidget(self.decision_reason)
        events = group('重要事件', detail, stretch=1)
        detail.handle(1).setEnabled(False)
        events.parentWidget().setObjectName('workspaceCard')
        self.dashboard_events = table(('时间', '事件'))
        self.dashboard_events.setObjectName('dashboardGrid')
        self.dashboard_events.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.dashboard_events.cellDoubleClicked.connect(self.open_dashboard_event)
        events.addWidget(self.dashboard_events)
        self.home_event = QLabel(objectName='workspaceNote')
        self.home_event.setTextFormat(Qt.PlainText)
        self.home_event.setWordWrap(True)
        self.home_event.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        events.addWidget(self.home_event)
        self.home_updated = QLabel()
        self.home_updated.hide()
        self.dashboard_markets.currentCellChanged.connect(self.select_dashboard_market)

    def position_page(self, layout):
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(10)
        layout.addWidget(split, 1)
        master, detail = QWidget(), QWidget()
        split.addWidget(master)
        split.addWidget(detail)
        split.setSizes([950, 440])
        left, right = QVBoxLayout(master), QVBoxLayout(detail)
        left.setContentsMargins(0, 0, 0, 0)
        right.setContentsMargins(0, 0, 0, 0)
        positions = group('当前策略持仓', left, stretch=1)
        positions.parentWidget().setObjectName('workspaceCard')
        self.home_position_title = QLabel('策略持仓', objectName='workspaceNote')
        positions.addWidget(self.home_position_title)
        self.home_positions = table(('合约', '方向', '手数', '浮动盈亏', '状态', '开仓均价', '最新价'))
        self.home_positions.setObjectName('dashboardGrid')
        self.home_positions.verticalHeader().setDefaultSectionSize(31)
        for column in (0, 3, 4, 5, 6):
            self.home_positions.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.home_positions.setItemDelegateForColumn(4, StatusBadge(self.home_positions))
        self.home_positions.setSelectionMode(QAbstractItemView.SingleSelection)
        positions.addWidget(self.home_positions, 1)
        self.home_empty_positions = QLabel()
        positions.addWidget(self.home_empty_positions)
        events = group('近期持仓事件', left)
        events.parentWidget().setObjectName('workspaceCard')
        self.position_events = table(('时间', '合约', '事件'))
        self.position_events.setObjectName('dashboardGrid')
        events.addWidget(self.position_events)
        protection = group('持仓保护', right)
        protection.parentWidget().setObjectName('workspaceCard')
        self.position_status = QLabel(objectName='decisionStatus')
        self.position_status.setTextFormat(Qt.PlainText)
        self.position_detail = QLabel(objectName='workspaceDetail')
        self.position_detail.setTextFormat(Qt.RichText)
        for label in (self.position_status, self.position_detail):
            label.setWordWrap(True)
            protection.addWidget(label)
        self.related_orders = button('查看关联委托', self.open_position_orders)
        self.related_orders.setObjectName('workspaceAccount')
        protection.addWidget(self.related_orders)
        protection.addWidget(button('持仓明细与平仓', self.open_position_controls))
        right.addStretch(1)
        self.home_positions.currentCellChanged.connect(self.render_position_detail)

    def symbol_parameters_page(self, layout):
        self.symbols_page(layout)
        comparison = group('所选合约 · 运行中与草稿', layout)
        self.parameter_comparison = table(('参数', '运行中', '编辑草稿'))
        self.parameter_comparison.setFixedHeight(175)
        comparison.addWidget(self.parameter_comparison)
        comparison.addWidget(button('编辑所选草稿', self.edit_selected_symbol))
        self.symbol_grid.currentCellChanged.connect(self.render_symbol_comparison)

    def render_symbol_comparison(self, *_):
        cell = self.symbol_grid.item(self.symbol_grid.currentRow(), 1)
        code = cell.text() if cell else None
        view = self.window.view if not self.window._stale else None
        active = next((item for item in (view or {}).get('configuration', {}).get('strategy_symbols', ())
                       if item['code'] == code), {})
        draft = next((item for item in self.preview_symbols if item['code'] == code), {})
        fields = ('open_offset', 'stop_offset', 'profit_offset', 'order_volume', 'half_close_ratio', 'hysteresis_points')
        def value(record, key):
            return '自动' if key == 'order_volume' and record.get(key) == 0 else display_value(record.get(key))
        rows = [(title, value(active, key), value(draft, key)) for title, key in zip(SYMBOL_HEADERS[2:], fields)] if draft else []
        populate(self.parameter_comparison, rows)
        for index, (_, current, edited) in enumerate(rows):
            if current != '—' and current != edited:
                self.parameter_comparison.item(index, 2).setForeground(QColor('#075de5'))

    @staticmethod
    def fill_selectable(grid, rows, keys):
        current = grid.item(grid.currentRow(), 0)
        selected = current.data(Qt.UserRole) if current is not None else None
        blocked = grid.blockSignals(True)
        try:
            populate(grid, rows)
            for index, key in enumerate(keys):
                grid.item(index, 0).setData(Qt.UserRole, key)
            if keys:
                grid.setCurrentCell(keys.index(selected) if selected in keys else 0, 0)
        finally:
            grid.blockSignals(blocked)

    def render_dashboard_markets(self, view, cycles, available):
        config = (view or {}).get('configuration', {})
        markets = {item['contract_id']: item for item in (view or {}).get('markets', ())}
        symbols = config.get('strategy_symbols') or self.symbols
        names = {item['code']: item['name'] for item in symbols}
        codes = list(dict.fromkeys(config.get('scoped_contracts') or [*markets, *names]))
        if config.get('strategy_symbols') and not config.get('scoped_contracts'):
            codes = list(dict.fromkeys([*names, *markets]))
        monitor = config.get('strategy_monitor') or {}
        owner_states = {item['contract_id']: item for item in monitor.get('symbols', ())}
        resting = monitor.get('quote_status') == '休市'
        self.dashboard_market_rows = {}
        rows = []
        for code in codes:
            quote = markets.get(code, {})
            report = owner_states.get(code, {})
            owned = [cycle for cycle in cycles if cycle['contract_id'] == code]
            quantities = {side: sum(cycle['remaining_quantity'] for cycle in owned if cycle['direction'] == direction)
                          for side, direction in (('多', 'LONG'), ('空', 'SHORT'))}
            position = ' / '.join(f'{side} {qty}' for side, qty in quantities.items() if qty) or ('0' if available else '—')
            reasons = quote.get('entry_reasons', ())
            explanation = [self.entry_reason_text(reason) for reason in reasons
                           if reason != 'DUPLICATE_MARKET_EVENT']
            quote_current = self.market_view_current(view) and quote.get('fresh')
            lines = (quote.get('trigger_prices') or {}) if quote_current else {}
            decision = quote.get('decision_time_utc')
            if not self.market_view_current(view):
                status = '状态待同步'
            elif any(cycle.get('exit_pending') for cycle in owned) or report.get('state') == 'PENDING':
                status = '平仓待确认'
            elif quote.get('enabled') is False:
                status = '已停用'
            elif quote.get('day_stop_reason') or report.get('state') == 'STOPPED':
                status = '当日停开'
                explanation.insert(0, quote.get('day_stop_reason') or report.get('status') or status)
            elif not quote.get('fresh'):
                status = '休市' if resting else '行情滞后' if quote.get('source_time_utc') else '等待行情'
            elif owned:
                status = '持仓中'
            elif not available:
                status = '交易待同步'
            elif view['entry_authority'] != 'ARMED':
                status = '已停止开仓'
            elif 'GEOMETRY_BLOCK' in reasons:
                status = '几何条件暂不满足'
            elif any(reason not in QUIET_ENTRY_REASONS and reason != 'TRIGGER_NOT_REACHED' for reason in reasons):
                status = '当前条件未满足'
            else:
                status = '等待触发'
            nearby = None
            last = quote.get('last')
            if status == '等待触发' and last is not None and lines:
                nearby = min(lines, key=lambda side: abs(Decimal(lines[side]) - Decimal(last)))
            focus = f'{("多头" if nearby == "LONG" else "空头")}触发 {lines[nearby]}' if nearby else '—'
            rows.append((f'{code}  {names[code]}' if code in names else code,
                         display_value(last if self.market_view_current(view) else None), focus, position, status))
            self.dashboard_market_rows[code] = (status, quote, lines, decision, explanation, nearby)
        self.fill_selectable(self.dashboard_markets, rows, codes)
        self.dashboard_markets.setFixedHeight(
            self.dashboard_markets.horizontalHeader().sizeHint().height()
            + max(1, min(6, len(codes))) * self.dashboard_markets.verticalHeader().defaultSectionSize() + 4)
        for index, (_, _, _, position, status) in enumerate(rows):
            contract = self.dashboard_markets.item(index, 0)
            font = QFont(self.dashboard_markets.font())
            font.setWeight(QFont.DemiBold)
            contract.setFont(font)
            if position.startswith(('多', '空')) and '/' not in position:
                self.dashboard_markets.item(index, 3).setForeground(
                    QColor('#bd263e' if position.startswith('多') else '#08764d'))
            item = self.dashboard_markets.item(index, 4)
            color = ('#087453' if status == '持仓中' else '#965619'
                     if status in {'平仓待确认', '行情滞后', '当日停开', '已停用'} else '#075de5')
            item.setForeground(QColor(color))
        self.select_dashboard_market()

    def market_view_current(self, view):
        return bool(view and not self.window._stale and not view['truncated'])

    @staticmethod
    def entry_reason_text(reason):
        code = reason.rsplit(':', 1)[-1]
        if code == 'TRIGGER_NOT_REACHED':
            return '价格尚未达到触发位'
        return (ENTRY_MESSAGES.get(reason) or ACTIVATION_MESSAGES.get(reason)
                or BLOCK_MESSAGES.get(code) or ENTRY_FILTERS.get(code, (None, None))[1]
                or f'策略条件待核对（{code}）')

    @staticmethod
    def market_time(stamp):
        if not stamp:
            return '—'
        return datetime.fromisoformat(stamp).astimezone(BEIJING).strftime('%H:%M:%S')

    def select_dashboard_market(self, *_):
        cell = self.dashboard_markets.item(self.dashboard_markets.currentRow(), 0)
        code = cell.data(Qt.UserRole) if cell is not None else ''
        record = self.dashboard_market_rows.get(code)
        self.candles.trigger_lines = {}
        details = {}
        explanation = ()
        if record is None:
            self.decision_status.setText('等待行情')
        else:
            status, quote, lines, decision, explanation, nearby = record
            self.decision_status.setText(f'{code} · {status}')
            details = {
                '多头触发': display_value(lines.get('LONG')),
                '空头触发': display_value(lines.get('SHORT')),
                '最近判断': self.market_time(decision),
                '最后报价': self.market_time(quote.get('source_time_utc')),
            }
            if nearby and quote.get('price_tick') and Decimal(quote['price_tick']) > 0:
                ticks = abs(Decimal(lines[nearby]) - Decimal(quote['last'])) / Decimal(quote['price_tick'])
                details['距离最近触发'] = f'{ticks.normalize():f} 跳'
            self.candles.trigger_lines = lines
        for key, (band, value) in self.decision_fields.items():
            value.setText(details.get(key, '—'))
            band.setVisible(key in details)
        self.decision_reason.setVisible(bool(explanation))
        if explanation:
            reasons = escape(' / '.join(dict.fromkeys(explanation)))
            self.decision_reason.setText(f'<b>本轮判断依据</b><br>{reasons}')
            self.decision_reason.setMinimumHeight(max(42, self.decision_reason.heightForWidth(
                max(180, self.explanation_card.width() - 18))))
            self.decision_reason.setToolTip(reasons)
        layout = self.explanation_card.layout()
        layout.invalidate()
        self.explanation_card.setFixedHeight(layout.sizeHint().height() + 26)
        self.select_candles(code)

    def selected_position(self):
        row = self.home_positions.currentRow()
        cell = self.home_positions.item(row, 0)
        return cell.data(Qt.UserRole) if cell is not None else None

    def render_position_detail(self, *_):
        selected = self.selected_position()
        view = self.window.view
        self.related_orders.setEnabled(bool(selected))
        if view is None or not selected:
            synchronized = view and not self.window._stale and view['connection'].get('synchronized')
            self.position_status.setText('当前无策略持仓' if synchronized else '等待持仓同步')
            self.position_detail.clear()
            self.position_events.setRowCount(0)
            return
        code, direction = selected
        cycles = [cycle for cycle in view['cycles'] if cycle['contract_id'] == code
                  and cycle['direction'] == direction and cycle['remaining_quantity'] > 0]
        status = self.home_positions.item(self.home_positions.currentRow(), 4).text()
        self.position_status.setText(f'{code} · {"多" if direction == "LONG" else "空"} · {status}')
        lines = []
        current = (not self.window._stale and not view['truncated'] and view['connection'].get('synchronized')
                   and not view['connection'].get('fault') and not view['connection'].get('storage_failed'))
        for index, cycle in enumerate(cycles, 1):
            if len(cycles) > 1:
                lines.append(f'持仓周期 {index}')
            lines.append(f'剩余持仓    {cycle["remaining_quantity"]} 手')
            if current:
                confirmation = cycle.get('initial_open_confirmation') or {}
                confirmed = confirmation.get('quantity')
                lines.append(f'首次确认成交    {confirmed} 手' if confirmed is not None else '首次确认成交    —')
                covered = cycle['remaining_quantity'] if status in {'保护中', '价格保护中'} and cycle.get('stop_price') is not None else None
                lines.append(f'本地保护覆盖    {covered} 手' if covered is not None else '本地保护覆盖    待核对')
                lines.append(f'当前保护价    {display_value(cycle.get("stop_price"))}')
                lines.append(f'止盈价    {display_value(cycle.get("take_profit_price"))}')
                floor = (display_value(cycle.get('stop_price')) if cycle.get('mode') == 'PROTECTED'
                         else '未激活' if cycle.get('mode') == 'NORMAL' else '—')
                lines.append(f'留利线    {floor}')
        self.position_detail.setText(detail_markup(lines))
        cycle_ids = {cycle['position_cycle_id'] for cycle in cycles}
        rows = [(native_time(item.get('source_time_ns')), code,
                 f'{"开仓" if item["effect"] == "OPEN" else "平仓"}成交 {item["quantity"]} 手')
                for item in view['trades'] if item.get('position_cycle_id') in cycle_ids]
        self.fill_home_table(self.position_events, rows[-6:])

    def open_position_controls(self):
        self.show_details(self.monitor_tab)
        self.monitor_details.setCurrentWidget(self.window.cycles)

    def open_position_orders(self):
        selected = self.selected_position()
        if selected and self.window.view is not None:
            code, direction = selected
            self.order_cycles = frozenset(cycle['position_cycle_id'] for cycle in self.window.view['cycles']
                                          if cycle['contract_id'] == code and cycle['direction'] == direction
                                          and cycle['remaining_quantity'] > 0)
            self.order_scope.setText(f'关联持仓 · {code} · {"多" if direction == "LONG" else "空"}')
            self.order_scope.show()
            self.clear_order_scope.show()
            self.history_scope.setText(self.order_scope.text())
            self.history_scope.show()
            self.clear_history_scope.show()
            for combo in (self.order_contract, self.order_effect, self.order_side, self.order_source):
                combo.setCurrentIndex(0)
            self.filter_orders()
            self.show_details(self.history_tab if self.order_active_count == 0 and self.historical_orders.rowCount()
                              else self.order_tab)

    def record_dashboard_alerts(self, view):
        if not self.market_view_current(view):
            return
        owner = view['account_key'], view['kernel_boot_id']
        if owner != self.dashboard_alert_owner:
            self.dashboard_alert_owner = owner
            self.dashboard_alerts.clear()
        active = {}
        actionable = {'平仓待确认', '行情滞后', '当日停开', '已停用'}
        for code, (status, quote, _lines, _decision, explanation, _nearby) in self.dashboard_market_rows.items():
            if status in actionable:
                latest = self.market_time(quote.get('source_time_utc'))
                detail = f'最后报价 {latest}' if status == '行情滞后' else (explanation[0] if explanation else '')
                active[(code, status)] = detail if detail != status else ''
        for index, (code, _direction) in enumerate(self.position_groups):
            status = self.home_positions.item(index, 4).text()
            if status in {'平仓待确认', '平仓待核对', '保护待核对', '本地自动保护待恢复'}:
                active.setdefault((code, status), '')
        for (code, status), detail in active.items():
            if (code, status) not in self.dashboard_alerts:
                self.add_activity(f'{code} · {status}' + (f' · {detail}' if detail else ''), category='warning')
        self.dashboard_alerts = active

    def render_dashboard_events(self, view):
        events = [(line.timestamp.strftime('%H:%M:%S') if line.timestamp else '—',
                   line.text.split('] ', 2)[-1]) for line in reversed(self.activity)]
        populate(self.dashboard_events, events[:20] if view is not None else [])

    def open_dashboard_event(self, row, _):
        event = self.dashboard_events.item(row, 1)
        if event is not None:
            for combo in (self.log_filter, self.log_event_filter, self.log_time_filter, self.log_contract_filter):
                combo.setCurrentIndex(0)
            self.log_search.setText(event.text())
            self.log_view_tabs.setCurrentIndex(0)
            self.show_details(self.log_tab)

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

    def reset_account_view(self, *, preserve_logs=False):
        """Discard the previous account's display facts before requesting a new view."""
        self.rendered_view = None
        self.quote_reference.clear()
        if getattr(self.quote_worker, 'offset_query', None) is not None:
            self.quote_worker.requestInterruption()
            self.offset_preview.setText('账户已切换，请按当前账号重新联网计算。')
        self.fund_values.clear()
        self.home_updated_at = None
        self.chart_owner = None
        self.last_state = None
        self.last_reasons.clear()
        self.dashboard_alerts.clear()
        self.dashboard_alert_owner = None
        if not preserve_logs:
            self.activity.clear()
            self.log_binding = None
            self.log_directory = None
            self.log_comparison = None
            self.log_tails.clear()
        self.balance_history.clear()
        self.last_balance_slot = None
        self.equity.set_rows([])
        self.candles.native_series.clear()
        self.candles.select('')
        self.candle_contract.clear()
        self.dashboard_market_rows = {}
        self.position_events.setRowCount(0)
        self.historical_orders.setRowCount(0)
        self.reset_order_scope()
        self.order_detail.clear()
        self.historical_orders.detail_label.clear()
        self.manual_code.clear()
        self.manual_price_scope = None
        self.manual_catalog = []
        self.manual_scoped_codes = ()
        self.manual_catalog_status.clear()
        self.manual_status.setText('等待所选账号同步')
        self.manual_funds.setText('资金未知')
        for label, title in self.fund_metrics.values():
            label.setText(f'{title}   —')
        self.render_strategy_monitor(None)
        for label in self.status_labels:
            label.setText('等待同步')
        self.quote_status.setText('合约行情 · 等待所选账号同步')
        self.instrument_grid.setRowCount(0)
        self.instrument_rules.set_markets(self.names, {})
        self.render_session_hours({})
        self.render_rollover_status({})
        if not preserve_logs:
            self.visible_log_lines.clear()
            self.last_log_text = None
            self.log_text.clear()
            self.log_record_keys = []
            self.log_records.setRowCount(0)
            self.render_log_detail()
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
                description = '状态数据不完整，请在日志与诊断中核对'
        elif not connected:
            state, description = '尚未连接', '连接设置位于「账户与设置」'
        elif not synced or connection.get('recovery_required'):
            state, description = '正在核对', '账户已连接 · 等待柜台或持仓核对'
        elif armed and config.get('manual_workbench'):
            state, description = '人工开仓已启用', '账户已连接'
        elif armed:
            count = len(config.get('scoped_contracts') or view['markets'])
            state, description = '策略运行中', f'账户已连接 · 正在监控 {count} 个合约'
        else:
            state = '已停止新开仓'
            description = f'仍有 {quantity} 手策略持仓 · 保护状态见「策略持仓」' if quantity else '账户已连接 · 等待启动'
        account = w.selected_account
        view_key = view['account_key'] if view else w.paired_account_key
        if account and view_key in (None, account['account_key']):
            provider = {'TQ_SIMNOW': 'SimNow 模拟', 'TQKQ': '快期模拟'}.get(account.get('provider'), account_type)
            login = account['masked_login']
        else:
            provider = account_type
            login = f'****{account_login[-4:]}' if account_login not in {'未提供', '未选择'} and '*' not in account_login else account_login
        self.toolbar_account.setText(' · '.join(filter(None, (provider, login))))
        clock_diagnostic = config.get('strategy_diagnostics', {}).get('ntp')
        clock = ('异常' if connection.get('clock_fault') or clock_diagnostic == 'NTP_SKEW'
                 else '待核对' if not available or clock_diagnostic == 'NTP_UNAVAILABLE' else '无已知异常')
        health = ('待核对' if stale or fault or truncated else '已连接' if connected else '未连接')
        quotes = (view or {}).get('markets', ())
        monitor_state = (config.get('strategy_monitor') or {}).get('quote_status')
        lagging = sum(not item.get('fresh') and bool(item.get('source_time_utc')) for item in quotes)
        if not available or not quotes:
            market_health = '待同步'
        elif monitor_state == '休市' and not any(item.get('fresh') for item in quotes):
            market_health = '休市'
        elif lagging:
            market_health = f'{lagging} 个合约滞后'
        elif any(not item.get('fresh') for item in quotes):
            market_health = '等待行情'
        else:
            market_health = '已更新'
        for (dot, label), (title, value, good, alert) in zip(self.toolbar_status, (
            ('行情', market_health, market_health == '已更新', market_health.endswith('个合约滞后')),
            ('交易', health, health == '已连接', stale or fault or truncated),
            ('策略', state, armed and available, state in {'状态待核对', '正在核对'}),
            ('校时', clock, clock == '无已知异常', clock == '异常'),
        )):
            label.setText(f'{title} {value}')
            label.setToolTip(description if title == '策略' else label.text())
            color = '#14a163' if good else '#eea124' if alert else '#a1afc1'
            dot.setStyleSheet(f'background:{color}; border-radius:4px;')
        self.home_state.setText(state)
        self.home_connection.setText(description)

        funds = self.fund_values if available else {}
        for key, (value, caption) in self.dashboard_money.items():
            amount = funds.get(key)
            value.setText(f'{caption}   {self.yuan(amount)}')
            for name, enabled in (('gain', bool(amount and amount.startswith('+'))),
                                  ('loss', bool(amount and amount.startswith('-')))):
                if value.property(name) != enabled:
                    value.setProperty(name, enabled)
                    value.style().unpolish(value)
                    value.style().polish(value)
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
        self.fill_selectable(self.home_positions, positions, self.position_groups)
        self.render_position_detail()
        self.home_positions.setVisible(bool(positions))
        self.home_empty_positions.setVisible(not positions)
        self.home_empty_positions.setText('当前无策略持仓' if available else '持仓待同步')
        for index, position in enumerate(positions):
            direction = self.home_positions.item(index, 1)
            direction.setForeground(QColor('#bd263e' if position[1] == '多' else '#08764d'))
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
        self.home_event.setToolTip(self.home_event.text())
        self.render_dashboard_markets(view, cycles, available)
        self.record_dashboard_alerts(view)
        self.render_dashboard_events(view)
        self.render_symbol_comparison()
        updated = self.home_updated_at if view is not None else None
        self.home_updated.setText(('最后更新 ' if stale else '状态更新 ') + updated if updated else '')

    @staticmethod
    def yuan(amount):
        if amount is None:
            return '—'
        return f'{amount[0]}¥{amount[1:]}' if amount[:1] in ('+', '-') else f'¥{amount}'

    def home_position_rows(self, view, cycles, available):
        self.position_groups = []
        if view is None:
            return [], []
        connection = view['connection']
        pending = []
        by_cycle = {}
        failed_exits = {block.get('position_cycle_id') for block in view['blocks']
                        if block.get('active') and block['reason_code'] == 'EXIT_REDUCTION_TERMINAL_UNFILLED'}
        protection = view['configuration'].get('strategy_diagnostics', {}).get('clock_protection', {})
        protection_blocked = protection.get('blocked_cycles', {}) if connection.get('clock_fault') else {}
        protection_active = protection.get('active_cycles', ()) if connection.get('clock_fault') else ()
        if available:
            if connection.get('recovery_required'):
                pending.append('柜台持仓待核对')
            if view['configuration'].get('stop_cancel_pending'):
                pending.append('停止时撤单待确认')
            if view['configuration'].get('strategy_stop_wait'):
                pending.append(view['configuration']['strategy_stop_wait'])
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
            current = not self.window._stale and not view['truncated']
            if current and cid in protection_blocked:
                status = '本地自动保护待恢复'
                reason = protection_blocked[cid]
                pending.append(f'{code} {status} · {CLOCK_PROTECTION_REASONS.get(reason, reason)}')
            elif current and cid in protection_active:
                if cid in failed_exits:
                    status = '平仓待核对'
                    pending.append(f'{code} 平仓剩余目标待核对')
                else:
                    status = '平仓待确认' if cycle.get('exit_pending') else '价格保护中'
            elif not available:
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
            grouped.setdefault((code, cycle['direction']), []).append((cycle, floating, status))

        names = self.names | {item['code']: item['name'] for item in view['configuration'].get('strategy_symbols', [])}
        status_order = ('本地自动保护待恢复', '待核对', '平仓待核对', '平仓待确认', '交接待确认', '保护待核对',
                        '平仓待成交', '已交接', '价格保护中', '保护中')
        rows = []
        for (code, direction), items in grouped.items():
            name = names.get(code)
            quantity = sum(item[0]['remaining_quantity'] for item in items)
            floating = sum(Decimal(item[1]) for item in items) if all(item[1] is not None for item in items) else None
            status = min((item[2] for item in items), key=status_order.index)
            members = [item[0] for item in items]
            self.position_groups.append((code, direction))
            entries = [item.get('entry_price') for item in members]
            average = (sum(Decimal(price) * item['remaining_quantity'] for price, item in zip(entries, members)) / quantity
                       if all(price is not None for price in entries) else None)
            last = members[0].get('last_price') if available else None
            rows.append((f'{code}  {name}' if name else code, '多' if direction == 'LONG' else '空',
                         f'{quantity} 手', self.yuan(f'{floating:+,.2f}' if floating is not None else None), status,
                         format(average, ',.2f') if average is not None else '—', display_value(last)))
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
        self.timed_start_button = button('安排定时启动', self.window.arrange_timed_start)
        if self.window.store is not None and self.paired is None:
            schedule.addWidget(self.timed_start_button)

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
            if self.window.store is not None:
                self.quote_reference_button = button('订阅行情（新主力）', self.subscribe_reference_quotes)
                actions.addWidget(self.quote_reference_button)
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
        self.preview_symbols = settings['symbols']
        selected_item = self.symbol_grid.item(self.symbol_grid.currentRow(), 1)
        selected_code = selected_item.text() if selected_item else None
        if self.calculator.base_hash != profile.content_hash:
            self.calculator.set_base_profile(profile)
            self.offset_preview.setText('参数预览已更新，请重新计算偏移。')
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
        if self.symbol_grid.rowCount():
            index = next((index for index, item in enumerate(self.preview_symbols)
                          if item['code'] == selected_code), 0)
            self.symbol_grid.setCurrentCell(index, 1)
        self.render_symbol_comparison()

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
            self.filter_manual_catalog()
        self.instrument_rules.set_markets(self.names, {
            item['contract_id']: item for item in (self.rendered_view or {}).get('markets', [])})

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

    def subscribe_reference_quotes(self):
        account = self.window.selected_account
        if self.quote_worker is not None:
            return
        if self.window.view is not None and not self.window._stale:
            self.render_rollover_status({item['contract_id']:item for item in self.window.view['markets']})
            self.window.show_home_feedback('已刷新原执行器的主力与行情资料。')
            return
        if account is None:
            self.window.show_home_feedback('请先选择提供行情的模拟账号。')
            return
        def start():
            profile = self.window.current_start_profile()
            codes = [item['code'] for item in profile.to_record()['settings']['symbols']]
            if not codes:
                raise TraderError('E_CONFIG','品种列表为空。')
            self.window.show_home_feedback(f'正在订阅 {len(codes)} 个品种及对应主力行情。')
            self.add_activity(f'开始订阅 {len(codes)} 个品种的行情')
            self.start_market_read(account['account_id'], codes)
        self.window._action(start)

    def start_market_read(self, account_id, codes=(), *, catalog=False, offset_query=None):
        from online_trader.desktop.startup import QuoteReferenceWorker
        options = {'catalog': True} if catalog else {}
        if offset_query is not None:
            options['offset_query'] = offset_query
        worker = QuoteReferenceWorker(self.window.store, account_id, codes, self.window, **options)
        self.quote_worker = worker
        self.set_market_read_busy(True)
        worker.received.connect(self.window._quote_reference_received)
        worker.failed.connect(self.window._quote_reference_failed)
        worker.finished.connect(self.window._quote_reference_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def set_market_read_busy(self, busy):
        for name in ('quote_reference_button', 'manual_catalog_button', 'offset_market_button'):
            control = getattr(self, name, None)
            if control is not None:
                control.setEnabled(not busy)

    def reference_quotes_received(self, rows):
        worker = self.quote_worker
        account = self.window.selected_account
        if self.window._closing or worker is None or account is None or account['account_id'] != worker.account_id:
            return
        if getattr(worker, 'offset_query', None) is not None:
            if self.offset_requested_input != self.calculator.calculation_input():
                self.offset_preview.setText('账号或计算输入已变化，请按当前参数重新联网计算。')
                return
            self.offset_preview.setText('请核对计算结果，勾选后可应用到本地参数预览。'
                                        if self.calculator.calculate_with_market(rows) else '联网数据已读取，计算未完成。')
            return
        if getattr(worker,'catalog',False):
            self.manual_catalog = rows
            self.manual_catalog_status.setText(f'已加载 {len(rows)} 个期货合约' if rows else '行情服务未返回合约')
            self.add_activity(self.manual_catalog_status.text())
            self.filter_manual_catalog()
            return
        self.quote_reference = {row['contract_id']:row | {'quote_reference_ready':row['ready']} for row in rows}
        markets = {row['contract_id']:row for row in (self.rendered_view or {}).get('markets',[])}
        self.render_rollover_status(markets)
        self.instrument_rules.set_markets(self.names,self.quote_reference | {code:row for code,row in markets.items() if row.get('fresh')})
        for row in rows:
            code = row['contract_id']
            message = (f"{code} → 主力 {row['main_contract'] or '—'}，最新价 {row['last'] or '—'}，"
                       f"保证金/手 {row['margin'] or '—'}，手续费/手 {row['commission'] or '—'}"
                       if row['ready'] else f'{code} 行情未就绪')
            self.add_activity(message)
        self.window.show_home_feedback(f'行情订阅完成 · {sum(row["ready"] for row in rows)} / {len(rows)} 个品种就绪。')
        self.update_logs()

    def reference_quotes_failed(self, code):
        account = self.window.selected_account
        if (not self.window._closing and self.quote_worker is not None and account is not None
                and self.quote_worker.account_id == account['account_id']):
            if getattr(self.quote_worker, 'offset_query', None) is not None:
                messages = {
                    'E_OFFSET_DAY': '截止交易日与可核对的当前交易日不一致。',
                    'E_OFFSET_SESSION': '所选合约未处于共同交易时段，可继续使用离线计算。',
                    'E_CREDENTIALS': '所选账户的行情凭据不完整。',
                    'E_DEPENDENCY': '本机行情组件未就绪。',
                }
                detail = messages.get(code, '未取得完整的已完成日线和当日振幅。')
                self.offset_preview.setText(f'{detail} 原缓存及参数预览仍可用。 · {code}')
                return
            if getattr(self.quote_worker,'catalog',False):
                feedback = '已保留原合约目录' if self.manual_catalog else '仍可选择已配置合约'
                message = f'合约加载失败 · {feedback}（{code}）'
                self.manual_catalog_status.setText(message)
            else:
                message = f'行情订阅失败 · {code}'
                self.window.show_home_feedback(message)
            self.add_activity(message,category='error')

    def reference_quotes_finished(self):
        self.quote_worker = None
        self.set_market_read_busy(False)

    def render_rollover_status(self, markets):
        profile = self.window._settings_dialog.profile if self.window._settings_dialog is not None else self.profile
        settings = profile.to_record()['settings']
        symbols = settings['symbols']
        account = (self.rendered_view or {}).get('account_metrics') or {}
        from online_trader.configuration.instrument_rules import product_code
        for index, symbol in enumerate(symbols):
            market = markets.get(symbol['code'],{})
            reference = self.quote_reference.get(symbol['code'],{})
            facts = market.get('rollover') or {key:reference.get(key) for key in ('main_contract','main_expiry_date')}
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
        self.home_state = QLabel()
        self.home_connection = QLabel()
        self.home_quote_delay = QLabel()
        account.addLayout(row(self.home_state, self.home_connection, self.home_quote_delay))
        metrics = QGridLayout()
        self.fund_metrics = {}
        fields = (
            ('equity','账户权益'), ('available','可用资金'), ('margin','当前保证金'),
            ('margin_ratio','资金使用率'), ('risk_ratio','风险度'), ('local_time','本机时间'),
            ('close_profit','平仓盈亏'), ('position_profit','持仓盈亏'),
            ('today_pnl','当日累计'), ('fees','手续费'), ('today_ratio','盈亏比例'),
            ('opening_orders','开仓委托'),
        )
        for index,(key,title) in enumerate(fields):
            metric = QLabel(f'{title}   —')
            metric.setStyleSheet('color: #19498c;')
            metrics.addWidget(metric,index//7,index%7)
            self.fund_metrics[key] = (metric,title)
        account.addLayout(metrics)
        self.quote_status = QLabel('合约行情 · 等待首份快照')
        self.quote_status.setWordWrap(True)
        account.addWidget(self.quote_status)
        states = group('品种状态', layout, horizontal=True)
        self.status_labels = []
        for name, title in (('waiting','未触发当日停开'),('blocked','当日已停止开仓'),('pending','平仓待确认')):
            box = group(title, states, stretch=1)
            label = QLabel('等待同步')
            label.setObjectName(name)
            label.setWordWrap(True)
            label.setMinimumHeight(62)
            label.setAlignment(Qt.AlignTop)
            if name == 'waiting':
                label.setToolTip('品种尚未触发当日停开。开仓仍需全局授权，并满足当前信号和风控。')
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

    def reports_page(self, layout):
        equity = group('权益曲线', layout, stretch=1)
        self.equity_panel = equity.parentWidget()
        self.equity_mode = QComboBox()
        self.equity_mode.addItems(['权益曲线','收益曲线'])
        self.equity_from = QComboBox()
        self.equity_to = QComboBox()
        self.equity_mode.currentIndexChanged.connect(self.render_equity_chart)
        equity.addLayout(row(QLabel('曲线'),self.equity_mode,QLabel('起'),self.equity_from,
                             QLabel('止'),self.equity_to,button('应用',self.render_equity_chart)))
        equity.addLayout(row(self.window.reports_button,button('导出 PNG',self.export_equity_png),
                             button('导出 CSV',self.export_equity_csv)))
        self.equity = EquityChart()
        self.equity.setMinimumHeight(160)
        equity.addWidget(self.equity)
        layout.addLayout(row(button('导出日报', self.open_daily_report)))

    def select_candles(self, code):
        self.candles.select(code)
        self.candle_header.setText(f'{code} · 1 分钟' if code else '1 分钟')
        self.candle_source.setText('原生 1 分钟' if code in self.candles.native_series else '等待原生 K 线')

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
        self.order_source = QComboBox()
        self.order_source.addItems(['全部来源', '策略', '手动', '未知'])
        self.order_source.currentTextChanged.connect(self.filter_orders)
        self.order_count = QLabel('等待柜台同步')
        self.order_contract.currentTextChanged.connect(self.filter_orders)
        self.order_effect.currentTextChanged.connect(self.filter_orders)
        self.order_side.currentTextChanged.connect(self.filter_orders)
        controls = row(QLabel('品种'),self.order_contract,QLabel('方向'),self.order_side,QLabel('开平'),self.order_effect)
        controls.insertWidget(6, self.order_source)
        controls.addWidget(self.order_count)
        controls.addWidget(button('刷新', self.window.refresh))
        filters.addLayout(controls)
        controls.addWidget(button('手动下单', lambda: self.show_details(self.manual_tab)))
        self.order_scope = QLabel()
        self.clear_order_scope = button('清除关联筛选', self.reset_order_scope)
        layout.addLayout(row(self.order_scope, self.clear_order_scope))
        self.order_cycles = frozenset()
        self.clear_order_scope.hide()
        self.order_scope.hide()
        split = QSplitter(Qt.Horizontal)
        layout.addWidget(split, 1)
        orders = QWidget()
        order_layout = QVBoxLayout(orders)
        grid = self.tables['orders']
        grid.setObjectName('dashboardGrid')
        grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        grid.setColumnCount(len(ORDER_HEADERS))
        grid.setHorizontalHeaderLabels(ORDER_HEADERS)
        grid.horizontalHeader().setStretchLastSection(True)
        grid.currentCellChanged.connect(self.render_order_detail)
        order_layout.addWidget(grid)
        order_layout.addLayout(row(self.buttons['cancel'],self.buttons['cancel_entries'],self.buttons['cancel_account']))
        split.addWidget(orders)
        split.addWidget(self.order_details_panel(grid))
        split.setSizes([950, 440])

    def fills_page(self, layout):
        grid = self.tables['trades']
        grid.setColumnCount(len(TRADE_HEADERS))
        grid.setHorizontalHeaderLabels(TRADE_HEADERS)
        grid.setObjectName('dashboardGrid')
        layout.addWidget(grid)

    def history_page(self, layout):
        self.history_scope = QLabel()
        self.clear_history_scope = button('清除关联筛选', self.reset_order_scope)
        layout.addLayout(row(self.history_scope, self.clear_history_scope))
        self.history_scope.hide()
        self.clear_history_scope.hide()
        self.historical_orders = table(ORDER_HEADERS)
        self.historical_orders.setObjectName('dashboardGrid')
        self.historical_orders.setSelectionMode(QAbstractItemView.SingleSelection)
        self.historical_orders.currentCellChanged.connect(self.render_order_detail)
        split = QSplitter(Qt.Horizontal)
        layout.addWidget(split, 1)
        split.addWidget(self.historical_orders)
        split.addWidget(self.order_details_panel(self.historical_orders))
        split.setSizes([950, 440])

    def order_details_panel(self, grid):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        details = group('委托详情', layout)
        label = QLabel('选择委托查看详情', objectName='workspaceDetail')
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        details.addWidget(label)
        layout.addStretch()
        grid.detail_label = label
        if grid is self.tables['orders']:
            self.order_detail = label
        return panel

    def reset_order_scope(self):
        self.order_cycles = frozenset()
        self.order_scope.clear()
        self.order_scope.hide()
        self.clear_order_scope.hide()
        self.history_scope.clear()
        self.history_scope.hide()
        self.clear_history_scope.hide()
        self.filter_orders()

    def render_order_detail(self, *_):
        view = self.rendered_view
        if view is None or not hasattr(self, 'historical_orders'):
            return
        for grid in (self.tables['orders'], self.historical_orders):
            row = grid.currentRow()
            cell = grid.item(row, 0)
            order_id = cell.data(Qt.UserRole) if cell is not None and not grid.isRowHidden(row) else None
            order = next((item for item in view['orders'] if item.get('child_order_id') == order_id), None) if order_id else None
            if order is None:
                grid.detail_label.setText('选择委托查看详情')
                continue
            cells = order_cells(order)
            lines = [f"{order['contract_id']} · {cells[2]} · {cells[3]}", f'委托编号    {cells[0]}',
                     f'来源    {self.order_source_title(order)}', f'委托量    {cells[4]} 手',
                     f'已成交    {display_value(order.get("filled_qty"))} 手', '', '委托与成交记录']
            if order.get('source_time_ns'):
                lines.append(f'{cells[8]}    柜台报单时间')
            fills = sorted((trade for trade in view['trades'] if trade.get('child_order_id') == order_id),
                           key=lambda trade: int(trade.get('source_time_ns') or 0))
            lines.extend(f'{native_time(trade.get("source_time_ns"))}    成交 {trade["quantity"]} 手 @ {trade["price"]}'
                         for trade in fills)
            lines.append(f'当前柜台回报    {cells[7]}')
            if order.get('cancel_status'):
                states = {'SEND_POSSIBLE': '撤单已请求', 'CALL_RETURNED': '等待柜台结果', 'UNKNOWN': '撤单结果待核对'}
                status = order['cancel_status']
                lines.append('撤单回报    ' + states.get(status, status))
            if order.get('native_message'):
                lines.append(order['native_message'])
            grid.detail_label.setText('\n'.join(lines))

    @staticmethod
    def current_trading_day(view):
        day = (view.get('account_metrics') or {}).get('trading_day')
        if day is not None:
            return day
        days = {item['trading_day'] for item in view['markets'] if item.get('fresh') and item.get('trading_day')}
        return next(iter(days)) if len(days)==1 else None

    @staticmethod
    def order_source_title(order):
        return {'STRATEGY': '策略', 'MANUAL': '手动'}.get(order.get('order_source'), '未知')

    def filter_orders(self, *_):
        if self.rendered_view is None:
            return
        chosen = self.order_contract.currentText()
        effect = {'开仓':'OPEN','平仓':'REDUCE'}.get(self.order_effect.currentText())
        side = self.order_side.currentText()
        source = self.order_source.currentText()
        view = self.rendered_view
        current = self.current_trading_day(view)
        def visible(item, day):
            return (day is None or current is None or item.get(day)==current) and (
                chosen == '全部品种' or item.get('contract_id') == chosen) and (
                effect is None or item.get('effect') == effect) and (side=='全部方向' or side==native_side(item))
        active_count = 0
        historical = []
        for index, item in enumerate(view['orders']):
            terminal = item.get('terminal_confirmed', item.get('broker_state') in {'FILLED', 'CANCELLED', 'REJECTED'})
            fills_consistent = item.get('reported_fill_qty', item.get('filled_qty')) == item.get('filled_qty')
            unresolved = (item.get('send_state') in {'UNKNOWN', 'SEND_POSSIBLE'} or item.get('cancel_status') == 'UNKNOWN'
                          or not fills_consistent)
            keep = visible(item, None) and (source == '全部来源' or source == self.order_source_title(item))
            if self.order_cycles:
                keep = item.get('position_cycle_id') in self.order_cycles
            if terminal and not unresolved and (not self.order_cycles or keep):
                historical.append(item)
            active = keep and (not terminal or unresolved)
            self.tables['orders'].setRowHidden(index, not active)
            active_count += active
        self.order_active_count = active_count
        self.fill_selectable(self.historical_orders, [order_cells(item) for item in historical],
                             [item.get('child_order_id') for item in historical])
        fill_count = 0
        for index, item in enumerate(view['trades']):
            keep = visible(item, 'trading_day')
            self.tables['trades'].setRowHidden(index, not keep)
            fill_count += keep
        self.order_count.setText(f'当前委托 {active_count} 笔   /   成交 {fill_count} 笔')
        self.render_order_detail()

    def manual_page(self, layout):
        account = group('连接', layout)
        self.manual_status = QLabel('等待账户同步')
        self.manual_close_price_mode = QComboBox()
        self.manual_close_price_mode.addItems(['对手价','排队价','最新价'])
        account.addLayout(row(self.manual_status,button('模拟连接设置',self.window.open_session),
                              QLabel('平仓价格'),self.manual_close_price_mode))
        self.manual_catalog_button = button('加载合约',self.load_manual_catalog)
        self.manual_catalog_status = QLabel('')
        account.addLayout(row(self.manual_catalog_button,self.manual_catalog_status))
        columns = QHBoxLayout()
        order = group('下单', columns, stretch=5)
        self.manual_code = QComboBox()
        self.manual_code.setEditable(True)
        self.manual_code.setInsertPolicy(QComboBox.NoInsert)
        self.manual_code.setCompleter(None)
        self.manual_scoped_codes = ()
        self.manual_code.lineEdit().setPlaceholderText('合约、品种名或月份')
        self.manual_code.lineEdit().textEdited.connect(self.manual_catalog_edited)
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
        self.update_manual_open_controls()
        self.refresh_manual_selection()

    def update_manual_open_controls(self):
        view = self.window.view
        config = view['configuration'] if view is not None else {}
        bound = self.manual_code.currentText() in (config.get('scoped_contracts') or self.names)
        ready = self.manual_controls_enabled and config.get('manual_workbench',False) and bound
        self.submit_button.setEnabled(ready)
        self.latest_button.setEnabled(ready)
        for control in self.quick_sizes:
            control.setEnabled(ready)
        if ready:
            message = '提交前确认合约、方向、手数和限价'
        elif config.get('manual_workbench') and not bound:
            message = '请选择当前人工范围内的合约'
        else:
            message = '人工开仓需要已同步的独立人工范围'
        self.manual_notice.setText(message)

    def load_manual_catalog(self):
        account = self.window.selected_account
        if self.quote_worker is not None:
            return
        if account is None:
            self.manual_catalog_status.setText('请先选择提供行情的模拟账号')
            return
        self.manual_catalog_status.setText('正在加载期货合约…')
        self.start_market_read(account['account_id'],catalog=True)

    def manual_catalog_edited(self, text):
        self.filter_manual_catalog(text)

    def filter_manual_catalog(self, text=None):
        codes = (self.rendered_view or {}).get('configuration',{}).get('scoped_contracts') or list(self.names)
        selected = self.manual_code.currentText() if text is None else text
        term = selected.strip().casefold() if text is not None else ''
        prefix = term.rstrip('0123456789')
        alias = ALIASES.get(prefix)
        matches = []
        for code in dict.fromkeys([*codes,*self.manual_catalog]):
            product = future_product(code)
            symbol = '.'.join(product) if product else ''
            name = PRODUCT_NAMES.get(symbol, '').casefold()
            suffix = code[len(symbol):] if product else ''
            if (not term or term in code.casefold() or term in name
                    or alias == product and product is not None and suffix.endswith(term[len(prefix):])):
                matches.append(code)
            if len(matches) >= 100:
                break
        self.manual_code.blockSignals(True)
        self.manual_code.clear()
        self.manual_code.addItems(matches)
        self.manual_code.setCurrentText(selected or (matches[0] if matches else ''))
        self.manual_code.blockSignals(False)
        self.render_manual()
        if text is not None and self.manual_catalog:
            self.manual_catalog_status.setText(f'显示 {len(matches)} 个匹配合约' if matches else '未找到匹配合约')

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
        self.update_manual_open_controls()
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
        self.log_event_filter.addItems(['全部事件', *LOG_EVENT_FILTERS, '警告'])
        self.log_event_filter.currentTextChanged.connect(self.update_logs)
        self.log_time_filter = QComboBox()
        self.log_time_filter.addItems(['全部时间', '最近 15 分钟', '最近 1 小时'])
        self.log_time_filter.currentIndexChanged.connect(self.update_logs)
        self.log_contract_filter = QComboBox()
        self.log_contract_filter.addItem('全部合约')
        self.log_contract_filter.currentTextChanged.connect(self.update_logs)
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText('搜索已加载日志')
        self.log_search.textChanged.connect(self.update_logs)
        self.log_count = QLabel('0 行')
        self.log_source_status = QLabel('日志来源未绑定')
        layout.addLayout(row(self.log_time_filter, self.log_contract_filter, self.log_filter,
                             self.log_event_filter, self.log_search, self.log_count))
        layout.addLayout(row(button('复制全部',self.copy_log),button('导出Excel',self.export_log),
                             button('清空',self.clear_log),button('导出日报',self.open_daily_report)))
        layout.addWidget(self.log_source_status)
        self.log_view_tabs = QTabWidget()
        self.log_view_tabs.setObjectName('workspaceSection')
        records = QSplitter(Qt.Horizontal)
        self.log_records = table(('时间', '级别', '合约', '事件'))
        self.log_records.setObjectName('dashboardGrid')
        self.log_records.setSelectionMode(QAbstractItemView.SingleSelection)
        for column in (0, 1, 2):
            self.log_records.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        records.addWidget(self.log_records)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        info = group('事件详情', detail_layout)
        info.parentWidget().setObjectName('workspaceCard')
        self.log_event_detail = QLabel(objectName='workspaceDetail')
        self.log_event_detail.setTextFormat(Qt.PlainText)
        self.log_event_detail.setWordWrap(True)
        self.log_event_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(self.log_event_detail)
        detail_layout.addStretch(1)
        records.addWidget(detail)
        records.setSizes([950, 440])
        self.log_records.currentCellChanged.connect(self.render_log_detail)
        self.log_view_tabs.addTab(records, '事件列表')
        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName('runtimeLog')
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view_tabs.addTab(self.log_text, '原始文本')
        layout.addWidget(self.log_view_tabs, 1)
        self.render_log_detail()

    def render_log_detail(self, *_):
        row = self.log_records.currentRow()
        if row < 0 or row >= self.log_records.rowCount():
            self.log_event_detail.setText('选择事件查看详情')
            return
        time_text, severity, contract, summary = [self.log_records.item(row, column).text() for column in range(4)]
        self.log_event_detail.setText(f'发生时间    {time_text}\n级别    {severity}\n合约    {contract}\n\n{summary}')

    def receipts_page(self, layout):
        layout.addLayout(row(self.buttons['receipt'], self.buttons['order_archive'], self.buttons['historical_orders']))
        layout.addWidget(self.tables['commands'], 1)

    def copy_log(self):
        self.update_logs()
        if self.visible_log_lines:
            QApplication.clipboard().setText('\n'.join(self.visible_log_lines))

    def export_log(self):
        self.update_logs()
        if not self.visible_log_lines:
            QMessageBox.information(self.window,'导出日志','当前筛选下没有日志可导出')
            return
        name,_ = QFileDialog.getSaveFileName(self.window,'导出运行日志','运行日志.xlsx','Excel (*.xlsx);;文本 (*.txt)')
        if not name:
            return
        self.update_logs()
        if not self.visible_log_lines:
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
            for path in self.log_paths(run):
                self.log_tail(path).clear(path)
        self.activity.clear()
        self.update_logs()

    def open_daily_report(self):
        self.window.open_reports()
        reports = self.window._reports_window
        if reports._worker is not None:
            return
        binding = None
        if self.paired is not None:
            binding = self.paired.packet/'ledger.db', self.window.paired_account_key
        elif self.window.store is not None and self.window.selected_account is not None:
            account = self.window.selected_account
            binding = self.window.store.ledger_path(account), account['account_key']
        if binding is not None:
            reports.bind_source(*binding)
        reports.request_daily_export(allow_offline=binding is not None)

    def latest_run(self):
        window = self.window
        selected = window.selected_account
        view = window.view
        if view is not None and selected is not None and view['account_key'] != selected['account_key']:
            return None
        candidates = []
        for launcher in (window._strategy_starter, window._session_dialog):
            if launcher is not None and launcher.run_directory is not None and (
                    (selected is not None and launcher.account_id == selected['account_id'])
                    or (self.paired is not None and launcher is window._session_dialog)):
                candidates.append(Path(launcher.run_directory).resolve())
        finished = getattr(window, '_finished_run', None)
        account = selected['account_key'] if selected else window.paired_account_key
        if (view is not None or finished is None or finished[0] != account
                or (candidates and candidates[0] != finished[2])):
            finished = None
        directory = getattr(window.store, 'directory', None)
        account_root = directory / 'desktop-starts' if selected and isinstance(directory, Path) else None
        paired_root = self.paired.packet / 'runs' if self.paired is not None else None
        identity = (view['account_key'], view['kernel_boot_id']) if view is not None else None
        if finished is not None:
            identity = finished[:2]
        binding = (identity, view['configuration'].get('run_directory') if view is not None else None,
                   selected['account_id'] if selected else None, tuple(candidates), finished)
        now = time.monotonic()
        same_binding = binding == self.log_binding
        archive = None
        if same_binding and self.log_directory is not None:
            archive = comparison_file(self.log_directory, identity) if identity is not None else None
            if archive == self.log_comparison and self.log_source_matches(self.log_directory, archive):
                return self.log_directory
        elif same_binding and now - self.log_resolution_at < 2.5:
            return self.log_directory
        directory = resolve_run(view, candidates, account_root, paired_root, finished=finished)
        archive = comparison_file(directory, identity) if directory is not None and identity is not None else None
        if (directory != self.log_directory or archive != self.log_comparison
                or (self.log_binding and self.log_binding[0] != identity)):
            self.log_tails.clear()
            self.last_reasons.clear()
        self.log_binding, self.log_directory = binding, directory
        self.log_comparison = archive
        self.log_resolution_at = now
        return directory

    def log_source_matches(self, run, archive):
        identity = self.log_binding[0]
        if identity is None:
            return run in self.log_binding[3]
        return matches_run_identity(run, identity) and comparison_file(run, identity) == archive

    def log_paths(self, run):
        paths = [executor_log_path(run), run/'startup-failure.json', *sorted(run.glob('phase-*.json'))]
        comparison = self.log_comparison
        paths.append(run / 'strategy-log.jsonl')
        paths.append(run.with_name(run.name + '.operation-log-recovery.json'))
        if comparison is not None:
            paths.append(comparison)
        return paths

    def log_tail(self, path):
        if path not in self.log_tails:
            document = path.suffix == '.json'
            limit = 65_536 if document else 360_000
            self.log_tails[path] = JsonLogTail(limit, document=document)
        return self.log_tails[path]

    def logged_strategy_reasons(self, run):
        result = {}
        archive = self.log_comparison
        if run is not None:
            path = run/'strategy-log.jsonl'
            for record in self.log_tail(path).read(path):
                if not self.strategy_log_row_matches(record):
                    continue
                kind = record.get('kind')
                code, day = record.get('contract_id'), record.get('trading_day')
                if all(isinstance(value, str) for value in (kind, code, day)) and record.get('message'):
                    result.setdefault((code,day), set()).add(kind)
            if not self.log_source_matches(run, archive):
                self.latest_run()
                return {}
        return result

    def strategy_log_row_matches(self, row):
        if not any(key in row for key in ('account_key', 'kernel_boot_id')):
            return True
        return (row.get('account_key'), row.get('kernel_boot_id')) == self.log_binding[0]

    def executor_log(self, run):
        phases = []
        run_identity = self.log_binding[0]
        for path in (executor_log_path(run), *sorted(run.glob('phase-*.json'))):
            for record in self.log_tail(path).read(path):
                phase = record.get('phase')
                if not isinstance(phase, str):
                    continue
                at, stamp = log_time(record.get('observed_at_utc'))
                if phase == 'DSV_OPERATION_LOG_DEGRADED':
                    if (record.get('account_key'), record.get('kernel_boot_id')) == run_identity:
                        phases.append(WorkbenchLogLine(f'[{at}] [日志] {operation_log_message(record)}', 'error', stamp))
                    continue
                message = record.get('message') or LOG_PHASES.get(phase, '策略状态已更新')
                if not isinstance(message, str):
                    message = LOG_PHASES.get(phase, '策略状态已更新')
                if phase == 'DSV_CLOCK_PROTECTION_BLOCKED':
                    protection = record.get('clock_protection')
                    blocked = protection.get('blocked_cycles') if isinstance(protection, dict) else None
                    if isinstance(blocked, dict):
                        reasons = {CLOCK_PROTECTION_REASONS.get(code, code) for code in blocked.values() if isinstance(code, str)}
                        if reasons:
                            message += ' · ' + '、'.join(sorted(reasons))
                error = record.get('error_code')
                if isinstance(error, str):
                    message += ' · ' + error
                exception = record.get('exception')
                if isinstance(exception, dict) and exception.get('exception_id'):
                    message += ' · ' + exception['exception_id']
                phases.append(WorkbenchLogLine(f'[{at}] [执行器] {message}', 'error' if error else None, stamp))
        failure = run/'startup-failure.json'
        for record in self.log_tail(failure).read(failure):
            if run_identity is not None and any(record.get(key) not in (None, expected)
                for key, expected in zip(('account_key', 'kernel_boot_id'), run_identity)):
                continue
            exception = record.get('exception')
            exception_id = exception.get('exception_id') if isinstance(exception, dict) else None
            suffix = f' · {exception_id}' if isinstance(exception_id, str) and exception_id else ''
            phases.append(WorkbenchLogLine(f"[执行器] {record.get('message','启动失败')} · {record.get('error_code','—')}{suffix}", 'error'))
        recovery_path = run.with_name(run.name + '.operation-log-recovery.json')
        for recovery in self.log_tail(recovery_path).read(recovery_path):
            if matches_operation_log_recovery(recovery, run, run_identity):
                at, stamp = log_time(recovery.get('observed_at_utc'))
                phases.append(WorkbenchLogLine(f'[{at}] [日志] {operation_log_message(recovery)}',
                                               None if recovery.get('status') == 'RECOVERED' else 'error', stamp))
        path = run/'strategy-log.jsonl'
        for record in self.log_tail(path).read(path):
            if not self.strategy_log_row_matches(record):
                continue
            stamp = record.get('native_time_utc') or record.get('observed_at_utc')
            tag, message = record.get('tag'), record.get('message')
            if not all(isinstance(value,str) and value for value in (stamp,tag,message)):
                continue
            at, moment = log_time(stamp)
            if moment is None:
                continue
            category = record.get('category')
            phases.append(WorkbenchLogLine(f'[{at}] [{tag}] {message}',
                                           category if isinstance(category, str) else LOG_TAGS.get(tag), moment))
        return phases

    def comparison_logs(self, run):
        path = self.log_comparison
        product, dsv = [], []
        if path is None:
            return product, dsv
        strategy_path = run/'strategy-log.jsonl'
        recorded = {row['event_seq'] for row in self.log_tail(strategy_path).read(strategy_path)
                    if isinstance(row.get('event_seq'), str) and self.strategy_log_row_matches(row)}
        expected_boot = self.log_binding[0][1]
        for record in self.log_tail(path).read(path):
            event = record.get('data',{})
            if not isinstance(event, dict):
                continue
            if record.get('kind')=='DSV_LOG':
                stamp = event.get('source_time_utc')
                at, moment = log_time(stamp)
                if moment is not None:
                    kind = event.get('kind')
                    message = event.get('message')
                    category = dsv_log_category(message) if isinstance(message, str) else None
                    if category is None:
                        category = event.get('operator_tag')
                    if not isinstance(category, str):
                        if isinstance(kind, str):
                            category = {'ENTRY_ORDER':'open','ENTRY_FILL':'open','EXIT_DECISION':'close'}.get(kind)
                    detail = message or f"{kind or ''} {event.get('reason','')}".rstrip()
                    dsv.append(WorkbenchLogLine(f"[{at}] [DSV] {event.get('contract_id','—')} · {detail}", category, moment))
            elif record.get('kind')=='PRODUCT_COMMITTED_EVENT':
                if event.get('kernel_boot_id', expected_boot) != expected_boot:
                    continue
                sequence = event.get('event_seq')
                if isinstance(sequence, str) and sequence in recorded:
                    continue
                label, category = {'ENTRY_PREPARED':('开仓意图','open'), 'EXIT_PREPARED':('平仓意图','close'),
                                   'FILL_CONFIRMED':('柜台成交',None), 'SEND_ABORTED':('发送取消','error'),
                                   'EXECUTION_CANCEL_TIMEOUT':('撤单待核对','error')}.get(event.get('event_type'), (None,None))
                if label is None:
                    continue
                payload = event.get('payload',{})
                if not isinstance(payload, dict):
                    continue
                data = payload.get('data',payload)
                if not isinstance(data, dict):
                    continue
                cycle = data.get('cycle')
                cycle = cycle if isinstance(cycle, dict) else {}
                plan = cycle.get('plan')
                plan = plan if isinstance(plan, dict) else {}
                fill = data.get('fill')
                child_id = None
                detail = ''
                if isinstance(fill, dict):
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
                at, moment = log_time(stamp)
                manual_children = (cycle.get('execution') or {}).get('manual_reduction_children',{})
                if category=='open' and cycle.get('entry_origin')=='MANUAL':
                    category = 'manual'
                elif category=='close' and (child_id in manual_children
                        or (cycle.get('nominal_exit') or {}).get('exit_reason')=='MANUAL_EXIT'):
                    category = 'manual'
                product.append(WorkbenchLogLine(f"[{at}] [执行器] {label} {plan.get('contract_id','—')} {detail}".rstrip(), category, moment))
        return product, dsv

    def update_logs(self, *_):
        if (self.tabs.currentWidget() is not self.diagnostic_sections.parentWidget()
                or self.diagnostic_sections.currentWidget() is not self.log_tab):
            return
        mode = self.log_filter.currentIndex()
        run = self.latest_run()
        archive = self.log_comparison
        lines = []
        product, dsv = self.comparison_logs(run) if run and mode in (0,1,3) else ([],[])
        if run and mode in (0,1):lines.extend(self.executor_log(run) + product)
        event_filter = self.log_event_filter.currentText()
        if mode in (0,3):
            if event_filter == '平仓':
                # The original archive reload and export use close/auto.
                dsv = [line for line in dsv if line.category in {'close', 'auto'}]
            lines.extend(dsv)
        if run and mode != 2 and not self.log_source_matches(run, archive):
            self.latest_run()
            run = None
            lines.clear()
        self.log_source_status.setVisible(run is None and mode != 2)
        if mode in (0,2):lines.extend(self.activity)
        lines.sort(key=lambda line: line.timestamp or datetime.min.replace(tzinfo=BEIJING))
        allowed = {'warning'} if event_filter == '警告' else LOG_EVENT_FILTERS.get(event_filter)
        query = self.log_search.text().strip().casefold()
        view = self.window.view or {}
        codes = sorted(set(self.names) | {item['contract_id'] for item in view.get('markets', ())
                                        } | {item['contract_id'] for item in view.get('cycles', ())})
        options = ['全部合约', *codes]
        if [self.log_contract_filter.itemText(index) for index in range(self.log_contract_filter.count())] != options:
            selected = self.log_contract_filter.currentText()
            blocked = self.log_contract_filter.blockSignals(True)
            self.log_contract_filter.clear()
            self.log_contract_filter.addItems(options)
            if selected in options:
                self.log_contract_filter.setCurrentText(selected)
            self.log_contract_filter.blockSignals(blocked)
        code_filter = self.log_contract_filter.currentText()
        minutes = (None, 15, 60)[self.log_time_filter.currentIndex()]
        since = datetime.now(BEIJING) - timedelta(minutes=minutes) if minutes else None
        visible = [line for line in lines if (allowed is None or line.category in allowed)
                   and (since is None or line.timestamp is not None and line.timestamp >= since)
                   and (code_filter == '全部合约' or code_filter in line.text)
                   and query in line.text.casefold()]
        self.visible_log_lines = [line.text for line in visible]
        recent = visible[-200:][::-1]
        keys = [(line.text, line.timestamp) for line in recent]
        if keys != getattr(self, 'log_record_keys', None):
            self.log_record_keys = keys
            rows = []
            for line in recent:
                message = line.text.split('] ', 2)[-1]
                code = next((code for code in codes if code in message), '—')
                severity = {'error': '异常', 'warning': '警告'}.get(line.category, '信息')
                rows.append((line.timestamp.strftime('%H:%M:%S') if line.timestamp else '—', severity, code, message))
            self.fill_selectable(self.log_records, rows, keys)
            self.render_log_detail()
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
        transfers = group('银期转账时间表（以期货公司 App 为准 · 占位）', layout)
        bank_times = table(['银行','日盘入金','日盘出金','夜盘入金','夜盘出金'])
        populate(bank_times, [('银行','日盘入金','日盘出金','夜盘入金','夜盘出金')])
        bank_times.setMaximumHeight(77)
        transfers.addWidget(bank_times)

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
        self.offset_preview.setWordWrap(True)
        self.offset_market_button = button('一键计算（联网）', self.calculate_market_offsets)
        layout.addLayout(row(self.offset_market_button, self.offset_preview))
        def applied():
            candidate = calculator.result_profile
            dialog = self.parameter_dialog()
            if candidate is not None and dialog.apply_edited_preview(candidate, expected_hash=calculator.base_hash):
                self.offset_preview.setText('所选偏移已应用到主控预览，可保存参数。')
            else:
                calculator.error_label.setText('参数预览已变化，请重新计算后应用。')
            calculator.show()
        calculator.accepted.connect(applied)
        layout.addWidget(calculator,1)
        self.calculator = calculator

    def calculate_market_offsets(self):
        if self.quote_worker is not None:
            return
        account = self.window.selected_account
        if self.window.store is None or account is None:
            self.offset_preview.setText('请先在「账户与设置」选用有行情凭据的账号。')
            return
        requested = self.calculator.calculation_input()
        if not requested[0].to_record()['settings']['symbols']:
            self.offset_preview.setText('请先在策略参数里添加合约。')
            return
        self.offset_requested_input = requested
        self.offset_preview.setText('正在读取原生日线和当前振幅…')
        self.start_market_read(account['account_id'], offset_query=requested[:3])

    def system_page(self, layout):
        sound_settings = getattr(self.window,'create_sound_settings',None)
        if sound_settings is not None:
            layout.addWidget(sound_settings())
        appearance = group('界面偏好', layout)
        appearance.addWidget(self.window._tray.preference_checkbox())
        self.font_size = QSpinBox()
        self.font_size.setRange(8, 13)
        self.font_size.setValue(self.settings.value('font_size', 12, int))
        appearance.addLayout(row(QLabel('字号'), self.font_size, button('按屏幕自适应', self.fit_current_screen)))
        appearance.addWidget(button('授权状态', self.show_license_status))
        for title,key,panel in (('显示 K 线图','show_candles',self.candle_panel),
                                ('显示权益曲线','show_equity',self.equity_panel)):
            chart = QCheckBox(title)
            chart.setChecked(self.settings.value(key,self.settings.value('show_charts',True,bool),bool))
            panel.setVisible(chart.isChecked())
            chart.toggled.connect(panel.setVisible)
            chart.toggled.connect(lambda visible,k=key:self.settings.setValue(k,visible))
            appearance.addWidget(chart)
        layout.addStretch()

    def fit_current_screen(self):
        window = self.window
        screen = window.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        if window.isMaximized() or window.isFullScreen():
            window.showNormal()
        window.layout().activate()
        # Fit the native frame inside the current monitor's work area, including its taskbar.
        frame_extra = window.frameGeometry().size() - window.geometry().size()
        usable = available.size() - frame_extra
        minimum = window.minimumSizeHint().expandedTo(window.minimumSize())
        while (minimum.width() > usable.width() or minimum.height() > usable.height()) and self.font_size.value() > self.font_size.minimum():
            self.font_size.setValue(self.font_size.value() - 1)
            window.layout().activate()
            minimum = window.minimumSizeHint().expandedTo(window.minimumSize())
        size = window.size().expandedTo(minimum).boundedTo(usable)
        window.resize(size)
        frame = window.frameGeometry()
        x = max(available.left(), min(frame.left(), available.right() - frame.width() + 1))
        y = max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1))
        window.move(x, y)
        window._tray.save_geometry()

    def show_license_status(self):
        from online_trader.desktop.entrypoint import license_status_text
        QMessageBox.information(self.window, '授权状态', license_status_text())

    def add_activity(self, message, *, category=None):
        moment = datetime.now(BEIJING)
        self.activity.append(WorkbenchLogLine(f'[{moment:%H:%M:%S}] [工作台] {message}', category, moment))

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

    def equity_anchor(self):
        view = self.rendered_view or {}
        source = view.get('configuration', {}).get('equity_base')
        daily = [source] if source is not None else sorted(view.get('equity_history', ()), key=lambda row: row['trading_day'])
        for row in daily:
            balance = row.get('balance')
            if balance is None or Decimal(balance) <= 0:
                continue
            previous = row.get('pre_balance')
            return Decimal(previous) if previous is not None and Decimal(previous) > 0 else Decimal(balance)
        return next((Decimal(row['balance']) for row in self.balance_history if row['balance'] is not None), None)

    def render_equity_chart(self, *_):
        daily,days = self.equity_rows()
        current = self.current_trading_day(self.rendered_view) if self.rendered_view is not None else None
        intraday = [item for item in self.balance_history if item['trading_day']==current] if current in days else []
        has_current_samples = any(item['balance'] is not None for item in intraday)
        rows = [item for item in daily if not has_current_samples or item['trading_day']!=current]
        if has_current_samples:
            rows.extend(intraday)
        if self.equity_mode.currentText()=='收益曲线':
            anchor = self.equity_anchor()
            if anchor is not None:
                rows = [item|{'balance':str(Decimal(item['balance'])-anchor) if item.get('balance') is not None else None}
                        for item in rows]
            self.equity.heading = '权益变化（相对起始权益）'
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
        writer.writerow(('日期','最近记录权益','当日盈亏','累计盈亏','区间收益率','记录时间（北京时间）','来源'))
        anchor = self.equity_anchor()
        selected_start = Decimal(daily[0]['balance'])
        for row in daily:
            equity = Decimal(row['balance'])
            previous = row.get('pre_balance')
            today = equity-Decimal(previous) if previous is not None and Decimal(previous)>0 else ''
            writer.writerow((row['trading_day'],equity,today,equity-anchor if anchor is not None else '',
                             f'{equity/selected_start-1:.2%}' if selected_start else '',
                             beijing(row['observed_at_utc']).isoformat(),'柜台记录'))
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
        self.fund_values = values
        for key, (metric,title) in self.fund_metrics.items():
            value = values.get(key)
            metric.setText(f'{title}   {value if value is not None else "—"}')

    def render_strategy_monitor(self, monitor):
        delay = monitor.get('quote_delay_seconds') if monitor else None
        if delay is None and monitor:
            status = monitor['quote_status']
            value = '—（休市）' if status == '休市' else '—（等待延迟样本）'
        else:
            value = display_quote_delay(delay) if monitor else '—'
        self.home_quote_delay.setText(f'整体行情延迟（抽样）   {value}')
        self.home_quote_delay.setToolTip('最近一笔有效行情的抽样延迟。各合约的新鲜度见下方合约行情。')
        if not monitor:
            return
        armed = (self.rendered_view or {}).get('entry_authority') == 'ARMED'
        groups = {'ACTIVE': [], 'STOPPED': [], 'PENDING': []}
        for symbol in monitor['symbols']:
            state = symbol['state']
            code = symbol['contract_id']
            status = symbol.get('status') or '等待行情'
            position = symbol.get('position_quantity', 0)
            if state == 'ACTIVE' and status == '可开仓' and not armed:
                status = '已停止新开仓'
            if state == 'ACTIVE' and position:
                status = '📌持仓中' if status == '持仓中' else '📌持仓中 · ' + status
            marker = {'ACTIVE':'●','STOPPED':'⛔','PENDING':'⏳'}[state]
            parts = [f"{marker} {symbol['name']} ({code})  {status}"]
            net = symbol.get('net_loss_points')
            if net is not None:
                points = Decimal(net)
                parts.append(f'{"净亏" if points >= 0 else "净盈"}{abs(points):.0f}点')
            if state == 'ACTIVE':
                parts.append(f'报单{symbol["opening_orders"]}次')
                if symbol.get('main_contract'):
                    parts.append(f'主力:{symbol["main_contract"]}')
                if symbol.get('real_margin_rate') is not None:
                    parts.append(f'实时保证金率 {Decimal(symbol["real_margin_rate"]):.2%}')
            elif state == 'PENDING' and position:
                parts.append(f'剩余 {position} 手')
            groups[state].append('  '.join(parts))
        for label, lines in zip(self.status_labels, groups.values()):
            label.setText('\n'.join(lines) or '（无）')

    def render(self, view):
        self.rendered_view = view
        self.render_session_hours(view)
        orders = self.tables['orders']
        populate(orders, [order_cells(item) for item in view['orders']])
        for index, item in enumerate(view['orders']):
            orders.item(index, 0).setData(Qt.UserRole, item.get('child_order_id'))
        populate(self.tables['trades'],[trade_cells(item) for item in view['trades']])
        codes = ['全部品种', *dict.fromkeys([*self.names, *(item['contract_id'] for item in view['orders']),
                                          *(item['contract_id'] for item in view['markets'])])]
        if [self.order_contract.itemText(index) for index in range(self.order_contract.count())] != codes:
            selected = self.order_contract.currentText()
            blocked = self.order_contract.blockSignals(True)
            self.order_contract.clear()
            self.order_contract.addItems(codes)
            if selected in codes:
                self.order_contract.setCurrentText(selected)
            self.order_contract.blockSignals(blocked)
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
        if self.manual_scoped_codes != tuple(codes):
            self.manual_scoped_codes = tuple(codes)
            self.filter_manual_catalog()
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
            self.candles.native_series.clear()
            self.last_reasons.clear()
        account = getattr(self.window,'_last_account_record',{})
        self.render_funds(account,view,markets)
        self.home_updated_at = datetime.now(BEIJING).strftime('%H:%M:%S')
        self.render_home()
        self.record_balance(account,view)
        self.render_equity_chart()
        self.candles.show_native_bars(view.get('minute_bars',{}) | view['configuration'].get('native_minute_bars',{}))
        self.select_dashboard_market()
        run = self.latest_run()
        strategy_records = self.logged_strategy_reasons(run)
        waiting, blocked, pending = [], [], []
        quote_states = {}
        for code in codes:
            market = markets.get(code,{})
            name = f'{self.names.get(code,code)} ({code})'
            reasons = market.get('entry_reasons',[])
            quote_states[code] = ('新鲜' if market.get('fresh') else
                                  '行情已滞后' if market.get('source_time_utc') else '等待行情')
            covered = strategy_records.get((code,market.get('trading_day')), set())
            activity_reasons = [reason for reason in reasons
                                if reason not in covered and reason not in QUIET_ENTRY_REASONS]
            if activity_reasons != self.last_reasons.get(code):
                self.last_reasons[code] = activity_reasons
                if activity_reasons:
                    messages = ENTRY_MESSAGES | ACTIVATION_MESSAGES | {
                        'NET_LOSS_LIMIT':'净亏已达上限，暂停开新仓',
                        'ACCOUNT_VALUES_UNKNOWN':'柜台账户字段待同步',
                        'ENTRY_AUTHORITY_DISARMED':'策略未启用开仓',
                    }
                    self.add_activity(f'{code} · {" / ".join(messages.get(reason,reason) for reason in activity_reasons)}')
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
        failed_exits = {block['position_cycle_id'] for block in view.get('blocks',())
                        if block.get('active') and block.get('reason_code')=='EXIT_REDUCTION_TERMINAL_UNFILLED'}
        for cycle in view['cycles']:
            target = cycle.get('target_remaining_quantity')
            if cycle.get('exit_pending',target is not None and target<cycle['remaining_quantity']):
                status = ' · 委托已终态，剩余目标待核对' if cycle['position_cycle_id'] in failed_exits else ''
                pending.append(f"● {cycle['contract_id']}  剩余 {cycle['remaining_quantity']} 手{status}")
        for label, lines in zip(self.status_labels,(waiting,blocked,pending)):
            label.setText('\n'.join(lines) or '（无）')
        monitor = view['configuration'].get('strategy_monitor')
        fresh = sum(status == '新鲜' for status in quote_states.values())
        lagging = [code for code, status in quote_states.items() if status == '行情已滞后']
        if monitor and monitor['quote_status'] == '休市' and not fresh:
            self.quote_status.setText('合约行情 · 休市')
        else:
            details = [f'{fresh} / {len(codes)} 个合约有新鲜报价']
            if lagging:
                details.append(f'滞后 ({len(lagging)})：{"、".join(lagging)}')
            waiting_count = len(codes) - fresh - len(lagging)
            if waiting_count:
                details.append(f'{waiting_count} 个合约等待行情')
            self.quote_status.setText('合约行情 · ' + ' · '.join(details))
        self.render_strategy_monitor(monitor)
        populate(self.instrument_grid,[[self.names.get(code,code),code,markets.get(code,{}).get('trading_day','—'),
             display_value(markets.get(code,{}).get('last')),quote_states[code]] for code in codes])
        rule_markets = self.quote_reference | {code:market for code,market in markets.items() if market.get('fresh') or code not in self.quote_reference}
        self.instrument_rules.set_markets({code:self.names.get(code,code) for code in codes}, rule_markets)
        self.render_manual_tables(view)
        self.render_manual()
        state = (view['kernel_boot_id'],view['entry_authority'],view['connection']['fault'],view['connection']['synchronized'])
        if state != self.last_state:
            self.last_state = state
            self.add_activity(self.window.connection_detail.text())
        self.render_dashboard_events(view)
        self.update_logs()
