"""Separate UTC calendar location from exact monotonic execution duration.

The guard reports and latches discontinuities. Resetting this local measurement
does not reconcile a position, clear a market gap or grant trading authority.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time

from online_trader.errors import TraderError


MAX_MONOTONIC_NS = 9_223_372_036_854_775_807
MAX_WALL_DRIFT_MS = 2_147_483_647
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")
_NS = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")


def _error(message):
    raise TraderError("E_CLOCK_INPUT", message)


def _milliseconds_text(nanoseconds):
    """Canonical decimal milliseconds from integer nanoseconds, without float."""
    sign = "-" if nanoseconds < 0 else ""
    whole, remainder = divmod(abs(nanoseconds), 1_000_000)
    if not remainder:
        return sign + str(whole) if whole else "0"
    return sign + str(whole) + "." + f"{remainder:06d}".rstrip("0")


def _wall_delta_ns(later, earlier):
    delta = later - earlier
    microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return microseconds * 1000


@dataclass(frozen=True, slots=True)
class ClockSample:
    now_utc: str
    monotonic_ns: str

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

    @classmethod
    def from_record(cls, record):
        if type(record) is not dict or set(record) != {"now_utc", "monotonic_ns"}:
            _error("Missing or unsupported clock sample fields")
        return cls(record["now_utc"], record["monotonic_ns"])

    def to_record(self):
        return {"now_utc": self.now_utc, "monotonic_ns": self.monotonic_ns}

    @property
    def utc(self):
        return datetime.fromisoformat(self.now_utc[:-1] + "+00:00")

    @property
    def monotonic(self):
        return int(self.monotonic_ns)


class SystemClock:
    """Read local clocks only; there is no timer, network or clock adjustment."""

    def sample(self):
        utc = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return ClockSample(utc, str(time.monotonic_ns())).to_record()


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
        wall_ns = _wall_delta_ns(sample.utc, self._accepted.utc)
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
