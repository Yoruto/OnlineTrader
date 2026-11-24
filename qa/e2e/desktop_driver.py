"""Real Qt Workbench automation driver running in an isolated GUI subprocess."""

from __future__ import annotations

import multiprocessing
import time
from typing import Any


def _qt_workbench_main(pipe_name: str, control) -> None:
    """Subprocess hosting a real QApplication + WorkbenchWindow(runtime_view=True)."""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication
    from online_trader.desktop.workbench import WorkbenchWindow

    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("OnlineTraderQA")
    app.setApplicationName("WorkbenchE2E")
    app.setQuitOnLastWindowClosed(False)
    window = WorkbenchWindow(pipe_name, runtime_view=True)
    window.setAttribute(Qt.WA_DeleteOnClose)
    window.show()
    window.refresh()

    def _read_ui_state() -> dict[str, Any]:
        tables = getattr(window.dashboard, "tables", {}) or {}
        orders_rows: list[list[str]] = []
        orders_table = tables.get("orders")
        if orders_table is not None:
            for r in range(orders_table.rowCount()):
                orders_rows.append([
                    orders_table.item(r, c).text() if orders_table.item(r, c) else ""
                    for c in range(orders_table.columnCount())
                ])
        protection_rows: list[list[str]] = []
        prot_table = tables.get("protection")
        if prot_table is not None:
            for r in range(prot_table.rowCount()):
                protection_rows.append([
                    prot_table.item(r, c).text() if prot_table.item(r, c) else ""
                    for c in range(prot_table.columnCount())
                ])
        trades_rows: list[list[str]] = []
        trades_table = tables.get("trades")
        if trades_table is not None:
            for r in range(trades_table.rowCount()):
                trades_rows.append([
                    trades_table.item(r, c).text() if trades_table.item(r, c) else ""
                    for c in range(trades_table.columnCount())
                ])
        return {
            "visible": window.isVisible(),
            "title": window.windowTitle(),
            "status_text": window.status.text() if hasattr(window, "status") else "",
            "orders_rows": orders_rows,
            "protection_rows": protection_rows,
            "trades_rows": trades_rows,
            "view_boot_id": None if window.view is None else window.view.get("kernel_boot_id"),
            "view_entry_authority": None if window.view is None else window.view.get("entry_authority"),
        }

    def _poll_commands() -> None:
        while control.poll(0):
            try:
                cmd = control.recv()
            except EOFError:
                app.quit()
                return
            action = cmd.get("action")
            req_id = cmd.get("id")
            try:
                if action == "PING":
                    control.send({"id": req_id, "ok": True, "state": _read_ui_state()})
                elif action == "REFRESH":
                    window.refresh()
                    control.send({"id": req_id, "ok": True})
                elif action == "CLICK_BUTTON":
                    name = str(cmd["button"])
                    if name == "pause":
                        btn = window.pause_button
                    else:
                        btn = window.command_buttons[name]
                    if not btn.isEnabled():
                        control.send({"id": req_id, "ok": False, "error": f"Button {name!r} is disabled"})
                    else:
                        btn.click()
                        control.send({"id": req_id, "ok": True})
                elif action == "CLOSE_TO_TRAY":
                    window._tray.set_enabled(True)
                    tray_ok = bool(window._tray.sync_icon())
                    if tray_ok:
                        window.close()
                    control.send({"id": req_id, "ok": True, "visible": window.isVisible(), "tray_available": tray_ok})
                elif action == "REOPEN_FROM_TRAY":
                    window._tray.show_window()
                    control.send({"id": req_id, "ok": True, "visible": window.isVisible()})
                elif action == "EXIT_WORKBENCH":
                    # Trigger the real WorkbenchTray exit path -> _confirm_and_stop_runtime
                    control.send({"id": req_id, "ok": True})
                    QTimer.singleShot(0, window._tray.close_window)
                elif action == "GET_STATE":
                    control.send({"id": req_id, "ok": True, "state": _read_ui_state()})
                else:
                    control.send({"id": req_id, "ok": False, "error": f"Unsupported action: {action}"})
            except Exception as error:
                control.send({"id": req_id, "ok": False, "error": f"{type(error).__name__}: {error}"})

    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(_poll_commands)
    timer.start()
    window.destroyed.connect(timer.stop)
    window.destroyed.connect(app.quit)
    control.send({"event": "QT_READY"})
    app.exec()


