from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from online_trader.cli import main
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import export_profile
from online_trader.configuration.symbol_csv import HEADERS, export_symbols
from online_trader.configuration.templates import TemplateStore
from online_trader.desktop.settings import SettingsDialog
from tests.test_symbol_csv import ROW, csv_bytes


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.profile, self.manifest = load_confirmed_baseline()
        self.store = self.root / "templates"

    def tearDown(self): self.temp.cleanup()

    def altered(self, **values):
        record = self.profile.to_record(); record["settings"]["trade"].update(values)
        return DsvProfile.from_record(record)


class ParameterWorkspaceCliTests(WorkspaceCase):
    def invoke(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err): result = main(["parameters", *map(str, args)])
        return result, out.getvalue(), err.getvalue()

    def good(self, *args):
        result, out, err = self.invoke(*args)
        self.assertEqual((result, err), (0, ""))
        value = json.loads(out); self.assertFalse(value["runtime_activation"])
        return value

    def test_template_cli_list_save_apply_overwrite_delete_and_reload(self):
        self.assertEqual(self.good("template-list", "--store", self.store)["revision"], 0)
        self.assertFalse(self.store.exists())
        first = self.good("template-save", "--store", self.store, "--name", "默认", "--expected-revision", 0)
        self.assertEqual(first["names"], ["默认"])
        changed = self.root / "changed.json"; export_profile(self.altered(max_volume=7), changed)
        second = self.good("template-save", "--store", self.store, "--name", "默认", "--expected-revision", 1, "--overwrite", "--input", changed)
        self.assertEqual(second["revision"], 2)
        target = self.root / "applied.json"
        applied = self.good("template-apply", "--store", self.store, "--name", "默认", "--output", target)
        self.assertEqual(applied["template_revision"], 2)
        self.assertEqual(DsvProfile.from_json(target.read_bytes()).to_record()["settings"]["trade"]["max_volume"], 7)
        self.assertEqual(self.good("template-delete", "--store", self.store, "--name", "默认", "--expected-revision", 2)["names"], [])
        self.assertEqual(self.good("template-list", "--store", self.store)["revision"], 3)

    def test_stale_cli_write_returns_conflict_without_new_revision(self):
        self.good("template-save", "--store", self.store, "--name", "甲", "--expected-revision", 0)
        result, out, err = self.invoke("template-delete", "--store", self.store, "--name", "甲", "--expected-revision", 0)
        self.assertEqual(result, 2); self.assertIn("E_TEMPLATE_CONFLICT", err)
        self.assertEqual(out, ""); self.assertEqual(TemplateStore(self.store).read().revision, 1)

    def test_csv_cli_export_import_preview_and_business_file(self):
        csv = self.root / "symbols.csv"
        self.good("symbols-export", "--output", csv)
        preview = self.good("symbols-import", "--csv", csv)
        self.assertEqual(preview["profile_hash"], self.profile.content_hash)
        target = self.root / "preview.json"
        self.good("symbols-import", "--csv", csv, "--output", target)
        self.assertEqual(DsvProfile.from_json(target.read_bytes()).content_hash, self.profile.content_hash)

    def test_csv_cli_bad_later_row_keeps_existing_output_and_redacts_input(self):
        source = self.root / "bad.csv"; row = ROW.copy(); row[3] = "PRIVATE-BAD-CELL"
        source.write_bytes(csv_bytes([ROW, row]))
        target = self.root / "keep.json"; target.write_bytes(b"preserve")
        result, out, err = self.invoke("symbols-import", "--csv", source, "--output", target)
        self.assertEqual(result, 2); self.assertIn("CSV line 3", err)
        self.assertNotIn("PRIVATE-BAD-CELL", err + out)
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_cli_all_workspace_operations_never_construct_kernel_or_account_lock(self):
        source = self.root / "symbols.csv"; source.write_bytes(csv_bytes([ROW]))
        with patch("online_trader.kernel.engine.Kernel", side_effect=AssertionError("No kernel")):
            self.good("template-save", "--store", self.store, "--name", "默认", "--expected-revision", 0)
            self.good("template-list", "--store", self.store)
            self.good("template-apply", "--store", self.store, "--name", "默认")
            self.good("symbols-import", "--csv", source, "--mode", "merge")
            self.good("symbols-export", "--output", self.root / "new.csv")
            self.good("template-delete", "--store", self.store, "--name", "默认", "--expected-revision", 1)
        self.assertEqual(list(self.root.rglob("*.db")), [])

    def test_template_cli_strips_dsv_private_sections_before_store(self):
        source = self.root / "dsv.json"
        source.write_text(self.profile.to_json()[:-1] + ',"account":{"password":"PRIVATE-SECRET"},"notify":{"dingtalk_secret":"PRIVATE-KEY"}}', encoding="utf-8")
        self.good("template-save", "--store", self.store, "--name", "默认", "--expected-revision", 0, "--input", source, "--dsv")
        raw = (self.store / "00000001.json").read_text("utf-8")
        self.assertNotIn("PRIVATE", raw)

    def test_template_apply_export_conflict_never_overwrites(self):
        self.good("template-save", "--store", self.store, "--name", "默认", "--expected-revision", 0)
        target = self.root / "keep.json"; target.write_bytes(b"preserve")
        result, _, error = self.invoke("template-apply", "--store", self.store, "--name", "默认", "--output", target)
        self.assertEqual(result, 2); self.assertIn("E_DUPLICATE_CONFLICT", error)
        self.assertEqual(target.read_bytes(), b"preserve")


