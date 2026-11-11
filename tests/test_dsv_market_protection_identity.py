from pathlib import Path
import tempfile
import unittest
import uuid

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.kernel import Kernel
from online_trader.kernel.dsv_strategy import DsvStrategyRuntime
from qa.recording_port import RecordingPort


class DsvMarketProtectionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = "DEV:protection-source-" + uuid.uuid4().hex
        self.port = RecordingPort()
        self.kernel = Kernel(Path(self.temp.name) / "ledger.db", self.key,
                             development=True, port=self.port, risk_budget="1000000")
        settings = load_confirmed_baseline()[0].to_settings()
        settings["trade"].update(half_close_enabled=True, half_stage2=True, half_breakeven=False)
        next(row for row in settings["symbols"] if row["code"] == "DCE.p2701")["order_volume"] = 5
        self.runtime = DsvStrategyRuntime(self.kernel, profile=DsvProfile.from_settings(settings))
        self.market = {"account_key": self.key, "contract_id": "DCE.p2701", "trading_day": "2026-11-09",
            "session_id": "day-20261109", "source_epoch": 1, "sequence": 1,
            "source_time_utc": "2026-11-09T01:05:00Z", "session_open_utc": "2026-11-09T01:00:00Z",
            "session_close_utc": "2026-11-09T07:00:00Z", "last": "1055", "bid1": "1054", "ask1": "1056",
            "day_high": "1090", "day_low": "1000", "tick_size": "1", "quality": "CONTIGUOUS",
            "day_range_complete": True, "path_complete": True, "is_trading": True,
            "calendar_complete": True, "candle_id": "20261109-0105"}
        self.account = {"account_key": self.key, "trading_day": "2026-11-09", "mode": "simulation",
            "revision": 1, "complete": True, "equity": "300000", "available": "200000", "margin": "10000",
            "frozen_margin": "1000", "pending_margin": "0", "symbol_margin": "1000", "risk_ratio": "0.1",
            "daily_loss": "0", "orders_today": 0, "active_symbols": [], "stopped_symbols": [],
            "unknown_orders": False, "local_reservations_excluded": True}
        self.instrument = {"contract_id": "DCE.p2701", "trading_day": "2026-11-09", "complete": True,
            "tick_size": "1", "multiplier": "10", "margin_rate": "0.1", "margin_per_lot": "1000",
            "lower_limit": "800", "upper_limit": "1200", "min_order_qty": 1, "max_order_qty": 100,
            "days_to_expiry": 30}
        self.runtime.activate()
        self.kernel.arm_development(); self.kernel.wait_idle()
        child = self.observe(self.market)["child_order_id"]
        self.kernel.record_fill(child, "source-identity-entry", 5, "1055"); self.kernel.wait_idle()
        self.cycle_id = self.kernel.snapshot()["cycles"][0]["position_cycle_id"]
        self.kernel.reconcile_development_cycle(self.cycle_id); self.kernel.wait_idle()

    def tearDown(self):
        try:
            self.kernel.close()
        finally:
            self.temp.cleanup()

    def observe(self, event):
        future = self.runtime.on_market(event, self.account, self.instrument)
        self.kernel.wait_idle()
        return future.result()

    def cycle(self):
        return self.kernel.snapshot()["cycles"][0]

    def test_duplicate_source_quote_cannot_consume_second_stage_or_refresh_executable_quote(self):
        quote = self.market | {"sequence": 2, "source_time_utc": "2026-11-09T01:05:01Z",
                               "last": "1090", "bid1": "1089", "ask1": "1091"}
        self.observe(quote)
        first = self.cycle()
        self.assertEqual(first["protection"]["target_remaining_qty"], 3)
        self.assertEqual(first["protection"]["phase2_status"], "NOT_TRIGGERED")
        self.assertEqual([o["original_qty"] for o in self.port.orders if o["effect"] == "REDUCE"], [2])
        report = self.observe(quote)
        duplicate = self.cycle()
        self.assertIn("DUPLICATE_MARKET_EVENT", report["reasons"])
        self.assertEqual(duplicate["protection"], first["protection"])
        self.assertEqual(duplicate["last_quote"], first["last_quote"])
        self.assertEqual([o["original_qty"] for o in self.port.orders if o["effect"] == "REDUCE"], [2])
        self.observe(quote | {"sequence": 3, "source_time_utc": "2026-11-09T01:05:02Z"})
        self.assertEqual(self.cycle()["protection"]["target_remaining_qty"], 1)
        self.assertEqual([o["original_qty"] for o in self.port.orders if o["effect"] == "REDUCE"], [2, 2])

    def test_regressive_source_time_cannot_update_existing_position_protection(self):
        first = self.market | {"sequence": 2, "source_time_utc": "2026-11-09T01:05:10Z",
                               "last": "1075", "bid1": "1074", "ask1": "1076"}
        self.observe(first)
        accepted = self.cycle()
        report = self.observe(first | {"sequence": 3, "source_time_utc": "2026-11-09T01:05:05Z",
                                      "last": "1090", "bid1": "1089", "ask1": "1091"})
        self.assertIn("OUT_OF_ORDER_SOURCE_TIME", report["reasons"])
        self.assertEqual(self.cycle()["protection"], accepted["protection"])
        self.assertEqual(self.cycle()["last_quote"], accepted["last_quote"])
        self.assertEqual([o["original_qty"] for o in self.port.orders if o["effect"] == "REDUCE"], [2])


if __name__ == "__main__":
    unittest.main()
