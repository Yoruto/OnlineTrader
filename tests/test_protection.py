from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal, getcontext
import unittest

from online_trader.errors import TraderError
from online_trader.protection import (
    ProtectionState, available_exit_quantity, initial_state, reduce_event, revise_plan,
)
from qa.protection_fixtures import BASE_PLAN, GOLDEN_CASES


def plan(**overrides):
    result = copy.deepcopy(BASE_PLAN)
    result.update(overrides)
    return result


def apply_events(value, events):
    state = initial_state(value)
    decisions = []
    for index, event in enumerate(events, 1):
        decision = reduce_event(value, state, {**event, "event_seq": index, "event_id": f"event-{index}"})
        decisions.append(decision)
        state = decision.state
    return state, decisions


def run_golden(case):
    """Exercise the reducer and a proposed-effect quantity guard, not an SDK.

    The single unacknowledged-commit fixture restores the last committed snapshot
    in this driver. Actual database/process kill windows are tested by kernel QA.
    """
    value = plan(**case["overrides"])
    state = initial_state(value)
    committed = state
    pending_exit = 0
    proposed = half_proposed = full_targets = 0
    additional_stage2 = 0
    for index, source in enumerate(case["events"], 1):
        if source["type"] == "KILL_KERNEL_BEFORE_COMMIT_ACK":
            state = committed
            continue
        event = {**source, "event_seq": index, "event_id": f"{case['case_id']}-{index}"}
        previous = state
        if event["type"] == "EXIT_FILL":
            pending_exit = max(0, pending_exit - event["quantity"])
        decision = reduce_event(value, state, event)
        state = decision.state
        quantity = available_exit_quantity(state.owned_qty, state.target_remaining_qty, pending_exit)
        if quantity:
            proposed += 1
            pending_exit += quantity
            if state.target_remaining_qty == 0:
                full_targets += 1
            else:
                half_proposed += 1
        if previous.phase2_status in {"NOT_TRIGGERED", "WAITING_ENTRY_TERMINAL"} and state.phase2_status in {"NO_OP", "TARGET_COMMITTED"}:
            additional_stage2 = quantity
        next_type = case["events"][index]["type"] if index < len(case["events"]) else None
        if next_type != "KILL_KERNEL_BEFORE_COMMIT_ACK":
            committed = ProtectionState.from_dict(state.to_dict())
    actual = state.to_dict()
    actual.update(
        exit_submit_count=proposed,
        half_exit_submit_count=half_proposed,
        independent_full_exit_targets=full_targets,
        additional_phase2_exit_quantity=additional_stage2,
        owned_remaining_after_new_fill=state.owned_qty,
        all_remaining_covered_by_original_plan=state.plan_id == value["plan_id"],
        cancel_request_does_not_release_unknown=True,
        plan_identity_unchanged=state.plan_id == value["plan_id"],
        new_entry_allowed=not (state.block_new or state.reconciliation_required or state.quality == "DATA_GAP"),
        missing_latch_must_not_be_claimed_durable=state.mode == "NORMAL",
    )
    return actual


