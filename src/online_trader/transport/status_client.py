"""Short-lived local client: snapshots are checked as a whole before publication."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from importlib.resources import files
import time
from typing import Any
import uuid

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from online_trader import __version__
from online_trader.contracts.codec import canonical_bytes, content_hash, payload_hash, strict_loads
from online_trader.contracts.validation import validate_message
from online_trader.domain.values import identifier
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import connect

MAX_SNAPSHOT_BYTES = 67_108_864
MAX_CHUNK_BYTES = 262_144
MAX_CHUNKS = 256


def _deny_retrieval(uri: str):
    raise NoSuchResource(ref=uri)


@lru_cache(maxsize=1)
def _projection_validator() -> Draft202012Validator:
    schema = strict_loads(files("online_trader").joinpath("resources", "ipc.schema.json").read_bytes())
    registry = Registry(retrieve=_deny_retrieval).with_resource(schema["$id"], Resource.from_contents(schema))
    return Draft202012Validator({"$ref": schema["$id"] + "#/$defs/projectionBatch"},
                               registry=registry, format_checker=FormatChecker())


def _id(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StatusClient:
    """Pull-only G1 client. No stream resume, SDK, stop-kernel or raw event API."""

    def __init__(self, pipe_name: str, *, timeout: float = 5.0,
                 expected_server_pid: int | None = None, expected_account_key=None, expected_kernel_boot_id=None,
                 client_instance_id=None) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise TraderError("E_SCHEMA", "Client timeout must be at most thirty seconds")
        self.pipe_name = pipe_name
        self.timeout = timeout
        self.expected_server_pid = expected_server_pid
        self.expected_account_key=expected_account_key
        self.expected_kernel_boot_id=expected_kernel_boot_id
        self.client_instance_id = identifier(client_instance_id) if client_instance_id is not None else None
        self.last_command=None
        self.last_metadata: dict[str, Any] | None = None

    @staticmethod
    def _message(kind: str, body: dict, welcome: dict | None = None) -> dict:
        return {"protocol_version": "1.0", "type": kind, "message_id": _id("msg"),
                "connection_id": None if welcome is None else welcome["connection_id"],
                "correlation_id": None, "sent_at_utc": _utc(),
                "kernel_boot_id": None if welcome is None else welcome["kernel_boot_id"],
                "account_key": None if welcome is None else welcome["account_key"], "body": body}

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TraderError("E_TIMEOUT", "Client request deadline expired")
        return min(remaining, 30.0)

    def _receive(self, connection, deadline: float, *, request: dict,
                 welcome: dict | None = None, expected_type: str | None = None, timings=None) -> dict:
        message = connection.recv(timeout=self._remaining(deadline))
        if timings is not None:
            timings.update(received_at_utc=_utc(), received_monotonic_ns=str(time.monotonic_ns()))
        validate_message(message)
        if message["correlation_id"] != request["message_id"]:
            raise TraderError("E_SCHEMA", "Response correlation does not match the request")
        if welcome is not None:
            if message["kernel_boot_id"] != welcome["kernel_boot_id"]:
                raise TraderError("E_BOOT_MISMATCH", "Snapshot crossed a kernel restart")
            if (message["account_key"], message["connection_id"]) != (welcome["account_key"], welcome["connection_id"]):
                raise TraderError("E_SCOPE", "Response is outside the negotiated connection scope")
        if message["type"] == "ProtocolError":
            raise TraderError(message["body"]["error_code"], "Kernel refused the requested operation")
        if expected_type is not None and message["type"] != expected_type:
            raise TraderError("E_SCHEMA", "Unexpected response message type")
        return message

    def _hello(self, connection, deadline: float) -> dict:
        request = self._message("Hello", {
            "client_instance_id": self.client_instance_id or _id("desktop"), "requested_role": "DESKTOP", "supported_versions": ["1.0"],
            "requested_account_key": self.expected_account_key, "client_build_id": "online-trader-" + __version__,
        })
        connection.send(request, timeout=self._remaining(deadline))
        welcome = self._receive(connection, deadline, request=request, expected_type="Welcome")
        if any(welcome[key] is None for key in ("connection_id", "kernel_boot_id", "account_key")):
            raise TraderError("E_AUTH", "Kernel did not bind the negotiated connection")
        if self.expected_account_key is not None and welcome['account_key']!=self.expected_account_key:
            raise TraderError('E_SCOPE','Endpoint differs from the selected account')
        if self.expected_kernel_boot_id is not None and welcome['kernel_boot_id']!=self.expected_kernel_boot_id:
            raise TraderError('E_BOOT_MISMATCH','Displayed owner has restarted; refresh before commanding')
        return welcome

    def workbench(self):
        return self._workbench('WorkbenchRequest', 'WorkbenchSnapshot', 'READ_STATUS')

    def workbench_monitor(self):
        return self._workbench('WorkbenchMonitorRequest', 'WorkbenchMonitorSnapshot', 'READ_DIAGNOSTIC')

    def workbench_positions(self, after=None):
        return self._workbench('WorkbenchPositionsRequest', 'WorkbenchPositionsSnapshot', 'READ_STATUS',
                               body={'after': after})

    def _workbench(self, request_type, response_type, capability, *, body=None):
        deadline=time.monotonic()+self.timeout
        timings = {'request_started_monotonic_ns':str(time.monotonic_ns())}
        with connect(self.pipe_name,timeout=self._remaining(deadline),expected_server_pid=self.expected_server_pid) as connection:
            welcome=self._hello(connection,deadline)
            if capability not in welcome['body']['capabilities']:
                if capability == 'READ_DIAGNOSTIC':
                    return None
                raise TraderError('E_AUTH','Read capability was not granted')
            request=self._message(request_type,{} if body is None else body,welcome)
            connection.send(request,timeout=self._remaining(deadline))
            response=self._receive(connection,deadline,request=request,welcome=welcome,expected_type=response_type,timings=timings)
            body=response['body']
            value=body['payload']
            if (content_hash(value)!=body['content_hash'] or value['account_key']!=welcome['account_key']
                    or value['kernel_boot_id']!=welcome['kernel_boot_id']):
                raise TraderError('E_SNAPSHOT','Workbench identity or content hash differs')
            self.last_metadata={'account_key':welcome['account_key'],'kernel_boot_id':welcome['kernel_boot_id'],
                'checkpoint_event_seq':value['checkpoint_event_seq'], 'reply_sent_at_utc':response['sent_at_utc'], **timings}
            return value

    def command(self,operation,scope,parameters,*,command_id=None,expected_config_revision=None):
        """One explicit attempt through the original context/hash/Ack chain.

        A timeout retains last_command for GetCommandStatus, never replays it.
        """
        deadline=time.monotonic()+self.timeout
        with connect(self.pipe_name,timeout=self._remaining(deadline),expected_server_pid=self.expected_server_pid) as connection:
            welcome=self._hello(connection,deadline)
            if not {'REQUEST_CONTEXT','OPERATOR_COMMAND'}<=set(welcome['body']['capabilities']):
                raise TraderError('E_AUTH','Command capability was not granted')
            ids=scope['scope_ids'];kind=scope['scope_type']
            scope_id=welcome['account_key'] if kind in {'ACCOUNT','OWNED_ORDERS'} else ids[0] if len(ids)==1 else content_hash(scope)
            request=self._message('ContextRequest',{'purpose':'OPERATOR_COMMAND','operation':operation,'scope_id':scope_id},welcome)
            connection.send(request,timeout=self._remaining(deadline))
            context=self._receive(connection,deadline,request=request,welcome=welcome,expected_type='RequestContext')['body']
            if context['scope_id']!=scope_id or context['purpose']!='OPERATOR_COMMAND' or context['kernel_epoch']!=welcome['body']['kernel_epoch']:
                raise TraderError('E_SCOPE','Command context differs from the negotiated scope')
            if (expected_config_revision is not None
                    and context['related_revisions']['config_revision'] != expected_config_revision):
                raise TraderError('E_REVISION_CONFLICT', 'The draft was prepared for a different strategy configuration')
            request=self._message('OperatorCommand',{'command_id':command_id or _id('desktop-command'),'context_id':context['context_id'],
                'kernel_epoch':context['kernel_epoch'],'expected_revisions':context['related_revisions'],'operation':operation,
                'scope':scope,'parameters':parameters,'payload_hash':'0'*64},welcome)
            request['body']['payload_hash']=payload_hash(request);validate_message(request)
            self.last_command={key:request['body'][key] for key in ('command_id','payload_hash')}
            connection.send(request,timeout=self._remaining(deadline))
            return self._receipt(connection,deadline,request,welcome,self.last_command)

    def _receipt(self,connection,deadline,request,welcome,identity):
        receipt=self._receive(connection,deadline,request=request,welcome=welcome,expected_type='CommandReceipt')['body']
        if any(receipt[key]!=identity[key] for key in ('command_id','payload_hash')):
            raise TraderError('E_DUPLICATE_CONFLICT','Command receipt identity differs')
        return receipt

    def command_status(self,identity):
        deadline=time.monotonic()+self.timeout
        with connect(self.pipe_name,timeout=self._remaining(deadline),expected_server_pid=self.expected_server_pid) as connection:
            welcome=self._hello(connection,deadline)
            request=self._message('GetCommandStatus',identity,welcome);validate_message(request)
            connection.send(request,timeout=self._remaining(deadline))
            return self._receipt(connection,deadline,request,welcome,identity)

    def snapshot(self) -> dict:
        """Return a complete projectionBatch, or raise without publishing partial data."""
        deadline = time.monotonic() + self.timeout
        with connect(self.pipe_name, timeout=self._remaining(deadline),
                     expected_server_pid=self.expected_server_pid) as connection:
            welcome = self._hello(connection, deadline)
            if "READ_STATUS" not in welcome["body"]["capabilities"]:
                raise TraderError("E_AUTH", "Status capability was not granted")
            subscription_id = _id("status")
            request = self._message("SnapshotRequest", {
                "subscription_id": subscription_id, "view": "STATUS", "resume_stream_seq": None,
                "resume_kernel_boot_id": None, "resume_projection_schema": None,
            }, welcome)
            connection.send(request, timeout=self._remaining(deadline))
            begin = self._receive(connection, deadline, request=request, welcome=welcome, expected_type="SnapshotBegin")["body"]
            if (begin["subscription_id"] != subscription_id or begin["projection_schema"] != "projection/1" or
                    int(begin["checkpoint_event_seq"]) > int(begin["durable_seq"]) or
                    not 1 <= begin["total_bytes"] <= MAX_SNAPSHOT_BYTES or not 1 <= begin["chunk_count"] <= MAX_CHUNKS):
                raise TraderError("E_SNAPSHOT", "Invalid snapshot boundary or size")
            chunks: dict[int, bytes] = {}
            received_bytes = 0
            end = None
            for _ in range(begin["chunk_count"] * 2 + 1):
                message = self._receive(connection, deadline, request=request, welcome=welcome)
                body = message["body"]
                if body.get("subscription_id") != subscription_id or body.get("snapshot_id") != begin["snapshot_id"]:
                    raise TraderError("E_SNAPSHOT", "Snapshot fragment identity mismatch")
                if message["type"] == "SnapshotEnd":
                    end = body
                    break
                if message["type"] != "SnapshotChunk":
                    raise TraderError("E_SNAPSHOT", "Unexpected snapshot fragment")
                index = body["chunk_index"]
                if index >= begin["chunk_count"]:
                    raise TraderError("E_SNAPSHOT", "Snapshot chunk index exceeds its declaration")
                try:
                    raw = base64.b64decode(body["data_base64"], validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise TraderError("E_SNAPSHOT", "Invalid snapshot Base64") from exc
                if (base64.b64encode(raw).decode("ascii") != body["data_base64"] or
                        not 1 <= len(raw) <= MAX_CHUNK_BYTES or len(raw) != body["raw_bytes"] or
                        hashlib.sha256(raw).hexdigest() != body["chunk_hash"]):
                    raise TraderError("E_SNAPSHOT", "Snapshot chunk length or hash mismatch")
                if index in chunks:
                    if chunks[index] != raw:
                        raise TraderError("E_SNAPSHOT", "Conflicting duplicate snapshot chunk")
                else:
                    received_bytes += len(raw)
                    if received_bytes > begin["total_bytes"]:
                        raise TraderError("E_SNAPSHOT", "Snapshot exceeds its declared length")
                    chunks[index] = raw
            if (end is None or len(chunks) != begin["chunk_count"] or
                    end["last_stream_seq"] != begin["last_stream_seq"] or received_bytes != begin["total_bytes"]):
                raise TraderError("E_SNAPSHOT", "Snapshot is incomplete or has inconsistent boundaries")
            raw = b"".join(chunks[index] for index in range(begin["chunk_count"]))
            if hashlib.sha256(raw).hexdigest() != end["snapshot_hash"]:
                raise TraderError("E_SNAPSHOT", "Snapshot content hash mismatch")
            projection = strict_loads(raw)
            if not _projection_validator().is_valid(projection) or canonical_bytes(projection) != raw:
                raise TraderError("E_SNAPSHOT", "Invalid snapshot projection or canonical encoding")
            records = projection["records"]
            for record in records:
                for key, value in record.items():
                    if key.endswith(("_seq", "_epoch", "_revision")) and type(value) is str and int(value) > 9_223_372_036_854_775_807:
                        raise TraderError("E_SNAPSHOT", "Projection revision exceeds the storage range")
            identities = [(record["record_type"], record["record_id"]) for record in records]
            accounts = [record for record in records if record["record_type"] == "ACCOUNT"]
            if (len(identities) != len(set(identities)) or len(accounts) != 1 or
                    accounts[0]["record_id"] != welcome["account_key"]):
                raise TraderError("E_SCOPE", "Projection has duplicate or mismatched account records")
            self.last_metadata = {
                "account_key": welcome["account_key"], "kernel_boot_id": welcome["kernel_boot_id"],
                "checkpoint_event_seq": begin["checkpoint_event_seq"], "durable_seq": begin["durable_seq"],
                "snapshot_id": begin["snapshot_id"], "received_at_utc": _utc(),
            }
            return projection

    def pause(self) -> dict:
        """Pause new entries only; returns the actual command stage, never a fill claim."""
        deadline = time.monotonic() + self.timeout
        with connect(self.pipe_name, timeout=self._remaining(deadline),
                     expected_server_pid=self.expected_server_pid) as connection:
            welcome = self._hello(connection, deadline)
            if not {"REQUEST_CONTEXT", "OPERATOR_COMMAND"} <= set(welcome["body"]["capabilities"]):
                raise TraderError("E_AUTH", "Pause capability was not granted")
            request = self._message("ContextRequest", {
                "purpose": "OPERATOR_COMMAND", "operation": "PAUSE_NEW_ENTRIES", "scope_id": welcome["account_key"],
            }, welcome)
            connection.send(request, timeout=self._remaining(deadline))
            context = self._receive(connection, deadline, request=request, welcome=welcome, expected_type="RequestContext")["body"]
            if (context["purpose"] != "OPERATOR_COMMAND" or context["scope_id"] != welcome["account_key"] or
                    context["kernel_epoch"] != welcome["body"]["kernel_epoch"]):
                raise TraderError("E_SCOPE", "Pause context does not match the negotiated kernel")
            command = self._message("OperatorCommand", {
                "command_id": _id("pause"), "context_id": context["context_id"],
                "kernel_epoch": context["kernel_epoch"], "expected_revisions": context["related_revisions"],
                "operation": "PAUSE_NEW_ENTRIES", "scope": {"scope_type": "ACCOUNT", "scope_ids": []},
                "parameters": {"cancel_owned_entry_remainders": False}, "payload_hash": "0" * 64,
            }, welcome)
            command["body"]["payload_hash"] = payload_hash(command)
            connection.send(command, timeout=self._remaining(deadline))
            receipt = self._receive(connection, deadline, request=command, welcome=welcome, expected_type="CommandReceipt")["body"]
            if (receipt["command_id"] != command["body"]["command_id"] or
                    receipt["payload_hash"] != command["body"]["payload_hash"]):
                raise TraderError("E_DUPLICATE_CONFLICT", "Command receipt identity mismatch")
            return receipt
