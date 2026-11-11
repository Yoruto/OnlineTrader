import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.strategy import StrategyCoordinator
from qa.recording_port import RecordingPort


BASE = json.loads((Path(__file__).parents[1] / "qa/fixtures/base-plan.json").read_text(encoding="utf-8"))


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "ledger.db"
        self.account = "DEV:strategy-" + uuid.uuid4().hex
        self.port = RecordingPort()
        self.kernel = Kernel(self.path, self.account, development=True, port=self.port, risk_budget="1000")
        self.coordinator = StrategyCoordinator(self.kernel)

    def tearDown(self):
        try:
            if not self.kernel._closed:
                self.kernel.close()
        finally:
            self.temporary.cleanup()

    def activate(self, instance="worker-one"):
        _seq, digest = self.coordinator.checkpoint()
        epoch = self.coordinator.register_shadow(instance, digest)
        barrier, digest = self.coordinator.checkpoint()
        old = self.kernel.active_epoch
        self.coordinator.switch(epoch, old, barrier, digest)
        self.kernel.wait_idle()
        return epoch

    def create_protected_cycle(self):
        self.kernel.arm_development(); self.kernel.wait_idle()
        plan = copy.deepcopy(BASE)
        plan.update(account_key=self.account, position_cycle_id="cycle-"+uuid.uuid4().hex,
                    plan_id="plan-"+uuid.uuid4().hex, initial_open_quantity=1,
                    config_hash=self.kernel._configuration["payload_hash"])
        plan["partial_reduction"] = {"enabled": False}
        context = self.kernel.issue_entry_context("qa", plan["contract_id"])
        child = self.kernel.admit_entry(plan, "decision-"+uuid.uuid4().hex, "1000", context_id=context["context_id"])
        self.kernel.wait_idle()
        self.kernel.record_fill(child, "trade-"+uuid.uuid4().hex, 1, "1000"); self.kernel.wait_idle()
        self.kernel.observe(plan["position_cycle_id"], "1080", bid1="1080", ask1="1081"); self.kernel.wait_idle()
        return plan["position_cycle_id"]

    def test_shadow_registration_does_not_make_business_hash_drift(self):
        seq, digest = self.coordinator.checkpoint()
        epoch = self.coordinator.register_shadow("shadow-one", digest)
        next_seq, next_digest = self.coordinator.checkpoint()
        self.assertGreater(next_seq, seq)
        self.assertEqual(digest, next_digest)
        self.assertEqual("SHADOW", self.kernel.ledger.query("SELECT status FROM strategy_instances WHERE strategy_epoch=?", (epoch,))[0]["status"])
        with self.assertRaises(TraderError): self.coordinator.assert_active(epoch)

    def test_activation_waits_for_owner_to_accept_commit_ack(self):
        _seq, digest = self.coordinator.checkpoint()
        epoch = self.coordinator.register_shadow("shadow-one", digest)
        seq, digest = self.coordinator.checkpoint()
        future = self.coordinator.switch(epoch, None, seq, digest)
        future.result(5)
        self.assertIsNone(self.kernel.active_epoch)
        with self.assertRaises(TraderError): self.coordinator.assert_active(epoch)
        self.kernel.wait_idle()
        self.assertEqual(epoch, self.kernel.active_epoch)
        self.coordinator.assert_active(epoch)
        self.assertFalse(self.kernel.snapshot()["armed"])

    def test_switch_rejects_old_epoch_and_never_has_two_active_rows(self):
        old = self.activate()
        new = self.activate("worker-two")
        self.assertGreater(new, old)
        with self.assertRaises(TraderError): self.coordinator.assert_active(old)
        self.coordinator.assert_active(new)
        self.assertEqual(1, self.kernel.ledger.query("SELECT count(*) AS n FROM strategy_instances WHERE status='ACTIVE'")[0]["n"])
        self.assertEqual("REVOKED", self.kernel.ledger.query("SELECT status FROM strategy_instances WHERE strategy_epoch=?", (old,))[0]["status"])

    def test_barrier_and_hash_mismatch_have_no_handoff_side_effect(self):
        active = self.activate()
        _seq, digest = self.coordinator.checkpoint()
        candidate = self.coordinator.register_shadow("worker-two", digest)
        seq, digest = self.coordinator.checkpoint()
        for barrier, state_hash in [(seq-1, digest), (seq, "0"*64)]:
            with self.assertRaises(TraderError): self.coordinator.switch(candidate, active, barrier, state_hash)
        self.coordinator.assert_active(active)
        self.assertFalse(self.kernel._strategy_switch_pending)
        self.assertEqual("SHADOW", self.kernel.ledger.query("SELECT status FROM strategy_instances WHERE strategy_epoch=?", (candidate,))[0]["status"])

    def test_candidate_must_prove_changed_business_state_again(self):
        _seq, digest = self.coordinator.checkpoint()
        candidate = self.coordinator.register_shadow("old-checkpoint", digest)
        self.create_protected_cycle()
        seq, changed = self.coordinator.checkpoint()
        self.assertNotEqual(digest, changed)
        with self.assertRaises(TraderError): self.coordinator.switch(candidate, None, seq, changed)
        self.assertIsNone(self.kernel.active_epoch)

    def test_handoff_preserves_protection_opportunities_and_orders(self):
        self.create_protected_cycle()
        before = self.kernel.snapshot()["cycles"]
        opportunities = set(self.kernel._decision_keys)
        count = len(self.port.orders)
        self.activate()
        self.assertEqual(before, self.kernel.snapshot()["cycles"])
        self.assertEqual(opportunities, self.kernel._decision_keys)
        self.assertEqual(count, len(self.port.orders))
        self.assertEqual("PROTECTED", before[0]["protection"]["mode"])

    def test_new_boot_revokes_old_epoch_and_epoch_numbers_do_not_repeat(self):
        old = self.activate()
        self.kernel.close()
        self.kernel = Kernel(self.path, self.account, development=True, port=self.port, risk_budget="1000")
        self.coordinator = StrategyCoordinator(self.kernel)
        self.assertIsNone(self.kernel.active_epoch)
        with self.assertRaises(TraderError): self.coordinator.assert_active(old)
        self.assertEqual("REVOKED", self.kernel.ledger.query("SELECT status FROM strategy_instances WHERE strategy_epoch=?", (old,))[0]["status"])
        self.assertGreater(self.activate("worker-after-restart"), old)

    def test_failed_switch_rolls_back_both_ownership_updates(self):
        active = self.activate()
        _seq, digest = self.coordinator.checkpoint()
        candidate = self.coordinator.register_shadow("failure-candidate", digest)
        seq, digest = self.coordinator.checkpoint()
        original_submit = self.kernel.ledger.submit
        def broken_submit(kind, event_type, payload, mutate=None, **kwargs):
            def broken(conn, durable_seq):
                mutate(conn, durable_seq)
                raise RuntimeError("Injected switch failure")
            return original_submit(kind, event_type, payload, broken, **kwargs)
        with patch.object(self.kernel.ledger, "submit", side_effect=broken_submit):
            future = self.coordinator.switch(candidate, active, seq, digest)
        with self.assertRaises(RuntimeError): future.result(5)
        with self.assertRaises(TraderError): self.kernel.wait_idle()
        self.assertEqual(active, self.kernel.ledger.query("SELECT strategy_epoch FROM authority")[0]["strategy_epoch"])
        self.assertEqual("ACTIVE", self.kernel.ledger.query("SELECT status FROM strategy_instances WHERE strategy_epoch=?", (active,))[0]["status"])
        self.assertEqual("SHADOW", self.kernel.ledger.query("SELECT status FROM strategy_instances WHERE strategy_epoch=?", (candidate,))[0]["status"])
        with self.assertRaises(TraderError): self.coordinator.assert_active(active)
        with self.assertRaises(TraderError): self.kernel.close()

    def test_duplicate_registration_is_refused_without_poisoning_storage(self):
        _seq, digest = self.coordinator.checkpoint()
        self.coordinator.register_shadow("same-instance", digest)
        with self.assertRaises(TraderError): self.coordinator.register_shadow("same-instance", digest)
        self.assertFalse(self.kernel.ledger.failed)

    def test_default_runtime_and_foreign_thread_cannot_switch(self):
        errors = []
        def outside_owner():
            try: self.coordinator.checkpoint()
            except TraderError as exc: errors.append(exc.code)
        thread = threading.Thread(target=outside_owner); thread.start(); thread.join()
        self.assertEqual(["E_OWNER"], errors)
        self.kernel.close()
        self.kernel = Kernel(self.path, self.account)
        self.coordinator = StrategyCoordinator(self.kernel)
        with self.assertRaises(TraderError): self.coordinator.checkpoint()


if __name__ == "__main__": unittest.main()
