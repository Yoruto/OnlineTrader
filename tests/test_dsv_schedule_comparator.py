import unittest

from qa.dsv_parity.scheduled_sdk_replay import compare


def round_trace(identity, targets=(), outbound=()):
    return {'id': identity, 'wait_update': True, 'wait_returned': True,
            'targets': list(targets), 'outbound': list(outbound)}


class ScheduledComparisonTests(unittest.TestCase):
    def assert_comparison(self, reference, candidate, expected):
        identities = {'extra': 'extra-id'}
        rows = compare({'rounds': reference, 'identities': identities},
                       {'rounds': candidate, 'identities': identities})
        self.assertEqual(all(row['match'] for row in rows), expected)

    def test_only_consecutive_restatements_are_collapsed(self):
        self.assert_comparison([round_trace('one', (0, 0))], [round_trace('one', (0,))], True)
        self.assert_comparison([round_trace('one', (0, 5, 0))], [round_trace('one', (0, 5))], False)
        self.assert_comparison([round_trace('one', (0, 5, 0, 0))], [round_trace('one', (0, 5, 0))], True)

    def test_extra_or_duplicate_round_cannot_hide_an_order(self):
        send = {'aid': 'insert_order', 'order_id': 'extra-id', 'direction': 'BUY', 'offset': 'OPEN',
                'volume': 1, 'limit_price': 1060}
        clean = round_trace('one')
        self.assert_comparison([clean], [clean, round_trace('two', outbound=(send,))], False)
        self.assert_comparison([clean], [round_trace('one', outbound=(send,)), clean], False)
        self.assert_comparison([clean, clean], [clean, clean], False)

    def test_missing_or_reordered_rounds_fail(self):
        one, two = round_trace('one'), round_trace('two')
        self.assert_comparison([one, two], [one], False)
        self.assert_comparison([one, two], [two, one], False)
