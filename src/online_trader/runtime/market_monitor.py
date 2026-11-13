"""Read-only DSV display timing and optional native tick diagnostics."""
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from online_trader.configuration.instrument_rules import live_margin_rate
from online_trader.contracts.codec import canonical_bytes
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import utc
from online_trader.gateway.tqkq import number
from online_trader.scheduling.reference import clock_observation
from online_trader.strategy.entry import CHINA_TIME


class DsvMarketMonitor:
    def __init__(self, contracts, calendar, *, diagnostic=False):
        self.contracts = tuple(contracts)
        self.calendar = calendar
        self.diagnostic = diagnostic
        self.source_epoch = None
        self.previous_stamps = {}
        self.arrivals = {}
        self.margin_rates = {}
        self.closing_sessions = {}
        self.trading_day = None
        self.sessions = {}
        self.latest = {'sample_at_utc': None, 'quote_delay_seconds': None, 'quote_status': '等待行情'}
        self.diagnostic_samples = deque(maxlen=128)
        self.last_session = {}
        self.reconnecting = set()
        self.loop_samples = {}
        self.last_summary_ns = None
        self.recoveries = {}

    def invalidate(self, wall):
        self.arrivals.clear()
        self.margin_rates.clear()
        self.closing_sessions.clear()
        self.loop_samples.clear()
        self.diagnostic_samples.clear()
        self.recoveries.clear()
        self.latest = {'sample_at_utc': utc(wall), 'quote_delay_seconds': None, 'quote_status': '连接中'}

    def observe(self, port, *, returned_ns, wall):
        """Capture the SDK return timestamp separately from the display clock."""
        if port.source_epoch != self.source_epoch:
            if self.diagnostic and self.source_epoch is not None:
                self.reconnecting.update(self.contracts)
            self.invalidate(wall)
            self.source_epoch = port.source_epoch
        instant = utc(port.clock.now())
        self.sessions = {code: self.calendar.query(code, instant) for code in self.contracts}
        days = {window['trading_day'] for window in self.sessions.values() if window['is_trading']}
        if not days and self.trading_day is None:
            days = {(window.get('next_session') or {}).get('trading_day')
                    for window in self.sessions.values()}
        if len(days) == 1 and None not in days:
            if self.trading_day is not None and self.trading_day not in days:
                self.invalidate(wall)
            self.trading_day = next(iter(days))
        if port.fault:
            if self.diagnostic:
                self.reconnecting.update(self.contracts)
            self.invalidate(wall)
            return
        self.recoveries.clear()

        lag = max(((wall - arrival['wall']).total_seconds() for arrival in self.arrivals.values()), default=None)
        status = '等待行情'
        for code in self.contracts:
            quote = port.quotes.get(code)
            if quote is None:
                self.margin_rates.pop(code, None)
                continue
            raw = getattr(quote, 'datetime', None)
            try:
                source = datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                self.margin_rates.pop(code, None)
                if raw and code == self.contracts[0]:
                    status = '未知'
                continue
            if source.tzinfo is None:
                source = source.replace(tzinfo=CHINA_TIME)
            if raw != self.previous_stamps.get(code):
                self.previous_stamps[code] = raw
                previous = self.arrivals.get(code)
                if previous is not None:
                    self.recoveries[code] = max(0, (wall - previous['wall']).total_seconds())
                self.arrivals[code] = {'source': source, 'wall': wall, 'monotonic_ns': returned_ns}
                source_lag = (wall - source.replace(microsecond=0)).total_seconds()
                if 0 <= source_lag < 86_400:
                    lag = source_lag
                if self.diagnostic:
                    count, previous, gap = self.loop_samples.get(code, (0, returned_ns, 0))
                    self.loop_samples[code] = (count + 1, returned_ns, max(gap, returned_ns - previous))
            if code in self.arrivals:
                margin = live_margin_rate(*(number(getattr(quote, key, None))
                                            for key in ('margin', 'last_price', 'volume_multiple')))
                self.margin_rates[code] = None if margin is None else decimal_text(margin)
                if code == self.contracts[0]:
                    age = (wall - source.replace(microsecond=0)).total_seconds()
                    active = self.sessions[code]['is_trading']
                    status = '正常' if age < 60 else '延迟' if age < 300 else '无行情' if active else '休市'

        self.latest = {'sample_at_utc': utc(wall), 'quote_delay_seconds': None if lag is None else f'{lag:.6f}',
                       'quote_status': status}

    def closing_protection(self, code, advance_seconds):
        arrival = self.arrivals.get(code)
        if arrival is None:
            return False
        live = self.sessions[code]
        if live['session']:
            window = live
        else:
            stamp = utc(arrival['source'].astimezone(timezone.utc))
            window = self.calendar.query(code, stamp)
        period = window['session'] or {}
        if not period.get('force_close_at_end') or period['trading_day'] != self.trading_day:
            return False
        identity = (period['trading_day'], period['session_id'])
        if live['is_trading'] and live['until_close_ms'] <= advance_seconds * 1000:
            self.closing_sessions[code] = identity
        return self.closing_sessions.get(code) == identity

    def processed(self, event, start_ns, end_ns, clock):
        if not self.diagnostic:
            return
        code = event['contract_id']
        if event['source_epoch'] != self.source_epoch or code not in self.arrivals:
            return
        arrival = self.arrivals[code]
        if arrival['source'] != datetime.fromisoformat(event['source_time_utc']):
            return
        if not arrival['monotonic_ns'] <= start_ns <= end_ns:
            return
        session = (event['trading_day'], event['session_id'])
        previous = self.last_session.get(code)
        if code in self.reconnecting:
            phase = 'RECONNECT'
        elif previous != session:
            window = self.sessions[code]
            period = window['session']
            if period and window['since_open_ms'] <= 60_000:
                phase = 'OPEN' if period['is_opening_session'] else 'RESUME'
            else:
                phase = 'STEADY'
        else:
            phase = 'STEADY'
        self.reconnecting.discard(code)
        self.last_session[code] = session
        record = {key: event.get(key) for key in ('contract_id', 'trading_day', 'session_id', 'source_epoch',
                  'native_tick_id', 'native_time_ns', 'source_time_utc')}
        self.diagnostic_samples.append(record | {
            'sdk_returned_at_utc': utc(arrival['wall']),
            'sdk_returned_monotonic_ns': str(arrival['monotonic_ns']),
            'strategy_start_monotonic_ns': str(start_ns), 'strategy_end_monotonic_ns': str(end_ns),
            'clock': diagnostic_clock(clock, wall=arrival['wall'], monotonic_ns=arrival['monotonic_ns']),
            'phase': phase,
        })

    def diagnostics(self, now_ns):
        if not self.diagnostic:
            return None
        wall = datetime.fromisoformat(self.latest['sample_at_utc']) if self.latest['sample_at_utc'] else None
        symbols = []
        for code in self.contracts:
            window = self.sessions.get(code, {})
            arrival = self.arrivals.get(code)
            age = (wall - arrival['wall']).total_seconds() if wall is not None and arrival is not None else None
            symbols.append({'contract_id': code, 'session_id': (window.get('session') or {}).get('session_id'),
                            'active': window.get('is_trading') is True,
                            'quote_age_seconds': f'{age:.6f}' if age is not None else None})
        return {'snapshot_at_utc': utc(datetime.now(timezone.utc)), 'snapshot_monotonic_ns': str(now_ns),
                'source_epoch': self.source_epoch, 'trading_day': self.trading_day,
                'ticks': list(self.diagnostic_samples), 'symbols': symbols}

    def summary(self, now_ns):
        if not self.diagnostic:
            return None
        if self.last_summary_ns is None:
            self.last_summary_ns = now_ns
        if now_ns - self.last_summary_ns < 60_000_000_000:
            return None
        parts = []
        for code, (count, _, gap) in self.loop_samples.items():
            parts.append(f'{code} {count}次（最大间隔 {gap/1e9:.1f} 秒）')
        self.last_summary_ns = now_ns
        self.loop_samples.clear()
        return '；'.join(parts) or '未收到新的报价'


def diagnostic_clock(clock, *, wall, monotonic_ns):
    try:
        observation = clock_observation(clock)
        elapsed = int(observation['monotonic_ns']) - monotonic_ns
        reference_at_sample = datetime.fromisoformat(observation['reference_utc'])
        skew_ms = (reference_at_sample - wall).total_seconds()*1000 - elapsed/1_000_000
        return {'reference_minus_system_ms': f'{skew_ms:.3f}',
                'rtt_ms': [f'{Decimal(row["rtt_seconds"]) * 1000:.3f}'
                           for row in observation['reference'].get('samples', ()) if row.get('valid')]}
    except (AttributeError, KeyError, InvalidOperation, TraderError, ValueError, TypeError):
        return None


def append_latency(path, row, *, capacity=4 * 1024 * 1024):
    """Keep the active and previous diagnostics file within a fixed byte budget."""
    path = Path(path)
    raw = canonical_bytes(row) + b'\n'
    if len(raw) > capacity:
        return
    if path.exists() and path.stat().st_size + len(raw) > capacity:
        path.replace(path.with_suffix('.previous.jsonl'))
    with path.open('ab') as stream:
        stream.write(raw)
