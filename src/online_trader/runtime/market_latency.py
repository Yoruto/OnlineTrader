"""Bounded F5 latency evidence for original SDK Tick identities."""
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import native_integer
from online_trader.scheduling.clock import delta_ns, utc_text

_WINDOW_NS = 15 * 60 * 1_000_000_000
_LOCAL_BUDGET_NS = 100_000_000
_BACKLOG_NS = 1_000_000_000
_TRACK_NS = 5_000_000_000
_PENDING_LIMIT = 4096
_NEW_SLOTS_PER_UPDATE = 512
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_BUCKET_MS = (1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 2000, 5000)
_BUCKET_NS = tuple(value * 1_000_000 for value in _BUCKET_MS)


def p99_budget_result(total, timely, late, *, coverage_proved):
    """Classify the nearest-rank P99 without treating unknowns as fast."""
    if not coverage_proved or total < 100:
        return 'NOT_PROVED'
    rank = (99 * total + 99) // 100
    if timely >= rank:
        return 'PASS'
    if total - late < rank:
        return 'FAIL'
    return 'NOT_PROVED'


def _serial_integer(value, *, minimum=0):
    scalar = value.item() if hasattr(value, 'item') else value
    return native_integer(scalar, minimum=minimum)


@dataclass(slots=True)
class _Window:
    scope: tuple
    bucket: int | None
    opens_utc: str
    closes_utc: str
    seen: int = 0
    pending: int = 0
    timely: int = 0
    late: int = 0
    unknown: int = 0
    coverage_losses: int = 0
    outside_session: int = 0
    first_poll_ns: int | None = None
    last_poll_ns: int | None = None
    first_poll_utc: str | None = None
    last_poll_utc: str | None = None
    last_tick_poll_ns: int | None = None
    poll_count: int = 0
    timeout_count: int = 0
    max_poll_gap_ns: int = 0
    max_without_tick_ns: int = 0
    max_measured_ns: int = 0
    max_wait_evidence_ns: int = 0
    histogram: list = field(default_factory=lambda: [0] * (len(_BUCKET_NS) + 1))

    def observe_poll(self, returned_ns, returned_utc, updated):
        if self.first_poll_ns is None:
            self.first_poll_ns = returned_ns
            self.first_poll_utc = returned_utc
            self.last_tick_poll_ns = returned_ns
        if self.last_poll_ns is not None:
            self.max_poll_gap_ns = max(self.max_poll_gap_ns, returned_ns - self.last_poll_ns)
        self.last_poll_ns = returned_ns
        self.last_poll_utc = returned_utc
        self.poll_count += 1
        self.timeout_count += not updated
        self.max_without_tick_ns = max(self.max_without_tick_ns, returned_ns - self.last_tick_poll_ns)

    def admit(self, returned_ns):
        self.seen += 1
        self.pending += 1
        self.last_tick_poll_ns = returned_ns

    def finish(self, measured_ns, lower_bound_ns=0):
        self.pending -= 1
        observed_wait = lower_bound_ns if measured_ns is None else measured_ns
        self.max_wait_evidence_ns = max(self.max_wait_evidence_ns, observed_wait)
        if measured_ns is None:
            if lower_bound_ns > _LOCAL_BUDGET_NS:
                self.late += 1
            else:
                self.unknown += 1
            return
        if measured_ns <= _LOCAL_BUDGET_NS:
            self.timely += 1
        else:
            self.late += 1
        self.max_measured_ns = max(self.max_measured_ns, measured_ns)
        self.histogram[bisect_left(_BUCKET_NS, measured_ns)] += 1

    def report(self):
        code, epoch, day, session = self.scope
        result = p99_budget_result(self.seen, self.timely, self.late, coverage_proved=not self.coverage_losses)
        return {
            'contract_id': code, 'source_epoch': epoch, 'trading_day': day, 'session_id': session,
            'first_seen_window': None if self.bucket is None else str(self.bucket),
            'window_seconds': None if self.bucket is None else 900,
            'observed_candidates': str(self.seen), 'coverage_losses': str(self.coverage_losses),
            'excluded_outside_session': str(self.outside_session),
            'session_open_utc': self.opens_utc, 'session_close_utc': self.closes_utc,
            'first_observed_poll_ns': str(self.first_poll_ns), 'last_observed_poll_ns': str(self.last_poll_ns),
            'first_observed_poll_utc': self.first_poll_utc, 'last_observed_poll_utc': self.last_poll_utc,
            'sdk_poll_count': str(self.poll_count), 'sdk_timeout_count': str(self.timeout_count),
            'longest_sdk_poll_gap_ns': str(self.max_poll_gap_ns),
            'longest_interval_without_observed_tick_ns': str(self.max_without_tick_ns),
            'm2_budget_ms': 100, 'm2_timely': str(self.timely), 'm2_late': str(self.late),
            'm2_unknown': str(self.unknown + self.pending), 'm2_p99_result': result,
            'm2_max_measured_ns': str(self.max_measured_ns) if any(self.histogram) else None,
            'm2_max_wait_lower_bound_ns': str(self.max_wait_evidence_ns) if self.seen else None,
            'm2_histogram_upper_ms': list(_BUCKET_MS) + [None],
            'm2_histogram_counts': [str(value) for value in self.histogram],
            'm1_result': 'NOT_PROVED', 'm1_reason': 'INDEPENDENT_SOURCE_AND_UTC_PROOF_REQUIRED',
        }


