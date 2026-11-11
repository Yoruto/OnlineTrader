import base64
import copy
from datetime import datetime,timezone
import hashlib
import threading
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.kernel.streams import StreamHub
from online_trader.transport.subscription import SubscriptionState,LiveSubscription


ACCOUNT="DEV:stream-test"
BOOT="boot-stream-test"


def batch(revision=1,authority="DISARMED"):
    return {"records":[{"record_type":"ACCOUNT","record_id":ACCOUNT,"record_revision":str(revision),
        "kernel_state":"DEGRADED","entry_authority":authority,"equity":None,"available":None,"margin":None,"fees":None,
        "funds_quality":"UNKNOWN","source_epoch":"0","fact_barrier_id":None}]}


def envelope(kind,body,*,boot=BOOT,connection="conn-stream-test",correlation=None):
    return {"protocol_version":"1.0","type":kind,"message_id":"message-"+kind,"connection_id":connection,
        "correlation_id":correlation,"sent_at_utc":"2026-11-09T00:00:00Z","kernel_boot_id":boot,"account_key":ACCOUNT,"body":body}


def welcome(*,boot=BOOT,connection="conn-stream-test"):
    return envelope("Welcome",{"negotiated_version":"1.0","peer_id":"peer-stream-test","granted_role":"DESKTOP",
        "kernel_epoch":"1","capabilities":["READ_STATUS","READ_STREAM"],"max_frame_bytes":1048576,
        "heartbeat_interval_ms":5000,"peer_liveness_timeout_ms":15000,"server_build_id":"g1-test"},boot=boot,connection=connection)


def request(subscription="sub-stream-test",*,resume=None,boot=None,schema=None,view="STATUS"):
    return {"subscription_id":subscription,"view":view,"resume_stream_seq":None if resume is None else str(resume),
            "resume_kernel_boot_id":boot,"resume_projection_schema":schema}


