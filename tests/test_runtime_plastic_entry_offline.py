from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from qa.runtime_fixture import OfflineRuntimeFixture


class PlasticOfflineEntryTests(unittest.TestCase):
    """Independent black-box scenario for the user-confirmed plastic parameters."""

    def test_plastic_crosses_long_trigger_and_reaches_native_send_boundary(self):
        contract = 'DCE.l2701'
        parameters = {
            'open_offset': 79,
            'stop_offset': 20,
            'profit_offset': 72,
            'half_close_ratio': 0.5,
            'hysteresis_points': 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            fixture = OfflineRuntimeFixture(
                Path(directory), quantity=0, contract=contract, symbol_changes=parameters,
                initial_price=7950, initial_high=8000, initial_low=7900,
            )
            try:
                self.assertTrue(fixture.port.broker_connected)
                self.assertEqual(fixture.api.inserted, [])
                self.assertEqual(fixture.api.orders, {})
                position = fixture.api.positions[contract]
                self.assertEqual((position.pos_long, position.pos_short), (0, 0))
                quote = fixture.api.quotes[contract]
                quote.lower_limit = 7000.0
                quote.upper_limit = 9000.0

                original_insert = fixture.api.insert_order
                with patch.object(fixture.api, 'insert_order', wraps=original_insert) as insert_spy:
                    for price in (7960, 7970, 7978, 7979):
                        fixture.quote(
                            price, milliseconds=20, high=8000, low=7900,
                            bid=price - 1, ask=price + 1,
                        )

                decision = fixture.session.last_decisions[contract]
                self.assertEqual(insert_spy.call_count, 1, json.dumps(
                    {'decision': decision, 'diagnostics': fixture.kernel._dsv_diagnostics},
                    ensure_ascii=False, sort_keys=True, default=str))
                self.assertEqual(len(fixture.api.inserted), 1)
                order = fixture.api.inserted[0]
                self.assertEqual(order['symbol'], contract)
                self.assertEqual((order['direction'], order['offset']), ('BUY', 'OPEN'))
                self.assertGreater(order['volume'], 0)
                self.assertIsInstance(order['volume'], int)
                self.assertEqual(order['limit_price'], 7980.0)

                self.assertNotIn('E_OWNERSHIP', decision.get('reasons', []))
                self.assertEqual(decision['status'], 'ENTRY_COMMITTED')
                cycle = next(iter(fixture.kernel._staged.values()))
                self.assertEqual((cycle['plan']['contract_id'], cycle['plan']['direction']),
                                 (contract, 'LONG'))
                self.assertEqual(cycle['entry_intent']['status'], 'BROKER_WORKING')

                evidence = {
                    'environment': 'OFFLINE_NATIVE_FACT_API_NO_NETWORK',
                    'broker_connected': fixture.port.broker_connected,
                    'real_broker_connections': 0,
                    'real_orders': 0,
                    'contract': contract,
                    'parameters': parameters | {'order_volume': 'auto'},
                    'daily_high': 8000,
                    'daily_low': 7900,
                    'amplitude': 100,
                    'geometry_limit': 138,
                    'long_trigger': 7979,
                    'path': [7950, 7960, 7970, 7978, 7979],
                    'native_interface': 'NativeFactApi.insert_order',
                    'native_call_count': insert_spy.call_count,
                    'order': {key: order[key] for key in
                              ('symbol', 'direction', 'offset', 'volume', 'limit_price')},
                    'decision_status': decision['status'],
                    'decision_reasons': decision.get('reasons', []),
                    'ownership_blocked': 'E_OWNERSHIP' in decision.get('reasons', []),
                }
                print('PLASTIC_OFFLINE_EVIDENCE=' + json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True))
            finally:
                fixture.close()


if __name__ == '__main__':
    unittest.main()
