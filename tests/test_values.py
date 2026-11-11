from decimal import Decimal
import unittest

from online_trader.domain.values import decimal_value, order_price, quantity, sequence
from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError


class ValueTests(unittest.TestCase):
    def test_exact_price_points_are_not_ticks(self):
        self.assertEqual(decimal_value("65"),Decimal(65))
        self.assertEqual(order_price("1005","5"),Decimal(1005))
        with self.assertRaises(TraderError): order_price("1001","5")

    def test_invalid_values_cannot_become_defaults(self):
        for value in ["NaN","Infinity","-0","1e3","0.80",1.2,True]:
            with self.subTest(value=value),self.assertRaises(TraderError):decimal_value(value)
        for value in [True,-1,1.0,2_147_483_648]:
            with self.subTest(value=value),self.assertRaises(TraderError):quantity(value)
        with self.assertRaises(TraderError):sequence("9223372036854775808")

    def test_cancellation_and_terminal_without_details_do_not_release(self):
        order=OrderState("child","intent","attempt","REDUCE",10,"1000",filled_qty=3,cancel_status="CALL_RETURNED")
        self.assertEqual(order.potential_remaining,7)
        order.terminal_confirmed=True;order.reported_fill_qty=5
        self.assertEqual(order.potential_remaining,7)
        order.filled_qty=5;order.send_state='CONFIRMED'
        self.assertEqual(order.potential_remaining,0)
