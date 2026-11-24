"""Bounded, same-owner Windows control endpoint; no inbound broker facts."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import queue
import threading
import time
import uuid

from online_trader.contracts.codec import encode_frame, content_hash
from online_trader.errors import TraderError
from online_trader.kernel.engine import Kernel
from online_trader.kernel.projection import projection_batch as kernel_projection_batch
from online_trader.kernel.streams import StreamHub
from online_trader.kernel.control import OPERATIONS
from online_trader.transport.named_pipe import PipeListener
from online_trader.runtime.processes import parent_pid


_STANDALONE_WORKBENCH_PUBLICATION_INTERVAL_NS = 750_000_000
_STANDALONE_ACCOUNT_PUBLICATION_INTERVAL_NS = 2_500_000_000


def _id():return uuid.uuid4().hex
def _utc():return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")


@dataclass(eq=False)
class _Peer:
    connection: object
    connection_id: str = field(default_factory=_id)
    peer_id: str = field(default_factory=_id)
    outgoing: queue.Queue = field(default_factory=lambda:queue.Queue(256))
    closed: threading.Event = field(default_factory=threading.Event)
    ready: bool = False
    role: str | None = None
    instance_id: str | None = None
    stream_key: tuple | None = None
    stream_ready: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    last_heartbeat: float = field(default_factory=time.monotonic)
    queued_bytes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def enqueue(self,message):
        size=len(encode_frame(message))
        with self.lock:
            if self.queued_bytes+size>16*1024*1024:raise TraderError("E_QUEUE_OVERFLOW","Peer output queue is full")
            self.outgoing.put_nowait((message,size));self.queued_bytes+=size

    def close(self):
        self.closed.set();self.connection.close()


class ControlServer:
    def __init__(self,kernel:Kernel,pipe_name:str, *, trusted_client_pid=None):
        self.kernel=kernel;self.listener=PipeListener(pipe_name)
        self._target_owner = getattr(kernel, 'runtime_protocol', None) == 'DSV_TARGET/1'
        self._trusted_client_pid = trusted_client_pid
        self._inbound=queue.Queue(256);self._stop=threading.Event();self._peers=set();self._workers=[];self._waiting=[]
        self._peers_lock=threading.Lock();self._queue_lock=threading.Lock();self._inbound_bytes=0
        self._registered={}
        self.streams=StreamHub(kernel.boot_id,kernel.account_key)
        snap=kernel.snapshot()
        boundary = self._boundary(snap)
        self.streams.update(self._projection(snap),boundary,boundary)
        kernel.take_committed_views()
        if self._target_owner and hasattr(kernel, 'set_stream_tracking'):
            kernel.set_stream_tracking(False)
        self._sync_streams_force = False
        self._stream_peers={}
        self._last_progress_utc=_utc()
        self._last_progress_seq=int(boundary)

    def _projection(self, snapshot):
        if self._target_owner:
            from online_trader.dsv_runtime.standalone_projection import projection_batch
            return projection_batch(snapshot)
        return kernel_projection_batch(snapshot)

    def _boundary(self, snapshot=None):
        if self._target_owner:
            state = self.kernel.snapshot() if snapshot is None else snapshot
            return str(max(1, int(state.get('projection_seq') or state.get('sequence') or 0)))
        return str(self.kernel._durable_seq if snapshot is None else snapshot['durable_seq'])

    def register_peer(self,instance_id,pid,role="SHADOW"):
        self.kernel._check_owner()
        if role!="SHADOW":raise TraderError("E_AUTH","Only a read-only shadow can be registered")
        self._registered[instance_id]=(pid,role)

    def _peer_snapshot(self):
        with self._peers_lock:return tuple(self._peers)

    def _publish_workbench(self, publication, *, standalone=False, native_snapshot=None):
        if self._target_owner:
            publication.publish(self.kernel.prepare_workbench_publication)
            self.kernel.publication_status = dict(publication.status)
            return
        from online_trader.kernel.workbench import prepare_workbench_publication, workbench_publication

        if standalone:
            publication.publish(lambda: prepare_workbench_publication(self.kernel, native_snapshot=native_snapshot))
        else:
            publication.publish(lambda: workbench_publication(self.kernel))
        diagnostics = getattr(self.kernel, '_dsv_diagnostics', None)
        if diagnostics is not None:
            if publication.status['error_code']:
                diagnostics['workbench_publication'] = publication.status.copy()
            else:
                diagnostics.pop('workbench_publication', None)

    def _reply(self,peer,request,kind,body):
        return {"protocol_version":"1.0","type":kind,"message_id":_id(),"connection_id":peer.connection_id,
            "correlation_id":None if request is None else request["message_id"],"sent_at_utc":_utc(),"kernel_boot_id":self.kernel.boot_id,"account_key":self.kernel.account_key,"body":body}

    def _reader(self,peer):
        deadline=time.monotonic()+5
        try:
            while not self._stop.is_set() and not peer.closed.is_set():
                if not peer.ready and time.monotonic()>deadline:break
                try:message=peer.connection.recv(timeout=5)
                except TraderError as exc:
                    if exc.code in {"E_TIMEOUT","E_PIPE_TIMEOUT"}:continue
                    break
                size=len(encode_frame(message))
                with self._queue_lock:
                    if self._inbound_bytes+size>8*1024*1024:break
                    try:self._inbound.put_nowait((peer,message,size))
                    except queue.Full:break
                    self._inbound_bytes+=size
        finally:peer.close()

    def _writer(self,peer):
        try:
            while not self._stop.is_set() and not peer.closed.is_set():
                try:message,size=peer.outgoing.get(timeout=0.1)
                except queue.Empty:continue
                try:peer.connection.send(message,timeout=5)
                finally:
                    with peer.lock:peer.queued_bytes-=size
        except (TraderError,OSError):pass
        finally:peer.close()

    def _acceptor(self):
        while not self._stop.is_set():
            try:connection=self.listener.accept(timeout=0.25)
            except TraderError as exc:
                if exc.code in {"E_TIMEOUT","E_PIPE_TIMEOUT"}:continue
                if self._stop.is_set():break
                continue
            except OSError:
                if self._stop.is_set():break
                continue
            trusted_pid = self._trusted_client_pid() if callable(self._trusted_client_pid) else self._trusted_client_pid
            if trusted_pid is not None and (trusted_pid <= 0 or (
                    connection.peer_pid != trusted_pid and parent_pid(connection.peer_pid) != trusted_pid)):
                connection.close()
                continue
            peer=_Peer(connection)
            with self._peers_lock:
                if len(self._peers)>=16:
                    peer.close();continue
                self._peers.add(peer)
            for target in (self._reader,self._writer):
                worker=threading.Thread(target=target,args=(peer,),daemon=True);worker.start()
                with self._peers_lock:self._workers.append(worker)

    def _handle(self,peer,message):
        kind,body=message["type"],message["body"]
        if not peer.ready:
            if kind!="Hello":raise TraderError("E_AUTH","Handshake is required")
            actual_pid=peer.connection.peer_pid
            launcher_pid=parent_pid(actual_pid)
            registered=self._registered.get(body["client_instance_id"])
            bound_roles={role for pid,role in self._registered.values() if actual_pid==pid or launcher_pid==pid}
            if body["requested_role"]=="SHADOW":
                if not registered or registered[1]!="SHADOW" or registered[0] not in {actual_pid,launcher_pid}:raise TraderError("E_AUTH","Shadow process is not registered")
                peer.role="SHADOW"
            elif body["requested_role"]=="DESKTOP":
                if "SHADOW" in bound_roles:raise TraderError("E_AUTH","Registered strategy process cannot claim desktop authority")
                peer.role="DESKTOP"
            else:raise TraderError("E_AUTH","Role is not enabled on this endpoint")
            if body["requested_account_key"] not in {None,self.kernel.account_key}:raise TraderError("E_SCOPE","Account is not bound to this endpoint")
            if "1.0" not in body["supported_versions"]:raise TraderError("E_VERSION","No supported protocol")
            peer.instance_id=body["client_instance_id"]
            peer.ready=True
            peer.last_seen=time.monotonic()
            capabilities = ["READ_STATUS", "READ_STREAM"]
            if peer.role == "DESKTOP":
                capabilities.extend(("REQUEST_CONTEXT", "OPERATOR_COMMAND"))
                if getattr(self.kernel, '_dsv_market_monitor', None) is not None:
                    capabilities.append("READ_DIAGNOSTIC")
            peer.enqueue(self._reply(peer,message,"Welcome",{"negotiated_version":"1.0","peer_id":peer.peer_id,"granted_role":peer.role,
                "kernel_epoch":str(self.kernel.kernel_epoch),"capabilities":capabilities,
                "max_frame_bytes":1048576,"heartbeat_interval_ms":5000,"peer_liveness_timeout_ms":15000,"server_build_id":"online-trader-g1"}))
            return
        if (message["connection_id"],message["account_key"],message["kernel_boot_id"])!=(peer.connection_id,self.kernel.account_key,self.kernel.boot_id):
            raise TraderError("E_SCOPE","Connection scope changed")
        if peer.role=="SHADOW" and kind not in {"SnapshotRequest","HeartbeatAck"}:
            raise TraderError("E_AUTH","Shadow has no control or order capability")
        peer.last_seen=time.monotonic()
        if kind=="SnapshotRequest":
            if self._target_owner:
                if hasattr(self.kernel, 'set_stream_tracking'):
                    self.kernel.set_stream_tracking(True)
                self._sync_streams_force = True
            try:
                self._sync_streams()
            finally:
                self._sync_streams_force = False
            key,messages=self.streams.request(peer.instance_id or peer.peer_id,body)
            if peer.stream_key is not None and peer.stream_key!=key:self._detach(peer)
            previous=self._stream_peers.get(key)
            if previous is not None and previous is not peer:previous.close()
            peer.stream_key=key;self._stream_peers[key]=peer
            peer.stream_ready=not any(kind=="Gap" for kind,_ in messages)
            for response_type,response_body in messages:peer.enqueue(self._reply(peer,message,response_type,response_body))
            self._heartbeat(peer)
        elif kind=='WorkbenchRequest':
            if self._target_owner:
                value = self.kernel.workbench()
            else:
                from online_trader.kernel.workbench import workbench_view
                value=workbench_view(self.kernel)
            peer.enqueue(self._reply(peer,message,'WorkbenchSnapshot',{'payload':value,'content_hash':content_hash(value)}))
        elif kind == 'WorkbenchPositionsRequest':
            if self._target_owner:
                value = self.kernel.positions(body['after'])
                value = dict(value, positions=[
                    {key: row[key] for key in ('position_cycle_id', 'contract_id', 'direction', 'remaining_quantity')}
                    for row in value.get('positions', ())
                ])
            else:
                from online_trader.kernel.workbench import workbench_positions
                value = workbench_positions(self.kernel, body['after'])
            peer.enqueue(self._reply(peer, message, 'WorkbenchPositionsSnapshot',
                                    {'payload': value, 'content_hash': content_hash(value)}))
        elif kind == 'WorkbenchMonitorRequest':
            if peer.role != 'DESKTOP' or getattr(self.kernel, '_dsv_market_monitor', None) is None:
                raise TraderError('E_AUTH', 'Read diagnostics capability was not granted')
            from online_trader.kernel.workbench import workbench_monitor_view
            value = workbench_monitor_view(self.kernel)
            peer.enqueue(self._reply(peer, message, 'WorkbenchMonitorSnapshot',
                                    {'payload': value, 'content_hash': content_hash(value)}))
        elif kind=="ContextRequest":
            if body["purpose"]!="OPERATOR_COMMAND" or body["operation"] not in OPERATIONS:
                raise TraderError("E_AUTH","Context purpose is not an enabled control capability")
            context=self.kernel.contexts.issue(peer.peer_id,body["purpose"],body["operation"],body["scope_id"],
                kernel_epoch=str(self.kernel.kernel_epoch),revisions=self.kernel.revisions,issued_event_seq=self._boundary())
            peer.enqueue(self._reply(peer,message,"RequestContext",context))
        elif kind=="OperatorCommand":
            if len(self._waiting)>=256:raise TraderError("E_QUEUE_OVERFLOW","Pending control capacity reached")
            if self._trusted_client_pid is None:
                result = self.kernel.execute_command(message, peer.peer_id)
            else:
                result = self.kernel.execute_command(message, peer.peer_id, operator_actor=peer.instance_id)
            self._waiting.append((peer,message,result))
        elif kind=="GetCommandStatus":
            if len(self._waiting)>=256:raise TraderError("E_QUEUE_OVERFLOW","Pending control capacity reached")
            result=self.kernel.control.get_status(body["command_id"],body["payload_hash"])
            self._waiting.append((peer,message,result))
        elif kind=="HeartbeatAck":
            high=self.streams.last_stream_seq(peer.stream_key) if peer.stream_key else 0
            event_high = int(self._boundary()) if self._target_owner else self.kernel._sequence
            if int(body["last_seen_stream_seq"])>high or int(body["last_seen_event_seq"])>event_high:
                raise TraderError("E_SCHEMA","Acknowledgement exceeds the emitted boundary")
        else:raise TraderError("E_AUTH","Message is not an enabled desktop capability")

    def _detach(self,peer):
        key=peer.stream_key
        if key is not None:
            if self._stream_peers.get(key) is peer:
                self._stream_peers.pop(key,None);self.streams.detach(key)
            peer.stream_key=None
            peer.stream_ready=False
        if self._target_owner and not self._stream_peers and not self.streams._streams and hasattr(self.kernel, 'set_stream_tracking'):
            self.kernel.set_stream_tracking(False)

    def _heartbeat(self,peer):
        if peer.stream_key is None or not peer.stream_ready or peer.closed.is_set():return
        snap=self.kernel._make_snapshot()
        boundary = self._boundary()
        high=self.streams.last_stream_seq(peer.stream_key)
        peer.enqueue(self._reply(peer,None,"Heartbeat",{"kernel_epoch":str(self.kernel.kernel_epoch),
            "business_advance_seq":str(self._last_progress_seq),"last_event_seq":boundary,
            "durable_seq":boundary,"last_stream_seq":str(high),
            "pending_transactions":len(self.kernel._pending),"critical_queue_depth":self._inbound.qsize(),
            "kernel_state":self._projection(self.kernel.snapshot() if self._target_owner else snap)["records"][0]["kernel_state"],
            "last_progress_utc":self._last_progress_utc}))
        peer.last_heartbeat=time.monotonic()

    def _sync_streams(self):
        force = getattr(self, '_sync_streams_force', False)
        if self._target_owner and not force and not self._stream_peers and not self.streams._streams:
            self.kernel.take_committed_views()
            return
        overflow,views=self.kernel.take_committed_views()
        if self._target_owner and force and not views:
            current_boundary = int(self._boundary())
            if current_boundary > self.streams.durable_seq:
                views = [self.kernel.snapshot()]
        if overflow:
            for key,peer in tuple(self._stream_peers.items()):
                kind,body=self.streams.invalidate(key,"QUEUE_OVERFLOW")
                peer.stream_ready=False
                try:peer.enqueue(self._reply(peer,None,kind,body))
                except (TraderError,queue.Full):peer.close()
        for view in views:
            boundary = self._boundary(view)
            updates=self.streams.update(self._projection(view),boundary,boundary)
            self._last_progress_seq=int(boundary);self._last_progress_utc=_utc()
            for key,messages in updates.items():
                peer=self._stream_peers.get(key)
                if peer is None or peer.closed.is_set():continue
                for kind,body in messages:
                    if kind=="Gap":peer.stream_ready=False
                    try:peer.enqueue(self._reply(peer,None,kind,body))
                    except (TraderError,queue.Full):
                        self.streams.invalidate(key,"QUEUE_OVERFLOW");peer.close();break

    def _error(self,peer,message,error,*,action="KEEP"):
        known={"E_AUTH","E_SCOPE","E_VERSION","E_SCHEMA","E_CONTEXT_EXPIRED","E_BOOT_MISMATCH","E_EPOCH_REVOKED","E_REVISION_CONFLICT",
            "E_DUPLICATE_CONFLICT","E_NOT_READY","E_RULE_UNAPPROVED","E_STORAGE","E_UNSUPPORTED","E_OWNERSHIP","E_QUEUE_OVERFLOW","E_FACT_BARRIER","E_RISK"}
        mapped={
            "E_DSV_COMMAND":"E_SCHEMA",
            "E_DSV_SCOPE":"E_SCOPE",
            "E_DSV_SDK_CLOCK":"E_NOT_READY",
            "E_DSV_SYNC":"E_NOT_READY",
            "E_DSV_NATIVE_ID":"E_OWNERSHIP",
            "E_DSV_OWNERSHIP":"E_REVISION_CONFLICT",
            "E_CAPABILITY":"E_NOT_READY",
            "E_CONFIG_EDIT":"E_REVISION_CONFLICT",
            "E_CONFIG_FILE":"E_STORAGE",
        }
        raw_code=getattr(error,"code","E_INTERNAL")
        code=mapped.get(raw_code,raw_code)
        code=code if code in known else "E_INTERNAL"
        reason=(str(error)[:512] if isinstance(error,TraderError) and str(error)
                else "Request refused; inspect current status and operation scope")
        peer.enqueue(self._reply(peer,message,"ProtocolError",{"error_code":code,"safe_reason":reason,
            "related_message_id":message["message_id"],"connection_action":action}))

    def _fail_closed_owner(self,error):
        session=getattr(self.kernel,"session",None)
        if session is not None:
            session._failed=True
        elif hasattr(self.kernel,"_failed"):
            self.kernel._failed=True
            if hasattr(self.kernel,"_armed"):
                self.kernel._armed=False
        try:
            self.kernel.contexts.revoke_all(reason="OWNER_INVARIANT_FAILED")
        except Exception:
            pass
        if hasattr(self.kernel,"stop_event"):
            self.kernel.stop_event.set()
        self._stop.set()
        logging.getLogger(__name__).error("Owner invariant failure in ControlServer: %s",
                                          getattr(error,"code",type(error).__name__))

    def _drain_outgoing(self,timeout=1):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.kernel.poll();self._sync_streams()
            self._service_inbound(closing=True);self._flush_receipts()
            pending=bool(self._waiting or self.kernel._pending or not self._inbound.empty())
            for peer in self._peer_snapshot():
                with peer.lock:
                    # A Welcome already accepted before stop may still be in
                    # transit when its SnapshotRequest arrives. Keep the same
                    # bounded drain window for that negotiated read round trip.
                    pending=pending or (not peer.closed.is_set() and (peer.queued_bytes>0 or peer.stream_key is None))
            if not pending:return
            time.sleep(.005)

    def _service_inbound(self,*,closing=False):
        # Bounded batches keep a busy status reader from starving the owner.
        invariant_codes={"E_DSV_STORE","E_DSV_OWNER","E_DSV_SDK_OWNER","E_STORAGE","E_OWNER","E_CLOSED"}
        for _ in range(32):
            try:peer,message,size=self._inbound.get_nowait()
            except queue.Empty:break
            with self._queue_lock:self._inbound_bytes-=size
            if peer.closed.is_set():continue
            try:
                if closing and message['type'] not in {'Hello','SnapshotRequest','GetCommandStatus','HeartbeatAck'}:
                    raise TraderError('E_NOT_READY','Execution owner is closing; no new command is accepted')
                self._handle(peer,message)
            except TraderError as error:
                fatal=error.code in invariant_codes
                try:self._error(peer,message,error,action="CLOSE" if fatal else "KEEP")
                except Exception:peer.close()
                if fatal:
                    peer.close()
                    self._fail_closed_owner(error)
                    break
            except Exception as error:
                try:self._error(peer,message,error,action="CLOSE")
                except Exception:pass
                peer.close()

    def _flush_receipts(self):
        invariant_codes={"E_DSV_STORE","E_DSV_OWNER","E_DSV_SDK_OWNER","E_STORAGE","E_OWNER","E_CLOSED"}
        remaining=[]
        for peer,message,result in self._waiting:
            if peer.closed.is_set():continue
            if not result.done():remaining.append((peer,message,result));continue
            try:peer.enqueue(self._reply(peer,message,'CommandReceipt',result.result()))
            except TraderError as error:
                fatal=error.code in invariant_codes
                try:self._error(peer,message,error,action="CLOSE" if fatal else "KEEP")
                except Exception:peer.close()
                if fatal:
                    peer.close()
                    self._fail_closed_owner(error)
            except Exception as error:
                try:self._error(peer,message,error,action="CLOSE")
                except Exception:pass
                peer.close()
        self._waiting=remaining

    def serve(self,stop_event=None,*,owner_tick=None,publish_workbench=False):
        acceptor=threading.Thread(target=self._acceptor,daemon=True);acceptor.start()
        publication = accounts = None
        standalone = self._trusted_client_pid is not None
        next_workbench_publication_ns = next_account_publication_ns = 0
        native_snapshot = None
        try:
            if standalone:
                from online_trader.runtime.account_publication import AccountPublication

                try:
                    accounts = AccountPublication(self.listener.name, self.kernel.account_key, self._trusted_client_pid)
                except (OSError, TraderError) as error:
                    logging.getLogger(__name__).warning('E_ACCOUNT_SOURCE_BIND: %s', getattr(error, 'code', 'OSError'))
            if publish_workbench:
                from online_trader.transport.workbench_snapshot import WorkbenchPublication
                try:
                    if standalone:
                        publication = WorkbenchPublication(self.listener.name, interval_ns=0, deferred=True)
                    else:
                        publication = WorkbenchPublication(self.listener.name)
                except (OSError, TraderError):
                    # The original control endpoint remains available.
                    pass
            while not self._stop.is_set() and not (stop_event and stop_event.is_set()):
                self.kernel.poll()
                self._sync_streams()
                self._service_inbound();self._flush_receipts()
                if publication is not None and not standalone:
                    self._publish_workbench(publication)
                if owner_tick is not None:owner_tick()
                if standalone:
                    now_ns = time.monotonic_ns()
                    if accounts is not None and now_ns >= next_account_publication_ns:
                        native_snapshot = accounts.capture(self.kernel)
                        next_account_publication_ns = now_ns + _STANDALONE_ACCOUNT_PUBLICATION_INTERVAL_NS
                    if publication is not None and now_ns >= next_workbench_publication_ns:
                        self._publish_workbench(publication, standalone=True, native_snapshot=native_snapshot)
                        next_workbench_publication_ns = now_ns + _STANDALONE_WORKBENCH_PUBLICATION_INTERVAL_NS
                self._sync_streams()
                self._service_inbound(closing=bool(self._stop.is_set() or (stop_event and stop_event.is_set())))
                self._flush_receipts()
                for peer in self._peer_snapshot():
                    if peer.closed.is_set():
                        self._detach(peer)
                        self.kernel.contexts.revoke_peer(peer.peer_id)
                        with self._peers_lock:self._peers.discard(peer)
                    elif peer.ready:
                        now=time.monotonic()
                        if now-peer.last_seen>15:peer.close()
                        elif now-peer.last_heartbeat>=5:
                            try:self._heartbeat(peer)
                            except (TraderError,queue.Full):peer.close()
                with self._peers_lock:self._workers=[worker for worker in self._workers if worker.is_alive()]
                self._stop.wait(.01)
        finally:
            if accounts is not None:
                accounts.close()
            if publication is not None:
                publication.close()
            try:
                self._drain_outgoing()
            finally:
                self._stop.set();self.listener.close()
                for peer in self._peer_snapshot():peer.close()
                acceptor.join(timeout=2)
                for worker in self._workers:worker.join(timeout=1)
