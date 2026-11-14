import sys
import tempfile
from pathlib import Path
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.runner import DsvSession
from qa.recording_port import RecordingPort


CONTRACT = "DCE.p2701"


def round_frame(sequence, prices, *, owned=0, facts=(), bid=None, ask=None):
    wall = f"2026-11-13T01:10:{sequence:02d}Z"
    ticks = [{"native_time": wall, "candle_id": "bar-1", "last": str(price),
              "day_low": "1020", "day_high": "1088", "bid1": str(price if bid is None else bid),
              "ask1": str(price if ask is None else ask), "tick_size": "1",
              "lower_limit": "900", "upper_limit": "1200"} for price in prices]
    market = {"contract_id": CONTRACT, "ticks": ticks, "opening_session": True,
              "session_open": "2026-11-13T01:00:00Z", "session_close": "2026-11-13T02:15:00Z",
              "long_qty": owned, "short_qty": 0, "available": "100000", "margin_per_lot": "1000"}
    return {"id": f"round-{sequence}", "sequence": sequence, "session_id": "session-1",
            "wall_time": wall, "monotonic_ms": sequence * 1000, "market": [market], "native": list(facts)}


def order_fact(child, status, filled):
    return {"kind": "ORDER", "child_order_id": child, "native_order_id": child + "|native",
            "status": status, "filled_qty": filled}


def trade_fact(child, native_trade_id, quantity, price):
    return {"kind": "TRADE", "child_order_id": child, "native_order_id": child + "|native",
            "native_trade_id": native_trade_id, "quantity": quantity, "price": str(price)}


@unittest.skipUnless(sys.platform == "win32", "Uses the Windows account owner lock")
class DsvRoundReplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        settings = load_confirmed_baseline()[0].to_settings()
        settings["symbols"] = [settings["symbols"][0] | {
            "open_offset": 40, "stop_offset": 8, "profit_offset": 20, "order_volume": 5,
        }]
        settings["trade"].update(no_chase_after_act=False, continuation_entry=False,
                                 protect_80_enabled=False, trailing_activate_ratio=0,
                                 passive_entry_first=False)
        self.profile = DsvProfile.from_settings(settings)
        self.port = RecordingPort()
        self.session = self.enterContext(DsvSession(Path(self.directory) / "facts.db", account_key="DEV:round-regression",
                                       provider="OFFLINE", trading_day="2026-11-13", profile=self.profile, port=self.port))
        self.session.command({"id": "start", "action": "START"})

    def test_caller_mutation_cannot_turn_cached_non_signal_into_order(self):
        frame = round_frame(1, [1060, 1054])
        self.assertEqual(self.session.apply_round(frame)["effects"], [])
        frame["market"][0]["ticks"][-1].update(last="1060", bid1="1060", ask1="1060")
        result = self.session.apply_round(round_frame(2, []))
        self.assertEqual(result["decisions"][0]["reason"], "TRIGGER_NOT_REACHED")
        self.assertEqual(self.port.orders, [])

    def test_terminal_before_late_fill_reserves_original_child_and_replaces_only_remaining(self):
        first = self.session.apply_round(round_frame(1, [1060]))["effects"][0]
        child = first["child_order_id"]
        self.assertEqual((first["quantity"], first["limit_price"]), (5, "1060"))
        cancelled = self.session.apply_round(round_frame(2, [1060], owned=2, ask=1061, facts=[
            order_fact(child, "ALIVE", 2), trade_fact(child, "trade-1", 2, 1060),
        ]))
        self.assertEqual([effect["kind"] for effect in cancelled["effects"]], ["CANCEL"])
        incomplete = self.session.apply_round(round_frame(3, [1060], owned=3, ask=1061,
                                               facts=[order_fact(child, "CANCELLED", 3)]))
        self.assertEqual(incomplete["effects"], [])
        completed = self.session.apply_round(round_frame(4, [1060], owned=3, ask=1061,
                                              facts=[trade_fact(child, "trade-2", 1, 1060)]))
        replacement, = completed["effects"]
        self.assertEqual((replacement["quantity"], replacement["limit_price"]), (2, "1061"))
        orders = self.session.snapshot()["orders"]
        self.assertEqual(orders[replacement["child_order_id"]]["parent_child_order_id"], child)
        self.assertEqual(orders[replacement["child_order_id"]]["intent_id"], orders[child]["intent_id"])


if __name__ == "__main__":
    unittest.main()
