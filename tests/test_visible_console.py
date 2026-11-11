import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid

from online_trader.kernel import Kernel
from online_trader.transport.status_client import StatusClient
from qa.crash_worker import seed_position
from qa.fault_report import FaultReport
from qa.recording_port import RecordingPort


ROOT = Path(__file__).parents[1]


class VisibleConsoleTests(unittest.TestCase):
    def setUp(self):
        self.evidence = FaultReport(self._testMethodName)
        self.data = self.evidence.path / "data"
        self.data.mkdir()
        self.account = "DEV:visible-" + uuid.uuid4().hex
        with Kernel(self.data / "ledger.db", self.account, development=True, port=RecordingPort(), risk_budget="1000") as kernel:
            plan, _ = seed_position(kernel)
            kernel.observe(plan["position_cycle_id"], "1080", bid1="1080", ask1="1081"); kernel.wait_idle()
        self.pipe = "OnlineTrader.Visible." + uuid.uuid4().hex
        self.server_log = (self.evidence.path / "server.log").open("w", encoding="utf-8")
        ready = self.evidence.path / "server-ready.json"
        self.server = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "online_trader", "serve", "--data-dir", str(self.data),
            "--account-key", self.account, "--pipe", self.pipe, "--ready-file", str(ready)], cwd=ROOT,
            stdout=self.server_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW, env=os.environ | {"PYTHONUTF8": "1"})
        deadline = time.monotonic() + 6
        while not ready.exists():
            if self.server.poll() is not None or time.monotonic() > deadline:
                self.server_log.flush()
                self.fail((self.evidence.path / "server.log").read_text(encoding="utf-8", errors="replace"))
            time.sleep(0.01)
        self.server_info = json.loads(ready.read_text(encoding="utf-8"))
        self.gui = None
        self.gui_log = None

    def tearDown(self):
        for process in [self.gui, self.server]:
            if process is not None:
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
        for stream in [self.gui_log, self.server_log]:
            if stream is not None:stream.close()
        self.evidence.finish()

    def show(self, mode):
        report = self.evidence.path / "visible-report.json"
        image = self.evidence.path / "console.png"
        self.gui_log = (self.evidence.path / "gui.log").open("w", encoding="utf-8")
        self.gui = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "qa.visible_console", "--pipe", self.pipe,
            "--report-file", str(report), "--screenshot-file", str(image), "--mode", mode], cwd=ROOT,
            stdout=self.gui_log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
            env=os.environ | {"PYTHONUTF8": "1", "QT_QPA_PLATFORM": "windows"})
        return report

    def wait_report(self, report, phase):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if report.exists():
                value = json.loads(report.read_text(encoding="utf-8"))
                if value.get("error"):self.fail(str(value))
                if value.get(phase):return value
            if self.gui.poll() is not None:
                self.gui_log.flush()
                self.fail((self.evidence.path / "gui.log").read_text(encoding="utf-8", errors="replace"))
            time.sleep(0.01)
        self.fail("Visible console did not reach phase: " + phase)

    def test_visible_window_refresh_and_close_keep_kernel_protection(self):
        report = self.show("close")
        self.gui.wait(timeout=7)
        self.gui_log.flush()
        self.assertEqual(self.gui.returncode, 0, (self.evidence.path / "gui.log").read_text(encoding="utf-8", errors="replace"))
        value = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(value["was_visible"])
        self.assertTrue(value["closed"])
        self.assertFalse(value["final_visible"])
        self.assertIn("未知", value["funds_text"])
        self.assertEqual(value["cycle_count"], 1)
        self.assertTrue(Path(value["screenshot"]).is_file())
        self.assertIsNone(self.server.poll())
        records = StatusClient(self.pipe, expected_server_pid=self.server_info["pid"]).snapshot()["records"]
        cycle = next(row for row in records if row["record_type"] == "CYCLE")
        self.assertEqual(cycle["protection_mode"], "PROTECTED")
        self.assertEqual(cycle["remaining_quantity"], 1)
        self.evidence.passed(native_window_visible=True, screenshot=value["screenshot"], unknown_funds_visible=True,
                             close_did_not_stop_kernel=True, protection_preserved=True)

    def test_visible_disconnected_window_retains_rows_and_marks_them_stale(self):
        report = self.show("disconnect")
        self.wait_report(report, "ready")
        self.server.terminate(); self.server.wait(timeout=4)
        self.gui.wait(timeout=7)
        self.gui_log.flush()
        self.assertEqual(self.gui.returncode, 0, (self.evidence.path / "gui.log").read_text(encoding="utf-8", errors="replace"))
        value = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(value["was_visible"])
        self.assertTrue(value["connection_failure_seen"])
        self.assertEqual(value["retained_cycle_count"], 1)
        self.assertIn("过期", value["stale_connection_text"])
        self.assertTrue(Path(value["stale_screenshot"]).is_file())
        self.evidence.passed(native_window_visible=True, stale_marked=True, old_rows_retained=True,
                             screenshot=value["stale_screenshot"], no_fake_connection=True)
