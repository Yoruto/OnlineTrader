import unittest
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.dsv_core.transition import ordinary_entry


def _base_inputs():
    settings = load_confirmed_baseline()[0].to_settings()["trade"]
    spec = {"open_offset": "55", "stop_offset": "16", "profit_offset": "50", "order_volume": 1}
    state = {
        "position": None,
        "target_count": 0,
        "net_loss": "0",
        "day_stop": False,
        "last_signal_bar": None,
        "last_stop": None,
        "last_order_ms": None,
        "last_order_direction": None,
        "expiry_days_checked": 30,
        "night_had_open_fill": False,
    }
    market = {
        "opening_session": False,
        "session_open": "2026-11-09T09:00:00+08:00",
        "session_close": "2026-11-09T15:00:00+08:00",
        "available": 1000000,
        "margin_per_lot": 1000,
        "tick": {
            "native_time": "2026-11-09T09:05:00+08:00",
            "candle_id": "20261109-0905",
            "last": "1055",
            "bid1": "1054",
            "ask1": "1055",
            "day_high": "1090",
            "day_low": "1000",
        },
    }
    frame = {"wall_time": "2026-11-09T09:05:00+08:00"}
    return state, frame, market, spec, settings


class DsvNewRequirementsTests(unittest.TestCase):
    def test_geometry_filter_blocks_on_exact_equality(self):
        """Rule 4: high - low >= 2 * opening - stop must block entry."""
        state, frame, market, spec, settings = _base_inputs()
        # 2 * 55 - 16 = 94; day_high=1094, day_low=1000 => range == 94
        market["tick"]["day_high"] = "1094"
        market["tick"]["day_low"] = "1000"
        market["tick"]["last"] = "1055"
        market["tick"]["ask1"] = "1055"
        res = ordinary_entry(state, frame, market, spec, settings, active_symbols=0, open_symbols=0)
        self.assertNotIn("target", res)
        self.assertTrue(res.get("geometry_block") or res["reason"] == "GEOMETRY_BLOCK")

    def test_empty_position_reaching_100pct_profit_is_blocked_by_geometry(self):
        """Rule 2: With default params, reaching 100% profit (range >= a + T = 105) blocks entry."""
        state, frame, market, spec, settings = _base_inputs()
        market["tick"]["day_high"] = "1105"
        market["tick"]["day_low"] = "1000"
        market["tick"]["last"] = "1055"
        res = ordinary_entry(state, frame, market, spec, settings, active_symbols=0, open_symbols=0)
        self.assertNotIn("target", res)
        self.assertTrue(res.get("geometry_block") or res["reason"] == "GEOMETRY_BLOCK")

    def test_morning_open_gap_over_3pct_without_night_fill_blocks_entry(self):
        """Rule 4: If night had no open fill, morning open gap > 3% of open_offset must block entry."""
        state, frame, market, spec, settings = _base_inputs()
        market["opening_session"] = True
        market["tick"]["native_time"] = "2026-11-09T09:00:10+08:00"
        frame["wall_time"] = "2026-11-09T09:00:10+08:00"
        # theory LONG = 1000 + 55 = 1055; 3% of 55 = 1.65; 1057 exceeds by 2 > 1.65 (but <= 5% = 2.75)
        market["tick"]["last"] = "1057"
        market["tick"]["ask1"] = "1057"
        res = ordinary_entry(state, frame, market, spec, settings, active_symbols=0, open_symbols=0)
        self.assertNotIn("target", res)


if __name__ == "__main__":
    unittest.main()
