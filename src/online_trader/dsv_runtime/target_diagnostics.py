"""Bounded, non-blocking Target Owner market latency and loop diagnostics."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from queue import Empty, Full, Queue
from pathlib import Path
from threading import Event, Thread
import time

from online_trader.errors import TraderError
from online_trader.runtime.market_monitor import append_latency


_SUMMARY_INTERVAL_NS = 1_000_000_000
_ANOMALY_COOLDOWN_NS = 10_000_000_000
_SILENCE_ANOMALY_NS = 3_000_000_000
_PUMP_GAP_ANOMALY_NS = 1_000_000_000
_OWNER_LOOP_ANOMALY_NS = 500_000_000
_RING_MAXLEN = 256
_ANOMALY_TAIL = 64


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _int_or_none(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ms_max(current: int | None, candidate_ns: int | None) -> int | None:
    if candidate_ns is None:
        return current
    candidate_ms = max(0, candidate_ns) // 1_000_000
    return candidate_ms if current is None else max(current, candidate_ms)


def _symbol_diag_row(code: str, market: dict | None, observation: dict | None) -> dict:
    market = market or {}
    observation = observation or {}
    return {
        'contract_id': code,
        'active': market.get('market_session_active') is True,
        'source_time_raw': observation.get('source_time_raw'),
        'source_age_raw_ns': observation.get('source_age_raw_ns'),
        'silence_ns': observation.get('silence_ns'),
        'age_quality': observation.get('age_quality'),
        'reason': observation.get('reason'),
    }


class TargetMarketDiagnostics:
    """Collect 1s summaries and a 60s anomaly black-box without blocking the Owner."""

    def __init__(self, output_dir, *, boot_id: str, sdk_version: str):
        self.output_dir = Path(output_dir)
        self.summary_path = self.output_dir / 'target-market-summary.jsonl'
        self.anomaly_path = self.output_dir / 'target-market-anomaly.jsonl'
        self.boot_id = str(boot_id)
        self.sdk_version = str(sdk_version)
        self._queue: Queue = Queue(maxsize=64)
        self._closing = Event()
        self._queue_drops = 0
        self._write_failures = 0
        self._ring: deque[dict] = deque(maxlen=_RING_MAXLEN)
        self._window = self._empty_window()
        self._window_started_ns = time.monotonic_ns()
        self._last_anomaly_ns: dict[str, int] = {}
        self._last_source_epoch: str | None = None
        self._last_synchronized: bool | None = None
        self._writer = Thread(
            target=self._write_loop,
            name='OnlineTrader-target-market-diag',
            daemon=True,
        )
        try:
            self._writer.start()
        except RuntimeError:
            self._writer = None
            self._write_failures += 1

    @staticmethod
    def _empty_window() -> dict:
        return {
            'sdk_polls': 0,
            'sdk_updates': 0,
            'sdk_timeouts': 0,
            'sdk_errors': 0,
            'sdk_wait_max_ms': None,
            'sdk_pump_gap_max_ms': None,
            'owner_loop_max_ms': None,
            'native_facts_max_ms': None,
            'transaction_max_ms': None,
            'dispatch_max_ms': None,
            'projection_prepare_max_ms': None,
            'projection_encode_max_ms': None,
            'publication_age_ms': None,
            'active_symbols': 0,
            'fresh_symbols': 0,
            'max_source_age_ms': None,
            'source_epoch': None,
            'clock_state': 'UNOBSERVED',
            'clock_reason': None,
        }

    def _enqueue(self, path: Path, row: dict) -> bool:
        if self._writer is None or self._closing.is_set():
            self._queue_drops += 1
            return False
        try:
            self._queue.put_nowait((path, row))
            return True
        except Full:
            self._queue_drops += 1
            return False

    def _write_loop(self) -> None:
        while not self._closing.is_set() or not self._queue.empty():
            try:
                path, row = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not append_latency(path, row):
                    self._write_failures += 1
            except (OSError, TraderError, TypeError, ValueError):
                self._write_failures += 1

    def observe_tick(
        self,
        *,
        owner,
        owner_loop_ns: int,
        synchronized: bool,
        disconnected: bool = False,
        error_code: str | None = None,
        now_ns: int | None = None,
    ) -> None:
        """Record one Owner tick sample and emit bounded summaries/anomalies."""
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        adapter = getattr(owner, 'adapter', None)
        metrics = dict(getattr(adapter, 'metrics', None) or {})
        clock_health = dict(getattr(adapter, '_health', None) or {})
        dispatch_market = getattr(adapter, 'dispatch_market', None) or {}
        pub_status = getattr(owner, 'publication_status', None) or {}

        source_epoch = metrics.get('source_epoch')
        if source_epoch is None and getattr(owner, 'connection', None) is not None:
            source_epoch = getattr(owner.connection, 'source_epoch', None)
        epoch_text = None if source_epoch is None else str(source_epoch)

        sdk_updated = metrics.get('sdk_updated')
        sdk_pump_ns = _int_or_none(metrics.get('sdk_pump_ns'))
        sdk_pump_gap_ns = _int_or_none(metrics.get('sdk_pump_gap_ns'))
        native_facts_ns = _int_or_none(metrics.get('native_facts_ns'))
        transaction_ns = _int_or_none(metrics.get('transaction_ns'))
        dispatch_ns = _int_or_none(metrics.get('dispatch_ns'))
        prepare_ns = _int_or_none(pub_status.get('last_prepare_ns'))
        encode_ns = _int_or_none(pub_status.get('last_encode_ns'))
        last_pub_ns = _int_or_none(pub_status.get('last_success_monotonic_ns'))
        pub_age_ms = None if last_pub_ns is None else max(0, now_ns - last_pub_ns) // 1_000_000
        pub_error = pub_status.get('error_code')

        symbol_rows = []
        active_count = 0
        fresh_count = 0
        max_source_age_ns = None
        all_active_silent = False
        active_silences = []
        for code, market in dispatch_market.items():
            obs = market.get('quote_observation') or {}
            row = _symbol_diag_row(code, market, obs)
            symbol_rows.append(row)
            if row['active']:
                active_count += 1
                if market.get('ticks') and row['age_quality'] == 'VALID':
                    fresh_count += 1
                age_ns = _int_or_none(row['source_age_raw_ns'])
                if age_ns is not None and age_ns >= 0:
                    max_source_age_ns = age_ns if max_source_age_ns is None else max(max_source_age_ns, age_ns)
                silence = _int_or_none(row['silence_ns'])
                if silence is None:
                    silence = age_ns
                active_silences.append(silence)

        if active_count >= 2:
            all_active_silent = all(s is not None and s >= _SILENCE_ANOMALY_NS for s in active_silences)
        elif active_count == 1 and sdk_updated is False:
            all_active_silent = bool(active_silences and active_silences[0] is not None
                                     and active_silences[0] >= _SILENCE_ANOMALY_NS)

        sample = {
            'monotonic_ns': str(now_ns),
            'observed_at_utc': _utc_now(),
            'source_epoch': epoch_text,
            'synchronized': bool(synchronized),
            'disconnected': bool(disconnected),
            'error_code': error_code,
            'sdk_updated': sdk_updated,
            'sdk_pump_ns': None if sdk_pump_ns is None else str(sdk_pump_ns),
            'sdk_pump_gap_ns': None if sdk_pump_gap_ns is None else str(sdk_pump_gap_ns),
            'owner_loop_ns': str(max(0, owner_loop_ns)),
            'native_facts_ns': None if native_facts_ns is None else str(native_facts_ns),
            'transaction_ns': None if transaction_ns is None else str(transaction_ns),
            'dispatch_ns': None if dispatch_ns is None else str(dispatch_ns),
            'projection_prepare_ns': None if prepare_ns is None else str(prepare_ns),
            'projection_encode_ns': None if encode_ns is None else str(encode_ns),
            'publication_age_ms': pub_age_ms,
            'publication_error': pub_error,
            'clock_state': clock_health.get('reference_status') or 'UNOBSERVED',
            'clock_reason': clock_health.get('reason'),
            'utc_error_ns': clock_health.get('utc_error_ns'),
            'active_symbols': active_count,
            'fresh_symbols': fresh_count,
            'symbols': symbol_rows,
        }
        self._ring.append(sample)

        w = self._window
        w['sdk_polls'] += 1
        if disconnected or error_code:
            w['sdk_errors'] += 1
        elif sdk_updated is True:
            w['sdk_updates'] += 1
        elif sdk_updated is False:
            w['sdk_timeouts'] += 1
        w['sdk_wait_max_ms'] = _ms_max(w['sdk_wait_max_ms'], sdk_pump_ns)
        w['sdk_pump_gap_max_ms'] = _ms_max(w['sdk_pump_gap_max_ms'], sdk_pump_gap_ns)
        w['owner_loop_max_ms'] = _ms_max(w['owner_loop_max_ms'], owner_loop_ns)
        w['native_facts_max_ms'] = _ms_max(w['native_facts_max_ms'], native_facts_ns)
        w['transaction_max_ms'] = _ms_max(w['transaction_max_ms'], transaction_ns)
        w['dispatch_max_ms'] = _ms_max(w['dispatch_max_ms'], dispatch_ns)
        w['projection_prepare_max_ms'] = _ms_max(w['projection_prepare_max_ms'], prepare_ns)
        w['projection_encode_max_ms'] = _ms_max(w['projection_encode_max_ms'], encode_ns)
        w['publication_age_ms'] = pub_age_ms
        w['active_symbols'] = active_count
        w['fresh_symbols'] = fresh_count
        w['max_source_age_ms'] = _ms_max(w['max_source_age_ms'], max_source_age_ns)
        w['source_epoch'] = epoch_text
        w['clock_state'] = sample['clock_state']
        w['clock_reason'] = sample['clock_reason']

        triggers = []
        if all_active_silent:
            triggers.append('ALL_ACTIVE_SYMBOLS_SILENT')
        if sdk_pump_gap_ns is not None and sdk_pump_gap_ns > _PUMP_GAP_ANOMALY_NS:
            triggers.append('SDK_PUMP_GAP')
        if owner_loop_ns > _OWNER_LOOP_ANOMALY_NS:
            triggers.append('OWNER_LOOP_SLOW')
        if sample['clock_reason'] or sample['clock_state'] == 'REFERENCE_CONFLICT':
            triggers.append('CLOCK_HEALTH_FAULT')
        if (self._last_source_epoch is not None and epoch_text != self._last_source_epoch) or (
            self._last_synchronized is True and not synchronized
        ) or disconnected:
            triggers.append('CONNECTION_STATE_CHANGE')
        if pub_error:
            triggers.append('PUBLICATION_ERROR')

        self._last_source_epoch = epoch_text
        self._last_synchronized = bool(synchronized)

        for trigger in triggers:
            last_at = self._last_anomaly_ns.get(trigger)
            if last_at is None or now_ns - last_at >= _ANOMALY_COOLDOWN_NS:
                self._last_anomaly_ns[trigger] = now_ns
                self._enqueue(self.anomaly_path, {
                    'schema_version': 'DSV_TARGET_MARKET_ANOMALY/1',
                    'runtime_boot_id': self.boot_id,
                    'sdk_version': self.sdk_version,
                    'observed_at_utc': sample['observed_at_utc'],
                    'trigger': trigger,
                    'source_epoch': epoch_text,
                    'latest_symbols': symbol_rows,
                    'recent_samples': list(self._ring)[-_ANOMALY_TAIL:],
                    'queue_drops': self._queue_drops,
                    'write_failures': self._write_failures,
                })

        if now_ns - self._window_started_ns >= _SUMMARY_INTERVAL_NS:
            self._flush_summary(now_ns)

    def _flush_summary(self, now_ns: int | None = None) -> None:
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        w = self._window
        if w['sdk_polls'] > 0:
            self._enqueue(self.summary_path, {
                'schema_version': 'DSV_TARGET_MARKET_SUMMARY/1',
                'runtime_boot_id': self.boot_id,
                'sdk_version': self.sdk_version,
                'observed_at_utc': _utc_now(),
                **w,
                'queue_drops': self._queue_drops,
                'write_failures': self._write_failures,
            })
        self._window = self._empty_window()
        self._window_started_ns = now_ns

    def close(self) -> None:
        try:
            self._flush_summary()
        except Exception:
            pass
        self._closing.set()
        if self._writer is not None:
            self._writer.join(timeout=1.5)

    def __enter__(self) -> 'TargetMarketDiagnostics':
        return self

    def __exit__(self, *_) -> None:
        self.close()
