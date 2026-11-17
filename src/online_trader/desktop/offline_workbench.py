"""Windows workbench for the offline owner; all effects go through its pipe."""

from uuid import uuid4

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QHeaderView, QLabel, QMainWindow, QSpinBox, QTabWidget, QVBoxLayout, QWidget

from online_trader.desktop.workbench_layout import button, populate, row, table
from online_trader.dsv_runtime.control_endpoint import PIPE_CONNECTION_ERRORS
from online_trader.dsv_runtime.offline_endpoint import OfflineClient
from online_trader.errors import TraderError


_REJECTION_REASONS = {
    'QUOTE_OR_PRICE_SCOPE': '缺少有效报价、存在外部委托或限价不合规',
    'ACCOUNT_SCOPE_OR_WINDOW': '合约、交易日或开仓时段超出账户授权',
    'PRE_SEND_VALIDATION': '发送前校验失败',
}

_STRATEGY_REASONS = {
    'NORMAL_ENTRY': '普通开仓信号成立',
    'CONTINUATION_ENTRY': '延续开仓信号成立',
    'AWAITING_NATIVE_SYNC': '等待柜台委托、成交及仓位核对',
    'LATE_FILL_OWNERSHIP_REVIEW': '迟到成交待核对归属',
    'UNRESOLVED_PRIOR_CHILD': '原委托或成交尚未核齐',
    'USER_STOPPED': '用户已停止新增开仓',
    'UNOWNED_POSITION': '存在外部持仓或委托',
    'QUOTE_UNAVAILABLE': '缺少有效行情',
    'MINUTE_BAR_UNAVAILABLE': '缺少分钟 K 线',
    'POSITION_OR_ENTRY_PENDING': '已有策略持仓或开仓委托待确认',
    'POSITION_WAITING_FOR_MARKET': '已有持仓等待有效行情或交易时段',
    'DAY_STOP': '当日停开',
    'MONTH_STOP': '当月停开',
    'DAILY_TARGET_LIMIT': '达到当日开仓目标次数上限',
    'AUCTION_DISABLED': '配置禁止集合竞价开仓',
    'OUTSIDE_SESSION': '当前不在交易时段',
    'OPENING_WAIT': '仍在开盘等待窗口',
    'CLOSE_ENTRY_WINDOW': '临近收盘，停止新增开仓',
    'QUOTE_AGE': '行情已超过开仓时效',
    'OFFSETS_UNAVAILABLE': '开仓、止损或止盈偏移无效',
    'GEOMETRY_BLOCK': '当前日内振幅不满足开仓条件',
    'BAR_CONSUMED': '当前分钟的开仓机会已使用',
    'NO_CHASE_AFTER_ACT': '达到激活幅度后禁止追价',
    'HYSTERESIS_NOT_READY': '价格尚未满足触发前的回撤条件',
    'TRIGGER_NOT_REACHED': '行情尚未达到开仓触发价',
    'ACTIVE_SYMBOL_LIMIT': '达到同时持仓品种数上限',
    'STOP_COOLDOWN': '仍在止损冷却期',
    'STOP_TRIGGER_UNCHANGED': '止损后开仓触发价尚未变化',
    'CHASE_CAP_EXCEEDED': '报单价超出追价上限',
    'TAKE_PROFIT_OUTSIDE_LIMIT': '理论止盈价超出涨跌停范围',
    'QUOTE_NEAR_LIMIT': '报价接近涨跌停',
    'ENTRY_THROTTLED': '同向开仓触发间隔过短',
    'FUNDS_UNAVAILABLE': '资金或保证金字段不足，无法计算手数',
    'INSUFFICIENT_MARGIN': '可用保证金不足',
    'ORDER_QUANTITY_UNSUPPORTED': '手数不符合合约委托要求',
    'SCOPE_OPENING_WINDOW_CLOSED': '超出账户开仓授权时段',
    'SCOPE_TARGET_LIMIT': '达到授权开仓目标次数上限',
    'SCOPE_RISK_UNPROVED': '缺少授权风控所需数据',
    'SCOPE_QUANTITY_LIMIT': '授权剩余手数低于合约最小手数',
    'ADVERSE_SLIPPAGE': '开仓滑点过大，触发保护退出',
    'DATA_GAP_EXIT': '行情断流，触发保护退出',
    'DATA_GAP_EXIT_PENDING': '断流保护等待行情或成交',
    'ENTRY_CONFIRM_TIMEOUT': '开仓确认超时',
    'STOP_LOSS': '触发止损',
    'TAKE_PROFIT': '触发止盈',
    'TRAILING_STOP': '触发移动止盈',
    'PROTECTION_ACTIVATED': '保护线已激活',
    'PARTIAL_STAGE_1': '触发第一档减仓',
    'PARTIAL_STAGE_2': '触发第二档减仓',
    'SCHEDULED_CLOSE': '到达收盘平仓窗口',
    'SCHEDULED_CLOSE_PENDING': '收盘平仓待完成',
    'PROTECTION_EXIT_AWAITING_SYNC': '保护退出等待策略同步',
    'PROTECTION_EXIT_SYNCHRONIZED': '保护退出已完成策略同步',
    'EXIT_WAITING_FOR_PRICE': '退出已登记，等待有效报价',
    'EXIT_WAITING_FOR_SESSION': '退出已登记，等待交易时段',
    'EXIT_WAITING_FOR_CLOCK_REVIEW': '退出已登记，等待时钟核对',
    'FIRST_MINUTE_JUMP_DAY_STOP': '首分钟跳空触发当日停开',
}

