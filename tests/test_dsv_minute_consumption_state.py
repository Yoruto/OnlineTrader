"""Verify the existing owner invariant instead of adding another entry gate.

Real DsvSession/controller/SQLite, synthetic quotes and RecordingPort. Reuse the
existing OFFLINE OS-lock fixture; this does not validate Windows IPC or the SDK.
"""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.runner import DsvSession
from qa.recording_port import RecordingPort
from tests.test_dsv_storage_read_isolation import CODE, DAY, SCOPE, frame, lease


class MinuteConsumptionStateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / 'facts.db'
        self.lock = self.enterContext(lease(self.root))
        self.port = RecordingPort()
        settings = load_confirmed_baseline()[0].to_settings()
        settings['symbols'] = [settings['symbols'][0] | {
            'code': CODE, 'open_offset': 40, 'stop_offset': 8,
            'profit_offset': 100, 'order_volume': 5}]
        settings['trade'].update(no_chase_after_act=True, continuation_entry=False,
            protect_80_enabled=True, protect_80_ratio=0.8, entry_slip_ratio=0.03,
            no_open_after_open=False, passive_entry_first=False)
        self.profile = DsvProfile.from_settings(settings)
        self.session = self.open_session()
        self.addCleanup(lambda: self.session.close())
        self.session.command({'id': 'start', 'action': 'START'})
        self.minute = CODE + ':20261116T0540'

    def open_session(self):
        return DsvSession(self.path, account_key=SCOPE['account_key'], provider='OFFLINE',
                          trading_day=DAY, profile=self.profile, port=self.port,
                          _execution_lock=self.lock)

    def observation(self, seq, *, high='1088', next_minute=False):
        value = frame(seq, price=1075)
        tick = value['market'][0]['ticks'][0]
        tick.update(day_high=high, upper_limit='1600')
        if next_minute:
            value['wall_time'] = (datetime.fromisoformat(value['wall_time']) + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
            value['monotonic_ms'] += 60_000
            tick.update(native_time=value['wall_time'], candle_id=CODE + ':20261116T0541')
        return value

    def assert_consumed_without_send(self, minute):
        symbol = self.session.snapshot()['symbols'][CODE]
        self.assertEqual(symbol['last_signal_bar'], minute)
        self.assertEqual(symbol['target_count'], 0)
        self.assertIsNone(symbol['position'])
        self.assertEqual(self.port.orders, [])

    def consume_by_chase_cap(self):
        first = self.session.apply_round(self.observation(1))
        self.assertEqual([item['reason'] for item in first['decisions']], ['CHASE_CAP_EXCEEDED'])
        self.assertEqual(first['decisions'][0]['consume'], self.minute)
        self.assertEqual(first['effects'], [])
        self.assert_consumed_without_send(self.minute)
        return first

    def test_other_reason_consumption_blocks_same_minute_activation(self):
        self.consume_by_chase_cap()
        result = self.session.apply_round(self.observation(2, high='1200'))
        self.assertEqual([item['reason'] for item in result['decisions']], ['GEOMETRY_BLOCK'])
        self.assertTrue(all('consume' not in item for item in result['decisions']))
        self.assertEqual(result['effects'], [])
        self.assert_consumed_without_send(self.minute)

    def test_consumption_survives_close_and_reopen_before_other_reason(self):
        self.consume_by_chase_cap()
        self.session.close()
        self.session = self.open_session()
        self.session.command({'id': 'restart', 'action': 'START'})
        result = self.session.apply_round(self.observation(2, high='1200'))
        self.assertEqual([item['reason'] for item in result['decisions']], ['GEOMETRY_BLOCK'])
        self.assertTrue(all('consume' not in item for item in result['decisions']))
        self.assert_consumed_without_send(self.minute)

    def test_next_minute_can_consume_once_without_reusing_previous_minute(self):
        self.consume_by_chase_cap()
        result = self.session.apply_round(self.observation(2, high='1200', next_minute=True))
        self.assertEqual([item['reason'] for item in result['decisions']],
                         ['GEOMETRY_BLOCK', 'NO_CHASE_AFTER_ACT'])
        self.assertEqual(result['decisions'][-1]['consume'], CODE + ':20261116T0541')
        again = self.session.apply_round(self.observation(3, high='1200', next_minute=True))
        self.assertEqual([item['reason'] for item in again['decisions']], ['GEOMETRY_BLOCK'])
        self.assertTrue(all('consume' not in item for item in again['decisions']))
        self.assert_consumed_without_send(CODE + ':20261116T0541')

    def test_duplicate_input_returns_receipt_without_duplicate_round_or_send(self):
        first = self.consume_by_chase_cap()
        again = self.session.apply_round(self.observation(1))
        self.assertEqual(again, first)
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM events WHERE kind='ROUND'").fetchone()[0], 1)
        self.assert_consumed_without_send(self.minute)


if __name__ == '__main__':
    unittest.main()
