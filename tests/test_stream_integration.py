"""Real Windows pipe coverage for offline, committed projection delivery."""

from concurrent.futures import Future
import copy
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
import uuid

from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.server import ControlServer
from online_trader.transport.status_client import StatusClient
from online_trader.transport.subscription import LiveSubscription


class _ObservedServer(ControlServer):
    """Run bounded QA actions on the owner; observe validated network ACKs."""

    def __init__(self, kernel, pipe, actions, acknowledgements, condition):
        super().__init__(kernel, pipe)
        self.actions = actions
        self.acknowledgements = acknowledgements
        self.condition = condition

    def _sync_streams(self):
        try:
            action, result = self.actions.get_nowait()
        except queue.Empty:
            pass
        else:
            try:
                result.set_result(action(self.kernel))
            except BaseException as error:
                result.set_exception(error)
        super()._sync_streams()

    def _handle(self, peer, message):
        super()._handle(peer, message)
        if message["type"] == "HeartbeatAck":
            with self.condition:
                self.acknowledgements.append((peer.connection_id, copy.deepcopy(message["body"])))
                self.condition.notify_all()


class _PipeKernel:
    def __init__(self, ledger, account, pipe):
        self.ledger, self.account, self.pipe = ledger, account, pipe
        self.actions = queue.Queue(8)
        self.acknowledgements = []
        self.condition = threading.Condition()
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.errors = []
        self.boot = None
        self.thread = threading.Thread(target=self._run, name="stream-test-owner", daemon=True)

    def _run(self):
        # Kernel creation, SQLite ACK application and server state have one owner.
        try:
            with Kernel(self.ledger, self.account) as kernel:
                server = _ObservedServer(kernel, self.pipe, self.actions,
                                         self.acknowledgements, self.condition)
                self.boot = kernel.boot_id
                self.ready.set()
                server.serve(self.stop)
        except BaseException as error:
            self.errors.append(error)
            self.ready.set()

    def start(self):
        self.thread.start()
        if not self.ready.wait(5):
            raise AssertionError("Owner did not start within five seconds")
        self.check()
        return self

    def check(self):
        if self.errors:
            raise AssertionError("Owner thread failed") from self.errors[0]

    def call(self, action):
        result = Future()
        self.actions.put_nowait((action, result))
        return result.result(timeout=5)

    def wait_ack(self, connection_id, stream_seq):
        deadline = time.monotonic() + 5
        with self.condition:
            while True:
                for connection, body in self.acknowledgements:
                    if connection == connection_id and int(body["last_seen_stream_seq"]) == stream_seq:
                        return body
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("Heartbeat high-water acknowledgement was not received")
                self.condition.wait(remaining)

    def close(self):
        self.stop.set()
        self.thread.join(8)
        if self.thread.is_alive():
            raise AssertionError("Owner failed to stop within eight seconds")
        self.check()


class _TraceConnection:
    """Observe actual received native frames without replacing the transport."""

    def __init__(self, connection):
        self.connection = connection
        self.messages = []

    def recv(self, **kwargs):
        message = self.connection.recv(**kwargs)
        self.messages.append(copy.deepcopy(message))
        return message

    def send(self, *args, **kwargs):
        return self.connection.send(*args, **kwargs)

    def close(self):
        return self.connection.close()


