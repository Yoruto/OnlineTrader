from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidget

from online_trader.configuration.dsv import DsvProfile
from online_trader.desktop.app import StatusWindow
from online_trader.desktop.settings import SettingsDialog
from online_trader.errors import TraderError


ROOT = Path(__file__).parents[1]
SETTINGS = json.loads((ROOT / "docs/references/dsv-settings-20261109.json").read_text(encoding="utf-8"))["settings"]


class SettingsDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.profile = DsvProfile.from_settings(copy.deepcopy(SETTINGS))
        self.manifest = {"record_id": "test-confirmation", "rule_set_id": "DSV-approved-reference",
                         "status": "APPROVED", "profile_hash": self.profile.content_hash}
        self.dialog = SettingsDialog(baseline_loader=lambda: (self.profile, self.manifest))

    def tearDown(self):
        self.dialog.close(); self.dialog.deleteLater(); self.app.processEvents()

    def parameter(self, path, column=2):
        table = self.dialog.parameters_table
        for row in range(table.rowCount()):
            if table.item(row, 0).toolTip() == path:
                return table.item(row, column).text()
        self.fail("Missing parameter " + path)

    def test_baseline_preserves_disabled_guard_values_zero_and_silver_diagnostic(self):
        self.assertIn("已确认DSV基准", self.dialog.profile_label.text())
        self.assertIn("实盘保护：关闭", self.dialog.guard_label.text())
        self.assertIn("2手 / 日亏5000元（未启用）", self.dialog.guard_label.text())
        self.assertIn("保底一手：开启", self.dialog.entry_label.text())
        self.assertIn("触发价更新：关闭", self.dialog.entry_label.text())
        self.assertEqual(self.parameter("trade.risk_per_trade_ratio"), "0.002")
        self.assertEqual(self.parameter("trade.max_quote_age_seconds"), "0")
        self.assertEqual(self.parameter("trade.hysteresis_points"), "0")
        self.assertEqual(self.parameter("trade.max_symbols_enabled"), "关闭")
        self.assertEqual(self.dialog.symbols_table.rowCount(), 5)
        self.assertEqual(self.dialog.symbols_table.item(4, 1).text(), "SHFE.ag2612")
        self.assertEqual(self.dialog.symbols_table.item(4, 2).text(), "0")
        self.assertEqual(self.dialog.symbols_table.item(4, 8).text(), "SHFE.ag2702")
        self.assertIn("待核参数", self.dialog.symbols_table.item(4, 9).text())
        self.assertEqual(self.dialog.parameters_table.editTriggers(), QTableWidget.NoEditTriggers)
        self.assertEqual(self.dialog.differences_table.rowCount(), 0)

    def test_import_is_only_preview_and_tracks_switch_and_numeric_differences(self):
        changed = copy.deepcopy(SETTINGS)
        changed["trade"].update(max_volume=31, require_trigger_update=True)
        before = self.profile.to_record()
        self.assertTrue(self.dialog.import_bytes(json.dumps(changed), source_kind="settings"))
        self.assertIn("有差异", self.dialog.profile_label.text())
        self.assertEqual(self.parameter("trade.max_volume"), "31")
        self.assertEqual(self.parameter("trade.require_trigger_update"), "开启")
        self.assertEqual(self.dialog.differences_table.rowCount(), 2)
        paths = {self.dialog.differences_table.item(row, 0).toolTip() for row in range(2)}
        self.assertEqual(paths, {"trade.max_volume", "trade.require_trigger_update"})
        self.assertEqual(self.profile.to_record(), before)
        self.assertIn("运行参数与持仓计划未改变", self.dialog.notice_label.text())
        self.dialog.reset_to_baseline()
        self.assertEqual(self.dialog.profile.content_hash, self.profile.content_hash)
        self.assertEqual(self.dialog.differences_table.rowCount(), 0)

    def test_invalid_import_retains_previous_preview_without_echoing_input(self):
        changed = copy.deepcopy(SETTINGS); changed["trade"]["max_volume"] = 32
        self.assertTrue(self.dialog.import_bytes(json.dumps(changed)))
        digest = self.dialog.profile.content_hash
        invalid = copy.deepcopy(changed); invalid["trade"]["max_volume"] = "PRIVATE-INVALID-TEXT"
        self.assertFalse(self.dialog.import_bytes(json.dumps(invalid)))
        self.assertEqual(self.dialog.profile.content_hash, digest)
        self.assertEqual(self.parameter("trade.max_volume"), "32")
        self.assertIn("当前预览已保留", self.dialog.error_label.text())
        self.assertNotIn("PRIVATE-INVALID-TEXT", self.dialog.error_label.text())
        self.assertFalse(self.dialog.import_bytes('{"trade":{},"trade":{}}'))
        self.assertEqual(self.dialog.profile.content_hash, digest)

    def test_raw_dsv_import_export_discards_credentials_and_does_not_overwrite_source(self):
        original = copy.deepcopy(SETTINGS)
        original.update(account={"username": "PRIVATE-USER", "password": "PRIVATE-PASSWORD"},
                        notify={"dingtalk_webhook": "PRIVATE-WEBHOOK", "dingtalk_secret": "PRIVATE-SECRET"})
        with tempfile.TemporaryDirectory() as name:
            source, exported = Path(name) / "config.json", Path(name) / "business.json"
            source.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            before = source.read_bytes()
            self.assertTrue(self.dialog.import_file(source))
            self.assertFalse(self.dialog.export_file(source))
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(self.dialog.export_file(exported))
            text = exported.read_text(encoding="utf-8")
            for value in ("PRIVATE-USER", "PRIVATE-PASSWORD", "PRIVATE-WEBHOOK", "PRIVATE-SECRET", "account", "notify"):
                self.assertNotIn(value, text)
            restored = DsvProfile.from_json(text)
            expected = copy.deepcopy(SETTINGS)
            expected["symbols"][-1]["code"] = expected["symbols"][-1].pop("code_new")
            self.assertEqual(restored.to_settings(), expected)
            self.assertEqual(source.read_bytes(), before)

    def test_import_file_failure_and_invalid_source_leave_previous_view(self):
        with tempfile.TemporaryDirectory() as name:
            missing = Path(name) / "not-created.json"
            self.assertFalse(self.dialog.import_file(missing))
        self.assertEqual(self.dialog.profile.content_hash, self.profile.content_hash)
        self.assertFalse(self.dialog.import_bytes(json.dumps(SETTINGS), source_kind="unknown"))
        self.assertEqual(self.dialog.symbols_table.rowCount(), 5)

    def test_own_export_reimport_preserves_disabled_schedule_and_rollover_candidate(self):
        settings = copy.deepcopy(SETTINGS)
        settings["schedule"]["enabled"] = False
        preview = DsvProfile.from_settings(settings)
        self.assertTrue(self.dialog.apply_edited_preview(preview, expected_hash=self.profile.content_hash))
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "renamed-config.json"
            self.assertTrue(self.dialog.export_file(path))
            self.dialog.reset_to_baseline()
            with patch("online_trader.desktop.settings.QFileDialog.getOpenFileName", return_value=(str(path), "")):
                self.dialog.import_button.click()
                self.assertEqual(self.dialog.profile.content_hash, preview.content_hash)
                self.assertEqual(self.parameter("schedule.enabled"), "关闭")
                self.dialog.import_dsv_button.click()
                self.assertEqual(self.parameter("schedule.enabled"), "开启")
                self.assertEqual(self.dialog.symbols_table.item(4, 1).text(), "SHFE.ag2702")

    def test_status_window_opens_settings_without_creating_ipc_client(self):
        calls = []
        class Client:
            def __init__(self, *_):
                calls.append("created")
                raise AssertionError("Parameter preview must not open IPC")
        window = StatusWindow("unused-pipe", client_factory=Client)
        try:
            with patch("online_trader.desktop.settings._default_baseline", return_value=(self.profile, self.manifest)):
                window.settings_button.click(); self.app.processEvents()
            self.assertIsInstance(window._settings_dialog, SettingsDialog)
            self.assertEqual(window._settings_dialog.profile.content_hash, self.profile.content_hash)
            self.assertEqual(calls, [])
        finally:
            if window._settings_dialog is not None: window._settings_dialog.close()
            window.close(); window.deleteLater(); self.app.processEvents()

    def test_failed_baseline_load_keeps_status_console_without_invented_parameters(self):
        window = StatusWindow("unused-pipe")
        try:
            with patch("online_trader.desktop.settings._default_baseline",
                       side_effect=TraderError("E_CONFIG", "PRIVATE-INPUT")):
                window.settings_button.click(); self.app.processEvents()
            self.assertIsNone(window._settings_dialog)
            self.assertIn("参数基准未通过校验", window.notice_label.text())
            self.assertNotIn("PRIVATE-INPUT", window.notice_label.text())
            self.assertTrue(window.refresh_button.isEnabled())
        finally:
            window.close(); window.deleteLater(); self.app.processEvents()


if __name__ == "__main__": unittest.main()