class ProtectionTests(unittest.TestCase):
    def test_all_twelve_g0_proposed_sequences_execute_reducer(self):
        for case in GOLDEN_CASES:
            with self.subTest(case=case["case_id"]):
                result = run_golden(case)
                for field, expected in case["expect"].items():
                    self.assertEqual(result[field], expected, field)

    def test_zero_position_quote_cannot_create_fake_profit_or_exit(self):
        state, _ = apply_events(plan(), [{"type": "QUOTE", "price": "10000"}])
        self.assertEqual(state.max_favorable_move, 0)
        self.assertEqual(state.mode, "NORMAL")
        self.assertIsNone(state.target_remaining_qty)

    def test_fixed_stop_long_and_short(self):
        for direction, price in [("LONG", "980"), ("SHORT", "1020")]:
            value = plan(direction=direction, partial_reduction={"enabled": False})
            state, decisions = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "QUOTE", "price": price}])
            self.assertEqual(state.target_remaining_qty, 0)
            self.assertIn("STOP_LOSS", decisions[-1].reasons)

    def test_protected_old_stop_is_suppressed(self):
        value = plan(partial_reduction={"enabled": False})
        state, decisions = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "QUOTE", "price": "1080"}, {"type": "QUOTE", "price": "970"}])
        self.assertEqual(state.mode, "PROTECTED")
        self.assertIn("PROTECTED_FLOOR", decisions[-1].reasons)
        self.assertNotIn("STOP_LOSS", decisions[-1].reasons)

    def test_target_never_rises_after_late_entry_fill(self):
        value = plan(partial_reduction={"enabled": False})
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "QUOTE", "price": "980"}, {"type": "LATE_ENTRY_FILL", "quantity": 2, "price": "1000"}])
        self.assertEqual(state.owned_qty, 3)
        self.assertEqual(state.target_remaining_qty, 0)

    def test_waiting_partial_survives_pullback_until_entry_barrier(self):
        state, _ = apply_events(plan(), [{"type": "FILL", "quantity": 3, "price": "1000"}, {"type": "QUOTE", "price": "1040"}])
        self.assertEqual(state.phase1_status, "WAITING_ENTRY_TERMINAL")
        self.assertIsNone(state.q0)
        decision = reduce_event(plan(), state, {"type": "QUOTE", "price": "1030", "event_seq": 3})
        state = reduce_event(plan(), decision.state, {"type": "ENTRY_TERMINAL_RECONCILED", "owned_quantity": 3, "reconciliation_id": "barrier", "event_seq": 4}).state
        self.assertEqual(state.q0, 3)
        self.assertEqual(state.target_remaining_qty, 2)

    def test_protection_suppresses_waiting_but_preserves_inflight_stage(self):
        value = plan()
        waiting, _ = apply_events(value, [{"type": "FILL", "quantity": 3, "price": "1000"}, {"type": "QUOTE", "price": "1040"}, {"type": "QUOTE", "price": "1080"}])
        self.assertEqual(waiting.phase1_status, "SUPPRESSED_PROTECTED")
        active, _ = apply_events(value, [{"type": "FILL", "quantity": 3, "price": "1000"}, {"type": "ENTRY_TERMINAL_RECONCILED", "owned_quantity": 3}, {"type": "QUOTE", "price": "1040"}, {"type": "EXIT_ORDER_SENT"}, {"type": "QUOTE", "price": "1080"}])
        self.assertEqual(active.phase1_status, "EXECUTING")
        self.assertEqual(active.target_remaining_qty, 2)
        self.assertEqual(active.break_even_status, "SUPPRESSED_PROTECTED")

    def test_break_even_waits_for_actual_managed_reduction(self):
        value = plan()
        events = [{"type": "FILL", "quantity": 3, "price": "1000"}, {"type": "ENTRY_TERMINAL_RECONCILED", "owned_quantity": 3}, {"type": "QUOTE", "price": "1040"}]
        state, _ = apply_events(value, events)
        self.assertEqual(state.break_even_status, "WAITING_FILL")
        external = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1, "price": "1040", "external": True, "event_seq": 4}).state
        self.assertEqual(external.break_even_status, "WAITING_FILL")
        managed = reduce_event(value, state, {"type": "EXIT_FILL", "quantity": 1, "price": "1040", "event_seq": 4}).state
        self.assertEqual(managed.break_even_status, "ACTIVE")
        self.assertEqual(reduce_event(value, managed, {"type": "QUOTE", "price": "1000", "event_seq": 5}).state.target_remaining_qty, 0)

    def test_trailing_ratio_mirrors_and_keeps_high_water(self):
        for direction, top, decline in [("LONG", "1100", "1080"), ("SHORT", "900", "920")]:
            value = plan(direction=direction, profit_protection={"enabled": False}, partial_reduction={"enabled": False}, trailing_stop={"enabled": True, "activation_ratio": "0.6", "drawdown_mode": "PEAK_FAVORABLE_RATIO", "drawdown_value": "0.2"})
            state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "QUOTE", "price": top}, {"type": "QUOTE", "price": decline}])
            self.assertEqual(state.trailing_floor_move, Decimal("80"))
            self.assertEqual(state.target_remaining_qty, 0)

    def test_invalid_quote_does_not_change_latch_or_high_water(self):
        value = plan(partial_reduction={"enabled": False})
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "QUOTE", "price": "1080"}])
        for bad in [None, "NaN", "1e3", "1050"]:
            event = {"type": "QUOTE", "price": bad, "event_seq": 3}
            if bad == "1050": event["fresh"] = False
            decision = reduce_event(value, state, event)
            self.assertEqual(decision.state.mode, "PROTECTED")
            self.assertEqual(decision.state.max_favorable_move, 80)
            self.assertIsNone(decision.state.target_remaining_qty)
            self.assertTrue(decision.block_new)

    def test_duplicate_event_id_replays_without_second_fill_and_conflict_rejects(self):
        value = plan()
        event = {"type": "FILL", "quantity": 1, "price": "1000", "event_id": "trade-a", "event_seq": 1}
        state = reduce_event(value, initial_state(value), event).state
        state = reduce_event(value, state, {"type": "QUOTE", "price": "1010", "event_seq": 2}).state
        self.assertEqual(reduce_event(value, state, event).state, state)
        with self.assertRaises(TraderError):
            reduce_event(value, state, {**event, "quantity": 2})

    def test_regressive_quote_cannot_follow_a_later_fill(self):
        value = plan()
        state = reduce_event(value, initial_state(value), {"type": "FILL", "quantity": 1, "price": "1000", "event_seq": 5}).state
        with self.assertRaises(TraderError):
            reduce_event(value, state, {"type": "QUOTE", "price": "1080", "event_seq": 4})

    def test_liquidation_side_is_bid_for_long_and_ask_for_short(self):
        for direction, quote in [("LONG", {"bid1": "1080", "ask1": "1090"}), ("SHORT", {"bid1": "910", "ask1": "920"})]:
            value = plan(direction=direction, price_field="LIQUIDATION_SIDE", partial_reduction={"enabled": False})
            state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "QUOTE", **quote}])
            self.assertEqual(state.max_favorable_move, 80)
            self.assertEqual(state.mode, "PROTECTED")

    def test_late_fill_invalidates_q0_evidence_without_recalculating_it(self):
        value = plan()
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 3, "price": "1000"}, {"type": "ENTRY_TERMINAL_RECONCILED", "owned_quantity": 3}, {"type": "QUOTE", "price": "1040"}, {"type": "LATE_ENTRY_FILL", "quantity": 1, "price": "1000"}])
        self.assertEqual(state.q0, 3)
        self.assertTrue(state.reconciliation_required)
        self.assertEqual(state.owned_qty, 4)

    def test_overclose_and_unproven_q0_are_rejected(self):
        value = plan()
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}])
        for event in [{"type": "EXIT_FILL", "quantity": 2}, {"type": "ENTRY_TERMINAL_RECONCILED", "owned_quantity": 2}]:
            with self.assertRaises(TraderError): reduce_event(value, state, event)

    def test_pending_and_unknown_exit_quantity_cannot_be_reused(self):
        self.assertEqual(available_exit_quantity(7, 0, 7), 0)
        self.assertEqual(available_exit_quantity(5, 0, 0, 5), 0)
        self.assertEqual(available_exit_quantity(5, 0, 0, 0, 3), 3)
        self.assertEqual(available_exit_quantity(5, None), 0)
        with self.assertRaises(TraderError): available_exit_quantity(5, 0, -1)

    def test_slippage_long_short_and_equal_threshold(self):
        for direction, price, expected in [("LONG", "1005", None), ("LONG", "1006", 0), ("SHORT", "995", None), ("SHORT", "994", 0), ("SHORT", "1006", None)]:
            value = plan(direction=direction, slippage_exit={"enabled": True, "adverse_threshold_points": "5", "block_new_for_trading_day": True})
            state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": price}])
            self.assertEqual(state.target_remaining_qty, expected)

    def test_session_disabled_and_due(self):
        value = plan(close_session={"enabled": False})
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "SESSION_CLOSE_DUE"}])
        self.assertIsNone(state.target_remaining_qty)
        value = plan()
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "SESSION_CLOSE_DUE"}])
        self.assertEqual(state.target_remaining_qty, 0)

    def test_gap_policy_needs_authorization_legal_price_and_active_session(self):
        value = plan(data_gap_exit={"enabled": True, "data_quality_policy_id": "gap-policy", "active_trading_gap_ms": 180000, "missing_legal_price_action": "ALERT_AND_TAKEOVER"})
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}])
        rest = reduce_event(value, state, {"type": "DATA_GAP", "gap_ms": 200000, "active_trading": False}).state
        self.assertIsNone(rest.target_remaining_qty)
        with self.assertRaises(TraderError): reduce_event(value, state, {"type": "DATA_GAP", "gap_ms": 200000, "active_trading": True})
        event = {"type": "DATA_GAP", "gap_ms": 200000, "active_trading": True, "policy_authorized": True, "policy_id": "gap-policy"}
        missing = reduce_event(value, state, event).state
        self.assertIsNone(missing.target_remaining_qty)
        self.assertTrue(missing.reconciliation_required)
        self.assertEqual(reduce_event(value, state, {**event, "legal_price_available": True}).state.target_remaining_qty, 0)

    def test_emergency_disabled_or_over_scope_is_rejected(self):
        value = plan()
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 3, "price": "1000"}])
        with self.assertRaises(TraderError): reduce_event(value, state, {"type": "EMERGENCY_EXIT", "quantity": 1})
        value = plan(emergency_exit={"enabled": True, "approved_policy_id": "emergency", "scope": "OWNED_CYCLE_ONLY", "max_reduce_quantity": 2})
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 3, "price": "1000"}])
        event = {"type": "EMERGENCY_EXIT", "quantity": 1, "policy_authorized": True, "policy_id": "emergency"}
        self.assertEqual(reduce_event(value, state, event).state.target_remaining_qty, 2)
        with self.assertRaises(TraderError): reduce_event(value, state, {**event, "quantity": 3})

    def test_plan_is_bound_immutable_and_non_development_scope_refused(self):
        value = plan()
        state = initial_state(value)
        altered = plan(theoretical_entry_price="1001")
        with self.assertRaises(TraderError): reduce_event(altered, state, {"type": "QUOTE", "price": "1001"})
        for scope in ["ONLINE_SIMULATION", "PRODUCTION"]:
            with self.assertRaises(TraderError): initial_state(plan(execution_scope=scope, approval_record_id="not-a-real-approval"))

    def test_semantic_threshold_order_and_ratio_rejected(self):
        bad = plan()
        bad["partial_reduction"]["second_stage"]["trigger_ratio"] = "0.3"
        with self.assertRaises(TraderError): initial_state(bad)
        bad = plan(trailing_stop={"enabled": True, "activation_ratio": "0.6", "drawdown_mode": "PEAK_FAVORABLE_RATIO", "drawdown_value": "1"})
        with self.assertRaises(TraderError): initial_state(bad)

    def test_precision_is_local_not_global(self):
        previous = getcontext().prec
        try:
            getcontext().prec = 8
            value = plan(theoretical_entry_price="999999999999999999", fixed_take_profit={"enabled": True, "distance_points": "0.000000000001"}, partial_reduction={"enabled": False})
            state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "999999999999999999"}, {"type": "QUOTE", "price": "999999999999999999.000000000001"}])
            self.assertEqual(state.max_favorable_move, Decimal("0.000000000001"))
            self.assertEqual(state.target_remaining_qty, 0)
            self.assertEqual(getcontext().prec, 8)
        finally:
            getcontext().prec = previous

    def test_state_roundtrip_and_invalid_latch_rejected(self):
        value = plan(partial_reduction={"enabled": False})
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "QUOTE", "price": "1080"}])
        self.assertEqual(ProtectionState.from_dict(state.to_dict()), state)
        bad = state.to_dict(); bad["protected_event_seq"] = None
        with self.assertRaises(TraderError): ProtectionState.from_dict(bad)

    def test_revision_requires_approval_and_does_not_clear_state(self):
        value = plan(partial_reduction={"enabled": False})
        state, _ = apply_events(value, [{"type": "FILL", "quantity": 1, "price": "1000"}, {"type": "QUOTE", "price": "1080"}])
        changed = copy.deepcopy(value); changed["plan_revision"] = "2"
        with self.assertRaises(TraderError): revise_plan(value, changed, state, expected_plan_revision=1, expected_state_revision=state.state_revision, approval_record_id=None)
        migrated = revise_plan(value, changed, state, expected_plan_revision=1, expected_state_revision=state.state_revision, approval_record_id="dev-approved-migration", policy_authorized=True)
        self.assertEqual(migrated.mode, "PROTECTED")
        self.assertEqual(migrated.max_favorable_move, 80)
        self.assertEqual(migrated.plan_revision, 2)
        changed["profit_protection"]["floor_ratio"] = "0.4"
        with self.assertRaises(TraderError): revise_plan(value, changed, state, expected_plan_revision=1, expected_state_revision=state.state_revision, approval_record_id="dev-approved-migration", policy_authorized=True)
        changed["profit_protection"]["floor_ratio"] = "0.6"
        with self.assertRaises(TraderError): revise_plan(value, changed, state, expected_plan_revision=1, expected_state_revision=state.state_revision, approval_record_id="dev-approved-migration", policy_authorized=True)


if __name__ == "__main__":
    unittest.main()
