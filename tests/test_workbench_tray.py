"""The DSV tray preference preserves the same local workbench instance."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon

from online_trader.desktop.tray import WorkbenchTray
from online_trader.desktop.workbench import WorkbenchWindow


class WorkbenchTrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / 'view.ini'), QSettings.IniFormat)

    def test_close_to_tray_keeps_refresh_and_exit_closes_the_window(self):
        with patch('online_trader.desktop.workbench.QSettings', return_value=self.settings):
            window = WorkbenchWindow(None)
        self.addCleanup(window.deleteLater)
        window.show()
        self.settings.setValue('minimize_to_tray', True)
        with patch('online_trader.desktop.tray.QSystemTrayIcon') as tray:
            tray.isSystemTrayAvailable.return_value = True
            tray.Trigger = QSystemTrayIcon.Trigger
            tray.DoubleClick = QSystemTrayIcon.DoubleClick
            icon = Mock()
            icon.isVisible.return_value = False
            tray.return_value = icon
            self.assertFalse(window.close())
            self.assertFalse(window.isVisible())
            self.assertTrue(window.timer.isActive())
            icon.show.assert_called_once()
            window._tray.on_activate(tray.DoubleClick)
            self.assertTrue(window.isVisible())
            window._tray.close_window()
            self.assertFalse(window.isVisible())
            self.assertFalse(window.timer.isActive())
            icon.hide.assert_called_once()

    def test_saved_preference_shows_icon_before_first_close_and_updates_status(self):
        self.settings.setValue('minimize_to_tray', True)
        with (patch('online_trader.desktop.workbench.QSettings', return_value=self.settings),
              patch('online_trader.desktop.tray.QSystemTrayIcon') as tray):
            tray.isSystemTrayAvailable.return_value = True
            icon = tray.return_value
            icon.isVisible.return_value = False
            window = WorkbenchWindow(None)
            self.addCleanup(window.deleteLater)
            window.show()
            self.assertTrue(window.isVisible())
            icon.show.assert_called_once()
            self.assertIn('策略未启用',icon.setToolTip.call_args.args[0])
            window._tray.set_status('running')
            self.assertIn('策略已启用',icon.setToolTip.call_args.args[0])
            window._tray.set_status('error')
            self.assertIn('状态待核对',icon.setToolTip.call_args.args[0])
            icon.isVisible.return_value = True
            window._tray.preference_checkbox().setChecked(False)
            icon.hide.assert_called_once()
            self.assertTrue(window.close())

    def test_unavailable_tray_uses_normal_close_and_saved_geometry_restores(self):
        first = QMainWindow()
        first.resize(450, 350)
        first.show()
        controller = WorkbenchTray(first, self.settings)
        controller.preference_checkbox().setChecked(True)
        with patch('online_trader.desktop.tray.QSystemTrayIcon.isSystemTrayAvailable', return_value=False):
            self.assertFalse(controller.minimize_on_close())
        controller.save_geometry()
        second = QMainWindow()
        WorkbenchTray(second, self.settings).restore_geometry()
        self.assertEqual(second.size(), first.size())
        first.close()
