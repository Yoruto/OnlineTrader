import copy
from decimal import Decimal
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.strategy.entry import (
    DsvEntryStrategy, chase_within_cap, continuation_direction, hysteresis_ready, opening_jump_block,
)


def profile(**trade):
    settings = load_confirmed_baseline()[0].to_settings()
    settings["trade"].update(trade)
    return DsvProfile.from_settings(settings)


def event(**changes):
    result = {"account_key": "DEV:entry", "contract_id": "DCE.p2701", "trading_day": "2026-11-09",
        "session_id": "day-20261109", "source_epoch": 1, "sequence": 1,
        "source_time_utc": "2026-11-09T01:05:00.000001Z", "session_open_utc": "2026-11-09T01:00:00Z",
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
        boundary = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:05:00Z"))
        self.assertIn("OPENING_WAIT", boundary["reasons"])
        self.assertEqual(strategy.evaluate(state, event())["candidates"][0]["entry_kind"], "NORMAL")
        first_minute = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:00:20Z", last="1000", bid1="999", ask1="1001"))
        self.assertIn("OPENING_WAIT", first_minute["reasons"])
        self.assertEqual(first_minute["state"]["first_minute_blocks"], [])
        self.assertEqual(first_minute["source_notes"], [])

    def test_explicit_continuation_wait_waiver_does_not_admit_ordinary_entries(self):
        p = profile(continuation_ignore_no_open=True)
        strategy, state = self.setup_strategy(p)
        result = strategy.evaluate(state, event(source_time_utc="2026-11-09T01:00:20Z"))
        self.assertIn("OPENING_WAIT", result["reasons"])
        self.assertEqual(result["candidates"], [])

    def test_auction_uses_source_minute_with_switch(self):
        fact = event(source_time_utc="2026-11-09T01:00:59.999999Z", last="1055")
        after = event(source_time_utc="2026-11-09T01:01:00Z", last="1055")
        strategy, state = self.setup_strategy(profile(no_open_after_open=False, participate_auction=False))
        self.assertIn("AUCTION_DISABLED", strategy.evaluate(state, fact)["reasons"])
        self.assertNotIn("AUCTION_DISABLED", strategy.evaluate(state, after)["reasons"])
        strategy, state = self.setup_strategy(profile(no_open_after_open=False, participate_auction=True))
        self.assertNotIn("AUCTION_DISABLED", strategy.evaluate(state, fact)["reasons"])

    def test_continuation_direction_matches_source_gap_and_equality_cases(self):
        for opening, night_last, high, low, fallback, expected in (
            ("1048", "1050", "1080", "990", None, "LONG"),
            ("1051", "1050", "1080", "990", None, None),
            ("1030", "1028", "1085", "1000", None, "SHORT"),
            ("1028", "1030", "1085", "1000", None, None),
            ("1045", "1045", "1100", "990", "SHORT", "LONG"),
            ("1045", "1045", "1090", "1000", "SHORT", "SHORT"),
            ("1045", "1045", "1090", "1000", None, None),
        ):
            with self.subTest(opening=opening, night_last=night_last, fallback=fallback):
                values = tuple(map(Decimal, (opening, night_last, high, low, "55")))
                self.assertEqual(continuation_direction(*values, fallback=fallback), expected)

    def test_continuation_uses_overnight_range_and_own_wait_boundary(self):
        strategy, state = self.setup_strategy(profile(chase_window_minutes=5))
        night = event(session_id="night-20261109", source_time_utc="2026-11-08T14:59:59Z",
                      session_open_utc="2026-11-08T13:00:00Z", session_close_utc="2026-11-08T15:00:00Z",
                      candle_id="20261108-1459", last="1050", bid1="1049", ask1="1051",
                      day_high="1080", day_low="990")
        night_result = strategy.evaluate(state, night)
        night_state = night_result["state"]
        self.assertFalse(night_state["night_reference"]["boundary_proved"])
        day = event(sequence=2, source_time_utc="2026-11-09T01:05:00Z",
                    last="1048", bid1="1047", ask1="1049", day_high="1080", day_low="1000",
                    session_policy={"schema_version": "DSV_TIME/1", "calendar_hash": "a" * 64,
                                    "force_close_at_end": True, "entry_close_utc": "2026-11-09T07:00:00Z"})
        blocked = strategy.evaluate(night_state, day | {"source_time_utc": "2026-11-09T01:04:59.999999Z"})
        self.assertIn("OPENING_WAIT", blocked["reasons"])
        self.assertFalse(blocked["candidates"])
        result = strategy.evaluate(night_state, day)
        candidate = result["candidates"][0]
        self.assertEqual((candidate["entry_kind"], candidate["direction"], candidate["theoretical_price"],
                          candidate["limit_price"]), ("CONTINUATION", "LONG", "1048", "1049"))
        self.assertEqual(candidate["decision_key"], strategy.continuation_key(day))
        self.assertNotEqual(result["state"]["last_signal_bar"], day["candle_id"])
        used = strategy.evaluate(night_state, day, continuation_attempted=True)
        self.assertFalse(used["candidates"])
        self.assertIn("OPENING_WAIT", used["reasons"])

    def test_continuation_wait_discards_only_the_night_close_direction_fallback(self):
        strategy, state = self.setup_strategy(profile(chase_window_minutes=5))
        night = event(session_id="night-20261109", source_time_utc="2026-11-08T14:59:59Z",
                      session_open_utc="2026-11-08T13:00:00Z", session_close_utc="2026-11-08T15:00:00Z",
                      candle_id="20261108-1459", last="1045", bid1="1044", ask1="1046")
        state = strategy.evaluate(state, night)["state"]
        day = event(sequence=2, source_time_utc="2026-11-09T01:05:00Z",
                    last="1045", bid1="1044", ask1="1046")
        at_boundary = strategy.evaluate(state, day, fallback_direction="SHORT")
        self.assertEqual(at_boundary["candidates"][0]["direction"], "SHORT")
        waiting = strategy.evaluate(state, day | {"source_time_utc": "2026-11-09T01:04:59Z"},
                                    fallback_direction="SHORT")["state"]
        self.assertTrue(waiting["night_close_fallback_cleared"])
        self.assertEqual(waiting["night_reference"]["last"], "1045")
        later = strategy.evaluate(waiting, day | {"sequence": 3}, fallback_direction="SHORT")
        self.assertEqual(later["candidates"], [])

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

    def test_geometry_hint_precedes_activation_and_live_bar_is_consumed(self):
        strategy, state = self.setup_strategy()
        market = event(day_high="1200", last="1100", bid1="1099", ask1="1101")
        result = strategy.evaluate(state, market)
        self.assertEqual(result['entry_log'], ['GEOMETRY_BLOCK', 'TRIGGER_HINT', 'LONG:NO_CHASE_AFTER_ACT'])
        self.assertEqual(result['reasons'], ['GEOMETRY_BLOCK', 'LONG:NO_CHASE_AFTER_ACT'])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['state']['last_signal_bar'], market['candle_id'])
        repeated = strategy.evaluate(result['state'], market | {'sequence': 2})
        self.assertEqual(repeated['entry_log'], ['GEOMETRY_BLOCK', 'TRIGGER_HINT'])
        self.assertIn('CANDLE_OPPORTUNITY_CONSUMED', repeated['reasons'])
        short = strategy.evaluate(state, market | {'last': '1040', 'bid1': '1039', 'ask1': '1041'})
        self.assertEqual(short['entry_log'][-1], 'SHORT:NO_CHASE_AFTER_ACT')

    def test_activation_requires_current_touch_and_timing_logs_short_circuit(self):
        strategy, state = self.setup_strategy()
        market = event(day_high='1095', last='1050', bid1='1049', ask1='1051')
        no_touch = strategy.evaluate(state, market)
        self.assertEqual(no_touch['entry_log'], ['GEOMETRY_BLOCK', 'TRIGGER_HINT'])
        self.assertIsNone(no_touch['state']['last_signal_bar'])
        for stamp, reason in (('2026-11-09T01:05:00Z', 'OPENING_WAIT'),
                              ('2026-11-09T06:50:00Z', 'CLOSE_SESSION_NO_OPEN'),
                              (market['source_time_utc'], 'DSV_QUOTE_AGE_EXCEEDED')):
            with self.subTest(reason=reason):
                result = strategy.evaluate(state, market | {'source_time_utc': stamp}, ordinary_quote_stale=True)
                self.assertEqual(result['entry_log'], [reason])
                self.assertEqual(result['reasons'], [reason])

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
        strategy, state = self.setup_strategy(profile(protect_80_enabled=False, trailing_activate_ratio=0))
        disabled = strategy.evaluate(state, event(day_high="1085"))
        self.assertNotIn("LONG:NO_CHASE_AFTER_ACT", disabled["reasons"])
        self.assertEqual(len(disabled["candidates"]), 1)

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

    def test_missing_opponent_uses_last_without_waiving_chase_or_tick_validation(self):
        for direction, price, book in (
            ("LONG", "1055", {"ask1": None}),
            ("SHORT", "1035", {"bid1": None, "ask1": "1036"}),
        ):
            with self.subTest(direction=direction):
                strategy, state = self.setup_strategy()
                result = strategy.evaluate(state, event(last=price, **book))
                self.assertEqual([(row["direction"], row["limit_price"]) for row in result["candidates"]],
                                 [(direction, price)])
        for price, reason in (("1058", "CHASE_CAP_EXCEEDED"), ("1055.5", "QUOTE_NOT_TICK_ALIGNED")):
            strategy, state = self.setup_strategy()
            result = strategy.evaluate(state, event(last=price, ask1=None))
            self.assertIn("LONG:" + reason, result["reasons"])
            self.assertFalse(result["candidates"])

    def test_hysteresis_helper_uses_inclusive_reverse_boundary(self):
        self.assertEqual(hysteresis_ready("LONG", "1039", "1055", "16", False), (True, True))
        self.assertEqual(hysteresis_ready("LONG", "1040", "1055", "16", False), (False, False))
        self.assertEqual(hysteresis_ready("SHORT", "1051", "1035", "16", False), (True, True))
        self.assertEqual(hysteresis_ready("SHORT", "1035", "1035", "16", True), (True, True))

    def test_first_minute_uses_shipped_control_flow_even_with_legacy_latch(self):
        strategy, state = self.setup_strategy(profile(no_open_after_open=False, participate_auction=True))
        state["first_minute_blocks"] = ["LONG", "SHORT"]
        state["need_hyst"] = True
        state["hysteresis_armed"]["LONG"] = True
        for elapsed in (0, 60, 61):
            with self.subTest(elapsed=elapsed):
                minute, second = divmod(elapsed, 60)
                result = strategy.evaluate(state, event(source_time_utc=f"2026-11-09T01:{minute:02}:{second:02}Z"))
                self.assertEqual([row["direction"] for row in result["candidates"]], ["LONG"])
                self.assertNotIn("FIRST_MINUTE_JUMP_DAY_STOP", result["reasons"])
                self.assertEqual(bool(result["source_notes"]), elapsed <= 60)

    def test_zero_jump_ratio_and_shared_hysteresis_helper_boundaries(self):
        self.assertTrue(opening_jump_block("1055", "1055", "50", "0"))
        self.assertTrue(opening_jump_block("1095", "1055", "50", "0.8"))
        self.assertFalse(opening_jump_block("1094", "1055", "50", "0.8"))
        ready, armed = hysteresis_ready("LONG", "1039", "1055", "16", False)
        self.assertEqual((ready, armed), (True, True))
        self.assertEqual(hysteresis_ready("SHORT", "1035", "1035", "16", armed), (True, True))

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
