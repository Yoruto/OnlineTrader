"""Cross-platform recovery-input boundaries, not a Windows recovery acceptance.

Use real calendar/codec/validator and explicitly synthetic reviewed evidence.
No source ledger is written, execution lock acquired, or SDK connected.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from online_trader.contracts.codec import content_hash
from online_trader.dsv_runtime.legacy_recovery import _check_pair, recover_flat
from online_trader.errors import TraderError
from online_trader.scheduling.calendar import TradingCalendar

CODE, DAY = 'SHFE.ag2702', '2026-11-16'


def ns(time):
    return str(int(datetime.fromisoformat(DAY+'T'+time+'Z').timestamp()) * 1_000_000_000)


def evidence():
    orders = [{'order_id': 'original-open', 'contract_id': CODE, 'direction': 'BUY', 'offset': 'OPEN',
               'quantity': 1, 'remaining': 0, 'status': 'FINISHED', 'limit_price': '101', 'source_time_ns': ns('01:10:00')},
              {'order_id': 'external-close', 'contract_id': CODE, 'direction': 'SELL', 'offset': 'CLOSETODAY',
               'quantity': 1, 'remaining': 0, 'status': 'FINISHED', 'limit_price': '99', 'source_time_ns': ns('01:12:00')}]
    trades = [{key: row[key] for key in ('order_id', 'contract_id', 'direction', 'offset', 'quantity')} | {
        'trade_id': 'SHFE|'+row['order_id'], 'price': '100', 'source_time_ns': ns(time)}
        for row, time in zip(orders, ('01:10:01', '01:12:01'))]
    return {'schema_version': 'SIMNOW_FLAT_RECOVERY/1', 'account_key': 'SIM:SIMNOW:'+'a'*16,
            'trading_day': DAY, 'source_event_seq': '3260', 'observed_at_utc': DAY+'T01:15:00Z',
            'proof_sha256': ['a'*64], 'complete': True, 'positions': [],
            'funds': {'balance': '100000'}, 'orders': orders, 'trades': trades}


def calendar():
    return TradingCalendar.from_record({'schema_version': 'TRADING_CALENDAR/1', 'calendar_id': 'recovery-test',
        'calendar_version': '1', 'complete': True, 'coverage_from_utc': DAY+'T00:00:00Z',
        'coverage_to_utc': '2026-11-17T00:00:00Z', 'contracts': [{'contract_id': CODE, 'sessions': [{
            'session_id': 'morning', 'trading_day': DAY, 'open_utc': DAY+'T01:00:00Z',
            'close_utc': DAY+'T03:30:00Z', 'is_opening_session': True, 'force_close_at_end': True}]}]})


class RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self): self.value, self.calendar = evidence(), calendar()
    def reject(self):
        with self.assertRaises(TraderError): _check_pair(self.value, self.calendar)

    def test_exact_pair_preserves_native_identity_and_source(self):
        original = deepcopy(self.value)
        result = _check_pair(self.value, self.calendar)
        self.assertEqual(result[1]['trade_id'], 'SHFE|original-open')
        self.assertEqual(self.value, original)

    def test_pair_order_in_input_does_not_change_pairing(self):
        self.value['orders'].reverse(); self.value['trades'].reverse()
        self.assertEqual(_check_pair(self.value, self.calendar)[0]['offset'], 'OPEN')

    def test_account_scope_is_required(self):
        self.value['account_key'] = 'SIM:KQ:'+'a'*16; self.reject()

    def test_flat_snapshot_is_required(self):
        self.value['positions'] = [{'contract_id': CODE, 'long_qty': 1}]; self.reject()

    def test_incomplete_snapshot_is_refused(self):
        self.value['complete'] = False; self.reject()

    def test_evidence_digest_format_is_required(self):
        self.value['proof_sha256'] = ['not-a-digest']; self.reject()

    def test_missing_fill_is_refused(self):
        self.value['trades'].pop(); self.reject()

    def test_duplicate_trade_id_is_refused(self):
        self.value['trades'][1]['trade_id'] = self.value['trades'][0]['trade_id']; self.reject()

    def test_nonterminal_order_is_refused(self):
        self.value['orders'][0]['status'] = 'ALIVE'; self.reject()

    def test_remaining_quantity_is_refused(self):
        self.value['orders'][0]['remaining'] = 1; self.reject()

    def test_order_trade_quantity_conflict_is_refused(self):
        self.value['trades'][0]['quantity'] = 2; self.reject()

    def test_fill_outside_limit_is_refused(self):
        self.value['trades'][0]['price'] = '102'; self.reject()

    def test_fill_before_order_is_refused(self):
        self.value['trades'][0]['source_time_ns'] = ns('01:09:59'); self.reject()

    def test_fill_after_snapshot_is_refused(self):
        self.value['trades'][1]['source_time_ns'] = ns('01:16:00'); self.reject()

    def test_external_order_before_open_fill_is_refused(self):
        self.value['orders'][1]['source_time_ns'] = ns('01:10:00'); self.reject()

    def test_other_cabinet_day_is_refused(self):
        self.value['trading_day'] = '2026-11-17'; self.reject()

    def test_different_contract_is_refused(self):
        self.value['orders'][1]['contract_id'] = 'SHFE.ag2703'; self.reject()

    def test_close_yesterday_is_not_reinterpreted_as_today(self):
        self.value['orders'][1]['offset'] = self.value['trades'][1]['offset'] = 'CLOSE'; self.reject()

    def test_same_side_is_not_a_close(self):
        self.value['orders'][1]['direction'] = self.value['trades'][1]['direction'] = 'BUY'
        self.value['orders'][1]['limit_price'] = '101'; self.reject()

    def test_changed_review_hash_refuses_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'source.db', Path(directory)/'repaired'
            source.write_bytes(b'untouched-source')
            with self.assertRaises(TraderError) as caught:
                recover_flat(source, self.value, Path(directory)/'calendar.json', target, expected_hash='0'*64)
            self.assertEqual(caught.exception.code, 'E_DSV_RECOVERY')
            self.assertFalse(target.exists()); self.assertEqual(source.read_bytes(), b'untouched-source')

    def test_existing_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/'source.db', Path(directory)/'repaired'
            source.write_bytes(b'untouched-source'); target.mkdir(); (target/'keep').write_text('keep')
            with self.assertRaises(TraderError):
                recover_flat(source, self.value, Path(directory)/'calendar.json', target,
                             expected_hash=content_hash(self.value))
            self.assertEqual((target/'keep').read_text(), 'keep')
            self.assertEqual(source.read_bytes(), b'untouched-source')


if __name__ == '__main__': unittest.main()
