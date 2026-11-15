"""D10 monthly observation from frozen _run 6005-6040; no owner or SDK."""

import copy
import unittest

from online_trader.dsv_core.business_statistics import MONTH_STOP_REASON
from online_trader.dsv_core.month_stop import observe_month_stop


DAYS = ['2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12', '2026-11-13', '2026-11-16']


class MonthObservationTests(unittest.TestCase):
    def observe(self, day='2026-11-13', **changes):
        inputs = dict(loss='40', stop_offset='20', trade={'month_stop_days': 4, 'max_net_loss_stops': 2},
                      history=DAYS[1:4], month_stop=None, day_stopped=False,
                      day_stopped_reason=None, calendar=DAYS)
        inputs.update(changes)
        before = copy.deepcopy(inputs)
        result = observe_month_stop(day, **inputs)
        self.assertEqual(inputs, before, 'the pure observation mutated its inputs')
        return result

    def test_first_qualifying_observation_sets_month_and_day_stop(self):
        result = self.observe()
        self.assertEqual(result['month_stop'], '2026-11')
        self.assertTrue(result['day_stopped'])
        self.assertEqual(result['day_stopped_reason'], MONTH_STOP_REASON)

    def test_existing_month_marker_does_not_reapply_day_stop(self):
        result = self.observe('2026-11-16', loss='0', month_stop='2026-11', history=DAYS[1:5])
        self.assertFalse(result['day_stopped'])
        self.assertIsNone(result['day_stopped_reason'])
        self.assertEqual(result['month_stop'], '2026-11')

    def test_existing_month_marker_preserves_independent_stop(self):
        result = self.observe(loss='0', month_stop='2026-11', day_stopped=True,
                              day_stopped_reason='止盈触发')
        self.assertEqual(result['day_stopped_reason'], '止盈触发')
        self.assertTrue(result['day_stopped'])

    def test_same_month_still_records_a_new_loss_day_before_skip(self):
        result = self.observe('2026-11-16', month_stop='2026-11', history=DAYS[1:5])
        self.assertEqual(result['stop_days'], DAYS[1:])
        self.assertFalse(result['day_stopped'])

    def test_recorded_history_can_trigger_after_current_loss_recovered(self):
        result = self.observe(loss='0', history=DAYS[1:5])
        self.assertTrue(result['day_stopped'])
        self.assertEqual(result['month_stop'], '2026-11')

    def test_loss_below_threshold_does_not_append(self):
        result = self.observe(loss='39.99')
        self.assertEqual(result['stop_days'], DAYS[1:4])
        self.assertFalse(result['day_stopped'])

    def test_float_boundary_retains_original_operation_order(self):
        result = self.observe(loss='0.3', stop_offset='0.1', history=[],
                              trade={'month_stop_days': 1, 'max_net_loss_stops': 3})
        self.assertEqual(result['stop_days'], [])
        self.assertFalse(result['day_stopped'])

    def test_missing_loss_day_breaks_consecutive_chain(self):
        self.assertFalse(self.observe(history=DAYS[1:2] + DAYS[3:4])['day_stopped'])

    def test_source_disabled_block_preserves_every_monthly_field(self):
        result = self.observe(month_stop='2026-10', day_stopped=True, day_stopped_reason=MONTH_STOP_REASON,
                              trade={'month_stop_days': 0, 'max_net_loss_stops': 2})
        self.assertEqual(result, {'stop_days': DAYS[1:4], 'month_stop': '2026-10',
                                  'day_stopped': True, 'day_stopped_reason': MONTH_STOP_REASON})

    def test_new_month_clears_only_month_stop_cause(self):
        result = self.observe('2026-12-01', loss='0', month_stop='2026-11',
                              day_stopped=True, day_stopped_reason=MONTH_STOP_REASON, history=[])
        self.assertIsNone(result['month_stop'])
        self.assertFalse(result['day_stopped'])
        self.assertIsNone(result['day_stopped_reason'])

    def test_new_month_keeps_other_stop_causes(self):
        result = self.observe('2026-12-01', loss='0', month_stop='2026-11',
                              day_stopped=True, day_stopped_reason='止盈触发', history=[])
        self.assertIsNone(result['month_stop'])
        self.assertTrue(result['day_stopped'])
        self.assertEqual(result['day_stopped_reason'], '止盈触发')

    def test_missing_calendar_does_not_invent_days_or_extra_gate(self):
        result = self.observe(calendar=[])
        self.assertEqual(result['stop_days'], DAYS[1:5])
        self.assertFalse(result['day_stopped'])

    def test_calendar_must_include_the_observed_saved_day(self):
        self.assertFalse(self.observe(calendar=DAYS[:4])['day_stopped'])

    def test_future_calendar_dates_do_not_fill_a_history_gap(self):
        self.assertFalse(self.observe(history=['2026-11-12'])['day_stopped'])

    def test_weekend_holiday_gap_uses_explicit_trading_dates(self):
        days = ['2026-11-06', '2026-11-09', '2026-11-11', '2026-11-13']
        result = self.observe(history=days[:-1], calendar=days)
        self.assertTrue(result['day_stopped'])

    def test_40_civil_day_window_includes_day_minus_39(self):
        days = ['2026-10-05', '2026-11-13']
        result = self.observe(history=days, calendar=days, loss='0',
                              trade={'month_stop_days': 2, 'max_net_loss_stops': 2})
        self.assertTrue(result['day_stopped'])

    def test_40_civil_day_window_excludes_day_minus_40(self):
        days = ['2026-10-04', '2026-11-13']
        result = self.observe(history=days, calendar=days, loss='0',
                              trade={'month_stop_days': 2, 'max_net_loss_stops': 2})
        self.assertFalse(result['day_stopped'])

    def test_append_trims_to_last_12_without_reordering(self):
        history = [f'2026-10-{day:02}' for day in range(1, 14)]
        result = self.observe(history=history, calendar=[])
        self.assertEqual(result['stop_days'], (history + ['2026-11-13'])[-12:])

    def test_duplicate_day_does_not_append_or_trim_old_history(self):
        history = [f'2026-10-{day:02}' for day in range(1, 14)] + ['2026-11-13']
        result = self.observe(history=history, calendar=[])
        self.assertEqual(result['stop_days'], history)

    def test_zero_daily_threshold_does_not_erase_recorded_month_history(self):
        result = self.observe(loss='0', history=DAYS[1:5],
                              trade={'month_stop_days': 4, 'max_net_loss_stops': 0})
        self.assertTrue(result['day_stopped'])


if __name__ == '__main__':
    unittest.main()
