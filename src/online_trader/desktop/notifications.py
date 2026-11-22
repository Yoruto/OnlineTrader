"""DSV desktop sound events sourced from accepted workbench snapshots."""
from decimal import Decimal, InvalidOperation
from threading import Thread

from PySide6.QtWidgets import QApplication, QCheckBox, QGroupBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from online_trader.desktop.dingtalk import WorkbenchDingTalk


SOUNDS = {
    'open': ('开仓提醒', 880),
    'close': ('平仓提醒', 660),
    'error': ('错误提醒', 440),
    'margin': ('保证金变化通知', 990),
}

# Original DSV's MARGIN_FEE_TABLE has four rows. These references are only
# for its notification comparison, independent of live risk or fee inputs.
DSV_EXIT_LABELS = {
    'ADVERSE_SLIPPAGE': '滑点超限平仓',
    'SESSION_CLOSE': '收盘平仓',
    'PROTECTED_FLOOR': '80%保护平仓',
    'TAKE_PROFIT': '止盈平仓',
    'STOP_LOSS': '止损平仓',
    'TRAILING_STOP': '移动止盈平仓',
    'BREAK_EVEN': '保本平仓',
    'DATA_GAP_EXIT': '行情断流强平',
}

DSV_MARGIN_REFERENCE = {
    '棕榈油': Decimal('0.12'),
    '塑料': Decimal('0.11'),
    '沥青': Decimal('0.10'),
    '菜油': Decimal('0.12'),
}


def _windows_tone(frequency, duration):
    if QApplication.platformName() == 'offscreen':
        return

    def play():
        try:
            import winsound
            winsound.Beep(frequency, duration)
        except (ImportError, RuntimeError, OSError):
            return

    Thread(target=play, daemon=True).start()


