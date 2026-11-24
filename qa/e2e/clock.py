"""Deterministic virtual clock emitting paired OT_TIME/2 samples.

Never monkey-patches OS wall or monotonic clocks. OS pipe/process timeouts
continue to use real ``time.monotonic()``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from online_trader.scheduling.clock import ClockSample, TIME_SEMANTICS, utc_text


class VirtualClockError(ValueError):
    """Raised when virtual business time moves invalidly."""


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise VirtualClockError("Virtual clock datetime must be timezone-aware")
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        raise VirtualClockError("Virtual clock timestamp must be an ISO UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VirtualClockError(f"Invalid virtual clock timestamp: {value}") from error
    if parsed.utcoffset() is None:
        raise VirtualClockError("Virtual clock timestamp must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


class VirtualClock:
    """Controllable Exchange/Strategy business clock separate from OS monotonic."""

    def __init__(
        self,
        initial_utc: str | datetime,
        *,
        initial_monotonic_ms: int = 1_000,
        clock_epoch: str | None = None,
        reference_status: str = "OBSERVED_OK",
    ) -> None:
        if type(initial_monotonic_ms) is not int or initial_monotonic_ms < 0:
            raise VirtualClockError("Initial monotonic milliseconds must be a nonnegative integer")
        self._utc = _parse_utc(initial_utc)
        self._monotonic_ns = initial_monotonic_ms * 1_000_000
        self.epoch = clock_epoch or uuid4().hex
        self.reference_status = reference_status
        self.continuous_samples = 0

    @property
    def now_utc(self) -> str:
        return utc_text(self._utc)

    @property
    def monotonic_ns(self) -> int:
        return self._monotonic_ns

    @property
    def monotonic_ms(self) -> int:
        return self._monotonic_ns // 1_000_000

    def advance(
        self,
        *,
        days: int = 0,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
        milliseconds: int = 0,
    ) -> ClockSample:
        delta = timedelta(
            days=days,
            hours=hours,
            minutes=minutes,
            seconds=seconds,
            milliseconds=milliseconds,
        )
        delta_ms = int(delta.total_seconds() * 1_000)
        if delta_ms <= 0:
            raise VirtualClockError("Virtual clock advance delta must be positive")
        self._utc += delta
        self._monotonic_ns += delta_ms * 1_000_000
        return self.sample()

    def advance_to(
        self,
        target_utc: str | datetime,
        *,
        monotonic_delta_ms: int | None = None,
    ) -> ClockSample:
        target = _parse_utc(target_utc)
        wall_delta_ms = int((target - self._utc).total_seconds() * 1_000)
        if wall_delta_ms <= 0 and monotonic_delta_ms is None:
            raise VirtualClockError("advance_to requires target_utc strictly after current time")
        step_ms = wall_delta_ms if monotonic_delta_ms is None else monotonic_delta_ms
        if type(step_ms) is not int or step_ms <= 0:
            raise VirtualClockError("Virtual monotonic step must be a positive integer")
        self._utc = target
        self._monotonic_ns += step_ms * 1_000_000
        return self.sample()

    def sample(self) -> ClockSample:
        return ClockSample(
            now_utc=self.now_utc,
            monotonic_ns=str(self._monotonic_ns),
            clock_epoch=self.epoch,
            sample_span_ns="0",
        )

    def frame_fields(self, *, health_reason: str | None = None) -> dict[str, object]:
        """Emit consistent OT_TIME/2 fields consumed by DsvSession._validate_frame."""
        self.continuous_samples = 0 if health_reason else self.continuous_samples + 1
        current = self.sample()
        return {
            "time_semantics_version": TIME_SEMANTICS,
            "time_sample": current.paired_record(),
            "decision_utc": current.now_utc,
            "wall_time": current.now_utc,
            "authorization_time": current.now_utc,
            "monotonic_ms": self.monotonic_ms,
            "clock_health": {
                "reason": health_reason,
                "utc_error_ns": None,
                "reference_status": self.reference_status,
                "continuous_samples": self.continuous_samples,
            },
        }
