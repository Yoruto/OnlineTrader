from __future__ import annotations

import base64
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.contracts.validation import validate_message
from online_trader.errors import TraderError
from online_trader.strategy.worker import ParentWatch, ShadowStatusClient, run_worker


def value():
    return {"records": [{"record_type": "ACCOUNT", "record_id": "account-test", "record_revision": "1",
                         "kernel_state": "DEGRADED", "entry_authority": "DISARMED", "equity": None,
                         "available": None, "margin": None, "fees": None, "funds_quality": "UNKNOWN",
                         "source_epoch": "0", "fact_barrier_id": None}]}


class WorkerPipe:
    def __init__(self, role="SHADOW", capabilities=None):
        self.role = role; self.capabilities = capabilities or ["READ_STATUS"]
        self.sent = []; self.queue = []; self.closed = False

    def response(self, kind, body, request):
        return {"protocol_version": "1.0", "type": kind, "message_id": "reply-" + str(len(self.queue)),
                "connection_id": "connection-test", "correlation_id": request["message_id"],
                "sent_at_utc": "2026-11-09T00:00:00Z", "kernel_boot_id": "boot-test",
                "account_key": "account-test", "body": body}

    def send(self, message, *, timeout):
        validate_message(message); self.sent.append(message)
        if message["type"] == "Hello":
            self.queue.append(self.response("Welcome", {
                "negotiated_version": "1.0", "peer_id": "peer-shadow", "granted_role": self.role,
                "kernel_epoch": "1", "capabilities": self.capabilities, "max_frame_bytes": 1048576,
                "heartbeat_interval_ms": 5000, "peer_liveness_timeout_ms": 15000, "server_build_id": "build-test",
            }, message))
        elif message["type"] == "SnapshotRequest":
            sid = message["body"]["subscription_id"]; raw = canonical_bytes(value())
            self.queue.extend([
                self.response("SnapshotBegin", {"subscription_id": sid, "snapshot_id": "snapshot-test",
                    "projection_schema": "projection/1", "checkpoint_event_seq": "2", "durable_seq": "2",
                    "last_stream_seq": "0", "total_bytes": len(raw), "chunk_count": 1}, message),
                self.response("SnapshotChunk", {"subscription_id": sid, "snapshot_id": "snapshot-test", "chunk_index": 0,
                    "raw_bytes": len(raw), "chunk_hash": hashlib.sha256(raw).hexdigest(),
                    "data_base64": base64.b64encode(raw).decode()}, message),
                self.response("SnapshotEnd", {"subscription_id": sid, "snapshot_id": "snapshot-test",
                    "snapshot_hash": hashlib.sha256(raw).hexdigest(), "last_stream_seq": "0"}, message),
            ])
        else:
            raise AssertionError("Worker attempted a non-read-only message")

    def recv(self, *, timeout): return self.queue.pop(0)
    def close(self): self.closed = True
    def __enter__(self): return self
    def __exit__(self, *_args): self.close()


