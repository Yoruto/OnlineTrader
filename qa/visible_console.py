"""Visible, isolated Qt acceptance probe; does not synthesize product account facts."""

import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from online_trader.desktop.app import StatusWindow
from online_trader.transport.status_client import StatusClient
from qa.fault_report import atomic_json


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipe", required=True)
    parser.add_argument("--report-file", required=True, type=Path)
    parser.add_argument("--screenshot-file", required=True, type=Path)
    parser.add_argument("--mode", choices=["close", "disconnect"], required=True)
    args = parser.parse_args(argv)
    application = QApplication.instance() or QApplication([])
    window = StatusWindow(args.pipe, client_factory=lambda pipe: StatusClient(pipe, timeout=0.8))
    window.setAttribute(Qt.WA_ShowWithoutActivating, True)
    window.show()
    window.refresh()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    data = {"pid": os.getpid(), "mode": args.mode, "scope": "visible local Qt with isolated development ledger", "ready": False,
            "was_visible": False, "connection_failure_seen": False, "screenshot": str(args.screenshot_file)}
    started = time.monotonic()
    refresh_at = started
    closing = False
    timer = QTimer()
    timer.setInterval(25)

    def close_after_capture():
        nonlocal closing
        closing = True
        QTimer.singleShot(100, window.close)

    def tick():
        nonlocal refresh_at
        if closing:
            return
        if time.monotonic() - started > 7:
            data.update(phase="failed", error="Visible console deadline expired")
            atomic_json(args.report_file, data)
            close_after_capture()
            return
        if not data["ready"] and not window._stale and window._worker is None:
            visible = bool(user32.IsWindowVisible(int(window.winId())))
            if not visible:
                data.update(phase="failed", error="Native window is not visible")
                atomic_json(args.report_file, data); close_after_capture(); return
            args.screenshot_file.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.screenshot_file)):
                data.update(phase="failed", error="Window capture failed")
                atomic_json(args.report_file, data); close_after_capture(); return
            data.update(phase="ready", ready=True, was_visible=visible, hwnd=str(int(window.winId())),
                        cycle_count=window.cycles.rowCount(), funds_text=window.funds_label.text(),
                        ready_connection_text=window.connection_label.text())
            atomic_json(args.report_file, data)
            if args.mode == "close":
                close_after_capture(); return
        elif data["ready"] and args.mode == "disconnect" and window._stale and window._worker is None:
            stale_file = args.screenshot_file.with_name(args.screenshot_file.stem + "-stale.png")
            if not window.grab().save(str(stale_file)):
                data.update(phase="failed", error="Stale window capture failed")
            else:
                data.update(phase="stale", connection_failure_seen=True, stale_screenshot=str(stale_file),
                            retained_cycle_count=window.cycles.rowCount(), stale_connection_text=window.connection_label.text())
            atomic_json(args.report_file, data)
            close_after_capture(); return
        if data["ready"] and args.mode == "disconnect" and window._worker is None and time.monotonic() >= refresh_at:
            refresh_at = time.monotonic() + 0.15
            window.refresh()

    timer.timeout.connect(tick)
    timer.start()
    application.exec()
    timer.stop()
    data.update(closed=True, final_visible=bool(user32.IsWindowVisible(int(window.winId()))))
    atomic_json(args.report_file, data)
    return 0 if data["ready"] and not data.get("error") and (args.mode != "disconnect" or data["connection_failure_seen"]) else 2


if __name__ == "__main__":raise SystemExit(main())
