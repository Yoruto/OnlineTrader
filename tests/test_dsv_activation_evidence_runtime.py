"""Real DEV session/controller/SQLite with test-only Linux lease and no SDK.

Windows uses the product's original account lock. The Linux lease is injected
only via the existing offline constructor seam; it is not a production lock.
"""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.runner import DsvSession
from qa.dsv_parity.activation_samples import CODE, samples
from qa.recording_port import RecordingPort


class _PosixTestLease:
    def __init__(self, path):
        import fcntl
        self.stream = path.open('w')
        fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def close(self): self.stream.close()


class ActivationEvidenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.packet = samples()['morning-activation']
        self.port = RecordingPort()
        self.kwargs = {}
        if sys.platform != 'win32':
            lease = _PosixTestLease(self.root/'test-lease')
            self.stack.callback(lease.close)
            self.kwargs['_execution_lock'] = lease

    def session(self):
        return DsvSession(self.root/'facts.db', account_key='DEV:activation-evidence', provider='OFFLINE',
                          trading_day=self.packet['trading_day'], profile=DsvProfile.from_settings(self.packet['settings']),
                          port=self.port, **self.kwargs)

    def run_packet(self):
        with self.session() as session:
            session.command({'id': 'start-evidence', 'action': 'START'})
            receipts = []
            for step in self.packet['rounds']:
                frame = deepcopy(step); frame.pop('send_aliases')
                receipts.append(session.apply_round(frame))
            state = session.snapshot()
        return receipts, state

    def test_generated_geometry_uses_rule_d02(self):
        receipts, _ = self.run_packet()
        for receipt in receipts:
            self.assertEqual(receipt['decisions'][0]['reason'], 'GEOMETRY_BLOCK')
            if len(receipt['decisions']) > 1:
                self.assertEqual(receipt['decisions'][0]['rule_ids'], ['D02'])
            else:
                self.assertIn('D02', receipt['decisions'][0]['rule_ids'])

    def test_direction_minute_and_durable_reopen(self):
        receipts, state = self.run_packet()
        self.assertEqual([d['reason'] for d in receipts[0]['decisions']], ['GEOMETRY_BLOCK', 'NO_CHASE_AFTER_ACT'])
        self.assertEqual(receipts[0]['decisions'][1]['direction'], 'LONG')
        self.assertEqual([d['reason'] for d in receipts[1]['decisions']], ['GEOMETRY_BLOCK'])
        self.assertEqual(receipts[2]['decisions'][1]['direction'], 'SHORT')
        self.assertEqual(receipts[3]['decisions'][1]['direction'], 'SHORT')
        self.assertTrue(all(not row['effects'] for row in receipts))
        self.assertEqual(self.port.orders, [])
        with self.session() as reopened:
            self.assertEqual(reopened.snapshot()['symbols'][CODE]['last_signal_bar'], state['symbols'][CODE]['last_signal_bar'])
        self.assertTrue(state['symbols'][CODE]['last_signal_bar'].endswith('02:57:00Z'))

    def test_disabled_chase_filter_does_not_consume_minute(self):
        self.packet = samples()['activation-disabled']
        receipts, state = self.run_packet()
        self.assertTrue(all([d['reason'] for d in row['decisions']] == ['GEOMETRY_BLOCK'] for row in receipts))
        self.assertIsNone(state['symbols'][CODE]['last_signal_bar'])

    def test_long_activation_equality_is_inclusive(self):
        self.packet = samples()['long-activation-equality']
        receipts, _ = self.run_packet()
        self.assertEqual(receipts[0]['decisions'][1]['direction'], 'LONG')
        self.assertEqual(receipts[0]['decisions'][1]['activation_price'], '9037.0')

    def test_short_activation_equality_is_inclusive(self):
        self.packet = samples()['short-activation-equality']
        receipts, _ = self.run_packet()
        self.assertEqual(receipts[0]['decisions'][1]['direction'], 'SHORT')
        self.assertEqual(receipts[0]['decisions'][1]['activation_price'], '8985.0')


if __name__ == '__main__': unittest.main()