@unittest.skipUnless(os.name == "nt", "Native named-pipe integration requires Windows")
class StreamIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = Path(self.temporary.name) / "ledger.db"
        self.account = "DEV:stream-pipe-" + uuid.uuid4().hex
        self.pipe = "OnlineTrader.StreamTest." + uuid.uuid4().hex
        self.host = _PipeKernel(self.ledger, self.account, self.pipe).start()
        self.addCleanup(self._close_host)
        self.client = LiveSubscription(self.pipe, client_instance_id="desktop-" + uuid.uuid4().hex,
            subscription_id="subscription-" + uuid.uuid4().hex, timeout=3,
            expected_server_pid=os.getpid())
        self.addCleanup(self.client.close)
        self.control = StatusClient(self.pipe, timeout=3, expected_server_pid=os.getpid())

    def _close_host(self):
        if self.host is not None:
            self.host.close()
            self.host = None

    def connect(self):
        self.client.connect()
        trace = _TraceConnection(self.client.connection)
        self.client.connection = trace
        return trace

    def receive_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.host.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail("Stream did not reach the expected committed state within its deadline")
            try:
                self.client.poll(timeout=min(remaining, 0.5))
            except TraderError as error:
                if error.code != "E_TIMEOUT":
                    raise

    def initial_snapshot(self):
        trace = self.connect()
        self.receive_until(lambda: not self.client.state.stale and
                           any(m["type"] == "Heartbeat" for m in trace.messages))
        self.host.wait_ack(self.client.state.connection_id, self.client.state.stream_seq)
        return trace

    def account_record(self):
        return next(r for r in self.client.projection()["records"] if r["record_type"] == "ACCOUNT")

    def test_snapshot_then_independent_pause_connection_delivers_committed_delta(self):
        trace = self.initial_snapshot()
        self.assertEqual([m["type"] for m in trace.messages],
                         ["SnapshotBegin", "SnapshotChunk", "SnapshotEnd", "Heartbeat"])
        self.assertIn("READ_STREAM", self.client.welcome["body"]["capabilities"])
        self.assertEqual(self.client.state.stream_seq, 0)
        before = self.client.state.durable_seq
        self.assertIsNone(self.account_record()["equity"])
        self.assertEqual(self.account_record()["funds_quality"], "UNKNOWN")

        receipt = self.control.pause()
        self.assertEqual(receipt["status"], "SUCCEEDED")
        committed = int(receipt["durable_seq"])
        self.assertGreater(committed, before)
        self.receive_until(lambda: self.client.state.stream_seq == 2 and
                           self.client.state.durable_seq == committed and
                           int(self.account_record()["record_revision"]) == committed)
        deltas = [m["body"] for m in trace.messages if m["type"] == "Delta"]
        self.assertTrue(deltas)
        self.assertEqual([int(d["stream_seq"]) for d in deltas], list(range(1, len(deltas) + 1)))
        self.assertTrue(all(d["event_seq"] == d["durable_seq"] for d in deltas))
        self.assertEqual(self.account_record()["entry_authority"], "DISARMED")
        self.assertFalse(self.client.state.stale)
        self.assertEqual(self.client.projection(), self.control.snapshot())

    def test_stable_instance_replays_offline_commits_and_heartbeat_confirms_highwater(self):
        self.initial_snapshot()
        previous_connection = self.client.state.connection_id
        previous_boot = self.client.state.kernel_boot_id
        identity = self.client.client_instance_id, self.client.state.subscription_id
        cached = self.client.projection()
        self.client.close()
        self.assertTrue(self.client.state.stale)
        self.assertEqual(self.client.projection(), cached)
        receipts = [self.control.pause(), self.control.pause()]
        final = int(receipts[-1]["durable_seq"])

        trace = self.connect()
        self.assertNotEqual(self.client.state.connection_id, previous_connection)
        self.assertEqual(self.client.state.kernel_boot_id, previous_boot)
        self.assertEqual((self.client.client_instance_id, self.client.state.subscription_id), identity)
        self.assertTrue(self.client.state.stale)
        self.assertEqual(self.client.projection(), cached)
        self.receive_until(lambda: self.client.state.durable_seq == final)
        self.assertTrue(self.client.state.stale, "Replay requires a server high-water heartbeat")
        self.assertTrue(all(m["type"] == "Delta" for m in trace.messages))
        account_revisions = [int(m["body"]["record"]["record_revision"]) for m in trace.messages
                             if m["body"]["record"]["record_type"] == "ACCOUNT"]
        self.assertEqual(account_revisions, [int(r["durable_seq"]) for r in receipts])
        self.receive_until(lambda: not self.client.state.stale)
        self.assertEqual(trace.messages[-1]["type"], "Heartbeat")
        self.assertFalse(any(m["type"].startswith("Snapshot") or m["type"] == "Gap" for m in trace.messages))
        self.assertEqual(self.client.state.stream_seq, 4)
        acknowledgement = self.host.wait_ack(self.client.state.connection_id, 4)
        self.assertEqual(int(acknowledgement["last_seen_event_seq"]), final)
        self.assertEqual(self.client.projection(), self.control.snapshot())

    def test_boot_change_emits_gap_and_atomically_refetches_snapshot(self):
        self.initial_snapshot()
        receipt = self.control.pause()
        self.receive_until(lambda: self.client.state.durable_seq == int(receipt["durable_seq"]))
        old_boot = self.client.state.kernel_boot_id
        cached = self.client.projection()
        self.assertGreater(self.client.state.stream_seq, 0)
        self.client.close()
        self._close_host()
        self.host = _PipeKernel(self.ledger, self.account, self.pipe).start()
        self.assertNotEqual(self.host.boot, old_boot)

        trace = self.connect()
        self.assertTrue(self.client.state.stale)
        self.assertEqual(self.client.projection(), cached)
        self.receive_until(lambda: self.client.state.last_gap is not None)
        self.assertEqual(self.client.state.last_gap["reason"], "BOOT_CHANGED")
        self.assertEqual(self.client.projection(), cached)
        self.assertTrue(self.client.state.require_snapshot)
        self.receive_until(lambda: not self.client.state.stale and
                           any(m["type"] == "Heartbeat" for m in trace.messages))
        kinds = [m["type"] for m in trace.messages]
        self.assertEqual(kinds, ["Gap", "SnapshotBegin", "SnapshotChunk", "SnapshotEnd", "Heartbeat"])
        self.assertEqual(self.client.state.snapshot_boot_id, self.host.boot)
        self.assertEqual(self.client.state.stream_seq, 0)
        self.assertFalse(self.client.state.require_snapshot)
        self.assertGreater(int(self.account_record()["record_revision"]), int(receipt["durable_seq"]))
        self.host.wait_ack(self.client.state.connection_id, 0)
        self.assertEqual(self.client.projection(), self.control.snapshot())

    def test_one_owner_poll_preserves_each_of_three_completed_commit_views(self):
        trace = self.initial_snapshot()
        before = self.client.state.durable_seq

        def three_commits(kernel):
            futures = [kernel.pause() for _ in range(3)]
            acks = [future.result(timeout=3) for future in futures]
            # All futures finish before a single owner poll accepts their ACKs.
            applied = kernel.poll()
            return applied, [ack.durable_seq for ack in acks]

        applied, sequences = self.host.call(three_commits)
        self.assertEqual(applied, 3)
        self.assertEqual(sequences, [before + 1, before + 2, before + 3])
        self.receive_until(lambda: self.client.state.stream_seq == 6)
        deltas = [m["body"] for m in trace.messages if m["type"] == "Delta"]
        self.assertEqual([int(d["stream_seq"]) for d in deltas], list(range(1, 7)))
        for record_type in ("ACCOUNT", "BLOCK"):
            matching = [d for d in deltas if d["record"]["record_type"] == record_type]
            self.assertEqual([int(d["record"]["record_revision"]) for d in matching], sequences)
            self.assertEqual([int(d["durable_seq"]) for d in matching], sequences)
        self.assertFalse(self.client.state.stale)
        self.assertEqual(self.client.projection(), self.control.snapshot())


if __name__ == "__main__":
    unittest.main()
