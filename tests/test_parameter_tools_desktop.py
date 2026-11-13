import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QItemSelectionModel
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QFileDialog, QTableWidgetItem

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.editor import form_values, profile_from_form
from online_trader.configuration.offsets import DailyDataset
from online_trader.desktop.settings import SettingsDialog
from online_trader.desktop.editor import ParameterEditorDialog
from online_trader.desktop.symbol_editor import SymbolEditorDialog
from online_trader.desktop.calculator import OffsetCalculatorDialog
from tests.test_offset_calculator import daily_csv


class ParameterToolsDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.profile, self.manifest = load_confirmed_baseline()
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.windows = []

    def tearDown(self):
        for window in reversed(self.windows): window.close(); window.deleteLater()
        self.app.processEvents(); self.temp.cleanup()

    def editor(self):
        dialog = ParameterEditorDialog(self.profile); self.windows.append(dialog); return dialog

    def calculator(self, *, load=True):
        dialog = OffsetCalculatorDialog(self.profile); self.windows.append(dialog)
        if load:
            source = self.root / "daily.csv"; source.write_bytes(daily_csv("DCE.p2701", [100] * 30).encode("utf-8"))
            self.assertTrue(dialog.load_file(source))
        dialog.date_input.setText("2026-11-10")
        return dialog

    def settings(self):
        dialog = SettingsDialog(baseline_loader=lambda: (self.profile, self.manifest)); self.windows.append(dialog); return dialog

    def test_editor_contains_every_non_symbol_field_and_nine_symbol_columns(self):
        dialog = self.editor()
        self.assertEqual(len(dialog.fields), 77)
        self.assertEqual(dialog.symbols_table.columnCount(), 9)
        self.assertTrue(dialog.validate_preview())
        self.assertEqual(dialog.result_profile.content_hash, self.profile.content_hash)

    def test_editor_typed_controls_edit_trade_schedule_coefficients_and_symbols(self):
        dialog = self.editor()
        dialog.fields["trade.max_volume"].setText("12")
        dialog.fields["trade.half_stage2"].setChecked(True)
        dialog.fields["schedule.day_start_time"].setText("09:15")
        dialog.fields["auto_offset.coef"].setPlainText('{"棕榈油":"0.5"}')
        dialog.symbols_table.item(4, 2).setText("10.5")
        self.assertTrue(dialog.validate_preview())
        settings = dialog.result_profile.to_record()["settings"]
        self.assertEqual(settings["trade"]["max_volume"], 12); self.assertTrue(settings["trade"]["half_stage2"])
        self.assertEqual(settings["schedule"]["day_start_time"], "09:15")
        self.assertEqual(settings["symbols"][4]["open_offset"], "10.5")

    def test_editor_invalid_apply_retains_form_without_parent_mutation_or_raw_echo(self):
        dialog = self.editor(); dialog.fields["trade.risk_per_trade_ratio"].setText("PRIVATE")
        self.assertFalse(dialog.validate_preview())
        self.assertIsNone(dialog.result_profile)
        self.assertEqual(dialog.fields["trade.risk_per_trade_ratio"].text(), "PRIVATE")
        self.assertNotIn("PRIVATE", dialog.error_label.text())
        self.assertEqual(dialog.base_profile.content_hash, self.profile.content_hash)

    def test_editor_cancel_discards_all_edits(self):
        dialog = self.editor(); dialog.fields["trade.max_volume"].setText("8")
        dialog.cancel_button.click()
        self.assertEqual(dialog.result(), QDialog.Rejected); self.assertIsNone(dialog.result_profile)

    def test_editor_search_hides_only_form_rows_and_does_not_omit_values(self):
        dialog = self.editor(); before = dialog.values()
        dialog.search.setText("策略数量上限")
        self.assertEqual(dialog.values(), before)
        self.assertTrue(dialog.validate_preview())
        self.assertEqual(dialog.result_profile.content_hash, self.profile.content_hash)

    def test_editor_add_and_remove_row_then_validate(self):
        dialog = self.editor()
        def accept(symbol):
            symbol.fields['code'].setText('cu2701')
            self.assertTrue(symbol.validate_symbol())
            return QDialog.Accepted
        with patch.object(SymbolEditorDialog, 'exec', accept): self.assertTrue(dialog.add_symbol())
        row = dialog.symbols_table.rowCount() - 1
        self.assertTrue(dialog.validate_preview())
        self.assertEqual(len(dialog.result_profile.to_record()["settings"]["symbols"]), 6)
        dialog.symbols_table.selectRow(row); dialog.remove_selected()
        self.assertEqual(dialog.symbols_table.rowCount(), 5)

    def test_editor_batch_is_atomic_and_requires_selection(self):
        dialog = self.editor(); self.assertFalse(dialog.batch_selected())
        dialog.symbols_table.selectRow(0)
        dialog.batch_value.setText("-1")
        self.assertFalse(dialog.batch_selected()); self.assertEqual(dialog.symbols_table.item(0, 2).text(), "55")
        dialog.batch_value.setText("99.5")
        self.assertTrue(dialog.batch_selected()); self.assertEqual(dialog.symbols_table.item(0, 2).text(), "99.5")
        self.assertEqual(dialog.symbols_table.item(1, 2).text(), "79")
        self.assertEqual(dialog.base_profile.content_hash, self.profile.content_hash)

    def test_editor_bad_late_symbol_focuses_row_without_partial_accept(self):
        dialog = self.editor(); dialog.symbols_table.item(4, 3).setText("bad")
        self.assertFalse(dialog.validate_preview())
        self.assertEqual(dialog.tabs.currentIndex(), 3)
        self.assertIn("第5行", dialog.error_label.text())
        self.assertIsNone(dialog.result_profile)

    def test_calculator_needs_loaded_data_explicit_date_and_successful_selection(self):
        dialog = self.calculator(load=False); self.assertFalse(dialog.calculate())
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog = self.calculator(); dialog.date_input.clear(); self.assertFalse(dialog.calculate())
        dialog.date_input.setText("2026-11-10"); self.assertTrue(dialog.calculate())
        self.assertEqual(dialog.selected_contracts(), []); self.assertFalse(dialog.apply_selected())
        dialog.select_successful(); self.assertEqual(dialog.selected_contracts(), ["DCE.p2701"])
        self.assertTrue(dialog.apply_selected())
        self.assertEqual(dialog.result_profile.to_record()["settings"]["symbols"][0]["open_offset"], "45")

    def test_calculator_shows_current_computed_and_missing_source_rows(self):
        dialog = self.calculator(); dialog.calculate()
        self.assertEqual(dialog.table.item(0, 4).text(), "55/16/50")
        self.assertEqual(dialog.table.item(0, 5).text(), "45/16/40")
        self.assertIn("没有匹配", dialog.table.item(1, 6).text())
        self.assertFalse(bool(dialog.table.item(1, 0).flags() & Qt.ItemIsUserCheckable))

    def test_calculator_manual_amplitude_recalculates_with_dsv_floor(self):
        dialog = self.calculator(); dialog.table.item(0, 2).setText("1")
        self.assertTrue(dialog.calculate())
        self.assertEqual(dialog.table.item(0, 5).text(), "18/16/40")
        self.assertIn("振幅约束已计算", dialog.table.item(0, 6).text())

    def test_changed_formula_ratios_date_or_amplitude_invalidates_old_result(self):
        for change in (lambda d: d.ceiling_input.setText("0.4"), lambda d: d.floor_input.setText("0.3"),
                       lambda d: d.date_input.setText("2026-11-09"), lambda d: d.formula_combo.setCurrentIndex(2),
                       lambda d: d.table.item(0, 2).setText("10")):
            dialog = self.calculator(); self.assertTrue(dialog.calculate()); dialog.select_successful()
            change(dialog)
            self.assertIsNone(dialog.calculation); self.assertFalse(dialog.apply_button.isEnabled())
            self.assertFalse(dialog.copy_text()); self.assertFalse(dialog.apply_selected())

    def test_cached_recalculation_ignores_source_file_rewrite_until_reload(self):
        dialog = self.calculator(); dialog.calculate(); before = dialog.calculation.content_hash
        (self.root / "daily.csv").write_bytes(b"bad")
        self.assertTrue(dialog.calculate()); self.assertEqual(dialog.calculation.content_hash, before)
        self.assertFalse(dialog.load_file(self.root / "daily.csv"))
        self.assertEqual(dialog.calculation.content_hash, before)
        self.assertIn("原数据缓存保留", dialog.error_label.text())

    def test_valid_new_file_invalidates_old_selection_and_result(self):
        dialog = self.calculator(); dialog.calculate(); dialog.select_successful()
        path = self.root / "new.csv"; path.write_bytes(daily_csv("DCE.p2701", [200] * 3).encode())
        self.assertTrue(dialog.load_file(path)); self.assertIsNone(dialog.calculation)
        self.assertFalse(dialog.apply_button.isEnabled())

    def test_copy_and_exclusive_report_export_do_not_apply_offsets(self):
        dialog = self.calculator(); dialog.calculate()
        self.assertTrue(dialog.copy_text()); self.assertIn("45/16/40", QApplication.clipboard().text())
        target = self.root / "report.json"; self.assertTrue(dialog.export_file(target)); before = target.read_bytes()
        self.assertFalse(dialog.export_file(target)); self.assertEqual(target.read_bytes(), before)
        self.assertIsNone(dialog.result_profile)

    def test_cancel_calculator_never_changes_parent_preview(self):
        dialog = self.calculator(); dialog.calculate(); dialog.select_successful(); dialog.cancel_button.click()
        self.assertIsNone(dialog.result_profile); self.assertEqual(dialog.base_profile.content_hash, self.profile.content_hash)

    def test_bad_numeric_amplitude_keeps_cache_but_disables_result(self):
        dialog = self.calculator(); dialog.table.item(0, 2).setText("PRIVATE-CELL")
        self.assertFalse(dialog.calculate()); self.assertIsNotNone(dialog.dataset)
        self.assertNotIn("PRIVATE-CELL", dialog.error_label.text()); self.assertFalse(dialog.apply_button.isEnabled())

    def test_main_editor_button_routes_accepted_result_and_cancel_keeps_previous(self):
        settings = self.settings()
        values = form_values(self.profile); values["trade"]["max_volume"] = "12"
        candidate = profile_from_form(self.profile, values)
        def accept(dialog): dialog.result_profile = candidate; return QDialog.Accepted
        with patch.object(ParameterEditorDialog, "exec", accept): settings.edit_button.click()
        self.assertEqual(settings.profile.content_hash, candidate.content_hash)
        with patch.object(ParameterEditorDialog, "exec", return_value=QDialog.Rejected): settings.edit_button.click()
        self.assertEqual(settings.profile.content_hash, candidate.content_hash)
        self.assertIn("运行参数与持仓计划未改变", settings.notice_label.text())

    def test_main_calculator_button_routes_result_without_owner_or_ipc(self):
        settings = self.settings()
        values = form_values(self.profile); values["symbols"][0]["open_offset"] = "90"
        candidate = profile_from_form(self.profile, values)
        def accept(dialog): dialog.result_profile = candidate; return QDialog.Accepted
        with patch("online_trader.kernel.engine.Kernel", side_effect=AssertionError("No owner")), patch.object(OffsetCalculatorDialog, "exec", accept):
            settings.calculator_button.click()
        self.assertEqual(settings.profile.content_hash, candidate.content_hash)
        self.assertEqual(list(self.root.rglob("*.db")), [])

    def test_main_rejects_stale_editor_or_calculator_result(self):
        settings = self.settings()
        values = form_values(self.profile); values["trade"]["max_volume"] = "12"
        candidate = profile_from_form(self.profile, values)
        settings.apply_edited_preview(candidate, expected_hash=self.profile.content_hash)
        self.assertFalse(settings.apply_edited_preview(self.profile, expected_hash=self.profile.content_hash))
        self.assertEqual(settings.profile.content_hash, candidate.content_hash)

    def test_calculator_cancel_load_and_export_dialogs_write_nothing(self):
        dialog = self.calculator(load=False)
        with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")), patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
            dialog.load_button.click(); dialog.export_button.click()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_calculator_coefficient_edit_invalidates_and_preserves_original_parent_hash(self):
        dialog = self.calculator(); dialog.calculate(); dialog.select_successful()
        self.assertTrue(dialog.set_coefficients('{"棕榈油":"0.6"}'))
        self.assertIsNone(dialog.calculation); self.assertEqual(dialog.base_hash, self.profile.content_hash)
        self.assertTrue(dialog.calculate()); self.assertEqual(dialog.table.item(0, 3).text(), "0.6")
        dialog.select_successful(); self.assertTrue(dialog.apply_selected())
        self.assertEqual(dialog.result_profile.to_record()["settings"]["auto_offset"]["coef"], {"棕榈油": "0.6"})
        self.assertEqual(dialog.result_profile.to_record()["settings"]["symbols"][0]["open_offset"], "60")

    def test_invalid_coefficient_edit_keeps_old_input_and_calculation(self):
        dialog = self.calculator(); dialog.calculate(); before = dialog.calculation.content_hash
        self.assertFalse(dialog.set_coefficients('{"棕榈油":true}'))
        self.assertEqual(dialog.calculation.content_hash, before)
        self.assertEqual(dialog.base_profile.content_hash, self.profile.content_hash)

    def test_batch_edits_two_selected_rows_and_keeps_other_rows(self):
        dialog = self.editor()
        selection = dialog.symbols_table.selectionModel()
        for row in (0, 1): selection.select(dialog.symbols_table.model().index(row, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
        dialog.batch_value.setText("100")
        self.assertTrue(dialog.batch_selected())
        self.assertEqual([dialog.symbols_table.item(row, 2).text() for row in range(3)], ["100", "100", "79"])
        self.assertTrue(dialog.validate_preview())

    def test_section_titles_survive_formula_and_price_mode_choices(self):
        dialog = self.editor()
        self.assertEqual([dialog.tabs.tabText(i) for i in range(4)], ["交易与风控", "偏移计算设置", "定时设置", "品种与批量编辑"])
        self.assertEqual(dialog.fields["auto_offset.formula"].count(), 6)
        self.assertEqual(dialog.fields["trade.close_price_mode"].count(), 3)