def heartbeat(seq,event,durable=None,*,boot=BOOT,connection="conn-stream-test"):
    return envelope("Heartbeat",{"kernel_epoch":"1","business_advance_seq":str(event),"last_event_seq":str(event),
        "durable_seq":str(event if durable is None else durable),"last_stream_seq":str(seq),"pending_transactions":0,
        "critical_queue_depth":0,"kernel_state":"DEGRADED","last_progress_utc":"2026-11-09T00:00:00Z"},boot=boot,connection=connection)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.hub=StreamHub(BOOT,ACCOUNT)
        self.hub.update(batch(),1,1)
        self.state=SubscriptionState("sub-stream-test")
        self.state.bind(welcome())

    def snapshot(self):
        self.key,messages=self.hub.request("desktop-stable",request())
        for kind,body in messages:self.state.apply(envelope(kind,body))
        return messages

    def test_complete_snapshot_is_atomic_and_unknown_funds_stay_unknown(self):
        key,messages=self.hub.request("desktop-stable",request())
        self.assertEqual(messages[0][1]["last_stream_seq"],"0")
        for kind,body in messages[:-1]:
            self.assertFalse(self.state.apply(envelope(kind,body)))
            self.assertEqual(self.state.projection(),{"records":[]})
        self.assertTrue(self.state.apply(envelope(*messages[-1])))
        self.assertFalse(self.state.stale)
        self.assertIsNone(self.state.projection()["records"][0]["equity"])

    def test_committed_changes_emit_contiguous_full_records(self):
        self.snapshot()
        messages=self.hub.update(batch(2,"ARMED"),2,2)[self.key]
        self.assertEqual(len(messages),1)
        self.assertEqual(messages[0][1]["stream_seq"],"1")
        self.state.apply(envelope(*messages[0]))
        self.assertEqual(self.state.projection(),batch(2,"ARMED"))
        self.assertEqual(self.state.event_seq,2)

    def test_disconnect_retains_replay_and_waits_for_heartbeat_barrier(self):
        self.snapshot();self.hub.detach(self.key);self.state.mark_disconnected()
        self.assertEqual(self.hub.update(batch(2,"ARMED"),2,2),{})
        self.assertEqual(self.hub.update(batch(3),3,3),{})
        self.state.bind(welcome(connection="conn-new"))
        key,messages=self.hub.request("desktop-stable",self.state.resume_body())
        self.assertEqual([body["stream_seq"] for _,body in messages],["1","2"])
        for kind,body in messages:self.state.apply(envelope(kind,body,connection="conn-new"))
        self.assertTrue(self.state.stale)
        self.state.apply(heartbeat(2,3,connection="conn-new"))
        self.assertFalse(self.state.stale);self.assertEqual(self.state.projection(),batch(3))

    def test_retention_expiry_requires_new_full_snapshot(self):
        self.hub=StreamHub(BOOT,ACCOUNT,retention_frames=1)
        self.hub.update(batch(),1,1);self.snapshot();self.hub.detach(self.key)
        self.hub.update(batch(2),2,2);self.hub.update(batch(3),3,3)
        key,messages=self.hub.request("desktop-stable",request(resume=0,boot=BOOT,schema="projection/1"))
        self.assertEqual(messages[0][0],"Gap");self.assertEqual(messages[0][1]["reason"],"RETENTION_EXPIRED")
        self.state.apply(envelope(*messages[0]));self.assertTrue(self.state.stale)
        _,messages=self.hub.request("desktop-stable",request())
        for message in messages:self.state.apply(envelope(*message))
        self.assertEqual(self.state.stream_seq,2);self.assertEqual(self.state.projection(),batch(3))

    def test_snapshot_boundary_preserves_later_facts_even_when_delta_interleaves(self):
        key,messages=self.hub.request("desktop-stable",request())
        updates=self.hub.update(batch(2,"ARMED"),2,2)[key]
        for message in messages[:-1]:self.state.apply(envelope(*message))
        self.state.apply(envelope(*updates[0]))
        self.assertEqual(self.state.projection(),{"records":[]})
        self.state.apply(envelope(*messages[-1]))
        self.assertEqual(self.state.projection(),batch(2,"ARMED"))
        self.assertEqual(self.state.stream_seq,1)

    def test_uncommitted_or_regressive_fact_boundaries_are_rejected(self):
        self.snapshot()
        for event,durable in [(2,1),(0,1),(1,0)]:
            with self.subTest(event=event,durable=durable),self.assertRaises(TraderError):self.hub.update(batch(),event,durable)
        self.assertEqual(self.hub.last_stream_seq(self.key),0)

    def test_same_revision_conflict_invalidates_source_and_subscription(self):
        self.snapshot()
        messages=self.hub.update(batch(1,"ARMED"),2,2)[self.key]
        self.assertEqual(messages[0][1]["reason"],"SEQUENCE_CONFLICT")
        with self.assertRaises(TraderError):self.hub.request("desktop-stable",request())
        self.hub.update(batch(2,"ARMED"),3,3)
        _,messages=self.hub.request("desktop-stable",request())
        self.state.apply(envelope("Gap",{"subscription_id":"sub-stream-test","expected_stream_seq":"1","observed_stream_seq":"0","reason":"SEQUENCE_CONFLICT","action":"REQUEST_SNAPSHOT"}))
        for message in messages:self.state.apply(envelope(*message))
        self.assertEqual(self.state.projection(),batch(2,"ARMED"))

    def test_retention_and_client_identity_cannot_cross_account_boot_or_view(self):
        self.snapshot()
        for body,reason in [(request(resume=0,boot="other-boot",schema="projection/1"),"BOOT_CHANGED"),
                            (request(resume=0,boot=BOOT,schema=None),"SCHEMA_CHANGED"),
                            (request(resume=0,boot=BOOT,schema="projection/1",view="TRADING"),"RETENTION_EXPIRED")]:
            _,messages=self.hub.request("desktop-stable",body)
            self.assertEqual(messages[0][1]["reason"],reason)
        _,messages=self.hub.request("other-instance",request(resume=0,boot=BOOT,schema="projection/1"))
        self.assertEqual(messages[0][1]["reason"],"RETENTION_EXPIRED")

    def test_capacity_evicts_only_disconnected_streams(self):
        hub=StreamHub(BOOT,ACCOUNT,max_streams=1);hub.update(batch(),1,1)
        key,_=hub.request("desktop-a",request("sub-a"))
        with self.assertRaises(TraderError):hub.request("desktop-b",request("sub-b"))
        hub.detach(key);hub.request("desktop-b",request("sub-b"))
        _,messages=hub.request("desktop-a",request("sub-a",resume=0,boot=BOOT,schema="projection/1"))
        self.assertEqual(messages[0][1]["reason"],"RETENTION_EXPIRED")

    def test_delivery_overflow_is_gap_not_silent_drop(self):
        self.snapshot()
        kind,body=self.hub.invalidate(self.key)
        self.assertEqual(kind,"Gap");self.assertEqual(body["reason"],"QUEUE_OVERFLOW")
        self.state.apply(envelope(kind,body));self.assertTrue(self.state.stale)
        self.assertEqual(self.state.projection(),batch())
        self.assertEqual(self.hub.update(batch(2),2,2),{})

    def test_a_record_larger_than_retention_invalidates_without_blocking(self):
        self.hub=StreamHub(BOOT,ACCOUNT,retention_bytes=1);self.hub.update(batch(),1,1);self.snapshot()
        messages=self.hub.update(batch(2),2,2)[self.key]
        self.assertEqual(messages[0][0],"Gap")

    def test_out_of_order_and_conflicting_duplicate_delta_preserve_old_view(self):
        self.snapshot();message=self.hub.update(batch(2),2,2)[self.key][0]
        wrong=copy.deepcopy(message[1]);wrong["stream_seq"]="2"
        with self.assertRaises(TraderError):self.state.apply(envelope("Delta",wrong))
        self.assertEqual(self.state.projection(),batch())
        self.state=SubscriptionState("sub-stream-test");self.state.bind(welcome());self.snapshot()
        message=self.hub.update(batch(3),3,3)[self.key][0]
        self.state.apply(envelope(*message));self.state.apply(envelope(*message))
        wrong=copy.deepcopy(message[1]);wrong["record"]["entry_authority"]="ARMED"
        with self.assertRaises(TraderError):self.state.apply(envelope("Delta",wrong))
        self.assertEqual(self.state.projection(),batch(3))

    def test_bad_snapshot_chunk_and_missing_chunk_never_publish_partial(self):
        self.snapshot();self.hub.update(batch(2),2,2)
        _,messages=self.hub.request("desktop-stable",request())
        self.state.apply(envelope(*messages[0]))
        with self.assertRaises(TraderError):self.state.apply(envelope(*messages[-1]))
        self.assertEqual(self.state.projection(),batch())
        self.state.apply(envelope(*messages[0]));bad=copy.deepcopy(messages[1][1]);bad["chunk_hash"]="0"*64
        with self.assertRaises(TraderError):self.state.apply(envelope("SnapshotChunk",bad))
        self.assertEqual(self.state.projection(),batch())

    def test_boot_change_requires_full_snapshot_and_rejects_old_connection_fragments(self):
        self.snapshot();self.state.mark_disconnected();self.state.bind(welcome(boot="boot-new",connection="conn-new"))
        with self.assertRaises(TraderError):self.state.apply(heartbeat(0,1))
        self.assertTrue(self.state.stale);self.assertEqual(self.state.projection(),batch())
        new=StreamHub("boot-new",ACCOUNT);new.update(batch(2),2,2)
        _,messages=new.request("desktop-stable",request())
        for kind,body in messages:self.state.apply(envelope(kind,body,boot="boot-new",connection="conn-new"))
        self.assertFalse(self.state.stale);self.assertEqual(self.state.snapshot_boot_id,"boot-new")

    def test_heartbeat_liveness_does_not_fake_business_progress(self):
        clock=[0.0];self.state=SubscriptionState("sub-stream-test",clock=lambda:clock[0]);self.state.bind(welcome());self.snapshot()
        advancement=self.state.business_seq;last_progress=self.state.last_business_progress_monotonic
        clock[0]=10;self.state.apply(heartbeat(0,1));self.assertTrue(self.state.check_health())
        self.assertEqual(self.state.business_seq,advancement);self.assertEqual(self.state.last_business_progress_monotonic,last_progress)
        clock[0]=26;self.assertFalse(self.state.check_health());self.assertTrue(self.state.stale)

    def test_pending_observation_heartbeat_cannot_advance_committed_view(self):
        self.snapshot()
        self.state.apply(heartbeat(0,5,durable=1))
        self.assertEqual(self.state.event_seq,1)
        self.assertEqual(self.state.durable_seq,1)
        self.assertEqual(self.state.projection(),batch())
        self.assertFalse(self.state.stale)

    def test_hub_rejects_foreign_thread_mutation(self):
        errors=[]
        def other():
            try:self.hub.update(batch(),1,1)
            except TraderError as error:errors.append(error.code)
        thread=threading.Thread(target=other);thread.start();thread.join()
        self.assertEqual(errors,["E_OWNER"])

    def test_nonfinite_heartbeat_deadline_cannot_disable_stale_detection(self):
        for value in (float("nan"),float("inf"),True,0):
            with self.subTest(value=value),self.assertRaises(TraderError):
                SubscriptionState("sub",heartbeat_timeout=value)