_QUOTE_REASONS = {
    'QUOTE_UNAVAILABLE': '尚无有效报价',
    'QUOTE_TIMESTAMP_INVALID': '源时间格式无效',
    'QUOTE_SESSION_UNKNOWN': '报价的交易时段无法确认',
    'QUOTE_OUTSIDE_CURRENT_SESSION': '当前休市',
    'QUOTE_SESSION_MISMATCH': '报价属于其他交易时段',
    'QUOTE_TIME_REGRESSION': '报价源时间发生倒退',
    'QUOTE_LEADS_LOCAL_UTC': '报价领先本机时间',
    'QUOTE_REFERENCE_CONFLICT': '报价时间与独立参考冲突',
    'CALENDAR_COVERAGE_UNKNOWN': '当前时段无完整日历',
    'CABINET_DAY_UNPROVED': '柜台交易日待核对',
}


class _Request(QThread):
    completed = Signal(object, object)

    def __init__(self, client, operation, body, parent):
        super().__init__(parent)
        self.client, self.operation, self.body = client, operation, body

    def run(self):
        try:
            result = self.client.request(self.operation, self.body, timeout=0.65)
        except (OSError, TraderError) as error:
            self.completed.emit(None, error)
        else:
            self.completed.emit(result, None)


class OfflineWorkbench(QMainWindow):
    def __init__(self, pipe, *, client=None):
        super().__init__()
        self.client = client if client is not None else OfflineClient(pipe)
        self.request = None
        self.pending = None
        self._state = None
        self._transport_ok = False
        self._confirmed_absent = False
        self.setWindowTitle('OnlineTrader · DSV 离线工作台')
        self.resize(1120, 760)

        panel = QWidget()
        self.setCentralWidget(panel)
        layout = QVBoxLayout(panel)
        self.status = QLabel('连接离线执行器…', objectName='offlineStatus')
        self.account = QLabel('—', objectName='offlineAccount')
        layout.addWidget(self.status)
        layout.addWidget(self.account)
        self.start = button('启动策略 (F5)', lambda: self._submit('START'))
        self.stop = button('停止策略 (F6)', lambda: self._submit('STOP'))
        self.recover_clock = button('恢复时钟核对', self._recover_clock)
        self.recover_clock.setObjectName('offlineRecoverClock')
        self.recover_clock.hide()
        self.retry = button('查询原回执', self._query_receipt)
        self.refresh_button = button('刷新', self.refresh)
        layout.addLayout(row(self.start, self.stop, self.recover_clock, self.refresh_button, self.retry))

        self.quantity = QSpinBox(objectName='offlineQuantity')
        self.quantity.setRange(1, 2_147_483_647)
        self.reduce = button('手工减仓', self._reduce_selected)
        self.cancel = button('撤选中委托', self._cancel_selected)
        self.positions = table(('合约', '持仓方向', '自有持仓', '今仓', '昨仓', '剩余目标'))
        self.orders = table(('合约', '买卖', '开平', '总量', '已成', '状态', '委托身份'))
        for control, name in ((self.start, 'offlineStart'), (self.stop, 'offlineStop'), (self.retry, 'offlineRetry'),
                              (self.reduce, 'offlineReduce'), (self.cancel, 'offlineCancel'),
                              (self.positions, 'offlinePositions'), (self.orders, 'offlineOrders')):
            control.setObjectName(name)
        layout.addWidget(QLabel('策略自有持仓'))
        layout.addWidget(self.positions, 2)
        layout.addLayout(row(QLabel('减仓手数'), self.quantity, self.reduce))
        layout.addWidget(QLabel('未核齐委托'))
        layout.addWidget(self.orders, 3)
        layout.addLayout(row(self.cancel))

        self.activity = table(('时间', '合约', '事件', '原因或计价依据', '委托身份'))
        self.activity.setObjectName('offlineActivity')
        activity_header = self.activity.horizontalHeader()
        activity_header.setSectionResizeMode(QHeaderView.Interactive)
        activity_header.resizeSection(0, 210)
        activity_header.resizeSection(2, 130)
        activity_header.resizeSection(4, 180)
        activity_header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.fills = table(('成交日', '合约', '买卖', '开平', '成交量', '成交价', '成交编号', '原生委托号'))
        self.fills.setObjectName('offlineFills')
        evidence = QTabWidget(objectName='offlineEvidence')
        evidence.addTab(self.activity, '策略事件')
        evidence.addTab(self.fills, '逐笔成交')
        self.clock_status = QLabel(objectName='offlineClockStatus')
        self.quote_health = table(('合约', '源报价时间', '源年龄', '观测静默', '行情状态', '退出等待'))
        self.quote_health.setObjectName('offlineQuoteHealth')
        clock_panel = QWidget()
        clock_layout = QVBoxLayout(clock_panel)
        clock_layout.addWidget(self.clock_status)
        clock_layout.addWidget(self.quote_health)
        self.evidence = evidence
        self.clock_tab = evidence.addTab(clock_panel, '行情与时钟')
        evidence.setTabVisible(self.clock_tab, False)
        layout.addWidget(evidence, 3)
        self.history_status = QLabel(objectName='offlineHistoryStatus')
        self.history_status.hide()
        layout.addWidget(self.history_status)
        self.receipt = QLabel('—', objectName='offlineReceipt')
        self.receipt.setWordWrap(True)
        layout.addWidget(self.receipt)

        for shortcut, control in (('F5', self.start), (('F6', self.stop))):
            QShortcut(QKeySequence(shortcut), self).activated.connect(control.click)
        self.positions.itemSelectionChanged.connect(self._update_controls)
        self.orders.itemSelectionChanged.connect(self._update_controls)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(650)
        self._update_controls()
        self.refresh()

    def _job(self, op, body=None):
        if self.request is not None:
            return
        self.request = _Request(self.client, op, body, self)
        self.request.completed.connect(self._complete)
        self.request.finished.connect(self._finished)
        self.request.start()
        self._update_controls()

    def refresh(self):
        if self.pending:
            self._job('receipt', {'id': self.pending['id'], 'expected_scope': self.pending['expected_scope']})
        else:
            self._job('snapshot', {'workbench_evidence': True})

    def _query_receipt(self):
        if self.pending:
            if self._confirmed_absent and self._transport_ok:
                self._job('command', self.pending)
            else:
                self._job('receipt', {'id': self.pending['id'], 'expected_scope': self.pending['expected_scope']})

    def _recover_clock(self):
        if self._state and (recovery_id := self._state.get('clock_recovery_id')):
            self._submit('CLOCK_RECOVER', clock_recovery_id=recovery_id)

    def _submit(self, action, **data):
        if self.request is None and self.pending is None and self._state is not None:
            self.pending = {'id': 'desktop-' + uuid4().hex, 'action': action,
                            'expected_scope': self._state['scope'], 'expected_day': self._state['trading_day'], **data}
            self._confirmed_absent = False
            self._job('command', self.pending)

    @staticmethod
    def _selected(grid, column):
        item = grid.item(grid.currentRow(), column)
        return item.data(Qt.UserRole) if item is not None else None

    def _reduce_selected(self):
        selection = self._selected(self.positions, 0)
        if isinstance(selection, tuple):
            contract, position = selection
            self._submit('REDUCE_DETACHED', contract_id=contract, position_id=position,
                         quantity=self.quantity.value())
        elif selection is not None:
            self._submit('REDUCE', contract_id=selection, quantity=self.quantity.value())

    def _cancel_selected(self):
        child = self._selected(self.orders, 0)
        if child is not None:
            self._submit('CANCEL', child_order_id=child)

    def _complete(self, result, error):
        operation = self.request.operation
        if error is not None:
            code = getattr(error, 'code', 'E_PIPE')
            if code in PIPE_CONNECTION_ERRORS:
                self._transport_ok = False
                self._confirmed_absent = False
                self.retry.setText('查询原回执')
                self.status.setText('离线执行器连接中断')
                if self.pending:
                    self.receipt.setText(f'等待原命令回执 {self.pending["id"]}')
            else:
                self.receipt.setText(f'{code} · {error}')
                if operation == 'command' or code == 'E_DSV_SCOPE':
                    self.pending = None
                    self._confirmed_absent = False
                    self.retry.setText('查询原回执')
            return
        self._transport_ok = True
        if operation == 'snapshot':
            self._show_state(result)
        elif result is None:
            self._confirmed_absent = True
            self.receipt.setText(f'原命令尚无回执 {self.pending["id"]}，可重试同一命令')
            self.retry.setText('重试原命令')
        else:
            reason = result['decisions'][0]['reason']
            messages = {'START': '已请求启动', 'STOP': '已停止新增', 'USER_REDUCE': '已登记减仓目标',
                        'CLOCK_RECOVERED': '时钟已核对，策略仍停止新增',
                        'USER_REDUCE_DETACHED': '已登记原周期减仓目标',
                        'USER_CANCEL': '已请求撤单', 'CANCEL_UNAVAILABLE': '该委托当前不可撤'}
            self.receipt.setText(f'{messages.get(reason, reason)} · {result["input_id"]}')
            self.pending = None
            self._confirmed_absent = False
            self.retry.setText('查询原回执')

    def _finished(self):
        operation = self.request.operation
        self.request.deleteLater()
        self.request = None
        self._update_controls()
        if operation == 'command' and self.pending is None:
            self.refresh()

    def _show_state(self, state):
        self._state = state
        if not state['synchronized']:
            status = '等待行情与订单核对'
        elif state.get('clock_recovery_required'):
            status = '时钟或长间隔待核对 · 已停止新增'
        elif any(row.get('native_conflicts') for row in state['symbols'].values()):
            status = '迟到平仓成交待核对原周期'
        elif any(row.get('detached_positions') for row in state['symbols'].values()):
            status = '迟到成交待核对归属'
        elif any((row.get('position') or {}).get('gap_exit') for row in state['symbols'].values()):
            status = '断流退出待行情或成交'
        else:
            status = '策略运行中' if state['running'] else '已停止新增'
        self.status.setText(status)
        mode = '替代执行链 · SDK 模拟' if state.get('sdk_connection') else '离线回放'
        if entry_point := (state.get('runtime_identity') or {}).get('entry_point'):
            mode += f' · {entry_point}'
        self.setWindowTitle(f'OnlineTrader · DSV {"SDK 模拟工作台" if state.get("sdk_connection") else "离线工作台"}')
        self.account.setText(f"{state['scope']['account_key']} · {state['trading_day']} · {mode}")
        positions = [(contract, data['position']) for contract, data in state['symbols'].items()
                     if data['position'] and data['position']['owned']]
        entries = [(contract, (contract, '多' if position['direction'] == 'LONG' else '空',
                         position['owned'], position.get('today', '—'), position.get('historical', '—'),
                         '—' if position['exit_target'] is None else position['exit_target']))
                   for contract, position in positions]
        for contract, data in state['symbols'].items():
            for position in data.get('detached_cycles', []):
                identity = position['position_id']
                target = position['reviewed_exit_target']
                entries.append(((contract, identity), (f'{contract} · {identity}', '多' if position['direction'] == 'LONG' else '空',
                                position['owned'], position['today'], position['historical'],
                                '待核对' if target is None else target)))
        self._populate(self.positions, entries)
        self._populate(self.orders, [(key, (order['contract_id'], order['direction'], order['native_offset'],
                         order['original_qty'], order['filled_qty'], order['cancel_status'] or
                         (order['broker_state'] if order['native_id'] else order['send_state']),
                         order['native_id'] or key)) for key, order in state['orders'].items()])
        populate(self.activity, [self._activity_row(event) for event in state.get('activity', ())])
        populate(self.fills, [
            (fill['fill_day'], fill.get('contract_id', '—'), fill.get('direction', '—'),
             fill.get('native_offset', '—'), fill['quantity'], fill['price'], fill['native_trade_id'],
             fill['native_order_id']) for fill in state.get('fills', ())])
        message = {'LOADING': '正在读取策略事件与逐笔成交…',
                   'UNAVAILABLE': '历史记录暂不可用，稍后自动重试',
                   'DEGRADED': '部分历史记录不完整；已继续读取后续记录，请查看诊断事件'}.get(state.get('history_status'), '')
        self.history_status.setText(message)
        self.history_status.setVisible(bool(message))
        self._show_clock(state)

    def _show_clock(self, state):
        paired = state.get('time_semantics_version') is not None
        self.evidence.setTabVisible(self.clock_tab, paired)
        if not paired:
            return
        health = state.get('clock_health') or {}
        reference = {'OBSERVED_OK': '已观测', 'REFERENCE_UNAVAILABLE': '暂不可用'}.get(
            health.get('reference_status'), '尚未观测')
        activity = state.get('sdk_activity')
        self.clock_status.setText(f"独立参考 {reference} · SDK 更新 {activity if activity is not None else '—'} 次" +
                                  (' · 需核对时钟与柜台' if state.get('clock_recovery_required') else ''))
        rows = []
        for code, row_state in state['symbols'].items():
            quote = row_state.get('quote_observation') or {}
            age, silence = (quote.get(name) for name in ('source_age_raw_ns', 'silence_ns'))
            time_reason = row_state.get('calendar_reason') or quote.get('reason')
            reason = _QUOTE_REASONS.get(time_reason, '已观测' if quote else '未知')
            wait = _STRATEGY_REASONS.get(row_state.get('exit_wait_reason'), '—')
            if row_state.get('timer_recovery_uncertain'):
                wait = '计时起点待核对' if wait == '—' else wait + ' · 计时起点待核对'
            rows.append((code, quote.get('source_time_raw') or '—',
                         '—' if age is None else f'{int(age)/1_000_000:+.3f} ms',
                         '—' if silence is None else f'{int(silence)/1_000_000:.3f} ms', reason, wait))
        populate(self.quote_health, rows)

    @staticmethod
    def _activity_row(event):
        reason = event['reason']
        if reason == 'EXPIRY_ENTRY_BLOCK':
            name = '到期阻断开仓'
            detail = (f"剩余 {event['days_to_expiry']} 天，达到或低于阈值 {event['min_days_to_expiry']} 天"
                      if event.get('days_to_expiry') is not None else '达到或低于到期开仓阈值')
        elif reason == 'NO_CHASE_AFTER_ACT' and event.get('activation_price') is not None:
            name = '策略决定'
            direction = {'LONG': '多头', 'SHORT': '空头'}.get(event.get('direction'), '')
            detail = (f"当日行情已跑过{direction}激活位（触发 {event['trigger_price']}，"
                      f"激活 {event['activation_price']}），本次不开仓")
        elif reason == 'PROTECTED_FLOOR':
            name = '保护线退出'
            detail = (f"理论保护价 {event['nominal_reference_price']}，名义点数 {event['nominal_move']}"
                      if event.get('nominal_move') is not None else '原记录缺少保护线名义点数')
        elif reason == 'HISTORY_RECORD_INCOMPLETE':
            name = '历史记录降级'
            detail = (f"事件序号 {event.get('event_seq', '—')} · {event.get('error_code', 'E_HISTORY_EVENT_FORMAT')}"
                      '；仅展示不完整，原始交易事实未修改')
        elif reason in ('DEFINITELY_NOT_SENT', 'UNKNOWN'):
            if reason == 'DEFINITELY_NOT_SENT':
                name = '本地拒发'
                detail = _REJECTION_REASONS.get(event.get('rejection_reason'), '发送前校验失败')
            else:
                cancelling = event.get('effect_kind') == 'CANCEL'
                name = {'CANCEL': '撤单结果未知', 'SEND': '发送结果未知'}.get(
                    event.get('effect_kind'), '报撤结果未知')
                detail = '撤单调用异常，原委托继续占用，等待柜台核对' if cancelling else '等待原生委托与成交核对'
                if cancelling and (error_type := event.get('exception_type')):
                    detail += f' · {error_type}'
            if code := event.get('error_code'):
                detail = f'{detail} · {code}'
        else:
            name = '策略决定'
            detail = _STRATEGY_REASONS.get(reason, reason)
            if stop := event.get('day_stop_reason'):
                detail = f'{detail} · {_STRATEGY_REASONS.get(stop, stop)}'
        return (event.get('at') or '—', event.get('contract_id') or '—', name, detail,
                event.get('child_order_id') or '—')

    def _populate(self, grid, entries):
        selected = self._selected(grid, 0)
        populate(grid, [values for _, values in entries])
        grid.clearSelection()
        grid.setCurrentCell(-1, -1)
        for index, (identity, _) in enumerate(entries):
            grid.item(index, 0).setData(Qt.UserRole, identity)
            if identity == selected:
                grid.selectRow(index)

    def _update_controls(self):
        idle = self.request is None and self.pending is None
        connected = self._transport_ok and self._state is not None
        state = self._state or {}
        fault = state.get('clock_recovery_required')
        self.start.setEnabled(idle and connected and not state.get('running') and not fault
                              and not state.get('scope_expired_at'))
        self.recover_clock.setVisible(bool(fault))
        self.recover_clock.setEnabled(bool(idle and connected and fault and state.get('clock_native_reconciled')
                                           and (state.get('clock_health') or {}).get('continuous_samples', 0) >= 2))
        self.stop.setEnabled(idle and connected and state.get('running', False))
        selection = self._selected(self.positions, 0)
        contract = selection[0] if isinstance(selection, tuple) else selection
        row_state = state.get('symbols', {}).get(contract, {})
        can_reduce = not row_state.get('native_conflicts') and (
            state.get('synchronized') if isinstance(selection, tuple) else not row_state.get('detached_positions'))
        self.reduce.setEnabled(idle and connected and selection is not None and bool(can_reduce))
        self.cancel.setEnabled(idle and connected and state.get('synchronized', False) and
                               self._selected(self.orders, 0) is not None)
        self.retry.setEnabled(self.request is None and self.pending is not None)
        self.refresh_button.setEnabled(self.request is None)

    def closeEvent(self, event):
        self.timer.stop()
        if self.request is not None:
            self.request.wait()
        super().closeEvent(event)


def run(pipe):
    app = QApplication.instance() or QApplication([])
    window = OfflineWorkbench(pipe)
    window.show()
    return app.exec()