class WorkbenchSounds:
    def __init__(self, settings, *, tone=_windows_tone, dingtalk=None, log=None):
        self.settings = settings
        self.tone = tone
        self.dingtalk = dingtalk if dingtalk is not None else WorkbenchDingTalk(settings, log=log)
        self.identity = None
        self.checkpoint = -1
        self.seen_trades = set()
        self.trade_events = set()
        self.strategy_events = set()
        self.strategy_ready = False
        self.error_events = set()
        self.margin_events = set()
        self.request_failed = False

    @staticmethod
    def _trade_identity(trade):
        raw_key = trade.get('trade_key')
        if isinstance(raw_key, (list, tuple)) and raw_key:
            return ('key', tuple(str(part) for part in raw_key))
        if isinstance(raw_key, str) and raw_key:
            return ('key', raw_key)
        return (
            'fill',
            str(trade.get('position_cycle_id') or ''),
            str(trade.get('child_order_id') or ''),
            str(trade.get('native_trade_id') or ''),
            str(trade.get('event_seq') or ''),
            str(trade.get('effect') or ''),
            str(trade.get('quantity') or ''),
            str(trade.get('price') or ''),
        )

    def _emit(self, event, *, contract=None, message=None):
        if not self.settings.value(f'sound/{event}', True, bool):
            return
        if self.settings.value('sound/enabled', True, bool):
            self.tone(SOUNDS[event][1], 200)
        if message is not None:
            self.dingtalk.notify(event, contract, message)

    def _checkbox(self, name, label):
        control = QCheckBox(label)
        key = f'sound/{name}'
        control.setChecked(self.settings.value(key, True, bool))
        control.toggled.connect(lambda checked: self.settings.setValue(key, checked))
        return control

    def settings_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        notifications = QGroupBox('通知设置')
        controls = QHBoxLayout(notifications)
        controls.addWidget(self._checkbox('enabled', '启用声音提醒'))
        test = QPushButton('测试声音')
        test.clicked.connect(lambda: self.tone(SOUNDS['open'][1], 200))
        controls.addWidget(test)
        controls.addStretch()
        layout.addWidget(notifications)

        sound_events = QGroupBox('通知事件')
        row = QHBoxLayout(sound_events)
        for event, (label, _) in SOUNDS.items():
            row.addWidget(self._checkbox(event, label))
        row.addStretch()
        layout.addWidget(sound_events)
        layout.addWidget(self.dingtalk.settings_panel())
        return panel

    def observe_request_error(self):
        if self.identity is not None and not self.request_failed and not self.error_events:
            self._emit('error', message='工作台无法刷新柜台状态，请检查连接。')
        self.request_failed = True

    def observe(self, view):
        identity = view['account_key'], view['kernel_boot_id']
        checkpoint = int(view['checkpoint_event_seq'])
        initial = identity != self.identity
        if initial:
            self.identity = identity
            self.checkpoint = -1
            self.seen_trades.clear()
            self.trade_events.clear()
            self.strategy_events.clear()
            self.strategy_ready = False
            self.error_events.clear()
            self.margin_events.clear()
        if checkpoint < self.checkpoint:
            return

        orders = {order['child_order_id']: order.get('intent_id') for order in view['orders']}
        new_trade_events = {}
        for trade in view['trades']:
            trade_id = self._trade_identity(trade)
            is_unseen_fill = trade_id not in self.seen_trades
            self.seen_trades.add(trade_id)
            effect = trade.get('effect')
            if effect == 'OPEN':
                event = 'open', trade['position_cycle_id']
            elif effect == 'REDUCE':
                child = (trade.get('child_order_id')
                         or (trade['trade_key'][0]
                             if isinstance(trade.get('trade_key'), (list, tuple)) and trade['trade_key']
                             else str(trade.get('trade_key', ''))))
                event = 'close', orders.get(child) or child
            else:
                continue
            if event not in self.trade_events and not initial and is_unseen_fill:
                new_trade_events.setdefault(event, []).append(trade)
        self.trade_events.update(new_trade_events)
        if initial:
            for trade in view['trades']:
                if trade.get('effect') == 'OPEN':
                    self.trade_events.add(('open', trade['position_cycle_id']))
                elif trade.get('effect') == 'REDUCE':
                    child = (trade.get('child_order_id')
                             or (trade['trade_key'][0]
                                 if isinstance(trade.get('trade_key'), (list, tuple)) and trade['trade_key']
                                 else str(trade.get('trade_key', ''))))
                    self.trade_events.add(('close', orders.get(child) or child))
            self.trade_events.update(('open', cycle['position_cycle_id']) for cycle in view['cycles']
                                     if cycle['remaining_quantity'] > 0)
        self.checkpoint = checkpoint
        if not view['truncated']:
            cycles = {cycle['position_cycle_id']:cycle for cycle in view['cycles']}
            strategy_closes = self._observe_strategy_notices(cycles, initial=not self.strategy_ready)
            self.strategy_ready = True
            for (event, _), trades in new_trade_events.items():
                cycle_id = trades[0]['position_cycle_id']
                cycle = cycles.get(cycle_id, {})
                if event == 'open' and cycle.get('initial_open_confirmation') is not None:
                    continue
                if event == 'close' and cycle_id in strategy_closes and cycle.get('exit_reason') != 'MANUAL_EXIT':
                    continue
                self._notify_trade(event, trades, cycles)

        connection = view['connection']
        error_text = {key:text for key, text in (
            ('fault','柜台连接故障'), ('clock_fault','时钟异常'),
            ('storage_failed','交易记录写入失败'), ('recovery_required','持仓或委托待核对'),
        ) if connection.get(key)}
        for receipt in view['commands']:
            if receipt['status'] in {'FAILED', 'REJECTED', 'NEEDS_RECONCILIATION'}:
                error_text[receipt['command_id'], receipt['error_code']] = '交易命令执行失败或待核对'
        for block in view.get('blocks', ()):
            code = block.get('reason_code')
            if not block.get('active'):
                continue
            if code in {'E_UNKNOWN_SEND', 'E_ATTEMPTS_EXHAUSTED'}:
                error_text[code] = '委托结果待核对' if code == 'E_UNKNOWN_SEND' else '平仓重试次数已达上限'
            elif code=='EXIT_REDUCTION_TERMINAL_UNFILLED':
                error_text[code,block['position_cycle_id']] = f"{block['contract_id']} 平仓委托已终态，剩余目标未完成，请人工核对"
        new_errors = error_text.keys() - self.error_events
        if not initial and not self.request_failed and new_errors:
            self._emit('error', message='、'.join(sorted({error_text[key] for key in new_errors})))
        self.error_events = set(error_text)
        self.request_failed = False

        config = view['configuration']
        for market in view['markets']:
            code = market['contract_id']
            discrepancy = self._margin_discrepancy(market, config.get('margin_rate_alert_threshold'))
            if discrepancy is False:
                self.margin_events.discard(code)
            elif discrepancy and code not in self.margin_events:
                self.margin_events.add(code)
                if not initial and config.get('margin_alert_grace') is False:
                    self._emit('margin', contract=code, message=f"{market['name']}({code}) 保证金率与参考值有差异。")

    def _once_strategy_notice(self, key, event, contract, message, initial):
        if key in self.strategy_events:
            return
        self.strategy_events.add(key)
        if not initial:
            self._emit(event, contract=contract, message=message)

    def _observe_strategy_notices(self, cycles, *, initial):
        strategy_closes = set()
        for cycle_id, cycle in cycles.items():
            code = cycle.get('contract_id')
            if not code:
                continue
            confirmation = cycle.get('initial_open_confirmation')
            if confirmation is not None and confirmation['status'] == 'CONFIRMED':
                tag = '延续开' if cycle.get('entry_tag') == 'CONTINUATION' else '开仓'
                side = {'LONG':'多','SHORT':'空'}.get(cycle.get('direction'), '')
                qty = confirmation['quantity']
                average = Decimal(confirmation['value']) / qty
                self._once_strategy_notice((cycle_id,'open'), 'open', code,
                    f'({code}) {tag}{side}已成交 {qty} 手 @{average:.1f}', initial)
            for stage in (1, 2):
                target = cycle.get(f'phase{stage}_target')
                if target is not None:
                    strategy_closes.add(cycle_id)
                    self._once_strategy_notice((cycle_id,f'half{stage}'), 'close', code,
                        f'({code}) 第{stage}档半仓平仓已触发，目标剩余 {target} 手。', initial)
            if cycle.get('target_remaining_quantity') != 0 or cycle.get('exit_reason') == 'MANUAL_EXIT':
                continue
            reason = cycle.get('exit_reason')
            if reason not in DSV_EXIT_LABELS:
                reason = next((item for item in cycle.get('exit_reasons', ()) if item in DSV_EXIT_LABELS), None)
            if reason is not None:
                strategy_closes.add(cycle_id)
                price = cycle.get('exit_reference_price')
                price_text = f'，参考价 {price}' if price is not None else ''
                event = 'error' if reason == 'DATA_GAP_EXIT' else 'close'
                self._once_strategy_notice((cycle_id,'full'), event, code,
                    f'({code}) {DSV_EXIT_LABELS[reason]}已触发{price_text}。', initial)
        return strategy_closes

    def _notify_trade(self, event, trades, cycles):
        first = trades[0]
        contract = first.get('contract_id')
        if not contract:
            self._emit(event)
            return
        cycle = cycles.get(first['position_cycle_id'], {})
        action = '开仓' if event == 'open' else '平仓'
        total = sum(row.get('quantity', 0) for row in trades)
        quantity = f' {total} 手' if total else ''
        direction = {'LONG':'多', 'SHORT':'空'}.get(cycle.get('direction'), '')
        price = ''
        if total and all(trade.get('price') is not None for trade in trades):
            average = sum(Decimal(trade['price'])*trade['quantity'] for trade in trades)/total
            price = f' @{average:.2f}'
        remaining = cycle.get('remaining_quantity')
        suffix = f'，余 {remaining} 手' if event == 'close' and remaining is not None else ''
        self._emit(event, contract=contract, message=f'{contract}({contract}) {action}{direction}已成交{quantity}{price}{suffix}')

    @staticmethod
    def _margin_discrepancy(market, threshold):
        reference = DSV_MARGIN_REFERENCE.get(market.get('name'))
        if reference is None or threshold is None or not market.get('fresh'):
            return None
        try:
            rate_threshold = Decimal(threshold)
            margin, last, multiplier = (Decimal(market[key]) for key in ('margin', 'last', 'volume_multiple'))
            if not all(value.is_finite() and value > 0 for value in (rate_threshold, margin, last, multiplier)):
                return None
        except (InvalidOperation, KeyError, ValueError, TypeError):
            return None
        notional = last * multiplier
        return abs(margin - reference * notional) >= rate_threshold * notional
