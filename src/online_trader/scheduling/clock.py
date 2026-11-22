"""Separate UTC calendar location from exact monotonic execution duration.

The guard reports and latches discontinuities. Resetting this local measurement
does not reconcile a position, clear a market gap or grant trading authority.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from uuid import uuid4

from online_trader.errors import TraderError


MAX_MONOTONIC_NS = 9_223_372_036_854_775_807
MAX_WALL_DRIFT_MS = 2_147_483_647
TIME_SEMANTICS = "OT_TIME/2"
NS_PER_SECOND = 1_000_000_000
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")
_NS = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_EPOCH = re.compile(r"[a-f0-9]{32}\Z")
_FRACTION = re.compile(r"[T ][0-9]{2}:[0-9]{2}:[0-9]{2}[.,]([0-9]{1,9})(?![0-9])")
_SOURCE_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.,][0-9]{1,9})?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})?\Z")


def _error(message):
    raise TraderError("E_CLOCK_INPUT", message)


def _milliseconds_text(nanoseconds):
    """Canonical decimal milliseconds from integer nanoseconds, without float."""
    sign = "-" if nanoseconds < 0 else ""
    whole, remainder = divmod(abs(nanoseconds), 1_000_000)
    if not remainder:
        return sign + str(whole) if whole else "0"
    return sign + str(whole) + "." + f"{remainder:06d}".rstrip("0")


def utc_text(at):
    if not isinstance(at, datetime) or at.utcoffset() is None:
        _error("Expected a timezone-aware wall clock")
    return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def delta_ns(later, earlier):
    delta = later - earlier
    microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return microseconds * 1000


@dataclass(frozen=True, slots=True)
class ClockSample:
    now_utc: str
    monotonic_ns: str
    clock_epoch: str | None = None
    sample_span_ns: str = "0"

    def __post_init__(self):
        if type(self.now_utc) is not str or not _UTC.fullmatch(self.now_utc):
            _error("Expected a strict UTC timestamp with at most six fractional digits")
        try:
            datetime.fromisoformat(self.now_utc[:-1] + "+00:00")
        except ValueError as error:
            raise TraderError("E_CLOCK_INPUT", "Invalid UTC calendar timestamp") from error
        if (type(self.monotonic_ns) is not str or not _NS.fullmatch(self.monotonic_ns)
                or int(self.monotonic_ns) > MAX_MONOTONIC_NS):
            _error("Expected canonical nonnegative signed-64-bit monotonic nanoseconds")
        if (self.clock_epoch is not None and
                (type(self.clock_epoch) is not str or not _EPOCH.fullmatch(self.clock_epoch))):
            _error("Expected a bounded owner clock epoch")
        if (type(self.sample_span_ns) is not str or not _NS.fullmatch(self.sample_span_ns)
                or int(self.sample_span_ns) > MAX_MONOTONIC_NS):
            _error("Expected a bounded clock capture span")

    @classmethod
    def from_record(cls, record):
        if type(record) is not dict or set(record) != {"now_utc", "monotonic_ns"}:
            _error("Missing or unsupported clock sample fields")
        return cls(record["now_utc"], record["monotonic_ns"])

    def to_record(self):
        return {"now_utc": self.now_utc, "monotonic_ns": self.monotonic_ns}

    @classmethod
    def from_paired_record(cls, record):
        if (type(record) is not dict or set(record) != {
                "now_utc", "monotonic_ns", "clock_epoch", "sample_span_ns"} or record["clock_epoch"] is None):
            _error("Missing or unsupported paired clock sample fields")
        return cls(**record)

    def paired_record(self):
        return self.to_record() | {"clock_epoch": self.clock_epoch, "sample_span_ns": self.sample_span_ns}

    @property
    def utc(self):
        return datetime.fromisoformat(self.now_utc[:-1] + "+00:00")

    @property
    def monotonic(self):
        return int(self.monotonic_ns)


class SystemClock:
    """Read local clocks only; there is no timer, network or clock adjustment."""

    def __init__(self):
        self.epoch = uuid4().hex

    @staticmethod
    def utc_now():
        return datetime.now(timezone.utc)

    @staticmethod
    def monotonic_ns():
        return time.monotonic_ns()

    def capture(self):
        before = self.monotonic_ns()
        utc = self.utc_now()
        after = self.monotonic_ns()
        return ClockSample(utc_text(utc), str((before + after) // 2), self.epoch,
                           str(after - before))

    def sample(self):
        return self.capture().to_record()


def sdk_deadline(seconds):
    """Convert only a relative wait slice at the TqSdk boundary."""
    return time.time() + seconds


def source_fraction_tail(raw):
    """Return nanoseconds below datetime's microsecond precision."""
    match = _FRACTION.search(raw)
    return int(match[1].ljust(9, "0")[6:]) if match else 0


def source_age_ns(now, source, raw):
    return delta_ns(now, source) - source_fraction_tail(raw)


