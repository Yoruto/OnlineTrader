"""Source _run 7825-7830: take-profit day stop, separate from trailing/floor.

Exercise real runner methods without constructing an account owner. No order,
connection, journal, clock, or account-lock method is invoked by these fixtures.
Full constructor/checkpoint and Windows integration are separate acceptance.
"""

from datetime import datetime
import unittest

from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.runner import DsvSession, initial_state


CODE = 'DCE.p2701'
DAY = '2026-11-13'
NOW = DAY + 'T01:10:00Z'


class ProfitStopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = DsvSession.__new__(DsvSession)
        self.runtime._trade = DsvProfile.from_dsv_json('{"symbols":[]}').to_record()['settings']['trade']
        self.runtime._trade.update(protect_80_enabled=False, protect_stop_day=False,
                                   half_close_enabled=False, trailing_activate_ratio='0',
                                   stale_force_close_seconds='0', stop_day_on_trailing=False,
                                   no_open_after_open=False, continuation_entry=False)
        self.runtime._sdk_port = None
        self.state = initial_state(DAY, [CODE])
        self.state['running'] = True
        self.row = self.state['symbols'][CODE]
        self.row.update(target_count=3, last_signal_bar='previous-minute')
        self.row['position'] = {
            'id': 'position-profit-test', 'contract_id': CODE, 'direction': 'LONG',
            'quantity': 4, 'owned': 4, 'entry_filled': 4, 'entry_notional': '4000',
            'entry_slip_checked': True, 'entry_stopped': True, 'children': [],
            'exit_target': None, 'exit_revision': 0, 'mode': 'ACTIVE', 'cap': '1000',
            'context_at': '0', 'theory': '1000', 'stop': '20', 'profit': '100',
            'peak': '0', 'protected': False, 'half_stage': 0,
            'submitted_at': str(datetime.fromisoformat(NOW).timestamp()),
            'entry_kind': 'NORMAL_ENTRY', 'open_day': DAY, 'fills': [],
        }
        self.spec = {'open_offset': '60', 'stop_offset': '20', 'profit_offset': '100',
                     'half_close_ratio': '0.4', 'hysteresis_points': '0', 'order_volume': 4}
        self.market = {
            'contract_id': CODE, 'long_qty': 4, 'short_qty': 0, 'opening_session': True,
            'session_open': DAY + 'T01:00:00Z', 'session_close': DAY + 'T02:15:00Z',
            'tick': {'last': '1100', 'native_time': NOW, 'candle_id': 'next-minute',
                     'day_high': '1100', 'day_low': '1000'},
        }
        self.frame = {'wall_time': NOW, 'monotonic_ms': '1000'}

    def maintain(self, price='1100'):
        self.market['tick']['last'] = price
        return self.runtime._maintain_position(self.row, self.row['position'], self.market, self.frame, self.spec)

    def test_default_profit_stop_is_set_before_any_fill_confirmation(self):
        events = self.maintain()
        self.assertEqual(events[-1]['reason'], 'TAKE_PROFIT')
        self.assertTrue(self.row['day_stop'])
        self.assertEqual(self.row['day_stop_reason'], '止盈触发')
        self.assertEqual(self.row['position']['owned'], 4)
        self.assertEqual(self.row['position']['exit_target'], 0)

    def test_explicit_true_stops(self):
        self.runtime._trade['stop_day_on_profit'] = True
        self.maintain()
        self.assertTrue(self.row['day_stop'])

    def test_explicit_false_keeps_ordinary_profit_stop_disabled(self):
        self.runtime._trade['stop_day_on_profit'] = False
        self.maintain()
        self.assertFalse(self.row['day_stop'])
        self.assertEqual(self.row['position']['exit_target'], 0)

    def test_short_profit_also_stops(self):
        self.row['position']['direction'] = 'SHORT'
        self.maintain('900')
        self.assertTrue(self.row['day_stop'])
        self.assertEqual(self.row['day_stop_reason'], '止盈触发')

    def test_full_take_profit_after_protection_is_still_take_profit(self):
        self.row['position']['protected'] = True
        self.maintain()
        self.assertTrue(self.row['day_stop'])

    def test_protected_floor_is_not_misclassified_as_ordinary_take_profit(self):
        self.row['position'].update(protected=True, peak='90')
        events = self.maintain('1040')
        self.assertEqual(events[-1]['reason'], 'PROTECTED_FLOOR')
        self.assertFalse(self.row['day_stop'])

    def test_trailing_exit_does_not_use_profit_stop_switch(self):
        self.runtime._trade['trailing_activate_ratio'] = '0.5'
        self.row['position']['peak'] = '80'
        events = self.maintain('1060')
        self.assertEqual(events[-1]['reason'], 'TRAILING_STOP')
        self.assertFalse(self.row['day_stop'])

    def test_trailing_retains_its_own_stop_switch(self):
        self.runtime._trade.update(trailing_activate_ratio='0.5', stop_day_on_trailing=True,
                                   stop_day_on_profit=False)
        self.row['position']['peak'] = '80'
        self.maintain('1060')
        self.assertTrue(self.row['day_stop'])

    def test_existing_day_stop_reason_is_not_overwritten(self):
        self.row.update(day_stop=True, day_stop_reason='existing-source-cause')
        self.maintain()
        self.assertEqual(self.row['day_stop_reason'], 'existing-source-cause')

    def test_stop_loss_does_not_trigger_profit_stop(self):
        events = self.maintain('980')
        self.assertEqual(events[-1]['reason'], 'STOP_LOSS')
        self.assertFalse(self.row['day_stop'])

    def test_partial_profit_does_not_trigger_profit_stop(self):
        self.runtime._trade['half_close_enabled'] = True
        events = self.maintain('1045')
        self.assertEqual(events[-1]['reason'], 'PARTIAL_STAGE_1')
        self.assertFalse(self.row['day_stop'])

    def test_repeated_quote_does_not_double_book_or_consume_entry(self):
        self.maintain()
        before = (self.row['net_loss'], self.row['position']['exit_revision'])
        self.maintain()
        self.assertEqual((self.row['net_loss'], self.row['position']['exit_revision']), before)
        self.assertEqual(self.row['target_count'], 3)
        self.assertEqual(self.row['last_signal_bar'], 'previous-minute')

    def test_next_ordinary_opportunity_is_blocked_without_consumption(self):
        self.maintain()
        # Isolate the next entry evaluation after an independently confirmed flat.
        # This fixture does not claim to test broker/Journal flat reconciliation.
        self.row['position'] = None
        self.market['long_qty'] = 0
        self.market['tick'].update(last='1000', day_high='1050', day_low='950')
        events = self.runtime._evaluate_symbol(self.state, self.row, self.market, self.frame, self.spec)
        self.assertEqual(events[0]['reason'], 'DAY_STOP')
        self.assertEqual(self.row['target_count'], 3)
        self.assertEqual(self.row['last_signal_bar'], 'previous-minute')

    def test_explicit_false_does_not_block_the_next_ordinary_evaluation(self):
        self.runtime._trade['stop_day_on_profit'] = False
        self.maintain()
        self.row['position'] = None
        self.market['long_qty'] = 0
        self.market['tick'].update(last='1000', day_high='1050', day_low='950')
        events = self.runtime._evaluate_symbol(self.state, self.row, self.market, self.frame, self.spec)
        self.assertEqual(events[0]['reason'], 'TRIGGER_NOT_REACHED')

    def test_statistics_preserves_the_profit_stop(self):
        self.maintain()
        self.runtime._update_stops(self.row, CODE, DAY, self.spec, [DAY])
        self.assertTrue(self.row['day_stop'])
        self.assertEqual(self.row['day_stop_reason'], '止盈触发')

    def test_normal_profit_stop_clears_on_real_day_transition_not_repeated_quote(self):
        self.maintain()
        self.assertTrue(self.row['day_stop'])
        self.runtime._roll_day(self.state, '2026-11-16')
        self.assertFalse(self.row['day_stop'])
        self.assertIsNone(self.row.get('day_stop_reason'))
        self.assertEqual(self.row['position']['owned'], 4)
        self.assertEqual(self.row['position']['exit_target'], 0)


if __name__ == '__main__':
    unittest.main()
