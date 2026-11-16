"""Shared SQLite preflight policy; no SDK connection or Windows lock is acquired.

Version selection is mocked only to exercise policy branches. Successful prepare
uses the real exclusive file publisher; source review and the account lock are
explicit component doubles. No online Journal is opened on a mocked runtime.
"""
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.dsv_runtime import sdk_startup
from online_trader.dsv_storage import journal
from online_trader.errors import TraderError


class SqliteRuntimePolicyTests(unittest.TestCase):
    def test_existing_supported_versions_are_not_changed(self):
        for version in ((3, 51, 3), (3, 52, 0), (3, 50, 7), (3, 44, 6)):
            for provider in ('TQKQ', 'TQ_SIMNOW'):
                with self.subTest(version=version, provider=provider), patch.object(
                        journal.sqlite3, 'sqlite_version_info', version):
                    self.assertEqual(journal.validate_sqlite_runtime(provider), {
                        'sqlite_version': '.'.join(map(str, version)), 'wal_runtime_supported': True})

    def test_existing_unsupported_versions_keep_the_same_error_code(self):
        for version in ((3, 51, 2), (3, 50, 6), (3, 44, 5), (3, 46, 1)):
            for provider in ('TQKQ', 'TQ_SIMNOW'):
                with self.subTest(version=version, provider=provider), patch.object(
                        journal.sqlite3, 'sqlite_version_info', version):
                    with self.assertRaises(TraderError) as caught:
                        journal.validate_sqlite_runtime(provider)
                    self.assertEqual(caught.exception.code, 'E_DSV_STORE_SQLITE_VERSION')

    def test_offline_policy_is_unchanged_and_reports_actual_version(self):
        with patch.object(journal.sqlite3, 'sqlite_version_info', (3, 46, 1)):
            self.assertEqual(journal.validate_sqlite_runtime('OFFLINE'), {
                'sqlite_version': '3.46.1', 'wal_runtime_supported': False})

    def test_journal_still_refuses_before_opening_any_database(self):
        with patch.object(journal.sqlite3, 'sqlite_version_info', (3, 46, 1)), patch.object(
                journal.sqlite3, 'connect') as connect:
            with self.assertRaises(TraderError) as caught:
                journal.Journal('must-not-open.db', {'provider': 'TQ_SIMNOW'}, {})
        self.assertEqual(caught.exception.code, 'E_DSV_STORE_SQLITE_VERSION')
        connect.assert_not_called()


class SqliteStartupPreflightTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.source = self.root / 'legacy' / 'ledger.db'
        self.database = self.root / 'replacement' / 'facts.db'
        self.output = self.root / 'output'
        self.profile = load_confirmed_baseline()[0]
        self.grant = SimpleNamespace(
            replacement=True, provider='TQ_SIMNOW', account_key='SIM:SIMNOW:0123456789abcdef',
            content_hash='a' * 64, in_window=lambda now: True,
            value={'contracts': [row['code'] for row in self.profile.to_settings()['symbols']],
                   'ledger_path': str(self.source), 'trading_day': '2026-11-16',
                   'not_before_utc': '2026-11-16T00:00:00+00:00'})
        self.calendar = SimpleNamespace(content_hash='b' * 64)
        self.enterContext(patch.object(sdk_startup, 'load_inputs',
                                      return_value=(self.grant, self.profile, self.calendar)))
        self.lock = self.enterContext(patch.object(sdk_startup, 'AccountLock',
                                                  side_effect=lambda account: nullcontext()))
        self.source_check = self.enterContext(patch.object(sdk_startup, '_validate_source', return_value={}))
        self.port = self.enterContext(patch.object(sdk_startup, 'strategy_port'))
        self.port.return_value.connect.side_effect = TraderError('E_TEST_CONNECT_BOUNDARY', 'No real connection')
        self.claim = self.enterContext(patch.object(sdk_startup, '_claim_run_output'))
        self.status = self.enterContext(patch.object(sdk_startup, '_status'))
        self.enterContext(patch.object(sdk_startup, 'sdk_version', return_value='3.10.4'))
        self.enterContext(patch.object(sdk_startup, '_date_resolver', return_value=lambda *args: '2026-11-16'))
        wall = self.enterContext(patch.object(sdk_startup, 'LocalWallClock'))
        wall.return_value.now.return_value = datetime(2026, 11, 16, 1, tzinfo=timezone.utc)
        self.db = Mock()
        self.db.execute.return_value.fetchone.return_value = (canonical_bytes({'symbols': {}, 'orders': {}}),)
        self.readonly = self.enterContext(patch.object(sdk_startup, '_readonly',
                                                       side_effect=lambda path: nullcontext(self.db)))

    def prepare(self):
        return sdk_startup.prepare('scope', 'profile', 'calendar', self.database, self.output)

    def run_owner(self):
        return sdk_startup.run('scope', {}, 'profile', 'calendar', self.database, self.output, pipe='test-pipe')

    def test_prepare_rejects_before_lock_source_review_or_output(self):
        for provider in ('TQKQ', 'TQ_SIMNOW'):
            self.grant.provider = provider
            with self.subTest(provider=provider), patch.object(journal.sqlite3, 'sqlite_version_info', (3, 46, 1)):
                with self.assertRaises(TraderError) as caught:
                    self.prepare()
                self.assertEqual(caught.exception.code, 'E_DSV_STORE_SQLITE_VERSION')
        self.lock.assert_not_called()
        self.source_check.assert_not_called()
        self.port.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.database.exists())

    def test_run_rejects_before_reading_facts_claiming_output_or_connecting(self):
        for provider in ('TQKQ', 'TQ_SIMNOW'):
            self.grant.provider = provider
            with self.subTest(provider=provider), patch.object(journal.sqlite3, 'sqlite_version_info', (3, 46, 1)):
                with self.assertRaises(TraderError) as caught:
                    self.run_owner()
                self.assertEqual(caught.exception.code, 'E_DSV_STORE_SQLITE_VERSION')
        for operation in (self.lock, self.source_check, self.readonly, self.claim, self.status, self.port):
            operation.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_prepare_records_runtime_but_stays_disarmed_and_offline(self):
        for provider in ('TQKQ', 'TQ_SIMNOW'):
            self.grant.provider = provider
            self.output = self.root / provider
            with self.subTest(provider=provider), patch.object(journal.sqlite3, 'sqlite_version_info', (3, 51, 3)):
                record = self.prepare()
                self.assertEqual(record['sqlite_runtime'], {
                    'sqlite_version': '3.51.3', 'wal_runtime_supported': True})
                self.assertEqual(record['type'], 'DSV_REPLACEMENT_PREPARE/1')
                self.assertEqual(record['entry_authority'], 'DISARMED')
                self.assertIs(record['broker_connected'], False)
                self.assertEqual(record['scope_hash'], self.grant.content_hash)
                self.assertEqual(strict_loads((self.output / 'replacement-preflight.json').read_bytes()), record)
        self.assertEqual(self.source_check.call_count, 2)
        self.port.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_successful_prepare_does_not_bypass_later_runtime_check(self):
        with patch.object(journal.sqlite3, 'sqlite_version_info', (3, 51, 3)):
            self.prepare()
        receipt = self.output / 'replacement-preflight.json'
        before = receipt.read_bytes()
        self.lock.reset_mock()
        self.source_check.reset_mock()
        with patch.object(journal.sqlite3, 'sqlite_version_info', (3, 46, 1)):
            with self.assertRaises(TraderError) as caught:
                self.run_owner()
        self.assertEqual(caught.exception.code, 'E_DSV_STORE_SQLITE_VERSION')
        self.assertEqual(receipt.read_bytes(), before)
        self.lock.assert_not_called()
        self.source_check.assert_not_called()
        self.port.assert_not_called()
        self.claim.assert_not_called()

    def test_unsupported_prepare_does_not_replace_an_existing_receipt(self):
        self.output.mkdir()
        receipt = self.output / 'replacement-preflight.json'
        receipt.write_bytes(b'previous preflight')
        with patch.object(journal.sqlite3, 'sqlite_version_info', (3, 46, 1)):
            with self.assertRaises(TraderError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, 'E_DSV_STORE_SQLITE_VERSION')
        self.assertEqual(receipt.read_bytes(), b'previous preflight')

    def test_supported_prepare_retains_exclusive_publish(self):
        with patch.object(journal.sqlite3, 'sqlite_version_info', (3, 51, 3)):
            self.prepare()
            with self.assertRaises(TraderError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, 'E_DUPLICATE_CONFLICT')

    def test_scope_validation_remains_required(self):
        self.grant.replacement = False
        with patch.object(journal.sqlite3, 'sqlite_version_info', (3, 51, 3)):
            with self.assertRaises(TraderError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, 'E_ONLINE_SCOPE')
        self.lock.assert_not_called()
        self.port.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_source_validation_remains_required_for_prepare_and_run(self):
        self.source_check.side_effect = TraderError('E_DSV_MIGRATION_SCOPE', 'Synthetic source mismatch')
        with patch.object(journal.sqlite3, 'sqlite_version_info', (3, 51, 3)):
            for operation in (self.prepare, self.run_owner):
                with self.subTest(operation=operation.__name__), self.assertRaises(TraderError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, 'E_DSV_MIGRATION_SCOPE')
        self.port.assert_not_called()
        self.claim.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
