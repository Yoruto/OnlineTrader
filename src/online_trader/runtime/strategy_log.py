"""Operator messages from accepted DSV decisions and committed execution facts."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from online_trader.contracts.codec import canonical_bytes


BEIJING = timezone(timedelta(hours=8))
TIMING_CHECKS = {'OPENING_WAIT', 'CLOSE_SESSION_NO_OPEN', 'DSV_QUOTE_AGE_EXCEEDED'}
NO_ENTRY_LOG = {'ENTRY_AUTHORITY_DISARMED', 'RECONCILIATION_REQUIRED', 'CONTROL_BARRIER',
                'STRATEGY_EPOCH_REVOKED', 'SCHEDULE_CURRENT_SESSION_UNPROVEN', 'OBSERVATION_ONLY_BACKLOG',
                'OWNED_EXPOSURE_EXISTS'}
DAY_STOPS = {'DAY_STOP_ACTIVE', 'MONTH_STOP_ACTIVE', 'PROTECTION_DAY_STOP', 'DSV_PERSISTED_DAY_OR_MONTH_STOP'}
ENTRY_FILTERS = {
    'STOP_COOLDOWN': ('跳过', '止损后同方向冷却中，本次不开仓', 300),
    'STOP_TRIGGER_UNCHANGED': ('跳过', '触发价仍是止损时的触发价（未出现新高低点），本次不开仓', 300),
    'HYSTERESIS_NOT_READY': ('跳过', '等待价格反向达到滞回点，本次不开仓', None),
    'FIRST_MINUTE_JUMP_DAY_STOP': ('风控', '开盘首分钟跳空，当日停止开仓', 'day'),
    'TAKE_PROFIT_OUTSIDE_PRICE_LIMIT': ('风控', '理论止盈超过对应涨跌停，无法触及，跳过开仓', None),
    'LAST_NEAR_PRICE_LIMIT': ('风控', '最新价接近涨跌停，跳过开仓', None),
    'SAME_DIRECTION_WITHIN_ONE_SECOND': ('风控', '同方向在 1 秒内重复信号，已忽略', None),
    'INSUFFICIENT_MARGIN': ('风控', '资金不足，跳过开仓', None),
    'AVAILABLE_NOT_POSITIVE': ('风控', '可用资金不足，跳过开仓', None),
    'EFFECTIVE_RISK_CAP': ('风控', '账户风险度已达上限，跳过开仓', None),
    'LIVE_DAILY_LOSS_LIMIT': ('风控', '实盘日亏已达上限，停止开仓', 'day'),
    'DAILY_ORDER_LIMIT': ('规则', '报单次数已达上限，当日停止开仓', 'day'),
    'EXPIRY_ENTRY_BLOCK': ('规则', '合约临近到期，停止开仓', 'day'),
    'ACTIVE_SYMBOL_LIMIT': ('风控', '已达持仓品种数上限，跳过开仓', None),
}
EXIT_REASONS = {
    'STOP_LOSS': '止损', 'TAKE_PROFIT': '止盈', 'PROTECTED_FLOOR': '80%保护',
    'TRAILING_STOP': '移动止盈', 'BREAK_EVEN': '保本', 'SESSION_CLOSE': '收盘平仓',
    'ADVERSE_SLIPPAGE': '开仓滑点超限强平', 'DATA_GAP_EXIT': '行情断流强平',
    'MANUAL_EXIT': '手动平仓',
}
SESSION_EVENTS = {
    'ONLINE_SIMULATION_ARMED': ('执行', '已启动策略'),
    'DSV_STRATEGY_STOPPED': ('执行', '已停止开仓'),
    'ONLINE_DSV_STOP_VERIFIED': ('执行', '账户仓位及活动委托已核清，策略停止'),
    'CONTROL_PAUSED': ('执行', '已暂停开仓'),
    'ONLINE_GATEWAY_DISCONTINUITY': ('警告', '行情或交易连接中断'),
    'ONLINE_DSV_GATEWAY_RESYNCED': ('执行', '连接已恢复，柜台状态已同步'),
    'ONLINE_DSV_DAY_CHANGE_REQUIRES_RECONCILIATION': ('警告', '柜台换日，等待核对原持仓及委托'),
    'ONLINE_DSV_FACT_BARRIER': ('警告', '柜台事实待核对'),
    'ONLINE_ARCHIVED_ORDER_CHANGED': ('警告', '历史委托收到不同的柜台回报，已暂停新增并保留原委托身份'),
    'ONLINE_NATIVE_ORDER_REJECTION_CONFLICT': ('警告', '柜台拒单标记与委托或成交数量冲突，等待核对'),
}
COMMITTED_EVENT_TYPES = frozenset(SESSION_EVENTS) | {
    'ENTRY_PREPARED', 'EXIT_PREPARED', 'REPLACEMENT_PREPARED', 'FILL_CONFIRMED',
    'SEND_RESULT', 'SEND_ABORTED', 'ORDER_TERMINAL', 'EXECUTION_CANCEL_TIMEOUT',
    'CANCEL_PREPARED', 'CANCEL_RESULT', 'CANCEL_UNSENT', 'ONLINE_SIMULATION_RECONCILED',
    'EXECUTION_OBSERVATION', 'PROTECTION_OBSERVATION', 'SCHEDULED_DATA_GAP',
}


class DsvEntryLog:
    def __init__(self, kernel, output, profile):
        self.kernel = kernel
        self.path = Path(output) / 'strategy-log.jsonl'
        self.path.touch(exist_ok=False)
        settings = profile.to_record()['settings']
        self.trade = settings['trade']
        self.symbols = {row['code']: row for row in settings['symbols']}
        self.geometry_days = set()
        self.last_time = {}
        self.last_hint = {}
        self.cycle_markers = {}
        self.order_markers = {}

    def write(self, rows):
        if rows:
            with self.path.open('ab') as stream:
                stream.write(b''.join(canonical_bytes(row) + b'\n' for row in rows))

    def due(self, key, now, seconds):
        previous = self.last_time.get(key)
        if previous is not None and (now - previous).total_seconds() < seconds:
            return False
        self.last_time[key] = now
        return True

    def market(self, event, decision):
        checks = decision.get('entry_log', ())
        reasons = set(decision.get('reasons', ()))
        if reasons & NO_ENTRY_LOG or not (checks or reasons):
            return
        code, day = event['contract_id'], event['trading_day']
        symbol = self.symbols[code]
        now = self.kernel.port.strategy_wall_time()
        prefix = f"{symbol['name']}({code})"
        rows = []

        def append(kind, tag, message):
            rows.append({'observed_at_utc': now.isoformat(), 'contract_id': code, 'trading_day': day,
                         'kind': kind, 'tag': tag, 'origin': '策略', 'message': f'{prefix} {message}'})

        if 'NET_LOSS_LIMIT' in reasons:
            if self.due((code, 'NET_LOSS_LIMIT'), now, 600):
                stop_state = (decision.get('stops') or {}).get('state')
                if stop_state is None:
                    stop_state = (self.kernel.statistics.snapshot(code, day) or {}).get('state')
                if stop_state is not None:
                    loss = Decimal(stop_state['net_loss_points'])
                    threshold = Decimal(symbol['stop_offset']) * self.trade['max_net_loss_stops']
                    append('NET_LOSS_LIMIT', '规则', f'当前净亏 {loss:.0f} 点 已达阈值 {threshold:.0f} 点 → **本次不开仓**')
        elif checks and checks[0] in TIMING_CHECKS:
            self.timing_message(code, checks[0], now, event, append)
        else:
            if checks and not reasons & DAY_STOPS:
                self.trigger_messages(code, day, symbol, now, event, checks, append)
            for reason in decision.get('reasons', ()):
                self.filter_message(code, day, reason, now, event, symbol, decision, append)

        self.write(rows)

    def timing_message(self, code, kind, now, event, append):
        if kind == 'OPENING_WAIT' and self.due((code, kind), now, 300):
            minutes = Decimal(self.trade['no_open_after_open_minutes'])
            append(kind, '执行', f'开盘后不足 {minutes:.0f} 分钟，本次信号跳过（开盘后禁开）')
        elif kind == 'CLOSE_SESSION_NO_OPEN' and self.due((code, kind), now, 300):
            minutes = Decimal(self.trade['no_open_before_close_minutes'])
            append(kind, '执行', f'距收盘不足 {minutes:.0f} 分钟，不再开新仓')
        elif kind == 'DSV_QUOTE_AGE_EXCEEDED' and self.due((code, kind), now, 60):
            seconds = (now - datetime.fromisoformat(event['source_time_utc']).replace(microsecond=0)).total_seconds()
            limit = Decimal(self.trade['max_quote_age_seconds'])
            append(kind, '风控', f'行情已滞后 {seconds:.0f} 秒（>{limit:.0f} 秒），暂停开新仓')

    def trigger_messages(self, code, day, symbol, now, event, checks, append):
        opening, stop, profit = (Decimal(symbol[key]) for key in ('open_offset', 'stop_offset', 'profit_offset'))
        high, low, last = (Decimal(event[key]) for key in ('day_high', 'day_low', 'last'))
        long_trigger, short_trigger = low + opening, high - opening
        geometry = 'GEOMETRY_BLOCK' in checks
        if geometry and (code, day) not in self.geometry_days:
            self.geometry_days.add((code, day))
            limit = 2 * opening - stop
            append('GEOMETRY_BLOCK', '提示', f'当日振幅 {high-low:.0f} 点 > 上限 {limit:.0f} 点'
                   f'（2×开仓偏移{opening} − 止损{stop}）→ 多空触发价交叉，本品种今日不建仓（几何条件，不是故障）')
        signature = (f'{long_trigger:.1f}', f'{short_trigger:.1f}', f'{last:.1f}', geometry)
        if signature != self.last_hint.get(code) and self.due((code, 'TRIGGER_HINT'), now, 60):
            self.last_hint[code] = signature
            suffix = '（几何条件不满足，暂不建仓）' if geometry else ''
            append('TRIGGER_HINT', '提示', f'当日 高{high:.0f}/低{low:.0f}（振幅 {high-low:.0f}，开仓偏移 {opening}）'
                   f'→ 多开触发 {long_trigger:.1f} / 空开触发 {short_trigger:.1f}，现价 {last:.1f}{suffix}')
        activation = checks[-1]
        if activation.endswith(':NO_CHASE_AFTER_ACT') and self.due((code, day, 'ACTIVATION_PASSED'), now, 600):
            ratio_key = 'protect_80_ratio' if self.trade['protect_80_enabled'] else 'trailing_activate_ratio'
            ratio = Decimal(self.trade[ratio_key])
            is_long = activation.startswith('LONG:')
            direction, trigger = ('多', long_trigger) if is_long else ('空', short_trigger)
            level = trigger + profit * ratio if is_long else trigger - profit * ratio
            append(activation, '跳过', f'当日行情已跑过{direction}头激活位（触发 {trigger:.1f} '
                   f'∓ 止盈×{ratio:.2f} = {level:.1f}），本次不开仓')

    def filter_message(self, code, day, reason, now, event, symbol, decision, append):
        direction, sep, kind = reason.partition(':')
        if not sep:
            kind, direction = direction, ''
        side = {'LONG': '多头', 'SHORT': '空头'}.get(direction, '')
        rule = ENTRY_FILTERS.get(kind)
        if rule is not None:
            tag, message, interval = rule
            # A quote can be repeated by the SDK. Non-throttled DSV filters
            # consume an opportunity or emit on a distinct accepted tick.
            if interval == 'day':
                key = (code, day, kind)
                if key in self.last_time:
                    return
                self.last_time[key] = now
            elif interval is not None and not self.due((code, kind), now, interval):
                return
            if kind == 'EFFECTIVE_RISK_CAP':
                cap = (decision.get('risk') or {}).get('metrics', {}).get('effective_risk_cap')
                if cap is not None:
                    message = f'账户风险度已达上限 {Decimal(cap):.2%}，跳过开仓'
            append(reason, tag, side + (' ' if side else '') + message)
        elif kind == 'CHASE_CAP_EXCEEDED' and direction in {'LONG', 'SHORT'}:
            opening = Decimal(symbol['open_offset'])
            is_long = direction == 'LONG'
            trigger = Decimal(event['day_low']) + opening if is_long else Decimal(event['day_high']) - opening
            opponent = Decimal(event['ask1' if is_long else 'bid1'])
            last = Decimal(event['last'])
            started = datetime.fromisoformat(event['session_open_utc'])
            elapsed = (now - started).total_seconds()
            window = float(self.trade['chase_window_minutes']) * 60
            opening_session = event.get('session_is_opening', started.astimezone(BEIJING).strftime('%H:%M') in
                                        {'09:00', '09:30', '21:00'})
            ratio = self.trade['chase_slip_ratio' if opening_session and 0 <= elapsed <= window else 'entry_slip_ratio']
            cap = max(1, round(opening * Decimal(ratio)))
            over = max(opponent, last) - trigger - cap if is_long else trigger - min(opponent, last) - cap
            append(reason, '跳过', f'{side} 对手价 {opponent:.1f} / 最新价 {last:.1f}，触发价 {trigger:.1f}，'
                   f'超出上限 {over:.1f} 点（单边上限 {cap:.1f} 点，有利方向除外），本次不开仓')

    def committed(self, kind, event_seq, observed_at_utc, data):
        """Describe an event once from committed state, preserving its provenance."""
        rows = []

        def append(tag, message, *, origin='策略', code=None, day=None, category=None, **facts):
            if code:
                name = self.symbols.get(code, {}).get('name', code)
                message = f'{name}({code}) {message}'
            rows.append({'observed_at_utc': observed_at_utc, 'kind': kind, 'event_seq': str(event_seq),
                         'tag': tag, 'category': category, 'origin': origin, 'message': message,
                         'contract_id': code, 'trading_day': day, **facts})

        session = SESSION_EVENTS.get(kind)
        if session is not None:
            append(*session)
        cycle = data.get('cycle')
        if isinstance(cycle, dict):
            self.cycle_event(kind, cycle, data.get('fill'), append)
        self.write(rows)

    def cycle_event(self, kind, cycle, fill, append):
        plan, protection = cycle['plan'], cycle['protection']
        code, day = plan['contract_id'], plan['trading_day']
        side = '多头' if plan['direction'] == 'LONG' else '空头'
        identity = cycle['position_cycle_id']
        previous = self.cycle_markers.get(identity, (None, None, None, None))
        current = (protection.get('protected_event_seq'), protection.get('phase1_target'),
                   protection.get('phase2_target'), cycle['lifecycle'])
        self.cycle_markers[identity] = current

        def report(tag, text, **fields):
            append(tag, text, code=code, day=day, **fields)

        if current[0] is not None and previous[0] != current[0]:
            settings = plan['profit_protection']
            ratio = Decimal(settings['activation_ratio'])
            keep = Decimal(settings['floor_ratio'])
            offset = Decimal(plan['fixed_take_profit']['distance_points'])
            base = Decimal(plan['theoretical_entry_price'])
            floor = base + offset * keep * (1 if plan['direction'] == 'LONG' else -1)
            report('保护', f'浮盈达止盈 {ratio:.0%}（最高 {protection["max_favorable_move"]} 点）'
                   f' → 保护线 {floor:.1f}（止盈×{keep:.0%}）')
            if settings.get('block_new_for_trading_day'):
                report('规则', f'浮盈曾达止盈 {ratio:.0%} → 当日停止开仓')
        for stage in (1, 2):
            if current[stage] is not None and previous[stage] != current[stage]:
                report('半仓', f'{side}第 {stage} 档减仓，目标剩余 {current[stage]} 手', category='half')
        if isinstance(fill, dict):
            order = next((row for row in cycle['orders'] if row['child_order_id'] == fill['child_order_id']), None)
            opening = fill['effect'] == 'OPEN'
            manual = cycle.get('entry_origin') == 'MANUAL' if opening else (
                fill['child_order_id'] in (cycle.get('execution') or {}).get('manual_reduction_children', {}))
            target = '开仓' if opening else '平仓'
            partial = '部分成交' if order and order['filled_qty'] < order['original_qty'] else '成交'
            stamp = (fill.get('native_day_evidence') or {}).get('source_time_ns')
            if stamp:
                seconds, nanos = divmod(int(stamp), 1_000_000_000)
                native_time = datetime.fromtimestamp(seconds,timezone.utc) + timedelta(microseconds=nanos//1000)
                native_time = native_time.isoformat().replace('+00:00','Z')
            else:
                native_time = None
            report('柜台成交', f'{side}{target} {partial} {fill["quantity"]} 手 @ {fill["price"]}',
                   origin='柜台', category='manual' if manual else 'open' if opening else 'close',
                   child_order_id=fill['child_order_id'], trade_key=fill['trade_key'],
                   native_time_utc=native_time)
        if current[3] == 'CLOSED' and previous[3] != 'CLOSED':
            exit_reason = (cycle.get('nominal_exit') or {}).get('exit_reason')
            reason = EXIT_REASONS.get(exit_reason, '仓位与委托已核对')
            report('平仓', f'{side}{reason}，周期已结案', category='manual' if exit_reason == 'MANUAL_EXIT' else 'close')
            for order in cycle['orders']:
                self.order_markers.pop(order['child_order_id'], None)
            return
        if current[3] == 'CLOSED':
            return
        for order in cycle['orders']:
            self.order_event(kind, cycle, order, side, report)

    def order_event(self, kind, cycle, order, side, report):
        child = order['child_order_id']
        current = (order['send_state'], order['cancel_status'], order['terminal_confirmed'], order['broker_state'])
        previous = self.order_markers.get(child, (None, None, False, None))
        self.order_markers[child] = current
        opening = order['effect'] == 'OPEN'
        manual = cycle.get('entry_origin') == 'MANUAL' if opening else (
            child in (cycle.get('execution') or {}).get('manual_reduction_children', {}))
        category = 'manual' if manual else 'open' if opening else 'close'
        label = f'{side}{"开仓" if opening else "平仓"}'
        details = f'{label} {order["original_qty"]} 手 @ {order["limit_price"]}'
        fields = {'child_order_id': child, 'native_order_id': cycle.get('native_order_keys', {}).get(child)}
        if previous[0] is None and current[0] == 'SEND_POSSIBLE':
            candidate = (cycle.get('strategy_metadata') or {}).get('candidate') or {}
            if opening and candidate.get('theoretical_price') is not None:
                details += f'，理论开仓 {candidate["theoretical_price"]}'
            if kind == 'REPLACEMENT_PREPARED':
                details = '撤单终态后重报 · ' + details
            report('手动' if manual else '开仓' if opening else '平仓', details + '，准备报单',
                   category=category, **fields)
        elif current[0] != previous[0] and current[0] in {'CALL_RETURNED', 'UNKNOWN', 'DEFINITELY_NOT_SENT'}:
            status = {'CALL_RETURNED':'报单请求已提交', 'UNKNOWN':'报单结果待核对',
                      'DEFINITELY_NOT_SENT':'发送条件变化，报单未发出'}[current[0]]
            abort = cycle.get('last_dispatch_abort') or {}
            if current[0] == 'DEFINITELY_NOT_SENT' and abort.get('child_order_id') == child:
                labels = {'ORIGINAL_BOOT':'启动身份变化', 'CURRENT_SESSION':'盘次待确认',
                          'ENTRY_AUTHORITY':'开仓权限变化', 'RISK_BUDGET':'风险预算不足',
                          'NOMINAL_DAY_STOP':'当日停开', 'STRATEGY_RECHECK':'策略条件复核未通过',
                          'ORIGINAL_EXIT_FACTS':'原平仓行情或回报待核对', 'ORIGINAL_EXIT_PRICE':'原平仓价格变化',
                          'ORIGINAL_EXIT_QUANTITY':'原平仓可用手数变化', 'EXECUTION_PRICE':'委托价格复核未通过',
                          'E_CLOCK':'时钟偏差超限', 'E_OWNERSHIP':'柜台归属待核对',
                          'E_FACT_BARRIER':'柜台同步待核对', 'E_GATEWAY_SCOPE':'行情、时窗或范围条件未通过'}
                status += ' · ' + ' / '.join(dict.fromkeys(labels.get(reason,'发送校验未通过') for reason in abort['reasons']))
            report('执行' if current[0] == 'CALL_RETURNED' else '警告', f'{details}，{status}',
                   origin='执行器', category=category if current[0] == 'CALL_RETURNED' else 'error', **fields)
        if current[1] != previous[1] and current[1] in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}:
            status = {'SEND_POSSIBLE':'准备撤单', 'CALL_RETURNED':'撤单请求已提交',
                      'UNKNOWN':'撤单结果待核对'}[current[1]]
            report('执行' if current[1] != 'UNKNOWN' else '警告', f'{label} {status}',
                   origin='执行器', category=category if current[1] != 'UNKNOWN' else 'error', **fields)
        if current[2] and not previous[2] and current[3] in {'FILLED', 'CANCELLED', 'REJECTED'}:
            status = {'FILLED':'已确认全成', 'CANCELLED':'已确认撤单', 'REJECTED':'柜台拒单'}[current[3]]
            report('柜台委托', f'{label} {status}，累计成交 {order["filled_qty"]} 手',
                   origin='柜台', category='error' if current[3] == 'REJECTED' else category, **fields)
        if kind == 'EXECUTION_CANCEL_TIMEOUT' and not current[2] and current[1] in {'CALL_RETURNED', 'UNKNOWN'}:
            report('警告', f'{label} 撤单超时，等待柜台核对', category='error', **fields)
