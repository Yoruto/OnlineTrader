"""Runtime operator contract tests; no account connection or native orders."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from secrets import token_urlsafe
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from online_trader.errors import TraderError
from online_trader.runtime.operator_api import OperatorEndpoint, PROTOCOL, validate_wire
from online_trader.runtime.portable_schedule import schedule_session


class RuntimeOperatorContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'private').mkdir()
        self.account = 'DEV:runtime-operator'
        self.token = token_urlsafe(48)
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
        self.record = {'schema_version': 'ONLINE_TRADER_OPERATORS/1', 'operators': [{
            'id': 'desk', 'token_sha256': sha256(self.token.encode()).hexdigest(),
            'account_keys': [self.account], 'operations': ['MANUAL_CLOSE', 'SET_ENTRY_POLICY'],
            'expires_at_utc': expiry,
        }]}
        (self.root / 'private/operators.json').write_text(json.dumps(self.record), encoding='utf-8')
        self.endpoint = OperatorEndpoint('Runtime.Operator.Test', self.root, self.root / 'ledger.db', self.account)

    def request(self, operation='authenticate', body=None, token=None):
        return {'protocol': PROTOCOL, 'request_id': 'request-runtime-auth', 'operation': operation,
                'body': {} if body is None else body, 'operator': 'desk',
                'token': self.token if token is None else token}

    def test_authentication_returns_only_non_secret_capabilities(self):
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*'))
        request = self.request()
        validate_wire(request)
        result = self.endpoint.handle(request)
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*'))
        self.assertEqual(result['status'], 'AUTHENTICATED')
        self.assertEqual(result['operator'], 'desk')
        self.assertEqual(result['account_key'], self.account)
        self.assertEqual(result['operations'], ['MANUAL_CLOSE', 'SET_ENTRY_POLICY'])
        self.assertNotIn('token', result)
        self.assertEqual(before, after)

    def test_wrong_token_and_authentication_body_are_rejected_without_a_ledger(self):
        with self.assertRaisesRegex(TraderError, 'invalid') as wrong:
            self.endpoint.handle(self.request(token='x' * 32))
        self.assertEqual(wrong.exception.code, 'E_AUTH')
        with self.assertRaises(TraderError) as body:
            self.endpoint.handle(self.request(body={'command_id': 'not-allowed'}))
        self.assertEqual(body.exception.code, 'E_SCHEMA')
        self.assertFalse((self.root / 'ledger.db').exists())

    def test_expired_wrong_account_and_missing_capability_are_rejected_before_enqueue(self):
        body = {'command_id': 'operator-open-denied', 'operation': 'MANUAL_OPEN',
                'scope': {'scope_type': 'CONTRACT', 'scope_ids': ['DCE.p2701']}, 'parameters': {}}
        with self.assertRaises(TraderError) as capability:
            self.endpoint.handle(self.request('submit', body))
        self.assertEqual(capability.exception.code, 'E_AUTH')
        for change in ({'account_keys': ['DEV:other-account']},
                       {'expires_at_utc': '2000-01-01T00:00:00Z'}):
            original = self.record['operators'][0].copy()
            self.record['operators'][0].update(change)
            (self.root / 'private/operators.json').write_text(json.dumps(self.record), encoding='utf-8')
            with self.subTest(change=change), self.assertRaises(TraderError) as denied:
                self.endpoint.handle(self.request())
            self.assertEqual(denied.exception.code, 'E_AUTH')
            self.record['operators'][0] = original
        (self.root / 'private/operators.json').write_text(json.dumps(self.record), encoding='utf-8')

    def test_public_runtime_schema_accepts_authentication_and_rejects_a_body(self):
        schema = json.loads((Path(__file__).parents[1] / 'src/online_trader/resources/runtime-ipc.schema.json').read_text('utf-8'))
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(self.request())), [])
        self.assertTrue(list(validator.iter_errors(self.request(body={'command_id': 'not-allowed'}))))

    def test_schedule_reuses_the_same_runtime_operator_grant(self):
        self.record['operators'][0]['operations'].append('SCHEDULE_STRATEGY')
        (self.root / 'private/operators.json').write_text(json.dumps(self.record), encoding='utf-8')
        profile = SimpleNamespace(content_hash='a' * 64, to_record=lambda: {
            'settings': {'schedule': {'enabled': False}, 'trade': {'auto_start_on_launch': False}}})
        store = SimpleNamespace(directory=self.root, runtime_control=lambda: {'scheduled_start': {'enabled': False}},
                                profile=lambda: profile)
        row = {'account_key': self.account, 'account_id': 'account-runtime'}
        with patch('online_trader.runtime.portable_schedule.time.sleep', side_effect=KeyboardInterrupt):
            self.assertEqual(schedule_session(store, row, 'desk', self.token), 0)


if __name__ == '__main__':
    unittest.main()
