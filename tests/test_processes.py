import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from online_trader.desktop.client import StatusClient
from online_trader.kernel import Kernel
from qa.recording_port import RecordingPort


ROOT=Path(__file__).parents[1]


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.data=self.folder/"data";self.data.mkdir()
        self.account="DEV:process-"+uuid.uuid4().hex
        # Persistent protection is seeded by the independent QA API, never a public IPC command.
        with Kernel(self.data/"ledger.db",self.account,development=True,port=RecordingPort(),risk_budget="1000") as kernel:
            kernel.arm_development();kernel.wait_idle()
            plan=json.loads((ROOT/"qa/fixtures/base-plan.json").read_text(encoding="utf-8"))
            plan.update(account_key=self.account,initial_open_quantity=1,config_hash=kernel._configuration["payload_hash"])
            plan["partial_reduction"]={"enabled":False}
            context=kernel.issue_entry_context("qa",plan["contract_id"])
            child=kernel.admit_entry(plan,"process-entry","1000",context_id=context["context_id"]);kernel.wait_idle()
            kernel.record_fill(child,"process-entry-fill",1,"1000");kernel.wait_idle()
            kernel.observe(plan["position_cycle_id"],"1080",bid1="1080",ask1="1081");kernel.wait_idle()
        self.pipe="OnlineTrader.Test."+uuid.uuid4().hex
        self.ready=self.folder/"ready.json"
        self.log=(self.folder/"server.log").open("w",encoding="utf-8")
        self.process=subprocess.Popen([sys.executable,"-m","online_trader","serve","--data-dir",str(self.data),"--account-key",self.account,
            "--pipe",self.pipe,"--ready-file",str(self.ready),"--shadow-worker"],stdout=self.log,stderr=subprocess.STDOUT,cwd=ROOT,creationflags=subprocess.CREATE_NO_WINDOW,env=os.environ|{"PYTHONUTF8":"1"})
        deadline=time.monotonic()+8
        while not self.ready.exists():
            if self.process.poll() is not None or time.monotonic()>deadline:
                self.log.flush();raise AssertionError((self.folder/"server.log").read_text(encoding="utf-8"))
            time.sleep(0.02)
        self.info=json.loads(self.ready.read_text(encoding="utf-8"))
        self.client=StatusClient(self.pipe,expected_server_pid=self.info["pid"])

    def tearDown(self):
        if self.process.poll() is None:self.process.terminate()
        self.process.wait(timeout=5)
        self.log.close()
        # The read-only worker detects its original parent handle becoming signalled.
        time.sleep(0.35)
        self.temp.cleanup()

    def test_cli_client_exit_does_not_delete_protection_or_stop_kernel(self):
        result=subprocess.run([sys.executable,"-m","online_trader","status","--pipe",self.pipe],capture_output=True,text=True,encoding="utf-8",cwd=ROOT,timeout=8,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr)
        records=json.loads(result.stdout)["records"]
        cycle=next(row for row in records if row["record_type"]=="CYCLE")
        self.assertEqual(cycle["protection_mode"],"PROTECTED")
        self.assertIsNone(self.process.poll())
        self.assertEqual(next(r for r in self.client.snapshot()["records"] if r["record_type"]=="CYCLE")["remaining_quantity"],1)

    def test_pause_is_durable_and_does_not_end_protection(self):
        receipt=self.client.pause()
        self.assertEqual(receipt["status"],"SUCCEEDED")
        batch=self.client.snapshot()
        self.assertEqual(next(r for r in batch["records"] if r["record_type"]=="ACCOUNT")["entry_authority"],"DISARMED")
        self.assertEqual(next(r for r in batch["records"] if r["record_type"]=="CYCLE")["protection_mode"],"PROTECTED")

    def test_registered_shadow_can_be_killed_without_stopping_kernel(self):
        report=self.data/"worker-status.json";deadline=time.monotonic()+8
        while not report.exists():
            if time.monotonic()>deadline:
                self.log.flush();self.fail((self.folder/"server.log").read_text(encoding="utf-8"))
            time.sleep(0.03)
        worker=json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(worker["parent_pid"],self.info["pid"])
        from online_trader.runtime.processes import parent_pid
        self.assertIn(self.info["shadow_launcher_pid"],{worker["pid"],parent_pid(worker["pid"])})
        kernel32=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel32.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel32.OpenProcess.restype=wintypes.HANDLE
        kernel32.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel32.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel32.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel32.OpenProcess(0x0001|0x00100000,False,worker["pid"])
        self.assertTrue(handle)
        try:
            self.assertTrue(kernel32.TerminateProcess(handle,0))
            self.assertEqual(kernel32.WaitForSingleObject(handle,3000),0)
        finally:kernel32.CloseHandle(handle)
        self.assertIsNone(self.process.poll())
        cycle=next(r for r in self.client.snapshot()["records"] if r["record_type"]=="CYCLE")
        self.assertEqual(cycle["protection_mode"],"PROTECTED")
        self.assertEqual(cycle["remaining_quantity"],1)

    def test_unregistered_shadow_cannot_claim_read_or_order_authority(self):
        from online_trader.strategy.worker import ShadowStatusClient
        from online_trader.errors import TraderError
        with self.assertRaises(TraderError):ShadowStatusClient(self.pipe,"forged-shadow",timeout=2,expected_server_pid=self.info["pid"]).snapshot()
        self.assertIsNone(self.process.poll())
