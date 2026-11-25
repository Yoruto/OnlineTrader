import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.order_diagnostics import OrderDiagnosticTracker
from online_trader.dsv_runtime.runner import DsvSession


class _NoopPort:
    def send(self, request):
        return {'stage': 'noop'}

    def cancel(self, request):
        return {'stage': 'noop'}


def _capture_tracker():
    tracker = OrderDiagnosticTracker(account_key='DEV:diag-test', boot_id='boot-1')
    events = []
    original_emit = tracker.emit

    def record(event_type, **kwargs):
        kwargs['log_to_logger'] = False
        payload = original_emit(event_type, **kwargs)
        events.append(payload)
        return payload

    tracker.emit = record
    return tracker, events


def _make_profile(contracts=('DCE.p2701', 'CZCE.OI701')):
    settings = load_confirmed_baseline()[0].to_settings()
    base = settings['symbols'][0]
    settings['symbols'] = [base | {'code': code} for code in contracts]
    return DsvProfile.from_settings(settings)


def _sample_market(contract_id='DCE.p2701', *, long_qty=0, short_qty=0):
    return {
        'contract_id': contract_id,
        'session_open': '2026-11-25T01:00:00Z',
        'session_close': '2026-11-25T07:00:00Z',
        'opening_session': True,
        'market_session_active': True,
        'market_permission': {'market_phase': 'CONTINUOUS', 'continuous_matching': True, 'can_cancel': True},
        'ticks': [],
        'long_qty': long_qty,
        'short_qty': short_qty,
        'long_today': long_qty,
        'long_historical': 0,
        'short_today': short_qty,
        'short_historical': 0,
        'allow_new_order': True,
        'allow_new_entry': True,
        'allow_protective_exit': True,
    }


class OrderDiagnosticsDedupeTests(unittest.TestCase):
    def test_dedupe_signature_suppresses_dynamic_detail_spam(self):
        tracker, events = _capture_tracker()
        for price in range(5001, 6001):
            tracker.observe_block(
                ('controller', 'DCE.p2701', 'OPEN'),
                active=True,
                contract_id='DCE.p2701',
                effect='OPEN',
                reason='E_ENTRY_PRICE_CAP',
                detail=f'proposed={price},cap=5000',
                dedupe_signature=('E_ENTRY_PRICE_CAP', '5000'),
            )
        blocked = [e for e in events if e['event'] == 'ORDER_BLOCKED']
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]['detail'], 'proposed=5001,cap=5000')

        tracker.observe_block(
            ('controller', 'DCE.p2701', 'OPEN'),
            active=False,
            contract_id='DCE.p2701',
            effect='OPEN',
        )
        unblocked = [e for e in events if e['event'] == 'ORDER_UNBLOCKED']
        self.assertEqual(len(unblocked), 1)
        self.assertEqual(unblocked[0]['previous_reason'], 'E_ENTRY_PRICE_CAP')
        self.assertEqual(unblocked[0]['previous_detail'], 'proposed=5001,cap=5000')

    def test_default_observe_block_still_tracks_detail_changes(self):
        tracker, events = _capture_tracker()
        tracker.observe_block(
            ('controller', 'DCE.p2701', 'OPEN'),
            active=True,
            contract_id='DCE.p2701',
            reason='SEND_RESULT_UNKNOWN',
            detail='child-1',
        )
        tracker.observe_block(
            ('controller', 'DCE.p2701', 'OPEN'),
            active=True,
            contract_id='DCE.p2701',
            reason='SEND_RESULT_UNKNOWN',
            detail='child-1,child-2',
        )
        self.assertEqual(len([e for e in events if e['event'] == 'ORDER_BLOCKED']), 2)


class RunnerDiagnosticKeyIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / 'journal.db'

    def _new_session(self, contracts=('DCE.p2701', 'CZCE.OI701'), account_key='DEV:diag-test'):
        session = DsvSession(
            self.db_path,
            account_key=account_key,
            provider='OFFLINE',
            trading_day='2026-11-25',
            profile=_make_profile(contracts),
            port=_NoopPort(),
        )
        self.addCleanup(session.close)
        return session

    def test_disarmed_ticks_do_not_oscillate_session_gate(self):
        session = self._new_session(('DCE.p2701', 'CZCE.OI701'), account_key='DEV:diag-disarmed')
        tracker, events = _capture_tracker()
        session._diagnostics = tracker

        for i in range(15):
            frame = {
                'id': f'f-{i}',
                'session_id': 's-1',
                'sequence': i + 1,
                'trading_day': '2026-11-25',
                'wall_time': f'2026-11-25T01:00:{i:02d}Z',
                'monotonic_ms': 1000 + i * 1000,
                'synchronized': True,
                'native': [],
                'sync_orders': [],
                'market': [_sample_market('DCE.p2701'), _sample_market('CZCE.OI701')],
            }
            session.apply_round(frame)

        blocked = [e for e in events if e['event'] == 'ORDER_BLOCKED' and e.get('reason') == 'USER_STOPPED']
        unblocked = [e for e in events if e['event'] == 'ORDER_UNBLOCKED']
        self.assertEqual(len(blocked), 2)
        self.assertEqual(len(unblocked), 0)

    def test_synchronize_uses_separate_keys_for_orders_and_positions(self):
        session = self._new_session(('DCE.p2701',), account_key='DEV:diag-sync')
        tracker, events = _capture_tracker()
        session._diagnostics = tracker
        state = session.snapshot()
        state['synchronized'] = False
        state['symbols']['DCE.p2701']['position'] = {
            'id': 'pos-1',
            'contract_id': 'DCE.p2701',
            'direction': 'LONG',
            'owned': 2,
            'open_day': '2026-11-25',
            'fills': [],
            'children': [],
        }

        for broker_qty in (1, 0, 1, 0):
            frame = {
                'native': [],
                'sync_orders': [],
                'market': [_sample_market('DCE.p2701', long_qty=broker_qty)],
            }
            session._synchronize(state, frame, set())

        self.assertIn(('synchronize_position', 'DCE.p2701'), tracker._active_blocks)
        self.assertNotIn(('synchronize_orders', 'DCE.p2701'), tracker._active_blocks)
        pos_blocks = [e for e in events if e['event'] == 'ORDER_BLOCKED' and e['reason'] == 'SYNCHRONIZE_POSITION_MISMATCH']
        self.assertEqual(len(pos_blocks), 1)


class WorkbenchTableBatchPerformanceTests(unittest.TestCase):
    def test_populate_and_fill_selectable_with_resize_to_contents_stay_fast(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QHeaderView
        from online_trader.desktop.workbench_layout import WorkbenchLayout, table

        app = QApplication.instance() or QApplication([])
        grid = table(('时间', '级别', '合约', '事件'))
        for col in (0, 1, 2):
            grid.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        grid.resize(950, 600)
        grid.show()
        app.processEvents()

        base_rows = [(f'20:51:{i % 60:02d}', '警告', 'DCE.p2701', f'事件 {i}') for i in range(200)]
        base_keys = [(row[3], i) for i, row in enumerate(base_rows)]
        WorkbenchLayout.fill_selectable(grid, base_rows, base_keys)
        grid.setCurrentCell(5, 0)
        selected_key = grid.item(5, 0).data(Qt.UserRole)

        durations_ms = []
        for step in range(5):
            shifted_rows = [(f'20:52:{step:02d}', '信息', 'CZCE.OI701', f'新事件 {step}-{i}') for i in range(200)]
            shifted_keys = [(row[3], step * 1000 + i) for i, row in enumerate(shifted_rows)]
            if step == 0:
                shifted_keys[10] = selected_key
            t0 = time.perf_counter()
            WorkbenchLayout.fill_selectable(grid, shifted_rows, shifted_keys)
            durations_ms.append((time.perf_counter() - t0) * 1000.0)
            if step == 0:
                self.assertEqual(grid.currentRow(), 10)
                self.assertEqual(grid.item(10, 0).data(Qt.UserRole), selected_key)

        self.assertEqual(grid.rowCount(), 200)
        self.assertEqual(grid.item(0, 3).text(), '新事件 4-0')
        self.assertEqual(grid.item(0, 3).toolTip(), '新事件 4-0')
        self.assertLess(max(durations_ms), 250.0, f'Expected fast batch update, got {durations_ms}')
        grid.close()


if __name__ == '__main__':
    unittest.main()