class LiveSubscriptionTests(unittest.TestCase):
    def test_live_client_handshake_snapshot_heartbeat_and_resume_use_stable_identity(self):
        hub=StreamHub(BOOT,ACCOUNT);hub.update(batch(),1,1)
        messages=[];sent=[]
        class Connection:
            def send(self,message,**_):
                sent.append(copy.deepcopy(message))
                if message["type"]=="Hello":
                    response=welcome();response["correlation_id"]=message["message_id"];messages.append(response)
                elif message["type"]=="SnapshotRequest":
                    _,response=hub.request("desktop-stable",message["body"])
                    messages.extend(envelope(kind,body,correlation=message["message_id"]) for kind,body in response)
            def recv(self,**_):return messages.pop(0)
            def close(self):pass
        client=LiveSubscription("OnlineTrader.Test",client_instance_id="desktop-stable",subscription_id="sub-stream-test")
        with patch("online_trader.transport.subscription.connect",return_value=Connection()):
            client.connect()
            while messages:client.poll()
            self.assertFalse(client.state.stale);self.assertEqual(client.projection(),batch())
            messages.append(heartbeat(0,1));client.poll()
            self.assertEqual(sent[-1]["type"],"HeartbeatAck")
            client.close();client.connect(resume=True)
            self.assertEqual(sent[-1]["body"]["resume_stream_seq"],"0")
            self.assertEqual([m["body"]["client_instance_id"] for m in sent if m["type"]=="Hello"],["desktop-stable","desktop-stable"])
            messages.append(heartbeat(0,1));client.poll();self.assertFalse(client.state.stale)
            client.close()

    def test_gap_keeps_old_view_stale_and_requests_new_snapshot(self):
        client=LiveSubscription("OnlineTrader.Test",subscription_id="sub-stream-test")
        client.state.bind(welcome());client.welcome=welcome()
        sent=[]
        class Connection:
            def send(self,message,**_):sent.append(message)
            def recv(self,**_):return envelope("Gap",{"subscription_id":"sub-stream-test","expected_stream_seq":"1","observed_stream_seq":"2","reason":"RETENTION_EXPIRED","action":"REQUEST_SNAPSHOT"})
            def close(self):pass
        client.connection=Connection();client.poll()
        self.assertTrue(client.state.stale);self.assertIsNone(sent[-1]["body"]["resume_stream_seq"])
        client.close()
