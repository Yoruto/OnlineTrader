"""Bounded, opt-in evidence for the legacy SDK quote and execution path."""
from collections import deque
from datetime import datetime, timezone
from queue import Empty, Full, Queue
from threading import Event, Thread
import time

from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import utc
from online_trader.gateway.tqkq import quote_book
from online_trader.runtime.market_latency import MarketLatency
from online_trader.runtime.market_monitor import append_latency
from online_trader.scheduling.clock import quote_timestamp, source_age_ns
from online_trader.strategy.entry import CHINA_TIME


def _raw_time(quote):
    raw = getattr(quote, 'datetime', None)
    return raw[:64] if isinstance(raw, str) else None


class QuoteTrace:
    def __init__(self, path, boot_id, calendar):
        self.path = path
        self.boot_id = boot_id
        self.market = MarketLatency(self, calendar)
        self.live_poll = False
        self._outbox = Queue(maxsize=8)
        self._closing = Event()
        self._write_failures = 0
        self._queue_drops = 0
        self._writer = Thread(target=self._write_rows, name='OnlineTrader-quote-trace', daemon=True)
        try:
            self._writer.start()
        except RuntimeError:
            self._writer = None
            self._write_failures += 1
        self._pending = deque(maxlen=512)
        self._dropped = 0
        self._last_state = {}
        self._last_sdk_return_ns = None
        self._sdk_window = {}
        self._next_write_ns = 0
        self._summary_batch = 0
        self._trace_batch = 0

    @property
    def write_failed(self):
        return bool(self._write_failures or self._queue_drops)

    def enqueue(self, path, row):
        if self._writer is None or self._closing.is_set():
            self._queue_drops += 1
            return False
        try:
            self._outbox.put_nowait((path, row))
        except Full:
            self._queue_drops += 1
            return False
        return True

    def _write_rows(self):
        while not self._closing.is_set() or not self._outbox.empty():
            try:
                path, row = self._outbox.get(timeout=.1)
            except Empty:
                continue
            try:
                if not append_latency(path, row):
                    self._write_failures += 1
            except (OSError, TraderError, ValueError, TypeError):
                self._write_failures += 1

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

    def sdk_wait(self, started_ns, returned_ns, updated, *, wait_mode=None):
        window = self._sdk_window
        window['mode'] = wait_mode
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
            book = quote_book(quote)
            expired = getattr(quote, 'expired', None) is True
            state = source_epoch, fault, stamp, expired, tuple(book.values())
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
                        expired=expired, book=book)

    def sdk_clock(self, sample, clock):
        if sample is None or self._last_sdk_return_ns is None:
            return
        # Keep the clock pair distinct from the exact wait_update return.
        health = getattr(clock, 'health', None)
        try:
            reference = health(sample) if callable(health) else {}
        except (TypeError, ValueError, TraderError):
            reference = {}
        estimate = reference.get('reference') or {}
        batch = reference.get('reference_batch_monotonic_ns')
        state = (sample.clock_epoch, reference.get('reference_status'), reference.get('protection_ready'), batch)
        if self._last_state.get(('SDK_CLOCK_SAMPLE', None)) == state:
            return
        details = clock.record() if hasattr(clock, 'record') else {}
        self.record('SDK_CLOCK_SAMPLE', state=state, reference_batch_monotonic_ns=batch,
                    reference_samples=details.get('reference_samples', []),
                    sdk_return_monotonic_ns=str(self._last_sdk_return_ns), **sample.paired_record(),
                    reference_status=reference.get('reference_status'),
                    estimated_utc_error_ns=reference.get('utc_error_ns'),
                    reference_offset_ns=estimate.get('offset_ns'),
                    reference_estimate_ns=estimate.get('estimated_error_ns'))

    def sdk_ticks(self, code, epoch, frame, utc, *, live):
        if self._last_sdk_return_ns is None:
            return
        try:
            self.market.observe(code, epoch, frame, utc, self._last_sdk_return_ns, live=live)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, TraderError) as error:
            if live:
                self.market.incomplete(code)
            self.record('SDK_TICK_COVERAGE', contract_id=code, source_epoch=epoch,
                        reason='SDK_TICK_OBSERVATION_FAILED', error_code=getattr(error, 'code', type(error).__name__))

    def strategy_start(self, event, now_ns, clock):
        timing = self.market.strategy_start(event, now_ns)
        capture = getattr(clock, 'capture_live', None)
        try:
            clock_evidence = capture().paired_record() if callable(capture) else None
        except (AttributeError, TypeError, ValueError, TraderError):
            clock_evidence = None
        self.record('STRATEGY_START', contract_id=event.get('contract_id'), trading_day=event.get('trading_day'),
                    session_id=event.get('session_id'), source_epoch=event.get('source_epoch'),
                    native_tick_id=event.get('native_tick_id'), native_time_ns=event.get('native_time_ns'),
                    strategy_start_monotonic_ns=str(now_ns), application_clock=clock_evidence,
                    **(timing or {}))

    def flush(self, *, phase, timings=None, force=False, finish=False):
        now_ns = time.monotonic_ns()
        reports = self.market.advance(now_ns, finish=finish)
        if reports or finish:
            self._summary_batch += 1
            self.enqueue(self.path.with_name('market-latency-summary.jsonl'), {
                'schema_version': 'DSV_MARKET_LATENCY/1', 'kernel_boot_id': self.boot_id,
                'batch_number': str(self._summary_batch),
                'monotonic_ns': str(now_ns), 'collection_ended': finish,
                'previous_diagnostic_failures': str(self._write_failures),
                'previous_unqueued_batches': str(self._queue_drops), 'windows': reports})
        if not force and now_ns < self._next_write_ns:
            return
        self._next_write_ns = now_ns + 1_000_000_000
        if not self._pending and not self._sdk_window:
            return
        self._trace_batch += 1
        row = {'schema_version': 'DSV_QUOTE_TRACE/2', 'kernel_boot_id': self.boot_id,
               'batch_number': str(self._trace_batch),
               'windows_utc': utc(datetime.now(timezone.utc)), 'monotonic_ns': str(now_ns),
               'phase': phase, 'owner_timings_ns': {name: str(duration) for name, duration in (timings or {}).items()},
               # The existing JSONL writer uses OT-CJ-1. Its integer fields are 31-bit.
               'sdk_wait': {name: str(value) if name.endswith('_ns') else value
                            for name, value in self._sdk_window.items()},
               'dropped_records': str(self._dropped), 'records': list(self._pending),
               'previous_write_failures': str(self._write_failures),
               'previous_unqueued_batches': str(self._queue_drops)}
        if self.enqueue(self.path, row):
            self._pending.clear()
            self._sdk_window.clear()
            self._dropped = 0

    def close(self):
        if self._closing.is_set():
            return
        self.record('SESSION_CLOSE')
        self.flush(phase='SESSION_CLOSE', force=True, finish=True)
        self._closing.set()
        if self._writer is not None:
            # Evidence is optional. A blocked filesystem must not delay exit.
            self._writer.join(timeout=.25)
