import copy
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.strategy.entry import DsvEntryStrategy, chase_within_cap, hysteresis_ready, opening_jump_block


def profile(**trade):
    settings = load_confirmed_baseline()[0].to_settings()
    settings["trade"].update(trade)
    return DsvProfile.from_settings(settings)


def event(**changes):
    result = {"account_key": "DEV:entry", "contract_id": "DCE.p2701", "trading_day": "2026-11-09",
        "session_id": "day-20261109", "source_epoch": 1, "sequence": 1,
        "source_time_utc": "2026-11-09T01:05:00Z", "session_open_utc": "2026-11-09T01:00:00Z",
        "session_close_utc": "2026-11-09T07:00:00Z", "last": "1055", "bid1": "1054", "ask1": "1056",
        "day_high": "1090", "day_low": "1000", "tick_size": "1", "quality": "CONTIGUOUS",
        "day_range_complete": True, "path_complete": True, "is_trading": True, "calendar_complete": True,
        "candle_id": "20261109-0105"}
    result.update(changes);return result


class DsvEntryTests(unittest.TestCase):
    def setup_strategy(self, parameters=None, fact=None):
        strategy = DsvEntryStrategy(parameters or profile())
        fact = fact or event()
        state = strategy.initial_state(fact["account_key"], fact["contract_id"], fact["trading_day"])
        return strategy, state

    def test_current_dsv_long_trigger_and_exact_opponent_without_global_hysteresis(self):
        strategy, state = self.setup_strategy()
        before = copy.deepcopy(state);result = strategy.evaluate(state, event())
        self.assertEqual(state, before)
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual((candidate["direction"], candidate["theoretical_price"], candidate["limit_price"]), ("LONG", "1055", "1056"))
        self.assertEqual((candidate["stop_distance"], candidate["take_profit_distance"]), ("16", "50"))
        self.assertFalse(result["state"]["hysteresis_armed"]["LONG"])
        self.assertEqual(result["state"]["last_signal_bar"], event()["candle_id"])
        canonical_bytes(result)

    def test_short_is_a_full_price_mirror(self):
        strategy, state = self.setup_strategy()
        result = strategy.evaluate(state, event(last="1035", bid1="1034", ask1="1036"))
        candidate = result["candidates"][0]
        self.assertEqual((candidate["direction"], candidate["theoretical_price"], candidate["limit_price"]), ("SHORT", "1035", "1034"))

    def test_candidate_identity_is_all_sides_and_does_not_change_with_profile(self):
        first, first_state = self.setup_strategy()
        second, second_state = self.setup_strategy(profile(max_volume=29))
        long = first.evaluate(first_state, event())["candidates"][0]
        short = second.evaluate(second_state, event(last="1035", bid1="1034", ask1="1036"))["candidates"][0]
        self.assertEqual(long["candle_key"], short["candle_key"])
        self.assertEqual(long["decision_key"], short["decision_key"])

    def test_same_candle_opportunity_is_shared_between_long_and_short(self):
        strategy, state = self.setup_strategy()
        first = strategy.evaluate(state, event())
        second = strategy.evaluate(first["state"], event(sequence=2, source_time_utc="2026-11-09T01:05:01Z",
            last="1035", bid1="1034", ask1="1036"))
        self.assertEqual(second["candidates"], [])
        self.assertIn("CANDLE_OPPORTUNITY_CONSUMED", second["reasons"])

    def test_owner_consumed_key_survives_reconstruction(self):
        strategy, state = self.setup_strategy()
        key = strategy.evaluate(state, event())["candidates"][0]["candle_key"]
        result = strategy.evaluate(state, event(consumed_candle_keys=[key]))
        self.assertIn("CANDLE_OPPORTUNITY_CONSUMED", result["reasons"])

    def test_wait_is_exact_and_default_continuation_does_not_bypass_it(self):
        strategy, state = self.setup_strategy()
        blocked = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:04:59.999999Z"))
        self.assertIn("OPENING_WAIT", blocked["reasons"])
        self.assertEqual(strategy.evaluate(state, event())["candidates"][0]["entry_kind"], "NORMAL")
        first_minute = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:00:20Z", last="1000", bid1="999", ask1="1001"))
        self.assertIn("OPENING_WAIT", first_minute["reasons"])
        self.assertEqual(first_minute["state"]["first_minute_blocks"], [])
        self.assertEqual(first_minute["source_notes"], [])

    def test_explicit_continuation_wait_waiver_only_removes_wait_gate(self):
        p = profile(continuation_ignore_no_open=True)
        strategy, state = self.setup_strategy(p)
        result = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:00:20Z"))
        self.assertNotIn("OPENING_WAIT", result["reasons"])
        self.assertEqual(result["candidates"][0]["entry_kind"], "CONTINUATION")
        self.assertIn("DSV_UNDEFINED_FIRST_MINUTE_CLOCK_REPAIRED", result["source_notes"])

    def test_final_close_ten_minute_boundary_is_inclusive(self):
        strategy, state = self.setup_strategy()
        result = strategy.evaluate(state, event(source_time_utc="2026-11-09T06:50:00Z"))
        self.assertIn("CLOSE_SESSION_NO_OPEN", result["reasons"])
        self.assertEqual(result["candidates"], [])

    def test_geometric_equality_passes_but_larger_range_blocks(self):
        strategy, state = self.setup_strategy()
        self.assertTrue(strategy.evaluate(state, event(day_high="1094"))["candidates"])
        result = strategy.evaluate(state, event(day_high="1095"))
        self.assertIn("GEOMETRY_BLOCK", result["reasons"])

    def test_no_chase_activation_includes_equality_and_respects_off_switch(self):
        settings = profile().to_settings();settings["symbols"][0]["stop_offset"] = 1
        p = DsvProfile.from_settings(settings);strategy, state = self.setup_strategy(p)
        result = strategy.evaluate(state, event(day_high="1095"))
        self.assertIn("LONG:NO_CHASE_AFTER_ACT", result["reasons"])
        settings["trade"]["no_chase_after_act"] = False
        strategy, state = self.setup_strategy(DsvProfile.from_settings(settings))
        self.assertTrue(strategy.evaluate(state, event(day_high="1095"))["candidates"])

    def test_disabled_protection_uses_trailing_activation_ratio(self):
        strategy, state = self.setup_strategy(profile(protect_80_enabled=False, trailing_activate_ratio=.6))
        result = strategy.evaluate(state, event(day_high="1085"))
        self.assertIn("LONG:NO_CHASE_AFTER_ACT", result["reasons"])

    def test_chase_helper_keeps_dsv_favourable_override_and_equality(self):
        self.assertTrue(chase_within_cap("LONG", "1055", "1100", "1055", "2"))
        self.assertTrue(chase_within_cap("LONG", "1057", "1057", "1055", "2"))
        self.assertFalse(chase_within_cap("LONG", "1058", "1058", "1055", "2"))
        self.assertTrue(chase_within_cap("SHORT", "1035", "1000", "1035", "2"))
        self.assertFalse(chase_within_cap("SHORT", "1032", "1032", "1035", "2"))

    def test_chase_rejection_consumes_candle_before_owner_risk(self):
        strategy, state = self.setup_strategy()
        result = strategy.evaluate(state, event(last="1058", bid1="1057", ask1="1059"))
        self.assertIn("LONG:CHASE_CAP_EXCEEDED", result["reasons"])
        self.assertEqual(result["state"]["last_signal_bar"], event()["candle_id"])

    def test_missing_opponent_cannot_inherit_original_helper_missing_price_exemption(self):
        strategy, state = self.setup_strategy()
        result = strategy.evaluate(state, event(ask1=None))
        self.assertIn("LONG:OPPONENT_PRICE_MISSING", result["reasons"])
        self.assertFalse(result["candidates"])

    def test_hysteresis_helper_uses_inclusive_reverse_boundary(self):
        self.assertEqual(hysteresis_ready("LONG", "1039", "1055", "16", False), (True, True))
        self.assertEqual(hysteresis_ready("LONG", "1040", "1055", "16", False), (False, False))
        self.assertEqual(hysteresis_ready("SHORT", "1051", "1035", "16", False), (True, True))
        self.assertEqual(hysteresis_ready("SHORT", "1035", "1035", "16", True), (True, True))

    def test_first_minute_observer_uses_both_absolute_distances_and_stops_whole_day(self):
        strategy, state = self.setup_strategy(profile(no_open_after_open=False))
        result = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:00:20Z", last="1010", bid1="1009", ask1="1011"))
        self.assertIn("FIRST_MINUTE_JUMP_DAY_STOP", result["reasons"])
        self.assertEqual(result["state"]["first_minute_blocks"], ["LONG", "SHORT"])
        later = strategy.evaluate(result["state"], event(sequence=2, candle_id="20261109-0106", source_time_utc="2026-11-09T01:06:00Z"))
        self.assertIn("FIRST_MINUTE_JUMP_DAY_STOP", later["reasons"])

    def test_need_hysteresis_is_session_observer_state_and_zero_falls_back_to_stop(self):
        settings = profile(no_open_after_open=False).to_settings();settings["symbols"][0]["stop_offset"] = 30
        strategy, state = self.setup_strategy(DsvProfile.from_settings(settings))
        first = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:00:20Z", day_high="1080", last="1026", bid1="1025", ask1="1027"))
        self.assertIn("LONG:HYSTERESIS_NOT_READY", first["reasons"])
        self.assertTrue(first["state"]["need_hyst"])
        second = strategy.evaluate(first["state"], event(sequence=2, candle_id="20261109-0101", source_time_utc="2026-11-09T01:01:01Z",
            day_high="1080", last="1025", bid1="1024", ask1="1026"))
        self.assertTrue(second["state"]["need_hyst"])
        self.assertTrue(second["state"]["hysteresis_armed"]["LONG"])

    def test_same_trigger_after_stop_is_allowed_when_dsv_switch_false(self):
        strategy, state = self.setup_strategy()
        self.assertTrue(strategy.evaluate(state, event(stop_trigger_prices={"LONG": "1055"}))["candidates"])
        strategy, state = self.setup_strategy(profile(require_trigger_update=True))
        rejected = strategy.evaluate(state, event(stop_trigger_prices={"LONG": "1055"}))
        self.assertIn("LONG:STOP_TRIGGER_UNCHANGED", rejected["reasons"])
        self.assertEqual(rejected["state"]["last_signal_bar"], event()["candle_id"])

    def test_duplicate_and_out_of_order_never_emit_again(self):
        strategy, state = self.setup_strategy();first = strategy.evaluate(state, event())
        self.assertEqual(strategy.evaluate(first["state"], event())["reasons"], ["DUPLICATE_MARKET_EVENT"])
        stale = event(sequence=1, source_time_utc="2026-11-09T01:05:01Z")
        with self.assertRaises(TraderError):strategy.evaluate(first["state"], stale)
        state2 = copy.deepcopy(first["state"]);state2["last_sequence"] = 3
        self.assertEqual(strategy.evaluate(state2, event(sequence=2))["reasons"], ["OUT_OF_ORDER_MARKET_EVENT"])

    def test_stop_trigger_comparison_keeps_dsv_absolute_tolerance(self):
        strategy, state = self.setup_strategy(profile(require_trigger_update=True))
        below = strategy.evaluate(state, event(stop_trigger_prices={"LONG": "1055.0000005"}))
        self.assertIn("LONG:STOP_TRIGGER_UNCHANGED", below["reasons"])
        boundary = strategy.evaluate(state, event(stop_trigger_prices={"LONG": "1055.000001"}))
        self.assertTrue(boundary["candidates"])

    def test_used_activation_consumes_bar_only_when_actual_trigger_is_reached(self):
        settings = profile().to_settings();settings["symbols"][0]["stop_offset"] = 1
        strategy, state = self.setup_strategy(DsvProfile.from_settings(settings))
        touched = strategy.evaluate(state, event(day_high="1095"))
        self.assertEqual(touched["state"]["last_signal_bar"], event()["candle_id"])
        waiting = strategy.evaluate(state, event(day_high="1095", last="1045", bid1="1044", ask1="1046"))
        self.assertIsNone(waiting["state"]["last_signal_bar"])

    def test_opening_chase_window_is_inclusive_and_does_not_apply_after_lunch(self):
        strategy, state = self.setup_strategy(profile(no_open_after_open=False))
        # cap is round(55*.05)=3 at 60 seconds, versus round(55*.03)=2 later.
        boundary = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:01:00Z",
            last="1058", bid1="1057", ask1="1058"))
        self.assertTrue(boundary["candidates"])
        after = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:01:00.000001Z",
            last="1058", bid1="1057", ask1="1058"))
        self.assertIn("LONG:CHASE_CAP_EXCEEDED", after["reasons"])
        lunch = strategy.evaluate(state, event(session_id="afternoon", session_open_utc="2026-11-09T05:30:00Z",
            source_time_utc="2026-11-09T05:30:20Z", last="1058", bid1="1057", ask1="1058"))
        self.assertIn("LONG:CHASE_CAP_EXCEEDED", lunch["reasons"])

    def test_calendar_can_explicitly_classify_a_night_open(self):
        strategy, state = self.setup_strategy(profile(no_open_after_open=False))
        result = strategy.evaluate(state, event(session_id="night", session_open_utc="2026-11-09T13:00:00Z",
            session_close_utc="2026-11-09T15:00:00Z", source_time_utc="2026-11-09T13:01:00Z",
            is_opening_session=True, last="1058", bid1="1057", ask1="1058"))
        self.assertTrue(result["candidates"])

    def test_gap_and_source_change_latch_incomplete_coverage(self):
        strategy, state = self.setup_strategy();first = strategy.evaluate(state, event())
        for changes in ({"sequence": 3}, {"sequence": 2, "source_epoch": 2}, {"sequence": 2, "quality": "DATA_GAP"}):
            result = strategy.evaluate(first["state"], event(candle_id="20261109-0106", **changes))
            self.assertIn("MARKET_PATH_INCOMPLETE", result["reasons"])
            self.assertFalse(result["candidates"])

    def test_owner_scope_profile_and_market_types_are_enforced(self):
        strategy, state = self.setup_strategy()
        for changes in ({"account_key": "DEV:other"}, {"trading_day": "2026-11-10"}, {"sequence": True},
                        {"last": 1055.0}, {"tick_size": "0"}, {"day_high": "900"}):
            with self.subTest(changes=changes), self.assertRaises(TraderError):strategy.evaluate(state, event(**changes))
        with self.assertRaises(TraderError):DsvEntryStrategy(profile(max_volume=5)).evaluate(state, event())

    def test_white_silver_zero_offsets_are_not_replaced_by_pdf_or_rollover(self):
        fact = event(contract_id="SHFE.ag2612")
        strategy, state = self.setup_strategy(fact=fact)
        result = strategy.evaluate(state, fact)
        self.assertIn("SYMBOL_OFFSETS_INCOMPLETE", result["reasons"])
        self.assertFalse(result["candidates"])

    def test_incomplete_calendar_range_and_path_are_explicit_reasons(self):
        strategy, state = self.setup_strategy()
        result = strategy.evaluate(state, event(calendar_complete=False, day_range_complete=False, path_complete=False))
        self.assertEqual(result["reasons"], ["MARKET_PATH_INCOMPLETE", "CALENDAR_INCOMPLETE", "DAY_RANGE_INCOMPLETE"])

    def test_jump_helper_uses_absolute_inclusive_distance(self):
        self.assertTrue(opening_jump_block("1015", "1055", "50", "0.8"))
        self.assertTrue(opening_jump_block("1095", "1055", "50", "0.8"))
        self.assertFalse(opening_jump_block("1016", "1055", "50", "0.8"))


if __name__ == "__main__":unittest.main()
