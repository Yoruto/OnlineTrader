"""C01: real SQLite and background reader; no SDK, account lock or Qt required."""

from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes
from online_trader.dsv_storage.journal import Journal
from online_trader.dsv_storage.workbench_evidence import WorkbenchEvidence


CODE = 'DCE.p2701'
DAY = '2026-11-13'
AT = DAY + 'T01:10:00Z'


def round_event(reason='STOP_LOSS'):
    return {'input': {'market': [{'contract_id': CODE}]},
            'receipt': {'evaluated_at': AT, 'decisions': [{'contract_id': CODE, 'reason': reason}]}}


class WorkbenchHistoryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / 'facts.db'
        self.scope = {'account_key': 'DEV:history-compat', 'provider': 'OFFLINE',
                      'trading_day': DAY, 'configuration_id': 'test-config'}
        self.state = {'orders': {'child-original': {'send_state': 'UNKNOWN', 'original_qty': 2,
                                                   'filled_qty': 0, 'terminal_confirmed': False}}}
        self.journal = Journal(self.path, self.scope, self.state)
        self.addCleanup(self.journal.close)
        self.effect = {'id': 'opaque-effect-key', 'child_order_id': 'child-original', 'kind': 'SEND',
                       'contract_id': CODE, 'account_key': self.scope['account_key'], 'provider': 'OFFLINE',
                       'quantity': 2, 'limit_price': '100'}

    def prepare(self, kind='SEND'):
        effect = self.effect | {'kind': kind}
        with self.journal.transaction():
            self.journal.prepare(effect)
            self.journal.resolve(effect, 'UNKNOWN')
        return effect

    def event(self, kind, value):
        with self.journal.transaction():
            self.journal.event(kind, value)

    def legacy_unknown(self, effect=None):
        effect = self.effect if effect is None else effect
        # Exact shape written by aa8bd35's dispatch, including no guessed time.
        self.event('EFFECT_RESULT', {'id': effect['id'], 'result': 'UNKNOWN'})

    def tail(self, reason='STOP_LOSS'):
        self.event('ROUND', round_event(reason))
        with self.journal.transaction():
            self.journal.record_trade(DAY, 'native-original', 'trade-original', {
                'native_order_id': 'native-original', 'native_trade_id': 'trade-original',
                'child_order_id': 'child-original', 'quantity': 1, 'price': '101', 'fill_day': DAY})

    def reader(self):
        reader = WorkbenchEvidence(self.path)
        self.addCleanup(reader.close)
        return reader

    def wait(self, reader, predicate=lambda value: value['history_status'] in ('READY', 'DEGRADED')):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = reader.request()
            if predicate(value):
                return value
            time.sleep(0.005)
        self.fail(f'History did not progress: status={value["history_status"]}, cursor={reader._seq}')

    def facts(self):
        return tuple(self.journal._db.iterdump())

    def test_legacy_unknown_recovers_only_committed_request_identity(self):
        self.prepare()
        self.legacy_unknown()
        self.tail()
        before = self.facts()
        value = self.wait(self.reader())
        self.assertEqual(value['history_status'], 'READY')
        unknown, = [r for r in value['activity'] if r['reason'] == 'UNKNOWN']
        self.assertEqual((unknown['child_order_id'], unknown['effect_kind'], unknown['contract_id']),
                         ('child-original', 'SEND', CODE))
        self.assertIsNone(unknown['at'])
        self.assertIsNone(unknown['error_code'])
        self.assertEqual(value['fills'][0]['price'], '101')
        self.assertIn('STOP_LOSS', [r['reason'] for r in value['activity']])
        self.assertEqual(before, self.facts())
        self.assertEqual(self.journal.load(), self.state)

    def test_legacy_cancel_unknown_uses_request_not_id_suffix(self):
        effect = self.prepare('CANCEL')
        self.legacy_unknown(effect)
        value = self.wait(self.reader())
        self.assertEqual(value['activity'][0]['effect_kind'], 'CANCEL')
        self.assertEqual(value['activity'][0]['reason'], 'UNKNOWN')

    def test_no_prepared_request_keeps_unknown_and_reports_incomplete_identity(self):
        self.legacy_unknown({'id': 'looks-like-child-send'})
        self.tail()
        before = self.facts()
        value = self.wait(self.reader())
        self.assertEqual(value['history_status'], 'DEGRADED')
        unknown, = [r for r in value['activity'] if r['reason'] == 'UNKNOWN']
        self.assertIsNone(unknown['child_order_id'])
        self.assertIsNone(unknown['effect_kind'])
        warning, = [r for r in value['activity'] if r['reason'] == 'HISTORY_RECORD_INCOMPLETE']
        self.assertEqual(warning['error_code'], 'E_HISTORY_EFFECT_MISSING')
        self.assertEqual(warning['event_seq'], '1')
        self.assertEqual(len(value['fills']), 1)
        self.assertEqual(before, self.facts())

    def test_conflicting_prepared_identity_is_not_invented_or_overwritten(self):
        self.prepare()
        self.event('EFFECT_RESULT', {'id': self.effect['id'], 'result': 'UNKNOWN',
                                     'child_order_id': 'different-child'})
        self.tail()
        value = self.wait(self.reader())
        self.assertEqual(value['history_status'], 'DEGRADED')
        self.assertNotIn('UNKNOWN', [r['reason'] for r in value['activity']])
        self.assertIn('E_HISTORY_EFFECT_CONFLICT', [r.get('error_code') for r in value['activity']])
        self.assertEqual(len(value['fills']), 1)

    def test_prepared_columns_and_payload_must_agree(self):
        self.prepare()
        self.journal._db.execute('UPDATE effects SET request=?',
                                 (canonical_bytes(self.effect | {'child_order_id': 'wrong'}),))
        self.legacy_unknown()
        self.tail()
        value = self.wait(self.reader())
        self.assertIn('E_HISTORY_EFFECT_CONFLICT', [r.get('error_code') for r in value['activity']])
        self.assertEqual(len(value['fills']), 1)

    def test_malformed_event_does_not_block_later_records_or_leak_payload(self):
        self.journal._db.execute('INSERT INTO events(kind,payload) VALUES(?,?)',
                                 ('EFFECT_RESULT', b'not-json fake-password=never-export'))
        self.tail()
        before = self.facts()
        reader = self.reader()
        value = self.wait(reader)
        self.assertEqual(value['history_status'], 'DEGRADED')
        self.assertEqual(reader._seq, 2)
        self.assertNotIn('never-export', repr(value))
        self.assertEqual(len(value['fills']), 1)
        self.assertEqual(before, self.facts())
        self.event('ROUND', round_event('NORMAL_ENTRY'))
        value = self.wait(reader, lambda v: any(r['reason'] == 'NORMAL_ENTRY' for r in v['activity']))
        self.assertEqual(len([r for r in value['activity'] if r['reason'] == 'HISTORY_RECORD_INCOMPLETE']), 1)

    def test_malformed_round_publishes_no_partial_decisions(self):
        bad = round_event('NORMAL_ENTRY')
        bad['receipt']['decisions'].append({'reason': 'missing-contract'})
        self.event('ROUND', bad)
        self.tail()
        value = self.wait(self.reader())
        self.assertNotIn('NORMAL_ENTRY', [r['reason'] for r in value['activity']])
        self.assertIn('STOP_LOSS', [r['reason'] for r in value['activity']])
        self.assertEqual(len(value['fills']), 1)

    def test_new_failure_fields_and_missing_historical_time_are_preserved(self):
        self.event('EFFECT_RESULT', {'id': 'new-effect', 'result': 'UNKNOWN', 'kind': 'CANCEL',
            'child_order_id': 'child-original', 'contract_id': CODE, 'error_code': 'E_DSV_PORT_CALL',
            'exception': {'stack': [{'type': 'OSError'}]}})
        value = self.wait(self.reader())
        row, = value['activity']
        self.assertEqual(value['history_status'], 'READY')
        self.assertEqual((row['effect_kind'], row['error_code'], row['exception_type']),
                         ('CANCEL', 'E_DSV_PORT_CALL', 'OSError'))
        self.assertIsNone(row['at'])

    def test_new_dated_send_rejection_is_unchanged(self):
        self.event('EFFECT_RESULT', {'id': 'new-effect', 'result': 'DEFINITELY_NOT_SENT', 'kind': 'SEND',
            'child_order_id': 'child-original', 'contract_id': CODE, 'at': AT,
            'rejection_reason': 'QUOTE_OR_PRICE_SCOPE', 'error_code': 'E_DSV_SDK_SCOPE'})
        row, = self.wait(self.reader())['activity']
        self.assertEqual(row['at'], AT)
        self.assertEqual(row['rejection_reason'], 'QUOTE_OR_PRICE_SCOPE')

    def test_old_success_without_identity_does_not_warn(self):
        self.event('EFFECT_RESULT', {'id': 'old-success', 'result': 'CALL_RETURNED'})
        self.tail()
        value = self.wait(self.reader())
        self.assertEqual(value['history_status'], 'READY')
        self.assertEqual([r['reason'] for r in value['activity']], ['STOP_LOSS'])

    def test_reopen_rebuilds_legacy_view_without_writing_migration(self):
        self.prepare()
        self.legacy_unknown()
        self.tail()
        before = self.facts()
        first = self.reader()
        one = self.wait(first)
        first.close()
        two = self.wait(self.reader())
        self.assertEqual(one, two)
        self.assertEqual(before, self.facts())

    def test_transient_database_lock_retries_without_skipping_events(self):
        self.prepare()
        self.legacy_unknown()
        locker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(locker.close)
        locker.execute('BEGIN EXCLUSIVE')
        reader = self.reader()
        self.wait(reader, lambda value: value['history_status'] == 'UNAVAILABLE')
        self.assertEqual(reader._seq, 0)
        locker.execute('ROLLBACK')
        value = self.wait(reader, lambda value: value['history_status'] == 'READY')
        self.assertEqual(value['activity'][0]['reason'], 'UNKNOWN')

    def test_sqlite_lookup_error_is_not_quarantined_as_bad_event(self):
        self.prepare()
        self.legacy_unknown()
        reader = self.reader()
        with patch.object(reader, '_dispatch_identity', side_effect=sqlite3.OperationalError('temporary')):
            self.wait(reader, lambda value: value['history_status'] == 'UNAVAILABLE')
            self.assertEqual(reader._seq, 0)
            self.assertFalse(reader._warnings)
        value = self.wait(reader, lambda value: value['history_status'] == 'READY')
        self.assertEqual(value['activity'][0]['reason'], 'UNKNOWN')

    def test_warning_snapshot_is_bounded_and_decisions_still_deduplicate(self):
        for _ in range(45):
            self.journal._db.execute("INSERT INTO events(kind,payload) VALUES('ROUND',?)", (b'{}',))
        self.event('ROUND', round_event('STOP_LOSS'))
        self.event('ROUND', round_event('STOP_LOSS'))
        value = self.wait(self.reader())
        self.assertEqual(len([r for r in value['activity'] if r['reason'] == 'HISTORY_RECORD_INCOMPLETE']), 20)
        self.assertEqual(len([r for r in value['activity'] if r['reason'] == 'STOP_LOSS']), 1)

    def test_history_reader_connection_is_read_only(self):
        reader = self.reader()
        connection = sqlite3.connect(reader._uri, uri=True)
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute('DELETE FROM events')


if __name__ == '__main__':
    unittest.main()
