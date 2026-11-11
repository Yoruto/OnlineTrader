import copy
from decimal import Decimal
import json
from pathlib import Path
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.contracts.validation import validate_plan
from online_trader.errors import TraderError
from online_trader.protection import initial_state, reduce_event
from online_trader.strategy.plan import build_development_plan, capability_notes


def candidate(profile, contract="DCE.p2701", **changes):
    row = next(row for row in profile.to_record()["settings"]["symbols"] if row["code"] == contract)
    result = {"account_key": "DEV:plan", "contract_id": contract, "trading_day": "2026-11-09",
              "profile_hash": profile.content_hash, "direction": "LONG", "theoretical_price": "1055",
              "limit_price": "1056", "open_offset": row["open_offset"], "stop_distance": row["stop_offset"],
              "take_profit_distance": row["profit_offset"]}
    result.update(changes)
    return result


def make_plan(profile=None, entry=None, **changes):
    baseline, approval = load_confirmed_baseline()
    profile = profile or baseline
    arguments = {"account_key": "DEV:plan", "trading_day": "2026-11-09", "config_revision": "1",
                 "config_hash": "a" * 64, "rule_set_id": approval["rule_set_id"], "quantity": 2,
                 "plan_id": "plan-dsv-1", "position_cycle_id": "cycle-dsv-1"}
    arguments.update(changes)
    return build_development_plan(profile, entry or candidate(profile), **arguments)


def changed_profile(**changes):
    settings = load_confirmed_baseline()[0].to_settings()
    settings["trade"].update(changes)
    return DsvProfile.from_settings(settings)


