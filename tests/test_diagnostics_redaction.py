"""Unit tests for diagnostic redaction boundaries and credential scrubbing."""

from pathlib import Path
import tempfile
import unittest

from online_trader.diagnostics.redaction import (
    account_pseudonym,
    redact_text,
    sanitize_diagnostic_mapping,
)
from online_trader.diagnostics.reporter import _collect_view_log


class DiagnosticsRedactionTests(unittest.TestCase):
    def test_scrubs_passwords_tokens_urls_accounts_and_paths_while_keeping_trading_facts(self):
        raw = (
            'login=trader01 password=MySecret123 "private_token": "glpat-xyz987" '
            "PRIVATE-TOKEN: ptok-111 DEPLOY-TOKEN: dtok-222 Bearer eyJhbGciOiJIUzI1NiJ9 "
            'https://alice:hunter2@jihulab.com/Yoruto/repo '
            'SIM:TQ_SIMNOW:123456 C:\\Users\\alice\\secrets.json \\\\fileserver\\share\\vault.db '
            'SHFE.ag2606 price=7512.5 E_DSV_SYNC otdsvabcdef123456'
        )
        cleaned = redact_text(raw, max_len=1024)
        for secret in ('trader01', 'MySecret123', 'glpat-xyz987', 'ptok-111', 'dtok-222',
                       'eyJhbGciOiJIUzI1NiJ9', 'alice:hunter2', 'SIM:TQ_SIMNOW:123456',
                       'secrets.json', 'vault.db'):
            self.assertNotIn(secret, cleaned)
        for trading_fact in ('SHFE.ag2606', '7512.5', 'E_DSV_SYNC', 'otdsvabcdef123456'):
            self.assertIn(trading_fact, cleaned)

    def test_sanitize_diagnostic_mapping_drops_sensitive_keys_and_preserves_trading_fields(self):
        mapping = {
            'contract_id': 'SHFE.ag2606',
            'last_price': '7500.5',
            'owned_after': 2,
            'broker_state': 'FILLED',
            'error_code': 'E_DSV_SYNC',
            'native_order_id': 'native-ord-99',
            'native_trade_id': 'native-trd-77',
            'username': 'secret_user',
            'login': '18800001111',
            'masked_login': '****1111',
            'account_key': 'SIM:SIMNOW:abcdef0123456789',
            'password': 'trade-password',
            'private_token': 'token-val',
            'deploy-token': 'deploy-val',
            'detail': 'connect failed for user password=abc123 at C:\\Users\\bob\\runtime.json',
        }
        cleaned = sanitize_diagnostic_mapping(mapping)
        self.assertEqual(cleaned['contract_id'], 'SHFE.ag2606')
        self.assertEqual(cleaned['last_price'], '7500.5')
        self.assertEqual(cleaned['owned_after'], 2)
        self.assertEqual(cleaned['broker_state'], 'FILLED')
        self.assertEqual(cleaned['error_code'], 'E_DSV_SYNC')
        self.assertEqual(cleaned['native_order_id'], 'native-ord-99')
        self.assertEqual(cleaned['native_trade_id'], 'native-trd-77')
        for forbidden_key in ('username', 'login', 'masked_login', 'account_key',
                              'password', 'private_token', 'deploy-token'):
            self.assertNotIn(forbidden_key, cleaned)
        self.assertNotIn('abc123', cleaned['detail'])
        self.assertNotIn('runtime.json', cleaned['detail'])

    def test_account_pseudonym_is_deterministic_and_hides_account_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p1 = account_pseudonym(root, 'SIM:SIMNOW:1234567890abcdef')
            p2 = account_pseudonym(root, 'SIM:SIMNOW:1234567890abcdef')
            p3 = account_pseudonym(root, 'SIM:KQ:1234567890abcdef')
            self.assertEqual(p1, p2)
            self.assertNotEqual(p1, p3)
            self.assertTrue(p1.startswith('acct-'))
            self.assertNotIn('1234567890abcdef', p1)

    def test_collect_view_log_redacts_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / 'logs'
            logs.mkdir(parents=True)
            (logs / 'runtime-view.log').write_text(
                '2026-11-25T01:00:00Z VIEW_CLIENT pid=42 boot=boot-1\n',
                encoding='utf-8',
            )
            staging = root / 'staging'
            staging.mkdir()
            files = []
            omissions = []
            request = {
                'runtime_boot_id': 'boot-1',
                'requested_at_utc': '2026-11-25T02:00:00Z',
                'expected_sources': {'runtime_view': True},
            }
            rows, workbench_expected = _collect_view_log(
                staging, root, request, '2026-11-25T00:00:00Z', files, omissions,
            )
            self.assertTrue(workbench_expected)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['message'], 'VIEW_CLIENT pid=42 boot=boot-1')


if __name__ == '__main__':
    unittest.main()
