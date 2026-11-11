"""Atomic snapshot/replay client state and a bounded live pipe subscription.

Old views survive disconnects for display, always marked STALE. No client state is
an account fact or an authority to execute a transaction.
"""

from __future__ import annotations

import base64
import binascii
import copy
from dataclasses import dataclass,field
from datetime import datetime,timezone
import hashlib
import math
import time
import uuid

from online_trader import __version__
from online_trader.contracts.codec import canonical_bytes,strict_loads
from online_trader.contracts.validation import validate_message
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import connect
from online_trader.transport.status_client import StatusClient,_projection_validator

MAX_SNAPSHOT_BYTES=67_108_864
CHUNK_BYTES=262_144


def _seq(value):
    if type(value) is str and value.isascii() and (value=="0" or (value.isdigit() and not value.startswith("0"))):value=int(value)
    if type(value) is not int or not 0<=value<=9_223_372_036_854_775_807:
        raise TraderError("E_SCHEMA","Invalid stream sequence")
    return value


def validate_projection(batch,account_key):
    canonical_bytes(batch)
    if not _projection_validator().is_valid(batch):raise TraderError("E_SNAPSHOT","Invalid projection/1 snapshot")
    result={}
    for record in batch["records"]:
        key=(record["record_type"],record["record_id"])
        if key in result:raise TraderError("E_SNAPSHOT","Duplicate projection identity")
        for name,value in record.items():
            if name.endswith(("_seq","_epoch","_revision")) and value is not None:_seq(value)
        result[key]=copy.deepcopy(record)
    accounts=[r for r in result.values() if r["record_type"]=="ACCOUNT"]
    if len(accounts)!=1 or accounts[0]["record_id"]!=account_key:raise TraderError("E_SCOPE","Projection account differs")
    return result


def _utc():return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")


@dataclass
class _Candidate:
    begin:dict
    chunks:dict[int,bytes]=field(default_factory=dict)
    total:int=0
    deltas:dict[int,dict]=field(default_factory=dict)
    delta_bytes:int=0


