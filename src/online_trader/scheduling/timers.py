"""Owner-local deadlines. A due check is evidence for a decision, never an effect."""

from datetime import datetime
from decimal import Decimal

from online_trader.scheduling.clock import NS_PER_SECOND, delta_ns


def timer_record(kind, target, start_event, sample, seconds, *, strict=False, utc_error_ns=None):
    duration = int(Decimal(str(seconds)) * NS_PER_SECOND)
    return {"timer_id": f"{target}:{kind}", "target_id": target, "kind": kind,
            "generation": start_event, "start_event_id": start_event,
            "start_utc": sample.now_utc, "utc_error_ns": utc_error_ns,
            "start_mono_ns": sample.monotonic_ns, "duration_ns": str(duration),
            "comparison": ">" if strict else ">=", "clock_epoch": sample.clock_epoch,
            "phase": "ACTIVE"}


def elapsed_ns(timer, sample):
    if (timer is None or timer["phase"] != "ACTIVE" or timer["clock_epoch"] != sample.clock_epoch
            or timer["start_mono_ns"] is None):
        return None
    difference = sample.monotonic - int(timer["start_mono_ns"])
    return difference + int(timer.get("carried_ns", "0")) if difference >= 0 else None


def due_check(timer, sample, target):
    if timer is None or timer["target_id"] != target:
        return None
    elapsed = elapsed_ns(timer, sample)
    if elapsed is None:
        return None
    duration = int(timer["duration_ns"])
    if elapsed < duration or (elapsed == duration and timer["comparison"] == ">"):
        return None
    scheduled = sample.monotonic - (elapsed - duration)
    return {key: timer[key] for key in ("timer_id", "target_id", "kind", "generation", "start_event_id")} | {
        "scheduled_mono_ns": str(scheduled) if scheduled >= 0 else None,
        "handled_mono_ns": sample.monotonic_ns, "lateness_ns": str(elapsed - duration),
        "clock_epoch": sample.clock_epoch, "result": "DUE"}


def recover_timer(timer, sample, utc_error_ns):
    """Rebase using a conservative UTC elapsed bound after native reconciliation."""
    if (timer["clock_epoch"] == sample.clock_epoch or timer["start_utc"] is None
            or timer["phase"] not in {"ACTIVE", "RECOVERY_UNCERTAIN"}):
        return None
    elapsed = delta_ns(sample.utc, datetime.fromisoformat(timer["start_utc"]))
    old_error = timer["utc_error_ns"]
    if old_error is None or utc_error_ns is None or elapsed < 0:
        if timer["phase"] == "RECOVERY_UNCERTAIN":
            return None
        timer["phase"] = "RECOVERY_UNCERTAIN"
        return "TIMER_RECOVERY_UNCERTAIN"
    carried = max(0, elapsed - int(old_error) - int(utc_error_ns))
    timer.update(clock_epoch=sample.clock_epoch, start_mono_ns=sample.monotonic_ns, carried_ns=str(carried), phase="ACTIVE")
    return "TIMER_REBASED"


def poll_slice(timers, sample, *, maximum=0.5, minimum=0.001):
    """Shorten a bounded wait to the nearest live check, with a positive floor."""
    remaining = []
    for timer in timers:
        elapsed = elapsed_ns(timer, sample)
        if elapsed is not None:
            wait_ns = int(timer["duration_ns"]) - elapsed + (timer["comparison"] == ">")
            remaining.append(wait_ns / NS_PER_SECOND)
    return max(minimum, min(maximum, min(remaining, default=maximum)))


class RoundTimers:
    """Edit only the current copy of the owner state inside its transaction."""

    def __init__(self, sample, event_id, utc_error_ns=None):
        self.sample, self.event_id, self.utc_error_ns = sample, event_id, utc_error_ns
        self.events = []

    @staticmethod
    def get(responsibility, kind):
        return responsibility.get("timers", {}).get(kind)

    def arm(self, responsibility, kind, target, seconds, *, strict=False):
        responsibility.setdefault("timers", {})[kind] = timer_record(
            kind, target, self.event_id, self.sample, seconds, strict=strict, utc_error_ns=self.utc_error_ns)

    def elapsed(self, responsibility, kind):
        return elapsed_ns(self.get(responsibility, kind), self.sample)

    def due(self, responsibility, kind, target, *, consume=False):
        timer = self.get(responsibility, kind)
        event = due_check(timer, self.sample, target)
        if event is None:
            return False
        if consume:
            timer["phase"] = "HANDLED"
            self.events.append(event)
        return True

    def retire(self, responsibility, *kinds):
        for kind in kinds:
            timer = self.get(responsibility, kind)
            if timer and timer["phase"] == "ACTIVE":
                timer["phase"] = "INVALIDATED"
                self.events.append({"result": "INVALIDATED", "timer_id": timer["timer_id"],
                                    "generation": timer["generation"], "handled_mono_ns": self.sample.monotonic_ns})

    def missing_legacy_start(self, responsibility, kind, target, seconds, *, strict=False):
        if self.get(responsibility, kind) is not None:
            return
        self.arm(responsibility, kind, target, seconds, strict=strict)
        timer = self.get(responsibility, kind)
        timer.update(start_event_id=None, start_utc=None, start_mono_ns=None, utc_error_ns=None,
                     clock_epoch=None, phase="RECOVERY_UNCERTAIN")
        self.events.append({"result": "TIMER_RECOVERY_UNCERTAIN", "reason": "LEGACY_START_UNPROVED",
                            "timer_id": timer["timer_id"], "generation": timer["generation"],
                            "handled_mono_ns": self.sample.monotonic_ns})

    def recover(self, responsibility, expected_target):
        for timer in responsibility.get("timers", {}).values():
            if timer["target_id"] != expected_target:
                self.retire(responsibility, timer["kind"])
                continue
            result = recover_timer(timer, self.sample, self.utc_error_ns)
            if result:
                self.events.append({"result": result, "timer_id": timer["timer_id"],
                                    "generation": timer["generation"], "handled_mono_ns": self.sample.monotonic_ns})
