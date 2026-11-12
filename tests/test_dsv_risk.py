import copy
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.strategy.risk import calculate_daily_loss, evaluate_entry


class DsvRiskTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_confirmed_baseline()[0]
        self.account = {"account_key": "DEV:risk", "trading_day": "2026-11-09", "mode": "simulation", "revision": 1,
            "complete": True, "equity": "100000", "available": "80000", "margin": "10000",
            "frozen_margin": "1000", "pending_margin": "2000", "symbol_margin": "1000",
            "risk_ratio": "0.13", "daily_loss": "0", "orders_today": 0,
            "active_symbols": [], "stopped_symbols": [], "unknown_orders": False}
        self.instrument = {"contract_id": "DCE.p2701", "trading_day": "2026-11-09", "complete": True,
            "tick_size": "1", "multiplier": "10", "margin_rate": "0.1", "margin_per_lot": "1000",
            "lower_limit": "800", "upper_limit": "1200", "min_order_qty": 1, "max_order_qty": 100,
            "days_to_expiry": 30}

    def configured(self, **trade):
        settings = self.profile.to_settings(); settings["trade"].update(trade)
        return DsvProfile.from_settings(settings)

    def evaluate(self, profile=None, **kwargs):
        return evaluate_entry(profile or self.profile, direction=kwargs.pop("direction", "LONG"),
            contract_id=kwargs.pop("contract_id", "DCE.p2701"), stop_distance=kwargs.pop("stop_distance", "16"),
            limit_price=kwargs.pop("limit_price", "1000"), requested_qty=kwargs.pop("requested_qty", 0),
            account=kwargs.pop("account", self.account), instrument=kwargs.pop("instrument", self.instrument), **kwargs)

    def test_baseline_decimal_sizing_uses_effective_minimum_cap_and_quote_margin(self):
        before = copy.deepcopy((self.account, self.instrument))
        result = self.evaluate()
        self.assertTrue(result["allowed"])
        self.assertEqual(result["quantity"], 1)
        self.assertEqual(result["metrics"]["effective_risk_cap"], "0.7")
        self.assertEqual(result["metrics"]["used_margin"], "13000")
        self.assertEqual(result["metrics"]["margin_per_lot"], "1000")
        self.assertEqual(result["metrics"]["risk_based_quantity"], 1)
        self.assertEqual(result["metrics"]["symbol_margin_budget"], "13000")
        self.assertEqual((self.account, self.instrument), before)
        canonical_bytes(result)

    def test_margin_fallback_is_fifteen_percent_not_the_margin_rate_field(self):
        for value in (None, "0", "-1"):
            with self.subTest(value=value):
                self.instrument["margin_per_lot"] = value
                self.instrument["margin_rate"] = "0.9"
                result = self.evaluate()
                self.assertTrue(result["allowed"])
                self.assertEqual(result["metrics"]["margin_per_lot"], "1500")
                self.assertEqual(result["metrics"]["margin_source"], "PRICE_MULTIPLIER_15_PERCENT")

    def test_margin_fallback_uses_market_last_and_validates_the_order_limit_separately(self):
        self.instrument['margin_per_lot'] = None
        result = self.evaluate(market_price='900', limit_price='1000')
        self.assertTrue(result['allowed'])
        self.assertEqual(result['metrics']['margin_per_lot'], '1350')
        invalid = self.evaluate(market_price='900', limit_price='1300')
        self.assertIn('PRICE_OUTSIDE_LIMITS', invalid['reasons'])

    def test_zero_risk_volume_does_not_replace_positive_margin_volume_with_one(self):
        self.account.update(equity="1000", available="1000", margin="0", frozen_margin="0", pending_margin="0",
                            symbol_margin="0", risk_ratio="0")
        self.instrument["margin_per_lot"] = "10"
        for fallback in (True, False):
            result = self.evaluate(self.configured(min_one_lot_fallback=fallback))
            self.assertTrue(result["allowed"])
            self.assertEqual(result["metrics"]["risk_based_quantity"], 0)
            self.assertEqual(result["quantity"], 14)
            self.assertFalse(result["metrics"]["min_one_lot_used"])

    def test_minimum_one_lot_requires_available_and_total_margin_capacity(self):
        self.account.update(available="10000", margin="0", frozen_margin="0", pending_margin="0",
                            symbol_margin="13950", risk_ratio="0")
        self.instrument["margin_per_lot"] = "100"
        result = self.evaluate()
        self.assertEqual(result["quantity"], 1)
        self.assertTrue(result["metrics"]["min_one_lot_used"])
        self.assertFalse(self.evaluate(self.configured(min_one_lot_fallback=False))["allowed"])
        self.account["available"] = "99.99"
        self.assertFalse(self.evaluate()["allowed"])
        self.account.update(available="10000", pending_margin="69950")
        self.assertFalse(self.evaluate()["allowed"])

    def test_fixed_branch_caps_available_and_max_volume_before_auto_risk_gates(self):
        settings = self.profile.to_settings(); settings["symbols"][0]["order_volume"] = 40
        fixed = DsvProfile.from_settings(settings)
        self.account.update(available="3500", margin="80000", frozen_margin="0", pending_margin="0", risk_ratio="0.8")
        result = self.evaluate(fixed, requested_qty=40)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["quantity"], 3)
        self.assertEqual(result["metrics"]["sizing_branch"], "FIXED")
        self.account["available"] = "50000"
        self.assertEqual(self.evaluate(fixed, requested_qty=40)["quantity"], 30)
        self.instrument["max_order_qty"] = 2
        self.assertEqual(self.evaluate(fixed, requested_qty=40)["quantity"], 2)

    def test_automatic_risk_ratio_at_effective_cap_is_blocked(self):
        self.account["risk_ratio"] = "0.7"
        result = self.evaluate()
        self.assertFalse(result["allowed"])
        self.assertIn("EFFECTIVE_RISK_CAP", result["reasons"])
        self.account["risk_ratio"] = "0.69999999999"
        self.assertTrue(self.evaluate()["allowed"])

    def test_stopped_configured_symbols_change_share_not_active_holdings(self):
        self.account["stopped_symbols"] = ["DCE.l2701", "SHFE.bu2701", "CZCE.OI701", "SHFE.ag2612"]
        result = self.evaluate()
        self.assertEqual(result["metrics"]["participating_configured_symbols"], 1)
        self.assertEqual(result["metrics"]["symbol_margin_budget"], "69000")
        self.account["active_symbols"] = ["OTHER.contract"]
        self.assertEqual(self.evaluate()["metrics"]["symbol_margin_budget"], "69000")
        self.account["stopped_symbols"].append("DCE.p2701")
        self.assertIn("SYMBOL_DAY_STOPPED", self.evaluate()["reasons"])

    def test_disabled_saved_live_and_symbol_limits_do_not_act_as_enabled_limits(self):
        self.account.update(daily_loss="9000", active_symbols=["DEV.A", "DEV.B", "DEV.C", "DEV.D"])
        self.assertTrue(self.evaluate()["allowed"])
        result = self.evaluate(self.configured(max_symbols_enabled=True))
        self.assertIn("ACTIVE_SYMBOL_LIMIT", result["reasons"])
        self.account["active_symbols"][-1] = "DCE.p2701"
        self.assertTrue(self.evaluate(self.configured(max_symbols_enabled=True))["allowed"])

    def test_daily_order_boundary_and_pending_margin_occupancy(self):
        self.account["orders_today"] = 199
        self.assertTrue(self.evaluate()["allowed"])
        self.account["orders_today"] = 200
        self.assertIn("DAILY_ORDER_LIMIT", self.evaluate()["reasons"])
        self.account.update(orders_today=0, pending_margin="60000")
        self.assertFalse(self.evaluate()["allowed"])

    def test_unknown_and_nonpositive_account_facts_never_become_zero_or_unlimited(self):
        for field, value, reason in (("complete", False, "ACCOUNT_FACTS_INCOMPLETE"),
                ("unknown_orders", True, "UNKNOWN_ORDERS"), ("equity", None, "ACCOUNT_VALUES_UNKNOWN"),
                ("available", None, "ACCOUNT_VALUES_UNKNOWN"), ("equity", "0", "EQUITY_NOT_POSITIVE"),
                ("equity", "-1", "EQUITY_NOT_POSITIVE"), ("margin", "-1", "ACCOUNT_VALUES_INVALID")):
            account = copy.deepcopy(self.account); account[field] = value
            with self.subTest(field=field, value=value):
                result = self.evaluate(account=account)
                self.assertFalse(result["allowed"])
                self.assertIn(reason, result["reasons"])

    def test_scope_profile_offsets_and_quantities_are_bound(self):
        instrument = self.instrument | {"trading_day": "2026-11-10"}
        self.assertIn("FACT_SCOPE_MISMATCH", self.evaluate(instrument=instrument)["reasons"])
        instrument = self.instrument | {"account_key": "DEV:other"}
        self.assertIn("FACT_SCOPE_MISMATCH", self.evaluate(instrument=instrument)["reasons"])
        self.assertIn("CONFIGURATION_MISMATCH", self.evaluate(stop_distance="1")["reasons"])
        self.assertIn("CONFIGURATION_MISMATCH", self.evaluate(requested_qty=1)["reasons"])
        instrument = self.instrument | {"contract_id": "SHFE.ag2612"}
        result = self.evaluate(contract_id="SHFE.ag2612", stop_distance="0", instrument=instrument)
        self.assertIn("SYMBOL_OFFSETS_INCOMPLETE", result["reasons"])

    def test_legal_price_tick_quantity_and_expiry_bounds(self):
        self.assertIn("PRICE_NOT_ALIGNED", self.evaluate(limit_price="1000.5")["reasons"])
        self.assertIn("PRICE_OUTSIDE_LIMITS", self.evaluate(limit_price="1201")["reasons"])
        self.assertTrue(self.evaluate(limit_price="1200")["allowed"])
        self.instrument["min_order_qty"] = 2
        self.assertIn("INSUFFICIENT_QUANTITY", self.evaluate()["reasons"])
        self.instrument.update(min_order_qty=1, days_to_expiry=4)
        self.assertIn("EXPIRY_ENTRY_BLOCK", self.evaluate()["reasons"])
        self.instrument["days_to_expiry"] = -1
        self.assertIn("EXPIRY_ENTRY_BLOCK", self.evaluate()["reasons"])
        self.instrument["days_to_expiry"] = 5
        self.assertIn("EXPIRY_ENTRY_BLOCK", self.evaluate()["reasons"])
        self.instrument["days_to_expiry"] = 6
        self.assertTrue(self.evaluate()["allowed"])
        self.instrument["days_to_expiry"] = None
        self.assertIn("EXPIRY_UNKNOWN", self.evaluate()["reasons"])
        self.assertTrue(self.evaluate(self.configured(rollover_stop_enabled=False))["allowed"])

    def test_bool_float_noncanonical_and_malformed_shapes_are_rejected(self):
        cases = [{"revision": True}, {"orders_today": True}, {"complete": 1}, {"equity": 100000.0},
                 {"equity": "1e5"}, {"available": "NaN"}, {"active_symbols": [{}]},
                 {"stopped_symbols": ["DCE.p2701", "DCE.p2701"]}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(TraderError) as caught:
                self.evaluate(account=self.account | changes)
            self.assertEqual(caught.exception.code, "E_RISK_INPUT")

    def test_aggressive_uses_symbol_budget_and_ignores_total_budget_and_single_risk_size(self):
        self.account.update(margin="69000", frozen_margin="0", pending_margin="0", symbol_margin="1000",
                            risk_ratio="0.69", available="30000")
        result = self.evaluate(self.configured(aggressive_mode=True))
        self.assertTrue(result["allowed"])
        self.assertEqual(result["quantity"], 13)
        self.assertEqual(result["metrics"]["risk_based_quantity"], 1)
        self.account["risk_ratio"] = "0.7"
        self.assertIn("EFFECTIVE_RISK_CAP", self.evaluate(self.configured(aggressive_mode=True))["reasons"])

    def test_live_protection_applies_only_in_live_mode_and_caps_automatic_branch(self):
        self.account.update(equity="1000", available="1000", margin="0", frozen_margin="0", pending_margin="0",
                            symbol_margin="0", risk_ratio="0", daily_loss="0")
        self.instrument["margin_per_lot"] = "10"
        protected = self.configured(live_protect=True)
        self.assertEqual(self.evaluate(protected)["quantity"], 14)
        self.account["mode"] = "live"
        self.assertEqual(self.evaluate(protected)["quantity"], 2)
        self.assertEqual(self.evaluate(self.configured(live_protect=True, live_first_one_lot=True))["quantity"], 1)
        self.assertEqual(self.evaluate(self.configured(live_protect=True, live_max_lots=0))["quantity"], 1)
        settings = protected.to_settings(); settings["symbols"][0]["order_volume"] = 7
        self.assertEqual(self.evaluate(DsvProfile.from_settings(settings), requested_qty=7)["quantity"], 7)

    def test_live_daily_loss_boundary_is_entry_gate_and_does_not_create_exit(self):
        protected = self.configured(live_protect=True)
        self.account.update(mode="live", daily_loss="4999.99")
        self.assertTrue(self.evaluate(protected)["allowed"])
        self.account["daily_loss"] = "5000"
        result = self.evaluate(protected)
        self.assertIn("LIVE_DAILY_LOSS_LIMIT", result["reasons"])
        self.assertEqual(result["quantity"], 0)
        self.assertNotIn("exit", result)
        self.account["daily_loss"] = None
        self.assertIn("DAILY_LOSS_UNKNOWN", self.evaluate(protected)["reasons"])
        self.assertTrue(self.evaluate(self.configured(live_protect=True, live_daily_loss_limit=0))["allowed"])
        self.account["mode"] = "simulation"
        self.assertTrue(self.evaluate(protected)["allowed"])

    def test_daily_loss_source_formula_uses_balance_or_profit_components_without_double_fee(self):
        self.assertEqual(calculate_daily_loss(balance="95000", pre_balance="100000", close_profit="-1",
            float_profit="-1", commission="100"), "5000")
        self.assertEqual(calculate_daily_loss(balance="1", pre_balance="0", close_profit="-4000",
            float_profit="-900", commission="100"), "5000")
        self.assertEqual(calculate_daily_loss(balance=None, pre_balance="0", close_profit="1000",
            float_profit="20", commission="10"), "0")
        self.assertIsNone(calculate_daily_loss(balance="100000", pre_balance=None, close_profit="0",
            float_profit="0", commission="0"))
        self.assertIsNone(calculate_daily_loss(balance=None, pre_balance="100000", close_profit="0",
            float_profit="0", commission="0"))


if __name__ == "__main__":unittest.main()
