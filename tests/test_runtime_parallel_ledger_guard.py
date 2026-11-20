"""Offline contract for process-owned Standalone Runtime execution ledgers."""

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from online_trader.errors import TraderError
from online_trader.runtime.portable_config import PortableAccountStore


class SessionLedgerGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.row = {
            'account_key': 'TQKQ:session-ledger',
            'environment': 'ONLINE_SIM',
            'provider': 'TQKQ',
        }
        self.store = SimpleNamespace()
        self.lease = SimpleNamespace(matches=lambda key: key == self.row['account_key'])

    def initialize(self, target):
        PortableAccountStore.initialize_session_ledger(
            self.store, self.row, self.lease, target)

    def test_fresh_runtime_session_creates_a_new_ledger(self):
        target = self.root / 'session-a' / 'ledger.db'

        self.initialize(target)

        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 0)

    def test_existing_session_target_is_rejected_without_overwrite(self):
        target = self.root / 'session-b' / 'ledger.db'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'existing-session-evidence')

        with self.assertRaises(TraderError) as caught:
            self.initialize(target)

        self.assertEqual(caught.exception.code, 'E_STORAGE')
        self.assertEqual(target.read_bytes(), b'existing-session-evidence')

    def test_account_lock_mismatch_rejects_before_creating_the_ledger(self):
        target = self.root / 'session-c' / 'ledger.db'
        wrong_lease = SimpleNamespace(matches=lambda _key: False)

        with self.assertRaises(TraderError) as caught:
            PortableAccountStore.initialize_session_ledger(
                self.store, self.row, wrong_lease, target)

        self.assertEqual(caught.exception.code, 'E_INSTANCE_LOCK')
        self.assertFalse(target.exists())

    def test_legacy_execution_ledger_remains_read_only_history(self):
        legacy = self.root / 'portable' / 'data' / 'session-ledger' / 'ledger.db'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'legacy-read-only-evidence')
        target = self.root / 'session-d' / 'ledger.db'

        self.initialize(target)

        self.assertTrue(target.is_file())
        self.assertEqual(legacy.read_bytes(), b'legacy-read-only-evidence')


if __name__ == '__main__':
    unittest.main()
