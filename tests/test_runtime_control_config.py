"""Retired Runtime control files cannot affect the manual-only owner."""

import json
from pathlib import Path
import tempfile
import unittest

from online_trader.errors import TraderError
from online_trader.runtime.portable_config import PortableAccountStore


class RuntimeControlConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'private').mkdir()
        (self.root / 'runtime.json').write_text(json.dumps({
            'schema_version': 'ONLINE_TRADER_RUNTIME/1',
            'selected_account': 'configured',
            'accounts': [{'id': 'configured', 'alias': 'Configured', 'provider': 'TQKQ',
                          'broker': 'KQ', 'line': 'SIM'}],
        }), encoding='utf-8')
        (self.root / 'private/OnlineTrader-defaults.json').write_text(json.dumps({
            'accounts': {'configured': {'username': 'configured-user', 'password': 'configured-password'}},
        }), encoding='utf-8')

    def test_retired_control_and_operator_files_are_ignored_unchanged(self):
        control = self.root / 'runtime-control.json'
        operators = self.root / 'private/operators.json'
        control.write_text('{not-json', encoding='utf-8')
        operators.write_text('{also-not-json', encoding='utf-8')
        before = (control.read_bytes(), operators.read_bytes())

        store = PortableAccountStore(self.root)

        state = store.read()
        selected = next(row for row in state['accounts'] if row['account_id'] == state['selected_id'])
        self.assertEqual(selected['alias'], 'Configured')
        self.assertEqual((control.read_bytes(), operators.read_bytes()), before)

    def test_retired_files_cannot_replace_current_runtime_configuration(self):
        (self.root / 'runtime.json').unlink()
        (self.root / 'runtime-control.json').write_text(json.dumps({
            'schema_version': 'ONLINE_TRADER_RUNTIME_CONTROL/1',
            'scheduled_start': {'enabled': True},
        }), encoding='utf-8')

        with self.assertRaises(TraderError) as caught:
            PortableAccountStore(self.root)

        self.assertEqual(caught.exception.code, 'E_RUNTIME_CONFIG')


if __name__ == '__main__':
    unittest.main()
