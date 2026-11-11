from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid

from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.strategy import StrategyCoordinator
from online_trader.runtime.processes import parent_pid
from qa.crash_worker import fixture_plan
from qa.fault_report import FaultReport
from qa.recording_port import RecordingPort


ROOT = Path(__file__).parents[1]


class CrashFaultTests(unittest.TestCase):
    def setUp(self):
        self.evidence = FaultReport(self._testMethodName)
        self.process = None
        self.log = None
        self.account = "DEV:fault-" + uuid.uuid4().hex
        self.db = self.evidence.path / "ledger.db"

    def tearDown(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=3)
            self.evidence.event("process_exit", launcher_pid=self.process.pid, exit_code=self.process.returncode)
        if self.log is not None:self.log.close()
        self.evidence.finish()

    def crash(self, mode):
        stage = self.evidence.path / "stage.json"
        self.log = (self.evidence.path / "child.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "qa.crash_worker", "--db", str(self.db),
            "--account-key", self.account, "--mode", mode, "--stage-file", str(stage)], cwd=ROOT,
            stdout=self.log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
            env=os.environ | {"PYTHONUTF8": "1"})
        deadline = time.monotonic() + 6
        while not stage.exists():
            if self.process.poll() is not None or time.monotonic() >= deadline:
                self.log.flush()
                self.fail((self.evidence.path / "child.log").read_text(encoding="utf-8", errors="replace"))
            time.sleep(0.01)
        value = json.loads(stage.read_text(encoding="utf-8"))
        self.assertEqual(value["phase"], mode)
        self.assertEqual(value["account_key"], self.account)
        self.assertIn(self.process.pid, {value["pid"], parent_pid(value["pid"])})
        self.evidence.event("checkpoint", **value)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, value["pid"])
        self.assertTrue(handle, "Cannot pin the checkpointed development process")
        try:
            self.assertTrue(kernel32.TerminateProcess(handle, 77))
            self.assertEqual(kernel32.WaitForSingleObject(handle, 4000), 0)
        finally:kernel32.CloseHandle(handle)
        self.process.wait(timeout=4)
        self.assertNotEqual(self.process.returncode, 0)
        self.evidence.event("forced_termination", runtime_pid=value["pid"], launcher_pid=self.process.pid, exit_code=self.process.returncode)
        return value

    def read(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, params)]

    def assert_recovered(self, checkpoint, expected_mode=None):
        port = RecordingPort()
        with Kernel(self.db, self.account, development=True, port=port, risk_budget="1000") as kernel:
            snap = kernel.snapshot()
            self.assertFalse(snap["armed"])
            self.assertTrue(snap["recovery_required"])
            cycle = next(c for c in snap["cycles"] if c["position_cycle_id"] == checkpoint["cycle_id"])
            self.assertEqual(cycle["plan"]["plan_id"], checkpoint["plan_id"])
            self.assertEqual(cycle["orders"][0]["child_order_id"], checkpoint["child_order_id"])
            if expected_mode:self.assertEqual(cycle["protection"]["mode"], expected_mode)
            self.assertEqual(port.orders, [])
            self.evidence.event("reopened", kernel_boot_id=kernel.boot_id, durable_seq=snap["durable_seq"],
                mode=cycle["protection"]["mode"], remaining_qty=cycle["protection"]["owned_qty"],
                order_state=cycle["orders"][0]["send_state"], effects_after_reopen=len(port.orders))
            return cycle

    def test_durable_intent_before_owner_ack_becomes_unknown_without_replay(self):
        checkpoint = self.crash("intent-committed")
        self.assertEqual(checkpoint["effects"], 0)
        cycle = self.assert_recovered(checkpoint)
        self.assertEqual(cycle["orders"][0]["send_state"], "UNKNOWN")
        reservation = self.read("SELECT status,amount FROM reservations")[0]
        self.assertEqual(reservation, {"status": "UNKNOWN", "amount": "60"})
        self.evidence.passed(identity_preserved=True, unknown_occupancy_preserved=True, no_replay=True)

    def test_actual_send_call_without_saved_return_remains_unknown(self):
        checkpoint = self.crash("send-entered")
        self.assertEqual(checkpoint["effects"], 1)
        cycle = self.assert_recovered(checkpoint)
        self.assertEqual(cycle["orders"][0]["send_state"], "UNKNOWN")
        self.assertEqual(self.read("SELECT COUNT(*) AS n FROM send_attempts")[0]["n"], 1)
        self.evidence.passed(one_original_attempt=True, no_replay=True, no_fake_fill=True)

    def test_uncommitted_protection_latch_is_not_claimed_durable_after_kill(self):
        checkpoint = self.crash("protection-before-commit")
        self.assertEqual(self.read("SELECT mode FROM protection_state")[0]["mode"], "NORMAL")
        cycle = self.assert_recovered(checkpoint, "NORMAL")
        self.assertEqual(cycle["protection"]["owned_qty"], 1)
        self.assertEqual(self.read("SELECT event_type FROM event_journal WHERE event_seq=?", (checkpoint["pending_event_seq"],))[0]["event_type"], "KERNEL_BOOT")  # only the never-committed sequence can be reused
        self.evidence.passed(uncommitted_latch_absent=True, quantity_preserved=True, recovery_required=True)

    def test_committed_latch_survives_kill_before_owner_accepts_ack(self):
        checkpoint = self.crash("protection-committed")
        self.assertEqual(self.read("SELECT mode FROM protection_state")[0]["mode"], "PROTECTED")
        cycle = self.assert_recovered(checkpoint, "PROTECTED")
        self.assertEqual(cycle["protection"]["max_favorable_move"], "80")
        self.evidence.passed(committed_latch_preserved=True, high_water_preserved=True, no_replay=True)

    def test_switch_before_commit_rolls_back_both_strategy_updates(self):
        checkpoint = self.crash("switch-before-commit")
        active = self.read("SELECT strategy_epoch FROM strategy_instances WHERE status='ACTIVE'")
        self.assertEqual(active, [{"strategy_epoch": checkpoint["old_epoch"]}])
        self.assertEqual(self.read("SELECT status FROM strategy_instances WHERE strategy_epoch=?", (checkpoint["new_epoch"],))[0]["status"], "SHADOW")
        self.assert_recovered(checkpoint, "PROTECTED")
        self.assertEqual(self.read("SELECT COUNT(*) AS n FROM strategy_instances WHERE status='ACTIVE'")[0]["n"], 0)
        self.evidence.passed(old_commit_preserved=True, candidate_not_granted=True, restart_disarmed=True)

    def test_switch_commit_survives_kill_before_owner_epoch_changes(self):
        checkpoint = self.crash("switch-committed")
        self.assertEqual(checkpoint["owner_active_epoch"], checkpoint["old_epoch"])
        active = self.read("SELECT strategy_epoch FROM strategy_instances WHERE status='ACTIVE'")
        self.assertEqual(active, [{"strategy_epoch": checkpoint["new_epoch"]}])
        self.assertEqual(self.read("SELECT new_strategy_epoch FROM update_transactions ORDER BY updated_event_seq DESC LIMIT 1")[0]["new_strategy_epoch"], checkpoint["new_epoch"])
        self.assert_recovered(checkpoint, "PROTECTED")
        self.assertEqual(self.read("SELECT COUNT(*) AS n FROM strategy_instances WHERE status='ACTIVE'")[0]["n"], 0)
        self.evidence.passed(new_committed_epoch_preserved=True, old_epoch_not_revived=True, restart_disarmed=True)