def quote_timestamp(raw, implicit_zone):
    """Parse the SDK's ISO local form while keeping the untouched raw value."""
    if type(raw) is not str or not _SOURCE_TIMESTAMP.fullmatch(raw):
        raise ValueError("Invalid SDK quote timestamp")
    source = datetime.fromisoformat(raw)
    if source.tzinfo is None:
        source = source.replace(tzinfo=implicit_zone)
    return source.astimezone(timezone.utc)


def quote_age_quality(age_ns, reference=None):
    if reference and reference.get('utc_error_ns') is not None:
        observed = reference['reference']
        corrected_age = age_ns + int(observed['offset_ns'])
        if corrected_age < -max(int(observed['estimated_error_ns']), NS_PER_SECOND):
            return 'SOURCE_ANOMALY', 'QUOTE_REFERENCE_CONFLICT'
        return 'VALID', None
    return ('CLOCK_UNVERIFIED', 'QUOTE_LEADS_LOCAL_UTC') if age_ns < 0 else ('VALID', None)


class ClockContinuity:
    """Observe per-owner jumps and stalls without substituting a different UTC."""

    def __init__(self, *, drift_ns=2 * NS_PER_SECOND, gap_ns=15 * NS_PER_SECOND):
        if type(drift_ns) is not int or type(gap_ns) is not int or not 0 < drift_ns < gap_ns:
            _error("Expected positive, ordered clock continuity thresholds")
        self.drift_ns, self.gap_ns = drift_ns, gap_ns
        self.previous = None
        self.continuous_samples = 0

    def compare(self, sample):
        previous = self.previous
        if previous is None:
            return {"reason": None, "wall_delta_ns": None, "mono_delta_ns": None, "drift_ns": None}
        wall = delta_ns(sample.utc, previous.utc)
        elapsed = sample.monotonic - previous.monotonic
        drift = wall - elapsed
        tolerance = self.drift_ns + int(sample.sample_span_ns) + int(previous.sample_span_ns)
        reason = None
        if elapsed < 0 or sample.clock_epoch != previous.clock_epoch:
            reason = "MONOTONIC_DISCONTINUITY"
        elif abs(drift) > tolerance:
            reason = "WALL_CLOCK_DISCONTINUITY"
        elif elapsed > self.gap_ns:
            reason = "OWNER_SCHEDULING_GAP"
        return {"reason": reason, "wall_delta_ns": str(wall),
                "mono_delta_ns": str(elapsed), "drift_ns": str(drift)}

    def observe(self, sample):
        result = self.compare(sample)
        if result["reason"] or self.previous is None:
            self.continuous_samples = 0
        elif sample.monotonic > self.previous.monotonic:
            self.continuous_samples = min(2, self.continuous_samples + 1)
        self.previous = sample
        return result | {"continuous_samples": self.continuous_samples}


class ClockGuard:
    def __init__(self, max_wall_drift_ms=1000):
        if (type(max_wall_drift_ms) is not int
                or not 0 <= max_wall_drift_ms <= MAX_WALL_DRIFT_MS):
            _error("Expected a bounded nonnegative integer wall drift tolerance")
        self.max_wall_drift_ms = max_wall_drift_ms
        self._accepted = None
        self._fault_reasons = ()

    @property
    def faulted(self):
        return bool(self._fault_reasons)

    def _result(self, sample, *, valid, elapsed_ms, wall_ns=0, drift_ns=0, reasons=()):
        return {"valid": valid, "elapsed_ms": elapsed_ms, "reasons": list(reasons),
                **sample.to_record(), "wall_delta_ms": _milliseconds_text(wall_ns),
                "drift_ms": _milliseconds_text(drift_ns)}

    def observe(self, record):
        sample = ClockSample.from_record(record)
        if self._accepted is None:
            self._accepted = sample
            return self._result(sample, valid=True, elapsed_ms=0)
        wall_ns = delta_ns(sample.utc, self._accepted.utc)
        monotonic_ns = sample.monotonic - self._accepted.monotonic
        drift_ns = wall_ns - monotonic_ns
        if self._fault_reasons:
            return self._result(sample, valid=False, elapsed_ms=0, wall_ns=wall_ns, drift_ns=drift_ns,
                                reasons=(*self._fault_reasons, "CLOCK_FAULT_LATCHED"))
        reasons = []
        if wall_ns < 0:
            reasons.append("CLOCK_WALL_REGRESSION")
        if monotonic_ns < 0:
            reasons.append("CLOCK_MONOTONIC_REGRESSION")
        if abs(drift_ns) > self.max_wall_drift_ms * 1_000_000:
            reasons.append("CLOCK_DRIFT_EXCEEDED")
        if reasons:
            self._fault_reasons = tuple(reasons)
            return self._result(sample, valid=False, elapsed_ms=0, wall_ns=wall_ns,
                                drift_ns=drift_ns, reasons=reasons)
        self._accepted = sample
        return self._result(sample, valid=True, elapsed_ms=monotonic_ns // 1_000_000,
                            wall_ns=wall_ns, drift_ns=drift_ns)

    def reset(self, record):
        """Clear this measurement latch explicitly; this is no trading authorization."""
        sample = ClockSample.from_record(record)
        self._accepted = sample
        self._fault_reasons = ()
        return self._result(sample, valid=True, elapsed_ms=0)
