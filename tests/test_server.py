from concurrent.futures import Future
from datetime import datetime,timezone
import base64
import os
from pathlib import Path
import queue
import tempfile
import threading,time
from types import SimpleNamespace
import unittest
import uuid

from online_trader.kernel import Kernel
from online_trader.kernel.server import ControlServer,_Peer
from online_trader.errors import TraderError
from online_trader.contracts.codec import strict_loads
from qa.recording_port import RecordingPort


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.kernel=Kernel(Path(self.temp.name)/"ledger.db","DEV:dispatcher-"+uuid.uuid4().hex,
            development=True,port=RecordingPort(),risk_budget="1000")
        self.server=ControlServer(self.kernel,"OnlineTrader.Dispatcher."+uuid.uuid4().hex)
        self.peer=_Peer(SimpleNamespace(peer_pid=os.getpid(),close=lambda:None))

    def tearDown(self):
        self.server.listener.close();self.kernel.close();self.temp.cleanup()

    def message(self,kind,body,peer=None):
        peer=self.peer if peer is None else peer
        return {"protocol_version":"1.0","type":kind,"message_id":"test-message","connection_id":peer.connection_id,
            "correlation_id":None,"sent_at_utc":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
            "kernel_boot_id":self.kernel.boot_id,"account_key":self.kernel.account_key,"body":body}

    def test_registered_shadow_pid_cannot_claim_desktop_even_with_changed_instance(self):
        self.server.register_peer("registered-shadow",os.getpid())
        request=self.message("Hello",{"client_instance_id":"changed-name","requested_role":"DESKTOP","supported_versions":["1.0"],
            "requested_account_key":None,"client_build_id":"test"})
        with self.assertRaises(TraderError) as result:self.server._handle(self.peer,request)
        self.assertEqual(result.exception.code,"E_AUTH")

    def test_status_waiters_have_same_capacity_as_mutation_waiters(self):
        pending=Future()
        self.peer.ready=True;self.peer.role="DESKTOP"
        self.server._waiting=[(self.peer,{},pending)]*256
        request=self.message("GetCommandStatus",{"command_id":"known","payload_hash":"0"*64})
        with self.assertRaises(TraderError) as result:self.server._handle(self.peer,request)
        self.assertEqual(result.exception.code,"E_QUEUE_OVERFLOW")
        self.assertEqual(len(self.server._waiting),256)

    def test_inflight_receipt_retains_output_credit_until_native_pipe_write_completes(self):
        entered=threading.Event();release=threading.Event()
        def send(message,timeout):
            entered.set();release.wait(1);self.server._stop.set()
        self.peer.connection.send=send;self.peer.enqueue(self.message('Heartbeat',{}))
        writer=threading.Thread(target=self.server._writer,args=(self.peer,));writer.start()
        try:
            self.assertTrue(entered.wait(1));self.assertGreater(self.peer.queued_bytes,0)
        finally:release.set();writer.join(timeout=2)
        self.assertFalse(writer.is_alive());self.assertEqual(self.peer.queued_bytes,0)

    def test_shutdown_drain_is_bounded_for_an_unresponsive_reader(self):
        self.peer.queued_bytes=100;self.server._peers.add(self.peer)
        before=time.monotonic();self.server._drain_outgoing(timeout=.02)
        self.assertLess(time.monotonic()-before,.3)

    def test_shutdown_waits_for_an_accepted_command_receipt_before_closing_pipe(self):
        receipt=Future();request=self.message('OperatorCommand',{})
        self.server._peers.add(self.peer);self.server._waiting=[(self.peer,request,receipt)]
        worker=threading.Timer(.02,lambda:receipt.set_result({'status':'SUCCEEDED'}));worker.start()
        self.server._drain_outgoing(timeout=.1);worker.join(timeout=1)
        replies=self.drain(self.peer)
        self.assertEqual([r['type'] for r in replies],['CommandReceipt'])

    def subscribe(self,peer=None,*,resume=False):
        peer=self.peer if peer is None else peer
        peer.ready=True;peer.role="DESKTOP";peer.instance_id="desktop-stable"
        body={"subscription_id":"dispatcher-subscription","view":"STATUS",
            "resume_stream_seq":"0" if resume else None,
            "resume_kernel_boot_id":self.kernel.boot_id if resume else None,
            "resume_projection_schema":"projection/1" if resume else None}
        self.server._handle(peer,self.message("SnapshotRequest",body,peer))
        return peer.stream_key

    @staticmethod
    def drain(peer):
        result=[]
        while True:
            try:message,size=peer.outgoing.get_nowait()
            except queue.Empty:return result
            with peer.lock:peer.queued_bytes-=size
            result.append(message)

    def test_server_created_after_commits_does_not_replay_older_view(self):
        self.kernel.pause();self.kernel.wait_idle()
        self.kernel.pause();self.kernel.wait_idle()
        durable=self.kernel._durable_seq
        self.server.listener.close()
        self.server=ControlServer(self.kernel,"OnlineTrader.Dispatcher.History."+uuid.uuid4().hex)
        self.server._sync_streams()
        self.assertEqual(self.server.streams.durable_seq,durable)
        self.subscribe()
        begin=next(m for m in self.drain(self.peer) if m["type"]=="SnapshotBegin")
        self.assertEqual(begin["body"]["durable_seq"],str(durable))

    def test_slow_reader_output_overflow_closes_only_that_peer(self):
        key=self.subscribe()
        heartbeat=next(m for m in self.drain(self.peer) if m["type"]=="Heartbeat")
        for _ in range(self.peer.outgoing.maxsize):self.peer.enqueue(heartbeat)
        self.assertEqual(self.peer.outgoing.qsize(),256)
        self.kernel.pause();self.kernel.wait_idle()
        self.server._sync_streams()
        self.assertTrue(self.peer.closed.is_set())
        self.assertTrue(self.server.streams._streams[key].invalid)
        self.assertFalse(self.kernel._failed)
        healthy=_Peer(SimpleNamespace(peer_pid=os.getpid(),close=lambda:None))
        self.subscribe(healthy)
        self.assertTrue(any(m["type"]=="SnapshotEnd" for m in self.drain(healthy)))
        self.assertFalse(healthy.closed.is_set())

    def test_old_peer_cleanup_cannot_detach_replacement_same_boot_subscription(self):
        key=self.subscribe();self.drain(self.peer)
        replacement=_Peer(SimpleNamespace(peer_pid=os.getpid(),close=lambda:None))
        self.assertEqual(self.subscribe(replacement,resume=True),key)
        self.assertTrue(self.peer.closed.is_set())
        self.server._detach(self.peer)
        self.assertIs(self.server._stream_peers[key],replacement)
        self.assertIn(key,self.server.streams.attached_keys())
        self.drain(replacement)
        self.kernel.pause();self.kernel.wait_idle();self.server._sync_streams()
        delivered=self.drain(replacement)
        self.assertTrue(any(m["type"]=="Delta" for m in delivered))
        self.assertTrue(all(m["connection_id"]==replacement.connection_id for m in delivered))
        self.assertFalse(replacement.closed.is_set())

    def test_pending_arm_ack_cannot_leak_into_snapshot_or_heartbeat_progress(self):
        durable=self.kernel._durable_seq
        arm=self.kernel.arm_development()
        pending=self.kernel._pending[0]
        held=Future();pending.future=held
        try:
            ack=arm.result(timeout=3)
            self.kernel.poll()
            self.subscribe()
            delivered=self.drain(self.peer)
            begin=next(m["body"] for m in delivered if m["type"]=="SnapshotBegin")
            raw=b"".join(base64.b64decode(m["body"]["data_base64"]) for m in delivered if m["type"]=="SnapshotChunk")
            account=next(r for r in strict_loads(raw)["records"] if r["record_type"]=="ACCOUNT")
            heartbeat=next(m["body"] for m in delivered if m["type"]=="Heartbeat")
            self.assertEqual(account["entry_authority"],"DISARMED")
            self.assertEqual(begin["durable_seq"],str(durable))
            self.assertEqual(heartbeat["durable_seq"],str(durable))
            self.assertEqual(heartbeat["business_advance_seq"],str(durable))
            self.assertEqual(heartbeat["pending_transactions"],1)
            held.set_result(ack);self.kernel.wait_idle();self.server._sync_streams()
            deltas=[m["body"]["record"] for m in self.drain(self.peer) if m["type"]=="Delta"]
            self.assertEqual(next(r for r in deltas if r["record_type"]=="ACCOUNT")["entry_authority"],"ARMED")
        finally:
            if not held.done():held.set_result(arm.result(timeout=3))
