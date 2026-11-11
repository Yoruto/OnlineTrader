"""Owner-side control commands, durable receipts and independently registered evidence.

No method in this module creates a broker connection.  Development registries are
internal QA interfaces and are deliberately absent from the pipe protocol.
"""

from __future__ import annotations

from concurrent.futures import Future
import copy
import uuid

from online_trader.contracts.codec import canonical_bytes, content_hash, payload_hash, strict_loads
from online_trader.contracts.validation import validate_message
from online_trader.domain.orders import OrderState
from online_trader.domain.values import identifier
from online_trader.errors import TraderError
from online_trader.gateway.ports import UnavailablePort


FINAL = frozenset({"SUCCEEDED", "REJECTED", "FAILED", "SUPERSEDED"})
OPERATIONS = frozenset({"PAUSE_NEW_ENTRIES", "ARM_ENTRIES", "FLATTEN_THEN_STOP", "BEGIN_TAKEOVER", "CONFIRM_TAKEOVER",
    "ENTER_MAINTENANCE", "MANUAL_OPEN", "MANUAL_CLOSE", "CANCEL_SELECTED", "CANCEL_OWNED", "CANCEL_ACCOUNT_CONFIRMED",
    "REQUEST_RECONCILIATION", "RESUME_ORIGINAL_EXITS"})


def _new(prefix): return prefix + "-" + uuid.uuid4().hex
def _ready(value):
    future = Future(); future.set_result(copy.deepcopy(value)); return future


