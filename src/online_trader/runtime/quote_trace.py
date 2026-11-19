"""Bounded, opt-in evidence for the legacy SDK quote and execution path."""
from collections import deque
from datetime import datetime, timezone
import time

from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import utc
from online_trader.gateway.tqkq import number
from online_trader.runtime.market_monitor import append_latency
from online_trader.scheduling.clock import quote_timestamp, source_age_ns
from online_trader.strategy.entry import CHINA_TIME


def _raw_time(quote):
    raw = getattr(quote, 'datetime', None)
    return raw[:64] if isinstance(raw, str) else None


def _book(quote):
    return {key: number(getattr(quote, key, None)) for key in (
        'last_price', 'bid_price1', 'ask_price1', 'bid_volume1', 'ask_volume1',
        'price_tick', 'lower_limit', 'upper_limit', 'volume')}


class QuoteTrace:
    def __init__(self, path, boot_id):
        self.path = path
        self.boot_id = boot_id
        self.write_failed = False
        self._pending = deque(maxlen=512)
        self._dropped = 0
        self._last_state = {}
        self._last_sdk_return_ns = None
        self._sdk_window = {}
        self._next_write_ns = 0

    def record(self, stage, *, contract_id=None, state=None, **fields):
        if state is not None:
            key = stage, contract_id
            if self._last_state.get(key) == state:
                return
            self._last_state[key] = state
        if len(self._pending) == self._pending.maxlen:
            self._dropped += 1
        if 'timings_ns' in fields:
            fields['timings_ns'] = {name: str(duration) for name, duration in fields['timings_ns'].items()}
        scalars = {name: str(value) if type(value) is int and name.endswith(('_ns', '_tick_id')) else value
                   for name, value in fields.items()}
        self._pending.append({'stage': stage, 'monotonic_ns': str(time.monotonic_ns()),
                              'contract_id': contract_id, **scalars})

    def sdk_wait(self, started_ns, returned_ns, updated):
        window = self._sdk_window
        window['polls'] = window.get('polls', 0) + 1
        key = 'updates' if updated else 'timeouts' if updated is False else 'errors'
        window[key] = window.get(key, 0) + 1
        wait_ns = returned_ns - started_ns
        gap_ns = 0 if self._last_sdk_return_ns is None else started_ns - self._last_sdk_return_ns
        if wait_ns >= window.get('max_wait_ns', 0):
            window.update(max_wait_ns=wait_ns, max_wait_started_ns=started_ns)
        if gap_ns >= window.get('max_gap_between_polls_ns', 0):
            window.update(max_gap_between_polls_ns=gap_ns, max_gap_ended_ns=started_ns)
        window['last_return_ns'] = returned_ns
        self._last_sdk_return_ns = returned_ns

    def sdk_quotes(self, quotes, reference, source_epoch, fault):
        for code, quote in quotes.items():
            stamp = _raw_time(quote)
            state = source_epoch, fault, stamp
            if self._last_state.get(('SDK_QUOTE', code)) == state:
                continue
            try:
                source = quote_timestamp(stamp, CHINA_TIME)
                age_ns = str(source_age_ns(reference, source, stamp))
            except (TypeError, ValueError, OverflowError):
                age_ns = None
            self.record('SDK_QUOTE', contract_id=code, state=state, source_epoch=source_epoch,
                        fault=fault, raw_time=stamp, reference_utc=utc(reference),
                        windows_utc=utc(datetime.now(timezone.utc)), source_age_ns=age_ns,
                        expired=getattr(quote, 'expired', None) is True, book=_book(quote))

    def native_rejection(self, code, quote, source_epoch, check, reason, **facts):
        self.record('NATIVE_QUOTE_REJECTED', contract_id=code, source_epoch=source_epoch, check=check,
                    reason=reason, raw_time=_raw_time(quote), book=_book(quote), **facts)

    def flush(self, *, phase, timings=None, force=False):
        now_ns = time.monotonic_ns()
        if not force and now_ns < self._next_write_ns:
            return
        self._next_write_ns = now_ns + 1_000_000_000
        if not self._pending and not self._sdk_window:
            return
        row = {'schema_version': 'DSV_QUOTE_TRACE/1', 'kernel_boot_id': self.boot_id,
               'windows_utc': utc(datetime.now(timezone.utc)), 'monotonic_ns': str(now_ns),
               'phase': phase, 'owner_timings_ns': {name: str(duration) for name, duration in (timings or {}).items()},
               # The existing JSONL writer uses OT-CJ-1. Its integer fields are 31-bit.
               'sdk_wait': {name: str(value) if name.endswith('_ns') else value
                            for name, value in self._sdk_window.items()},
               'dropped_records': self._dropped, 'records': list(self._pending)}
        try:
            append_latency(self.path, row)
        except (OSError, TraderError, ValueError, TypeError):
            self.write_failed = True
            return
        self.write_failed = False
        self._pending.clear()
        self._sdk_window.clear()
        self._dropped = 0