class ShadowClientTests(unittest.TestCase):
    def test_read_only_worker_uses_registered_instance_and_strict_snapshot(self):
        pipe = WorkerPipe()
        with patch("online_trader.transport.status_client.connect", return_value=pipe):
            client = ShadowStatusClient("pipe-test", "registered-shadow")
            self.assertEqual(client.snapshot(), value())
        self.assertEqual(pipe.sent[0]["body"]["client_instance_id"], "registered-shadow")
        self.assertEqual(pipe.sent[0]["body"]["requested_role"], "SHADOW")
        self.assertEqual([m["type"] for m in pipe.sent], ["Hello", "SnapshotRequest"])
        self.assertTrue(pipe.closed)

    def test_wrong_role_or_mutating_capabilities_refused_before_snapshot(self):
        for role, capabilities in [("DESKTOP", ["READ_STATUS"]), ("SHADOW", ["READ_STATUS", "OPERATOR_COMMAND"]),
                                   ("SHADOW", ["READ_STATUS", "PROPOSE_ENTRY"]),
                                   ("SHADOW", ["READ_STATUS", "REQUEST_CONTEXT"])]:
            pipe = WorkerPipe(role, capabilities)
            with self.subTest(role=role, capabilities=capabilities):
                with patch("online_trader.transport.status_client.connect", return_value=pipe), self.assertRaises(TraderError) as caught:
                    ShadowStatusClient("pipe-test", "registered-shadow").snapshot()
                self.assertEqual(caught.exception.code, "E_AUTH")
                self.assertEqual([m["type"] for m in pipe.sent], ["Hello"])

    def test_worker_pause_is_not_an_available_operation(self):
        with self.assertRaises(TraderError) as caught:
            ShadowStatusClient("pipe", "shadow").pause()
        self.assertEqual(caught.exception.code, "E_AUTH")

    def test_worker_import_does_not_load_gui(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import online_trader.strategy.worker; assert not any(x.startswith('PySide6') for x in sys.modules); assert 'online_trader.desktop' not in sys.modules"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class WorkerReportTests(unittest.TestCase):
    def test_fixture_report_is_atomic_and_contains_only_minimal_health(self):
        instances = []
        class Client:
            last_metadata = {"kernel_boot_id": "boot-test", "durable_seq": "2"}
            def __init__(self, pipe, instance, **kwargs):
                instances.append((pipe, instance, kwargs))
            def snapshot(self): return value()
        watches = []
        class Watch:
            def __init__(self, pid): self.closed = False; watches.append(self)
            def is_alive(self): return True
            def wait(self, seconds): return True
            def close(self): self.closed = True
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "worker.json"
            report.write_text('{"old":true}')
            status = run_worker("pipe", "shadow", 123, report, client_factory=Client, watch_factory=Watch, max_iterations=1)
            self.assertEqual(status, 0)
            record = json.loads(report.read_text())
            self.assertEqual(set(record), {"pid", "parent_pid", "kernel_boot_id", "durable_seq", "snapshot_hash", "records_count"})
            self.assertEqual(record["snapshot_hash"], content_hash(value()))
            self.assertEqual(record["parent_pid"], 123)
            self.assertEqual(record["records_count"], 1)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
        self.assertEqual(instances[0][2]["expected_server_pid"], 123)
        self.assertTrue(watches[0].closed)

    def test_missing_parent_exits_without_reading_or_writing(self):
        calls = []
        class Client:
            def __init__(self, *args, **kwargs): pass
            def snapshot(self): calls.append("snapshot")
        class Watch:
            def __init__(self, pid): pass
            def is_alive(self): return False
            def close(self): calls.append("closed")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            self.assertEqual(run_worker("pipe", "shadow", 123, target, client_factory=Client, watch_factory=Watch), 0)
            self.assertFalse(target.exists())
        self.assertEqual(calls, ["closed"])

    def test_failure_retries_are_bounded(self):
        calls = []
        class Client:
            def __init__(self, *args, **kwargs): pass
            def snapshot(self): calls.append("snapshot"); raise TraderError("E_AUTH", "Refused")
        class Watch:
            def __init__(self, pid): pass
            def is_alive(self): return True
            def wait(self, seconds): return True
            def close(self): pass
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()):
            self.assertEqual(run_worker("pipe", "shadow", 123, Path(directory) / "report.json", client_factory=Client, watch_factory=Watch), 2)
        self.assertEqual(len(calls), 20)

    def test_original_parent_handle_can_be_closed_without_signalling_process(self):
        watch = ParentWatch(os.getpid() if os.name == "nt" else os.getppid())
        self.assertTrue(watch.is_alive())
        self.assertTrue(watch.wait(0.001))
        watch.close()
        if os.name == "nt": self.assertFalse(watch.is_alive())


if __name__ == "__main__":
    unittest.main()
