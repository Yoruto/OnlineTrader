"""Real migration projection + adapter check with SQLite and synthetic native facts.

No SDK connection or execution owner. Native identities come from the real port
codec; the minimal tables deliberately isolate migration from Windows locks.
"""
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
import unittest

from online_trader.contracts.codec import canonical_bytes
from online_trader.dsv_runtime.migration import _selected_native_history, RECOVERY_EVENT
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.errors import TraderError
from online_trader.gateway.ports import OnlineSimulationPort

CODE = 'SHFE.ag2702'
ACCOUNT = 'SIM:SIMNOW:' + 'a' * 16


class NativeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE ledger_scope(singleton INTEGER, account_key TEXT, environment TEXT, broker_identity TEXT);
            CREATE TABLE event_journal(event_seq INTEGER, event_type TEXT, payload_json TEXT);
            CREATE TABLE child_orders(child_order_id TEXT, intent_id TEXT, confirmed_fill_qty INTEGER,
                                      limit_price TEXT, original_qty INTEGER, offset_kind TEXT);
            CREATE TABLE order_intents(intent_id TEXT, position_cycle_id TEXT, effect TEXT);
            CREATE TABLE position_cycles(position_cycle_id TEXT, contract_id TEXT, direction TEXT);
            CREATE TABLE send_attempts(child_order_id TEXT, send_state TEXT);
            CREATE TABLE fill_facts(raw_fact_json TEXT);
        ''')
        self.db.execute('INSERT INTO ledger_scope VALUES(1,?,?,?)', (ACCOUNT, 'ONLINE_SIM', 'TQ_SIMNOW'))
        self.port = OnlineSimulationPort()
        self.port.trade_prefix = 'sn-'
        self.add_order()

    def add_order(self, child='child-1', native='native-1', quantity=1, filled=1, code=CODE, status='CONFIRMED'):
        self.db.execute('INSERT INTO event_journal VALUES(?,?,?)', (1, 'OBSERVED', canonical_bytes({'data': {
            'cycle': {'plan': {'contract_id': code}, 'native_order_keys': {child: native}}}}).decode()))
        self.db.execute('INSERT INTO child_orders VALUES(?,?,?,?,?,?)', (child, child+'-intent', filled, '100', quantity, 'OPEN'))
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?)', (child+'-intent', child+'-cycle', 'OPEN'))
        self.db.execute('INSERT INTO position_cycles VALUES(?,?,?)', (child+'-cycle', code, 'LONG'))
        self.db.execute('INSERT INTO send_attempts VALUES(?,?)', (child, status))

    def add_fill(self, native='T12345', child='child-1', quantity=1, price='100', **override):
        fact = self.port.trade_identity(native) | {'child_order_id': child, 'quantity': quantity, 'price': price}
        fact.update(override)
        self.db.execute('INSERT INTO fill_facts VALUES(?)', (canonical_bytes(fact).decode(),))
        return fact

    def history(self):
        return _selected_native_history(self.db, {CODE})

    def assert_rejected(self):
        with self.assertRaises(TraderError) as caught:
            self.history()
        self.assertEqual(caught.exception.code, 'E_DSV_MIGRATION_HISTORY')

    def recheck(self, history, trades):
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter._legacy_native_orders = history
        orders = {key: SimpleNamespace(status='FINISHED', exchange_id='SHFE', instrument_id='ag2702',
                   direction=row['direction'], offset=row['offset'], volume_orign=row['quantity'],
                   volume_left=row['quantity']-row['filled'], limit_price=float(row['limit_price']))
                  for key, row in history.items()}
        native = {tid: SimpleNamespace(order_id=order, exchange_id='SHFE', instrument_id='ag2702',
                  direction=history[order]['direction'], offset=history[order]['offset'], volume=qty, price=float(price))
                  for tid, order, qty, price in trades}
        adapter._check_legacy_orders(orders, native)

    def test_plain_simnow_id_survives_native_recheck(self):
        self.add_fill()
        result = self.history()
        self.assertEqual(result['native-1']['fills'], {'T12345': {'quantity': 1, 'price': '100'}})
        self.recheck(result, [('T12345', 'native-1', 1, '100')])

    def test_plain_kq_id_uses_bound_provider(self):
        self.db.execute('UPDATE ledger_scope SET account_key=?, broker_identity=?', ('SIM:KQ:'+'b'*16, 'TQKQ'))
        self.port.trade_prefix = 'kq-'
        self.add_fill('20261116-0001')
        result = self.history()
        self.recheck(result, [('20261116-0001', 'native-1', 1, '100')])
        self.assertIn('20261116-0001', result['native-1']['fills'])

    def test_encoded_simnow_id_keeps_original_identity(self):
        self.add_fill('SHFE|T12345')
        result = self.history()
        self.recheck(result, [('SHFE|T12345', 'native-1', 1, '100')])
        self.assertIn('SHFE|T12345', result['native-1']['fills'])

    def test_encoded_kq_id_keeps_original_identity(self):
        self.db.execute('UPDATE ledger_scope SET account_key=?, broker_identity=?', ('SIM:KQ:'+'b'*16, 'TQKQ'))
        self.port.trade_prefix = 'kq-'
        self.add_fill('EX|123')
        self.recheck(self.history(), [('EX|123', 'native-1', 1, '100')])

    def test_valid_explicit_id_also_supported(self):
        self.add_fill(native_trade_id='T12345')
        self.recheck(self.history(), [('T12345', 'native-1', 1, '100')])

    def test_legacy_namespace_cannot_alias_encoded_identity(self):
        encoded = self.port.trade_key('EX|123')
        self.db.execute('UPDATE child_orders SET original_qty=2, confirmed_fill_qty=2')
        self.add_fill('EX|123')
        self.add_fill(encoded)
        result = self.history()
        self.assertEqual(set(result['native-1']['fills']), {'EX|123', encoded})

    def test_multiple_partial_fills_are_all_retained(self):
        self.db.execute('UPDATE child_orders SET original_qty=5, confirmed_fill_qty=5')
        self.add_fill('A', quantity=3)
        self.add_fill('B|2', quantity=2, price='99')
        result = self.history()
        self.recheck(result, [('A', 'native-1', 3, '100'), ('B|2', 'native-1', 2, '99')])
        self.assertEqual(sum(row['quantity'] for row in result['native-1']['fills'].values()), 5)

    def test_missing_encoded_original_id_is_rejected(self):
        self.add_fill(trade_key=self.port.trade_key('EX|123'))
        self.assert_rejected()

    def test_changed_encoded_binding_is_rejected(self):
        self.add_fill('EX|123', native_trade_id='EX|456')
        self.assert_rejected()

    def test_conflicting_explicit_legacy_id_is_rejected(self):
        self.add_fill(native_trade_id='DIFFERENT')
        self.assert_rejected()

    def test_provider_prefix_mismatch_is_rejected(self):
        self.add_fill(trade_key='kq-T12345')
        self.assert_rejected()

    def test_unrecognized_trade_namespace_is_rejected(self):
        self.add_fill(trade_key='manual-T12345', native_trade_id='T12345')
        self.assert_rejected()

    def test_empty_legacy_identity_is_rejected(self):
        self.add_fill(trade_key='sn-')
        self.assert_rejected()

    def test_incomplete_fill_mapping_is_rejected(self):
        self.assert_rejected()

    def test_excess_fill_quantity_is_rejected(self):
        self.add_fill(quantity=2)
        self.assert_rejected()

    def test_duplicate_native_trade_is_not_silently_overwritten(self):
        self.db.execute('UPDATE child_orders SET original_qty=2, confirmed_fill_qty=2')
        self.add_fill('A')
        self.add_fill('A')
        self.assert_rejected()

    def test_duplicate_order_binding_is_rejected(self):
        self.add_fill('A')
        self.add_order(child='child-2', native='native-1')
        self.add_fill('B', child='child-2')
        self.assert_rejected()

    def test_confirmed_child_without_native_binding_is_rejected(self):
        self.db.execute('DELETE FROM event_journal')
        self.add_fill()
        self.assert_rejected()

    def test_zero_fill_cancelled_order_has_empty_mapping(self):
        self.db.execute('UPDATE child_orders SET confirmed_fill_qty=0')
        self.assertEqual(self.history()['native-1']['fills'], {})

    def test_definitely_unsent_child_is_not_a_native_order(self):
        self.db.execute("UPDATE send_attempts SET send_state='DEFINITELY_NOT_SENT'")
        self.assertEqual(self.history(), {})

    def test_unselected_contract_history_is_not_adopted(self):
        self.db.execute("UPDATE position_cycles SET contract_id='DCE.p2701'")
        self.assertEqual(self.history(), {})

    def test_unknown_provider_is_rejected(self):
        self.add_fill()
        self.db.execute("UPDATE ledger_scope SET broker_identity='OTHER'")
        self.assert_rejected()

    def test_inconsistent_account_provider_is_rejected(self):
        self.add_fill()
        self.db.execute("UPDATE ledger_scope SET account_key='SIM:KQ:aaaaaaaaaaaaaaaa'")
        self.assert_rejected()

    def test_invalid_fill_quantity_is_rejected(self):
        self.add_fill(quantity=0)
        self.assert_rejected()

    def test_reviewed_external_close_is_preserved(self):
        self.add_fill()
        order = {'order_id': 'external-close', 'contract_id': CODE, 'direction': 'SELL',
                 'limit_price': '98', 'quantity': 1, 'offset': 'CLOSETODAY'}
        trade = {'trade_id': 'EX|CLOSE', 'quantity': 1, 'price': '99'}
        self.db.execute('INSERT INTO event_journal VALUES(?,?,?)', (2, RECOVERY_EVENT, canonical_bytes({
            'data': {'allocation_review': {'external_order': order, 'external_trade': trade}}}).decode()))
        result = self.history()
        self.recheck(result, [('T12345', 'native-1', 1, '100'), ('EX|CLOSE', 'external-close', 1, '99')])

    def test_projection_is_read_only(self):
        self.add_fill('EX|123')
        before = '\n'.join(self.db.iterdump())
        self.history()
        self.assertEqual('\n'.join(self.db.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