@dataclass(frozen=True, slots=True)
class _Arrival:
    monotonic_ns: int
    native_time_ns: int
    windows: tuple


class MarketLatency:
    """Opt-in pre-filter counts and matched local times, owned by the F5 loop."""

    def __init__(self, trace, calendar):
        self.trace, self.calendar = trace, calendar
        self._frontiers = {}
        self._active = {}
        self._finished = []
        self._pending = OrderedDict()
        self._contract_pending = {}
        self._backlog_since = {}
        self._lost_backlog = set()
        self._unproved_contracts = set()

    def _window(self, scope, bucket, kind, session):
        key = scope[0], kind
        current = self._active.get(key)
        if current is not None and (current.scope != scope or current.bucket != bucket):
            self._finished.append(current)
            current = None
        if current is None:
            current = self._active[key] = _Window(scope, bucket, session['open_utc'], session['close_utc'])
        return current

    def _record_poll(self, code, epoch, utc, returned_ns, *, updated, unavailable):
        observed_utc = utc_text(utc)
        at = self.calendar.query(code, observed_utc)
        if not at['complete']:
            self.incomplete(code)
            self.trace.record('SDK_TICK_COVERAGE', contract_id=code, source_epoch=epoch,
                              reason='CALENDAR_COVERAGE_UNKNOWN', state=(epoch, 'CALENDAR_UNKNOWN'))
            return None
        if not at['is_trading']:
            for kind in ('session', 'interval'):
                window = self._active.pop((code, kind), None)
                if window is not None:
                    self._finished.append(window)
            return None
        session = at['session']
        scope = code, epoch, at['trading_day'], session['session_id']
        bucket = delta_ns(utc, _EPOCH) // _WINDOW_NS
        windows = (self._window(scope, None, 'session', session),
                   self._window(scope, bucket, 'interval', session))
        for window in windows:
            window.observe_poll(returned_ns, observed_utc, updated)
        if unavailable is not None:
            self._coverage_gap(windows, unavailable)
        if code in self._unproved_contracts:
            self._unproved_contracts.remove(code)
            self._coverage_gap(windows, 'SDK_TICK_OBSERVATION_INTERRUPTED')
        return windows

    def observe(self, code, epoch, frame, utc, returned_ns, *, live, updated, unavailable=None):
        windows = self._record_poll(code, epoch, utc, returned_ns, updated=updated,
                                    unavailable=unavailable) if live else None
        if not updated or frame is None:
            return
        ids, stamps = frame['id'], frame['datetime']
        if len(ids) == 0:
            if windows:
                self._coverage_gap(windows, 'SDK_TICK_SERIAL_EMPTY')
            return
        last = _serial_integer(ids.iat[-1])
        last_stamp = _serial_integer(stamps.iat[-1])
        previous = self._frontiers.get(code)
        boundary = epoch, id(frame), last, last_stamp
        self._frontiers[code] = boundary
        if not windows or unavailable is not None:
            return
        if previous is None or previous[:2] != boundary[:2] or last < previous[2]:
            self._coverage_gap(windows, 'SDK_TICK_FRONTIER_RESET')
            return
        increase = last - previous[2]
        if increase == 0:
            if last_stamp != previous[3]:
                self._coverage_gap(windows, 'SDK_TICK_TIMESTAMP_REWRITTEN')
            return
        if previous[3] and last_stamp < previous[3]:
            self._coverage_gap(windows, 'SDK_TICK_TIMESTAMP_REGRESSED')
        session_window = windows[0]
        opens_ns = delta_ns(datetime.fromisoformat(session_window.opens_utc), _EPOCH)
        closes_ns = delta_ns(datetime.fromisoformat(session_window.closes_utc), _EPOCH)
        first_index = max(0, len(ids) - min(increase, _NEW_SLOTS_PER_UPDATE))
        if increase > len(ids):
            self._coverage_gap(windows, 'SDK_TICK_WINDOW_OVERFLOW')
        if min(increase, len(ids)) > _NEW_SLOTS_PER_UPDATE:
            self._coverage_gap(windows, 'SDK_TICK_DIAGNOSTIC_BUDGET')
        # The pinned SDK projects one slot for each ID, including zero-stamp
        # placeholders. The visible slot is not a tradeable market fact.
        expected = last - len(ids) + 1 + first_index
        self.trace.record('SDK_TICK_BATCH', contract_id=code, source_epoch=epoch,
                          trading_day=session_window.scope[2], session_id=session_window.scope[3],
                          first_tick_id=str(expected), last_tick_id=str(last),
                          sdk_first_seen_monotonic_ns=str(returned_ns), observed_utc=utc_text(utc))
        for index in range(first_index, len(ids)):
            try:
                tick_id = _serial_integer(ids.iat[index])
                source_ns = _serial_integer(stamps.iat[index], minimum=1)
                if tick_id != expected:
                    raise ValueError('Unexpected native serial slot')
            except (TraderError, ValueError):
                self._coverage_gap(windows, 'SDK_TICK_SLOT_UNPROVED')
            else:
                if opens_ns <= source_ns < closes_ns:
                    self._admit((code, epoch, tick_id), source_ns, returned_ns, windows)
                else:
                    for window in windows:
                        window.outside_session += 1
            expected += 1

    def incomplete(self, code):
        self._unproved_contracts.add(code)
        for kind in ('session', 'interval'):
            window = self._active.get((code, kind))
            if window is not None:
                window.coverage_losses += 1

    def current_wait_ns(self, code, now_ns):
        pending = self._contract_pending.get(code)
        if not pending:
            return None
        oldest = next(iter(pending.values()))
        wait = now_ns - oldest
        return wait if wait >= _BACKLOG_NS else None

    def _coverage_gap(self, windows, reason):
        for window in windows:
            window.coverage_losses += 1
        scope = windows[0].scope
        self.trace.record('SDK_TICK_COVERAGE', contract_id=scope[0], source_epoch=scope[1],
                          trading_day=scope[2], session_id=scope[3], reason=reason,
                          state=(scope, windows[1].bucket, reason))

    def _admit(self, key, source_ns, received_ns, windows):
        if key in self._pending:
            return
        if len(self._pending) >= _PENDING_LIMIT:
            oldest = next(iter(self._pending))
            if oldest[0] in self._backlog_since:
                self._lost_backlog.add(oldest[0])
            self._finish(oldest, received_ns)
        for window in windows:
            window.admit(received_ns)
        self._pending[key] = _Arrival(received_ns, source_ns, windows)
        self._contract_pending.setdefault(key[0], OrderedDict())[key] = received_ns

    def _finish(self, key, now_ns, *, measured_ns=None):
        arrival = self._pending.pop(key)
        code = key[0]
        pending = self._contract_pending[code]
        pending.pop(key)
        lower_bound = now_ns - arrival.monotonic_ns
        if not pending:
            self._contract_pending.pop(code)
        for window in arrival.windows:
            window.finish(measured_ns, lower_bound_ns=lower_bound)

    def strategy_start(self, event, now_ns):
        try:
            code = event['contract_id']
            key = code, event['source_epoch'], int(event['native_tick_id'])
            source_ns = int(event['native_time_ns'])
        except (KeyError, TypeError, ValueError):
            return None
        arrival = self._pending.get(key)
        if arrival is None:
            return None
        _, _, day, session = arrival.windows[0].scope
        if (source_ns != arrival.native_time_ns or day != event['trading_day']
                or session != event['session_id'] or now_ns < arrival.monotonic_ns):
            return None
        elapsed = now_ns - arrival.monotonic_ns
        self._note_backlog(code, arrival.monotonic_ns, now_ns)
        self._finish(key, now_ns, measured_ns=elapsed)
        return {'sdk_first_seen_monotonic_ns': str(arrival.monotonic_ns), 'm2_ns': str(elapsed),
                'first_seen_window': str(arrival.windows[1].bucket)}

    def _note_backlog(self, code, oldest_ns, now_ns):
        if now_ns - oldest_ns >= _BACKLOG_NS and code not in self._backlog_since:
            self._backlog_since[code] = oldest_ns + _BACKLOG_NS
            self.trace.record('LOCAL_MARKET_BACKLOG', contract_id=code,
                              pending_count=len(self._contract_pending[code]), oldest_wait_ns=now_ns - oldest_ns)

    def advance(self, now_ns, *, finish=False):
        for code, pending in self._contract_pending.items():
            self._note_backlog(code, next(iter(pending.values())), now_ns)
        expired = self._lost_backlog
        while self._pending:
            key = next(iter(self._pending))
            if not finish and now_ns - self._pending[key].monotonic_ns < _TRACK_NS:
                break
            expired.add(key[0])
            self._finish(key, now_ns)
        for code, started in tuple(self._backlog_since.items()):
            pending = self._contract_pending.get(code)
            if pending and now_ns - next(iter(pending.values())) >= _BACKLOG_NS:
                continue
            del self._backlog_since[code]
            reason = ('OBSERVATION_ENDED' if finish else
                      'UNCONSUMED_OBSERVATION_EXPIRED' if code in expired else 'OBSERVED_DRAIN')
            self.trace.record('LOCAL_MARKET_BACKLOG_ENDED', contract_id=code, elapsed_ns=now_ns - started,
                              reason=reason)
        self._lost_backlog.intersection_update(self._backlog_since)
        if finish:
            self._finished.extend(self._active.values())
            self._active.clear()
        reports = [window.report() for window in self._finished if not window.pending]
        self._finished = [window for window in self._finished if window.pending]
        return reports
