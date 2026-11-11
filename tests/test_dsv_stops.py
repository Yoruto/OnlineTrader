import copy
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.strategy.stops import evaluate_stops, MONTH_STOP_REASON


class DsvStopTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_confirmed_baseline()[0]
        self.facts = {"net_loss_points": "0", "stop_streak": 0, "continuation_stopped": False,
            "day_stopped": False, "day_stopped_reason": None, "stop_days": [], "month_stop": None,
            "trading_days": ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"]}

    def evaluate(self, *, profile=None, today="2026-11-09", **changes):
        return evaluate_stops(profile or self.profile, "DCE.p2701", today, **(self.facts | changes))

    def test_nominal_limit_includes_equality_and_does_not_use_currency(self):
        self.assertTrue(self.evaluate(net_loss_points="31.99999999999")["allow_new_entries"])
        result = self.evaluate(net_loss_points="32")
        self.assertFalse(result["allow_new_entries"])
        self.assertIn("NET_LOSS_LIMIT", result["reasons"])
        self.assertEqual(result["state"]["stop_days"], ["2026-11-09"])
        self.assertEqual(result["state"]["day_stopped_reason"], "净亏达32点")
        self.assertIsNone(result["state"]["month_stop"])
        settings = self.profile.to_settings(); settings["trade"]["month_stop_days"] = 0
        disabled = self.evaluate(profile=DsvProfile.from_settings(settings), net_loss_points="32", stop_days=self.facts["trading_days"][:-1],
                                 trading_days=[])
        self.assertIsNone(disabled["state"]["month_stop"])
        self.assertNotIn("TRADING_CALENDAR_COVERAGE_INCOMPLETE", disabled["reasons"])
        self.assertEqual(disabled["state"]["stop_days"], self.facts["trading_days"][:-1])
        old = self.evaluate(profile=DsvProfile.from_settings(settings), month_stop="2026-10",
                            day_stopped=True, day_stopped_reason=MONTH_STOP_REASON)
        self.assertEqual(old["state"]["month_stop"], "2026-10")
        self.assertFalse(old["allow_new_entries"])
        canonical_bytes(result)

    def test_month_stop_requires_the_last_consecutive_explicit_trading_days(self):
        prior = ["2026-11-04", "2026-11-05", "2026-11-06"]
        result = self.evaluate(net_loss_points="32", stop_days=prior)
        self.assertEqual(result["state"]["month_stop"], "2026-11")
        self.assertEqual(result["state"]["day_stopped_reason"], MONTH_STOP_REASON)
        # Four stopped dates anywhere in the month are insufficient when a more
        # recent explicit trading day between them did not stop.
        result = self.evaluate(net_loss_points="32", stop_days=["2026-11-02", "2026-11-04", "2026-11-05"],
            trading_days=["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"])
        self.assertIsNone(result["state"]["month_stop"])

    def test_month_change_releases_only_the_month_stop_reason(self):
        result = self.evaluate(month_stop="2026-10", day_stopped=True, day_stopped_reason=MONTH_STOP_REASON)
        self.assertTrue(result["allow_new_entries"])
        self.assertIsNone(result["state"]["month_stop"])
        self.assertIsNone(result["state"]["day_stopped_reason"])
        result = self.evaluate(month_stop="2026-10", day_stopped=True, day_stopped_reason="PROTECTION_LOCK")
        self.assertFalse(result["allow_new_entries"])
        self.assertIsNone(result["state"]["month_stop"])
        self.assertEqual(result["state"]["day_stopped_reason"], "PROTECTION_LOCK")

    def test_calendar_shortfall_is_explicit_and_never_fabricates_weekdays(self):
        result = self.evaluate(net_loss_points="32", stop_days=["2026-11-05", "2026-11-06"],
                               trading_days=["2026-11-05", "2026-11-06", "2026-11-09"])
        self.assertIn("TRADING_CALENDAR_COVERAGE_INCOMPLETE", result["reasons"])
        self.assertIsNone(result["state"]["month_stop"])
        self.assertFalse(result["allow_new_entries"])
        result = self.evaluate(trading_days=[])
        self.assertTrue(result["allow_new_entries"])
        self.assertNotIn("TRADING_CALENDAR_COVERAGE_INCOMPLETE", result["reasons"])

    def test_stop_streak_and_continuation_switch_do_not_stop_ordinary_entries(self):
        result = self.evaluate(stop_streak=2)
        self.assertTrue(result["allow_new_entries"])
        self.assertFalse(result["allow_continuation"])
        self.assertIn("CONTINUATION_STOP_STREAK", result["reasons"])
        result = self.evaluate(continuation_stopped=True)
        self.assertTrue(result["allow_new_entries"])
        self.assertFalse(result["allow_continuation"])
        settings = self.profile.to_settings(); settings["trade"]["cont_stop_day_stop"] = False
        self.assertTrue(self.evaluate(profile=DsvProfile.from_settings(settings), continuation_stopped=True)["allow_continuation"])

    def test_history_is_idempotent_immutable_and_bounded_to_twelve(self):
        history = [f"2026-10-{day:02}" for day in range(1, 14)]
        before = copy.deepcopy(history)
        result = self.evaluate(net_loss_points="32", stop_days=history)
        self.assertEqual(history, before)
        self.assertEqual(len(result["state"]["stop_days"]), 12)
        self.assertEqual(result["state"]["stop_days"][-1], "2026-11-09")
        repeated = self.evaluate(net_loss_points="32", stop_days=result["state"]["stop_days"])
        self.assertEqual(repeated["state"]["stop_days"], result["state"]["stop_days"])

    def test_duplicate_invalid_and_coerced_inputs_are_rejected(self):
        for changes in ({"net_loss_points": 32.0}, {"net_loss_points": "NaN"}, {"stop_streak": True},
            {"continuation_stopped": 1}, {"day_stopped": "false"}, {"day_stopped_reason": "x\n"},
            {"stop_days": ["2026-11-09", "2026-11-09"]}, {"stop_days": ["2026-11-10"]},
            {"trading_days": ["2026-11-09", "2026-11-09"]}, {"trading_days": ["2026-02-30"]},
            {"month_stop": "2026-13"}):
            with self.subTest(changes=changes), self.assertRaises(TraderError) as caught:
                self.evaluate(**changes)
            self.assertEqual(caught.exception.code, "E_STRATEGY_STOP_INPUT")


if __name__ == "__main__":unittest.main()
