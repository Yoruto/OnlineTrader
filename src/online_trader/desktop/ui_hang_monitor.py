"""Lightweight watchdog detecting Qt main-thread event-loop stalls."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import threading
import time
import traceback

_LOGGER = logging.getLogger('online_trader.ui_hang')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class UiHangMonitor:
    """Detect main-thread freezes from a daemon thread and capture stack traces.

    The UI main thread calls ``heartbeat()`` periodically from a Qt timer.
    When the elapsed time since the last heartbeat exceeds ``threshold_s``,
    a background daemon thread captures the main thread's Python stack via
    ``sys._current_frames()`` and writes a bounded JSONL diagnostic record.
    """

    def __init__(
        self,
        *,
        threshold_s: float = 5.0,
        poll_interval_s: float = 1.0,
        cooldown_s: float = 30.0,
        log_dir_getter=None,
        logger: logging.Logger | None = None,
        main_thread_ident: int | None = None,
    ):
        self.threshold_s = max(0.05, float(threshold_s))
        self.poll_interval_s = max(0.02, float(poll_interval_s))
        self.cooldown_s = max(0.05, float(cooldown_s))
        self._log_dir_getter = log_dir_getter
        self._logger = logger or _LOGGER
        self._main_ident = (
            threading.get_ident() if main_thread_ident is None else main_thread_ident
        )
        self._lock = threading.Lock()
        self._last_heartbeat = time.monotonic()
        self._hung_since: float | None = None
        self._last_dump = 0.0
        self._dump_count = 0
        self._pending_emits: list[tuple[dict, int]] = []
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def dump_count(self) -> int:
        with self._lock:
            return self._dump_count

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._last_heartbeat = time.monotonic()
            self._hung_since = None
            thread = threading.Thread(
                target=self._run,
                name='online-trader-ui-hang-monitor',
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def heartbeat(self) -> None:
        now = time.monotonic()
        wake = False
        with self._lock:
            self._last_heartbeat = now
            if self._hung_since is not None:
                recovered_ms = max(0, int((now - self._hung_since) * 1000))
                self._hung_since = None
                self._pending_emits.append(({
                    'at': _utc_now(),
                    'event': 'UI_HANG_RECOVERED',
                    'hung_duration_ms': recovered_ms,
                }, logging.INFO))
                wake = True
        if wake:
            self._wake_event.set()

    def _flush_pending(self) -> None:
        with self._lock:
            if not self._pending_emits:
                return
            items = self._pending_emits
            self._pending_emits = []
        for payload, level in items:
            self._emit(payload, level=level)

    def _run(self) -> None:
        while True:
            self._wake_event.wait(self.poll_interval_s)
            self._wake_event.clear()
            self._flush_pending()
            if self._stop_event.is_set():
                break
            self._check_once()
        self._flush_pending()

    def _check_once(self) -> None:
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last_heartbeat
            if elapsed < self.threshold_s:
                return
            if self._hung_since is None:
                self._hung_since = self._last_heartbeat
            if self._last_dump and (now - self._last_dump) < self.cooldown_s:
                return
            self._last_dump = now
            self._dump_count += 1
            dump_index = self._dump_count

        frames = sys._current_frames()
        frame = frames.get(self._main_ident)
        stack = traceback.format_stack(frame) if frame is not None else []
        payload = {
            'at': _utc_now(),
            'event': 'UI_HANG_DETECTED',
            'stall_ms': max(0, int(elapsed * 1000)),
            'dump_index': dump_index,
            'main_thread_ident': self._main_ident,
            'stack': ''.join(stack[-16:]),
        }
        self._emit(payload, level=logging.WARNING)

    def _emit(self, payload: dict, *, level: int) -> None:
        try:
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            self._logger.log(level, line)
        except Exception:
            return
        if self._log_dir_getter is None:
            return
        try:
            directory = self._log_dir_getter()
            if directory is None:
                return
            path = Path(directory)
            if not path.is_dir():
                return
            with (path / 'ui-hang-diagnostics.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except Exception:
            pass
