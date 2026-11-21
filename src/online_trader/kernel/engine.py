"""G1 coordinator: ordered commits, uncertain quantities and kernel-owned protection.

The installed entry point never enables development authority. Tests inject an external
recording port; this module does not construct a broker connection or local matcher.
"""

from collections import deque
from concurrent.futures import Future
import copy
from dataclasses import dataclass
from decimal import Decimal, localcontext
import hashlib
from pathlib import Path
import threading
import time
import uuid

from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.contracts.context import ContextRegistry
from online_trader.contracts.validation import validate_plan
from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_value, decimal_text, identifier, order_price, quantity
from online_trader.errors import DefinitelyNotSentError, TraderError, exception_record
from online_trader.execution.policy import committed_limit_is_safe
from online_trader.gateway.ports import UnavailablePort, OnlineSimulationPort
from online_trader.ledger.store import Ledger
from online_trader.protection import initial_state, reduce_event, ProtectionState
from online_trader.runtime.instance import AccountLock


def _id(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex


@dataclass
class _Pending:
    sequence: int
    revision: int
    future: Future
    cycle: dict | None
    after: object = None
    tx_id: str | None = None
    authority: dict | None = None
    cycles: list | None = None
    binding: dict | None = None


class Kernel:
    def __init__(self, path, account_key: str, *, development: bool = False,
                 port=None, risk_budget: str = "0", tick_size: str = "1",
                 multiplier: str = "1", max_quote_age_ms: int = 30_000, simulation_grant=None, _execution_lock=None,
                 _archive_source=None, _existing_ledger=False, _legacy_stream=True) -> None:
        self._owner = threading.get_ident()
        self._legacy_stream = _legacy_stream
        self.account_key = identifier(account_key)
        self.boot_id = _id("boot")
        self.development = development
        self.port = port if port is not None else UnavailablePort()
        self.simulation=None
        self._owns_lock = True
        if simulation_grant is not None:
            from online_trader.online_scope import SimulationGrant,DsvSimulationGrant
            scope_path = Path(path).resolve()
            if _archive_source is not None:
                from online_trader.dsv_runtime.legacy_recovery import ArchivedSimNowPort
                source = Path(_archive_source).resolve()
                if (type(self.port) is not ArchivedSimNowPort or _execution_lock is None
                        or scope_path.samefile(source)):
                    raise TraderError('E_ONLINE_SCOPE', 'Historical recovery needs an isolated ledger and a read-only archive port')
                scope_path = source
                self._owns_lock = False
            if getattr(simulation_grant,'automatic_offsets',False):
                raise TraderError('E_AUTO_OFFSETS','An automatic source scope must be calculated before attaching the execution owner')
            if (type(simulation_grant) not in (SimulationGrant,DsvSimulationGrant) or development or not isinstance(self.port,OnlineSimulationPort)
                    or simulation_grant.account_key!=account_key or scope_path!=Path(simulation_grant.value['ledger_path']).resolve()):
                raise TraderError('E_ONLINE_SCOPE','Online simulation requires its explicit grant, ledger and dedicated adapter')
        elif isinstance(self.port,OnlineSimulationPort):
            raise TraderError('E_ONLINE_SCOPE','An online adapter cannot be attached to a development or default kernel')
        self.budget = decimal_value(risk_budget)
        if simulation_grant is not None and type(simulation_grant) is DsvSimulationGrant and self.budget!=decimal_value(simulation_grant.value['risk_budget']):
            raise TraderError('E_ONLINE_SCOPE','Kernel risk budget must equal the original finite DSV scope')
        self.tick_size, self.multiplier = tick_size, decimal_value(multiplier, positive=True)
        self.max_quote_age_ms = quantity(max_quote_age_ms, positive=True)
        if _execution_lock is not None and (type(_execution_lock) is not AccountLock or not _execution_lock.matches(account_key)):
            raise TraderError('E_INSTANCE_LOCK','The original connection lease does not match this owner/account')
        self._lock = _execution_lock if _execution_lock is not None else AccountLock(account_key)
        self._closed = False
        try:
            self.ledger = Ledger(path, account_key,
                broker_identity=getattr(simulation_grant, 'provider', 'TQKQ') if simulation_grant is not None else 'development',
                existing_only=_existing_ledger)
            ack = self.ledger.bootstrap(self.boot_id, hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        except BaseException:
            if hasattr(self,"ledger"):
                try:self.ledger.close()
                except Exception:pass
            if self._owns_lock:self._lock.close()
            raise
        try:
            self._restore(ack)
            if simulation_grant is not None:
                from online_trader.kernel.simulation import OnlineSimulation
                if type(simulation_grant) is DsvSimulationGrant:
                    from online_trader.kernel.online_dsv import OnlineDsvSimulation
                    self.simulation=OnlineDsvSimulation(self,simulation_grant)
                else:self.simulation=OnlineSimulation(self,simulation_grant)
        except BaseException:
            try:self.ledger.close()
            finally:
                if self._owns_lock:self._lock.close()
            raise

    def _restore(self,ack):
        self._sequence = self._durable_seq = ack.event_seq
        self.kernel_epoch = self.ledger.query("SELECT kernel_epoch FROM kernel_boots WHERE kernel_boot_id=?", (self.boot_id,))[0]["kernel_epoch"]
        auth = self.ledger.query("SELECT * FROM authority")[0]
        self._config_revision=auth["config_revision"]
        self._configuration = self.ledger.query("SELECT payload_hash,rule_set_id FROM config_revisions WHERE config_revision=?",(self._config_revision,))[0]
        self._state_revision = auth["state_revision"]
        self._risk_revision = auth["risk_revision"]
        self._authority_revision = auth["authority_revision"]
        self._committed_authority={"armed":False,"state_revision":self._state_revision,"risk_revision":self._risk_revision,"authority_revision":self._authority_revision,"config_revision":self._config_revision}
        self.active_epoch = auth["strategy_epoch"]
        self._armed = False
        self._failed = False
        self._pending = deque()
        self.commit_wait_ns = 0
        self._staged: dict[str, dict] = {}
        self._committed: dict[str, dict] = {}
        self._facts: dict[str, str] = {}
        self._money_fact_meta: dict[str, dict] = {}
        self._decision_keys: set[str] = set()
        self._dispatched: set[str] = set()
        self._errors: list[str] = []
        self._entry_gates={}
        self._in_poll=False
        self._command_binding=None
        self._maintenance_state=None
        self._commit_views=deque()
        self._commit_view_bytes=0
        self._commit_view_overflow=False
        self.contexts = ContextRegistry(self.boot_id, self.account_key)
        for row in self.ledger.query("SELECT payload_json FROM event_journal ORDER BY event_seq"):
            body = strict_loads(row["payload_json"])
            data = body.get("data", body)
            if "cycle" in data:
                self._committed[data["cycle"]["position_cycle_id"]] = data["cycle"]
            for cycle in data.get("cycles",[]):
                self._committed[cycle["position_cycle_id"]]=cycle
            if data.get("fill"):
                fill = data["fill"]
                self._facts[fill["trade_key"]] = fill["fact_hash"]
                self._money_fact_meta[fill["trade_key"]] = fill
        for row in self.ledger.query("SELECT decision_key FROM order_intents WHERE effect='OPEN'"):
            self._decision_keys.add(row["decision_key"])
        from online_trader.ledger.dsv_rollback import read_handoffs
        self._dsv_rollback = tuple(read_handoffs(self.ledger, self.account_key))
        current_orders = {r["child_order_id"]: r for r in self.ledger.query(
            "SELECT c.child_order_id,a.send_state FROM child_orders c JOIN send_attempts a ON a.child_order_id=c.child_order_id")}
        for cycle in self._committed.values():
            for order in cycle["orders"]:
                if order["child_order_id"] in current_orders:
                    order["send_state"] = current_orders[order["child_order_id"]]["send_state"]
        self._staged = copy.deepcopy(self._committed)
        self._recovery_pending = {cid for cid,c in self._committed.items() if c['lifecycle']!='CLOSED'}
        self.recovery_required = bool(self._recovery_pending)
        from online_trader.kernel.control import ControlProtocol
        self.control=ControlProtocol(self)
        from online_trader.kernel.configuration import ConfigurationCatalog
        self.configurations=ConfigurationCatalog(self)
        from online_trader.kernel.execution import ExecutionCoordinator
        self.execution=ExecutionCoordinator(self)
        from online_trader.kernel.statistics import StatisticsOwner
        self.statistics=StatisticsOwner(self)
        from online_trader.kernel.money import MoneyOwner
        self.money=MoneyOwner(self)

    def _check_owner(self) -> None:
        if threading.get_ident() != self._owner:
            raise TraderError("E_OWNER", "State mutation requires the kernel owner thread")
        if self._closed:
            raise TraderError("E_CLOSED", "Kernel is closed")

    def _writable(self) -> None:
        self._check_owner()
        if self._failed:
            raise TraderError("E_STORAGE", "Storage failed; reconciliation is required")

    @property
    def revisions(self) -> dict:
        return {"state_revision": str(self._state_revision), "risk_revision": str(self._risk_revision),
                "config_revision": str(self._config_revision), "authority_revision": str(self._authority_revision)}

    def _authority(self) -> dict:
        return {"armed": self._armed, "state_revision": self._state_revision,
                "risk_revision": self._risk_revision, "authority_revision": self._authority_revision,"config_revision":self._config_revision}

    def _submit(self, kind, event_type, data, mutate, *, cycle=None, cycles=None, after=None, tx_id=None) -> Future:
        self._writable()
        sequence=self._sequence+1
        tx_id=tx_id or _id("tx")
        binding=self._command_binding
        if binding is not None and binding.get("bound"):binding=None
        if binding is not None:
            tx_id,data,mutate,after=self.control.bind_submission(binding,sequence,tx_id,data,mutate,after)
        if hasattr(self,'money'):
            frozen_money_data=copy.deepcopy(data);original_after=after
            def accept_money():
                self.money.accept(frozen_money_data,sequence)
                if original_after is not None:original_after()
            after=accept_money
        revision = cycle["revision"] if cycle is not None else self._state_revision
        future = self.ledger.submit(kind, event_type, data, mutate,
                                    event_seq=sequence, expected_boot_id=self.boot_id,
                                    state_revision=revision,tx_id=tx_id)
        self._sequence=sequence
        self._pending.append(_Pending(sequence,revision,future,copy.deepcopy(cycle),after,tx_id,
            copy.deepcopy(data.get("authority",self._authority())),copy.deepcopy(cycles),binding))
        return future

    def poll(self) -> int:
        self._check_owner()
        if self._in_poll:return 0
        self._in_poll=True
        try:
            scheduler=getattr(self,'_scheduled_runtime',None)
            if scheduler is not None:scheduler.preflight()
            count=self._poll_commits()
            if scheduler is not None:scheduler.poll()
            return count
        finally:self._in_poll=False

    def _quote_age_ns(self,received):
        scheduler=getattr(self,'_scheduled_runtime',None)
        return scheduler.quote_age_ns(received) if scheduler is not None else max(0,time.monotonic_ns()-int(received))

    def _poll_commits(self):
        count = 0
        while self._pending and self._pending[0].future.done():
            pending = self._pending.popleft()
            try:
                ack = pending.future.result()
                if self._failed:raise TraderError("E_STORAGE","An earlier required commit failed")
                if (ack.kernel_boot_id != self.boot_id or ack.tx_id!=pending.tx_id or ack.event_seq != pending.sequence
                        or ack.durable_seq != pending.sequence or ack.state_revision != pending.revision
                        or ack.outcome!="COMMITTED" or pending.sequence != self._durable_seq + 1):
                    raise TraderError("E_COMMIT_ACK", "Commit identity or sequence mismatch")
                self._durable_seq = ack.durable_seq
                if pending.cycle is not None:
                    self._committed[pending.cycle["position_cycle_id"]] = pending.cycle
                for cycle in pending.cycles or []:
                    self._committed[cycle["position_cycle_id"]]=cycle
                if pending.authority is not None:self._committed_authority=pending.authority
                if pending.after is not None:
                    pending.after()
                if self._legacy_stream:
                    self._append_committed_view()
            except BaseException as exc:
                self._armed = False
                self._failed = True
                self.contexts.revoke_all(reason='STORAGE_COMMIT_FAILED')
                self._entry_gates.clear()
                self._errors.append(getattr(exc, "code", "E_STORAGE"))
                self._errors=self._errors[-64:]
                if pending.binding:
                    result=pending.binding.get("result")
                    if result is not None and not result.done():result.set_exception(TraderError("E_STORAGE","Required command transaction failed"))
            count += 1
        if not self._failed:self.control.poll()
        if hasattr(self,"configurations"):self.configurations.poll()
        if hasattr(self,"_dsv_runtime"):self._dsv_runtime.poll()
        if hasattr(self,'money'):self.money.poll()
        return count

    def _append_committed_view(self):
        view=self._make_snapshot()
        size=len(canonical_bytes(view))
        while self._commit_views and (len(self._commit_views)>=1024 or self._commit_view_bytes+size>16*1024*1024):
            _,old_size=self._commit_views.popleft();self._commit_view_bytes-=old_size;self._commit_view_overflow=True
        if size>16*1024*1024:
            self._commit_view_overflow=True
            return
        self._commit_views.append((view,size));self._commit_view_bytes+=size

    def take_committed_views(self):
        self._check_owner()
        overflow=self._commit_view_overflow
        result=[view for view,_ in self._commit_views]
        self._commit_views.clear();self._commit_view_bytes=0;self._commit_view_overflow=False
        return overflow,result

    def wait_idle(self, timeout: float = 5) -> None:
        if self._pending:
            started = time.monotonic_ns()
            end = time.monotonic() + timeout
            try:
                while self._pending:
                    self.poll()
                    if time.monotonic() >= end:
                        raise TraderError("E_TIMEOUT", "Commit queue did not drain")
                    time.sleep(0.001)
            finally:
                self.commit_wait_ns += time.monotonic_ns() - started
        if self._failed:
            raise TraderError("E_STORAGE", "A required transaction failed")

    def snapshot(self) -> dict:
        self._check_owner()
        self.poll()
        return self._make_snapshot()

    def _make_snapshot(self):
        authority=self._committed_authority
        revisions={"state_revision":str(authority["state_revision"]),"risk_revision":str(authority["risk_revision"]),
            "authority_revision":str(authority["authority_revision"]),"config_revision":str(authority["config_revision"])}
        return copy.deepcopy({"account_key": self.account_key, "kernel_boot_id": self.boot_id,
                              "kernel_epoch": str(self.kernel_epoch), "durable_seq": str(self._durable_seq),
                              "armed": authority["armed"] and not self._failed, "development_session": self.development,
                              "execution_scope":"ONLINE_SIMULATION" if self.simulation else "DEVELOPMENT" if self.development else "DISARMED",
                              "recovery_required": self.recovery_required, "storage_failed": self._failed,
                              "pending_transactions": len(self._pending), "revisions": revisions,
                              "maintenance_state":self._maintenance_state,
                              "active_strategy_epoch": None if self.active_epoch is None else str(self.active_epoch),
                              "funds":self.money.account_projection() if hasattr(self,'money') else None,
                              "errors": self._errors, "cycles": list(self._committed.values())}
                             | ({'dsv_rollback': self._dsv_rollback[-1]['candidate_digest']} if self._dsv_rollback else {}))

    def _write_authority(self, conn, seq, authority):
        conn.execute("UPDATE authority SET new_open_status=?, grant_id=?, grant_policy_json=?, state_revision=?, risk_revision=?, authority_revision=?,config_revision=?, updated_event_seq=? WHERE singleton=1",
                     ("ARMED" if authority["armed"] else "DISARMED", (self.simulation.grant.value['grant_id'] if self.simulation else 'development-only') if authority["armed"] else None,
                      canonical_bytes(self.simulation.authority_policy() if self.simulation else {"allowed_origins": ["MANUAL","STRATEGY"], "development_only": True}).decode(),
                      authority["state_revision"], authority["risk_revision"], authority["authority_revision"],authority["config_revision"], seq))

    def arm_development(self) -> Future:
        self._writable()
        if not self.development or isinstance(self.port, UnavailablePort):
            raise TraderError("E_RULE_UNAPPROVED", "No approved online rules or adapter are available")
        if not self.control.can_increase():raise TraderError("E_NOT_READY","A control barrier blocks new risk")
        if self.recovery_required or self._has_unknown():
            raise TraderError("E_NOT_READY", "Recovery or unknown orders block new openings")
        if any(c["protection"].get("reconciliation_required") or (c["protection"]["owned_qty"] and c["protection"]["quality"]!="CONTIGUOUS") for c in self._staged.values()):
            raise TraderError("E_NOT_READY", "Existing positions have unverified observations")
        self._state_revision += 1
        self._authority_revision += 1
        authority = self._authority() | {"armed": True}
        generation = self._authority_revision
        return self._submit("TX_AUTH", "DEVELOPMENT_ARM", {"authority": authority},
                            lambda conn, seq: self._write_authority(conn, seq, authority),
                            after=lambda: setattr(self, "_armed", generation == self._authority_revision))

    def pause(self) -> Future:
        self._writable()
        self._armed = False
        self.contexts.revoke_all()
        self._state_revision += 1
        self._authority_revision += 1
        authority = self._authority()
        return self._submit("TX_AUTH", "PAUSE_NEW", {"authority": authority},
                            lambda conn, seq: self._write_authority(conn, seq, authority))

    def issue_entry_context(self, peer_id: str, contract_id: str) -> dict:
        self._writable()
        return self.contexts.issue(peer_id, "OPERATOR_COMMAND", "MANUAL_OPEN", contract_id,
                                   kernel_epoch=str(self.kernel_epoch), revisions=self.revisions,
                                   issued_event_seq=str(self._durable_seq))

    def execute_pause_command(self, message: dict, peer_id: str) -> Future:
        return self.execute_command(message,peer_id)

    def execute_command(self,message:dict,peer_id:str,*,operator_actor=None)->Future:
        return self.control.execute(message,peer_id,operator_actor=operator_actor)

    def _has_unknown(self) -> bool:
        return any(o["send_state"] in {"SEND_POSSIBLE", "UNKNOWN"} for c in self._staged.values() for o in c["orders"])

    def _exposure(self) -> Decimal:
        total = Decimal(0)
        with localcontext() as context:
            context.prec = 80
            for cycle in self._staged.values():
                state = cycle["protection"]
                potential = sum(OrderState.from_dict(o).potential_remaining for o in cycle["orders"] if o["effect"] == "OPEN")
                if (not potential and (cycle.get('entry_intent') or {}).get('status') == 'ACTIVE'
                        and state['entry_filled_qty'] < cycle['plan']['initial_open_quantity']):
                    potential = cycle['plan']['initial_open_quantity'] - state['entry_filled_qty']
                total += Decimal(cycle["risk_per_lot"]) * (state["owned_qty"] + potential)
        return total

    def admit_entry(self, plan: dict, decision_key: str, limit_price: str, *, context_id: str, peer_id: str = "qa", _strategy=None, _manual=None) -> str:
        self._writable()
        validate_plan(plan)
        if self.simulation is not None:
            if getattr(self.simulation,'strategy_enabled',False):
                candidate = _strategy['metadata']['candidate'] if _strategy is not None else {}
                self.simulation.validate_entry(plan, manual=_manual is not None, entry_kind=candidate.get('entry_kind'))
            else:self.simulation.validate_entry(plan)
            if (_strategy is not None or _manual is not None)!=bool(getattr(self.simulation,'strategy_enabled',False)):
                raise TraderError('E_UNSUPPORTED','Online strategy and manual scopes cannot be interchanged')
        elif not self.development or plan["execution_scope"] != "DEVELOPMENT":
            raise TraderError("E_RULE_UNAPPROVED", "This baseline only executes isolated development events")
        if not self._armed or self.recovery_required:
            raise TraderError("E_NOT_READY", "New openings are disarmed")
        origin = 'STRATEGY' if _strategy is not None else 'MANUAL'
        if not self.control.can_increase(plan['contract_id'], origin=origin):
            raise TraderError('E_NOT_READY', 'A control barrier blocks this contract and entry origin')
        if self.statistics.entry_reasons(plan['contract_id'],plan['trading_day']):
            raise TraderError('E_NOT_READY','Committed local nominal statistics block this opening')
        if any(c["plan"]["contract_id"]==plan["contract_id"] and c["plan"]["trading_day"]==plan["trading_day"]
               and c["protection"].get("block_new_for_trading_day") for c in self._staged.values()):
            raise TraderError("E_NOT_READY", "A persistent day block prevents this contract opening")
        purpose,operation=("ENTRY_PROPOSAL","DSV_ENTRY") if _strategy is not None else ("OPERATOR_COMMAND","MANUAL_OPEN")
        if _strategy is not None:
            if self.active_epoch!=_strategy["epoch"] or not callable(_strategy["gate"]):
                raise TraderError("E_EPOCH_REVOKED","Strategy entry is outside the active epoch")
        self.contexts.validate(context_id, peer_id, purpose, operation, plan["contract_id"],
                               revisions=self.revisions, kernel_epoch=str(self.kernel_epoch),
                               strategy_epoch=None if _strategy is None else str(_strategy["epoch"]))
        if plan["account_key"] != self.account_key or plan["config_revision"] != str(self._config_revision):
            raise TraderError("E_SCOPE", "Plan scope or configuration does not match")
        config = self._configuration
        if plan["config_hash"] != config["payload_hash"] or plan["rule_set_id"] != config["rule_set_id"]:
            raise TraderError("E_CONFIG", "Plan configuration identity does not match")
        decision_key = identifier(decision_key)
        if decision_key in self._decision_keys:
            raise TraderError("E_DUPLICATE_CONFLICT", "Entry opportunity already consumed")
        cycle_id = plan["position_cycle_id"]
        if cycle_id in self._staged:
            raise TraderError("E_DUPLICATE_CONFLICT", "Cycle identity already exists")
        instrument=(self.port.instrument(plan['contract_id']) if self.simulation else {}) if _strategy is None else _strategy["metadata"]["instrument"]
        order_price(limit_price, instrument.get("tick_size",self.tick_size))
        qty = quantity(plan["initial_open_quantity"], positive=True)
        with localcontext() as context:
            context.prec = 80
            per_lot = Decimal(plan["fixed_stop_loss"]["distance_points"]) * Decimal(instrument.get("multiplier",str(self.multiplier)))
            if self._exposure() + per_lot * qty > self.budget:
                raise TraderError("E_RISK", "Insufficient development risk budget")
        state = initial_state(plan,simulation_permit=self.simulation.permit(plan) if self.simulation else None)
        candidate = ((_strategy or {}).get('metadata') or {}).get('candidate') or {}
        order = OrderState(_id("child"), _id("intent"), _id("attempt"), "OPEN", qty, limit_price,
                           kernel_boot_id=self.boot_id,
                           pricing_quote_id=candidate.get('native_tick_id') or candidate.get('market_sequence'),
                           pricing_source_epoch=candidate.get('source_epoch'))
        if getattr(self.simulation,'strategy_enabled',False):
            order.native_offset='OPEN';order.native_trading_day=plan['trading_day']
        cycle = {"position_cycle_id": cycle_id, "plan": copy.deepcopy(plan), "protection": state.to_dict(),
                 "orders": [order.to_dict()], "decision_key": decision_key, "risk_per_lot": decimal_text(per_lot),
                 "lifecycle": "ENTRY_PENDING", "revision": 1, "entry_reconciliation_id": None,
                 "last_quote": None,"entry_authorization":{"context_id":context_id,"peer_id":peer_id,
                    "authority_revision":self._authority_revision,"purpose":purpose,"operation":operation},
                 "entry_origin":"MANUAL" if _strategy is None else "STRATEGY",
                 "strategy_epoch":None if _strategy is None else _strategy["epoch"],
                 "strategy_metadata":copy.deepcopy(_manual if _strategy is None else _strategy["metadata"]),"instrument":instrument}
        cycle['entry_intent'] = {
            'intent_id': order.intent_id,
            'status': 'ACTIVE',
            'signal': copy.deepcopy(((_strategy or {}).get('metadata') or {}).get('signal')),
            'local_reprice_count': 0,
            'sdk_send_count': 0,
            'last_attempt_id': order.attempt_id,
            'last_child_order_id': order.child_order_id,
            'next_action': 'DISPATCH_PREPARED',
            'invalidation_reason': None,
        }
        if _manual is not None:cycle['manual_limit_order']=True
        if (_manual is not None and self._command_binding is not None
                and self._command_binding['metadata'].get('actor') is not None):
            cycle['entry_authorization']['operator_command_id'] = self._command_binding['body']['command_id']
        cycle['financial_instrument']={'multiplier':instrument.get('multiplier',str(self.multiplier)),
            'currency':'CNY','basis':'ORIGINAL_INSTRUMENT','max_quote_age_ms':self.max_quote_age_ms}
        if _strategy is not None and self.simulation and getattr(self.simulation,'strategy_enabled',False):
            initial=_strategy['initial_market'];market=initial['event'];received=str(initial['received_ns'])
            decision=reduce_event(plan,state,{'type':'QUOTE','price':market['last'],'bid1':market['bid1'],'ask1':market['ask1'],
                'valid':True,'event_seq':self._sequence+1,'event_id':'entry-price-'+cycle_id},simulation_permit=self.simulation.permit(plan))
            cycle['protection']=decision.state.to_dict()
            cycle['last_quote']={'price':market['last'],'bid1':market['bid1'],'ask1':market['ask1'],
                                 'kernel_boot_id':self.boot_id,'received_ns':received}
            cycle['money_quote']={'price':market['last'],'observed_at_utc':market['source_time_utc'],
                                  'trading_day':plan['trading_day'],'received_ns':received}
        if self.simulation:cycle['online_entry_baseline']=self.port.snapshot()
        scheduler=getattr(self,'_scheduled_runtime',None)
        self.statistics.enroll(cycle)
        if _strategy is not None:
            cycle['statistics_observed_at_utc']=_strategy['metadata']['candidate']['source_time_utc']
        if scheduler is not None and cycle['strategy_metadata'] is not None:
            cycle['schedule_binding']=scheduler.binding_for(plan,entry_time_utc=cycle['strategy_metadata']['candidate']['source_time_utc'])
            if scheduler.strategy.online:
                cycle['execution_clock_origin'] = scheduler.execution_origin()
        if _strategy is not None:
            self._entry_gates[order.attempt_id] = _strategy["gate"]
            cycle['strategy_target_accepted'] = True
        self._decision_keys.add(decision_key)
        self._risk_revision += 1
        self._state_revision += 1
        def accepted():
            if _strategy is not None:_strategy["accepted"]()
            self._dispatch(cycle_id, order.child_order_id)
        self._stage_cycle(cycle, "TX_INTENT", "ENTRY_PREPARED", after=accepted,
                          extra_mutate=None if _strategy is None else _strategy["mutate"],
                          money_account=None if _strategy is None else _strategy.get('money_account'),
                          evidence=None if _strategy is None else {'report': _strategy['report'] | {'child_order_id': order.child_order_id}})
        return order.child_order_id

    def _stage_cycle(self, cycle, kind, event_type, *, fill=None, after=None,extra_mutate=None,nominal_statistics=None,money_account=None,evidence=None):
        if self.simulation:cycle['native_order_keys']={order['child_order_id']:self.port.order_id(order['child_order_id']) for order in cycle['orders']}
        self._writable()
        self.statistics.freeze_exit(cycle)
        self._normalize_order_revisions(cycle)
        self._staged[cycle["position_cycle_id"]] = copy.deepcopy(cycle)
        frozen = copy.deepcopy(cycle)
        authority = self._authority()
        data = {"cycle": frozen, "authority": authority}
        if evidence is not None:
            data.update(copy.deepcopy(evidence))
        if fill is not None:
            data["fill"] = copy.deepcopy(fill)
        if nominal_statistics is not None:
            data['nominal_statistics']=copy.deepcopy(nominal_statistics)
        if money_account is not None:data['money_account']=copy.deepcopy(money_account)
        def mutate(conn, seq):
            self._project_cycle(conn, seq, frozen, fill)
            self._write_authority(conn, seq, authority)
            if extra_mutate is not None:extra_mutate(conn,seq)
        def accepted():
            if nominal_statistics is not None:self.statistics.accept(nominal_statistics)
            if after is not None:after()
        return self._submit(kind, event_type, data, mutate, cycle=frozen, after=accepted)

    def _normalize_order_revisions(self,cycle):
        previous=self._staged.get(cycle["position_cycle_id"],{})
        previous_orders={o["child_order_id"]:o for o in previous.get("orders",[])}
        for order in cycle["orders"]:
            old=previous_orders.get(order["child_order_id"])
            if old and {k:v for k,v in order.items() if k!="revision"}!={k:v for k,v in old.items() if k!="revision"}:
                order["revision"]=max(order["revision"],old["revision"]+1)

    def stage_control_cycles(self,cycles,kind,event_type,*,extra_mutate=None,after=None,allocation_review=None):
        self._writable()
        self._state_revision+=1;self._risk_revision+=1
        frozen=[]
        for cycle in cycles:
            cycle=copy.deepcopy(cycle)
            self.statistics.freeze_exit(cycle)
            previous=self._staged.get(cycle["position_cycle_id"])
            if previous:cycle["revision"]=max(cycle["revision"],previous["revision"]+1)
            self._normalize_order_revisions(cycle)
            self._staged[cycle["position_cycle_id"]]=copy.deepcopy(cycle)
            frozen.append(cycle)
        authority=self._authority()
        def mutate(conn,seq):
            for cycle in frozen:self._project_cycle(conn,seq,cycle,None)
            self._write_authority(conn,seq,authority)
            if extra_mutate:extra_mutate(conn,seq)
        data={"cycles":frozen,"authority":authority}
        if allocation_review is not None:data['allocation_review']=copy.deepcopy(allocation_review)
        return self._submit(kind,event_type,data,mutate,cycles=frozen,after=after)

    def _manual_close_available(self):
        self._writable()
        if not (self.development or self.simulation) or isinstance(self.port,UnavailablePort):raise TraderError("E_NOT_READY","A trusted execution adapter is required")

    def _manual_close_cycle(self,cycle_id,qty,statistics_at_utc,price_mode,request_id):
        quantity(qty,positive=True)
        cycle=copy.deepcopy(self._staged.get(cycle_id))
        if not cycle:raise TraderError("E_OWNERSHIP","Cycle is not managed by this kernel")
        if cycle.get("management_state") in {"TAKEOVER_PENDING","TAKEN_OVER"}:raise TraderError("E_OWNERSHIP","Cycle is not under original management")
        self.statistics.mark_observation(cycle,statistics_at_utc)
        state=cycle["protection"]
        if qty>state["owned_qty"]:raise TraderError("E_QUANTITY","Close quantity exceeds owned position")
        target=state["owned_qty"]-qty
        if state.get("target_remaining_qty") is not None:target=min(target,state["target_remaining_qty"])
        if price_mode is not None:
            self.execution.prepare_manual_reduction(cycle,target,price_mode,request_id)
        state.update(target_remaining_qty=target,block_new=True,state_revision=str(int(state["state_revision"])+1))
        cycle.update(cancel_entries=True,lifecycle="EXIT_PENDING",reasons=sorted(set(cycle.get("reasons",[]))|{"MANUAL_EXIT"}))
        self.statistics.freeze_exit(cycle,'MANUAL_EXIT')
        return cycle

    def manual_close(self,cycle_id,qty,*,statistics_at_utc=None,price_mode=None,request_id=None):
        self._manual_close_available()
        cycle=self._manual_close_cycle(cycle_id,qty,statistics_at_utc,price_mode,request_id or _id("manual-close"))
        return self.stage_control_cycles([cycle],"TX_PROTECT","MANUAL_CLOSE",after=lambda:self._progress_exits(cycle_id))

    def manual_close_all(self,positions,*,price_mode,request_id):
        self._manual_close_available()
        current={cid:cycle["protection"]["owned_qty"] for cid,cycle in self._staged.items()
                 if cycle["protection"]["owned_qty"] and cycle.get("management_state") not in {"TAKEOVER_PENDING","TAKEN_OVER"}}
        if not current or positions!=current:
            raise TraderError("E_FACT_BARRIER","Reviewed managed positions differ from the current owned quantities")
        cycles=[self._manual_close_cycle(cid,qty,None,price_mode,request_id) for cid,qty in sorted(current.items())]
        def progress():
            for cid in current:self._progress_exits(cid)
        return self.stage_control_cycles(cycles,"TX_PROTECT","MANUAL_CLOSE_ALL",after=progress)

    def flatten(self,cycle_ids,*,statistics_at_utc=None):
        self._writable()
        if not (self.development or self.simulation) or isinstance(self.port,UnavailablePort):raise TraderError("E_NOT_READY","A trusted execution adapter is required")
        cycles=[]
        for cid in dict.fromkeys(cycle_ids):
            if cid not in self._staged:raise TraderError("E_OWNERSHIP","Cycle is not managed by this kernel")
            cycle=copy.deepcopy(self._staged[cid])
            if cycle.get("management_state")=="TAKEN_OVER":raise TraderError("E_OWNERSHIP","Cycle management was handed over")
            self.statistics.mark_observation(cycle,statistics_at_utc)
            state=cycle["protection"]
            state.update(target_remaining_qty=0,block_new=True,state_revision=str(int(state["state_revision"])+1))
            cycle.update(cancel_entries=True,lifecycle="EXIT_PENDING" if state["owned_qty"] else "FLAT_PENDING_CONFIRM",reasons=sorted(set(cycle.get("reasons",[]))|{"FLATTEN_THEN_STOP"}))
            self.statistics.freeze_exit(cycle,'MANUAL_EXIT')
            cycles.append(cycle)
        self._armed=False;self.contexts.revoke_all();self._authority_revision+=1
        def progress():
            for cycle in cycles:self._progress_exits(cycle["position_cycle_id"])
        return self.stage_control_cycles(cycles,"TX_PROTECT","FLATTEN_THEN_STOP",after=progress)

    @staticmethod
    def _upsert(conn, table, data, keys):
        columns = list(data)
        updates = [c for c in columns if c not in keys and c not in {"created_event_seq","prepared_event_seq"}]
        sql = "INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
        sql += " ON CONFLICT(" + ",".join(keys) + ") DO UPDATE SET " + ",".join(c+"=excluded."+c for c in updates)
        conn.execute(sql, [data[c] for c in columns])

    def _project_cycle(self, conn, seq, c, fill):
        plan, state = c["plan"], c["protection"]
        cid = c["position_cycle_id"]
        adopted=c.get('external_adoption',{}).get('native_quantity',0)
        external_reduced=sum(row['quantity'] for row in c.get('external_reductions',[]))
        self._upsert(conn, "position_cycles", {"position_cycle_id":cid,"contract_id":plan["contract_id"],"direction":plan["direction"],
            "trading_day":plan["trading_day"],"ownership":"MANAGED","lifecycle":c["lifecycle"],
            "confirmed_open_qty":state["entry_filled_qty"]-adopted,"confirmed_close_qty":state["exit_filled_qty"]-external_reduced,
            "allocation_adjustment_qty":adopted-external_reduced,"remaining_qty":state["owned_qty"],"actual_average_entry_price":c.get("actual_average_entry_price"),
            "state_revision":c["revision"],"created_event_seq":seq,"updated_event_seq":seq}, ["position_cycle_id"])
        existing = conn.execute("SELECT plan_hash FROM protection_plans WHERE plan_id=? AND plan_revision=?",(plan["plan_id"],int(plan["plan_revision"]))).fetchone()
        if existing is None:
            conn.execute("INSERT INTO protection_plans(plan_id,plan_revision,position_cycle_id,rule_set_id,config_revision,plan_json,plan_hash,created_event_seq) VALUES (?,?,?,?,?,?,?,?)",
                         (plan["plan_id"],int(plan["plan_revision"]),cid,plan["rule_set_id"],int(plan["config_revision"]),canonical_bytes(plan).decode(),content_hash(plan),seq))
        elif existing[0] != content_hash(plan):
            raise TraderError("E_PLAN_CONFLICT", "An immutable plan changed content")
        if c["entry_reconciliation_id"]:
            rid = c["entry_reconciliation_id"]
            online=c.get('online_reconciliation')
            conn.execute("INSERT OR IGNORE INTO reconciliation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (rid,online['gateway_identity'] if online else 'DEV',online['source_epoch'] if online else 1,online['trading_day'] if online else plan["trading_day"],"COMPLETE",
                          canonical_bytes({'cycle_id':cid,'snapshot_id':online['snapshot_id'],'snapshot_hash':content_hash(online)} if online else {"cycle_id":cid,"development_only":True}).decode(),0,seq,0,seq,seq))
            for part in ("FUNDS","POSITIONS","ORDERS","TRADES"):
                evidence={'kind':'TQKQ_FULL_SYNC_SNAPSHOT','snapshot_id':online['snapshot_id'],'source_epoch':online['source_epoch'],'complete':True} if online else {'development_only':True}
                if online and getattr(self.simulation.grant,'provider','TQKQ')=='TQ_SIMNOW':evidence['kind']='SIMNOW_FULL_SYNC_SNAPSHOT'
                conn.execute("INSERT OR IGNORE INTO reconciliation_parts VALUES(?,?,?,?,?,?,?)",(rid,part,"COMPLETE","{}",canonical_bytes(evidence).decode(),0,
                    canonical_bytes(online[part.lower()] if online else {}).decode()))
        target = state.get("target_remaining_qty")
        if target is not None:
            self._upsert(conn,"exit_targets",{"position_cycle_id":cid,"desired_remaining_qty":target,
                "reason_codes_json":canonical_bytes(c.get("reasons",[])).decode(),"state_revision":c["revision"],"updated_event_seq":seq},["position_cycle_id"])
        protection = {"position_cycle_id":cid,"plan_id":plan["plan_id"],"plan_revision":int(plan["plan_revision"]),
            "mode":state["mode"],"max_favorable_move":state["max_favorable_move"],"protected_event_seq":state.get("protected_event_seq"),
            "quality":state.get("quality","UNKNOWN"),"phase1_status":state["phase1_status"],"phase2_status":state["phase2_status"],
            "q0":state.get("q0"),"q0_reconciliation_id":state.get("q0_reconciliation_id") if state.get("q0") else None,
            "phase1_confirmed_reduction_qty":state.get("phase1_confirmed_reduction_qty",0),
            "phase2_confirmed_reduction_qty":state.get("phase2_confirmed_reduction_qty",0),
            "break_even_status":state["break_even_status"],"trailing_activated_event_seq":state.get("trailing_activated_event_seq"),
            "trailing_floor_move":state.get("trailing_floor_move"),"last_observed_event_seq":state.get("last_observed_event_seq"),
            "target_remaining_qty":target,"state_revision":c["revision"],"updated_event_seq":seq}
        self._upsert(conn,"protection_state",protection,["position_cycle_id"])
        by_intent = {}
        for item in c['orders']:
            by_intent.setdefault(item['intent_id'], []).append(item)
        for item in c["orders"]:
            effect = item["effect"]
            related = by_intent[item['intent_id']]
            logical = c.get('entry_intent') or {} if effect == 'OPEN' else {}
            if any(child['send_state'] == 'UNKNOWN' for child in related):
                intent_status = 'UNKNOWN'
            elif effect == 'OPEN' and logical.get('status') == 'TERMINATED':
                intent_status = 'ABORTED'
            elif (effect == 'OPEN' and all(child['terminal_confirmed'] for child in related)
                  and state['entry_filled_qty'] >= plan['initial_open_quantity']):
                intent_status = 'DONE'
            elif (effect == 'OPEN' and logical.get('status') in {'ACTIVE', 'BROKER_WORKING', 'RECONCILING'}
                  or any(OrderState.from_dict(child).potential_remaining for child in related)):
                intent_status = 'ACTIVE'
            elif all(child['terminal_confirmed'] for child in related):
                intent_status = 'DONE'
            else:
                intent_status = 'ACTIVE'
            intent_reasons = list(c.get('reasons', []))
            if effect == 'OPEN':
                failure = logical.get('last_failure') or {}
                intent_reasons.extend(failure.get('reason_codes', []))
                if logical.get('invalidation_reason'):
                    intent_reasons.append(logical['invalidation_reason'])
            self._upsert(conn,"order_intents",{"intent_id":item["intent_id"],"position_cycle_id":cid,
                "decision_key":c["decision_key"] if effect=="OPEN" else None,"effect":effect,"origin":c.get("entry_origin","MANUAL") if effect=="OPEN" else "PROTECTION",
                "requested_qty":plan["initial_open_quantity"] if effect=="OPEN" else None,"desired_remaining_qty":target if effect=="REDUCE" else None,
                "status":intent_status,
                "reason_codes_json":canonical_bytes(list(dict.fromkeys(intent_reasons))).decode(),"kernel_epoch":self.kernel_epoch,"strategy_epoch":c.get("strategy_epoch"),
                "state_revision":c["revision"],"created_event_seq":seq,"updated_event_seq":seq},["intent_id"])
            self._upsert(conn,"child_orders",{"child_order_id":item["child_order_id"],"intent_id":item["intent_id"],"ordinal":item.get("ordinal",1),
                "offset_kind":{'CLOSETODAY':'CLOSE_TODAY'}.get(item.get('native_offset'),item.get('native_offset',"OPEN" if effect=="OPEN" else "CLOSE")),"limit_price":item["limit_price"],"original_qty":item["original_qty"],
                "confirmed_fill_qty":item["filled_qty"],"broker_state":item["broker_state"],
                "terminal_evidence_json":None if not item["terminal_confirmed"] else canonical_bytes({"confirmed_fill_qty":item["reported_fill_qty"],
                    **({'gateway_identity':self.port.gateway_identity,'source_epoch':self.port.source_epoch,'native_order_id':self.port.order_id(item['child_order_id'])} if self.simulation else {'development_only':True})}).decode(),
                "state_revision":item["revision"],"updated_event_seq":seq},["child_order_id"])
            self._upsert(conn,"send_attempts",{"attempt_id":item["attempt_id"],"child_order_id":item["child_order_id"],"ordinal":1,
                "send_state":item["send_state"],"request_hash":content_hash({"child_order_id":item["child_order_id"],"quantity":item["original_qty"],"limit_price":item["limit_price"]}|{key:item[key] for key in ('native_offset','native_trading_day','close_route') if key in item}),
                "kernel_boot_id":item.get("kernel_boot_id") or self.boot_id,"call_result_json":None,"prepared_event_seq":seq,"updated_event_seq":seq},["attempt_id"])
            if item["cancel_status"]:
                self._upsert(conn,"cancel_actions",{"cancel_id":"cancel-"+item["child_order_id"],"child_order_id":item["child_order_id"],
                    "status":item["cancel_status"],"request_hash":content_hash({"child_order_id":item["child_order_id"]}),"attempt_count":1,
                    "created_event_seq":seq,"updated_event_seq":seq},["cancel_id"])
        opening = next((o for o in c["orders"] if o["effect"]=="OPEN"),None)
        open_pending = sum(OrderState.from_dict(o).potential_remaining for o in c["orders"] if o["effect"]=="OPEN")
        logical = c.get('entry_intent') or {}
        if (not open_pending and logical.get('status') == 'ACTIVE'
                and state['entry_filled_qty'] < plan['initial_open_quantity']):
            open_pending = plan['initial_open_quantity'] - state['entry_filled_qty']
        risk_qty = state["owned_qty"] + open_pending
        with localcontext() as context:
            context.prec = 80
            amount = decimal_text(Decimal(c["risk_per_lot"])*risk_qty)
        if opening is not None:
            self._upsert(conn,"reservations",{"reservation_id":"risk-"+cid,"intent_id":opening["intent_id"],"child_order_id":opening["child_order_id"],
                "reservation_kind":"RISK_BUDGET","amount":amount,"quantity":None,"status":"RELEASED" if risk_qty==0 else "UNKNOWN" if any(o['send_state']=='UNKNOWN' for o in c['orders'] if o['effect']=='OPEN') else "HELD",
                "state_revision":c["revision"],"updated_event_seq":seq},["reservation_id"])
        if state.get("block_new_for_trading_day"):
            conn.execute("INSERT OR IGNORE INTO day_blocks VALUES(?,?,?,?,?,?,?,?)",("block-"+cid,plan["contract_id"],plan["trading_day"],"PROTECTION_LOCK","{}","ACTIVE",seq,None))
        if fill is not None:
            conn.execute("INSERT INTO fill_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(fill["fill_id"],fill["trade_key"],fill["child_order_id"],self.port.gateway_identity if self.simulation else 'DEV',self.port.source_epoch if self.simulation else 1,fill.get('trading_day',plan["trading_day"]),plan["contract_id"],fill["side"],fill["offset_kind"],fill["quantity"],fill["price"],canonical_bytes(fill).decode(),fill["fact_hash"],seq))
            conn.execute("INSERT INTO fill_allocations VALUES(?,?,?,?,?,?)",(fill["fill_id"],cid,fill["effect"],fill["quantity"],canonical_bytes({'gateway_identity':self.port.gateway_identity,'owned_native_fill':True} if self.simulation else {'development_only':True}).decode(),seq))

    def _find(self, child_id):
        for cycle in self._staged.values():
            for item in cycle["orders"]:
                if item["child_order_id"] == child_id:
                    return copy.deepcopy(cycle), item["child_order_id"]
        raise TraderError("E_OWNERSHIP", "Order is not owned by this kernel")

    def _dispatch(self, cid, child_id):
        if self._failed:
            return
        cycle = copy.deepcopy(self._staged[cid])
        order = next(o for o in cycle["orders"] if o["child_order_id"]==child_id)
        if order["attempt_id"] in self._dispatched:
            return
        if order["send_state"] != "SEND_POSSIBLE":
            self._entry_gates.pop(order["attempt_id"],None)
            return
        checks = {}
        review_details = {}
        checks['ORIGINAL_BOOT'] = order.get("kernel_boot_id") == self.boot_id
        scheduler=getattr(self,'_scheduled_runtime',None)
        if scheduler is not None and checks['ORIGINAL_BOOT']:
            expected_day = order.get('native_trading_day', cycle['plan']['trading_day'])
            checks['CURRENT_SESSION'] = scheduler.scope_current(cycle['plan']['contract_id'], expected_day)
            if scheduler._fault and order['effect'] == 'REDUCE':
                now = scheduler.clock_protection_time(cycle)
                if now is not None:
                    window = scheduler.clock_protection_session(cycle['plan']['contract_id'], now)
                    checks['CURRENT_SESSION'] = window['trading_day'] == expected_day
            if not checks['CURRENT_SESSION']:
                review_details['CURRENT_SESSION'] = {'expected_trading_day': expected_day,
                                                      'clock_fault': scheduler._fault}
        gate_result = None
        if order["effect"]=="OPEN":
            authorization=cycle["entry_authorization"]
            try:
                strategy=cycle.get("entry_origin")=="STRATEGY"
                if command_id := authorization.get('operator_command_id'):
                    # The original intent and command must share a confirmed ledger commit.
                    command = self.control.states.get(command_id, {})
                    committed = self._committed.get(cid) or {}
                    if (strategy or committed.get('entry_authorization') != authorization
                            or command.get('body', {}).get('operation') != 'MANUAL_OPEN'
                            or cid not in command.get('metadata', {}).get('cycle_ids', ())
                            or command.get('metadata', {}).get('actor') is None):
                        raise TraderError('E_AUTH', 'The original accepted operator authorization is unavailable')
                else:
                    self.contexts.validate(authorization['context_id'], authorization['peer_id'],
                        authorization.get('purpose', 'OPERATOR_COMMAND'), authorization.get('operation', 'MANUAL_OPEN'),
                        cycle['plan']['contract_id'], kernel_boot_id=self.boot_id, kernel_epoch=str(self.kernel_epoch),
                        strategy_epoch=str(cycle['strategy_epoch']) if strategy else None)
                checks['ENTRY_AUTHORITY'] = (self._armed and not order["cancel_status"] and not order["terminal_confirmed"]
                                             and authorization["authority_revision"]==self._authority_revision)
                origin = 'STRATEGY' if strategy else 'MANUAL'
                checks['HUMAN_ENTRY_POLICY'] = self.control.entry_origin_allowed(origin, cycle['plan']['contract_id'])
                if all(checks.values()):
                    checks['RISK_BUDGET'] = self._exposure() <= self.budget
                    if checks['RISK_BUDGET']:
                        for reason in self.statistics.entry_reasons(cycle['plan']['contract_id'], cycle['plan']['trading_day']):
                            checks[reason] = False
                    else:
                        review_details['RISK_BUDGET'] = {'expected_maximum': decimal_text(self.budget),
                                                         'observed': decimal_text(self._exposure())}
                if strategy and all(checks.values()):
                    gate=self._entry_gates.get(order["attempt_id"])
                    if self.active_epoch != cycle["strategy_epoch"]:
                        checks['STRATEGY_EPOCH_REVOKED'] = False
                    elif gate is None:
                        checks['ENTRY_RECHECK_UNAVAILABLE'] = False
                    else:
                        gate_result = gate()
                        if isinstance(gate_result, dict):
                            for reason in gate_result['reason_codes']:
                                checks[reason] = False
                            current_limit = (gate_result.get('details') or {}).get('current_limit_price')
                            if (current_limit is not None and not committed_limit_is_safe(
                                    self.execution.side(cycle, order['effect']),
                                    order['limit_price'], current_limit)):
                                checks['E_QUOTE_REVISION_CHANGED'] = False
                                review_details['E_QUOTE_REVISION_CHANGED'] = {
                                    'committed_limit_price': order['limit_price'],
                                    'current_policy_price': current_limit,
                                    'quote_id': gate_result.get('quote_id'),
                                    'source_epoch': gate_result.get('source_epoch'),
                                }
                        else:
                            checks['STRATEGY_RECHECK'] = bool(gate_result)
                            gate_result = None
            except TraderError as error:
                checks[error.code] = False
        else:
            quote=cycle.get("last_quote")
            checks['ORIGINAL_EXIT_FACTS']=bool(all(checks.values()) and cid not in self._recovery_pending and quote and quote["kernel_boot_id"]==self.boot_id
                and cycle.get("management_state")!="TAKEN_OVER"
                and cycle["protection"]["quality"]=="CONTIGUOUS" and self._quote_age_ns(quote["received_ns"])<=self.max_quote_age_ms*1_000_000)
            if quote:
                current_price=quote.get("bid1" if cycle["plan"]["direction"]=="LONG" else "ask1")
                if not self.execution.active(cycle):checks['ORIGINAL_EXIT_PRICE']=current_price==order["limit_price"]
            other_pending=sum(OrderState.from_dict(o).potential_remaining for o in cycle["orders"] if o["effect"]=="REDUCE" and o["child_order_id"]!=child_id)
            target=cycle["protection"].get("target_remaining_qty")
            checks['ORIGINAL_EXIT_QUANTITY']=target is not None and order["original_qty"]<=max(0,cycle["protection"]["owned_qty"]-target-other_pending)
        if self.execution.active(cycle):
            priced=self.execution.price(cycle,order['effect'])
            checks['EXECUTION_PRICE']=bool(priced['allowed'])
            if checks['EXECUTION_PRICE'] and not committed_limit_is_safe(
                    self.execution.side(cycle, order['effect']), order['limit_price'], priced['price']):
                checks['E_QUOTE_REVISION_CHANGED'] = False
            if not checks['EXECUTION_PRICE'] or checks.get('E_QUOTE_REVISION_CHANGED') is False:
                review_details['EXECUTION_PRICE'] = {'expected': order['limit_price'],
                    'observed': priced['price'], 'reason_codes': priced['reasons']}
        try:order_price(order["limit_price"],cycle.get("instrument",{}).get("tick_size",self.tick_size))
        except TraderError as error:checks[error.code]=False
        request=copy.deepcopy(order) | {"position_cycle_id":cid,"account_key":self.account_key}
        if self.simulation is not None:
            request.update(contract_id=cycle['plan']['contract_id'],direction='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL',trading_day=order.get('native_trading_day',cycle['plan']['trading_day']))
            try:
                self.port.validate_send(request)
            except TraderError as error:
                checks[error.code] = False
                quote_evidence = getattr(error, 'quote_evidence', None)
                if error.code == 'E_NATIVE_QUOTE_NOT_CURRENT' and quote_evidence:
                    review_details[error.code] = quote_evidence
        trace = getattr(self, '_dsv_quote_trace', None)
        if trace is not None:
            trace.record('DISPATCH_PRECHECK', contract_id=cycle['plan']['contract_id'],
                         child_order_id=child_id, attempt_id=order['attempt_id'], effect=order['effect'],
                         pricing_quote_id=order.get('pricing_quote_id'),
                         pricing_source_epoch=order.get('pricing_source_epoch'),
                         reasons=[check for check, passed in checks.items() if not passed],
                         review_details=review_details)
        if not all(checks.values()):
            self._entry_gates.pop(order["attempt_id"],None)
            order["send_state"] = "DEFINITELY_NOT_SENT"
            order["terminal_confirmed"] = True
            order["reported_fill_qty"] = order["filled_qty"]
            if getattr(self.simulation, 'strategy_enabled', False):
                reasons = [check for check, passed in checks.items() if not passed]
                cycle["last_dispatch_abort"] = {
                    "child_order_id": child_id,
                    "decision_id": (cycle.get("strategy_metadata") or {}).get("decision_id"),
                    "reasons": reasons,
                    "recheck": gate_result,
                    "checks": checks,
                    "details": review_details,
                    "pricing_quote_id": order.get('pricing_quote_id'),
                    "pricing_source_epoch": order.get('pricing_source_epoch'),
                    "current_quote_id": (gate_result or {}).get('quote_id'),
                    "current_source_epoch": (gate_result or {}).get('source_epoch'),
                    "expected_authority_revision": (cycle.get("entry_authorization") or {}).get("authority_revision"),
                    "authority_revision": self._authority_revision,
                    "sdk_boundary": "NOT_ENTERED",
                }
                self._update_attempt_responsibility(cycle, order, reasons, review_details)
            cycle["revision"] += 1
            self._stage_cycle(cycle,"TX_ORDER","SEND_ABORTED",
                              after=lambda:self._progress_definitely_not_sent(cid, order["effect"]))
            return
        self._dispatched.add(order["attempt_id"])
        self._entry_gates.pop(order["attempt_id"],None)
        evidence = None
        try:
            self.port.send(request)
            order["send_state"] = "CALL_RETURNED"
        except DefinitelyNotSentError as error:
            order.update(send_state="DEFINITELY_NOT_SENT", terminal_confirmed=True,
                         reported_fill_qty=order["filled_qty"])
            evidence = {"exception": exception_record(error), "child_order_id": child_id,
                        "sdk_boundary": "NOT_ENTERED", "gateway_evidence": error.evidence}
            reasons = [error.code]
            self._update_attempt_responsibility(cycle, order, reasons, error.evidence or {})
            if trace is not None:
                trace.record('DISPATCH_RESULT', contract_id=cycle['plan']['contract_id'],
                             child_order_id=child_id, attempt_id=order['attempt_id'],
                             result=order['send_state'], reasons=reasons)
            cycle["revision"] += 1
            order["revision"] += 1
            self._stage_cycle(cycle,"TX_ORDER","SEND_ABORTED",evidence=evidence,
                              after=lambda:self._progress_definitely_not_sent(cid, order["effect"]))
            return
        except Exception as error:
            order["send_state"] = "UNKNOWN"
            self._armed = False
            evidence = {"exception": exception_record(error), "child_order_id": child_id}
            if isinstance(error, TraderError) and error.code == 'E_NATIVE_QUOTE_NOT_CURRENT':
                native_quote_review = getattr(error, 'quote_evidence', None)
                if native_quote_review:
                    evidence["native_quote_review"] = native_quote_review
        if order['effect'] == 'OPEN':
            intent = cycle.setdefault('entry_intent', {})
            intent['sdk_send_count'] = int(intent.get('sdk_send_count', 0)) + 1
            intent['last_attempt_id'] = order['attempt_id']
            intent['last_child_order_id'] = order['child_order_id']
            intent['status'] = 'RECONCILING' if order['send_state'] == 'UNKNOWN' else 'BROKER_WORKING'
            intent['next_action'] = 'QUERY_ORIGINAL_ORDER' if order['send_state'] == 'UNKNOWN' else 'WAIT_BROKER_FACTS'
        if trace is not None:
            trace.record('DISPATCH_RESULT', contract_id=cycle['plan']['contract_id'],
                         child_order_id=child_id, attempt_id=order['attempt_id'], result=order['send_state'])
        cycle["revision"] += 1
        order["revision"] += 1
        self._stage_cycle(cycle,"TX_ORDER","SEND_RESULT", evidence=evidence)
        if self.simulation and order['send_state']=='UNKNOWN':self.pause()

    def _update_attempt_responsibility(self, cycle, order, reasons, details):
        """Retain the logical target after a proved local non-send."""
        if order['effect'] == 'REDUCE':
            cycle['exit_responsibility'] = {
                'state': 'RETRY_SCHEDULED', 'last_child_order_id': order['child_order_id'],
                'reason_codes': list(reasons), 'next_action': 'REPRICE_FROM_LATEST_QUOTE'}
            return
        intent = cycle.setdefault('entry_intent', {})
        intent.update(last_attempt_id=order['attempt_id'], last_child_order_id=order['child_order_id'],
                      last_failure={'reason_codes': list(reasons), 'details': copy.deepcopy(details)})
        terminal = next((reason for reason in reasons if reason in {
            'E_ENTRY_SIGNAL_INVALIDATED', 'E_ENTRY_PRICE_CAP', 'E_ENTRY_WINDOW_EXPIRED',
            'ENTRY_RECHECK_SESSION_CHANGED', 'ENTRY_RECHECK_CONFIGURATION_CHANGED'}), None)
        reconcile = next((reason for reason in reasons if reason in {
            'E_POSITION_MISMATCH', 'E_FOREIGN_ACTIVE_ORDER', 'E_UNKNOWN_SEND',
            'RECONCILIATION_REQUIRED'}), None)
        if terminal:
            intent.update(status='TERMINATED', next_action='NONE', invalidation_reason=terminal)
            if cycle.get('execution') is not None:
                cycle['execution']['entry_stopped'] = True
        elif reconcile:
            intent.update(status='RECONCILING', next_action='RECONCILE_BROKER_FACTS')
            self._armed = False
            self.contexts.revoke_all(reason=reconcile)
            self._state_revision += 1
            self._authority_revision += 1
            cycle['protection'].update(reconciliation_required=True, block_new=True)
            self._recovery_pending.add(cycle['position_cycle_id'])
            self.recovery_required = True
        else:
            intent.update(status='ACTIVE', next_action='REPRICE_FROM_LATEST_QUOTE')

    def _progress_definitely_not_sent(self, cycle_id, effect):
        # A proved local non-send releases the child quantity but does not make
        # the same quote a new execution fact.  Retrying synchronously here can
        # spin through durable children without a newer market observation.
        # The retained entry/exit responsibility is advanced by the next
        # explicit quote or broker fact.
        return

    def record_fill(self, child_id: str, trade_key: str, qty: int, price: str, *, observed_at_utc=None,
                    trading_day=None,commission=None,estimated_commission=None,native_day_evidence=None,
                    native_trade_id=None,_initial_entry_confirmation=None,_defer_entry_slippage=False,_adapter_token=None):
        self._writable()
        if not self.development and not (self.simulation and _adapter_token is self.port.callback_token):
            raise TraderError("E_UNSUPPORTED", "No trusted broker callback adapter is installed")
        quantity(qty,positive=True)
        decimal_value(price)
        cycle,_ = self._find(child_id)
        order = next(o for o in cycle["orders"] if o["child_order_id"]==child_id)
        identity = {"child_order_id":child_id,"trade_key":identifier(trade_key),"quantity":qty,"price":price,"effect":order["effect"]}
        if native_trade_id is not None and (not self.simulation or self.port.trade_key(native_trade_id)!=trade_key):
            raise TraderError('E_FACT_BARRIER','Fill does not match its original native trade identity')
        digest = content_hash(identity)
        if trade_key in self._facts:
            if self._facts[trade_key] != digest:
                self._record_conflict("FILL_IDENTITY_CONFLICT",{"incoming":identity,"incoming_hash":digest,"existing_hash":self._facts[trade_key]})
                raise TraderError("E_DUPLICATE_CONFLICT", "Conflicting fill identity requires reconciliation")
            previous=self._money_fact_meta.get(trade_key,{})
            if ((trading_day is not None and trading_day!=previous.get('trading_day')) or
                (observed_at_utc is not None and observed_at_utc!=previous.get('observed_at_utc'))):
                raise TraderError('E_DUPLICATE_CONFLICT','Original fill day or observation identity changed')
            if native_day_evidence is not None and previous.get('native_day_evidence') not in (None,native_day_evidence):
                raise TraderError('E_DUPLICATE_CONFLICT','Original native cabinet-day proof changed')
            if native_trade_id is not None and previous.get('native_trade_id')!=native_trade_id:
                raise TraderError('E_DUPLICATE_CONFLICT','Original native trade identity changed')
            if commission is not None or estimated_commission is not None:
                from online_trader.accounting.book import validate_fee
                if commission is not None and estimated_commission is not None:
                    raise TraderError('E_MONEY_INPUT','Duplicate fill fees are ambiguous')
                fee=validate_fee({'trade_key':trade_key,'amount':commission if commission is not None else estimated_commission,
                    'quality':'CONFIRMED' if commission is not None else 'ESTIMATED',
                    'evidence_id':'fill-fee-'+content_hash({'trade_key':trade_key}),'revision':1})
                original_fee=previous.get('money_fee') or previous.get('original_fee')
                if original_fee!=fee:
                    raise TraderError('E_DUPLICATE_CONFLICT','Fee changes require a separate append-only fee revision')
            return False
        if order["filled_qty"]+qty > order["original_qty"]:
            self._record_conflict("FILL_QUANTITY_CONFLICT",{"incoming":identity,"original_quantity":order["original_qty"],"known_fill_quantity":order["filled_qty"]})
            raise TraderError("E_FILL_CONFLICT", "Reported quantity exceeds the proved order")
        late_terminal=bool(order['terminal_confirmed'] and order.get('reported_fill_qty')!=order['filled_qty']+qty)
        closed_statistics=bool(cycle.get('statistics_close_id'))
        self.statistics.mark_observation(cycle,observed_at_utc)
        from online_trader.accounting.book import trading_day as money_day, validate_fee, utc_now
        financial_day=money_day(trading_day or cycle['plan']['trading_day'])
        fee=None;money_issues=[]
        if commission is not None or estimated_commission is not None:
            try:
                if commission is not None and estimated_commission is not None:
                    raise TraderError('E_MONEY_INPUT','Fee fields are ambiguous')
                fee=validate_fee({'trade_key':trade_key,'amount':commission if commission is not None else estimated_commission,
                    'quality':'CONFIRMED' if commission is not None else 'ESTIMATED',
                    'evidence_id':'fill-fee-'+content_hash({'trade_key':trade_key}),'revision':1})
            except TraderError as error:
                money_issues=[{'code':'FEE_INPUT_INVALID','trade_key':trade_key,'error_code':error.code}]
        self._facts[trade_key] = digest
        order["filled_qty"] += qty
        order["reported_fill_qty"] = order["filled_qty"] if order["filled_qty"]==order["original_qty"] else order["reported_fill_qty"]
        order["broker_state"] = "FILLED" if order["filled_qty"]==order["original_qty"] else "PARTIALLY_FILLED" if not order["terminal_confirmed"] else order["broker_state"]
        order["terminal_confirmed"] = order["filled_qty"]==order["original_qty"] or order["terminal_confirmed"]
        order["send_state"] = "CONFIRMED"
        order["revision"] += 1
        event={"type":"FILL" if order["effect"]=="OPEN" else "EXIT_FILL","quantity":qty,"price":price,"event_seq":self._sequence+1,"event_id":trade_key}
        if (order["effect"] == "OPEN" and cycle["plan"]["schema_version"] == "1.3"
                and cycle["plan"]["slippage_exit"]["enabled"]):
            seen = cycle.get("entry_slippage_confirmation_seen", bool(cycle["protection"]["entry_filled_qty"]))
            if seen or _defer_entry_slippage:
                event["skip_entry_slippage"] = True
            elif _initial_entry_confirmation is not None:
                event["initial_entry_confirmation"] = _initial_entry_confirmation
            cycle["entry_slippage_confirmation_seen"] = seen or not _defer_entry_slippage
        quote=cycle.get("last_quote")
        if quote:
            field="price" if cycle["plan"]["price_field"]=="LAST" else ("bid1" if cycle["plan"]["direction"]=="LONG" else "ask1")
            if quote.get(field) is not None:
                event.update(current_price=quote[field],quote_valid=quote["kernel_boot_id"]==self.boot_id and self._quote_age_ns(quote["received_ns"])<=self.max_quote_age_ms*1_000_000)
        decision = reduce_event(cycle["plan"],ProtectionState.from_dict(cycle["protection"]),event,simulation_permit=self.simulation.permit(cycle['plan']) if self.simulation else None)
        self._apply_decision(cycle,decision)
        if order["effect"] == "OPEN" and cycle.get("entry_origin") == "STRATEGY":
            notice = cycle.get("initial_open_confirmation")
            if notice is None or notice["status"] == "PENDING":
                if _defer_entry_slippage:
                    cycle["initial_open_confirmation"] = {"status":"PENDING"}
                elif _initial_entry_confirmation is not None:
                    status = "SLIPPAGE_EXIT" if "ADVERSE_SLIPPAGE" in decision.reasons else "CONFIRMED"
                    cycle["initial_open_confirmation"] = {"status":status, **_initial_entry_confirmation}
        if self.simulation:self.simulation.mark_fill(cycle,order)
        if closed_statistics:
            cycle['statistics_review_required']=True
        if late_terminal or closed_statistics:
            cycle['protection'].update(reconciliation_required=True,block_new=True)
            self._armed=False;self.contexts.revoke_all()
            self._recovery_pending.add(cycle['position_cycle_id']);self.recovery_required=True
            cycle['reasons']=sorted(set(cycle.get('reasons',[]))|{'LATE_FILL_CONTRADICTS_TERMINAL'})
        if order["effect"]=="OPEN":
            with localcontext() as context:
                context.prec=80
                cycle["actual_average_entry_price"]=decimal_text(Decimal(cycle["protection"]["entry_value"])/cycle["protection"]["entry_filled_qty"])
        cycle["revision"] += 1
        cycle["lifecycle"] = "FLAT_PENDING_CONFIRM" if decision.state.owned_qty==0 else "EXIT_PENDING" if decision.target_remaining_qty is not None else "HOLDING"
        self._risk_revision += 1
        fill = identity | {"fill_id":_id("fill"),"fact_hash":digest,"side":"BUY" if (cycle["plan"]["direction"]=="LONG")== (order["effect"]=="OPEN") else "SELL","offset_kind":"OPEN" if order["effect"]=="OPEN" else "CLOSE"}
        if order.get('native_offset'):fill['offset_kind']={'CLOSETODAY':'CLOSE_TODAY'}.get(order['native_offset'],order['native_offset'])
        fill.update(trading_day=financial_day,observed_at_utc=observed_at_utc or cycle.get('statistics_observed_at_utc') or utc_now())
        if native_day_evidence is not None:fill['native_day_evidence']=copy.deepcopy(native_day_evidence)
        if native_trade_id is not None:fill['native_trade_id']=native_trade_id
        if fee is not None:fill['money_fee']=fee
        if money_issues:fill['money_issues']=money_issues
        self._money_fact_meta[trade_key]=copy.deepcopy(fill)
        def after_fill():
            if late_terminal or closed_statistics:
                current=self._staged[cycle['position_cycle_id']]
                for item in current['orders']:
                    if item['child_order_id']!=child_id and OrderState.from_dict(item).potential_remaining:
                        self.request_cancel(item['child_order_id'])
            else:self._progress_exits(cycle['position_cycle_id'])
        statistics=None
        if order['effect']=='OPEN' and not closed_statistics:
            try:statistics=self.statistics.prepare_entry(cycle,fill)
            except TraderError as error:
                # Confirmed executions remain durable even if a derived aggregate
                # reaches its bounded domain or needs a separately reviewed repair.
                cycle['statistics_review_required']=True
                cycle['statistics_error']=error.code
                cycle['protection']['block_new']=True
                self._armed=False;self.contexts.revoke_all()
                # The underlying owned quantity is still proved. A derived
                # statistics failure blocks openings without suspending exits.
                self.recovery_required=True
        self._stage_cycle(cycle,"TX_FILL","FILL_CONFIRMED",fill=fill,after=after_fill,nominal_statistics=statistics)
        return True

    def observe(self, cycle_id: str, price: str, *, bid1: str | None = None, ask1: str | None = None, valid: bool = True, observed_at_utc=None,_adapter_token=None):
        self._writable()
        if not self.development and not (self.simulation and _adapter_token is self.port.callback_token):
            raise TraderError("E_UNSUPPORTED", "No trusted online market adapter is installed")
        cycle = copy.deepcopy(self._staged[cycle_id])
        self.statistics.mark_observation(cycle,observed_at_utc)
        try:
            decimal_value(price)
            if bid1 is not None:decimal_value(bid1)
            if ask1 is not None:decimal_value(ask1)
        except TraderError:
            valid=False
            price=price if isinstance(price,str) else None
            bid1=bid1 if isinstance(bid1,str) else None
            ask1=ask1 if isinstance(ask1,str) else None
        decision = reduce_event(cycle["plan"],ProtectionState.from_dict(cycle["protection"]),
            {"type":"QUOTE","price":price,"bid1":bid1,"ask1":ask1,"valid":valid,"event_seq":self._sequence+1,"event_id":_id("quote")},
            simulation_permit=self.simulation.permit(cycle['plan']) if self.simulation else None)
        self._apply_decision(cycle,decision)
        if valid and decision.state.quality=="CONTIGUOUS":
            cycle["last_quote"] = {"price":price,"bid1":bid1,"ask1":ask1,"kernel_boot_id":self.boot_id,"received_ns":str(time.monotonic_ns())}
            from online_trader.accounting.book import utc_now
            cycle['money_quote']={'price':price,'observed_at_utc':observed_at_utc or cycle.get('statistics_observed_at_utc') or utc_now(),
                'trading_day':cycle['plan']['trading_day'],'received_ns':cycle['last_quote']['received_ns']}
        else:
            cycle["last_quote"]=None
        cycle["revision"] += 1
        cycle["reasons"] = sorted(set(cycle.get("reasons",[]))|set(decision.reasons))
        if decision.target_remaining_qty is not None:
            cycle["lifecycle"] = "EXIT_PENDING" if decision.state.owned_qty else "FLAT_PENDING_CONFIRM"
        if decision.block_new and self._decision_revokes_authority(decision):
            self._armed = False
            self.contexts.revoke_all()
        return self._stage_cycle(cycle,"TX_PROTECT","PROTECTION_OBSERVATION",after=lambda:self._progress_exits(cycle_id))

    def _apply_decision(self,cycle,decision):
        cycle["protection"]=decision.state.to_dict()
        self.statistics.freeze_exit(cycle)
        cycle["cancel_entries"]=cycle.get("cancel_entries",False) or decision.cancel_entry_remainders
        cycle["reasons"]=sorted(set(cycle.get("reasons",[]))|set(decision.reasons))
        if decision.block_new and self._decision_revokes_authority(decision):
            self._armed=False
            self.contexts.revoke_all()
        if decision.state.reconciliation_required:
            self._recovery_pending.add(cycle["position_cycle_id"])
            self.recovery_required=True
        if decision.target_remaining_qty is not None:
            cycle["lifecycle"]="EXIT_PENDING" if decision.state.owned_qty else "FLAT_PENDING_CONFIRM"

    def _decision_revokes_authority(self,decision):
        # A finite DSV run may continue on other contracts/next opportunities.
        # Cycle targets/day stops are enforced by the owner entry gates. They
        # do not masquerade as a user PAUSE, unknown send or source fault.
        return (not getattr(self.simulation,'strategy_enabled',False)
                or decision.state.reconciliation_required or decision.state.quality!='CONTIGUOUS')

    def observe_development_event(self, cycle_id: str, event: dict, *, observed_at_utc=None):
        """Persist an explicit offline session/gap fact through the same owner.

        There is no public IPC or automatic clock source for these facts. The
        installed kernel cannot use this development entry to acquire authority.
        """
        self._writable()
        if not self.development or isinstance(self.port, UnavailablePort):
            raise TraderError("E_UNSUPPORTED", "An isolated development port is required")
        if type(event) is not dict or event.get("type") not in {"DATA_GAP", "SESSION_CLOSE_DUE"}:
            raise TraderError("E_EVENT", "Expected an explicit development session or gap fact")
        canonical_bytes(event)
        cycle=copy.deepcopy(self._staged[cycle_id])
        self.statistics.mark_observation(cycle,observed_at_utc)
        expected={"account_key":self.account_key,"position_cycle_id":cycle_id,"contract_id":cycle["plan"]["contract_id"]}
        if any(name in event and event[name]!=value for name,value in expected.items()):
            raise TraderError("E_SCOPE", "Development protection fact belongs to another scope")
        fact=copy.deepcopy(event)
        fact.update(event_seq=self._sequence+1,event_id=_id("development-protection"),
                    account_key=self.account_key,position_cycle_id=cycle_id,contract_id=cycle["plan"]["contract_id"])
        decision=reduce_event(cycle["plan"],ProtectionState.from_dict(cycle["protection"]),fact)
        self._apply_decision(cycle,decision)
        if decision.state.quality != "CONTIGUOUS":cycle["last_quote"]=None
        cycle["revision"]+=1
        return self._stage_cycle(cycle,"TX_PROTECT","DEVELOPMENT_PROTECTION_EVENT",after=lambda:self._progress_exits(cycle_id))

    def execution_tick(self, cycle_id: str, event: dict):
        """Advance DSV pricing from an explicit offline clock/market fact."""
        return self.execution.tick(cycle_id,event)

    def _record_conflict(self,event_type,details):
        self._armed=False
        self.recovery_required=True
        self._recovery_pending.update(cid for cid,c in self._staged.items() if c['lifecycle']!='CLOSED')
        self.contexts.revoke_all(reason=event_type)
        self._state_revision+=1
        self._authority_revision+=1
        authority=self._authority()
        return self._submit("TX_RECONCILE",event_type,{"conflict":details,"authority":authority},
                            lambda conn,seq:self._write_authority(conn,seq,authority))

    def request_cancel(self, child_id: str, *, retain_exit_target=False):
        self._writable()
        cycle,_ = self._find(child_id)
        order = next(o for o in cycle["orders"] if o["child_order_id"]==child_id)
        if order["terminal_confirmed"] or order["cancel_status"]:
            return
        if retain_exit_target and order['effect']=='REDUCE' and self.execution.active(cycle):
            cycle['execution'].setdefault('cancel_reasons',{})[child_id]='ACCOUNT_CANCEL_RETAIN_EXIT_TARGET'
        if order["attempt_id"] not in self._dispatched and order["kernel_boot_id"]==self.boot_id and order["send_state"]=="SEND_POSSIBLE":
            order.update(send_state="DEFINITELY_NOT_SENT",terminal_confirmed=True,reported_fill_qty=order["filled_qty"],cancel_status="NOT_NEEDED")
            cycle["revision"]+=1
            return self._stage_cycle(cycle,"TX_CANCEL","CANCEL_UNSENT")
        order["cancel_status"]="SEND_POSSIBLE"
        cycle["revision"]+=1
        def send_cancel():
            updated,_ = self._find(child_id)
            item = next(o for o in updated["orders"] if o["child_order_id"]==child_id)
            evidence = None
            try:
                self.port.cancel({"child_order_id":child_id,"account_key":self.account_key})
                item["cancel_status"]="CALL_RETURNED"
            except Exception as error:
                item["cancel_status"]="UNKNOWN"
                evidence = {"exception": exception_record(error), "child_order_id": child_id}
            updated["revision"]+=1
            self._stage_cycle(updated,"TX_CANCEL","CANCEL_RESULT", evidence=evidence,
                              after=lambda:self._progress_exits(cycle['position_cycle_id']))
        self._stage_cycle(cycle,"TX_CANCEL","CANCEL_PREPARED",after=send_cancel)

    def confirm_terminal(self, child_id: str, status: str, cumulative_fill_qty: int,*,_adapter_token=None,_historical_recovery=False):
        self._writable()
        if not (self.development or self.simulation and _adapter_token is self.port.callback_token) or status not in {"FILLED","CANCELLED","REJECTED"}:
            raise TraderError("E_EVIDENCE", "Development terminal evidence is required")
        cycle,_ = self._find(child_id)
        order=next(o for o in cycle["orders"] if o["child_order_id"]==child_id)
        quantity(cumulative_fill_qty)
        if cumulative_fill_qty != order["filled_qty"]:
            self._armed=False
            raise TraderError("E_FACT_BARRIER", "Cumulative quantity and fill details differ")
        if status=="FILLED" and cumulative_fill_qty!=order["original_qty"]:
            raise TraderError("E_FACT_BARRIER", "Filled terminal quantity is incomplete")
        order.update(broker_state=status,reported_fill_qty=cumulative_fill_qty,terminal_confirmed=True,send_state="CONFIRMED")
        if order["cancel_status"]:
            order["cancel_status"]="CONFIRMED" if status=="CANCELLED" else "NOT_NEEDED"
        if _historical_recovery and status=='CANCELLED' and order['effect']=='REDUCE' and self.execution.active(cycle):
            cycle['execution'].setdefault('cancel_reasons',{})[child_id]='HISTORICAL_TERMINAL_RECONCILED'
        cycle["revision"]+=1
        return self._stage_cycle(cycle,"TX_ORDER","ORDER_TERMINAL",after=lambda:self._progress_exits(cycle["position_cycle_id"]))

    def reconcile_development_cycle(self, cycle_id: str, *, close: bool = False, statistics_at_utc=None):
        self._writable()
        if not self.development:
            raise TraderError("E_EVIDENCE", "An online reconciliation adapter is not installed")
        cycle=copy.deepcopy(self._staged[cycle_id])
        if close and cycle.get('statistics_review_required'):
            raise TraderError('E_FACT_BARRIER','Late confirmed fills require an explicit statistics correction review')
        if close and cycle.get('statistics_close_id') and cycle['lifecycle']=='CLOSED' and cycle_id not in self._recovery_pending:
            if any(p.cycle and p.cycle['position_cycle_id']==cycle_id for p in self._pending):
                raise TraderError('E_FACT_BARRIER','Accept the original close transaction before repeating reconciliation')
            result=Future();result.set_result({'status':'ALREADY_RECONCILED','statistics_close_id':cycle['statistics_close_id']});return result
        orders=[o for o in cycle["orders"] if close or o["effect"]=="OPEN"]
        if any(not o["terminal_confirmed"] or o["reported_fill_qty"]!=o["filled_qty"] or o["send_state"]=="UNKNOWN" for o in orders):
            raise TraderError("E_FACT_BARRIER", "Relevant orders are not fully reconciled")
        if any(o["send_state"]=="UNKNOWN" for o in cycle["orders"]):
            raise TraderError("E_FACT_BARRIER", "Unknown exits still require complete evidence")
        cycle["entry_reconciliation_id"]=_id("reconciliation")
        if close:
            if cycle["protection"]["owned_qty"]:
                raise TraderError("E_FACT_BARRIER", "Managed position is not flat")
            cycle["lifecycle"]="CLOSED"
        else:
            decision=reduce_event(cycle["plan"],ProtectionState.from_dict(cycle["protection"]),
                {"type":"ENTRY_TERMINAL_RECONCILED","owned_quantity":cycle["protection"]["owned_qty"],"reconciliation_id":cycle["entry_reconciliation_id"],"event_seq":self._sequence+1,"event_id":_id("evidence")})
            self._apply_decision(cycle,decision)
        cycle["revision"]+=1
        statistics=self.statistics.prepare_close(cycle,statistics_at_utc) if close else None
        def confirmed():
            self._recovery_pending.discard(cycle_id)
            self.recovery_required=bool(self._recovery_pending) or self._has_unknown()
            self._progress_exits(cycle_id)
        return self._stage_cycle(cycle,"TX_RECONCILE","DEVELOPMENT_RECONCILIATION",after=confirmed,nominal_statistics=statistics)

    def _progress_exits(self, cycle_id):
        if self._failed:
            return
        cycle=copy.deepcopy(self._staged[cycle_id])
        if self.execution.active(cycle):
            return self.execution.progress(cycle_id)
        if cycle.get("management_state")=="TAKEN_OVER":return
        if cycle_id in self._recovery_pending:
            return
        state=cycle["protection"]
        target=state.get("target_remaining_qty")
        for order in cycle["orders"]:
            if (target is not None or cycle.get("cancel_entries")) and order["effect"]=="OPEN" and OrderState.from_dict(order).potential_remaining and not order["cancel_status"]:
                self.request_cancel(order["child_order_id"])
        if target is None:
            return
        cycle=copy.deepcopy(self._staged[cycle_id])
        pending=sum(OrderState.from_dict(o).potential_remaining for o in cycle["orders"] if o["effect"]=="REDUCE")
        qty=max(0,state["owned_qty"]-target-pending)
        if qty==0:
            return
        attempt_limit=cycle['plan']['execution_policy']['max_child_order_attempts']
        if self.simulation and attempt_limit is not None and sum(o['effect']=='REDUCE' for o in cycle['orders'])>=attempt_limit:
            if 'E_ATTEMPTS_EXHAUSTED' not in self._errors:self._errors.append('E_ATTEMPTS_EXHAUSTED')
            return
        quote=cycle["last_quote"]
        if not quote or state["quality"]!="CONTIGUOUS" or quote["kernel_boot_id"]!=self.boot_id or self._quote_age_ns(quote["received_ns"])>self.max_quote_age_ms*1_000_000:
            self._errors.append("E_NOT_READY")
            return
        executable_price=quote.get("bid1" if cycle["plan"]["direction"]=="LONG" else "ask1")
        if executable_price is None:
            self._errors.append("E_NOT_READY")
            return
        order_price(executable_price,cycle.get("instrument",{}).get("tick_size",self.tick_size))
        order=OrderState(_id("child"),_id("exit-intent"),_id("attempt"),"REDUCE",qty,executable_price,kernel_boot_id=self.boot_id)
        if getattr(self.simulation,'strategy_enabled',False) and self.simulation.prepare_order(cycle,order) is False:return
        cycle["orders"].append(order.to_dict())
        cycle["revision"]+=1
        self._stage_cycle(cycle,"TX_INTENT","EXIT_PREPARED",after=lambda:self._dispatch(cycle_id,order.child_order_id))

    def close(self):
        if self._closed:
            return
        self._check_owner()
        self._armed=False
        self.contexts.revoke_all(reason='EXECUTOR_CLOSED')
        try:
            try:self.wait_idle()
            finally:self.ledger.close()
        finally:
            if self._owns_lock:self._lock.close()
            self._closed=True

    def __enter__(self): return self
    def __exit__(self,*_): self.close()
