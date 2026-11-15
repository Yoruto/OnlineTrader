"""Windows Qt display checks; no connection to a trading owner or SDK."""

import sys
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows Qt/IPC import environment required')
class WorkbenchHistoryQtTests(unittest.TestCase):
    def test_unresolved_identity_is_not_labelled_as_a_known_send(self):
        from online_trader.desktop.offline_workbench import OfflineWorkbench
        row = OfflineWorkbench._activity_row({'reason': 'UNKNOWN', 'effect_kind': None,
                                               'child_order_id': None, 'contract_id': None})
        self.assertEqual(row, ('—', '—', '报撤结果未知', '等待原生委托与成交核对', '—'))

    def test_real_qt_window_displays_degraded_history_and_event_sequence(self):
        from PySide6.QtWidgets import QApplication
        from online_trader.desktop.offline_workbench import OfflineWorkbench
        app = QApplication.instance() or QApplication([])
        state = {'synchronized': True, 'running': False, 'trading_day': '2026-11-13',
                 'scope': {'account_key': 'DEV:history-display'}, 'symbols': {}, 'orders': {},
                 'history_status': 'DEGRADED', 'fills': [], 'activity': [
                     {'reason': 'HISTORY_RECORD_INCOMPLETE', 'event_seq': '7',
                      'error_code': 'E_HISTORY_EFFECT_MISSING'}]}

        class Client:
            def request(self, *args, **kwargs):
                return state

        window = OfflineWorkbench('unused-offline-test-pipe', client=Client())
        try:
            window.timer.stop()
            window._show_state(state)
            window.show()
            app.processEvents()
            self.assertFalse(window.history_status.isHidden())
            self.assertIn('部分历史记录不完整', window.history_status.text())
            self.assertEqual(window.activity.item(0, 2).text(), '历史记录降级')
            self.assertIn('事件序号 7', window.activity.item(0, 3).text())
            self.assertIn('E_HISTORY_EFFECT_MISSING', window.activity.item(0, 3).text())
        finally:
            window.close()
            app.processEvents()
            window.deleteLater()
            app.processEvents()


if __name__ == '__main__':
    unittest.main()
