"""No-trade parity must compare source evidence, not merely empty order lists."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from qa.dsv_parity.compare_original_loop import compare

CODE = 'DCE.p2701'
SCHEMA = 'DSV_ENTRY_TRACE/1'


def fixture():
    packet = {'settings': {'trade': {'min_days_to_expiry': 5}}, 'rounds': [
        {'id': 'round-1', 'market': [{'contract_id': CODE, 'long_qty': 0, 'short_qty': 0}]}]}
    decisions = [{'contract_id': CODE, 'reason': 'GEOMETRY_BLOCK'},
                 {'contract_id': CODE, 'reason': 'NO_CHASE_AFTER_ACT', 'direction': 'LONG',
                  'trigger_price': '8981', 'activation_price': '9039.4', 'consume': '10:55'}]
    reference = {'id': 'round-1', 'targets': [], 'entry_trace_schema': SCHEMA,
                 'entry_rejections': deepcopy(decisions), 'state': {CODE: {
                     'no_open_reason': None, 'net_loss_points': 0, 'day_stopped': False,
                     'close_booked': False, 'last_signal_bar': '10:55'}}}
    current = {'id': 'round-1', 'effects': [], 'decisions': deepcopy(decisions),
               'symbols': {CODE: {'net_loss': '0', 'day_stop': False, 'position': None,
                                  'last_signal_bar': '10:55'}}}
    return packet, reference, current


class EntryComparisonTests(unittest.TestCase):
    def setUp(self):
        self.packet, self.reference, self.current = fixture()

    def differences(self):
        return compare(self.packet, [self.reference], [self.current])[0]['differences']

    def test_matching_no_trade_evidence_passes(self):
        self.assertEqual(self.differences(), [])

    def test_missing_rejection_is_detected_even_with_zero_orders(self):
        self.current['decisions'].pop()
        self.assertIn('entry_rejections', self.differences())

    def test_extra_rejection_is_detected(self):
        self.current['decisions'].append(deepcopy(self.current['decisions'][-1]))
        self.assertIn('entry_rejections', self.differences())

    def test_reordered_rejections_are_detected(self):
        self.current['decisions'].reverse()
        self.assertIn('entry_rejections', self.differences())

    def test_wrong_direction_is_detected(self):
        self.current['decisions'][-1]['direction'] = 'SHORT'
        self.assertIn('entry_rejections', self.differences())

    def test_wrong_trigger_is_detected(self):
        self.current['decisions'][-1]['trigger_price'] = '8982'
        self.assertIn('entry_rejections', self.differences())

    def test_wrong_activation_is_detected(self):
        self.current['decisions'][-1]['activation_price'] = '9040'
        self.assertIn('entry_rejections', self.differences())

    def test_wrong_consumed_bar_is_detected(self):
        self.current['decisions'][-1]['consume'] = '10:56'
        self.assertIn('entry_rejections', self.differences())

    def test_missing_minute_state_is_not_equal_to_unused(self):
        del self.current['symbols'][CODE]['last_signal_bar']
        self.assertIn('entry_evidence_missing', self.differences())

    def test_different_minute_state_is_detected(self):
        self.current['symbols'][CODE]['last_signal_bar'] = None
        self.assertIn('last_signal_bar', self.differences())

    def test_missing_reference_minute_state_fails_closed(self):
        del self.reference['state'][CODE]['last_signal_bar']
        self.assertIn('entry_evidence_missing', self.differences())

    def test_old_reference_without_trace_is_not_reported_equal(self):
        del self.reference['entry_trace_schema']
        del self.reference['entry_rejections']
        self.assertIn('entry_evidence_missing', self.differences())

    def test_missing_candidate_decisions_fails_closed(self):
        del self.current['decisions']
        self.assertIn('entry_evidence_missing', self.differences())

    def test_price_format_is_not_a_behavior_difference(self):
        self.current['decisions'][-1].update(trigger_price='8981.0', activation_price='9039.400')
        self.assertEqual(self.differences(), [])

    def test_nan_metadata_is_invalid_not_equal(self):
        self.current['decisions'][-1]['activation_price'] = 'NaN'
        self.assertIn('entry_evidence_invalid', self.differences())

    def test_missing_activation_metadata_is_invalid(self):
        del self.current['decisions'][-1]['direction']
        self.assertIn('entry_evidence_invalid', self.differences())

    def test_old_target_difference_is_still_checked(self):
        self.current['effects'] = [{'kind': 'SEND', 'contract_id': CODE, 'quantity': 1, 'direction': 'BUY'}]
        self.assertIn('targets', self.differences())

    def test_noncovered_reasons_do_not_claim_full_reason_parity(self):
        self.current['decisions'].append({'contract_id': CODE, 'reason': 'USER_STOPPED'})
        self.assertEqual(self.differences(), [])  # Only the documented two-reason scope is compared.


class FrozenObservationTests(unittest.TestCase):
    """Observer mechanics with explicit source-locals fixtures, not a source replay."""
    def setUp(self):
        self.state = {'last_signal_bar': '10:55'}
        self.values = {'cont_code': CODE, 'st': self.state, 'state': {CODE: self.state},
                       'lb': '10:55', '_geo5': False, '_hit_long': True, '_hit_short': True,
                       'lo_open': 8981.0, 'so_open': 9041.0, '_act_off': 58.4}

    def observe(self, line):
        from qa.dsv_parity.entry_evidence import source_rejection
        return source_rejection(line, self.values)

    def test_source_geometry_uses_computed_flag(self):
        self.assertEqual(self.observe(8027), {'contract_id': CODE, 'reason': 'GEOMETRY_BLOCK'})
        self.values['_geo5'] = True
        self.assertIsNone(self.observe(8027))

    def test_long_priority_uses_executed_source_flags(self):
        self.assertEqual(self.observe(8062), {'contract_id': CODE, 'reason': 'NO_CHASE_AFTER_ACT',
            'direction': 'LONG', 'trigger_price': '8981.0', 'activation_price': '9039.4', 'consume': '10:55'})

    def test_short_direction_and_level(self):
        self.values['_hit_long'] = False
        row = self.observe(8062)
        self.assertEqual((row['direction'], row['trigger_price'], row['activation_price']),
                         ('SHORT', '9041.0', '8982.6'))

    def test_text_logging_locals_are_not_required_or_trusted(self):
        expected = self.observe(8062)
        self.values.update(_w='stale', _trig=1, _lvl=2)
        self.assertEqual(self.observe(8062), expected)

    def test_same_text_throttle_does_not_hide_next_minute(self):
        first = self.observe(8062)
        self.values['lb'] = self.state['last_signal_bar'] = '10:56'
        second = self.observe(8062)
        self.assertNotEqual(first['consume'], second['consume'])

    def test_source_state_is_not_mutated(self):
        before = deepcopy(self.values)
        self.observe(8027); self.observe(8062)
        self.assertEqual(self.values, before)

    def test_uncommitted_minute_does_not_get_recorded(self):
        self.state['last_signal_bar'] = None
        with self.assertRaises(ValueError): self.observe(8062)

    def test_wrong_contract_row_does_not_get_recorded(self):
        self.values['st'] = {'last_signal_bar': '10:55'}
        with self.assertRaises(ValueError): self.observe(8062)

    def test_unreached_line_is_not_a_decision(self):
        self.assertIsNone(self.observe(8034))

    def test_no_direction_is_an_evidence_error(self):
        self.values.update(_hit_long=False, _hit_short=False)
        with self.assertRaises(ValueError): self.observe(8062)

    def test_trace_error_escapes_source_exception_handlers(self):
        from qa.dsv_parity.entry_evidence import FrozenEntryObserver, SourceEvidenceError
        code = SimpleNamespace(co_lines=lambda: [(0, 2, 8027), (2, 4, 8062)])
        feed = SimpleNamespace(frame={'id': 'test'}, entry_rejections=[])
        observer = FrozenEntryObserver(code, feed)
        frame = SimpleNamespace(f_code=code, f_lineno=8062, f_locals={})
        with self.assertRaises(SourceEvidenceError): observer(frame, 'line', None)
        self.assertFalse(issubclass(SourceEvidenceError, Exception))

    def test_observer_ignores_other_frames(self):
        from qa.dsv_parity.entry_evidence import FrozenEntryObserver
        code = SimpleNamespace(co_lines=lambda: [(0, 2, 8027), (2, 4, 8062)])
        feed = SimpleNamespace(frame={'id': 'test'}, entry_rejections=[])
        observer = FrozenEntryObserver(code, feed)
        self.assertIsNone(observer(SimpleNamespace(f_code=object()), 'call', None))
        self.assertEqual(feed.entry_rejections, [])

    def test_missing_frozen_site_is_refused(self):
        from qa.dsv_parity.entry_evidence import FrozenEntryObserver
        with self.assertRaises(ValueError):
            FrozenEntryObserver(SimpleNamespace(co_lines=lambda: []), SimpleNamespace())


if __name__ == '__main__': unittest.main()