class InjectedFaultTests(unittest.TestCase):
    def setUp(self):
        self.evidence = FaultReport(self._testMethodName)
        self.port = RecordingPort()
        self.kernel = Kernel(self.evidence.path / "ledger.db", "DEV:injected-" + uuid.uuid4().hex,
                             development=True, port=self.port, risk_budget="1000")

    def tearDown(self):
        try:self.kernel.close()
        except TraderError:pass
        self.evidence.finish()

    def arm_and_admit(self):
        self.kernel.arm_development(); self.kernel.wait_idle()
        plan = fixture_plan(self.kernel, 2)
        context = self.kernel.issue_entry_context("qa", plan["contract_id"])
        child = self.kernel.admit_entry(plan, "fault-pending", "1000", context_id=context["context_id"])
        return plan, child

    def test_injected_disk_full_rolls_back_intent_and_blocks_all_sends(self):
        self.kernel.arm_development(); self.kernel.wait_idle()
        before = self.kernel._durable_seq
        submit = self.kernel.ledger.submit
        def fail(kind, event, data, mutate=None, **kwargs):
            if event == "ENTRY_PREPARED":
                def full(conn, seq):
                    mutate(conn, seq)
                    raise sqlite3.OperationalError("database or disk is full")
                return submit(kind, event, data, full, **kwargs)
            return submit(kind, event, data, mutate, **kwargs)
        self.kernel.ledger.submit = fail
        plan = fixture_plan(self.kernel)
        context = self.kernel.issue_entry_context("qa", plan["contract_id"])
        self.kernel.admit_entry(plan, "disk-full", "1000", context_id=context["context_id"])
        with self.assertRaises(TraderError):self.kernel.wait_idle()
        self.assertEqual(self.port.orders, [])
        self.assertEqual(self.kernel._durable_seq, before)
        self.assertEqual(self.kernel.ledger.query("SELECT * FROM order_intents"), [])
        self.evidence.passed(injected_sqlite_full=True, physical_disk_not_filled=True, transaction_rolled_back=True, no_send=True)

    def test_old_boot_ack_cannot_authorize_current_owner_send(self):
        self.arm_and_admit()
        pending = self.kernel._pending[0]
        ack = pending.future.result(timeout=3)
        fake = Future(); fake.set_result(replace(ack, kernel_boot_id="boot-revoked"))
        pending.future = fake
        with self.assertRaises(TraderError):self.kernel.wait_idle()
        self.assertTrue(self.kernel._failed)
        self.assertEqual(self.port.orders, [])
        self.evidence.passed(old_boot_rejected=True, no_send=True)

    def test_later_ack_cannot_advance_past_missing_earlier_commit(self):
        plan, _ = self.arm_and_admit()
        other = fixture_plan(self.kernel, 1); other["contract_id"] = "DEV.TEST002"
        context = self.kernel.issue_entry_context("qa", other["contract_id"])
        self.kernel.admit_entry(other, "fault-second", "1000", context_id=context["context_id"])
        earlier, later = self.kernel._pending[0], self.kernel._pending[1]
        ack = later.future.result(timeout=3)
        before = self.kernel._durable_seq
        fake = Future(); fake.set_result(ack)
        earlier.future = fake
        with self.assertRaises(TraderError):self.kernel.wait_idle()
        self.assertEqual(self.kernel._durable_seq, before)
        self.assertEqual(self.port.orders, [])
        self.evidence.passed(ack_gap_rejected=True, durable_not_advanced=True, no_send=True)
