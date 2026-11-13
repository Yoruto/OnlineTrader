from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from tests.qt_lifecycle import dispose_widget

from online_trader.contracts.codec import canonical_bytes, payload_hash
from online_trader.contracts.validation import validate_message
from online_trader.desktop.app import StatusWindow
from online_trader.desktop.client import StatusClient
from online_trader.errors import TraderError


def projection():
    return {"records": [{
        "record_type": "ACCOUNT", "record_id": "account-test", "record_revision": "1",
        "kernel_state": "DEGRADED", "entry_authority": "DISARMED", "equity": None,
        "available": None, "margin": None, "fees": None, "funds_quality": "UNKNOWN",
        "source_epoch": "0", "fact_barrier_id": None,
    }]}


class FakePipe:
    def __init__(self, transform=None, value=None):
        self.sent = []; self.queue = []; self.closed = False
        self.transform = transform or (lambda message: message)
        self.value = value or projection()

    def response(self, kind, body, request):
        message = {"protocol_version": "1.0", "type": kind, "message_id": "response-" + str(len(self.queue)),
                   "connection_id": "connection-test", "correlation_id": request["message_id"],
                   "sent_at_utc": "2026-11-09T00:00:00Z", "kernel_boot_id": "boot-test",
                   "account_key": "account-test", "body": body}
        return self.transform(message)

    def send(self, message, *, timeout):
        validate_message(message)
        self.sent.append(copy.deepcopy(message))
        if message["type"] == "Hello":
            self.queue.append(self.response("Welcome", {
                "negotiated_version": "1.0", "peer_id": "peer-test", "granted_role": "DESKTOP",
                "kernel_epoch": "1", "capabilities": ["READ_STATUS", "REQUEST_CONTEXT", "OPERATOR_COMMAND"],
                "max_frame_bytes": 1048576, "heartbeat_interval_ms": 5000,
                "peer_liveness_timeout_ms": 15000, "server_build_id": "build-test",
            }, message))
        elif message["type"] == "SnapshotRequest":
            raw = canonical_bytes(self.value); middle = len(raw) // 2
            sid = message["body"]["subscription_id"]
            self.queue.append(self.response("SnapshotBegin", {
                "subscription_id": sid, "snapshot_id": "snapshot-test", "projection_schema": "projection/1",
                "checkpoint_event_seq": "2", "durable_seq": "2", "last_stream_seq": "0",
                "total_bytes": len(raw), "chunk_count": 2,
            }, message))
            # Deliberately out of order. The client must use checked chunk identities.
            for index, chunk in [(1, raw[middle:]), (0, raw[:middle])]:
                self.queue.append(self.response("SnapshotChunk", {
                    "subscription_id": sid, "snapshot_id": "snapshot-test", "chunk_index": index,
                    "raw_bytes": len(chunk), "chunk_hash": hashlib.sha256(chunk).hexdigest(),
                    "data_base64": base64.b64encode(chunk).decode(),
                }, message))
            self.queue.append(self.response("SnapshotEnd", {
                "subscription_id": sid, "snapshot_id": "snapshot-test", "snapshot_hash": hashlib.sha256(raw).hexdigest(),
                "last_stream_seq": "0",
            }, message))
        elif message["type"] == "ContextRequest":
            self.queue.append(self.response("RequestContext", {
                "context_id": "context-test", "purpose": "OPERATOR_COMMAND", "scope_id": "account-test",
                "kernel_epoch": "1", "strategy_epoch": None,
                "related_revisions": {"state_revision": "1", "risk_revision": "1", "config_revision": "1", "authority_revision": "1"},
                "issued_event_seq": "2", "valid_for_ms": 30000,
                "expires_at_utc": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            }, message))
        elif message["type"] == "OperatorCommand":
            assert payload_hash(message) == message["body"]["payload_hash"]
            self.queue.append(self.response("CommandReceipt", {
                "command_id": message["body"]["command_id"], "payload_hash": message["body"]["payload_hash"],
                "status": "SUCCEEDED", "receipt_revision": "1", "tx_id": "tx-pause", "event_seq": "3",
                "durable_seq": "3", "state_revision": "2", "error_code": "NONE", "reason": "Paused",
                "evidence_ids": ["tx-pause"],
            }, message))

    def recv(self, *, timeout):
        if not self.queue:
            raise TraderError("E_TIMEOUT", "No response")
        return self.queue.pop(0)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class StatusClientTests(unittest.TestCase):
    def run_snapshot(self, pipe):
        with patch("online_trader.transport.status_client.connect", return_value=pipe):
            client = StatusClient("test-pipe")
            result = client.snapshot()
        return client, result

    def test_complete_snapshot_is_checked_and_no_resume_is_requested(self):
        pipe = FakePipe()
        client, result = self.run_snapshot(pipe)
        self.assertEqual(result, projection())
        self.assertEqual(client.last_metadata["kernel_boot_id"], "boot-test")
        self.assertTrue(pipe.closed)
        request = pipe.sent[-1]
        self.assertEqual(request["type"], "SnapshotRequest")
        self.assertEqual(request["body"]["view"], "STATUS")
        self.assertIsNone(request["body"]["resume_stream_seq"])

    def test_cross_boot_or_account_fragments_never_publish(self):
        for field in ["kernel_boot_id", "account_key", "connection_id"]:
            def transform(message, field=field):
                if message["type"] == "SnapshotEnd": message[field] = "other"
                return message
            with self.subTest(field=field), self.assertRaises(TraderError):
                self.run_snapshot(FakePipe(transform))

    def test_chunk_hash_base64_identity_and_size_fail_closed(self):
        for field, value in [("chunk_hash", "0" * 64), ("data_base64", "!!!!"),
                             ("chunk_index", 2), ("snapshot_id", "other")]:
            def transform(message, field=field, value=value):
                if message["type"] == "SnapshotChunk": message["body"][field] = value
                return message
            with self.subTest(field=field), self.assertRaises(TraderError):
                self.run_snapshot(FakePipe(transform))

    def test_snapshot_end_hash_boundary_and_total_are_checked(self):
        cases = [("SnapshotEnd", "snapshot_hash", "0" * 64),
                 ("SnapshotEnd", "last_stream_seq", "1"),
                 ("SnapshotBegin", "total_bytes", 500),
                 ("SnapshotBegin", "checkpoint_event_seq", "3")]
        for kind, field, value in cases:
            def transform(message, kind=kind, field=field, value=value):
                if message["type"] == kind: message["body"][field] = value
                return message
            with self.subTest(kind=kind, field=field), self.assertRaises(TraderError):
                self.run_snapshot(FakePipe(transform))

    def test_duplicate_account_and_unknown_projection_fields_rejected(self):
        duplicate = projection(); duplicate["records"].append(copy.deepcopy(duplicate["records"][0]))
        unknown = projection(); unknown["records"][0]["pretend_ready"] = True
        wrong_account = projection(); wrong_account["records"][0]["record_id"] = "wrong-account"
        overflow = projection(); overflow["records"][0]["record_revision"] = "9223372036854775808"
        for value in [duplicate, unknown, wrong_account, overflow]:
            with self.subTest(value=value), self.assertRaises(TraderError):
                self.run_snapshot(FakePipe(value=value))

    def test_pause_uses_context_revisions_and_business_hash(self):
        pipe = FakePipe()
        with patch("online_trader.transport.status_client.connect", return_value=pipe):
            receipt = StatusClient("test-pipe").pause()
        self.assertEqual(receipt["status"], "SUCCEEDED")
        command = pipe.sent[-1]
        self.assertEqual(command["body"]["context_id"], "context-test")
        self.assertEqual(command["body"]["parameters"], {"cancel_owned_entry_remainders": False})
        self.assertEqual(command["body"]["payload_hash"], payload_hash(command))
        self.assertTrue(pipe.closed)

    def test_pause_refuses_wrong_receipt_identity(self):
        def transform(message):
            if message["type"] == "CommandReceipt": message["body"]["command_id"] = "another-command"
            return message
        with patch("online_trader.transport.status_client.connect", return_value=FakePipe(transform)), self.assertRaises(TraderError):
            StatusClient("test-pipe").pause()


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_idle(self, window):
        deadline = time.monotonic() + 3
        while window._worker is not None and time.monotonic() < deadline:
            self.app.processEvents(); QTest.qWait(5)
        self.assertIsNone(window._worker)

    def test_refresh_unknown_funds_then_stale_and_close_has_no_kernel_command(self):
        calls = []
        class Client:
            last_metadata = {"kernel_boot_id": "boot-test", "received_at_utc": "now"}
            def __init__(self, _pipe): pass
            def snapshot(self):
                calls.append("snapshot")
                if len(calls) > 1: raise TraderError("E_PIPE_CLOSED", "Closed")
                return projection()
            def pause(self):
                calls.append("pause")
                return {"status": "COMMITTED"}
        window = StatusWindow("test-pipe", client_factory=Client)
        try:
            window.refresh(); self.wait_idle(window)
            self.assertIn("未知", window.funds_label.text())
            self.assertIn("DEGRADED", window.kernel_label.text())
            self.assertIn("DISARMED", window.kernel_label.text())
            self.assertFalse(window._stale)
            window.refresh(); self.wait_idle(window)
            self.assertTrue(window._stale)
            self.assertIn("过期", window.connection_label.text())
            self.assertIn("DEGRADED", window.kernel_label.text())
        finally:
            dispose_widget(window); self.app.processEvents()
        self.assertEqual(calls, ["snapshot", "snapshot"])

    def test_pause_displays_actual_stage_and_marks_snapshot_stale(self):
        class Client:
            last_metadata = None
            def __init__(self, _pipe): pass
            def pause(self): return {"status": "COMMITTED"}
        window = StatusWindow("test-pipe", client_factory=Client)
        try:
            window.pause_entries(); self.wait_idle(window)
            self.assertIn("COMMITTED", window.notice_label.text())
            self.assertTrue(window._stale)
            self.assertNotIn("已成交", window.notice_label.text())
        finally:
            dispose_widget(window); self.app.processEvents()

    def test_close_while_request_runs_only_hides_and_waits_for_local_worker(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Client:
            last_metadata = None
            def __init__(self, _pipe): pass
            def snapshot(self):
                calls.append("snapshot"); entered.set()
                if not release.wait(2): raise TraderError("E_TIMEOUT", "Timed out")
                return projection()
        window = StatusWindow("test-pipe", client_factory=Client)
        try:
            window.refresh()
            self.assertTrue(entered.wait(1))
            before = time.monotonic(); window.close()
            self.assertLess(time.monotonic() - before, 0.25)
            self.assertTrue(window._closing)
            release.set(); self.wait_idle(window); self.app.processEvents()
            self.assertEqual(calls, ["snapshot"])
        finally:
            release.set(); self.wait_idle(window); dispose_widget(window); self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
