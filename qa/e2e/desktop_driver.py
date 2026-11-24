"""Real Qt Workbench automation driver running in an isolated GUI subprocess."""

from __future__ import annotations

import multiprocessing
import time
from typing import Any


def _qt_workbench_main(pipe_name: str, control) -> None:
    """Subprocess hosting a real QApplication + WorkbenchWindow(runtime_view=True)."""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox, QSpinBox
    from online_trader.desktop.workbench import WorkbenchWindow

    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("OnlineTraderQA")
    app.setApplicationName("WorkbenchE2E")
    app.setQuitOnLastWindowClosed(False)
    window = WorkbenchWindow(pipe_name, runtime_view=True)
    window.setAttribute(Qt.WA_DeleteOnClose)
    window.show()
    window.refresh()

    def _table_rows(table) -> list[list[str]]:
        if table is None:
            return []
        rows: list[list[str]] = []
        for r in range(table.rowCount()):
            if table.isRowHidden(r):
                continue
            rows.append([
                table.item(r, c).text() if table.item(r, c) else ""
                for c in range(table.columnCount())
            ])
        return rows

    def _read_ui_state() -> dict[str, Any]:
        tables = getattr(window.dashboard, "tables", {}) or {}
        toolbar_texts = [
            label.text() for _dot, label in getattr(window.dashboard, "toolbar_status", ())
        ]
        buttons = {"pause": window.pause_button.isEnabled()}
        for name, btn in window.command_buttons.items():
            buttons[name] = btn.isEnabled()
        view = window.view or {}
        return {
            "visible": window.isVisible(),
            "title": window.windowTitle(),
            "status_text": window.status.text() if hasattr(window, "status") else "",
            "notice_text": window.notice_label.text() if hasattr(window, "notice_label") else "",
            "home_state": window.dashboard.home_state.text() if hasattr(window.dashboard, "home_state") else "",
            "home_connection": window.dashboard.home_connection.text() if hasattr(window.dashboard, "home_connection") else "",
            "toolbar_status": toolbar_texts,
            "button_enabled": buttons,
            "cycles_rows": _table_rows(getattr(window, "cycles", None)),
            "markets_rows": _table_rows(tables.get("markets")),
            "orders_rows": _table_rows(tables.get("orders")),
            "protection_rows": _table_rows(tables.get("protection")),
            "trades_rows": _table_rows(tables.get("trades")),
            "blocks_rows": _table_rows(tables.get("blocks")),
            "view_boot_id": view.get("kernel_boot_id"),
            "view_entry_authority": view.get("entry_authority"),
            "view_synchronized": (view.get("connection") or {}).get("synchronized"),
            "view_owned_quantity": (view.get("current_responsibility") or {}).get("owned_quantity"),
        }

    def _resolve_table(name: str):
        if name == "cycles":
            return window.cycles
        return (getattr(window.dashboard, "tables", {}) or {}).get(name)

    def _handle_modal(dialog_int: int | None, dialog_text: str | None, accept_msg: bool, attempts: list[int]) -> None:
        modal = app.activeModalWidget()
        if modal is None:
            attempts[0] += 1
            if attempts[0] < 40:
                QTimer.singleShot(25, lambda: _handle_modal(dialog_int, dialog_text, accept_msg, attempts))
            return
        if isinstance(modal, QInputDialog):
            if dialog_int is not None:
                spin = modal.findChild(QSpinBox)
                if spin is not None:
                    spin.setValue(int(dialog_int))
                else:
                    modal.setIntValue(int(dialog_int))
            if dialog_text is not None:
                edit = modal.findChild(QLineEdit)
                if edit is not None:
                    edit.setText(str(dialog_text))
                else:
                    modal.setTextValue(str(dialog_text))
            modal.accept()
            return
        if isinstance(modal, QMessageBox):
            btn = modal.button(QMessageBox.Yes if accept_msg else QMessageBox.No)
            if btn is not None:
                btn.click()
            else:
                modal.accept()

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
                elif action == "SELECT_ROW":
                    table_name = str(cmd["table"])
                    row_idx = int(cmd.get("row", 0))
                    table = _resolve_table(table_name)
                    if table is None or row_idx < 0 or row_idx >= table.rowCount():
                        control.send({"id": req_id, "ok": False, "error": f"Invalid row {row_idx} for table {table_name}"})
                    else:
                        table.selectRow(row_idx)
                        control.send({"id": req_id, "ok": True})
                elif action == "CLICK_BUTTON":
                    name = str(cmd["button"])
                    btn = window.pause_button if name == "pause" else window.command_buttons[name]
                    if not btn.isEnabled():
                        control.send({"id": req_id, "ok": False, "error": f"Button {name!r} is disabled"})
                    else:
                        if cmd.get("dialog_int") is not None or cmd.get("dialog_text") is not None or cmd.get("expect_modal"):
                            d_int = cmd.get("dialog_int")
                            d_txt = cmd.get("dialog_text")
                            d_acc = bool(cmd.get("message_box_accept", True))
                            QTimer.singleShot(25, lambda: _handle_modal(d_int, d_txt, d_acc, [0]))
                        QTimer.singleShot(0, btn.click)
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

    def select_row(self, table: str, row: int = 0) -> None:
        self._call("SELECT_ROW", table=table, row=int(row))

    def click_button(
        self,
        button: str,
        *,
        dialog_int: int | None = None,
        dialog_text: str | None = None,
        expect_modal: bool = False,
        message_box_accept: bool = True,
    ) -> None:
        self._call(
            "CLICK_BUTTON",
            button=button,
            dialog_int=dialog_int,
            dialog_text=dialog_text,
            expect_modal=expect_modal,
            message_box_accept=message_box_accept,
        )

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