class SubscriptionState:
    def __init__(self,subscription_id:str,*,view="STATUS",clock=time.monotonic,heartbeat_timeout=15.0):
        if view not in {"STATUS","TRADING","STRATEGY"} or type(subscription_id) is not str or not subscription_id:
            raise TraderError("E_SCHEMA","Invalid subscription identity")
        if isinstance(heartbeat_timeout,bool) or not isinstance(heartbeat_timeout,(int,float)) or not math.isfinite(heartbeat_timeout) or heartbeat_timeout<=0:
            raise TraderError("E_SCHEMA","Heartbeat timeout must be positive")
        self.subscription_id,self.view=subscription_id,view
        self.clock,self.heartbeat_timeout=clock,heartbeat_timeout
        self.account_key=self.kernel_boot_id=self.connection_id=None
        self.snapshot_boot_id=None
        self._records={};self._candidate=None;self._seen={}
        self.stream_seq=self.event_seq=self.durable_seq=self.business_seq=0
        self.stale=True;self.require_snapshot=True;self.connected=False
        self.last_received_message_id=None
        self.last_received_monotonic=self.clock()
        self.last_business_progress_monotonic=self.clock()
        self._server_highwater=0
        self._awaiting_resume=False
        self.last_gap=None

    def bind(self,welcome:dict):
        validate_message(welcome)
        if welcome["type"]!="Welcome" or any(welcome[k] is None for k in ("account_key","kernel_boot_id","connection_id")):
            raise TraderError("E_AUTH","Welcome does not bind a subscription")
        if self.account_key is not None and self.account_key!=welcome["account_key"]:
            self.mark_disconnected();raise TraderError("E_SCOPE","A subscription cannot switch account")
        self.account_key=welcome["account_key"]
        self.kernel_boot_id=welcome["kernel_boot_id"];self.connection_id=welcome["connection_id"]
        self.connected=True;self.stale=True;self._candidate=None
        self._awaiting_resume=self.snapshot_boot_id is not None
        self.last_received_monotonic=self.clock()
        if self.snapshot_boot_id is None:
            self.require_snapshot=True

    def request_body(self,*,resume=True):
        eligible=resume and self.snapshot_boot_id is not None and not self.require_snapshot
        return {"subscription_id":self.subscription_id,"view":self.view,
                "resume_stream_seq":str(self.stream_seq) if eligible else None,
                "resume_kernel_boot_id":self.snapshot_boot_id if eligible else None,
                "resume_projection_schema":"projection/1" if eligible else None}

    def resume_body(self):return self.request_body(resume=True)

    def mark_disconnected(self):
        self.connected=False;self.stale=True;self._candidate=None

    def check_health(self)->bool:
        if not self.connected or self.clock()-self.last_received_monotonic>=self.heartbeat_timeout:
            self.stale=True
            return False
        return not self.stale

    def projection(self)->dict:
        return {"records":[copy.deepcopy(r) for _,r in sorted(self._records.items())]}

    def _bad(self,code,message):
        self.stale=True;self.require_snapshot=True;self._candidate=None
        raise TraderError(code,message)

    def _binding(self,message):
        if not self.connected:
            self._bad("E_SCOPE","Subscription is not bound to a connection")
        if message["kernel_boot_id"]!=self.kernel_boot_id:
            self._bad("E_BOOT_MISMATCH","A stream crossed a kernel boot")
        if (message["account_key"],message["connection_id"])!=(self.account_key,self.connection_id):
            self._bad("E_SCOPE","Stream response is outside its connection scope")

    def _record_delta(self,records,body):
        record=body["record"];key=(record["record_type"],record["record_id"])
        old=records.get(key)
        if record["record_type"]=="ACCOUNT" and record["record_id"]!=self.account_key:
            self._bad("E_SCOPE","Delta belongs to another account")
        if old is not None:
            before,after=_seq(old["record_revision"]),_seq(record["record_revision"])
            if after<before or (after==before and canonical_bytes(old)!=canonical_bytes(record)):
                self._bad("E_STREAM_GAP","Record revision conflicts with the current view")
        records[key]=copy.deepcopy(record)

    def _delta(self,body,*,records=None,stream_seq=None,event_seq=None,durable_seq=None,seen=None):
        current_records=self._records if records is None else records
        current_stream=self.stream_seq if stream_seq is None else stream_seq
        current_event=self.event_seq if event_seq is None else event_seq
        current_durable=self.durable_seq if durable_seq is None else durable_seq
        history=self._seen if seen is None else seen
        seq,event,durable=_seq(body["stream_seq"]),_seq(body["event_seq"]),_seq(body["durable_seq"])
        digest=hashlib.sha256(canonical_bytes(body)).hexdigest()
        if seq<=current_stream:
            if history.get(seq)==digest:return current_stream,current_event,current_durable
            self._bad("E_STREAM_GAP","Repeated stream identity has different or unavailable content")
        if seq!=current_stream+1 or event<current_event or durable<current_durable or event>durable:
            self._bad("E_STREAM_GAP","Delta is not contiguous within committed facts")
        self._record_delta(current_records,body)
        history[seq]=digest
        for old in list(history):
            if old<seq-1023:del history[old]
        return seq,event,durable

    def apply(self,message:dict)->bool:
        try:validate_message(message)
        except TraderError:
            self.stale=True;self.require_snapshot=True;self._candidate=None
            raise
        self._binding(message)
        kind,body=message["type"],message["body"]
        if kind not in {"SnapshotBegin","SnapshotChunk","SnapshotEnd","Delta","Gap","Heartbeat"}:
            self._bad("E_SCHEMA","Message is not a stream response")
        if kind!="Heartbeat" and body["subscription_id"]!=self.subscription_id:
            self._bad("E_SCOPE","Response belongs to another subscription")
        self.last_received_message_id=message["message_id"]
        self.last_received_monotonic=self.clock()
        if kind=="Gap":
            self.last_gap=copy.deepcopy(body);self.stale=True;self.require_snapshot=True;self._candidate=None
            return False
        if kind=="SnapshotBegin":
            if self._candidate is not None:
                self._bad("E_SNAPSHOT","Snapshot begin replaced an unfinished candidate")
            event,durable=_seq(body["checkpoint_event_seq"]),_seq(body["durable_seq"])
            if event>durable or (self.snapshot_boot_id==self.kernel_boot_id and durable<self.durable_seq):
                self._bad("E_SNAPSHOT","Snapshot is outside its committed boundary")
            self._candidate=_Candidate(copy.deepcopy(body));self.stale=True
            return False
        if kind=="SnapshotChunk":
            candidate=self._candidate
            if candidate is None or body["snapshot_id"]!=candidate.begin["snapshot_id"]:
                self._bad("E_SNAPSHOT","Chunk has no matching snapshot candidate")
            if body["chunk_index"]>=candidate.begin["chunk_count"]:
                self._bad("E_SNAPSHOT","Chunk index exceeds declared count")
            try:raw=base64.b64decode(body["data_base64"],validate=True)
            except (binascii.Error,ValueError):self._bad("E_SNAPSHOT","Invalid Base64 snapshot chunk")
            if (len(raw)!=body["raw_bytes"] or not 1<=len(raw)<=CHUNK_BYTES or
                    base64.b64encode(raw).decode("ascii")!=body["data_base64"] or hashlib.sha256(raw).hexdigest()!=body["chunk_hash"]):
                self._bad("E_SNAPSHOT","Snapshot chunk hash or size differs")
            previous=candidate.chunks.get(body["chunk_index"])
            if previous is not None and previous!=raw:
                self._bad("E_SNAPSHOT","Conflicting repeated chunk")
            if previous is None:
                candidate.chunks[body["chunk_index"]]=raw;candidate.total+=len(raw)
                if candidate.total>candidate.begin["total_bytes"]:
                    self._bad("E_SNAPSHOT","Snapshot exceeds its declared total")
            return False
        if kind=="Delta":
            if self._candidate is not None:
                candidate=self._candidate;seq=_seq(body["stream_seq"])
                raw=canonical_bytes(body)
                previous=candidate.deltas.get(seq)
                if previous is not None and canonical_bytes(previous)!=raw:
                    self._bad("E_STREAM_GAP","Conflicting delta during snapshot construction")
                if previous is None:
                    candidate.deltas[seq]=copy.deepcopy(body);candidate.delta_bytes+=len(raw)
                    if len(candidate.deltas)>1024 or candidate.delta_bytes>16*1024*1024:
                        self._bad("E_QUEUE_OVERFLOW","Snapshot increment buffer is full")
                return False
            if self.require_snapshot or self.snapshot_boot_id!=self.kernel_boot_id:
                self._bad("E_STREAM_GAP","Delta cannot replace a required full snapshot")
            before=self.stream_seq
            self.stream_seq,self.event_seq,self.durable_seq=self._delta(body)
            changed=self.stream_seq!=before
            if changed:self.business_seq+=1;self.last_business_progress_monotonic=self.clock()
            self.stale=self._awaiting_resume or self.stream_seq<self._server_highwater
            return changed
        if kind=="SnapshotEnd":
            candidate=self._candidate
            if candidate is None or body["snapshot_id"]!=candidate.begin["snapshot_id"]:
                self._bad("E_SNAPSHOT","Snapshot end has no matching candidate")
            begin=candidate.begin
            if (len(candidate.chunks)!=begin["chunk_count"] or candidate.total!=begin["total_bytes"] or
                    body["last_stream_seq"]!=begin["last_stream_seq"]):
                self._bad("E_SNAPSHOT","Snapshot ended without all declared content")
            raw=b"".join(candidate.chunks[i] for i in range(begin["chunk_count"]))
            if hashlib.sha256(raw).hexdigest()!=body["snapshot_hash"]:
                self._bad("E_SNAPSHOT","Snapshot whole-content hash differs")
            try:
                batch=strict_loads(raw)
                records=validate_projection(batch,self.account_key)
                if canonical_bytes(batch)!=raw:raise TraderError("E_SNAPSHOT","Snapshot is not canonical")
            except TraderError:
                self.stale=True;self.require_snapshot=True;self._candidate=None
                raise
            if self.snapshot_boot_id==self.kernel_boot_id:
                for key,old in self._records.items():
                    if key not in records or _seq(records[key]["record_revision"])<_seq(old["record_revision"]):
                        self._bad("E_SNAPSHOT","Fresh snapshot cannot roll back or remove current facts")
                    if records[key]["record_revision"]==old["record_revision"] and canonical_bytes(records[key])!=canonical_bytes(old):
                        self._bad("E_SNAPSHOT","Fresh snapshot conflicts at an existing revision")
            stream,event,durable=_seq(begin["last_stream_seq"]),_seq(begin["checkpoint_event_seq"]),_seq(begin["durable_seq"])
            seen={}
            for _,delta in sorted(candidate.deltas.items()):
                stream,event,durable=self._delta(delta,records=records,stream_seq=stream,event_seq=event,durable_seq=durable,seen=seen)
            self._records=records;self.stream_seq,self.event_seq,self.durable_seq=stream,event,durable
            self.snapshot_boot_id=self.kernel_boot_id;self._seen=seen;self._candidate=None
            self.require_snapshot=False;self.stale=False;self._server_highwater=stream
            self._awaiting_resume=False
            self.business_seq+=1;self.last_business_progress_monotonic=self.clock()
            return True
        # Heartbeat freshness is not a promise of business advancement or trading readiness.
        last_stream,last_event,durable=_seq(body["last_stream_seq"]),_seq(body["last_event_seq"]),_seq(body["durable_seq"])
        if durable<self.durable_seq or last_stream<self.stream_seq:
            self._bad("E_STREAM_GAP","Heartbeat disagrees with observed committed progress")
        self._server_highwater=last_stream
        if self._candidate is None and not self.require_snapshot and self.snapshot_boot_id==self.kernel_boot_id:
            if self.stream_seq==last_stream:self._awaiting_resume=False
            self.stale=self.stream_seq<last_stream
        return False

    def heartbeat_ack(self)->dict:
        return {"last_received_message_id":self.last_received_message_id,"last_seen_event_seq":str(self.event_seq),
                "last_seen_stream_seq":str(self.stream_seq),"peer_business_seq":str(self.business_seq)}


