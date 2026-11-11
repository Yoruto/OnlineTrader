"""Offline DSV 1.1 behavior at the public compiler/reducer boundary."""

import copy
from decimal import Decimal
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.contracts.validation import validate_plan
from online_trader.errors import TraderError
from online_trader.protection import initial_state, reduce_event
from online_trader.strategy.plan import build_development_plan


def plan(quantity=7, direction="LONG", first_ratio=0.4, **trade):
    baseline, approval = load_confirmed_baseline()
    settings = baseline.to_settings()
    settings["trade"].update(half_close_enabled=True, half_stage2=True,
        half_breakeven=False, protect_80_enabled=False, trailing_activate_ratio=0)
    settings["trade"].update(trade)
    row = next(row for row in settings["symbols"] if row["code"] == "DCE.p2701")
    row["half_close_ratio"] = first_ratio
    profile = DsvProfile.from_settings(settings)
    row = next(row for row in profile.to_record()["settings"]["symbols"] if row["code"] == "DCE.p2701")
    candidate = {"account_key": "DEV:protection", "contract_id": row["code"],
        "trading_day": "2026-11-09", "direction": direction, "profile_hash": profile.content_hash,
        "theoretical_price": "1000", "limit_price": "1000", "open_offset": row["open_offset"],
        "stop_distance": row["stop_offset"], "take_profit_distance": row["profit_offset"]}
    return build_development_plan(profile, candidate, account_key=candidate["account_key"],
        trading_day=candidate["trading_day"], config_revision="1", config_hash=profile.content_hash,
        rule_set_id=approval["rule_set_id"], quantity=quantity,
        plan_id="plan-dsv-protection", position_cycle_id="cycle-dsv-protection")


def opened(value, *, terminal=True):
    state = reduce_event(value, initial_state(value),
        {"type": "FILL", "quantity": value["initial_open_quantity"], "price": "1000"}).state
    if terminal:
        state = reduce_event(value, state, {"type": "ENTRY_TERMINAL_RECONCILED",
            "owned_quantity": state.owned_qty, "reconciliation_id": "entry-terminal-1"}).state
    return state


def quote(value, state, move, **metadata):
    sign = 1 if value["direction"] == "LONG" else -1
    price = Decimal(1000) + sign * Decimal(str(move))
    return reduce_event(value, state, {"type": "QUOTE", "price": str(price), **metadata}).state


def gap(value, state, milliseconds=180001, elapsed=200000, **changes):
    event = {"type": "DATA_GAP", "gap_ms": milliseconds, "session_elapsed_ms": elapsed,
        "active_trading": True, "policy_authorized": True,
        "policy_id": value["data_gap_exit"]["data_quality_policy_id"], "legal_price_available": True}
    event.update(changes)
    return reduce_event(value, state, event).state


def legacy(value):
    result = copy.deepcopy(value)
    result["schema_version"] = "1.0"
    if result["partial_reduction"]["enabled"]:
        result["partial_reduction"]["quantity_rule"] = "CEIL_Q0_DIV_2_AND_4"
    result["trailing_stop"] = {"enabled": False}
    if result["profit_protection"]["enabled"]:
        result["profit_protection"]["block_new_for_trading_day"] = True
    if result["data_gap_exit"]["enabled"]:
        result["data_gap_exit"].pop("block_new_for_trading_day")
    validate_plan(result)
    return result