class DsvPlanTests(unittest.TestCase):
    def test_baseline_compiles_exact_source_parameters_and_versioned_schema(self):
        profile, approval = load_confirmed_baseline()
        plan = make_plan(profile)
        validate_plan(plan)
        self.assertEqual(plan["schema_version"], "1.1")
        self.assertEqual(plan["execution_scope"], "DEVELOPMENT")
        self.assertEqual(plan["approval_record_id"], approval["record_id"])
        self.assertEqual(plan["price_field"], "LAST")
        self.assertEqual(plan["fixed_stop_loss"], {"enabled": True, "distance_points": "16"})
        self.assertEqual(plan["fixed_take_profit"]["distance_points"], "50")
        self.assertEqual(plan["profit_protection"], {"enabled": True, "activation_ratio": "0.8",
            "floor_ratio": "0.5", "block_new_for_trading_day": True})
        self.assertEqual(plan["slippage_exit"], {"enabled": True, "adverse_threshold_points": "6.4",
            "block_new_for_trading_day": False})
        self.assertEqual(plan["data_gap_exit"]["active_trading_gap_ms"], 180001)
        self.assertEqual(plan["data_gap_exit"]["missing_legal_price_action"], "ALERT_AND_TAKEOVER")
        self.assertFalse(plan["data_gap_exit"]["block_new_for_trading_day"])
        self.assertEqual(plan["close_session"]["advance_seconds"], 20)

    def test_mirrored_direction_keeps_same_protection_and_exact_entry_anchor(self):
        profile = load_confirmed_baseline()[0]
        long = make_plan(profile)
        short = make_plan(profile, candidate(profile, direction="SHORT", theoretical_price="1035", limit_price="1034"))
        self.assertEqual(short["direction"], "SHORT")
        self.assertEqual(short["theoretical_entry_price"], "1035")
        for key in ("fixed_stop_loss", "fixed_take_profit", "profit_protection", "slippage_exit"):
            self.assertEqual(long[key], short[key])

    def test_every_configured_nonzero_contract_uses_its_own_distances(self):
        profile = load_confirmed_baseline()[0]
        for row in profile.to_record()["settings"]["symbols"]:
            with self.subTest(contract=row["code"]):
                if row["stop_offset"] == "0":
                    with self.assertRaises(TraderError):
                        make_plan(profile, candidate(profile, row["code"]))
                else:
                    plan = make_plan(profile, candidate(profile, row["code"]))
                    self.assertEqual(plan["fixed_stop_loss"]["distance_points"], row["stop_offset"])
                    self.assertEqual(plan["fixed_take_profit"]["distance_points"], row["profit_offset"])

    def test_input_scope_and_distances_cannot_be_substituted(self):
        profile = load_confirmed_baseline()[0]
        substitutions = {"profile_hash": "b" * 64, "account_key": "DEV:other", "trading_day": "2026-11-10",
                         "contract_id": "DCE.unknown", "direction": "BUY", "stop_distance": "15",
                         "open_offset": "54", "take_profit_distance": "49", "execution_scope": "PRODUCTION",
                         "theoretical_price": 1055, "limit_price": "1056.0"}
        for key, value in substitutions.items():
            with self.subTest(field=key), self.assertRaises(TraderError):
                make_plan(profile, candidate(profile, **{key: value}))

    def test_illegal_quantity_and_identity_are_rejected_before_use(self):
        for value in (0, -1, True, 2.0, "2", 2_147_483_648):
            with self.subTest(quantity=value), self.assertRaises(TraderError):
                make_plan(quantity=value)
        for change in ({"account_key": "../escape"}, {"config_revision": 1}, {"config_revision": "0"},
                       {"config_hash": "A" * 64}, {"plan_id": ""}, {"position_cycle_id": None},
                       {"trading_day": "2026-02-30"}, {"trading_day": "20261109"}):
            with self.subTest(change=change), self.assertRaises(TraderError):
                make_plan(**change)

    def test_unsupported_protection_switches_fail_explicitly(self):
        for change in ({"passive_entry_first": True}, {"close_price_mode": "市价"}):
            with self.subTest(change=change), self.assertRaises(TraderError) as raised:
                make_plan(changed_profile(**change))
            self.assertEqual(raised.exception.code, "E_UNSUPPORTED")

    def test_disable_slippage_and_unrepresentable_gap_are_not_approximated(self):
        self.assertEqual(make_plan(changed_profile(entry_slip_abort=False))["slippage_exit"], {"enabled": False})
        self.assertEqual(make_plan(changed_profile(entry_slip_abort_ratio=0))["slippage_exit"]["adverse_threshold_points"], "6.4")
        self.assertEqual(make_plan(changed_profile(stale_force_close_seconds=0))["data_gap_exit"], {"enabled": False})
        self.assertEqual(make_plan(changed_profile(stale_force_close_seconds=0.0001))["data_gap_exit"]["active_trading_gap_ms"], 1)
        for seconds in (2147483.647, 2147484):
            with self.subTest(seconds=seconds), self.assertRaises(TraderError) as raised:
                make_plan(changed_profile(stale_force_close_seconds=seconds))
            self.assertEqual(raised.exception.code, "E_UNSUPPORTED")

    def test_actual_reducer_preserves_source_strict_gap_boundary(self):
        plan = make_plan(quantity=1)
        state = reduce_event(plan, initial_state(plan), {"type": "FILL", "quantity": 1, "price": "1055"}).state
        event = {"type": "DATA_GAP", "active_trading": True, "policy_authorized": True,
                 "policy_id": plan["data_gap_exit"]["data_quality_policy_id"], "legal_price_available": True,
                 "session_elapsed_ms": 180001}
        exact = reduce_event(plan, state, {**event, "gap_ms": 180000}).state
        self.assertIsNone(exact.target_remaining_qty)
        after = reduce_event(plan, exact, {**event, "gap_ms": 180001}).state
        self.assertEqual(after.target_remaining_qty, 0)
        self.assertIn("DATA_GAP_EXIT", after.exit_reasons)

    def test_gap_exit_requires_the_explicit_first_minute_grace_boundary(self):
        plan = make_plan(changed_profile(stale_force_close_seconds=0.001), quantity=1)
        state = reduce_event(plan, initial_state(plan), {"type": "FILL", "quantity": 1, "price": "1055"}).state
        event = {"type": "DATA_GAP", "active_trading": True, "policy_authorized": True,
                 "policy_id": plan["data_gap_exit"]["data_quality_policy_id"], "legal_price_available": True,
                 "gap_ms": 2}
        grace = reduce_event(plan, state, {**event, "session_elapsed_ms": 60000}).state
        self.assertIsNone(grace.target_remaining_qty)
        eligible = reduce_event(plan, grace, {**event, "session_elapsed_ms": 60001}).state
        self.assertEqual(eligible.target_remaining_qty, 0)
        self.assertIn("DATA_GAP_EXIT", eligible.exit_reasons)
        with self.assertRaises(TraderError) as raised:
            reduce_event(plan, state, {**event, "gap_ms": 60002, "session_elapsed_ms": 60001})
        self.assertEqual(raised.exception.code, "E_FACT_BARRIER")

    def test_first_request_policy_and_suppressed_trailing_are_explicit(self):
        profile = load_confirmed_baseline()[0]
        plan = make_plan(profile)
        policy = plan["execution_policy"]
        self.assertEqual((policy["first_price_mode"], policy["max_child_order_attempts"], policy["price_steps"]),
                         ("OPPONENT_LIMIT", 1, []))
        self.assertEqual(plan["trailing_stop"], {"enabled": True, "activation_ratio": "0.8",
            "drawdown_mode": "DSV_MAX_TP_AND_PEAK_RATIO", "drawdown_value": "0.1",
            "block_new_for_trading_day": True})
        self.assertEqual(plan["partial_reduction"], {"enabled": False})
        self.assertIn("NORMAL_TRAILING_SUPPRESSED_BY_PROFIT_PROTECTION", capability_notes(profile))
        self.assertIn("DSV_TRAILING_DAYSTOP_SHADOWED_BRANCH_REPAIRED_WITH_SAVED_SWITCH", capability_notes(profile))
        self.assertNotIn("G0_CANNOT_EXPRESS_DSV_MAX_DRAWDOWN_FORMULA", capability_notes(profile))

    def test_compilation_is_deterministic_does_not_mutate_and_binds_policy_references(self):
        profile = load_confirmed_baseline()[0]
        entry = candidate(profile)
        before = copy.deepcopy(entry)
        first, second = make_plan(profile, entry), make_plan(profile, entry)
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        self.assertEqual(entry, before)
        revised = make_plan(profile, entry, config_revision="2")
        self.assertNotEqual(first["close_session"]["schedule_policy_id"], revised["close_session"]["schedule_policy_id"])
        self.assertNotEqual(first["market_quality_policy_id"], revised["market_quality_policy_id"])

    def test_half_trigger_is_the_source_symbol_ratio_and_uses_current_volume_rounding(self):
        profile = changed_profile(half_close_enabled=True, half_close_ratio=0.9)
        partial = make_plan(profile)["partial_reduction"]
        self.assertEqual(partial, {"enabled": True, "first_trigger_ratio": "0.4",
            "quantity_rule": "DSV_ROUND_HALF_CURRENT", "q0_freeze_policy": "ENTRY_TERMINAL_AND_RECONCILED",
            "second_stage": {"enabled": False}, "break_even": {"enabled": False}})
        for invalid_ratio in (0, 1):
            settings = profile.to_settings(); settings["symbols"][0]["half_close_ratio"] = invalid_ratio
            self.assertEqual(make_plan(DsvProfile.from_settings(settings))["partial_reduction"], {"enabled": False})

    def test_disabled_half_keeps_stage_and_breakeven_subswitches_dormant(self):
        profile = changed_profile(half_close_enabled=False, half_stage2=True, half_breakeven=True)
        self.assertEqual(make_plan(profile)["partial_reduction"], {"enabled": False})
        self.assertIn("HALF_SUBSWITCHES_DORMANT_WITH_HALF_DISABLED", capability_notes(profile))

    def test_second_stage_uses_source_clamp_and_one_point_five_adjustment(self):
        profile = changed_profile(half_close_enabled=True, half_stage2=True, half_stage2_trigger_ratio=0)
        settings = profile.to_settings(); settings["symbols"][0]["half_close_ratio"] = 0.02
        partial = make_plan(DsvProfile.from_settings(settings))["partial_reduction"]
        self.assertEqual(partial["second_stage"], {"enabled": True, "trigger_ratio": "0.05"})
        settings["symbols"][0]["half_close_ratio"] = 0.8
        partial = make_plan(DsvProfile.from_settings(settings))["partial_reduction"]
        self.assertEqual(partial["second_stage"]["trigger_ratio"], "1.2")
        profile = changed_profile(half_close_enabled=True, half_stage2=True, half_stage2_trigger_ratio=1)
        self.assertEqual(make_plan(profile)["partial_reduction"]["second_stage"]["trigger_ratio"], "0.95")

    def test_breakeven_records_the_explicit_saved_switch_repair_and_confirmed_reduction(self):
        profile = changed_profile(half_close_enabled=True, half_breakeven=True)
        self.assertEqual(make_plan(profile)["partial_reduction"]["break_even"], {"enabled": True,
            "anchor": "THEORETICAL_ENTRY", "activate_after": "FIRST_STAGE_CONFIRMED_REDUCTION"})
        self.assertIn("DSV_BREAKEVEN_UNDEFINED_GLOBAL_REPAIRED_WITH_SAVED_SWITCH_AND_CONFIRMED_REDUCTION",
                      capability_notes(profile))

    def test_protection_zero_defaults_and_day_stop_switch_are_independent(self):
        profile = changed_profile(protect_80_ratio=0, protect_keep_ratio=0, protect_stop_day=False)
        self.assertEqual(make_plan(profile)["profit_protection"], {"enabled": True,
            "activation_ratio": "0.8", "floor_ratio": "0.5", "block_new_for_trading_day": False})
        plan = make_plan(changed_profile(protect_80_ratio=0.2, protect_keep_ratio=0.8))
        self.assertEqual(plan["profit_protection"]["floor_ratio"], "0.8")

    def test_normal_trailing_and_gap_stop_switches_compile_without_approximation(self):
        profile = changed_profile(protect_80_enabled=False, trailing_activate_ratio=0.7,
                                  trailing_pullback_ratio=0, stop_day_on_trailing=False, stale_close_day_stop=True)
        plan = make_plan(profile)
        self.assertEqual(plan["profit_protection"], {"enabled": False})
        self.assertEqual(plan["trailing_stop"], {"enabled": True, "activation_ratio": "0.7",
            "drawdown_mode": "DSV_MAX_TP_AND_PEAK_RATIO", "drawdown_value": "0",
            "block_new_for_trading_day": False})
        self.assertTrue(plan["data_gap_exit"]["block_new_for_trading_day"])
        self.assertEqual(make_plan(changed_profile(trailing_activate_ratio=0))["trailing_stop"], {"enabled": False})

    def test_parameter_variants_do_not_inherit_the_saved_configuration_approval(self):
        baseline = make_plan()
        variant = make_plan(changed_profile(half_close_enabled=True))
        self.assertIsNotNone(baseline["approval_record_id"])
        self.assertIsNone(variant["approval_record_id"])
        self.assertIn("DEVELOPMENT_VARIANT_IS_NOT_THE_APPROVED_SAVED_CONFIGURATION",
                      capability_notes(changed_profile(half_close_enabled=True)))

    def test_dsv_drawdown_contract_rejects_negative_or_greater_than_one_ratios(self):
        for value in ("-0.1", "1.000000000001"):
            with self.subTest(drawdown=value):
                plan = make_plan(changed_profile(protect_80_enabled=False))
                plan["trailing_stop"]["drawdown_value"] = value
                with self.assertRaises(TraderError):
                    validate_plan(plan)
                with self.assertRaises(TraderError):
                    initial_state(plan)

    def test_exact_stage_two_derived_ratio_preserves_its_thirteenth_decimal_place(self):
        settings = load_confirmed_baseline()[0].to_settings()
        settings["trade"].update(half_close_enabled=True, half_stage2=True,
                                 protect_80_enabled=False, trailing_activate_ratio=0)
        next(row for row in settings["symbols"] if row["code"] == "DCE.p2701")["half_close_ratio"] = Decimal("0.900000000001")
        plan = make_plan(DsvProfile.from_settings(settings), quantity=5)
        self.assertEqual(plan["partial_reduction"]["second_stage"]["trigger_ratio"], "1.3500000000015")
        validate_plan(plan)
        initial_state(plan)
        state = reduce_event(plan, initial_state(plan), {"type": "FILL", "quantity": 5, "price": "1055"}).state
        state = reduce_event(plan, state, {"type": "ENTRY_TERMINAL_RECONCILED", "owned_quantity": 5,
                                         "reconciliation_id": "derived-ratio-entry-complete"}).state
        state = reduce_event(plan, state, {"type": "QUOTE", "price": "1101", "valid": True}).state
        self.assertEqual(state.target_remaining_qty, 3)
        state = reduce_event(plan, state, {"type": "QUOTE", "price": "1102", "valid": True}).state
        self.assertEqual(state.phase2_status, "NOT_TRIGGERED")
        self.assertEqual(state.target_remaining_qty, 3)
        for value in ("1.35000000000150", "01.3500000000015", "+1.3500000000015",
                      "1.35e0", "1.35000000000151"):
            with self.subTest(derived_ratio=value):
                malformed = copy.deepcopy(plan)
                malformed["partial_reduction"]["second_stage"]["trigger_ratio"] = value
                with self.assertRaises(TraderError):
                    validate_plan(malformed)

    def test_legacy_plan_keeps_its_old_schema_and_cannot_use_new_quantity_rule(self):
        legacy = json.loads((Path(__file__).parents[1] / "qa/fixtures/base-plan.json").read_text(encoding="utf-8"))
        validate_plan(legacy)
        self.assertEqual(legacy["schema_version"], "1.0")
        self.assertEqual(legacy["partial_reduction"]["quantity_rule"], "CEIL_Q0_DIV_2_AND_4")
        legacy["partial_reduction"]["quantity_rule"] = "DSV_ROUND_HALF_CURRENT"
        with self.assertRaises(TraderError):validate_plan(legacy)
        new = make_plan(); new["schema_version"] = "1.2"
        with self.assertRaises(TraderError):validate_plan(new)


if __name__ == "__main__":
    unittest.main()