class LiveSubscription(StatusClient):
    """Long-lived read-only client; all mutations remain separate command requests."""
    def __init__(self,pipe_name:str,*,client_instance_id=None,subscription_id=None,view="STATUS",timeout=5.0,
                 expected_server_pid=None,clock=time.monotonic,auto_resync=True):
        super().__init__(pipe_name,timeout=timeout,expected_server_pid=expected_server_pid)
        self.client_instance_id=client_instance_id or "desktop-"+uuid.uuid4().hex
        self.state=SubscriptionState(subscription_id or "subscription-"+uuid.uuid4().hex,view=view,clock=clock)
        self.auto_resync=auto_resync;self.connection=None;self.welcome=None;self._request_id=None

    def connect(self,*,resume=True):
        self.close()
        deadline=time.monotonic()+self.timeout
        connection=connect(self.pipe_name,timeout=self._remaining(deadline),expected_server_pid=self.expected_server_pid)
        try:
            hello=self._message("Hello",{"client_instance_id":self.client_instance_id,"requested_role":"DESKTOP",
                "supported_versions":["1.0"],"requested_account_key":self.state.account_key,
                "client_build_id":"online-trader-"+__version__})
            connection.send(hello,timeout=self._remaining(deadline))
            welcome=self._receive(connection,deadline,request=hello,expected_type="Welcome")
            if "READ_STREAM" not in welcome["body"]["capabilities"]:
                raise TraderError("E_AUTH","Live stream capability was not granted")
            self.state.bind(welcome);self.connection,self.welcome=connection,welcome
            self.request_snapshot(resume=resume)
            return self
        except BaseException:
            connection.close();self.connection=None;self.state.mark_disconnected();raise

    def request_snapshot(self,*,resume=False):
        if self.connection is None:raise TraderError("E_PIPE_CLOSED","Subscription is disconnected")
        request=self._message("SnapshotRequest",self.state.request_body(resume=resume),self.welcome)
        self._request_id=request["message_id"]
        self.connection.send(request,timeout=self.timeout)

    def poll(self,*,timeout=None)->bool:
        if self.connection is None:raise TraderError("E_PIPE_CLOSED","Subscription is disconnected")
        self.state.check_health()
        try:
            message=self.connection.recv(timeout=self.timeout if timeout is None else timeout)
            if message["type"]=="ProtocolError":
                self.state.mark_disconnected()
                raise TraderError(message["body"]["error_code"],"Kernel refused subscription request")
            correlation=message["correlation_id"]
            if message["type"].startswith("Snapshot"):
                if correlation!=self._request_id:raise TraderError("E_SCHEMA","Snapshot response correlation differs")
            elif correlation not in {None,self._request_id}:
                raise TraderError("E_SCHEMA","Stream response correlation differs")
            changed=self.state.apply(message)
            if message["type"]=="Heartbeat":
                self.connection.send(self._message("HeartbeatAck",self.state.heartbeat_ack(),self.welcome),timeout=self.timeout)
            elif message["type"]=="Gap" and self.auto_resync:
                self.request_snapshot(resume=False)
            return changed
        except TraderError as error:
            if error.code!="E_TIMEOUT":self.close()
            else:self.state.check_health()
            raise

    def projection(self)->dict:return self.state.projection()

    def close(self):
        if self.connection is not None:self.connection.close()
        self.connection=None;self.welcome=None;self.state.mark_disconnected()

    def __enter__(self):return self.connect()
    def __exit__(self,*_):self.close()
