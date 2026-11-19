"""Real runner methods and Journal; no AccountLock constructor, SDK or orders.

A no-order controller double accepts EMPTY native/effect lists only. Entry-rule
calls are explicitly mocked in the routing tests. This is not a broker test.
"""

import copy
from pathlib import Path
import sqlite3
import tempfile
from threading import get_ident
import unittest
from unittest.mock import patch

from online_trader.dsv_core.business_statistics import MONTH_STOP_REASON
from online_trader.dsv_runtime.runner import DsvSession, initial_state
from online_trader.dsv_storage.journal import Journal
from online_trader.errors import TraderError


CODE = 'DCE.p2701'
DAY = '2026-11-13'
DAYS = ['2026-11-10', '2026-11-11', '2026-11-12', DAY, '2026-11-16']


class NoOrderController:
    def accept_native(self, state, facts):
        if facts:
            raise AssertionError('This fixture must not consume broker facts')

    def settle(self, state, row):
        pass

    def dispatch(self, state, effects):
        if effects:
            raise AssertionError('This fixture must not dispatch')

    def plan(self, *args):
        raise AssertionError('This fixture has no executable market')


class MonthRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'facts.db'
        self.scope = {'account_key': 'DEV:month-observation', 'provider': 'OFFLINE',
                      'trading_day': DAY, 'configuration_id': 'unchanged-profile'}
        self.runtime = self.owner(initial_state(DAY, [CODE]))
        self.addCleanup(lambda: self.runtime._journal.close())

    def owner(self, initial):
        runtime = DsvSession.__new__(DsvSession)
        runtime._owner = get_ident()
        runtime._closed = runtime._failed = runtime._busy = False
        runtime._sdk_port = None
        runtime._scope = self.scope
        runtime._symbols = {CODE: {'stop_offset': '20'}}
        runtime._trade = {'month_stop_days': 4, 'max_net_loss_stops': 2,
                          'continuation_entry': False, 'cont_stop_day_stop': True}
        runtime._controller = NoOrderController()
        runtime._journal = Journal(self.path, self.scope, initial)
        runtime._state = runtime._journal.load()
        return runtime

    def row(self):
        return self.runtime._state['symbols'][CODE]

    def save(self):
        with self.runtime._journal.transaction():
            self.runtime._journal.save(self.runtime._state)

    def round(self, day=DAY):
        seq = self.runtime._state['sequence'] + 1
        frame = {'id': f'round-{seq}', 'sequence': seq, 'session_id': 'source-session',
                 'monotonic_ms': str(seq * 1000), 'wall_time': day + 'T01:10:00Z',
                 'trading_day': day, 'trading_days': DAYS + ['2026-12-01'],
                 'synchronized': True, 'native': [], 'sync_orders': [],
                 'market': [{'contract_id': CODE, 'long_qty': 0, 'short_qty': 0, 'ticks': [],
                             'opening_session': True, 'session_open': day + 'T01:00:00Z',
                             'session_close': day + 'T02:15:00Z'}]}
        return self.runtime.apply_round(frame), frame

    def test_roll_day_clears_day_stop_but_keeps_month_history(self):
        row = self.row()
        row.update(day_stop=True, day_stop_reason=MONTH_STOP_REASON, month_stop='2026-11', stop_days=DAYS[:4])
        self.runtime._roll_day(self.runtime._state, '2026-11-16')
        self.runtime._update_stops(row, CODE, '2026-11-16', {'stop_offset': '20'}, DAYS)
        self.assertFalse(row['day_stop'])
        self.assertEqual(row['month_stop'], '2026-11')
        self.assertEqual(row['stop_days'], DAYS[:4])

    def test_month_marker_is_not_an_independent_entry_gate(self):
        state = self.runtime._state
        state['running'] = True
        self.row()['month_stop'] = '2026-11'
        market = {'contract_id': CODE, 'long_qty': 0, 'short_qty': 0, 'tick': {'candle_id': 'minute'}}
        with patch('online_trader.dsv_runtime.runner.continuation_entry', return_value=None), \
             patch('online_trader.dsv_runtime.runner.ordinary_entry', return_value={'reason': 'TRIGGER_NOT_REACHED'}) as rule:
            result = self.runtime._evaluate_symbol(state, self.row(), market, {}, {})
        rule.assert_called_once()
        self.assertEqual(result[0]['reason'], 'TRIGGER_NOT_REACHED')

    def test_month_marker_does_not_bypass_user_stop(self):
        self.row()['month_stop'] = '2026-11'
        result = self.runtime._evaluate_symbol(self.runtime._state, self.row(),
                                              {'contract_id': CODE, 'tick': None}, {}, {})
        self.assertEqual(result[0]['reason'], 'USER_STOPPED')

    def test_loss_after_symbol_phase_is_not_recorded_as_monthly_until_next_round(self):
        row = self.row()
        row.update(net_loss='40', stop_days=DAYS[:3])
        self.runtime._update_stops(row, CODE, DAY, {'stop_offset': '20'}, DAYS)
        self.assertTrue(row['day_stop'])
        self.assertEqual(row['stop_days'], DAYS[:3])
        self.assertIsNone(row['month_stop'])
        self.round()
        self.assertEqual(self.row()['stop_days'], DAYS[:4])
        self.assertEqual(self.row()['month_stop'], '2026-11')
        self.assertEqual(self.row()['day_stop_reason'], MONTH_STOP_REASON)

    def test_first_new_day_round_records_previous_loss_before_reset(self):
        self.row().update(net_loss='40', stop_days=DAYS[:3])
        self.round('2026-11-16')
        self.assertEqual(self.row()['stop_days'], DAYS[:4])
        self.assertEqual(self.row()['month_stop'], '2026-11')
        self.assertEqual(self.row()['net_loss'], '0')
        self.assertFalse(self.row()['day_stop'])

    def test_subsequent_round_does_not_restore_whole_month_stop(self):
        self.row().update(day_stop=True, day_stop_reason=MONTH_STOP_REASON,
                          net_loss='40', month_stop='2026-11', stop_days=DAYS[:4])
        self.round('2026-11-16')
        self.round('2026-11-16')
        self.assertFalse(self.row()['day_stop'])
        self.assertEqual(self.row()['month_stop'], '2026-11')

    def test_current_day_independent_loss_still_stops(self):
        row = self.row()
        row.update(month_stop='2026-11', net_loss='40')
        self.runtime._update_stops(row, CODE, DAY, {'stop_offset': '20'}, DAYS)
        self.assertTrue(row['day_stop'])
        self.assertEqual(row['day_stop_reason'], '净亏达40点')

    def test_existing_stop_reason_is_not_overwritten_by_post_phase(self):
        row = self.row()
        row.update(month_stop='2026-11', net_loss='40', day_stop=True, day_stop_reason=MONTH_STOP_REASON)
        self.runtime._update_stops(row, CODE, DAY, {'stop_offset': '20'}, DAYS)
        self.assertEqual(row['day_stop_reason'], MONTH_STOP_REASON)

    def test_disabled_month_rule_does_not_restore_day_stop(self):
        self.runtime._trade['month_stop_days'] = 0
        self.row().update(month_stop='2026-11', day_stop=False)
        self.round()
        self.assertFalse(self.row()['day_stop'])
        self.assertEqual(self.row()['month_stop'], '2026-11')

    def test_cross_month_clear_occurs_in_next_observation_not_in_roll(self):
        self.row().update(month_stop='2026-11')
        self.round('2026-12-01')
        self.assertEqual(self.row()['month_stop'], '2026-11')
        self.round('2026-12-01')
        self.assertIsNone(self.row()['month_stop'])

    def test_disabled_rule_does_not_clear_marker_during_roll(self):
        self.runtime._trade['month_stop_days'] = 0
        self.row().update(month_stop='2026-11')
        self.round('2026-12-01')
        self.round('2026-12-01')
        self.assertEqual(self.row()['month_stop'], '2026-11')

    def test_journal_reopen_keeps_same_day_stop_and_month_marker(self):
        self.row().update(net_loss='40', stop_days=DAYS[:3])
        self.round()
        self.runtime._journal.close()
        self.runtime = self.owner(initial_state(DAY, [CODE]))
        self.assertTrue(self.row()['day_stop'])
        self.assertEqual(self.row()['month_stop'], '2026-11')
        self.round()
        self.assertTrue(self.row()['day_stop'])

    def test_journal_reopen_after_day_roll_does_not_resurrect_day_stop(self):
        self.row().update(net_loss='40', stop_days=DAYS[:3])
        self.round('2026-11-16')
        self.runtime._journal.close()
        self.runtime = self.owner(initial_state(DAY, [CODE]))
        self.round('2026-11-16')
        self.assertFalse(self.row()['day_stop'])
        self.assertEqual(self.row()['stop_days'], DAYS[:4])

    def test_duplicate_round_uses_original_receipt_without_double_observation(self):
        self.row().update(net_loss='40', stop_days=DAYS[:3])
        first, frame = self.round()
        before = copy.deepcopy(self.runtime._state)
        second = self.runtime.apply_round(copy.deepcopy(frame))
        self.assertEqual(first, second)
        self.assertEqual(before, self.runtime._state)
        count = self.runtime._journal._db.execute("SELECT COUNT(*) FROM events WHERE kind='ROUND'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_failed_required_save_rolls_back_monthly_observation(self):
        self.row().update(net_loss='40', stop_days=DAYS[:3])
        self.save()
        before = self.runtime._journal.load()
        with patch.object(self.runtime._journal, 'save_checkpoint', side_effect=sqlite3.OperationalError('disk fixture')):
            with self.assertRaises(TraderError):
                self.round()
        self.assertEqual(before, self.runtime._journal.load())
        self.assertEqual(before, self.runtime._state)
        self.assertTrue(self.runtime._failed)
        self.assertEqual(self.runtime._journal._db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)

    def test_roll_does_not_erase_unknown_order_or_owned_exit(self):
        row = self.row()
        row.update(month_stop='2026-11', position={'owned': 3, 'exit_target': 0, 'children': ['unknown']})
        unknown = {'child_order_id': 'unknown', 'send_state': 'UNKNOWN', 'original_qty': 2, 'filled_qty': 0}
        self.runtime._state['orders']['unknown'] = copy.deepcopy(unknown)
        self.runtime._roll_day(self.runtime._state, '2026-11-16')
        self.assertEqual(self.runtime._state['orders']['unknown'], unknown)
        self.assertEqual(row['position']['owned'], 3)
        self.assertEqual(row['position']['exit_target'], 0)
        self.assertFalse(self.runtime._state['synchronized'])


if __name__ == '__main__':
    unittest.main()
