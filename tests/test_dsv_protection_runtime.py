import copy
from concurrent.futures import Future
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.dsv_strategy import DsvStrategyRuntime
from online_trader.strategy.plan import build_development_plan
from qa.recording_port import RecordingPort


class DsvProtectionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.db"
        self.account = "DEV:dsv-protection-" + uuid.uuid4().hex
        self.port = RecordingPort()
        self.kernel = self.new_kernel(self.path, self.account, self.port)

    @staticmethod
    def new_kernel(path, account, port):
        return Kernel(path, account, development=True, port=port, risk_budget="1000000",
                      tick_size="0.5", multiplier="10")

    def tearDown(self):
        try:self.kernel.close()
        except TraderError:
            if not self.kernel._failed:raise
        finally:self.temp.cleanup()

    def cycle(self, identity):
        return next(c for c in self.kernel.snapshot()["cycles"] if c["position_cycle_id"] == identity)

    def create_cycle(self, quantity=5, *, fill_quantity=None, reconcile=True, direction="LONG",
                     half=True, stage2=True, break_even=True, protection=True, gap_day_stop=False, kernel=None):
        kernel = kernel or self.kernel
        settings = load_confirmed_baseline()[0].to_settings()
        settings["trade"].update(half_close_enabled=half, half_stage2=stage2,
            half_breakeven=break_even, protect_80_enabled=protection, stale_close_day_stop=gap_day_stop)
        profile = DsvProfile.from_settings(settings)
        candidate = {"account_key": kernel.account_key, "contract_id": "DCE.p2701", "trading_day": "2026-11-09",
            "profile_hash": profile.content_hash, "direction": direction, "theoretical_price": "1000",
            "limit_price": "1000", "open_offset": "55", "stop_distance": "16", "take_profit_distance": "50"}
        identity = "dsv-protection-cycle-" + uuid.uuid4().hex
        plan = build_development_plan(profile, candidate, account_key=kernel.account_key, trading_day="2026-11-09",
            config_revision=str(kernel._config_revision), config_hash=kernel._configuration["payload_hash"],
            rule_set_id=kernel._configuration["rule_set_id"], quantity=quantity,
            plan_id="dsv-protection-plan-" + uuid.uuid4().hex, position_cycle_id=identity)
        self.assertEqual(plan["schema_version"], "1.1")
        kernel.arm_development();kernel.wait_idle()
        context = kernel.issue_entry_context("qa", plan["contract_id"])
        child = kernel.admit_entry(plan, "dsv-protection-decision-" + uuid.uuid4().hex, "1000",
                                  context_id=context["context_id"])
        kernel.wait_idle()
        actual = quantity if fill_quantity is None else fill_quantity
        if actual:
            kernel.record_fill(child, "entry-" + uuid.uuid4().hex, actual, "1000");kernel.wait_idle()
        if reconcile:
            if actual != quantity:
                kernel.confirm_terminal(child, "CANCELLED", actual);kernel.wait_idle()
            kernel.reconcile_development_cycle(identity);kernel.wait_idle()
        return identity, child, plan

    def observe(self, identity, move, *, wait=True):
        plan = self.cycle(identity)["plan"];direction = plan["direction"]
        price = Decimal(plan["theoretical_entry_price"]) + (Decimal(str(move)) if direction == "LONG" else -Decimal(str(move)))
        bid, ask = (price, price + Decimal("0.5")) if direction == "LONG" else (price - Decimal("0.5"), price)
        result = self.kernel.observe(identity, decimal_text(price), bid1=decimal_text(bid), ask1=decimal_text(ask))
        if wait:self.kernel.wait_idle()
        return result

    def exits(self, identity):return [o for o in self.cycle(identity)["orders"] if o["effect"] == "REDUCE"]

    def requests(self, identity):return [o for o in self.port.orders if o["effect"] == "REDUCE" and o["position_cycle_id"] == identity]

    def reopen(self):
        self.kernel.close();self.port = RecordingPort()
        self.kernel = self.new_kernel(self.path, self.account, self.port)

    def test_round_half_current_odd_quantities_and_one_lot_do_not_create_zero_orders(self):
        for quantity, expected_closed in ((1, 0), (3, 2), (5, 2), (7, 4)):
            with self.subTest(quantity=quantity):
                identity, _, _ = self.create_cycle(quantity, stage2=False, break_even=False)
                self.observe(identity, 20)
                requests = self.requests(identity)
                self.assertEqual(sum(o["original_qty"] for o in requests), expected_closed)
                if expected_closed:
                    state = self.cycle(identity)["protection"]
                    self.assertEqual(state["target_remaining_qty"], quantity - expected_closed)
                    self.assertEqual(state["phase1_target"], quantity - expected_closed)
                self.assertTrue(all(o["original_qty"] > 0 for o in requests))

    def test_one_quote_consumes_only_one_stage_and_next_stage_uses_current_move(self):
        identity, _, _ = self.create_cycle(5, break_even=False)
        self.observe(identity, 35)
        state = self.cycle(identity)["protection"]
        self.assertEqual(state["target_remaining_qty"], 3)
        self.assertEqual(state["phase2_status"], "NOT_TRIGGERED")
        self.observe(identity, 15)
        self.assertEqual(self.cycle(identity)["protection"]["phase2_status"], "NOT_TRIGGERED")
        self.assertEqual(sum(o["original_qty"] for o in self.requests(identity)), 2)
        self.observe(identity, 30)
        self.assertEqual(self.cycle(identity)["protection"]["target_remaining_qty"], 1)
        self.assertEqual(sum(o["original_qty"] for o in self.requests(identity)), 4)

    def test_second_target_can_tighten_inflight_but_break_even_requires_a_real_phase_one_fill(self):
        identity, _, _ = self.create_cycle(5)
        self.observe(identity, 20);first = self.exits(identity)[0]
        self.assertEqual(self.cycle(identity)["protection"]["break_even_status"], "WAITING_FILL")
        self.observe(identity, 30)
        state = self.cycle(identity)["protection"]
        self.assertEqual((state["phase1_target"], state["phase2_target"], state["target_remaining_qty"]), (3, 1, 1))
        self.assertEqual(state["break_even_status"], "WAITING_FILL")
        self.assertEqual(sum(OrderState.from_dict(o).potential_remaining for o in self.exits(identity)), 4)
        self.kernel.record_fill(first["child_order_id"], "first-real-half-fill", 1, "1020");self.kernel.wait_idle()
        state = self.cycle(identity)["protection"]
        self.assertEqual(state["break_even_status"], "ACTIVE")
        self.assertEqual(state["owned_qty"], 4)
        self.assertEqual(state["phase1_confirmed_reduction_qty"], 1)
        self.assertEqual(sum(o["original_qty"] for o in self.requests(identity)), 4)

    def test_latched_half_waits_for_entry_terminal_and_includes_late_entry_before_barrier(self):
        identity, entry, plan = self.create_cycle(5, fill_quantity=3, reconcile=False)
        self.observe(identity, 20)
        self.assertEqual(self.cycle(identity)["protection"]["phase1_status"], "WAITING_ENTRY_TERMINAL")
        self.assertEqual(self.requests(identity), [])
        self.observe(identity, 15)
        self.kernel.record_fill(entry, "late-before-entry-barrier", 2, "1000");self.kernel.wait_idle()
        self.assertEqual(self.requests(identity), [])
        self.kernel.reconcile_development_cycle(identity);self.kernel.wait_idle()
        state = self.cycle(identity)["protection"]
        self.assertEqual(state["owned_qty"], 5)
        self.assertEqual(state["target_remaining_qty"], 3)
        self.assertEqual(sum(o["original_qty"] for o in self.requests(identity)), 2)
        self.assertEqual(self.cycle(identity)["plan"], plan)

    def test_late_entry_after_frozen_target_invalidates_barrier_without_loosening_plan_or_target(self):
        identity, entry, plan = self.create_cycle(5, fill_quantity=3, stage2=False, break_even=False)
        self.observe(identity, 20)
        self.assertEqual(self.cycle(identity)["protection"]["target_remaining_qty"], 1)
        before = len(self.requests(identity))
        self.kernel.record_fill(entry, "late-after-entry-barrier", 2, "1000");self.kernel.wait_idle()
        state = self.cycle(identity)["protection"]
        self.assertEqual(state["owned_qty"], 5)
        self.assertEqual(state["target_remaining_qty"], 1)
        self.assertTrue(state["reconciliation_required"])
        self.assertTrue(self.kernel.snapshot()["recovery_required"])
        self.assertEqual(len(self.requests(identity)), before)
        self.assertEqual(self.cycle(identity)["plan"], plan)
        self.kernel.reconcile_development_cycle(identity);self.kernel.wait_idle()
        self.assertEqual(sum(o["original_qty"] for o in self.requests(identity)), 4)
        self.assertEqual(self.cycle(identity)["protection"]["target_remaining_qty"], 1)

    def test_protection_suppresses_unconsumed_half_stages_and_shares_inflight_quantity(self):
        identity, _, _ = self.create_cycle(7)
        self.observe(identity, 20)
        half = self.exits(identity)[0]
        self.assertEqual(half["original_qty"], 4)
        self.observe(identity, 40)
        state = self.cycle(identity)["protection"]
        self.assertEqual(state["mode"], "PROTECTED")
        self.assertEqual(state["phase2_status"], "SUPPRESSED_PROTECTED")
        self.assertEqual(state["break_even_status"], "SUPPRESSED_PROTECTED")
        self.assertEqual(state["target_remaining_qty"], 3)
        self.observe(identity, 25)
        self.assertEqual(self.cycle(identity)["protection"]["target_remaining_qty"], 0)
        self.assertEqual(sum(o["original_qty"] for o in self.requests(identity)), 7)
        self.kernel.record_fill(half["child_order_id"], "protected-late-half-fill", 4, "1020");self.kernel.wait_idle()
        self.assertEqual(self.cycle(identity)["protection"]["break_even_status"], "SUPPRESSED_PROTECTED")
        self.assertEqual(sum(o["original_qty"] for o in self.requests(identity)), 7)

    def test_unknown_half_orders_remain_occupied_when_second_target_tightens(self):
        identity, _, _ = self.create_cycle(5)
        self.port.fail_send = True
        self.observe(identity, 20);self.observe(identity, 20)
        self.assertEqual(len(self.requests(identity)), 1)
        self.assertEqual(self.exits(identity)[0]["send_state"], "UNKNOWN")
        self.observe(identity, 30);self.observe(identity, 30)
        self.assertEqual(len(self.requests(identity)), 2)
        state = self.cycle(identity)["protection"]
        pending = sum(OrderState.from_dict(o).potential_remaining for o in self.exits(identity))
        self.assertEqual((state["owned_qty"], state["target_remaining_qty"], pending), (5, 1, 4))
        self.assertEqual(state["break_even_status"], "WAITING_FILL")

    def test_cancel_quantity_mismatch_cannot_release_unknown_half_order(self):
        identity, _, _ = self.create_cycle(5, stage2=False, break_even=False)
        self.port.fail_send = True;self.observe(identity, 20)
        exit_order = self.exits(identity)[0]
        with self.assertRaises(TraderError):self.kernel.confirm_terminal(exit_order["child_order_id"], "CANCELLED", 1)
        self.observe(identity, 20)
        self.assertEqual(len(self.requests(identity)), 1)
        self.assertEqual(OrderState.from_dict(self.exits(identity)[0]).potential_remaining, 2)
        self.port.fail_send = False
        self.kernel.confirm_terminal(exit_order["child_order_id"], "CANCELLED", 0);self.kernel.wait_idle()
        self.assertEqual(len(self.requests(identity)), 2)
        self.assertEqual(self.requests(identity)[-1]["original_qty"], 2)

    def test_unknown_exit_restart_preserves_stage_target_and_never_replays(self):
        identity, _, plan = self.create_cycle(5)
        self.port.fail_send = True;self.observe(identity, 20);self.observe(identity, 30)
        order_ids = [o["child_order_id"] for o in self.exits(identity)]
        self.reopen()
        cycle = self.cycle(identity)
        self.assertEqual(cycle["plan"], plan)
        self.assertEqual(cycle["protection"]["target_remaining_qty"], 1)
        self.assertEqual([o["child_order_id"] for o in self.exits(identity)], order_ids)
        self.assertTrue(all(o["send_state"] == "UNKNOWN" for o in self.exits(identity)))
        self.assertFalse(self.kernel.snapshot()["armed"])
        self.observe(identity, 30)
        with self.assertRaises(TraderError):self.kernel.reconcile_development_cycle(identity)
        self.assertEqual(self.port.orders, [])

    def test_source_max_drawdown_is_mirrored_and_not_pure_peak_ratio(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                self.kernel.close();self.path = Path(self.temp.name) / ("trailing-" + direction + ".db")
                self.port = RecordingPort();self.kernel = self.new_kernel(self.path, self.account, self.port)
                identity, _, plan = self.create_cycle(1, direction=direction, half=False, stage2=False,
                                                      break_even=False, protection=False)
                self.assertEqual(plan["trailing_stop"]["drawdown_mode"], "DSV_MAX_TP_AND_PEAK_RATIO")
                self.observe(identity, 42)
                self.assertEqual(self.cycle(identity)["protection"]["trailing_floor_move"], "37")
                self.observe(identity, "37.5")
                self.assertEqual(self.requests(identity), [])
                self.observe(identity, 37)
                self.assertEqual(self.requests(identity)[0]["original_qty"], 1)
                self.assertTrue(self.cycle(identity)["protection"]["block_new_for_trading_day"])

    def test_trailing_restart_requires_reconciliation_before_original_exit_continues(self):
        identity, _, plan = self.create_cycle(1, half=False, stage2=False, break_even=False, protection=False)
        self.observe(identity, 42)
        self.reopen()
        self.assertEqual(self.cycle(identity)["plan"], plan)
        self.assertEqual(self.cycle(identity)["protection"]["max_favorable_move"], "42")
        self.assertEqual(self.cycle(identity)["protection"]["trailing_floor_move"], "37")
        self.observe(identity, 37)
        self.assertEqual(self.port.orders, [])
        self.kernel.reconcile_development_cycle(identity);self.kernel.wait_idle()
        self.assertEqual(len(self.requests(identity)), 1)
        self.assertEqual(self.requests(identity)[0]["original_qty"], 1)
        self.assertFalse(self.kernel.snapshot()["armed"])

    def test_nondefault_profile_runs_through_the_formal_development_strategy_chain(self):
        settings = load_confirmed_baseline()[0].to_settings()
        settings["trade"].update(half_close_enabled=True, half_stage2=True, half_breakeven=True)
        variant = DsvProfile.from_settings(settings)
        runtime = DsvStrategyRuntime(self.kernel, profile=variant)
        self.assertFalse(runtime.activate()["armed"])
        self.kernel.arm_development();self.kernel.wait_idle()
        market = {"account_key": self.account, "contract_id": "DCE.p2701", "trading_day": "2026-11-09",
            "session_id": "day-20261109", "source_epoch": 1, "sequence": 1,
            "source_time_utc": "2026-11-09T01:05:00Z", "session_open_utc": "2026-11-09T01:00:00Z",
            "session_close_utc": "2026-11-09T07:00:00Z", "last": "1055", "bid1": "1054", "ask1": "1056",
            "day_high": "1090", "day_low": "1000", "tick_size": "1", "quality": "CONTIGUOUS",
            "day_range_complete": True, "path_complete": True, "is_trading": True, "calendar_complete": True,
            "candle_id": "20261109-0105"}
        account = {"account_key": self.account, "trading_day": "2026-11-09", "mode": "simulation", "revision": 1,
            "complete": True, "equity": "400000", "available": "300000", "margin": "10000", "frozen_margin": "1000",
            "pending_margin": "0", "symbol_margin": "1000", "risk_ratio": "0.1", "daily_loss": "0",
            "orders_today": 0, "active_symbols": [], "stopped_symbols": [], "unknown_orders": False,
            "local_reservations_excluded": True}
        instrument = {"contract_id": "DCE.p2701", "trading_day": "2026-11-09", "complete": True,
            "tick_size": "1", "multiplier": "10", "margin_rate": "0.1", "margin_per_lot": "1000",
            "lower_limit": "800", "upper_limit": "1200", "min_order_qty": 1, "max_order_qty": 100,
            "days_to_expiry": 30}
        future = runtime.on_market(market, account, instrument);self.kernel.wait_idle()
        child = future.result()["child_order_id"]
        cycle = next(c for c in self.kernel.snapshot()["cycles"] if c["orders"][0]["child_order_id"] == child)
        identity = cycle["position_cycle_id"]
        self.assertEqual(cycle["plan"]["initial_open_quantity"], 5)
        self.assertTrue(cycle["plan"]["partial_reduction"]["enabled"])
        self.assertEqual(cycle["plan"]["partial_reduction"]["quantity_rule"], "DSV_ROUND_HALF_CURRENT")
        self.assertIsNone(cycle["plan"]["approval_record_id"])
        self.assertEqual(self.kernel.ledger.query("SELECT origin FROM order_intents WHERE effect='OPEN'"), [{"origin": "STRATEGY"}])
        self.kernel.record_fill(child, "variant-opening-filled", 5, "1055");self.kernel.wait_idle()
        self.kernel.reconcile_development_cycle(identity);self.kernel.wait_idle()
        self.observe(identity, 20)
        self.assertEqual(self.requests(identity)[0]["original_qty"], 2)
        self.assertEqual(self.cycle(identity)["protection"]["target_remaining_qty"], 3)

    def test_gap_day_stop_waits_for_confirmed_flat_not_target_or_send(self):
        identity, _, plan = self.create_cycle(1, half=False, stage2=False, break_even=False, gap_day_stop=True)
        self.observe(identity, 10)
        self.kernel.observe_development_event(identity, {"type": "DATA_GAP", "active_trading": True,
            "gap_ms": 180001, "session_elapsed_ms": 300000, "policy_authorized": True,
            "policy_id": plan["data_gap_exit"]["data_quality_policy_id"], "legal_price_available": True})
        self.kernel.wait_idle()
        state = self.cycle(identity)["protection"]
        self.assertEqual(state["target_remaining_qty"], 0)
        self.assertTrue(state["gap_day_stop_pending"])
        self.assertFalse(state["block_new_for_trading_day"])
        self.observe(identity, 10)
        request = self.requests(identity)[0]
        self.assertFalse(self.cycle(identity)["protection"]["block_new_for_trading_day"])
        self.kernel.record_fill(request["child_order_id"], "gap-exit-confirmed", 1, "1010");self.kernel.wait_idle()
        state = self.cycle(identity)["protection"]
        self.assertEqual(state["owned_qty"], 0)
        self.assertTrue(state["block_new_for_trading_day"])

    def test_target_rollback_or_wrong_ack_never_dispatches_a_half_order(self):
        for mode in ("rollback", "wrong-ack"):
            with self.subTest(mode=mode):
                # Close the preceding isolated owner and use a fresh file so a
                # deliberately failed writer cannot affect the next scenario.
                self.kernel.close();self.path = Path(self.temp.name) / (mode + ".db")
                self.port = RecordingPort();self.kernel = self.new_kernel(self.path, self.account, self.port)
                identity, _, _ = self.create_cycle(5)
                if mode == "rollback":
                    original = self.kernel.ledger.submit
                    def fail(kind, event_type, payload, mutate=None, **kwargs):
                        if event_type == "PROTECTION_OBSERVATION":
                            def rolled_back(connection, sequence):
                                mutate(connection, sequence)
                                raise sqlite3.OperationalError("Injected failed half target commit")
                            return original(kind, event_type, payload, rolled_back, **kwargs)
                        return original(kind, event_type, payload, mutate, **kwargs)
                    self.kernel.ledger.submit = fail
                self.observe(identity, 20, wait=False)
                if mode == "wrong-ack":
                    pending = self.kernel._pending[0];ack = pending.future.result(timeout=3)
                    pending.future = Future();pending.future.set_result(replace(ack, kernel_boot_id="old-boot"))
                with self.assertRaises(TraderError):self.kernel.wait_idle()
                self.assertEqual([o for o in self.port.orders if o["effect"] == "REDUCE"], [])
                self.assertIsNone(self.kernel._committed[identity]["protection"]["target_remaining_qty"])
                if mode == "rollback":
                    self.assertIsNone(self.kernel.ledger.query("SELECT target_remaining_qty FROM protection_state WHERE position_cycle_id=?", (identity,))[0]["target_remaining_qty"])
                try:self.kernel.close()
                except TraderError:
                    if not self.kernel._failed:raise


if __name__ == "__main__":unittest.main()
