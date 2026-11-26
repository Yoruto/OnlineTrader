"""Real Qt Workbench UI interaction stress driver (lazy PySide6 import).

Measures Qt main-thread event-loop lag and simulates periodic user interactions
(switching tabs, refreshing diagnostics/logs panels) while the Runtime processes
high-rate market feeds and order events.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

from qa.scenario_engine.monitor import ExternalPerformanceMonitor


class WorkbenchUIStressDriver:
    """Drives a real WorkbenchWindow under realtime stress and reports UI latency."""

    def __init__(
        self,
        *,
        pipe_name: str,
        runtime_root: Path,
        monitor: ExternalPerformanceMonitor,
        ops_per_minute: float = 12.0,
        duration_seconds: float = 30.0,
        offscreen: bool = True,
    ) -> None:
        self.pipe_name = pipe_name
        self.runtime_root = Path(runtime_root).resolve()
        self.monitor = monitor
        self.ops_per_minute = max(0.0, float(ops_per_minute))
        self.duration_seconds = max(1.0, float(duration_seconds))
        self.offscreen = bool(offscreen)
        self.operations_executed = 0

    def run(self) -> dict[str, object]:
        """Launch real WorkbenchWindow in Qt event loop and execute stress interactions."""
        if self.offscreen and "QT_QPA_PLATFORM" not in os.environ:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"

        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QTabWidget
        from online_trader.desktop.workbench import WorkbenchWindow

        app = QApplication.instance()
        owns_app = app is None
        if app is None:
            app = QApplication([])

        window: Any = WorkbenchWindow(
            pipe_name=self.pipe_name,
            runtime_root=self.runtime_root,
        )
        window.show()

        # Find any QTabWidget children for realistic tab-switching interactions
        tab_widgets = window.findChildren(QTabWidget)

        lag_interval_ms = 50
        last_tick = [time.monotonic()]
        op_cursor = [0]

        def _measure_lag() -> None:
            now = time.monotonic()
            actual_ms = (now - last_tick[0]) * 1000.0
            last_tick[0] = now
            lag = max(0.0, actual_ms - lag_interval_ms)
            self.monitor.record_ui_lag(lag)

        def _perform_interaction() -> None:
            t0 = time.monotonic()
            try:
                if tab_widgets:
                    tw = tab_widgets[op_cursor[0] % len(tab_widgets)]
                    if tw.count() > 1:
                        next_idx = (tw.currentIndex() + 1) % tw.count()
                        tw.setCurrentIndex(next_idx)
                if hasattr(window, "_request_refresh"):
                    window._request_refresh()
                elif hasattr(window, "refresh_snapshot"):
                    window.refresh_snapshot()
                app.processEvents()
                op_cursor[0] += 1
                self.operations_executed += 1
            finally:
                self.monitor.record_ui_operation((time.monotonic() - t0) * 1000.0)

        lag_timer = QTimer()
        lag_timer.setInterval(lag_interval_ms)
        lag_timer.timeout.connect(_measure_lag)
        lag_timer.start()

        op_timer: Any = None
        if self.ops_per_minute > 0:
            op_interval_ms = max(200, int((60.0 / self.ops_per_minute) * 1000.0))
            op_timer = QTimer()
            op_timer.setInterval(op_interval_ms)
            op_timer.timeout.connect(_perform_interaction)
            op_timer.start()

        stop_timer = QTimer()
        stop_timer.setSingleShot(True)
        stop_timer.setInterval(int(self.duration_seconds * 1000.0))
        stop_timer.timeout.connect(app.quit)
        stop_timer.start()

        try:
            app.exec()
        finally:
            lag_timer.stop()
            if op_timer is not None:
                op_timer.stop()
            window.close()
            if owns_app:
                app.processEvents()

        return {
            "ui_operations_executed": self.operations_executed,
            "ops_per_minute_target": self.ops_per_minute,
            "duration_seconds": self.duration_seconds,
        }