class ParameterWorkspaceDesktopTests(WorkspaceCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.dialog = SettingsDialog(baseline_loader=lambda: (self.profile, self.manifest))

    def tearDown(self):
        self.dialog.close(); self.dialog.deleteLater(); self.app.processEvents()
        super().tearDown()

    def test_initial_template_controls_disabled_and_no_directory_created(self):
        self.assertFalse(self.dialog.template_save_button.isEnabled())
        self.assertFalse(self.dialog.template_apply_button.isEnabled())
        self.assertTrue(self.dialog.symbol_import_button.isEnabled())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_choose_empty_store_is_readonly_save_apply_and_delete_preserve_preview(self):
        self.assertTrue(self.dialog.set_template_directory(self.store))
        self.assertFalse(self.store.exists())
        self.assertTrue(self.dialog.template_save_button.isEnabled())
        self.assertTrue(self.dialog.save_template("默认"))
        self.assertTrue(self.dialog.template_apply_button.isEnabled())
        changed = self.altered(max_volume=7)
        self.assertTrue(self.dialog.import_bytes(changed.to_json()))
        self.assertTrue(self.dialog.apply_template("默认"))
        self.assertEqual(self.dialog.profile.content_hash, self.profile.content_hash)
        self.assertTrue(self.dialog.delete_template("默认"))
        self.assertEqual(self.dialog.profile.content_hash, self.profile.content_hash)
        self.assertIn("历史版本保留", self.dialog.notice_label.text())

    def test_csv_buttons_route_whole_batch_and_export_candidate_column(self):
        source = self.root / "in.csv"; source.write_bytes(csv_bytes([ROW]))
        target = self.root / "out.csv"
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.dialog.symbol_import_button.click()
        self.assertEqual(self.dialog.symbols_table.rowCount(), 1)
        self.assertEqual(self.dialog.symbols_table.item(0, 8).text(), "SHFE.ag2702")
        self.assertIn("运行参数与持仓计划未改变", self.dialog.notice_label.text())
        with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            self.dialog.symbol_export_button.click()
        self.assertIn("候选换月合约", target.read_text("utf-8-sig"))

    def test_bad_csv_keeps_previous_profile_table_and_redacts_value(self):
        self.assertTrue(self.dialog.import_symbols_bytes(csv_bytes([ROW])))
        digest = self.dialog.profile.content_hash
        row = ROW.copy(); row[1] = "cu2701"; row[4] = "PRIVATE-CELL"
        self.assertFalse(self.dialog.import_symbols_bytes(csv_bytes([ROW, row])))
        self.assertEqual(self.dialog.profile.content_hash, digest)
        self.assertEqual(self.dialog.symbols_table.rowCount(), 1)
        self.assertIn("第3行", self.dialog.error_label.text())
        self.assertNotIn("PRIVATE-CELL", self.dialog.error_label.text())

    def test_csv_merge_selection_preserves_existing_symbols(self):
        source = self.root / "in.csv"; source.write_bytes(csv_bytes([["铜", "cu2701", "1", "1", "1", "1", "0.5", "0", ""]]))
        self.dialog.symbol_mode.setCurrentIndex(1)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.dialog.symbol_import_button.click()
        self.assertEqual(self.dialog.symbols_table.rowCount(), 6)

    def test_stale_selected_template_apply_save_and_delete_require_reload(self):
        self.dialog.set_template_directory(self.store); self.dialog.save_template("默认")
        TemplateStore(self.store).save("默认", self.altered(max_volume=9), expected_revision=1, overwrite=True)
        for operation in (lambda: self.dialog.apply_template("默认"), lambda: self.dialog.save_template("甲"), lambda: self.dialog.delete_template("默认")):
            self.assertFalse(operation()); self.assertIn("E_TEMPLATE_CONFLICT", self.dialog.error_label.text())
        self.assertEqual(self.dialog.profile.content_hash, self.profile.content_hash)
        self.assertTrue(self.dialog.reload_templates())
        self.assertTrue(self.dialog.apply_template("默认"))
        self.assertEqual(self.dialog.profile.to_record()["settings"]["trade"]["max_volume"], 9)

    def test_template_overwrite_and_delete_buttons_require_explicit_choice(self):
        self.dialog.set_template_directory(self.store); self.dialog.save_template("默认")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.dialog.template_overwrite_button.click(); self.dialog.template_delete_button.click()
        self.assertEqual(TemplateStore(self.store).read().revision, 1)
        self.dialog.import_bytes(self.altered(max_volume=6).to_json())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.dialog.template_overwrite_button.click()
        self.assertEqual(TemplateStore(self.store).read().templates["默认"]["max_volume"], 6)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes): self.dialog.template_delete_button.click()
        self.assertEqual(TemplateStore(self.store).read().templates, {})

    def test_corrupt_new_directory_selection_keeps_previous_store(self):
        self.dialog.set_template_directory(self.store); self.dialog.save_template("默认")
        bad = self.root / "bad"; bad.mkdir(); (bad / "00000001.json").write_bytes(b"bad")
        self.assertFalse(self.dialog.set_template_directory(bad))
        self.assertIn("默认", self.dialog.template_combo.currentText())
        self.assertTrue(self.dialog.apply_template("默认"))

    def test_failed_export_and_missing_import_do_not_clear_preview(self):
        self.dialog.import_symbols_bytes(csv_bytes([ROW])); digest = self.dialog.profile.content_hash
        self.assertFalse(self.dialog.import_symbols_file(self.root / "missing.csv"))
        target = self.root / "exists.csv"; target.write_bytes(b"preserve")
        self.assertFalse(self.dialog.export_symbols_file(target))
        self.assertEqual(self.dialog.profile.content_hash, digest)
        self.assertEqual(target.read_bytes(), b"preserve")

    def test_cancel_all_file_dialogs_leaves_state_and_disk_unchanged(self):
        with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")), patch.object(QFileDialog, "getSaveFileName", return_value=("", "")), patch.object(QFileDialog, "getExistingDirectory", return_value=""):
            self.dialog.symbol_import_button.click(); self.dialog.symbol_export_button.click(); self.dialog.template_directory_button.click()
        self.assertEqual(self.dialog.profile.content_hash, self.profile.content_hash)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_template_and_csv_actions_never_create_kernel_or_ipc(self):
        with patch("online_trader.kernel.engine.Kernel", side_effect=AssertionError("No kernel")), patch("online_trader.desktop.client.StatusClient", side_effect=AssertionError("No IPC")):
            self.dialog.set_template_directory(self.store); self.dialog.save_template("默认")
            self.dialog.import_symbols_bytes(csv_bytes([ROW])); self.dialog.apply_template("默认")
            self.assertEqual(self.dialog.symbols_table.rowCount(), 1)
            self.dialog.export_symbols_file(self.root / "out.csv"); self.dialog.delete_template("默认")
        self.assertEqual(list(self.root.rglob("*.db")), [])

    def test_open_owned_cycle_keeps_committed_plan_authority_and_orders_during_preview_changes(self):
        import copy
        from online_trader.kernel import Kernel
        from online_trader.strategy.plan import build_development_plan
        from qa.recording_port import RecordingPort
        port = RecordingPort(); account = "DEV:parameter-isolation-" + uuid.uuid4().hex
        with Kernel(self.root / "ledger.db", account, development=True, port=port,
                    risk_budget="1000000", tick_size="1", multiplier="10") as kernel:
            candidate = {"account_key": account, "contract_id": "DCE.p2701", "trading_day": "2026-11-10",
                "profile_hash": self.profile.content_hash, "direction": "LONG", "theoretical_price": "1000",
                "limit_price": "1000", "open_offset": "55", "stop_distance": "16", "take_profit_distance": "50"}
            plan = build_development_plan(self.profile, candidate, account_key=account, trading_day="2026-11-10",
                config_revision=str(kernel._config_revision), config_hash=kernel._configuration["payload_hash"],
                rule_set_id=kernel._configuration["rule_set_id"], quantity=3, plan_id="parameter-plan",
                position_cycle_id="parameter-cycle", execution_version="1.2")
            kernel.arm_development(); kernel.wait_idle()
            context = kernel.issue_entry_context("qa", plan["contract_id"])
            child = kernel.admit_entry(plan, "parameter-decision", "1000", context_id=context["context_id"])
            kernel.wait_idle(); kernel.observe(plan["position_cycle_id"], "1000", bid1="999", ask1="1000"); kernel.wait_idle()
            kernel.record_fill(child, "parameter-fill", 3, "1000"); kernel.wait_idle()
            before = copy.deepcopy(kernel._committed)
            authority = kernel.ledger.query("SELECT * FROM authority")
            sequence, orders = kernel._durable_seq, copy.deepcopy(port.orders)
            self.dialog.set_template_directory(self.store)
            self.dialog.import_bytes(self.altered(max_volume=1, half_close_enabled=True).to_json())
            self.dialog.save_template("不同数量")
            self.dialog.import_symbols_bytes(csv_bytes([ROW]))
            self.dialog.apply_template("不同数量")
            self.dialog.reset_to_baseline(); self.dialog.delete_template("不同数量")
            from online_trader.configuration.editor import form_values, profile_from_form
            from online_trader.configuration.offsets import DailyDataset, calculate_offsets
            values = form_values(self.dialog.profile); values["trade"]["half_stage2"] = True
            edited = profile_from_form(self.dialog.profile, values)
            self.assertTrue(self.dialog.apply_edited_preview(edited, expected_hash=self.dialog.profile.content_hash))
            raw = "series_id,date,high,low,close\nDCE.p2701,2026-11-01,1200,1000,1100\nDCE.p2701,2026-11-02,1200,1000,1100\nDCE.p2701,2026-11-03,1200,1000,1100\n"
            calculated = calculate_offsets(edited, DailyDataset.from_csv(raw), as_of_date="2026-11-10").apply(edited, ["DCE.p2701"])
            self.assertTrue(self.dialog.apply_edited_preview(calculated, expected_hash=edited.content_hash))
            self.assertEqual(kernel._committed, before)
            self.assertEqual(kernel.ledger.query("SELECT * FROM authority"), authority)
            self.assertEqual(kernel._durable_seq, sequence)
            self.assertEqual(port.orders, orders)
            self.assertEqual(kernel._committed["parameter-cycle"]["protection"]["owned_qty"], 3)
