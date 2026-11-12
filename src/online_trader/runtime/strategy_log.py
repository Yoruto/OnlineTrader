"""DSV-frequency operator messages from the accepted ordinary entry checks."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from online_trader.contracts.codec import canonical_bytes


TIMING_CHECKS = {'OPENING_WAIT', 'CLOSE_SESSION_NO_OPEN', 'DSV_QUOTE_AGE_EXCEEDED'}
NO_ENTRY_LOG = {'ENTRY_AUTHORITY_DISARMED', 'RECONCILIATION_REQUIRED', 'CONTROL_BARRIER',
                'STRATEGY_EPOCH_REVOKED', 'SCHEDULE_CURRENT_SESSION_UNPROVEN', 'OBSERVATION_ONLY_BACKLOG',
                'OWNED_EXPOSURE_EXISTS'}
DAY_STOPS = {'DAY_STOP_ACTIVE', 'MONTH_STOP_ACTIVE', 'PROTECTION_DAY_STOP', 'DSV_PERSISTED_DAY_OR_MONTH_STOP'}


class DsvEntryLog:
    def __init__(self, kernel, output, profile):
        self.kernel = kernel
        self.path = Path(output) / 'strategy-log.jsonl'
        self.path.touch(exist_ok=True)
        settings = profile.to_record()['settings']
        self.trade = settings['trade']
        self.symbols = {row['code']: row for row in settings['symbols']}
        self.geometry_days = set()
        self.last_time = {}
        self.last_hint = {}

    def due(self, key, now, seconds):
        previous = self.last_time.get(key)
        if previous is not None and (now - previous).total_seconds() < seconds:
            return False
        self.last_time[key] = now
        return True

    def market(self, event, decision):
        checks = decision.get('entry_log', ())
        if not checks:
            return
        reasons = set(decision.get('reasons', ()))
        if reasons & NO_ENTRY_LOG:
            return
        code, day = event['contract_id'], event['trading_day']
        symbol = self.symbols[code]
        now = self.kernel.port.clock.now()
        prefix = f"{symbol['name']}({code})"
        rows = []

        def append(kind, tag, message):
            rows.append({'observed_at_utc': now.isoformat(), 'contract_id': code, 'trading_day': day,
                         'kind': kind, 'tag': tag, 'message': f'{prefix} {message}'})

        if 'NET_LOSS_LIMIT' in reasons:
            if self.due((code, 'NET_LOSS_LIMIT'), now, 600):
                stop_state = (decision.get('stops') or {}).get('state')
                if stop_state is None:
                    stop_state = (self.kernel.statistics.snapshot(code, day) or {}).get('state')
                if stop_state is not None:
                    loss = Decimal(stop_state['net_loss_points'])
                    threshold = Decimal(symbol['stop_offset']) * self.trade['max_net_loss_stops']
                    append('NET_LOSS_LIMIT', '规则', f'当前净亏 {loss:.0f} 点 已达阈值 {threshold:.0f} 点 → **本次不开仓**')
        elif checks[0] in TIMING_CHECKS:
            self.timing_message(code, checks[0], now, event, append)
        elif not reasons & DAY_STOPS:
            self.trigger_messages(code, day, symbol, now, event, checks, append)

        if rows:
            with self.path.open('ab') as stream:
                stream.write(b''.join(canonical_bytes(row) + b'\n' for row in rows))

    def timing_message(self, code, kind, now, event, append):
        if kind == 'OPENING_WAIT' and self.due((code, kind), now, 300):
            minutes = Decimal(self.trade['no_open_after_open_minutes'])
            append(kind, '执行', f'开盘后不足 {minutes:.0f} 分钟，本次信号跳过（开盘后禁开）')
        elif kind == 'CLOSE_SESSION_NO_OPEN' and self.due((code, kind), now, 300):
            minutes = Decimal(self.trade['no_open_before_close_minutes'])
            append(kind, '执行', f'距收盘不足 {minutes:.0f} 分钟，不再开新仓')
        elif kind == 'DSV_QUOTE_AGE_EXCEEDED' and self.due((code, kind), now, 60):
            seconds = (now - datetime.fromisoformat(event['source_time_utc'])).total_seconds()
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
                   f'（2×开仓偏移{opening} − 止损{stop}）→ 多空触发价交叉，本品种今日不建仓（几何条件）')
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
