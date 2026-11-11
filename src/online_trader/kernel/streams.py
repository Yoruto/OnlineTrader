"""Owner-thread, bounded projection streams built only from committed states.

The pipe server owns delivery queues. This hub never waits for a client and never
coalesces records after assigning stream_seq. A disconnected subscription continues
to collect a finite replay window; delivery overflow is an explicit Gap.
"""

from __future__ import annotations

from collections import OrderedDict, deque
import base64
import copy
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
from importlib.resources import files
import threading
import uuid

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.domain.values import identifier
from online_trader.errors import TraderError


MAX_SEQUENCE = 9_223_372_036_854_775_807
MAX_SNAPSHOT_BYTES = 67_108_864
CHUNK_BYTES = 262_144
StreamKey = tuple[str, str, str]
Message = tuple[str, dict]


def _seq(value) -> int:
    if type(value) is str and value.isascii() and (value == "0" or (value.isdigit() and not value.startswith("0"))):
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_SEQUENCE:
        raise TraderError("E_SCHEMA", "Invalid stream or event sequence")
    return value


def _deny(uri):
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=1)
def _validators():
    schema = strict_loads(files("online_trader").joinpath("resources", "ipc.schema.json").read_bytes())
    registry = Registry(retrieve=_deny).with_resource(schema["$id"], Resource.from_contents(schema))
    return {name: Draft202012Validator({"$ref":schema["$id"]+"#/$defs/"+name}, registry=registry,
                                      format_checker=FormatChecker())
            for name in ("projectionBatch", "SnapshotRequest")}


def validate_projection(batch: dict, account_key: str) -> dict[tuple[str,str],dict]:
    # canonical_bytes additionally rejects Python floats/bools masquerading as ints.
    canonical_bytes(batch)
    if not _validators()["projectionBatch"].is_valid(batch):
        raise TraderError("E_SNAPSHOT", "Projection does not conform to projection/1")
    records = {}
    for record in batch["records"]:
        identity = (record["record_type"],record["record_id"])
        if identity in records:
            raise TraderError("E_SNAPSHOT", "Projection contains duplicate record identities")
        for key,value in record.items():
            if key.endswith(("_seq","_revision","_epoch")) and value is not None:
                _seq(value)
        records[identity] = copy.deepcopy(record)
    accounts = [record for record in records.values() if record["record_type"]=="ACCOUNT"]
    if len(accounts)!=1 or accounts[0]["record_id"]!=account_key:
        raise TraderError("E_SCOPE", "Projection does not belong to the stream account")
    return records


@dataclass
class _Stream:
    key: StreamKey
    sequence: int = 0
    history: deque = field(default_factory=deque)
    history_bytes: int = 0
    attached: bool = True
    invalid: bool = False


