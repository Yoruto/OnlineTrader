from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.configuration import read_catalog_file


class ConfigurationCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"ledger.db"
        self.account = "DEV:catalog-" + uuid.uuid4().hex
        self.kernel = Kernel(self.path, self.account)

    def tearDown(self):
        try:
            try: self.kernel.close()
            except TraderError:
                if not self.kernel._failed: raise
        finally: self.temp.cleanup()

    def register(self):
        result = self.kernel.configurations.register_confirmed_baseline()
        self.kernel.wait_idle()
        return result.result()

    def changed(self):
        profile, _ = load_confirmed_baseline()
        settings = profile.to_settings()
        settings["trade"]["risk_per_trade_ratio"] = 0.003
        return DsvProfile.from_settings(settings)

    def test_registration_waits_for_owner_ack_and_is_one_atomic_reference(self):
        result = self.kernel.configurations.register_confirmed_baseline()
        self.kernel._pending[0].future.result(timeout=3)
        self.assertFalse(result.done())
        self.assertIsNone(self.kernel.configurations.get())
        self.kernel.wait_idle(); receipt = result.result()
        seq = int(receipt["event_seq"])
        decisions = self.kernel.ledger.query("SELECT * FROM rule_decisions WHERE status='APPROVED'")
        self.assertEqual(len(decisions), 20)
        self.assertEqual({row["recorded_event_seq"] for row in decisions}, {seq})
        self.assertEqual({row["recorded_event_seq"] for row in self.kernel.ledger.query("SELECT * FROM approval_manifests")}, {seq})
        self.assertEqual(self.kernel.ledger.query("SELECT created_event_seq FROM config_revisions WHERE config_revision=?", (receipt["config_revision"],))[0]["created_event_seq"], seq)
        self.assertEqual(self.kernel.ledger.query("SELECT COUNT(*) AS n FROM approval_manifest_items")[0]["n"], 20)
        authority = self.kernel.ledger.query("SELECT config_revision,new_open_status FROM authority")[0]
        self.assertEqual(authority, {"config_revision": 1, "new_open_status": "DISARMED"})
        self.assertFalse(receipt["runtime_activation"])

    def test_inflight_and_committed_registration_are_idempotent(self):
        first = self.kernel.configurations.register_confirmed_baseline()
        self.assertIs(self.kernel.configurations.register_confirmed_baseline(), first)
        self.kernel.wait_idle(); receipt = first.result()
        before = self.kernel._sequence
        replay = self.kernel.configurations.register_confirmed_baseline().result()
        self.assertTrue(replay["replayed"])
        self.assertEqual((replay["config_revision"], replay["tx_id"]), (receipt["config_revision"], receipt["tx_id"]))
        self.assertEqual(self.kernel._sequence, before)
        self.assertEqual(len(self.kernel.configurations.list_revisions()), 1)

    def test_saved_zero_and_disabled_values_are_not_reinterpreted(self):
        self.register()
        profile = DsvProfile.from_record(self.kernel.configurations.get()["settings_record"])
        values = profile.to_settings()
        self.assertTrue(values["trade"]["min_one_lot_fallback"])
        self.assertFalse(values["trade"]["live_protect"])
        self.assertFalse(values["trade"]["require_trigger_update"])
        self.assertEqual(values["trade"]["hysteresis_points"], 0)
        self.assertEqual(values["trade"]["max_quote_age_seconds"], 0)
        self.assertEqual([values["symbols"][-1][k] for k in ("open_offset", "stop_offset", "profit_offset")], [0, 0, 0])

    def test_changed_draft_is_append_only_and_does_not_inherit_confirmation(self):
        baseline = self.register()
        changed = self.changed()
        result = self.kernel.configurations.save(changed, baseline["config_revision"])
        self.assertEqual(self.kernel.configurations.get()["config_revision"], baseline["config_revision"])
        self.kernel.wait_idle(); receipt = result.result()
        self.assertEqual(receipt["confirmation_status"], "UNCONFIRMED_DRAFT")
        self.assertFalse(receipt["matches_confirmed_baseline"])
        self.assertFalse(receipt["runtime_activation"])
        self.assertEqual(len(self.kernel.configurations.list_revisions()), 2)
        original = self.kernel.configurations.get(baseline["config_revision"])
        self.assertEqual(original["confirmation_status"], "CONFIRMED_DSV_REFERENCE")
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM rule_decisions WHERE status='APPROVED'")), 20)
        self.assertEqual(self.kernel.ledger.query("SELECT config_revision FROM authority")[0]["config_revision"], 1)

    def test_revision_conflict_does_not_write_or_allocate(self):
        baseline = self.register(); before = self.kernel._sequence
        for expected in (0, True, -1, baseline["config_revision"] + 1):
            with self.subTest(expected=expected), self.assertRaises(TraderError) as error:
                self.kernel.configurations.save(self.changed(), expected)
            self.assertEqual(error.exception.code, "E_REVISION_CONFLICT")
        self.assertEqual(self.kernel._sequence, before)
        self.assertEqual(len(self.kernel.configurations.list_revisions()), 1)

    def test_saving_identical_current_profile_is_idempotent(self):
        baseline = self.register(); profile, _ = load_confirmed_baseline(); before = self.kernel._sequence
        receipt = self.kernel.configurations.save(profile, baseline["config_revision"]).result()
        self.assertTrue(receipt["replayed"])
        self.assertEqual(self.kernel._sequence, before)

    def test_restart_restores_latest_draft_and_original_reference(self):
        baseline = self.register()
        result = self.kernel.configurations.save(self.changed(), baseline["config_revision"])
        self.kernel.wait_idle(); saved = result.result()
        self.kernel.close(); self.kernel = Kernel(self.path, self.account)
        current = self.kernel.configurations.get()
        self.assertEqual((current["config_revision"], current["profile_hash"]), (saved["config_revision"], saved["profile_hash"]))
        self.assertEqual(self.kernel.configurations.register_confirmed_baseline().result()["config_revision"], baseline["config_revision"])
        self.assertFalse(self.kernel.snapshot()["armed"])

    def test_failed_registration_rolls_back_decisions_manifest_and_profile(self):
        original = self.kernel.ledger.submit
        def injected(kind, event, body, mutate=None, **kwargs):
            if event == "DSV_REFERENCE_REGISTERED":
                def fail(conn, seq):
                    mutate(conn, seq)
                    raise sqlite3.OperationalError("injected required transaction failure")
                return original(kind, event, body, fail, **kwargs)
            return original(kind, event, body, mutate, **kwargs)
        self.kernel.ledger.submit = injected
        result = self.kernel.configurations.register_confirmed_baseline()
        with self.assertRaises(TraderError): self.kernel.wait_idle()
        with self.assertRaises(TraderError): result.result()
        self.assertEqual(self.kernel.ledger.query("SELECT * FROM rule_decisions WHERE status='APPROVED'"), [])
        self.assertEqual(self.kernel.ledger.query("SELECT * FROM approval_manifests"), [])
        self.assertEqual(self.kernel.configurations.list_revisions(), [])

    def test_wrong_ack_never_publishes_catalog_success(self):
        result = self.kernel.configurations.register_confirmed_baseline()
        pending = self.kernel._pending[0]
        ack = pending.future.result(timeout=3)
        bad = Future(); bad.set_result(replace(ack, kernel_boot_id="boot-wrong")); pending.future = bad
        self.kernel.poll()
        self.assertTrue(result.done())
        with self.assertRaises(TraderError) as error: result.result()
        self.assertEqual(error.exception.code, "E_STORAGE")
        self.assertIsNone(self.kernel.configurations.get())

    def test_readonly_history_does_not_create_boot_or_change_authority(self):
        self.register()
        before = self.kernel.ledger.query("SELECT * FROM authority")[0]
        boots = self.kernel.ledger.query("SELECT COUNT(*) AS n FROM kernel_boots")[0]["n"]
        self.kernel.close()
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        history = read_catalog_file(self.path, self.account)
        self.assertEqual(len(history), 1)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(dict(conn.execute("SELECT * FROM authority").fetchone()), before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM kernel_boots").fetchone()[0], boots)

    def test_readonly_history_rejects_wrong_account_and_missing_file(self):
        self.register(); self.kernel.close()
        with self.assertRaises(TraderError) as error: read_catalog_file(self.path, "DEV:other")
        self.assertEqual(error.exception.code, "E_SCOPE")
        missing = self.path.with_name("missing.db")
        with self.assertRaises(TraderError): read_catalog_file(missing, self.account)
        self.assertFalse(missing.exists())

    def test_nonowner_cannot_read_or_write_catalog(self):
        errors = []
        def other():
            try: self.kernel.configurations.list_revisions()
            except TraderError as error: errors.append(error.code)
        worker = threading.Thread(target=other); worker.start(); worker.join(timeout=2)
        self.assertEqual(errors, ["E_OWNER"])
