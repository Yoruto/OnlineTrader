"""External performance monitor isolated from Runtime Owner and Qt main thread.

Collects OS process CPU/memory via Windows Win32 ctypes (zero third-party deps),
Named Pipe IPC latency, frame backlog/apply latency, log file growth, and optional
UI event-loop lag, plus baseline regression comparison for DEGRADED verdicts.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event, Lock, Thread
import time
from typing import Any, Callable


# ---- Windows native process metrics (zero third-party dependencies) ----

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _filetime_to_seconds(ft: _FILETIME) -> float:
    ticks_100ns = (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)
    return ticks_100ns / 10_000_000.0


def sample_windows_process_metrics(pid: int | None) -> dict[str, float | int | None]:
    """Return CPU seconds and working-set/private memory bytes for a Windows PID."""
    if os.name != "nt" or not pid or pid <= 0:
        return {"cpu_seconds": None, "working_set_bytes": None, "private_bytes": None}
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, int(pid))
    if not handle:
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return {"cpu_seconds": None, "working_set_bytes": None, "private_bytes": None}
    try:
        creation = _FILETIME()
        exit_t = _FILETIME()
        kernel_t = _FILETIME()
        user_t = _FILETIME()
        cpu_s: float | None = None
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            cpu_s = _filetime_to_seconds(kernel_t) + _filetime_to_seconds(user_t)

        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX)
        ws: int | None = None
        priv: int | None = None
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            ws = int(counters.WorkingSetSize)
            priv = int(counters.PrivateUsage)
        return {
            "cpu_seconds": cpu_s,
            "working_set_bytes": ws,
            "private_bytes": priv,
        }
    finally:
        kernel32.CloseHandle(handle)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[idx], 3)


def _summarize_series(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": round(max(values), 3),
        "mean": round(sum(values) / len(values), 3),
    }


def _directory_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for item in root.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


@dataclass
class DegradationThresholds:
    """Default tolerances when comparing current run against a recorded baseline."""

    ipc_p95_ms_ratio: float = 1.5
    ipc_p95_ms_floor: float = 50.0
    frame_apply_p95_ms_ratio: float = 1.5
    frame_apply_p95_ms_floor: float = 40.0
    ui_lag_p95_ms_ratio: float = 1.5
    ui_lag_p95_ms_floor: float = 50.0
    max_memory_growth_ratio: float = 1.4
    max_ipc_errors: int = 0


def compare_with_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    thresholds: DegradationThresholds | None = None,
) -> dict[str, object]:
    """Compare current performance summary against a saved baseline on the same machine."""
    th = thresholds or DegradationThresholds()
    regressions: list[dict[str, object]] = []

    def _check_latency(section: str, ratio_limit: float, floor_ms: float) -> None:
        cur_p95 = ((current.get(section) or {}).get("p95"))
        base_p95 = ((baseline.get(section) or {}).get("p95"))
        if isinstance(cur_p95, (int, float)) and isinstance(base_p95, (int, float)):
            limit = max(floor_ms, base_p95 * ratio_limit)
            if cur_p95 > limit:
                regressions.append({
                    "metric": f"{section}.p95",
                    "current_ms": cur_p95,
                    "baseline_ms": base_p95,
                    "limit_ms": round(limit, 3),
                })

    _check_latency("ipc_rtt_ms", th.ipc_p95_ms_ratio, th.ipc_p95_ms_floor)
    _check_latency("frame_apply_ms", th.frame_apply_p95_ms_ratio, th.frame_apply_p95_ms_floor)
    _check_latency("ui_event_loop_lag_ms", th.ui_lag_p95_ms_ratio, th.ui_lag_p95_ms_floor)

    cur_mem = (current.get("owner_memory_mib") or {}).get("max")
    base_mem = (baseline.get("owner_memory_mib") or {}).get("max")
    if isinstance(cur_mem, (int, float)) and isinstance(base_mem, (int, float)) and base_mem > 0:
        limit = base_mem * th.max_memory_growth_ratio
        if cur_mem > limit:
            regressions.append({
                "metric": "owner_memory_mib.max",
                "current_mib": cur_mem,
                "baseline_mib": base_mem,
                "limit_mib": round(limit, 3),
            })

    cur_ipc_err = int(current.get("ipc_errors") or 0)
    if cur_ipc_err > th.max_ipc_errors:
        regressions.append({
            "metric": "ipc_errors",
            "current": cur_ipc_err,
            "limit": th.max_ipc_errors,
        })

    return {
        "degraded": bool(regressions),
        "regressions": regressions,
    }


class ExternalPerformanceMonitor:
    """Background observer running outside the Runtime Owner and Qt main loop."""

    def __init__(
        self,
        *,
        output_jsonl: str | Path,
        owner_pid: int | None,
        view_pid: int | None = None,
        log_dir: Path | None = None,
        sample_interval_seconds: float = 0.5,
        ipc_probe: Callable[[], dict[str, object]] | None = None,
        backlog_probe: Callable[[], int] | None = None,
    ) -> None:
        self.output_jsonl = Path(output_jsonl).resolve()
        self.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        self.owner_pid = owner_pid
        self.view_pid = view_pid
        self.log_dir = log_dir
        self.sample_interval = max(0.1, float(sample_interval_seconds))
        self._ipc_probe = ipc_probe
        self._backlog_probe = backlog_probe

        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()

        self._ipc_rtt_ms: list[float] = []
        self._ipc_errors = 0
        self._frame_apply_ms: list[float] = []
        self._ui_lag_ms: list[float] = []
        self._ui_op_ms: list[float] = []
        self._backlog_samples: list[float] = []
        self._owner_cpu_pct: list[float] = []
        self._owner_mem_mib: list[float] = []
        self._log_bytes_samples: list[int] = []
        self._frames_sent = 0
        self._frames_applied = 0
        self._backpressure_events = 0
        self._started_monotonic = 0.0
        self._prev_cpu_s: float | None = None
        self._prev_sample_mono: float | None = None

    def record_frame_sent(self) -> None:
        with self._lock:
            self._frames_sent += 1

    def record_frame_applied(self, latency_ms: float) -> None:
        with self._lock:
            self._frames_applied += 1
            self._frame_apply_ms.append(max(0.0, float(latency_ms)))

    def record_backpressure(self) -> None:
        with self._lock:
            self._backpressure_events += 1

    def record_ui_lag(self, lag_ms: float) -> None:
        with self._lock:
            self._ui_lag_ms.append(max(0.0, float(lag_ms)))

    def record_ui_operation(self, op_ms: float) -> None:
        with self._lock:
            self._ui_op_ms.append(max(0.0, float(op_ms)))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_monotonic = time.monotonic()
        self._stop.clear()
        self._thread = Thread(target=self._run_loop, name="external-perf-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        return self.summary()

    def _run_loop(self) -> None:
        with self.output_jsonl.open("w", encoding="utf-8") as stream:
            while not self._stop.is_set():
                now_mono = time.monotonic()
                owner_m = sample_windows_process_metrics(self.owner_pid)
                view_m = sample_windows_process_metrics(self.view_pid) if self.view_pid else None

                cpu_pct: float | None = None
                cur_cpu_s = owner_m.get("cpu_seconds")
                if isinstance(cur_cpu_s, float) and self._prev_cpu_s is not None and self._prev_sample_mono is not None:
                    dt = max(1e-6, now_mono - self._prev_sample_mono)
                    cpu_pct = max(0.0, ((cur_cpu_s - self._prev_cpu_s) / dt) * 100.0)
                if isinstance(cur_cpu_s, float):
                    self._prev_cpu_s = cur_cpu_s
                self._prev_sample_mono = now_mono

                ws_bytes = owner_m.get("working_set_bytes")
                mem_mib = (ws_bytes / (1024 * 1024)) if isinstance(ws_bytes, int) else None

                ipc_ms: float | None = None
                ipc_ok = True
                if self._ipc_probe is not None:
                    t0 = time.monotonic()
                    try:
                        self._ipc_probe()
                        ipc_ms = (time.monotonic() - t0) * 1000.0
                    except Exception:
                        ipc_ok = False

                backlog = self._backlog_probe() if self._backlog_probe is not None else None
                log_bytes = _directory_bytes(self.log_dir) if self.log_dir is not None else None

                with self._lock:
                    if cpu_pct is not None:
                        self._owner_cpu_pct.append(cpu_pct)
                    if mem_mib is not None:
                        self._owner_mem_mib.append(mem_mib)
                    if ipc_ms is not None:
                        self._ipc_rtt_ms.append(ipc_ms)
                    if not ipc_ok:
                        self._ipc_errors += 1
                    if backlog is not None:
                        self._backlog_samples.append(float(backlog))
                    if log_bytes is not None:
                        self._log_bytes_samples.append(log_bytes)
                    sample = {
                        "utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "elapsed_s": round(now_mono - self._started_monotonic, 3),
                        "owner_cpu_pct": None if cpu_pct is None else round(cpu_pct, 2),
                        "owner_memory_mib": None if mem_mib is None else round(mem_mib, 3),
                        "view_memory_mib": (
                            None
                            if not view_m or not isinstance(view_m.get("working_set_bytes"), int)
                            else round(int(view_m["working_set_bytes"]) / (1024 * 1024), 3)
                        ),
                        "ipc_rtt_ms": None if ipc_ms is None else round(ipc_ms, 3),
                        "ipc_ok": ipc_ok,
                        "frame_backlog": backlog,
                        "frames_sent": self._frames_sent,
                        "frames_applied": self._frames_applied,
                        "backpressure_events": self._backpressure_events,
                        "log_dir_bytes": log_bytes,
                    }

                stream.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                self._stop.wait(self.sample_interval)

    def summary(self) -> dict[str, object]:
        elapsed = max(1e-6, time.monotonic() - self._started_monotonic) if self._started_monotonic else 0.0
        with self._lock:
            log_growth = (
                (self._log_bytes_samples[-1] - self._log_bytes_samples[0])
                if len(self._log_bytes_samples) >= 2
                else 0
            )
            mem_growth = (
                round(self._owner_mem_mib[-1] - self._owner_mem_mib[0], 3)
                if len(self._owner_mem_mib) >= 2
                else None
            )
            return {
                "elapsed_seconds": round(elapsed, 3),
                "frames_sent": self._frames_sent,
                "frames_applied": self._frames_applied,
                "send_rate_fps": round(self._frames_sent / elapsed, 2) if elapsed else 0.0,
                "apply_rate_fps": round(self._frames_applied / elapsed, 2) if elapsed else 0.0,
                "backpressure_events": self._backpressure_events,
                "ipc_errors": self._ipc_errors,
                "ipc_rtt_ms": _summarize_series(self._ipc_rtt_ms),
                "frame_apply_ms": _summarize_series(self._frame_apply_ms),
                "frame_backlog": _summarize_series(self._backlog_samples),
                "owner_cpu_pct": _summarize_series(self._owner_cpu_pct),
                "owner_memory_mib": _summarize_series(self._owner_mem_mib),
                "owner_memory_delta_mib": mem_growth,
                "ui_event_loop_lag_ms": _summarize_series(self._ui_lag_ms),
                "ui_operation_ms": _summarize_series(self._ui_op_ms),
                "log_dir_growth_bytes": log_growth,
                "metrics_jsonl": str(self.output_jsonl),
            }