class StreamHub:
    def __init__(self, kernel_boot_id: str, account_key: str, *, max_streams: int = 64,
                 retention_frames: int = 1024, retention_bytes: int = 16*1024*1024):
        self.kernel_boot_id = identifier(kernel_boot_id)
        self.account_key = identifier(account_key)
        if any(type(value) is not int or value < 1 for value in (max_streams,retention_frames,retention_bytes)):
            raise TraderError("E_SCHEMA", "Stream capacities must be positive integers")
        self.max_streams, self.retention_frames, self.retention_bytes = max_streams,retention_frames,retention_bytes
        self._owner = threading.get_ident()
        self._streams: OrderedDict[StreamKey,_Stream] = OrderedDict()
        self._records: dict[tuple[str,str],dict] | None = None
        self._projection_invalid = False
        self.event_seq = self.durable_seq = 0

    def _check(self):
        if threading.get_ident()!=self._owner:
            raise TraderError("E_OWNER", "Stream state requires the kernel owner thread")

    def update(self, projection_batch: dict, event_seq, durable_seq) -> dict[StreamKey,list[Message]]:
        self._check()
        event_seq,durable_seq = _seq(event_seq),_seq(durable_seq)
        if event_seq>durable_seq or event_seq<self.event_seq or durable_seq<self.durable_seq:
            raise TraderError("E_FACT_BARRIER", "Projection is outside its committed boundary")
        incoming = validate_projection(projection_batch,self.account_key)
        changed = []
        conflict = False
        if self._records is not None:
            if not self._records.keys()<=incoming.keys():
                conflict = True
            for key,record in incoming.items():
                old = self._records.get(key)
                if old is None:
                    changed.append(record);continue
                before,after = int(old["record_revision"]),int(record["record_revision"])
                if after<before or (after==before and canonical_bytes(old)!=canonical_bytes(record)):
                    conflict = True
                elif canonical_bytes(old)!=canonical_bytes(record):
                    changed.append(record)
            if event_seq==self.event_seq and changed:
                conflict = True
        if conflict:
            self._projection_invalid=True
            return {key:[self.invalidate(key,"SEQUENCE_CONFLICT")] for key in list(self._streams)}
        if self._records is None:
            changed = list(incoming.values())
        self._records = incoming
        self._projection_invalid=False
        self.event_seq,self.durable_seq = event_seq,durable_seq
        outputs = {}
        for key,stream in list(self._streams.items()):
            if stream.invalid:
                continue
            messages = []
            for record in sorted(changed,key=lambda r:(r["record_type"],r["record_id"])):
                if stream.sequence>=MAX_SEQUENCE:
                    messages = [self.invalidate(key,"SEQUENCE_CONFLICT")];break
                stream.sequence+=1
                body={"subscription_id":key[1],"stream_seq":str(stream.sequence),"event_seq":str(event_seq),
                      "durable_seq":str(durable_seq),"projection_schema":"projection/1","record":copy.deepcopy(record)}
                size=len(canonical_bytes(body))
                if size>self.retention_bytes:
                    messages = [self.invalidate(key,"QUEUE_OVERFLOW")];break
                stream.history.append((stream.sequence,body,size));stream.history_bytes+=size
                while len(stream.history)>self.retention_frames or stream.history_bytes>self.retention_bytes:
                    _,_,removed=stream.history.popleft();stream.history_bytes-=removed
                messages.append(("Delta",copy.deepcopy(body)))
            if messages and stream.attached:
                outputs[key]=messages
        return outputs

    def _gap(self,subscription_id,expected,observed,reason) -> Message:
        return "Gap",{"subscription_id":subscription_id,"expected_stream_seq":str(min(expected,MAX_SEQUENCE)),
                       "observed_stream_seq":str(observed),"reason":reason,"action":"REQUEST_SNAPSHOT"}

    def _new(self,key):
        if len(self._streams)>=self.max_streams:
            removable=next((k for k,s in self._streams.items() if not s.attached),None)
            if removable is None:
                raise TraderError("E_QUEUE_OVERFLOW", "Subscription capacity reached")
            del self._streams[removable]
        stream=_Stream(key)
        self._streams[key]=stream
        return stream

    def request(self, client_instance_id: str, body: dict) -> tuple[StreamKey,list[Message]]:
        self._check();identifier(client_instance_id)
        canonical_bytes(body)
        if not _validators()["SnapshotRequest"].is_valid(body):
            raise TraderError("E_SCHEMA", "Invalid snapshot request")
        if self._records is None or self._projection_invalid:
            raise TraderError("E_NOT_READY", "No committed projection has been registered")
        key=(client_instance_id,body["subscription_id"],body["view"])
        stream=self._streams.get(key)
        cursor=body["resume_stream_seq"]
        if cursor is not None:
            cursor=_seq(cursor)
            if body["resume_kernel_boot_id"]!=self.kernel_boot_id:
                return key,[self._gap(key[1],cursor+1,0,"BOOT_CHANGED")]
            if body["resume_projection_schema"]!="projection/1":
                return key,[self._gap(key[1],cursor+1,0,"SCHEMA_CHANGED")]
            if stream is None:
                return key,[self._gap(key[1],cursor+1,0,"RETENTION_EXPIRED")]
            if stream.invalid:
                return key,[self._gap(key[1],cursor+1,stream.sequence,"RETENTION_EXPIRED")]
            if cursor>stream.sequence:
                stream.invalid=True
                return key,[self._gap(key[1],stream.sequence+1,cursor,"SEQUENCE_CONFLICT")]
            first=stream.history[0][0] if stream.history else stream.sequence+1
            if cursor<first-1:
                return key,[self._gap(key[1],cursor+1,first,"RETENTION_EXPIRED")]
            stream.attached=True
            self._streams.move_to_end(key)
            return key,[("Delta",copy.deepcopy(delta)) for seq,delta,_ in stream.history if seq>cursor]
        if body["resume_kernel_boot_id"] is not None or body["resume_projection_schema"] is not None:
            raise TraderError("E_SCHEMA", "A full snapshot cannot carry partial resume metadata")
        if stream is None:
            stream=self._new(key)
        stream.attached=True;stream.invalid=False
        self._streams.move_to_end(key)
        raw=canonical_bytes({"records":[copy.deepcopy(r) for _,r in sorted(self._records.items())]})
        if len(raw)>MAX_SNAPSHOT_BYTES:
            stream.invalid=True
            raise TraderError("E_SNAPSHOT", "Snapshot exceeds the bounded snapshot capacity")
        snapshot_id="snapshot-"+uuid.uuid4().hex
        chunks=[raw[i:i+CHUNK_BYTES] for i in range(0,len(raw),CHUNK_BYTES)]
        common={"subscription_id":key[1],"snapshot_id":snapshot_id}
        messages=[("SnapshotBegin",common|{"projection_schema":"projection/1","checkpoint_event_seq":str(self.event_seq),
                   "durable_seq":str(self.durable_seq),"last_stream_seq":str(stream.sequence),"total_bytes":len(raw),"chunk_count":len(chunks)})]
        messages += [("SnapshotChunk",common|{"chunk_index":i,"raw_bytes":len(chunk),"chunk_hash":hashlib.sha256(chunk).hexdigest(),
                                               "data_base64":base64.b64encode(chunk).decode("ascii")}) for i,chunk in enumerate(chunks)]
        messages.append(("SnapshotEnd",common|{"snapshot_hash":hashlib.sha256(raw).hexdigest(),"last_stream_seq":str(stream.sequence)}))
        return key,messages

    def detach(self,key:StreamKey):
        self._check()
        if key in self._streams:
            self._streams[key].attached=False

    def invalidate(self,key:StreamKey,reason="QUEUE_OVERFLOW") -> Message:
        self._check()
        if reason not in {"QUEUE_OVERFLOW","RETENTION_EXPIRED","BOOT_CHANGED","SCHEMA_CHANGED","SEQUENCE_CONFLICT"}:
            raise TraderError("E_SCHEMA", "Unknown gap reason")
        stream=self._streams.get(key)
        observed=0 if stream is None else stream.sequence
        if stream is not None:
            stream.invalid=True;stream.history.clear();stream.history_bytes=0
        return self._gap(key[1],observed+1,observed,reason)

    def last_stream_seq(self,key:StreamKey) -> int:
        self._check()
        return self._streams[key].sequence if key in self._streams else 0

    def attached_keys(self) -> tuple[StreamKey,...]:
        self._check()
        return tuple(key for key,stream in self._streams.items() if stream.attached and not stream.invalid)
