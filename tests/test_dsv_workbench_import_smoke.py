"""Desktop entry imports on every platform; real Qt tests require installed Qt.

The memory client is explicit. No Windows pipe, SDK or trading account is used.
"""
import ast
import importlib
import importlib.util
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkbenchImportContractTests(unittest.TestCase):
    def test_runtime_names_imported_by_desktop_actually_exist(self):
        tree = ast.parse((ROOT/'src/online_trader/desktop/offline_workbench.py').read_text(encoding='utf-8'))
        checked = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('online_trader.dsv_runtime.'):
                module = importlib.import_module(node.module)
                for item in node.names:
                    self.assertTrue(hasattr(module, item.name), (node.module, item.name))
                    checked.append(item.name)
        self.assertIn('PIPE_CONNECTION_ERRORS', checked)
        self.assertIn('OfflineClient', checked)


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'PySide6 is not installed; real Qt smoke NOT_RUN')
class WorkbenchQtSmokeTests(unittest.TestCase):
    def test_real_window_constructs_and_renders_activation(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from online_trader.desktop.offline_workbench import OfflineWorkbench
        app = QApplication.instance() or QApplication([])
        state = {'synchronized': True, 'running': False, 'trading_day': '2026-11-16',
                 'scope': {'account_key': 'DEV:desktop-smoke'}, 'symbols': {}, 'orders': {},
                 'history_status': 'READY', 'fills': [], 'activity': [{
                     'contract_id': 'DCE.p2701', 'reason': 'NO_CHASE_AFTER_ACT', 'direction': 'LONG',
                     'trigger_price': '8981.0', 'activation_price': '9039.4'}]}
        class Client:
            def request(self, *args, **kwargs): return state
        window = OfflineWorkbench('unused-pipe', client=Client())
        try:
            window.timer.stop()
            if window.request is not None:
                self.assertTrue(window.request.wait(2000))
            app.processEvents()
            window._show_state(state)
            window.show(); app.processEvents()
            text = window.activity.item(0, 3).text()
            self.assertIn('9039.4', text)
            self.assertIn('多头', text)
        finally:
            window.timer.stop()
            if window.request is not None: window.request.wait(2000)
            app.processEvents(); window.close(); window.deleteLater(); app.processEvents()

    def test_real_import_and_activity_formatter(self):
        from online_trader.desktop.offline_workbench import OfflineWorkbench
        row = OfflineWorkbench._activity_row({'contract_id': 'DCE.p2701', 'reason': 'NO_CHASE_AFTER_ACT',
                                              'direction': 'SHORT', 'trigger_price': '9041.0',
                                              'activation_price': '8982.6'})
        self.assertIn('空头', row[3]); self.assertIn('8982.6', row[3])


if __name__ == '__main__': unittest.main()
