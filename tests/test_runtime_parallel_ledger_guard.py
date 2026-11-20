"""Offline regression for portable runtimes predating parallel-ledger detection."""

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from online_trader.errors import TraderError
from online_trader.runtime.portable_config import PortableAccountStore


class ParallelLedgerGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / 'portable' / 'ledger.db'
        self.ledger.parent.mkdir()
        self.ledger.touch()
        self.row = {'account_key': 'TQKQ:parallel-guard',
                    'environment': 'ONLINE_SIM', 'provider': 'TQKQ'}
        self.check = Mock()
        self.store = SimpleNamespace(
            directory=self.ledger.parent,
            ledger_path=lambda row: self.ledger,
            _reject_parallel_desktop_ledger=self.check,
        )
        self.lease = SimpleNamespace(matches=lambda key: key == self.row['account_key'])

    def _initialize(self, handoff):
        with (patch('online_trader.runtime.handoff.validate_handoff', return_value=handoff),
              patch('online_trader.runtime.portable_config.Ledger') as ledger,
              patch('online_trader.runtime.portable_config.publish_new_bytes')):
            PortableAccountStore.initialize_ledger(self.store, self.row, self.lease)
            return ledger

    def test_existing_portable_ledger_still_checks_desktop_for_fork(self):
        self._initialize(frozenset())
        self.check.assert_called_once_with(self.row, self.ledger)

    def test_verified_handoff_skips_source_fork_guard(self):
        self._initialize(frozenset({self.row['account_key']}))
        self.check.assert_not_called()

    def test_existing_standard_desktop_ledger_requires_explicit_handoff(self):
        source = (self.root / 'OnlineTrader' / 'accounts' / 'data' /
                  'parallel-guard' / 'ledger.db')
        source.parent.mkdir(parents=True)
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE ledger_scope (account_key TEXT, environment TEXT, '
                       'broker_identity TEXT, schema_version INTEGER)')
            db.execute('INSERT INTO ledger_scope VALUES (?,?,?,?)',
                       (self.row['account_key'], 'ONLINE_SIM', 'TQKQ', 1))
        with patch.dict(os.environ, {'LOCALAPPDATA': str(self.root)}):
            with self.assertRaises(TraderError) as caught:
                PortableAccountStore._reject_parallel_desktop_ledger(self.row, self.ledger)
        self.assertEqual(caught.exception.code, 'E_RUNTIME_HANDOFF')


if __name__ == '__main__':
    unittest.main()