class DsvProtectionTests(unittest.TestCase):
    def test_first_stage_rounds_current_quantity_with_ties_to_even_and_both_directions(self):
        for direction in ("LONG", "SHORT"):
            for quantity, target in ((1, None), (2, 1), (3, 1), (5, 3), (7, 3), (9, 5)):
                with self.subTest(direction=direction, quantity=quantity):
                    value = plan(quantity, direction)
                    state = quote(value, opened(value), 20)
                    self.assertEqual(state.target_remaining_qty, target)
                    self.assertEqual(state.owned_qty, quantity)
                    self.assertEqual(state.exit_filled_qty, 0)

    def test_second_stage_rounds_virtual_remaining_without_manufacturing_fills(self):
        for direction in ("LONG", "SHORT"):
            for quantity, target in ((2, 1), (3, 1), (5, 1), (7, 1), (9, 3)):
                with self.subTest(direction=direction, quantity=quantity):
                    value = plan(quantity, direction)
                    first = quote(value, opened(value), 20)
                    second = quote(value, first, 30)
                    self.assertEqual(second.target_remaining_qty, target)
                    self.assertEqual(second.owned_qty, quantity)
                    self.assertEqual(second.phase1_confirmed_reduction_qty, 0)

    def test_one_quote_above_both_thresholds_only_commits_first_stage(self):
        value = plan()
        state = quote(value, opened(value), 35)
        self.assertEqual(state.phase1_target, 3)
        self.assertIsNone(state.phase2_target)
        self.assertEqual(state.phase2_status, "NOT_TRIGGERED")
        self.assertEqual(quote(value, state, 35).phase2_target, 1)

    def test_replayed_quote_identity_cannot_consume_another_stage(self):
        value = plan()
        state = quote(value, opened(value), 35, event_id="market-stage-1", event_seq=3)
        replay = quote(value, state, 35, event_id="market-stage-1", event_seq=3)
        self.assertEqual(replay, state)
        self.assertIsNone(replay.phase2_target)

    def test_second_stage_uses_current_profit_instead_of_the_old_peak(self):
        value = plan()
        first = quote(value, opened(value), 35)
        pulled_back = quote(value, first, 25)
        self.assertEqual(pulled_back.max_favorable_move, 35)
        self.assertIsNone(pulled_back.phase2_target)
        self.assertEqual(pulled_back.target_remaining_qty, 3)

    def test_second_stage_off_stays_off_after_a_new_high(self):
        value = plan(half_stage2=False)
        state = quote(value, opened(value), 20)
        state = quote(value, state, 40)
        self.assertIsNone(state.phase2_target)
        self.assertEqual(state.target_remaining_qty, 3)

    def test_one_lot_remainder_cannot_be_reduced_again(self):
        value = plan(3)
        state = quote(value, opened(value), 20)
        state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 2}).state
        state = quote(value, state, 35)
        self.assertEqual(state.owned_qty, 1)
        self.assertEqual(state.target_remaining_qty, 1)
        self.assertIsNone(state.phase2_target)

    def test_adjusted_second_threshold_can_exceed_one_and_fixed_profit_exits_first(self):
        value = plan(first_ratio=0.8, half_stage2_trigger_ratio=0.6)
        self.assertEqual(value["partial_reduction"]["second_stage"]["trigger_ratio"], "1.2")
        state = quote(value, opened(value), 40)
        state = quote(value, state, 50)
        self.assertEqual(state.target_remaining_qty, 0)
        self.assertIn("TAKE_PROFIT", state.exit_reasons)
        self.assertIsNone(state.phase2_target)

    def test_disabled_symbol_half_ratio_does_not_inherit_global_ratio(self):
        for ratio in (0, 1):
            with self.subTest(ratio=ratio):
                value = plan(first_ratio=ratio, half_close_ratio=0.5)
                state = quote(value, opened(value), 35)
                self.assertEqual(value["partial_reduction"], {"enabled": False})
                self.assertIsNone(state.target_remaining_qty)

    def test_entry_terminal_barrier_is_required_before_committing_reduction(self):
        value = plan()
        state = quote(value, opened(value, terminal=False), 20)
        self.assertIsNone(state.target_remaining_qty)
        self.assertIsNone(state.q0)
        self.assertEqual(state.phase1_status, "WAITING_ENTRY_TERMINAL")
        state = reduce_event(value, state, {"type": "ENTRY_TERMINAL_RECONCILED",
            "owned_quantity": 7, "reconciliation_id": "proven-entry-terminal"}).state
        self.assertEqual((state.q0, state.target_remaining_qty), (7, 3))

    def test_send_and_unknown_cancellation_never_activate_breakeven(self):
        value = plan(half_breakeven=True)
        state = quote(value, opened(value), 20)
        for event in ({"type": "EXIT_ORDER_SENT"}, {"type": "CANCEL_UNKNOWN"}):
            state = reduce_event(value, state, event).state
            self.assertEqual(state.break_even_status, "WAITING_FILL")
            self.assertEqual(state.owned_qty, 7)
            self.assertEqual(state.phase1_confirmed_reduction_qty, 0)

    def test_a_real_first_stage_partial_fill_activates_breakeven_before_all_fills(self):
        value = plan(half_breakeven=True)
        state = quote(value, opened(value), 20)
        state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1}).state
        self.assertEqual(state.break_even_status, "ACTIVE")
        self.assertEqual(state.phase1_confirmed_reduction_qty, 1)
        self.assertEqual(state.phase1_status, "EXECUTING")
        self.assertEqual(state.owned_qty, 6)

    def test_external_reduction_does_not_prove_the_planned_first_stage(self):
        value = plan(half_breakeven=True)
        state = quote(value, opened(value), 20)
        state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1, "external": True}).state
        self.assertEqual(state.break_even_status, "WAITING_FILL")
        self.assertEqual(state.phase1_confirmed_reduction_qty, 0)
        self.assertEqual(state.owned_qty, 6)

    def test_breakeven_anchor_is_exact_and_mirrored(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                value = plan(direction=direction, half_breakeven=True)
                state = quote(value, opened(value), 20)
                state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1}).state
                before = quote(value, state, "0.000001")
                self.assertEqual(before.target_remaining_qty, 3)
                exact = quote(value, before, 0)
                self.assertEqual(exact.target_remaining_qty, 0)
                self.assertIn("BREAK_EVEN", exact.exit_reasons)

    def test_fill_with_fresh_quote_can_apply_new_breakeven_without_consuming_stage_two(self):
        value = plan(half_breakeven=True)
        state = quote(value, opened(value), 20)
        state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1,
            "current_price": "1000", "quote_valid": True}).state
        self.assertEqual(state.target_remaining_qty, 0)
        self.assertIn("BREAK_EVEN", state.exit_reasons)
        self.assertIsNone(state.phase2_target)

    def test_protection_suppresses_breakeven_even_when_late_reduction_is_confirmed(self):
        value = plan(half_breakeven=True, protect_80_enabled=True)
        state = quote(value, opened(value), 20)
        state = quote(value, state, 40)
        self.assertEqual(state.mode, "PROTECTED")
        state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1}).state
        self.assertEqual(state.break_even_status, "SUPPRESSED_PROTECTED")
        self.assertEqual(state.phase1_confirmed_reduction_qty, 1)

    def test_protection_activation_wins_over_same_observation_half_trigger(self):
        value = plan(first_ratio=0.8, protect_80_enabled=True)
        state = quote(value, opened(value), 40)
        self.assertEqual(state.mode, "PROTECTED")
        self.assertIsNone(state.phase1_target)
        self.assertEqual(state.phase1_status, "SUPPRESSED_PROTECTED")
        self.assertIsNone(state.target_remaining_qty)

    def test_earlier_trailing_activation_is_suppressed_after_protection_latches(self):
        value = plan(half_close_enabled=False, protect_80_enabled=True, protect_stop_day=False,
            trailing_activate_ratio=0.4, trailing_pullback_ratio=0.1, stop_day_on_trailing=False)
        state = quote(value, opened(value), 20)
        self.assertIsNotNone(state.trailing_activated_event_seq)
        self.assertEqual(state.trailing_floor_move, 15)
        state = quote(value, state, 40)
        state = quote(value, state, 30)
        self.assertEqual(state.mode, "PROTECTED")
        self.assertIsNone(state.target_remaining_qty)
        self.assertNotIn("TRAILING_STOP", state.exit_reasons)
        self.assertFalse(state.block_new_for_trading_day)

    def test_trailing_tp_distance_term_and_equal_drawdown_boundary_are_mirrored(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                value = plan(direction=direction, half_close_enabled=False, trailing_activate_ratio=0.4,
                    trailing_pullback_ratio=0.1, stop_day_on_trailing=False)
                state = quote(value, opened(value), 35)
                self.assertEqual(state.trailing_floor_move, 30)
                state = quote(value, state, "30.000001")
                self.assertIsNone(state.target_remaining_qty)
                state = quote(value, state, 30)
                self.assertEqual(state.target_remaining_qty, 0)
                self.assertIn("TRAILING_STOP", state.exit_reasons)
                self.assertFalse(state.block_new_for_trading_day)

    def test_peak_term_is_preserved_during_an_unfilled_fixed_profit_exit(self):
        value = plan(half_close_enabled=False, trailing_activate_ratio=0.4, trailing_pullback_ratio=0.1)
        state = quote(value, opened(value), 100)
        self.assertEqual(state.target_remaining_qty, 0)
        self.assertEqual(state.owned_qty, 7)
        state = quote(value, state, 49)
        self.assertEqual(state.max_favorable_move, 100)
        self.assertEqual(state.trailing_floor_move, 90)
        self.assertEqual(state.target_remaining_qty, 0)

    def test_zero_trailing_pullback_exits_at_activation(self):
        value = plan(half_close_enabled=False, trailing_activate_ratio=0.4, trailing_pullback_ratio=0)
        state = quote(value, opened(value), 20)
        self.assertEqual(state.target_remaining_qty, 0)
        self.assertIn("TRAILING_STOP", state.exit_reasons)
        self.assertTrue(state.block_new_for_trading_day)

    def test_zero_trailing_activation_disables_instead_of_immediately_activating(self):
        value = plan(half_close_enabled=False, trailing_activate_ratio=0, trailing_pullback_ratio=0)
        state = quote(value, opened(value), 35)
        state = quote(value, state, 0)
        self.assertIsNone(state.trailing_activated_event_seq)
        self.assertIsNone(state.target_remaining_qty)

    def test_false_protection_day_stop_keeps_protection_without_a_day_lock(self):
        value = plan(protect_80_enabled=True, protect_stop_day=False, half_close_enabled=False)
        state = quote(value, opened(value), 40)
        self.assertEqual(state.mode, "PROTECTED")
        self.assertFalse(state.block_new_for_trading_day)
        state = quote(value, state, 25)
        self.assertEqual(state.target_remaining_qty, 0)
        self.assertIn("PROTECTED_FLOOR", state.exit_reasons)
        self.assertFalse(state.block_new_for_trading_day)

    def test_keep_above_activation_is_allowed_and_exits_at_the_activation_observation(self):
        value = plan(protect_80_enabled=True, protect_80_ratio=0.2,
            protect_keep_ratio=0.8, protect_stop_day=False, half_close_enabled=False)
        state = quote(value, opened(value), 10)
        self.assertEqual(state.mode, "PROTECTED")
        self.assertEqual(state.target_remaining_qty, 0)
        self.assertIn("PROTECTED_FLOOR", state.exit_reasons)

    def test_gap_session_grace_requires_strictly_more_than_sixty_seconds(self):
        value = plan(stale_force_close_seconds=10)
        state = opened(value)
        exact = gap(value, state, 10001, 60000)
        self.assertIsNone(exact.target_remaining_qty)
        after = gap(value, exact, 10001, 60001)
        self.assertEqual(after.target_remaining_qty, 0)

    def test_gap_quiet_threshold_is_strict_after_session_grace(self):
        value = plan(stale_force_close_seconds=10)
        state = opened(value)
        exact = gap(value, state, 10000, 100000)
        self.assertIsNone(exact.target_remaining_qty)
        self.assertEqual(gap(value, exact, 10001, 100000).target_remaining_qty, 0)

    def test_gap_larger_than_proven_session_duration_is_refused(self):
        value = plan(stale_force_close_seconds=10)
        state = opened(value)
        with self.assertRaises(TraderError) as raised:
            gap(value, state, 100001, 100000)
        self.assertEqual(raised.exception.code, "E_FACT_BARRIER")
        self.assertEqual(state.owned_qty, 7)
        self.assertIsNone(state.target_remaining_qty)

    def test_gap_day_stop_waits_for_real_flat_and_not_a_send_return(self):
        value = plan(stale_close_day_stop=True)
        state = gap(value, opened(value))
        self.assertTrue(state.gap_day_stop_pending)
        self.assertFalse(state.block_new_for_trading_day)
        state = reduce_event(value, state, {"type": "EXIT_ORDER_SENT"}).state
        self.assertFalse(state.block_new_for_trading_day)
        state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 6}).state
        self.assertEqual(state.owned_qty, 1)
        self.assertFalse(state.block_new_for_trading_day)
        state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1}).state
        self.assertEqual(state.owned_qty, 0)
        self.assertTrue(state.block_new_for_trading_day)

    def test_gap_without_day_stop_does_not_add_a_lock_when_flat(self):
        value = plan(stale_close_day_stop=False)
        state = gap(value, opened(value))
        state = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 7}).state
        self.assertFalse(state.block_new_for_trading_day)

    def test_gap_can_exit_protected_mode_without_extra_override(self):
        value = plan(protect_80_enabled=True, protect_stop_day=False)
        state = quote(value, opened(value), 40)
        state = gap(value, state)
        self.assertEqual(state.target_remaining_qty, 0)
        self.assertIn("DATA_GAP_EXIT", state.exit_reasons)

    def test_missing_legal_gap_price_alerts_without_a_fake_exit_or_day_stop(self):
        value = plan(stale_close_day_stop=True)
        state = gap(value, opened(value), legal_price_available=False)
        self.assertIsNone(state.target_remaining_qty)
        self.assertTrue(state.reconciliation_required)
        self.assertFalse(state.block_new_for_trading_day)
        self.assertFalse(state.gap_day_stop_pending)

    def test_legacy_quantity_and_multi_stage_observation_remain_different(self):
        modern = plan()
        old = legacy(modern)
        modern_state = quote(modern, opened(modern), 35)
        old_state = quote(old, opened(old), 35)
        self.assertEqual(modern_state.phase1_target, 3)
        self.assertIsNone(modern_state.phase2_target)
        self.assertEqual((old_state.phase1_target, old_state.phase2_target), (4, 2))
        self.assertEqual(old_state.target_remaining_qty, 2)

    def test_legacy_gap_does_not_gain_session_grace_or_protected_override(self):
        modern = plan(protect_80_enabled=True, half_close_enabled=False, stale_force_close_seconds=10)
        old = legacy(modern)
        state = opened(old)
        event = {"type": "DATA_GAP", "gap_ms": 10001, "active_trading": True,
            "policy_authorized": True, "policy_id": old["data_gap_exit"]["data_quality_policy_id"],
            "legal_price_available": True}
        self.assertEqual(reduce_event(old, state, event).state.target_remaining_qty, 0)
        protected = quote(old, opened(old), 40)
        self.assertIsNone(reduce_event(old, protected, event).state.target_remaining_qty)
        self.assertEqual(reduce_event(old, protected, {**event, "allow_protected_override": True}).state.target_remaining_qty, 0)

    def test_legacy_protected_floor_limits_do_not_silently_expand(self):
        modern = plan(protect_80_enabled=True, protect_80_ratio=0.2, protect_keep_ratio=0.8)
        initial_state(modern)
        old = legacy(modern)
        with self.assertRaises(TraderError) as raised:
            initial_state(old)
        self.assertEqual(raised.exception.code, "PLAN_THRESHOLD_ORDER")

    def test_stages_and_confirmations_round_trip_without_input_mutation(self):
        value = plan(half_breakeven=True)
        state = quote(value, opened(value), 20)
        before = state.to_dict()
        plan_bytes = canonical_bytes(value)
        decision = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1, "event_id": "confirmed-exit-1"})
        restored = type(state).from_dict(decision.state.to_dict())
        self.assertEqual(restored, decision.state)
        self.assertEqual(state.to_dict(), before)
        self.assertEqual(canonical_bytes(value), plan_bytes)
        replay = reduce_event(value, restored, {"type": "EXIT_FILL", "quantity": 1, "event_id": "confirmed-exit-1"})
        self.assertEqual(replay.state, restored)

    def test_nonpositive_quotes_cannot_create_protection_breakeven_or_exit(self):
        for direction in ("LONG", "SHORT"):
            value = plan(direction=direction, half_breakeven=True, protect_80_enabled=True)
            waiting = opened(value)
            reducing = quote(value, waiting, 20)
            active = reduce_event(value, reducing, {"type": "EXIT_FILL", "quantity": 1}).state
            for previous in (waiting, active):
                for price in ("0", "-1"):
                    with self.subTest(direction=direction, price=price, breakeven=previous.break_even_status):
                        decision = reduce_event(value, previous, {"type": "QUOTE", "price": price})
                        state = decision.state
                        self.assertEqual(state.quality, "DATA_GAP")
                        self.assertEqual(decision.reasons, ("INVALID_MARKET_DATA",))
                        self.assertEqual(state.last_price, previous.last_price)
                        self.assertEqual(state.max_favorable_move, previous.max_favorable_move)
                        self.assertEqual(state.mode, previous.mode)
                        self.assertEqual(state.break_even_status, previous.break_even_status)
                        self.assertEqual(state.target_remaining_qty, previous.target_remaining_qty)
                        self.assertEqual(state.exit_reasons, previous.exit_reasons)
                        self.assertEqual(state.owned_qty, previous.owned_qty)


if __name__ == "__main__":
    unittest.main()
