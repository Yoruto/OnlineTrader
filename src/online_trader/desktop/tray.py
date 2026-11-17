"""Window hiding and restoration through the Windows tray."""
from PySide6.QtCore import QByteArray, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import QCheckBox, QMenu, QStyle, QSystemTrayIcon


class WorkbenchTray(QObject):
    def __init__(self, window, settings):
        super().__init__(window)
        self.window = window
        self.settings = settings
        self.icon = None
        self.menu = None
        self.exit_requested = False
        self.status = 'idle'
        self.base_icon = None
        self.minimize_timer = QTimer(self)
        self.minimize_timer.setSingleShot(True)
        self.minimize_timer.timeout.connect(self._hide_if_minimized)

    def preference_checkbox(self):
        control = QCheckBox('最小化或关闭窗口时收起到系统托盘')
        control.setChecked(self.settings.value('minimize_to_tray', True, bool))
        control.toggled.connect(self.set_enabled)
        return control

    def set_enabled(self, enabled):
        self.settings.setValue('minimize_to_tray', enabled)
        self.sync_icon()

    def restore_geometry(self):
        geometry = self.settings.value('window_geometry')
        if isinstance(geometry, QByteArray) and geometry:
            self.window.restoreGeometry(geometry)

    def save_geometry(self):
        if self.window.isVisible():
            self.settings.setValue('window_geometry', self.window.saveGeometry())

    def minimize_on_close(self):
        if not self.sync_icon():
            return False
        self.window.hide()
        return True

    def on_minimized(self):
        # Let Windows finish its native minimize transition before hiding the taskbar window.
        self.minimize_timer.start(0)

    def _hide_if_minimized(self):
        if self.window.isVisible() and self.window.isMinimized():
            if self.sync_icon():
                self.save_geometry()
                self.window.hide()

    def sync_icon(self):
        if self.exit_requested:
            return False
        if (not self.settings.value('minimize_to_tray', True, bool)
                or not QSystemTrayIcon.isSystemTrayAvailable()):
            if self.icon is not None and self.icon.isVisible():
                self.icon.hide()
            return False
        if self.icon is None:
            self.base_icon = self.window.windowIcon()
            if self.base_icon.isNull():
                self.base_icon = self.window.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self.icon = QSystemTrayIcon(self.base_icon, self)
            self.menu = QMenu(self.window)
            restore = self.menu.addAction('显示窗口', self.show_window)
            self.menu.setDefaultAction(restore)
            self.menu.addAction('退出工作台', self.close_window)
            self.icon.setContextMenu(self.menu)
            self.icon.activated.connect(self.on_activate)
            self._render_status()
        if not self.icon.isVisible():
            self.icon.show()
        return True

    def set_status(self, status):
        if status != self.status:
            self.status = status
            if self.icon is not None:
                self._render_status()

    def _render_status(self):
        label, color = {'idle': ('策略未启用', '#94a3b8'), 'running': ('策略已启用', '#22c55e'),
                        'error': ('状态待核对', '#ef4444')}[self.status]
        pixmap = self.base_icon.pixmap(32, 32)
        with QPainter(pixmap) as painter:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor('#ffffff'))
            painter.setBrush(QColor(color))
            painter.drawEllipse(pixmap.width()-13, pixmap.height()-13, 12, 12)
        self.icon.setIcon(QIcon(pixmap))
        self.icon.setToolTip(f'{self.window.windowTitle()} · {label}')

    def on_activate(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def show_window(self):
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def close_window(self):
        self.exit_requested = True
        self.minimize_timer.stop()
        if self.icon is not None:
            self.icon.hide()
        self.window.close()