class ControlProtocol:
    def __init__(self, kernel):
        kernel._check_owner()
        self.kernel = kernel
        self.states = {}
        self.policies = {r["policy_id"]: strict_loads(r["policy_json"]) for r in kernel.ledger.query("SELECT policy_id,policy_json FROM policy_revisions")}
        self.evidence = {}
        self.takeovers = {r["takeover_id"]: dict(r) for r in kernel.ledger.query("SELECT * FROM takeover_records")}
        self.maintenance = next(iter(kernel.ledger.query("SELECT * FROM maintenance_locks")), None)
        kernel._maintenance_state = None if self.maintenance is None else self.maintenance["status"]
        self._updating = set()
        self._polling = False
        self.symbol_entries={};self._symbol_staged={}
        for row in kernel.ledger.query("SELECT command_id,payload_hash,payload_json,result_json FROM operator_commands"):
            self.states[row["command_id"]] = {"body": strict_loads(row["payload_json"]), "digest": row["payload_hash"], "receipt": strict_loads(row["result_json"]), "metadata": {}}
        for row in kernel.ledger.query("SELECT payload_json FROM event_journal ORDER BY event_seq"):
            data = strict_loads(row["payload_json"]); data = data.get("data", data)
            if data.get("control_metadata"):
                item = data["control_metadata"]
                if item["command_id"] in self.states: self.states[item["command_id"]]["metadata"] = item["metadata"]
            if data.get("control_evidence"):
                item = data["control_evidence"]; self.evidence[item["id"]] = item
            if data.get('symbol_entry_state'):self.symbol_entries.update(data['symbol_entry_state'])
        self._symbol_staged=copy.deepcopy(self.symbol_entries)
        # A safe window from another boot is never a current stopping decision.
        if self.maintenance and self.maintenance["status"]=="SAFE_TO_STOP":self.poll()

    def _development(self):
        self.kernel._writable()
        if not self.kernel.development:
            raise TraderError("E_RULE_UNAPPROVED", "Only internal development registries are available")

    def register_policy(self, policy_id, kind, payload):
        self._development(); policy_id = identifier(policy_id)
        if payload.get("execution_scope") != "DEVELOPMENT" or kind not in {"AUTHORITY", "EXECUTION", "RESUME", "EMERGENCY", "CALENDAR", "MARKET_QUALITY", "COUNTERS"}:
            raise TraderError("E_RULE_UNAPPROVED", "An explicit development policy is required")
        if self.kernel._pending: raise TraderError("E_FACT_BARRIER", "Register policies at an accepted boundary")
        material = copy.deepcopy(payload) | {"account_key": self.kernel.account_key, "kernel_boot_id": self.kernel.boot_id, "policy_kind": kind}
        if policy_id in self.policies:
            if self.policies[policy_id] == material: return
            raise TraderError("E_DUPLICATE_CONFLICT", "Policy identity cannot change content")
        def mutate(conn, seq):
            conn.execute("INSERT INTO policy_revisions VALUES(?,?,?,?,?,?)", (policy_id, kind, self.kernel._configuration["rule_set_id"], canonical_bytes(material).decode(), content_hash(material), seq))
        self.kernel._submit("TX_CONFIG", "DEVELOPMENT_CONTROL_POLICY", {"policy_id": policy_id, "policy": material}, mutate,
                            after=lambda: self.policies.__setitem__(policy_id, material))
        self.kernel.wait_idle()

    def _cycles(self, scope, *, committed=False):
        if set(scope) != {"scope_type", "scope_ids"}: raise TraderError("E_SCOPE", "Invalid command scope")
        kind, ids = scope["scope_type"], scope["scope_ids"]
        if len(ids) != len(set(ids)): raise TraderError("E_SCOPE", "Repeated scope identities")
        source = self.kernel._committed if committed else self.kernel._staged
        if kind in {"ACCOUNT", "OWNED_ORDERS"}:
            if ids: raise TraderError("E_SCOPE", "Account scope cannot contain object IDs")
            return sorted(source)
        if not ids: raise TraderError("E_SCOPE", "Object scope must be explicit")
        if kind == "CYCLE":
            if not set(ids) <= set(source): raise TraderError("E_OWNERSHIP", "Cycle is not owned")
            return sorted(ids)
        if kind == "CONTRACT":
            return sorted(cid for cid, c in source.items() if c["plan"]["contract_id"] in ids)
        if kind == "SELECTED_ORDERS":
            mapping = {o["child_order_id"]: cid for cid, c in source.items() for o in c["orders"]}
            if not set(ids) <= set(mapping): raise TraderError("E_OWNERSHIP", "Order is not owned")
            return sorted({mapping[key] for key in ids})
        raise TraderError("E_SCOPE", "Unknown scope")

    def risk_manifest(self, scope):
        self.kernel._check_owner()
        cycles = []
        for cid in self._cycles(scope):
            c = self.kernel._staged[cid]; s = c["protection"]
            cycles.append({"position_cycle_id": cid, "plan_hash": content_hash(c["plan"]), "owned_quantity": s["owned_qty"],
                "protection_mode": s["mode"], "max_favorable_move": s["max_favorable_move"], "target_remaining_quantity": s.get("target_remaining_qty"),
                "orders": [{"child_order_id": o["child_order_id"], "attempt_id": o["attempt_id"], "effect": o["effect"],
                    "potential_quantity": OrderState.from_dict(o).potential_remaining, "send_state": o["send_state"],
                    "confirmed_fill_quantity": o["filled_qty"], "terminal_confirmed": o["terminal_confirmed"]} for o in c["orders"]]})
        return {"account_key": self.kernel.account_key, "scope": copy.deepcopy(scope), "cycles": cycles}

    def manifest_hash(self, scope): return content_hash(self.risk_manifest(scope))

    def register_evidence(self, run_id, scope=None, *, complete=True, confirmation_id=None):
        self._development(); run_id = identifier(run_id)
        if self.kernel._pending: raise TraderError("E_FACT_BARRIER", "Evidence requires an accepted fact boundary")
        scope = copy.deepcopy(scope or {"scope_type": "ACCOUNT", "scope_ids": []})
        cycle_ids = self._cycles(scope)
        item = {"id": run_id, "account_key": self.kernel.account_key, "kernel_boot_id": self.kernel.boot_id,
            "scope": scope, "complete": bool(complete), "parts": ["FUNDS", "POSITIONS", "ORDERS", "TRADES"] if complete else ["POSITIONS"],
            "cycle_revisions": {cid: self.kernel._staged[cid]["revision"] for cid in cycle_ids},
            "manifest_hash": self.manifest_hash(scope), "barrier_seq": str(self.kernel._durable_seq),
            "confirmation_id": confirmation_id, "execution_scope": "DEVELOPMENT"}
        def mutate(conn, seq):
            conn.execute("INSERT INTO reconciliation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(reconciliation_id) DO UPDATE SET status=excluded.status,scope_json=excluded.scope_json,barrier_event_seq=excluded.barrier_event_seq,difference_count=excluded.difference_count,completed_event_seq=excluded.completed_event_seq",
                (run_id,"DEV:CONTROL",1,"1970-01-01","COMPLETE" if complete else "INCOMPLETE",canonical_bytes(item).decode(),0,seq,0,seq,seq if complete else None))
            for part in ("FUNDS", "POSITIONS", "ORDERS", "TRADES"):
                done = complete or part == "POSITIONS"
                conn.execute("INSERT INTO reconciliation_parts VALUES(?,?,?,?,?,?,?) ON CONFLICT(reconciliation_id,part_kind) DO UPDATE SET status=excluded.status,coverage_json=excluded.coverage_json,completion_evidence_json=excluded.completion_evidence_json,last_ingress_seq=excluded.last_ingress_seq,snapshot_json=excluded.snapshot_json",
                    (run_id,part,"COMPLETE" if done else "UNREQUESTED","{}",'{"development_only":true}' if done else None,0,"{}"))
        self.kernel._submit("TX_RECONCILE", "DEVELOPMENT_CONTROL_EVIDENCE", {"control_evidence": item}, mutate,
                            after=lambda: self.evidence.__setitem__(run_id, item))
        self.kernel.wait_idle()

    def _policy(self, policy_id, kind):
        if self.kernel.simulation and (kind=='EXECUTION' or kind=='AUTHORITY' and getattr(self.kernel.simulation,'strategy_enabled',False)):
            grant=self.kernel.simulation.grant
            rows=self.kernel.ledger.query("SELECT policy_json FROM policy_revisions WHERE policy_id=? AND policy_kind=?",(policy_id,kind))
            if rows and strict_loads(rows[0]['policy_json']).get('grant_hash')==grant.content_hash:
                return {'execution_scope':'ONLINE_SIMULATION','policy_kind':kind,'account_key':self.kernel.account_key,
                        'allowed_origins':strict_loads(rows[0]['policy_json']).get('allowed_origins',[])}
        policy = self.policies.get(policy_id)
        if not self.kernel.development or not policy or policy.get("execution_scope") != "DEVELOPMENT" or policy.get("kernel_boot_id") != self.kernel.boot_id or policy.get("account_key") != self.kernel.account_key or policy.get("policy_kind") != kind:
            raise TraderError("E_RULE_UNAPPROVED", "No applicable approved online or isolated development policy exists")
        return policy

    def _evidence(self, run_id, scope):
        item = self.evidence.get(run_id)
        if not item or not item["complete"] or set(item["parts"]) != {"FUNDS", "POSITIONS", "ORDERS", "TRADES"}:
            raise TraderError("E_FACT_BARRIER", "Four complete fact ranges are required")
        if item["account_key"] != self.kernel.account_key or item["kernel_boot_id"] != self.kernel.boot_id:
            raise TraderError("E_BOOT_MISMATCH", "Evidence belongs to another account or boot")
        source = item["scope"]
        if source["scope_type"] != "ACCOUNT" and (scope["scope_type"] != source["scope_type"] or not set(scope["scope_ids"]) <= set(source["scope_ids"])):
            raise TraderError("E_FACT_BARRIER", "Reconciliation scope does not cover the requested fact range")
        ids = self._cycles(scope)
        covered = set(item["cycle_revisions"])
        if scope["scope_type"] == "ACCOUNT" and covered != set(ids): raise TraderError("E_FACT_BARRIER", "Account evidence does not cover current cycles")
        if not set(ids) <= covered: raise TraderError("E_FACT_BARRIER", "Evidence does not cover this operation")
        for cid in ids:
            if item["cycle_revisions"][cid] != self.kernel._staged[cid]["revision"] or self.kernel._staged[cid] != self.kernel._committed.get(cid):
                raise TraderError("E_FACT_BARRIER", "Relevant facts changed after reconciliation")
        if scope == item["scope"] and item["manifest_hash"] != self.manifest_hash(scope):
            raise TraderError("E_FACT_BARRIER", "Residual risk changed after reconciliation")
        return item

    def _stage(self, cycles, kind, event_type, *, extra=None, after=None):
        return self.kernel.stage_control_cycles(copy.deepcopy(cycles), kind, event_type, extra_mutate=extra, after=after)

    def bind_submission(self, binding, seq, tx_id, data, mutate, after):
        if binding.get("bound"): return tx_id, data, mutate, after
        binding["bound"] = True
        tx_id = tx_id or _new("tx-control")
        if binding["body"]["operation"] == "MANUAL_OPEN" and data.get("cycle"):
            binding["metadata"]["order_ids"] = [o["child_order_id"] for o in data["cycle"]["orders"] if o["effect"] == "OPEN"]
        receipt = {"command_id": binding["body"]["command_id"], "payload_hash": binding["digest"], "status": binding["initial_status"],
            "receipt_revision": "1", "tx_id": tx_id, "event_seq": str(seq), "durable_seq": str(seq),
            "state_revision": str(self.kernel._state_revision), "error_code": binding.get("error_code", "NONE"),
            "reason": binding.get("reason", "Command accepted; inspect its recorded stage"), "evidence_ids": binding.get("evidence_ids", [])}
        binding["receipt"] = receipt
        command = binding["body"]
        wrapped_data = copy.deepcopy(data) | {"control_metadata": {"command_id": command["command_id"], "metadata": copy.deepcopy(binding["metadata"])}}
        def wrapped_mutate(conn, actual_seq):
            conn.execute("INSERT INTO operator_commands(command_id,context_id,authenticated_peer_reference,operation,payload_hash,payload_json,status,expected_kernel_boot_id,expected_state_revision,receipt_revision,result_json,created_event_seq,updated_event_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (command["command_id"],command["context_id"],binding["peer_id"],command["operation"],binding["digest"],canonical_bytes(command).decode(),receipt["status"],
                 binding["expected_boot"],int(command["expected_revisions"]["state_revision"]),1,canonical_bytes(receipt).decode(),actual_seq,actual_seq))
            if mutate: mutate(conn, actual_seq)
        def committed():
            if after: after()
            self.committed(binding, receipt)
        return tx_id, wrapped_data, wrapped_mutate, committed

    def committed(self, binding, receipt):
        state = self.states[binding["body"]["command_id"]]
        state["receipt"] = copy.deepcopy(receipt); state.pop("future", None)
        if not binding["result"].done(): binding["result"].set_result(copy.deepcopy(receipt))

    def get_status(self, command_id, digest):
        self.kernel._check_owner()
        state = self.states.get(command_id)
        if not state or state["digest"] != digest: raise TraderError("E_OWNERSHIP", "Command record is unavailable")
        return state["future"] if "future" in state else _ready(state["receipt"])

    def execute(self, message, peer_id):
        self.kernel._writable(); validate_message(message)
        if message["type"] != "OperatorCommand" or message["account_key"] != self.kernel.account_key:
            raise TraderError("E_SCOPE", "Command account is not bound to this endpoint")
        body = copy.deepcopy(message["body"]); digest = payload_hash(message); cid = body["command_id"]
        if digest != body["payload_hash"]: raise TraderError("E_SCHEMA", "Command payload hash differs")
        if cid in self.states:
            if self.states[cid]["digest"] != digest: raise TraderError("E_DUPLICATE_CONFLICT", "Command identity changed content")
            return self.get_status(cid, digest)
        binding = {"body": body, "digest": digest, "peer_id": peer_id, "result": Future(), "initial_status": "COMMITTED",
            "expected_boot": message["kernel_boot_id"], "metadata": {"operation": body["operation"], "scope": body["scope"]}}
        self.states[cid] = {"body": body, "digest": digest, "future": binding["result"], "metadata": binding["metadata"]}
        self.kernel._command_binding = binding
        try:
            if message["kernel_boot_id"] != self.kernel.boot_id: raise TraderError("E_BOOT_MISMATCH", "Command is for a different boot")
            operation, scope = body["operation"], body["scope"]
            if operation not in OPERATIONS: raise TraderError("E_SCHEMA", "Unknown operation")
            scope_id = self._context_scope(body)
            self.kernel.contexts.validate(body["context_id"],peer_id,"OPERATOR_COMMAND",operation,scope_id,
                kernel_boot_id=self.kernel.boot_id,kernel_epoch=body["kernel_epoch"],revisions=body["expected_revisions"])
            if body["expected_revisions"] != self.kernel.revisions: raise TraderError("E_REVISION_CONFLICT", "Relevant state changed")
            self._dispatch(binding)
            if not binding.get("bound"):
                self._stage([], "TX_AUTH", "CONTROL_COMMAND")
        except TraderError as error:
            if binding.get("bound"):
                binding["reason"] = "Accepted operation needs follow-up reconciliation"
            else:
                binding.update(initial_status="REJECTED",error_code=error.code if error.code in _ERRORS else "E_NOT_READY",reason=str(error))
                self._stage([], "TX_AUTH", "CONTROL_REJECTED")
        finally:
            self.kernel._command_binding = None
        return binding["result"]

    def _context_scope(self, body):
        scope, params, op = body["scope"], body["parameters"], body["operation"]
        if op == "MANUAL_OPEN":
            if scope != {"scope_type":"CONTRACT","scope_ids":[params["contract_id"]]}: raise TraderError("E_SCOPE", "Open contract scope differs")
            return params["contract_id"]
        if op == "MANUAL_CLOSE":
            if scope != {"scope_type":"CYCLE","scope_ids":[params["position_cycle_id"]]}: raise TraderError("E_SCOPE", "Close cycle scope differs")
            return params["position_cycle_id"]
        self._cycles(scope)
        if scope["scope_type"] in {"ACCOUNT", "OWNED_ORDERS"}: return self.kernel.account_key
        return scope["scope_ids"][0] if len(scope["scope_ids"]) == 1 else content_hash(scope)

    def context_scope(self, operation, scope, parameters=None):
        return self._context_scope({"operation":operation,"scope":scope,"parameters":parameters or {}})

    def can_increase(self, contract_id=None):
        if contract_id is not None and not self.symbol_entry_enabled(contract_id):return False
        if self.maintenance and self.maintenance["status"] not in {"COMPLETE", "FAILED"}: return False
        if any(c.get("management_state") in {"TAKEOVER_PENDING","TAKEN_OVER"} and (contract_id is None or c["plan"]["contract_id"] == contract_id) for c in self.kernel._staged.values()): return False
        if any(s["metadata"].get("operation") == "ENTER_MAINTENANCE" and s.get("receipt",{}).get("status") not in FINAL for s in self.states.values()): return False
        return not any(s["metadata"].get("operation") == "FLATTEN_THEN_STOP" and s.get("receipt",{}).get("status") not in FINAL for s in self.states.values())

    def symbol_entry_enabled(self,contract_id,*,committed=False):
        current=self.symbol_entries.get(contract_id,{}).get('enabled',True)
        return current if committed else current and self._symbol_staged.get(contract_id,{}).get('enabled',True)

    def _symbol_entry_command(self,binding):
        k=self.kernel;b=binding['body'];scope=b['scope'];p=b['parameters'];enabled=b['operation']=='ARM_ENTRIES'
        runtime=getattr(k,'_dsv_runtime',None)
        if runtime is None:raise TraderError('E_UNSUPPORTED','Activate the original strategy before changing contract entry switches')
        allowed={s['code'] for s in runtime.profile.to_record()['settings']['symbols']}
        if k.simulation:allowed&=set(k.simulation.grant.value['contracts'])
        codes=scope['scope_ids']
        if not codes or not set(codes)<=allowed:raise TraderError('E_SCOPE','Contract switches are confined to the current strategy scope')
        if enabled:
            policy=self._policy(p['authority_policy_id'],'AUTHORITY')
            origin='STRATEGY' if getattr(k.simulation,'strategy_enabled',False) else 'MANUAL'
            if origin not in policy.get('allowed_origins',[]):raise TraderError('E_RULE_UNAPPROVED','Current policy does not permit this strategy origin')
            if p['rule_set_id']!=k._configuration['rule_set_id'] or p['config_revision']!=str(k._config_revision):
                raise TraderError('E_RULE_UNAPPROVED','Contract enable must bind the current immutable configuration')
        update={code:{'enabled':enabled,'revision':self._symbol_staged.get(code,{}).get('revision',0)+1,
                      'command_id':b['command_id']} for code in codes}
        self._symbol_staged.update(copy.deepcopy(update))
        k._authority_revision+=1;k._state_revision+=1;k.contexts.revoke_all()
        targets=[]
        if not enabled and p['cancel_owned_entry_remainders']:
            targets=[order['child_order_id'] for cycle in k._staged.values() if cycle['plan']['contract_id'] in codes
                     for order in cycle['orders'] if order['effect']=='OPEN' and OrderState.from_dict(order).potential_remaining]
        binding['metadata'].update(symbol_entry_update=update,order_ids=targets)
        binding.update(initial_status='EXECUTING' if targets else 'SUCCEEDED',
            reason='Contract entry switch saved; global authority and original protection remain separate')
        authority=k._authority()
        def accepted():
            self.symbol_entries.update(copy.deepcopy(update))
            for child in targets:k.request_cancel(child)
        k._submit('TX_AUTH','SYMBOL_ENTRY_SWITCH',{'symbol_entry_state':update,'authority':authority},
            lambda conn,seq:k._write_authority(conn,seq,authority),after=accepted)

    def _dispatch(self, binding):
        k = self.kernel; b = binding["body"]; op, p, scope = b["operation"], b["parameters"], b["scope"]
        ids = self._cycles(scope); binding["metadata"]["cycle_ids"] = ids
        if op in {'PAUSE_NEW_ENTRIES','ARM_ENTRIES'} and scope['scope_type']=='CONTRACT':
            return self._symbol_entry_command(binding)
        if op in {"PAUSE_NEW_ENTRIES", "ARM_ENTRIES", "FLATTEN_THEN_STOP", "ENTER_MAINTENANCE", "CANCEL_ACCOUNT_CONFIRMED"} and scope["scope_type"] != "ACCOUNT":
            raise TraderError("E_SCOPE", "This command requires the whole account scope")
        if op == "PAUSE_NEW_ENTRIES":
            binding.update(initial_status="SUCCEEDED",reason="New entries disabled; existing protection retained")
            targets = [o["child_order_id"] for cid in ids for o in k._staged[cid]["orders"] if o["effect"] == "OPEN" and OrderState.from_dict(o).potential_remaining]
            k._armed=False;k.contexts.revoke_all();k._authority_revision+=1
            if p["cancel_owned_entry_remainders"] and targets:
                binding["metadata"]["order_ids"] = targets
                if (k.development or k.simulation) and not isinstance(k.port,UnavailablePort):
                    binding["initial_status"]="EXECUTING";self._cancel_batch(targets)
                else:
                    binding.update(initial_status="NEEDS_RECONCILIATION",error_code="E_NOT_READY",reason="New entries disabled; entry remainders need trusted cancellation evidence")
                    self._stage([],"TX_AUTH","CONTROL_PAUSED")
            else:self._stage([],"TX_AUTH","CONTROL_PAUSED")
        elif op == "ARM_ENTRIES":
            policy = self._policy(p["authority_policy_id"], "AUTHORITY")
            online_dsv=bool(getattr(k.simulation,'strategy_enabled',False))
            origin='STRATEGY' if online_dsv else 'MANUAL'
            if not self.can_increase() or origin not in policy.get("allowed_origins",[]): raise TraderError("E_RULE_UNAPPROVED", "Authority policy cannot enable the requested entry origin")
            if p["rule_set_id"] != k._configuration["rule_set_id"] or p["config_revision"] != str(k._config_revision): raise TraderError("E_RULE_UNAPPROVED", "Rule/configuration identity is not applicable")
            binding["metadata"]["authority_revision"] = k._authority_revision + 1
            if online_dsv:k.simulation.arm()
            else:k.arm_development()
        elif op == "MANUAL_OPEN":
            self._policy(p["order_policy_id"], "EXECUTION")
            if not self.can_increase(): raise TraderError("E_NOT_READY", "A stop or maintenance decision blocks new risk")
            plan = p["protection_plan"]
            if (plan["contract_id"],plan["direction"],plan["initial_open_quantity"]) != (p["contract_id"],p["direction"],p["quantity"]): raise TraderError("E_SCHEMA", "Manual order and protection plan disagree")
            binding["initial_status"] = "EXECUTING"; binding["metadata"]["cycle_ids"] = [plan["position_cycle_id"]]
            child = k.admit_entry(plan, "command-" + content_hash(b["command_id"])[:40], p["limit_price"], context_id=b["context_id"], peer_id=binding["peer_id"])
            binding["metadata"]["order_ids"] = [child]
        elif op in {"MANUAL_CLOSE", "FLATTEN_THEN_STOP"}:
            self._policy(p["execution_policy_id"], "EXECUTION")
            if not (k.development or k.simulation) or isinstance(k.port, UnavailablePort): raise TraderError("E_RULE_UNAPPROVED", "A permitted execution adapter is required")
            if op == "MANUAL_CLOSE":
                if p["external_takeover_id"] is not None: raise TraderError("E_OWNERSHIP", "External positions have no registered execution owner in G1")
                c = k._staged[p["position_cycle_id"]]
                if p["quantity"] > c["protection"]["owned_qty"]: raise TraderError("E_RISK", "Close quantity exceeds owned position")
                binding["metadata"]["desired_remaining"] = {p["position_cycle_id"]:c["protection"]["owned_qty"]-p["quantity"]}
                binding["initial_status"] = "EXECUTING"; k.manual_close(p["position_cycle_id"], p["quantity"])
            else:
                binding["metadata"]["desired_remaining"] = {cid:0 for cid in ids}
                binding["initial_status"] = "EXECUTING"; k.flatten(ids)
        elif op in {"CANCEL_SELECTED", "CANCEL_OWNED", "CANCEL_ACCOUNT_CONFIRMED"}:
            targets = self._cancel_targets(op,p,scope,ids)
            binding["metadata"]["order_ids"] = targets
            binding["initial_status"] = "SUCCEEDED" if not targets else "EXECUTING"
            if targets and (not (k.development or k.simulation) or isinstance(k.port,UnavailablePort)):
                binding.update(initial_status="NEEDS_RECONCILIATION",error_code="E_NOT_READY",reason="Cancellation cannot be confirmed without an execution adapter")
            elif targets:self._cancel_batch(targets)
        elif op == "REQUEST_RECONCILIATION":
            self._request_reconciliation(binding)
        elif op == "RESUME_ORIGINAL_EXITS":
            self._policy(p["recovery_authorization_id"],"RESUME")
            if not p["position_cycle_ids"] or set(p["position_cycle_ids"]) != set(ids): raise TraderError("E_SCOPE", "Recovery cycle scope must match exactly")
            self._evidence(p["reconciliation_run_id"],scope)
            if any(o["send_state"] == "UNKNOWN" for cid in ids for o in k._staged[cid]["orders"]):raise TraderError("E_UNKNOWN_SEND", "Unknown attempts still block resume")
            cycles = [copy.deepcopy(k._staged[cid]) for cid in ids]
            for c in cycles:
                if c.get("management_state") == "TAKEN_OVER": raise TraderError("E_OWNERSHIP", "Transferred management cannot be resumed by an old target")
                c["protection"]["reconciliation_required"] = False;c["revision"] += 1
            binding.update(initial_status="SUCCEEDED",reason="Original exit management resumed; no new entry authority granted",evidence_ids=[p["reconciliation_run_id"]])
            def resumed():
                for cid in ids:k._recovery_pending.discard(cid)
                k.recovery_required = bool(k._recovery_pending) or k._has_unknown()
                for cid in ids:k._progress_exits(cid)
            self._stage(cycles,"TX_RECONCILE","CONTROL_RESUME_ORIGINAL_EXITS",after=resumed)
        elif op == "BEGIN_TAKEOVER": self._begin_takeover(binding)
        elif op == "CONFIRM_TAKEOVER": self._confirm_takeover(binding)
        elif op == "ENTER_MAINTENANCE": self._enter_maintenance(binding)

    def _cancel_targets(self,op,p,scope,ids):
        orders={o["child_order_id"]:o for cid in ids for o in self.kernel._staged[cid]["orders"]}
        if op=="CANCEL_SELECTED":
            if scope["scope_type"]!="SELECTED_ORDERS" or set(scope["scope_ids"])!=set(p["child_order_ids"]) or not p["child_order_ids"]:raise TraderError("E_SCOPE","Selected cancellation scope differs")
            selected=p["child_order_ids"]
        elif op=="CANCEL_OWNED": selected=[key for key,o in orders.items() if p["include_exits"] or o["effect"]=="OPEN"]
        else:
            evidence=self._evidence(p["reconciliation_run_id"],scope)
            if evidence.get("confirmation_id") != p["confirmation_id"] or p["manifest_hash"] != self.manifest_hash(scope):raise TraderError("E_FACT_BARRIER","Account cancellation confirmation does not match current risk")
            selected=list(orders)
        if len(selected)!=len(set(selected)):raise TraderError("E_SCHEMA","Repeated cancellation order IDs")
        return [key for key in selected if OrderState.from_dict(orders[key]).potential_remaining]

    def _cancel_batch(self,targets):
        """Prepare every selected cancellation in the command's first transaction."""
        k=self.kernel;selected=set(targets);cycles=[];prepared=[]
        for original in k._staged.values():
            cycle=copy.deepcopy(original);changed=False
            for order in cycle["orders"]:
                if order["child_order_id"] not in selected or order["terminal_confirmed"] or order["cancel_status"]:continue
                changed=True
                if order["attempt_id"] not in k._dispatched and order["kernel_boot_id"]==k.boot_id and order["send_state"]=="SEND_POSSIBLE":
                    order.update(send_state="DEFINITELY_NOT_SENT",terminal_confirmed=True,reported_fill_qty=order["filled_qty"],cancel_status="NOT_NEEDED")
                else:
                    order["cancel_status"]="SEND_POSSIBLE";prepared.append((cycle["position_cycle_id"],order["child_order_id"]))
            if changed:cycles.append(cycle)
        def send_prepared():
            results={}
            for cycle_id,order_id in prepared:
                cycle=results.setdefault(cycle_id,copy.deepcopy(k._staged[cycle_id]))
                order=next(o for o in cycle["orders"] if o["child_order_id"]==order_id)
                if order["terminal_confirmed"] or order["cancel_status"]!="SEND_POSSIBLE":continue
                try:
                    k.port.cancel({"child_order_id":order_id,"account_key":k.account_key})
                    order["cancel_status"]="CALL_RETURNED"
                except Exception:order["cancel_status"]="UNKNOWN"
            if results:self._stage(list(results.values()),"TX_CANCEL","CONTROL_CANCEL_RESULT")
        self._stage(cycles,"TX_CANCEL","CONTROL_CANCEL_PREPARED",after=send_prepared)

    def _request_reconciliation(self,binding):
        p=binding["body"]["parameters"];scope=binding["body"]["scope"]
        if p["requested_scope"]=="FULL_ACCOUNT":
            if p["position_cycle_id"] is not None or scope!={"scope_type":"ACCOUNT","scope_ids":[]}:raise TraderError("E_SCOPE","Full account reconciliation scope differs")
        elif scope!={"scope_type":"CYCLE","scope_ids":[p["position_cycle_id"]]}:raise TraderError("E_SCOPE","Original cycle reconciliation scope differs")
        run_id="control-recon-"+content_hash(binding["body"]["command_id"])[:32]
        binding["metadata"]["run_id"]=run_id
        binding.update(initial_status="NEEDS_RECONCILIATION",error_code="E_NOT_READY",reason="Reconciliation requested; complete source facts have not arrived",evidence_ids=[run_id])
        def extra(conn,seq):
            conn.execute("INSERT INTO reconciliation_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",(run_id,"G1:NO_ADAPTER",1,"1970-01-01","IN_PROGRESS",canonical_bytes(scope).decode(),0,None,0,seq,None))
            for part in ("FUNDS","POSITIONS","ORDERS","TRADES"):conn.execute("INSERT INTO reconciliation_parts VALUES(?,?,?,?,?,?,?)",(run_id,part,"REQUESTED","{}",None,0,None))
        self._stage([],"TX_RECONCILE","CONTROL_RECONCILIATION_REQUESTED",extra=extra)

    def _begin_takeover(self,binding):
        k=self.kernel;b=binding["body"];p=b["parameters"];scope=b["scope"];ids=binding["metadata"]["cycle_ids"]
        if scope["scope_type"] not in {"ACCOUNT","CYCLE"}:raise TraderError("E_SCOPE","Takeover scope must identify managed cycles or account")
        if p["takeover_id"] in self.takeovers:raise TraderError("E_DUPLICATE_CONFLICT","Takeover identity already exists")
        manifest=self.risk_manifest(scope);binding["metadata"]["takeover_id"]=p["takeover_id"]
        k._armed=False;k.contexts.revoke_all();k._state_revision+=1;k._authority_revision+=1
        cycles=[copy.deepcopy(k._staged[cid]) for cid in ids]
        for c in cycles:c["management_state"]="TAKEOVER_PENDING";c["revision"]+=1
        record={"takeover_id":p["takeover_id"],"command_id":b["command_id"],"status":"TAKEOVER_PENDING","scope_json":canonical_bytes(scope).decode(),
            "residual_risk_manifest_json":canonical_bytes(manifest).decode(),"confirmed_by_reference":None,"reconciliation_id":None}
        def extra(conn,seq):
            conn.execute("INSERT INTO takeover_records VALUES(?,?,?,?,?,?,?,?)",(record["takeover_id"],record["command_id"],record["status"],record["scope_json"],record["residual_risk_manifest_json"],None,None,seq))
        binding.update(initial_status="SUCCEEDED",reason="Takeover pending; existing protection retained; manifest="+content_hash(manifest),evidence_ids=[p["takeover_id"]])
        self._stage(cycles,"TX_TAKEOVER","CONTROL_TAKEOVER_PENDING",extra=extra,after=lambda:self.takeovers.__setitem__(p["takeover_id"],record))

    def _confirm_takeover(self,binding):
        k=self.kernel;b=binding["body"];p=b["parameters"];scope=b["scope"];record=self.takeovers.get(p["takeover_id"])
        if not record or record["status"]!="TAKEOVER_PENDING" or strict_loads(record["scope_json"])!=scope:raise TraderError("E_OWNERSHIP","Takeover scope is not pending")
        self._evidence(p["reconciliation_run_id"],scope)
        manifest=self.risk_manifest(scope)
        if p["manifest_hash"] != content_hash(manifest) or content_hash(strict_loads(record["residual_risk_manifest_json"]))!=p["manifest_hash"]:raise TraderError("E_FACT_BARRIER","Takeover risk manifest changed")
        pending={o["child_order_id"] for c in manifest["cycles"] for o in c["orders"] if o["potential_quantity"]}
        unknown={o["attempt_id"] for c in manifest["cycles"] for o in c["orders"] if o["send_state"] in {"UNKNOWN","SEND_POSSIBLE"}}
        if set(p["accepted_order_ids"])!=pending or set(p["accepted_unknown_attempt_ids"])!=unknown:raise TraderError("E_FACT_BARRIER","Every residual order and unknown attempt must be explicitly accepted")
        cycles=[copy.deepcopy(k._staged[cid]) for cid in binding["metadata"]["cycle_ids"]]
        for c in cycles:c["management_state"]="TAKEN_OVER";c["revision"]+=1
        record=copy.deepcopy(record);record.update(status="TAKEN_OVER",confirmed_by_reference=binding["peer_id"],reconciliation_id=p["reconciliation_run_id"])
        def extra(conn,seq):conn.execute("UPDATE takeover_records SET status='TAKEN_OVER',confirmed_by_reference=?,reconciliation_id=?,updated_event_seq=? WHERE takeover_id=?",(binding["peer_id"],p["reconciliation_run_id"],seq,p["takeover_id"]))
        binding.update(initial_status="SUCCEEDED",reason="Explicit residual risk transferred; original targets will not replenish",evidence_ids=[p["takeover_id"],p["reconciliation_run_id"]])
        self._stage(cycles,"TX_TAKEOVER","CONTROL_TAKEOVER_CONFIRMED",extra=extra,after=lambda:self.takeovers.__setitem__(p["takeover_id"],record))

    def _enter_maintenance(self,binding):
        k=self.kernel;p=binding["body"]["parameters"];scope=binding["body"]["scope"]
        if self.maintenance and self.maintenance["status"] not in {"COMPLETE","FAILED"}:raise TraderError("E_NOT_READY","A maintenance lock already exists")
        handover=p["handover_record_id"]
        if handover:
            record=self.takeovers.get(handover)
            if not record or record["status"]!="TAKEN_OVER" or strict_loads(record["scope_json"])!=scope:raise TraderError("E_FACT_BARRIER","Account-wide explicit handover is required")
        k._armed=False;k.contexts.revoke_all();k._state_revision+=1;k._authority_revision+=1
        record={"singleton":1,"maintenance_token":p["maintenance_id"],"status":"QUIESCING","command_id":binding["body"]["command_id"],"reconciliation_id":None,"barrier_event_seq":None}
        binding["metadata"].update(maintenance_id=p["maintenance_id"],handover_record_id=handover)
        binding.update(initial_status="NEEDS_RECONCILIATION",error_code="E_NOT_READY",reason="Maintenance excludes new risk; safe stop is not yet proven")
        def extra(conn,seq):conn.execute("INSERT INTO maintenance_locks VALUES(?,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET maintenance_token=excluded.maintenance_token,status=excluded.status,command_id=excluded.command_id,reconciliation_id=excluded.reconciliation_id,barrier_event_seq=excluded.barrier_event_seq,updated_event_seq=excluded.updated_event_seq",(1,p["maintenance_id"],"QUIESCING",binding["body"]["command_id"],None,None,seq))
        def accepted():
            self.maintenance = record
            k._maintenance_state = "QUIESCING"
        self._stage([],"TX_MAINTENANCE","CONTROL_MAINTENANCE_REQUESTED",extra=extra,after=accepted)

    def poll(self):
        if self._polling or self.kernel._failed:return
        self._polling=True
        try:
            if self.maintenance and self.maintenance["status"]=="SAFE_TO_STOP":
                command_id=self.maintenance["command_id"];state=self.states.get(command_id)
                item=self.evidence.get(self.maintenance.get("reconciliation_id"))
                if state and command_id not in self._updating and (not item or not self._usable_evidence(item,{"scope_type":"ACCOUNT","scope_ids":[]})):
                    self._advance(command_id,state,"NEEDS_RECONCILIATION","E_FACT_BARRIER","New facts invalidated the empty-account maintenance barrier",[])
            for cid,state in tuple(self.states.items()):
                receipt=state.get("receipt")
                if not receipt or receipt["status"] in FINAL or cid in self._updating:continue
                update=self._postcondition(state)
                if update:self._advance(cid,state,*update)
        finally:self._polling=False

    def _postcondition(self,state):
        k=self.kernel;m=state["metadata"];op=m.get("operation");ids=m.get("cycle_ids",[])
        if any(k._committed.get(cid)!=k._staged.get(cid) for cid in ids):return None
        orders={o["child_order_id"]:o for cid in ids if cid in k._committed for o in k._committed[cid]["orders"]}
        if op=="ARM_ENTRIES":
            return ("SUCCEEDED","NONE","New entry authority granted",[]) if k._armed else ("SUPERSEDED","NONE","A newer authority decision superseded this grant",[])
        if op=="MANUAL_OPEN":
            selected=[orders[oid] for oid in m.get("order_ids",[]) if oid in orders]
            if selected and all(o["terminal_confirmed"] and o["reported_fill_qty"]==o["filled_qty"] for o in selected):return "SUCCEEDED","NONE","Entry cycle reached a proved order terminal state",m["order_ids"]
            if any(o["send_state"]=="UNKNOWN" for o in selected):return "NEEDS_RECONCILIATION","E_UNKNOWN_SEND","Opening result is unknown; original identity retained",m.get("order_ids",[])
        elif op in {"CANCEL_SELECTED","CANCEL_OWNED","CANCEL_ACCOUNT_CONFIRMED","PAUSE_NEW_ENTRIES"}:
            selected=[orders[oid] for oid in m.get("order_ids",[]) if oid in orders]
            if selected and all(o["terminal_confirmed"] and o["reported_fill_qty"]==o["filled_qty"] for o in selected):return "SUCCEEDED","NONE","Related orders reached quantity-consistent terminal states",m.get("order_ids",[])
            if any(o["send_state"]=="UNKNOWN" or o["cancel_status"]=="UNKNOWN" or (o["cancel_status"]=="SEND_POSSIBLE" and o["kernel_boot_id"]!=k.boot_id) for o in selected):return "NEEDS_RECONCILIATION","E_UNKNOWN_SEND","Cancellation outcome is unknown; quantity remains reserved",m.get("order_ids",[])
        elif op in {"MANUAL_CLOSE","FLATTEN_THEN_STOP"}:
            goals=m.get("desired_remaining",{})
            if any(o["send_state"]=="UNKNOWN" or o["cancel_status"]=="UNKNOWN" for o in orders.values()):return "NEEDS_RECONCILIATION","E_UNKNOWN_SEND","Reduction outcome is unknown; original target and quantity remain reserved",list(goals)
            for cycle_id,target in goals.items():
                c=k._committed.get(cycle_id)
                if not c or c["protection"]["owned_qty"]>target:return None
                if any(OrderState.from_dict(o).potential_remaining or o["send_state"]=="UNKNOWN" for o in c["orders"]):return None
            if op=="FLATTEN_THEN_STOP":
                if not any(self._usable_evidence(e,{"scope_type":"ACCOUNT","scope_ids":[]}) for e in self.evidence.values()):return None
            return "SUCCEEDED","NONE","Requested reduction is confirmed; new entry authority remains separate",list(goals)
        elif op=="REQUEST_RECONCILIATION":
            evidence=self.evidence.get(m.get("run_id"))
            if evidence and self._usable_evidence(evidence,m["scope"]):return "SUCCEEDED","NONE","Four fact ranges completed at the current scope",[evidence["id"]]
        elif op=="ENTER_MAINTENANCE":
            if not self.maintenance or self.maintenance["command_id"]!=state["body"]["command_id"]:return None
            scope={"scope_type":"ACCOUNT","scope_ids":[]}
            available=[e for e in self.evidence.values() if self._usable_evidence(e,scope)]
            if not available:return None
            handover=m.get("handover_record_id")
            if handover:
                record=self.takeovers.get(handover)
                if not record or record["status"]!="TAKEN_OVER":return None
                return "SUCCEEDED","NONE","Explicitly handed-over maintenance decision recorded; not an empty-account safe window",[available[-1]["id"],handover]
            if any(c["protection"]["owned_qty"] or any(OrderState.from_dict(o).potential_remaining or o["send_state"]=="UNKNOWN" for o in c["orders"]) for c in k._committed.values()):return None
            if k._pending:return None
            if k.ledger.query("SELECT 1 FROM reservations WHERE status IN ('HELD','UNKNOWN') LIMIT 1"):return None
            if k.ledger.query("SELECT 1 FROM reconciliation_differences WHERE status='OPEN' LIMIT 1"):return None
            return "SUCCEEDED","NONE","Empty-account maintenance barrier established; kernel has not been declared stopped",[available[-1]["id"]]
        return None

    def _usable_evidence(self,item,scope):
        try:self._evidence(item["id"],scope);return True
        except TraderError:return False

    def _advance(self,cid,state,status,error_code,reason,evidence_ids):
        old=state["receipt"]
        if (old["status"],old["error_code"],old["reason"],old["evidence_ids"])==(status,error_code,reason,evidence_ids):return
        self._updating.add(cid);seq=self.kernel._sequence+1;txid=_new("tx-receipt")
        receipt=copy.deepcopy(old)|{"status":status,"error_code":error_code,"reason":reason,"evidence_ids":evidence_ids,
            "receipt_revision":str(int(old["receipt_revision"])+1),"event_seq":str(seq),"durable_seq":str(seq),"tx_id":txid}
        maintenance=state["metadata"].get("operation")=="ENTER_MAINTENANCE"
        maintenance_mode=("STOP_COMMITTED" if state["metadata"].get("handover_record_id") else "SAFE_TO_STOP") if status=="SUCCEEDED" else "QUIESCING"
        def mutate(conn,actual_seq):
            changed=conn.execute("UPDATE operator_commands SET status=?,receipt_revision=?,result_json=?,updated_event_seq=? WHERE command_id=? AND receipt_revision=?",
                (status,int(receipt["receipt_revision"]),canonical_bytes(receipt).decode(),actual_seq,cid,int(old["receipt_revision"]))).rowcount
            if changed!=1:raise TraderError("E_REVISION_CONFLICT","Receipt revision changed")
            if maintenance:
                conn.execute("UPDATE maintenance_locks SET status=?,reconciliation_id=?,barrier_event_seq=?,updated_event_seq=? WHERE singleton=1 AND command_id=?",(maintenance_mode,evidence_ids[0] if evidence_ids else None,actual_seq if status=="SUCCEEDED" else None,actual_seq,cid))
        def accepted():
            state["receipt"]=receipt;self._updating.discard(cid)
            if maintenance:
                self.maintenance.update(status=maintenance_mode,reconciliation_id=evidence_ids[0] if evidence_ids else None,barrier_event_seq=seq if status=="SUCCEEDED" else None)
                self.kernel._maintenance_state = self.maintenance["status"]
        self.kernel._submit("TX_AUTH","CONTROL_RECEIPT_PROGRESS",{"command_id":cid,"receipt":receipt},mutate,after=accepted,tx_id=txid)


_ERRORS=frozenset({"E_SCHEMA","E_AUTH","E_SCOPE","E_CONTEXT_EXPIRED","E_BOOT_MISMATCH","E_EPOCH_REVOKED","E_REVISION_CONFLICT",
    "E_DUPLICATE_CONFLICT","E_NOT_READY","E_RULE_UNAPPROVED","E_RISK","E_OWNERSHIP","E_FACT_BARRIER","E_STORAGE","E_UNKNOWN_SEND","E_UNSUPPORTED"})
