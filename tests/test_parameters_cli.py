from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from online_trader.cli import main
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import export_profile
from online_trader.errors import TraderError
from online_trader.kernel.engine import Kernel


class ParameterWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile, self.manifest = load_confirmed_baseline()

    def tearDown(self):self.temp.cleanup()

    def invoke(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):result = main(["parameters", *map(str, args)])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_packaged_baseline_binds_approval_and_preserves_source_switches(self):
        self.assertEqual(self.manifest["profile_hash"], self.profile.content_hash)
        self.assertEqual([item["b_code"] for item in self.manifest["decisions"]], [f"B{i:02}" for i in range(1, 21)])
        self.assertFalse(self.manifest["effective_scope"]["runtime_activation"])
        trade = self.profile.to_record()["settings"]["trade"]
        self.assertEqual(trade["risk_per_trade_ratio"], "0.002")
        self.assertTrue(trade["min_one_lot_fallback"])
        for switch in ("require_trigger_update", "live_protect", "half_close_enabled", "half_stage2", "half_breakeven"):
            self.assertFalse(trade[switch])
        self.manifest["status"] = "REVOKED"
        self.assertEqual(load_confirmed_baseline()[1]["status"], "APPROVED")

    def test_show_and_diff_never_start_a_kernel(self):
        settings = self.profile.to_settings(); settings["trade"]["min_one_lot_fallback"] = False
        candidate = self.root / "changed.json"
        candidate.write_text(json.dumps(settings), encoding="utf-8")
        with patch("online_trader.kernel.engine.Kernel", side_effect=AssertionError("Read-only command started a kernel")):
            result, output, _ = self.invoke("show")
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output)["summary"]["configured_symbol_count"], 4)
            result, output, _ = self.invoke("diff", "--input", candidate)
            self.assertEqual(result, 0)
        delta = json.loads(output)
        self.assertFalse(delta["matches_confirmed_baseline"])
        self.assertEqual(delta["changes"], [{"path": "$.trade.min_one_lot_fallback", "before": True, "after": False}])

    def test_original_dsv_import_exports_only_business_values_and_preserves_source(self):
        source = self.root / "original-config.json"
        exported = self.root / "business.json"
        original = self.profile.to_settings() | {"account": {"password": "PRIVATE-PASSWORD"},
                    "notify": {"dingtalk_secret": "PRIVATE-NOTIFICATION-KEY"}, "ui": {"x": -1920}}
        source.write_text(json.dumps(original), encoding="utf-8")
        before = source.read_bytes()
        result, _, error = self.invoke("export", "--input", source, "--dsv", "--output", exported)
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(source.read_bytes(), before)
        raw = exported.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE-", raw)
        self.assertEqual(set(json.loads(raw)), {"trade", "auto_offset", "schedule", "symbols"})
        self.assertEqual(DsvProfile.from_json(raw).content_hash, self.profile.content_hash)
        result, _, _ = self.invoke("export", "--input", source, "--dsv", "--output", source)
        self.assertEqual(result, 2)
        self.assertEqual(source.read_bytes(), before)

    def test_export_failure_leaves_no_partial_file_or_temporary(self):
        destination = self.root / "profile.json"
        with patch("online_trader.configuration.files.os.link", side_effect=OSError("Injected publication failure")):
            with self.assertRaises(TraderError):export_profile(self.profile, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_show_preserves_large_decimal_digits_without_float_rounding(self):
        raw = self.profile.to_json().replace('"risk_reference_equity":0',
            '"risk_reference_equity":123456789012345678.12345678901')
        source = self.root / "precise.json"; source.write_text(raw, encoding="utf-8")
        result, output, error = self.invoke("show", "--input", source)
        self.assertEqual((result, error), (0, ""))
        self.assertEqual(json.loads(output)["settings_record"]["settings"]["trade"]["risk_reference_equity"],
                         "123456789012345678.12345678901")

    def test_invalid_save_input_does_not_create_a_ledger_or_expose_input_values(self):
        source = self.root / "invalid.json"; source.write_text('{"password":"PRIVATE-PASSWORD"}', encoding="utf-8")
        directory = self.root / "not-created"
        result, output, error = self.invoke("save", "--input", source, "--data-dir", directory,
                                           "--account-key", "DEV:invalid-input", "--expected-revision", "0")
        self.assertEqual(result, 2)
        self.assertFalse(directory.exists())
        self.assertNotIn("PRIVATE-PASSWORD", output + error)

    def test_reference_registration_draft_save_and_history_do_not_activate_execution(self):
        directory = self.root / "catalog"; account = "DEV:parameters-" + uuid.uuid4().hex
        common = ("--data-dir", directory, "--account-key", account)
        result, output, error = self.invoke("register", *common)
        self.assertEqual((result, error), (0, ""))
        baseline_revision = json.loads(output)["config_revision"]
        settings = self.profile.to_settings(); settings["trade"]["max_volume"] = 7
        candidate = self.root / "draft.json"; candidate.write_text(json.dumps(settings), encoding="utf-8")
        result, output, error = self.invoke("save", *common, "--input", candidate,
                                            "--expected-revision", baseline_revision)
        self.assertEqual((result, error), (0, ""))
        receipt = json.loads(output)
        self.assertEqual(receipt["confirmation_status"], "UNCONFIRMED_DRAFT")
        self.assertFalse(receipt["runtime_activation"])
        with Kernel(directory / "ledger.db", account) as kernel:
            before = kernel.ledger.query("SELECT COUNT(*) AS count FROM event_journal")[0]["count"]
            authority = kernel.ledger.query("SELECT config_revision,new_open_status FROM authority")[0]
            with patch("online_trader.kernel.engine.Kernel", side_effect=AssertionError("History created a boot")):
                result, output, error = self.invoke("history", *common)
            self.assertEqual((result, error), (0, ""))
            self.assertEqual(len(json.loads(output)), 2)
            self.assertEqual(kernel.ledger.query("SELECT COUNT(*) AS count FROM event_journal")[0]["count"], before)
            self.assertEqual(authority, {"config_revision": 1, "new_open_status": "DISARMED"})
            self.assertEqual(kernel.configurations.get()["profile_hash"], receipt["profile_hash"])
            self.assertEqual(kernel.ledger.query("SELECT COUNT(*) AS n FROM rule_decisions WHERE rule_set_id=? AND status='APPROVED'",
                                                (self.manifest["rule_set_id"],))[0]["n"], 20)

    def test_missing_history_is_read_only_and_creates_no_database(self):
        directory = self.root / "missing"
        result, _, _ = self.invoke("history", "--data-dir", directory, "--account-key", "DEV:missing")
        self.assertEqual(result, 2)
        self.assertFalse(directory.exists())


if __name__ == "__main__":unittest.main()
