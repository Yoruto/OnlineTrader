import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QDialog

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.files import load_profile
from online_trader.desktop.editor import ParameterEditorDialog
from online_trader.desktop.settings import SettingsDialog
from online_trader.desktop.symbol_editor import SymbolEditorDialog


class SymbolEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.profile, self.manifest = load_confirmed_baseline()
        self.windows = []

    def tearDown(self):
        for window in reversed(self.windows):
            window.close(); window.deleteLater()
        self.app.processEvents()

    def dialog(self, row=None):
        dialog = SymbolEditorDialog(self.profile, symbol_row=row)
        self.windows.append(dialog)
        return dialog

    def test_add_alias_name_and_parameters_are_applied_atomically(self):
        dialog = self.dialog()
        dialog.fields['code'].setText('铜2701')
        self.assertEqual(dialog.fields['name'].text(), '铜')
        dialog.fields['stop_offset'].setText('12.5')
        dialog.volume_mode.setCurrentIndex(1)
        dialog.volume_input.setText('2')
        dialog.fields['half_close_ratio'].setText('40')
        self.assertTrue(dialog.validate_symbol())
        row = dialog.result_symbol
        self.assertEqual((row['code'], row['name'], row['stop_offset'], row['order_volume'], row['half_close_ratio']),
                         ('SHFE.cu2701', '铜', 12.5, 2, 0.4))
        result = dialog.result_profile.to_settings()
        self.assertEqual(result['symbols'][:-1], self.profile.to_settings()['symbols'])
        for section in ('trade', 'schedule', 'auto_offset'):
            self.assertEqual(result[section], self.profile.to_settings()[section])
        self.assertEqual(len(self.profile.to_settings()['symbols']), 5)

    def test_cancel_add_and_edit_never_publish_partial_changes(self):
        for row in (None, 0):
            dialog = self.dialog(row)
            dialog.fields['code'].setText('cu2701')
            dialog.fields['stop_offset'].setText('123')
            dialog.cancel_button.click()
            self.assertEqual(dialog.result(), QDialog.Rejected)
            self.assertIsNone(dialog.result_profile)
            self.assertIsNone(dialog.result_symbol)

    def test_edit_retains_unedited_values_and_custom_name(self):
        dialog = self.dialog(4)
        dialog.fields['name'].setText('自定义白银')
        dialog.fields['code'].setText('ag2702')
        self.assertEqual(dialog.fields['name'].text(), '自定义白银')
        self.assertTrue(dialog.validate_symbol())
        row = dialog.result_symbol
        self.assertEqual(row['code'], 'SHFE.ag2702')
        self.assertEqual(row['code_new'], 'SHFE.ag2702')
        self.assertEqual(row['half_close_ratio'], 0.4)
        self.assertEqual(dialog.result_profile.to_settings()['symbols'][:4], self.profile.to_settings()['symbols'][:4])

    def test_name_tracks_known_product_until_manually_changed(self):
        dialog = self.dialog()
        dialog.fields['code'].setText('cu2701'); dialog.fields['code'].setText('rb2701')
        self.assertEqual(dialog.fields['name'].text(), '螺纹钢')
        dialog.fields['name'].setText('自定义'); dialog.fields['code'].setText('cu2701')
        self.assertEqual(dialog.fields['name'].text(), '自定义')

    def test_duplicate_and_invalid_values_stay_open_with_inline_errors(self):
        dialog = self.dialog()
        self.assertFalse(dialog.validate_symbol())
        self.assertIn('合约代码', dialog.error_label.text())
        dialog.fields['code'].setText('p2701')
        self.assertFalse(dialog.validate_symbol()); self.assertIn('已在列表', dialog.error_label.text())
        dialog.fields['code'].setText('cu2701'); dialog.fields['stop_offset'].setText('-1')
        self.assertFalse(dialog.validate_symbol()); self.assertIn('止损请填写大于等于 0 的数字。', dialog.error_label.text())
        dialog.fields['stop_offset'].setText('12'); dialog.fields['half_close_ratio'].setText('101')
        self.assertFalse(dialog.validate_symbol()); self.assertTrue(dialog.more_button.isChecked())
        self.assertIn('0～100', dialog.error_label.text())
        self.assertIsNone(dialog.result_profile)

    def test_fixed_quantity_needs_positive_integer_and_auto_preserves_zero(self):
        for invalid in ('0', '自动', '1.5', '-1', '2147483648'):
            dialog = self.dialog(); dialog.fields['code'].setText('cu2701')
            dialog.volume_mode.setCurrentIndex(1); dialog.volume_input.setText(invalid)
            self.assertFalse(dialog.validate_symbol())
            self.assertIn('固定手数', dialog.error_label.text())
        dialog.volume_mode.setCurrentIndex(0)
        self.assertTrue(dialog.validate_symbol()); self.assertEqual(dialog.result_symbol['order_volume'], 0)

    def test_footer_is_inside_small_dialog_with_advanced_fields_and_error(self):
        dialog = self.dialog(); dialog.show(); dialog.resize(480, 360)
        dialog.more_button.setChecked(True); dialog.fields['code'].setText('cu2701')
        dialog.fields['half_close_ratio'].setText('101'); dialog.validate_symbol()
        self.app.processEvents()
        for button in (dialog.confirm_button, dialog.cancel_button):
            self.assertTrue(button.isVisible())
            top_left = button.mapTo(dialog, QPoint(0, 0))
            self.assertTrue(dialog.rect().contains(top_left))
            self.assertTrue(dialog.rect().contains(top_left + QPoint(button.width() - 1, button.height() - 1)))

    def test_main_entry_add_edit_cancel_and_saved_file_round_trip(self):
        settings = SettingsDialog(baseline_loader=lambda: (self.profile, self.manifest)); self.windows.append(settings)
        def add(dialog):
            dialog.fields['code'].setText('cu2701'); dialog.validate_symbol(); return dialog.result()
        with patch.object(SymbolEditorDialog, 'exec', add): self.assertTrue(settings.edit_preview(add_symbol=True))
        def edit(dialog):
            dialog.fields['stop_offset'].setText('33'); dialog.validate_symbol(); return dialog.result()
        with patch.object(SymbolEditorDialog, 'exec', edit): self.assertTrue(settings.edit_preview(symbol_row=5))
        before = settings.profile.content_hash
        with patch.object(SymbolEditorDialog, 'exec', return_value=QDialog.Rejected):
            self.assertFalse(settings.edit_preview(symbol_row=5))
        self.assertEqual(settings.profile.content_hash, before)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'saved.json'
            self.assertTrue(settings.export_file(path))
            self.assertEqual(load_profile(path).content_hash, before)
            self.assertEqual(load_profile(path).to_settings()['symbols'][-1]['stop_offset'], 33)

    def test_advanced_editor_new_form_cancel_and_accept_preserve_other_drafts(self):
        editor = ParameterEditorDialog(self.profile); self.windows.append(editor)
        editor.fields['trade.max_volume'].setText('12')
        with patch.object(SymbolEditorDialog, 'exec', return_value=QDialog.Rejected): self.assertFalse(editor.add_symbol())
        self.assertEqual(editor.symbols_table.rowCount(), 5)
        def add(dialog):
            dialog.fields['code'].setText('cu2701'); dialog.validate_symbol(); return dialog.result()
        with patch.object(SymbolEditorDialog, 'exec', add): self.assertTrue(editor.add_symbol())
        self.assertEqual(editor.fields['trade.max_volume'].text(), '12')
        self.assertTrue(editor.validate_preview())
        self.assertEqual(editor.result_profile.to_settings()['trade']['max_volume'], 12)
        self.assertEqual(len(editor.result_profile.to_settings()['symbols']), 6)


if __name__ == '__main__': unittest.main()
