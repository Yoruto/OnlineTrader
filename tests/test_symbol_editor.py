from datetime import date
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.files import load_profile
from online_trader.configuration.symbol_csv import suggest_contracts
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

    def test_contract_dropdown_suggests_up_to_six_months_offline_and_from_catalog(self):
        offline, from_catalog = suggest_contracts('ag', (), limit=6, today=date(2026, 11, 24))
        self.assertFalse(from_catalog)
        self.assertEqual(
            offline,
            ['SHFE.ag2612', 'SHFE.ag2701', 'SHFE.ag2702', 'SHFE.ag2703', 'SHFE.ag2704', 'SHFE.ag2705'],
        )
        dialog = self.dialog()
        dialog.show()
        self.app.processEvents()
        dialog.fields['code'].setFocus()
        QTest.keyClicks(dialog.fields['code'], 'ag')
        self.assertEqual(dialog._catalog_completer.maxVisibleItems(), 6)
        self.assertEqual(len(dialog._catalog_model.stringList()), 6)
        self.assertTrue(all(item.startswith('SHFE.ag') for item in dialog._catalog_model.stringList()))

        catalog = [f'SHFE.ag2612', *(f'SHFE.ag270{m}' for m in range(1, 9)), 'SHFE.cu2701']
        dialog.set_contract_catalog(catalog)
        self.assertEqual(
            dialog._catalog_model.stringList(),
            ['SHFE.ag2612', 'SHFE.ag2701', 'SHFE.ag2702', 'SHFE.ag2703', 'SHFE.ag2704', 'SHFE.ag2705'],
        )

    def test_contract_dropdown_single_click_selects_and_closes(self):
        dialog = self.dialog()
        catalog = ['SHFE.ag2612', 'SHFE.ag2701', 'SHFE.ag2702']
        dialog.set_contract_catalog(catalog)
        dialog.show()
        self.app.processEvents()
        code = dialog.fields['code']
        code.setFocus()
        QTest.keyClicks(code, 'ag')
        self.app.processEvents()

        popup = dialog._catalog_completer.popup()
        self.assertTrue(popup.isVisible())
        index = popup.model().index(1, 0)
        self.assertEqual(index.data(), 'SHFE.ag2701')
        activated = []
        dialog._catalog_completer.activated[str].connect(activated.append)
        QTest.mouseClick(popup.viewport(), Qt.LeftButton, pos=popup.visualRect(index).center())
        self.app.processEvents()

        self.assertEqual(activated, ['SHFE.ag2701'])
        self.assertEqual(code.text(), 'SHFE.ag2701')
        self.assertEqual(dialog.fields['name'].text(), '白银')
        self.assertFalse(popup.isVisible())
        self.assertIsNone(dialog.result_profile)  # Still requires form confirmation.
        dialog.set_contract_catalog(catalog + ['SHFE.ag2703'])
        self.app.processEvents()
        self.assertFalse(popup.isVisible())
        self.assertEqual(code.text(), 'SHFE.ag2701')
        dialog.fields['stop_offset'].setText('12')
        self.assertEqual(code.text(), 'SHFE.ag2701')

    def test_contract_dropdown_keyboard_selection(self):
        dialog = self.dialog()
        dialog.set_contract_catalog(['SHFE.ag2612', 'SHFE.ag2701', 'SHFE.ag2702'])
        dialog.show()
        self.app.processEvents()
        code = dialog.fields['code']
        code.setFocus()
        QTest.keyClicks(code, 'ag')
        self.app.processEvents()
        popup = dialog._catalog_completer.popup()
        self.assertTrue(popup.isVisible())
        QTest.keyClick(popup, Qt.Key_Down)
        index = popup.currentIndex()
        self.assertTrue(index.isValid())
        expected = index.data()
        QTest.keyClick(popup, Qt.Key_Return)
        self.app.processEvents()
        self.assertEqual(code.text(), expected)
        self.assertEqual(dialog.fields['name'].text(), '白银')
        self.assertFalse(popup.isVisible())
        self.assertIsNone(dialog.result_profile)

    def test_complete_contract_input_does_not_keep_popup_open(self):
        dialog = self.dialog()
        dialog.set_contract_catalog(['SHFE.ag2612', 'SHFE.ag2701', 'SHFE.ag2702'])
        dialog.show()
        self.app.processEvents()
        code = dialog.fields['code']
        code.setFocus()
        QTest.keyClicks(code, 'ag2701')
        self.app.processEvents()
        self.assertEqual(code.text(), 'ag2701')
        self.assertEqual(dialog.fields['name'].text(), '白银')
        self.assertFalse(dialog._catalog_completer.popup().isVisible())

    def test_runtime_symbol_limit_enforces_eleven_contracts(self):
        settings = self.profile.to_settings()
        base = settings['symbols'][0]
        settings['symbols'] = [
            dict(base, code=f'SHFE.cu27{month:02d}', name=f'铜{month:02d}')
            for month in range(1, 12)
        ]
        from online_trader.configuration.dsv import DsvProfile
        eleven_profile = DsvProfile.from_settings(settings)
        dialog = SymbolEditorDialog(eleven_profile)
        self.windows.append(dialog)
        dialog.fields['code'].setText('ag2702')
        self.assertFalse(dialog.validate_symbol())
        self.assertIn('11', dialog.error_label.text())


if __name__ == '__main__': unittest.main()