class DesktopWorkbenchDriver:
    """Parent-side driver for an independent Qt WorkbenchWindow subprocess."""

    def __init__(self, pipe_name: str, *, startup_timeout: float = 15.0) -> None:
        self.pipe_name = pipe_name
        self.startup_timeout = startup_timeout
        self._ctx = multiprocessing.get_context("spawn")
        self._process = None
        self._control = None
        self._seq = 0
        self._closed = False

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def is_alive(self) -> bool:
        return not self._closed and self._process is not None and self._process.is_alive()

    def start(self) -> "DesktopWorkbenchDriver":
        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_qt_workbench_main,
            name="online-trader-qa-qt-workbench",
            args=(self.pipe_name, child),
            daemon=True,
        )
        proc.start()
        child.close()
        self._process = proc
        self._control = parent
        if not parent.poll(self.startup_timeout):
            self.close(force=True)
            raise TimeoutError("Qt Workbench subprocess did not signal QT_READY")
        msg = parent.recv()
        if msg.get("event") != "QT_READY":
            self.close(force=True)
            raise RuntimeError(f"Unexpected Qt startup message: {msg}")
        self.wait_until(lambda s: s.get("view_boot_id") is not None, timeout=self.startup_timeout)
        return self

    def _call(self, action: str, *, timeout: float = 10.0, **fields: Any) -> dict[str, Any]:
        if self._control is None or not self.is_alive:
            raise RuntimeError("Qt Workbench subprocess is not running")
        self._seq += 1
        req_id = f"qt-cmd-{self._seq}"
        self._control.send({"id": req_id, "action": action, **fields})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._control.poll(0.1):
                reply = self._control.recv()
                if reply.get("id") == req_id:
                    if not reply.get("ok"):
                        raise RuntimeError(f"Qt action {action} failed: {reply.get('error')}")
                    return reply
        raise TimeoutError(f"Timed out waiting for Qt action {action}")

    def state(self) -> dict[str, Any]:
        return self._call("GET_STATE")["state"]

    def refresh(self) -> None:
        self._call("REFRESH")

    def click_button(self, button: str) -> None:
        self._call("CLICK_BUTTON", button=button)

    def close_to_tray(self) -> tuple[bool, bool]:
        reply = self._call("CLOSE_TO_TRAY")
        return bool(reply["visible"]), bool(reply.get("tray_available", False))

    def reopen_from_tray(self) -> bool:
        return bool(self._call("REOPEN_FROM_TRAY")["visible"])

    def exit_workbench(self, *, timeout: float = 20.0) -> int | None:
        self._call("EXIT_WORKBENCH")
        assert self._process is not None
        self._process.join(timeout)
        if self._process.is_alive():
            raise TimeoutError(f"Qt Workbench pid={self._process.pid} did not exit cleanly")
        return self._process.exitcode

    def wait_until(self, predicate, *, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            st = self.state()
            if predicate(st):
                return st
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Workbench UI condition timed out; last state={st}")
            time.sleep(0.1)

    def close(self, *, force: bool = False) -> None:
        if self._closed:
            return
        proc = self._process
        if proc is not None and proc.is_alive():
            if force:
                proc.terminate()
                proc.join(3)
                if proc.is_alive():
                    proc.kill()
                    proc.join(2)
            else:
                try:
                    self.exit_workbench(timeout=15.0)
                except Exception:
                    proc.terminate()
                    proc.join(3)
        if proc is not None and not proc.is_alive():
            proc.close()
        self._process = None
        if self._control is not None:
            try:
                self._control.close()
            except OSError:
                pass
            self._control = None
        self._closed = True

    def __enter__(self) -> "DesktopWorkbenchDriver":
        return self.start()

    def __exit__(self, _exc_type, _exc_val, _tb) -> None:
        self.close(force=True)
