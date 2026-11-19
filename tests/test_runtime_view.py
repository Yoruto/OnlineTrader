"""Versioned Runtime view contract tests independent of a trading owner."""

import time
import unittest
from unittest.mock import patch

from online_trader.errors import TraderError
from online_trader.transport.runtime_view import ViewHistory, ViewReplica


def publication(boot, sequence, *, authority='DISARMED'):
    return {
        'published_ns': sequence,
        'base': {'records': [{'record_type': 'ACCOUNT', 'record_id': 'DEV:runtime-view',
                              'record_revision': str(sequence)}]},
        'view': {'account_key': 'DEV:runtime-view', 'kernel_boot_id': boot,
                 'entry_authority': authority, 'markets': [], 'cycles': [], 'orders': [],
                 'trades': [], 'commands': [], 'status_projection': {'not': 'published'}},
    }


class RuntimeViewContractTests(unittest.TestCase):
    def test_baseline_contiguous_delta_and_current_reconstruct_one_state(self):
        history, replica = ViewHistory(), ViewReplica()
        now = time.monotonic_ns()
        history.publish(publication('boot-1', now))
        baseline = history.response(None)
        self.assertEqual(baseline['kind'], 'BASELINE')
        self.assertTrue(replica.apply(baseline))
        history.publish(publication('boot-1', now + 1, authority='ARMED'))
        delta = history.response(replica.cursor)
        self.assertEqual(delta['kind'], 'DELTA')
        self.assertTrue(replica.apply(delta))
        self.assertEqual(replica.state['view']['entry_authority'], 'ARMED')
        self.assertEqual(history.response(replica.cursor)['kind'], 'CURRENT')

    def test_gap_or_wrong_generation_cannot_be_applied_as_current_state(self):
        history, replica = ViewHistory(), ViewReplica()
        now = time.monotonic_ns()
        history.publish(publication('boot-1', now))
        replica.apply(history.response(None))
        history.publish(publication('boot-1', now + 1, authority='ARMED'))
        delta = history.response(replica.cursor)
        delta['cursor'] = delta['cursor'] | {'sequence': '99'}
        with self.assertRaises(TraderError) as error:
            replica.apply(delta)
        self.assertEqual(error.exception.code, 'E_SNAPSHOT')

    def test_owner_boot_change_requires_a_new_baseline_generation(self):
        history = ViewHistory()
        now = time.monotonic_ns()
        history.publish(publication('boot-1', now))
        first = history.response(None)
        history.publish(publication('boot-2', now + 1))
        second = history.response(first['cursor'])
        self.assertEqual(second['kind'], 'BASELINE')
        self.assertNotEqual(first['cursor']['generation'], second['cursor']['generation'])
        self.assertEqual(second['body']['view']['kernel_boot_id'], 'boot-2')

    def test_expired_publication_is_unavailable_and_contains_no_state(self):
        history = ViewHistory()
        history.publish(publication('boot-1', 1))
        with patch('online_trader.transport.runtime_view.time.monotonic_ns', return_value=10**20):
            result = history.response(None)
        self.assertEqual(result['kind'], 'UNAVAILABLE')
        self.assertIsNone(result['cursor'])
        self.assertIsNone(result['body'])


if __name__ == '__main__':
    unittest.main()
