"""Windows workbench for the offline owner; all effects go through its pipe."""

from uuid import uuid4

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QHeaderView, QLabel, QMainWindow, QSpinBox, QTabWidget, QVBoxLayout, QWidget

from online_trader.desktop.workbench_layout import button, populate, row, table
from online_trader.dsv_runtime.offline_endpoint import OfflineClient, _TIMEOUT
from online_trader.errors import TraderError


_REJECTION_REASONS = {
    'QUOTE_OR_PRICE_SCOPE': '缺少有效报价、存在外部委托或限价不合规',
    'ACCOUNT_SCOPE_OR_WINDOW': '合约、交易日或开仓时段超出账户授权',
    'PRE_SEND_VALIDATION': '发送前校验失败',
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
        self.retry = button('查询原回执', self._query_receipt)
        self.refresh_button = button('刷新', self.refresh)
        layout.addLayout(row(self.start, self.stop, self.refresh_button, self.retry))

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
        layout.addWidget(evidence, 3)
        self.receipt = QLabel('—', objectName='offlineReceipt')
        self.receipt.setWordWrap(True)
        layout.addWidget(self.receipt)

        for shortcut, control in (('F5', self.start), ('F6', self.stop)):
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
            if code in _TIMEOUT:
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
        elif any(row.get('native_conflicts') for row in state['symbols'].values()):
            status = '迟到平仓成交待核对原周期'
        elif any(row.get('detached_positions') for row in state['symbols'].values()):
            status = '迟到成交待核对归属'
        elif any((row.get('position') or {}).get('gap_exit') for row in state['symbols'].values()):
            status = '断流退出待行情或成交'
        else:
            status = '策略运行中' if state['running'] else '已停止新增'
        self.status.setText(status)
        mode = 'SDK 模拟' if state.get('sdk_connection') else '离线回放'
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

    @staticmethod
    def _activity_row(event):
        reason = event['reason']
        if reason == 'EXPIRY_ENTRY_BLOCK':
            name = '到期阻断开仓'
            detail = (f"剩余 {event['days_to_expiry']} 天，达到或低于阈值 {event['min_days_to_expiry']} 天"
                      if event.get('days_to_expiry') is not None else '达到或低于到期开仓阈值')
        elif reason == 'PROTECTED_FLOOR':
            name = '保护线退出'
            detail = (f"理论保护价 {event['nominal_reference_price']}，名义点数 {event['nominal_move']}"
                      if event.get('nominal_move') is not None else '原记录缺少保护线名义点数')
        else:
            if reason == 'DEFINITELY_NOT_SENT':
                name = '本地拒发'
                detail = _REJECTION_REASONS.get(event.get('rejection_reason'), '发送前校验失败')
            else:
                name = '发送结果未知'
                detail = '等待原生委托与成交核对'
            if code := event.get('error_code'):
                detail = f'{detail} · {code}'
        return (event.get('at') or '—', event.get('contract_id', '—'), name, detail,
                event.get('child_order_id', '—'))

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
        self.start.setEnabled(idle and connected and not state.get('running'))
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
