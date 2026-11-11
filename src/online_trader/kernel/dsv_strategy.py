"""Owner-side DSV decisions through the existing durable execution chain.

Explicit development kernels and independently scoped DSV simulation owners can
activate this runtime. No matcher, timer-generated quote or public fact injection
endpoint exists; the optional native adapter supplies its own original facts.
"""

from concurrent.futures import Future
import copy
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import time
from uuid import uuid4

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.gateway.ports import UnavailablePort
from online_trader.kernel.strategy import StrategyCoordinator
from online_trader.strategy.entry import DsvEntryStrategy
from online_trader.strategy.risk import evaluate_entry


def _ready(value):
    future=Future();future.set_result(copy.deepcopy(value));return future


class DsvStrategyRuntime:
    def __init__(self,kernel,*,profile=None):
        kernel._check_owner()
        if hasattr(kernel,"_dsv_runtime"):
            raise TraderError("E_OWNER","This owner already has a DSV strategy runtime")
        self.kernel=kernel
        self.online=bool(getattr(kernel.simulation,'strategy_enabled',False))
        self.profile,self.approval=load_confirmed_baseline()
        self._baseline_profile_hash=self.profile.content_hash
        if profile is not None:
            if not isinstance(profile,DsvProfile):
                raise TraderError("E_CONFIG","Expected an immutable development DSV profile")
            if (not kernel.development and not self.online) or isinstance(kernel.port,UnavailablePort):
                raise TraderError("E_RULE_UNAPPROVED","Development profile variants require an isolated development port")
            self.profile=profile
        if self.online and self.profile.content_hash!=kernel.simulation.grant.value['profile_hash']:
            raise TraderError('E_ONLINE_SCOPE','The selected DSV profile differs from the explicit strategy grant')
        self.strategy=DsvEntryStrategy(self.profile)
        self._states={};self._stop_states={};self._latest={};self._rebuilds={};self._pending=None;self.epoch=None;self._account_revision=0
        def restore_state(state):
            if state is None:return
            state=copy.deepcopy(state)
            if state.get("profile_hash")!=self.profile.content_hash:
                # A new development profile cannot erase an already consumed
                # bar or turn the former parameter path into complete evidence.
                state["profile_hash"]=self.profile.content_hash
                state["coverage_lost"]=True
            self._states[(state["contract_id"],state["trading_day"])]=state
        for row in kernel.ledger.query("SELECT event_seq,event_type,payload_json FROM event_journal ORDER BY event_seq"):
            material=strict_loads(row["payload_json"]);data=material.get("data",material)
            state=data.get("dsv_strategy_state")
            if data.get("rebuild_proof"):
                proof=data["rebuild_proof"]
                self._rebuilds[proof["evidence_id"]]=proof|{"profile_hash":proof.get("profile_hash",self._baseline_profile_hash),
                    "status":"OBSERVATION_REBUILT","armed":False,"durable_seq":str(row["event_seq"])}
            restore_state(state)
            stops=data.get("dsv_strategy_stops")
            if stops is not None:self._stop_states[(stops["state"]["contract_id"],stops["state"]["trading_day"])]=stops
            cycle=data.get("cycle")
            if row["event_type"]=="ENTRY_PREPARED" and cycle and cycle.get("strategy_metadata",{}):
                state=cycle["strategy_metadata"].get("strategy_state")
                restore_state(state)
                stops=cycle["strategy_metadata"].get("stop_result")
                if stops is not None:self._stop_states[(stops["state"]["contract_id"],stops["state"]["trading_day"])]=stops
        kernel._dsv_runtime=self

    def _development(self):
        self.kernel._writable()
        if (not self.kernel.development and not self.online) or isinstance(self.kernel.port,UnavailablePort):
            raise TraderError("E_RULE_UNAPPROVED","DSV execution requires an isolated development port")

    def activate(self):
        """Administrative activation of a DEVELOPMENT-only next-cycle config/epoch."""
        self._development();k=self.kernel;k.wait_idle()
        if not self.online and (k.recovery_required or k._has_unknown() or not k.control.can_increase()):
            raise TraderError("E_NOT_READY","Resolve existing management barriers before strategy activation")
        if self.epoch is not None and self.epoch==k.active_epoch:
            return self.status()
        reference=k.configurations.register_confirmed_baseline();k.wait_idle();reference.result()
        from online_trader.strategy import entry,risk,plan,stops,statistics
        from online_trader.protection import reducer
        from online_trader.contracts import validation
        from online_trader.execution import policy as execution_policy
        from online_trader.kernel import execution as execution_owner
        from online_trader.kernel import statistics as statistics_owner
        from online_trader.accounting import report as money_report,book as money_book
        from online_trader.kernel import money as money_owner
        from importlib.resources import files
        code_hashes={name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                     for name,module in (("entry",entry),("risk",risk),("plan",plan),("stops",stops),("statistics",statistics))}
        code_hashes["runtime"]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        code_hashes["protection"]=hashlib.sha256(Path(reducer.__file__).read_bytes()).hexdigest()
        code_hashes["validation"]=hashlib.sha256(Path(validation.__file__).read_bytes()).hexdigest()
        code_hashes['execution_policy']=hashlib.sha256(Path(execution_policy.__file__).read_bytes()).hexdigest()
        code_hashes['execution_owner']=hashlib.sha256(Path(execution_owner.__file__).read_bytes()).hexdigest()
        code_hashes['statistics_owner']=hashlib.sha256(Path(statistics_owner.__file__).read_bytes()).hexdigest()
        for name,module in (('money_report',money_report),('money_book',money_book),('money_owner',money_owner)):
            code_hashes[name]=hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for name in ("protection-plan.schema.json","protection-plan-v1.1.schema.json","protection-plan-v1.2.schema.json"):
            code_hashes[name]=hashlib.sha256(files("online_trader").joinpath("resources",name).read_bytes()).hexdigest()
        descriptor={"kind":"DSV_ONLINE_SIMULATION_EXECUTION" if self.online else "DSV_DEVELOPMENT_EXECUTION",
            "execution_scope":"ONLINE_SIMULATION" if self.online else "DEVELOPMENT",
            "profile_hash":self.profile.content_hash,"reference_approval_id":self.approval["record_id"],
            "source_program_hash":self.approval["source_program_hash"],"code_hashes":code_hashes,
            "baseline_profile_hash":self._baseline_profile_hash,
            "profile_source":"CONFIRMED_SAVED_VALUES" if self.profile.content_hash==self._baseline_profile_hash else "ISOLATED_DEVELOPMENT_VARIANT",
            "profile":self.profile.to_record(),
            "broker_authority":False}
        scheduler=getattr(k,'_scheduled_runtime',None)
        if scheduler is not None:descriptor['scheduled_development']=scheduler.descriptor()
        if self.online:descriptor['online_grant_hash']=k.simulation.grant.content_hash
        if self.online:
            descriptor['profile_source']='EXPLICIT_SCOPED_DSV_PROFILE'
            descriptor['code_hashes']['protection-plan-v1.3.schema.json']=hashlib.sha256(files('online_trader').joinpath('resources','protection-plan-v1.3.schema.json').read_bytes()).hexdigest()
        digest=content_hash(descriptor)
        if k._configuration["payload_hash"]!=digest:
            revision=k.ledger.query("SELECT COALESCE(MAX(config_revision),0)+1 AS n FROM config_revisions")[0]["n"]
            k._armed=False;k.contexts.revoke_all();k._state_revision+=1;k._risk_revision+=1;k._authority_revision+=1
            authority=k._authority()|{"config_revision":revision,"armed":False}
            def mutate(conn,seq):
                conn.execute("INSERT INTO config_revisions VALUES(?,?,?,?,'NEXT_CYCLE',?)",
                    (revision,self.approval["rule_set_id"],canonical_bytes(descriptor).decode(),digest,seq))
                k._write_authority(conn,seq,authority)
            def accepted():
                k._config_revision=revision
                k._configuration={"payload_hash":digest,"rule_set_id":self.approval["rule_set_id"]}
            k._submit("TX_CONFIG","DSV_DEVELOPMENT_CONFIG_ACTIVATED",{"authority":authority,"dsv_execution":descriptor},mutate,after=accepted)
            k.wait_idle()
        coordinator=StrategyCoordinator(k)
        _,checkpoint=coordinator.checkpoint()
        epoch=coordinator.register_shadow("dsv-builtin-"+uuid4().hex,checkpoint)
        barrier,checkpoint=coordinator.checkpoint()
        coordinator.switch(epoch,expected_old_epoch=k.active_epoch,barrier_seq=barrier,state_hash=checkpoint)
        k.wait_idle();self.epoch=epoch
        return self.status()

    def status(self):
        self.kernel._check_owner()
        return {"profile_hash":self.profile.content_hash,"config_revision":str(self.kernel._config_revision),
            "strategy_epoch":None if self.epoch is None else str(self.epoch),"execution_scope":"ONLINE_SIMULATION" if self.online else "DEVELOPMENT",
            "armed":self.kernel._armed,"source":"DSV_STATIC_COMPATIBILITY","broker_connected":bool(getattr(self.kernel.port,'broker_connected',False)),
            "baseline_profile_hash":self._baseline_profile_hash,
            "profile_source":"CONFIRMED_SAVED_VALUES" if self.profile.content_hash==self._baseline_profile_hash else "ISOLATED_DEVELOPMENT_VARIANT",
            "states":copy.deepcopy(list(self._states.values()))} | ({'profile_source':'EXPLICIT_SCOPED_DSV_PROFILE'} if self.online else {})

    def business_state(self):
        """Committed qualification state included in strategy handoff hashes."""
        self.kernel._check_owner()
        return {"profile_hash":self.profile.content_hash,
                "entry_states":[copy.deepcopy(value) for _,value in sorted(self._states.items())],
                "stop_states":[copy.deepcopy(value) for _,value in sorted(self._stop_states.items())]}

    def poll(self):
        self.kernel._check_owner()
        if self._pending is not None and self.kernel._failed:
            if not self._pending.done():self._pending.set_exception(TraderError("E_STORAGE","Strategy state could not be accepted"))
            self._pending=None

    def _stop_decision(self,event,account):
        if account.get("strategy_stop_facts") is None:return None
        from online_trader.strategy.stops import evaluate_stops
        stop_facts=copy.deepcopy(account["strategy_stop_facts"])
        if type(stop_facts) is not dict:raise TraderError("E_RISK_INPUT","Expected explicit strategy stop facts")
        old=self._stop_states.get((event["contract_id"],event["trading_day"]))
        if old is not None and old["state"]["day_stopped"]:
            stop_facts["day_stopped"]=True;stop_facts["day_stopped_reason"]=old["state"]["day_stopped_reason"]
        try:return evaluate_stops(self.profile,event["contract_id"],event["trading_day"],**stop_facts)
        except TypeError as error:raise TraderError("E_RISK_INPUT","Incomplete strategy stop fact structure") from error

    def update_facts(self,event,account,instrument):
        """Update trusted owner facts even while an earlier intent awaits its Ack."""
        self._development()
        if any(type(item) is not dict for item in (event,account,instrument)):
            raise TraderError("E_SCHEMA","Expected explicit market, account and instrument records")
        if account.get("account_key")!=self.kernel.account_key or event.get("account_key")!=self.kernel.account_key:
            raise TraderError("E_SCOPE","Strategy facts belong to another account")
        if (account.get("trading_day")!=event.get("trading_day") or instrument.get("trading_day")!=event.get("trading_day")
                or instrument.get("contract_id")!=event.get("contract_id")):
            raise TraderError("E_SCOPE","Strategy fact ranges differ")
        if account.get("local_reservations_excluded") is not True:
            raise TraderError("E_FACT_BARRIER","Account facts must distinguish the owner's local pending reservations")
        key=(event["contract_id"],event["trading_day"])
        state=self._states.get(key) or self.strategy.initial_state(self.kernel.account_key,*key)
        self.strategy.evaluate(state,event) # strict market/state validation without mutation
        row=next((row for row in self.profile.to_record()["settings"]["symbols"] if row["code"]==event["contract_id"]),None)
        evaluate_entry(self.profile,direction="LONG",contract_id=event["contract_id"],
            stop_distance="1" if row is None else row["stop_offset"],limit_price=event["last"],
            requested_qty=0 if row is None else row["order_volume"],account=account,instrument=instrument)
        if instrument.get("tick_size") is not None and instrument["tick_size"]!=event["tick_size"]:
            raise TraderError("E_SCOPE","Market and instrument tick sizes differ")
        if account["revision"]<self._account_revision:
            raise TraderError("E_REVISION_CONFLICT","Account facts are older than the current owner view")
        covered=account.get("covered_local_cycle_ids",[])
        if (type(covered) is not list or any(type(cid) is not str or cid not in self.kernel._staged for cid in covered)
                or len(covered)!=len(set(covered))):
            raise TraderError("E_FACT_BARRIER","Local position coverage must identify known owner cycles")
        covered_attempts=account.get("covered_local_attempt_ids",[])
        known_attempts={o["attempt_id"] for c in self.kernel._staged.values() for o in c["orders"]}
        if (type(covered_attempts) is not list or any(type(a) is not str or a not in known_attempts for a in covered_attempts)
                or len(covered_attempts)!=len(set(covered_attempts)) or len(covered_attempts)>account["orders_today"]):
            raise TraderError("E_FACT_BARRIER","Reported attempt coverage must identify known owner attempts")
        previous=self._latest.get(key);arrival=time.monotonic_ns()
        if previous is not None:
            old=previous["event"]
            if event["source_epoch"]<old["source_epoch"] or (event["source_epoch"]==old["source_epoch"] and event["sequence"]<old["sequence"]):
                raise TraderError("E_REVISION_CONFLICT","Older market facts cannot replace the final-send view")
            if event["source_epoch"]==old["source_epoch"] and event["sequence"]==old["sequence"] and content_hash(event)!=content_hash(old):
                raise TraderError("E_DUPLICATE_CONFLICT","Market identity changed content")
            if event["source_epoch"]==old["source_epoch"] and event["sequence"]==old["sequence"]:
                arrival=previous["received_ns"]
            if account["revision"]<previous["account"]["revision"]:
                raise TraderError("E_REVISION_CONFLICT","Account fact revision moved backwards")
            if account["revision"]==previous["account"]["revision"] and content_hash(account)!=content_hash(previous["account"]):
                raise TraderError("E_DUPLICATE_CONFLICT","Account fact identity changed content")
        canonical_bytes(account);canonical_bytes(instrument)
        stops=account.get('strategy_stop_facts')
        if type(stops) is dict:self.kernel.statistics.observe_calendar(event['contract_id'],stops.get('trading_days'))
        self._account_revision=account["revision"]
        self._latest[key]={"event":copy.deepcopy(event),"account":copy.deepcopy(account),
                           "instrument":copy.deepcopy(instrument),"received_ns":arrival,"stop_result":self._stop_decision(event,account)}

    def _risk(self,candidate,facts,*,exclude_decision=None,exclude_attempt=None):
        account=copy.deepcopy(facts["account"])
        instrument=facts["instrument"]
        pending=Decimal(0);symbol_extra=Decimal(0);attempts=0;active=set(account["active_symbols"])
        covered=set(account.get("covered_local_cycle_ids",[]));covered_attempts=set(account.get("covered_local_attempt_ids",[]))
        for cycle in self.kernel._staged.values():
            if cycle["decision_key"]==exclude_decision:continue
            same_day=cycle["plan"]["trading_day"]==account["trading_day"]
            if cycle["protection"]["owned_qty"]:active.add(cycle["plan"]["contract_id"])
            local_remaining=0
            for order in cycle["orders"]:
                if order['attempt_id']==exclude_attempt:continue
                if (same_day and cycle["plan"]["contract_id"]==candidate["contract_id"] and order["send_state"]!="DEFINITELY_NOT_SENT"
                        and order["attempt_id"] not in covered_attempts):attempts+=1
                if order["send_state"]=="UNKNOWN":account["unknown_orders"]=True
                if order["effect"]=="OPEN":
                    local_remaining+=OrderState.from_dict(order).potential_remaining
            uncovered=0 if cycle['position_cycle_id'] in covered else cycle['protection']['owned_qty']
            if local_remaining or uncovered:
                amount=cycle.get('strategy_metadata') or {}
                active.add(cycle['plan']['contract_id'])
                if amount.get('margin_per_lot') is None:account['unknown_orders']=True
                else:
                    per_lot=Decimal(amount['margin_per_lot']);pending+=per_lot*(local_remaining+uncovered)
                    if cycle['plan']['contract_id']==candidate['contract_id']:symbol_extra+=per_lot*uncovered
        account["pending_margin"]=decimal_text(Decimal(account["pending_margin"])+pending) if account["pending_margin"] is not None else None
        account["symbol_margin"]=decimal_text(Decimal(account["symbol_margin"])+symbol_extra) if account["symbol_margin"] is not None else None
        account["orders_today"]+=attempts;account["active_symbols"]=sorted(active)
        return evaluate_entry(self.profile,direction=candidate["direction"],contract_id=candidate["contract_id"],
            stop_distance=candidate["stop_distance"],limit_price=candidate["limit_price"],
            requested_qty=next(row["order_volume"] for row in self.profile.to_record()["settings"]["symbols"] if row["code"]==candidate["contract_id"]),
            account=account,instrument=instrument)

    def _blocked(self,event,*,exclude_decision=None):
        k=self.kernel;reasons=[]
        reasons+=k.statistics.entry_reasons(event['contract_id'],event['trading_day'],profile=self.profile,
            now_utc=event['source_time_utc'])
        scheduler=getattr(k,'_scheduled_runtime',None)
        if scheduler is not None and not scheduler.scope_current(event['contract_id'],event['trading_day'],event['session_id']):
            reasons.append('SCHEDULE_CURRENT_SESSION_UNPROVEN')
        if not k._armed:reasons.append("ENTRY_AUTHORITY_DISARMED")
        if k.recovery_required:reasons.append("RECONCILIATION_REQUIRED")
        if self.epoch!=k.active_epoch or self.epoch is None:reasons.append("STRATEGY_EPOCH_REVOKED")
        if not k.control.can_increase(event["contract_id"]):reasons.append("CONTROL_BARRIER")
        for (contract,day),stops in self._stop_states.items():
            if contract==event["contract_id"] and ((day==event["trading_day"] and not stops["allow_new_entries"])
                    or stops["state"].get("month_stop")==event["trading_day"][:7]):
                reasons.append("DSV_PERSISTED_DAY_OR_MONTH_STOP")
        for cycle in k._staged.values():
            if cycle["decision_key"]==exclude_decision or cycle["plan"]["contract_id"]!=event["contract_id"]:continue
            if cycle["protection"].get("block_new_for_trading_day") and cycle["plan"]["trading_day"]==event["trading_day"]:
                reasons.append("PROTECTION_DAY_STOP")
            if cycle["protection"]["owned_qty"] or any(OrderState.from_dict(o).potential_remaining for o in cycle["orders"]):
                reasons.append("OWNED_EXPOSURE_EXISTS")
        return list(dict.fromkeys(reasons))

    def _final_gate(self,original,pre_state,quantity,frozen_instrument):
        key=(original["contract_id"],original["trading_day"])
        facts=self._latest.get(key)
        if (facts is None or facts["account"]["revision"]<self._account_revision
                or self.kernel._quote_age_ns(facts["received_ns"])>self.kernel.max_quote_age_ms*1_000_000):return False
        event=facts["event"]
        if any(facts["instrument"].get(key)!=frozen_instrument.get(key) for key in ("tick_size","multiplier")):return False
        stops=facts.get("stop_result")
        if stops is not None and (not stops["allow_new_entries"] or
                (original.get("entry_kind")=="CONTINUATION" and not stops["allow_continuation"])):return False
        if self._blocked(event,exclude_decision=original["decision_key"]):return False
        if self.kernel.statistics.entry_reasons(event['contract_id'],event['trading_day'],profile=self.profile,
                direction=original['direction'],entry_kind=original.get('entry_kind','NORMAL'),now_utc=event['source_time_utc']):return False
        current=self.strategy.evaluate(pre_state,event)
        candidates=[candidate for candidate in current["candidates"] if all(candidate[k]==original[k] for k in
            ("direction","theoretical_price","limit_price","decision_key","profile_hash"))]
        if not candidates:return False
        result=self._risk(candidates[0],facts,exclude_decision=original["decision_key"])
        return result["allowed"] and result["quantity"]>=quantity

    def prepare_replacement(self,cycle,order):
        """Issue a fresh development entry context for an original target remainder."""
        k=self.kernel;candidate=copy.deepcopy(cycle['strategy_metadata']['candidate'])
        def ready():
            facts=self._latest.get((cycle['plan']['contract_id'],cycle['plan']['trading_day']))
            if (facts is None or facts['account']['revision']<self._account_revision or
                    k._quote_age_ns(facts['received_ns'])>k.max_quote_age_ms*1_000_000):return False
            if (cycle['strategy_epoch']!=self.epoch or cycle['plan']['config_revision']!=str(k._config_revision)
                    or cycle['plan']['config_hash']!=k._configuration['payload_hash']):return False
            event=facts['event'];frozen=cycle.get('instrument',{})
            moment=datetime.fromisoformat(event['source_time_utc'])
            if not datetime.fromisoformat(event['session_open_utc'])<=moment<datetime.fromisoformat(event['session_close_utc']):return False
            if (event['session_id']!=candidate['session_id'] or event['quality']!='CONTIGUOUS' or
                    not event['path_complete'] or not event['calendar_complete'] or not event['is_trading']):return False
            if any(facts['instrument'].get(key)!=frozen.get(key) for key in ('tick_size','multiplier')):return False
            if self._blocked(event,exclude_decision=cycle['decision_key']):return False
            if k.statistics.entry_reasons(event['contract_id'],event['trading_day'],profile=self.profile,
                    direction=candidate['direction'],entry_kind=candidate.get('entry_kind','NORMAL'),now_utc=event['source_time_utc']):return False
            stop=facts.get('stop_result')
            if stop is not None and (not stop['allow_new_entries'] or
                    (candidate.get('entry_kind')=='CONTINUATION' and not stop['allow_continuation'])):return False
            candidate['limit_price']=order.limit_price
            risk=self._risk(candidate,facts,exclude_attempt=order.attempt_id)
            return risk['allowed'] and risk['quantity']>=order.original_qty
        if not ready():return None
        peer='dsv-owner-'+str(self.epoch)
        context=k.contexts.issue(peer,'ENTRY_PROPOSAL','DSV_ENTRY',cycle['plan']['contract_id'],
            kernel_epoch=str(k.kernel_epoch),strategy_epoch=str(self.epoch),revisions=k.revisions,issued_event_seq=str(k._durable_seq))
        return {'authorization':{'context_id':context['context_id'],'peer_id':peer,'authority_revision':k._authority_revision,
                                'purpose':'ENTRY_PROPOSAL','operation':'DSV_ENTRY'},'gate':ready}

    def rebuild_observation(self,events,*,evidence_id,extend=False):
        """Commit a complete path or its contiguous suffix without executing signals."""
        from online_trader.domain.values import identifier
        identifier(evidence_id);self._development();k=self.kernel
        if self.epoch is None or self.epoch!=k.active_epoch:raise TraderError("E_EPOCH_REVOKED","Observation rebuild requires the active owner")
        if self._pending is not None or k._pending:raise TraderError("E_FACT_BARRIER","Observation rebuild requires an accepted boundary")
        if type(events) is not list or not 1<=len(events)<=10000:raise TraderError("E_SCHEMA","Expected a bounded explicit source path")
        if len(canonical_bytes(events))>16*1024*1024:raise TraderError("E_QUEUE_OVERFLOW","Observation replay exceeds its bounded capacity")
        events=copy.deepcopy(events);path_hash=content_hash(events)
        if evidence_id in self._rebuilds:
            if (self._rebuilds[evidence_id]["path_hash"]!=path_hash
                    or self._rebuilds[evidence_id]["profile_hash"]!=self.profile.content_hash
                    or ("continued_from_sequence" in self._rebuilds[evidence_id])!=extend):
                raise TraderError("E_DUPLICATE_CONFLICT","Observation evidence identity changed content or profile")
            return _ready(self._rebuilds[evidence_id])
        first=events[0];key=(first.get("contract_id"),first.get("trading_day")) if type(first) is dict else (None,None)
        old=self._states.get(key)
        observation="dsv-observation-"+content_hash({"account":k.account_key,"contract":key[0],"day":key[1]})
        if extend:
            if (old is None or old["coverage_lost"] or old["source_epoch"]!=first.get("source_epoch")
                    or old["session_id"]!=first.get("session_id") or old["last_sequence"]+1!=first.get("sequence")):
                raise TraderError("E_FACT_BARRIER","Observation suffix does not continue its accepted path")
            rows=k.ledger.query("SELECT observed_from_utc FROM observation_windows WHERE observation_id=?",(observation,))
            if not rows:raise TraderError("E_FACT_BARRIER","Observation suffix has no durable starting window")
            observed_from=rows[0]["observed_from_utc"]
            state=copy.deepcopy(old)
        else:
            state=self.strategy.initial_state(k.account_key,*key)
        for event in events:
            if (type(event) is not dict or event.get("account_key")!=k.account_key
                    or (event.get("contract_id"),event.get("trading_day"))!=key
                    or event.get("source_epoch")!=first.get("source_epoch") or event.get("session_id")!=first.get("session_id")
                    or event.get("quality")!="CONTIGUOUS" or not event.get("path_complete") or not event.get("day_range_complete")
                    or not event.get("calendar_complete")):
                raise TraderError("E_FACT_BARRIER","Replay does not prove a complete single source path")
            evaluated=self.strategy.evaluate(state,event);new=evaluated["state"]
            if new["coverage_lost"] or new["last_sequence"]!=event["sequence"] or state["last_sequence"]==new["last_sequence"]:
                raise TraderError("E_FACT_BARRIER","Replay is incomplete, repeated or out of order")
            state=new
        if not extend:observed_from=first["source_time_utc"]
        if old is not None:
            if (state["source_epoch"]<old["source_epoch"] or
                    datetime.fromisoformat(state["last_source_time_utc"])<datetime.fromisoformat(old["last_source_time_utc"])):
                raise TraderError("E_REVISION_CONFLICT","Replay cannot move accepted source facts backwards")
            if state["source_epoch"]==old["source_epoch"] and state["last_sequence"]<old["last_sequence"]:
                raise TraderError("E_REVISION_CONFLICT","Replay ends before the accepted source boundary")
            state["first_minute_blocks"]=sorted(set(old["first_minute_blocks"])|set(state["first_minute_blocks"]))
            if events[-1]["candle_id"]==old["last_signal_bar"]:state["last_signal_bar"]=old["last_signal_bar"]
        result=Future();self._pending=result
        proof={"evidence_id":evidence_id,"event_count":len(events),"path_hash":path_hash,"profile_hash":self.profile.content_hash,
               "contract_id":key[0],"trading_day":key[1],"source_epoch":str(state["source_epoch"]),
               "last_source_sequence":str(state["last_sequence"]),"signals_executed":0}
        if extend:proof["continued_from_sequence"]=str(old["last_sequence"])
        k._armed=False;k.contexts.revoke_all();k._state_revision+=1;k._risk_revision+=1;k._authority_revision+=1
        authority=k._authority()
        def mutate(conn,seq):
            k._write_authority(conn,seq,authority)
            conn.execute("INSERT INTO observation_windows VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET session_id=excluded.session_id,source_epoch=excluded.source_epoch,observation_epoch=observation_epoch+1,quality='CONTIGUOUS',observed_from_utc=excluded.observed_from_utc,observed_to_utc=excluded.observed_to_utc,path_state_json=excluded.path_state_json,updated_event_seq=excluded.updated_event_seq",
                (observation,key[0],first["session_id"],first["source_epoch"],1,"CONTIGUOUS",observed_from,events[-1]["source_time_utc"],canonical_bytes(state).decode(),seq))
        def accepted():
            self._states[key]=state;self._latest.pop(key,None);self._pending=None
            receipt=proof|{"status":"OBSERVATION_REBUILT","armed":False,"durable_seq":str(k._durable_seq)}
            self._rebuilds[evidence_id]=receipt;result.set_result(copy.deepcopy(receipt))
        try:k._submit("TX_OBSERVATION","DSV_OBSERVATION_REBUILT",{"dsv_strategy_state":state,"rebuild_proof":proof,"authority":authority},mutate,after=accepted)
        except BaseException:self._pending=None;raise
        return result

    def on_market(self,event,account,instrument,*,_allow_entry=True):
        self._development();k=self.kernel
        self.update_facts(event,account,instrument)
        if self.epoch is None or self.epoch!=k.active_epoch:
            raise TraderError("E_EPOCH_REVOKED","Activate the strategy for this boot before observing entry facts")
        if self._pending is not None or k._pending:
            raise TraderError("E_FACT_BARRIER","Accept the preceding owner transaction before processing another market fact")
        key=(event["contract_id"],event["trading_day"])
        previous=copy.deepcopy(self._states.get(key) or self.strategy.initial_state(k.account_key,*key))
        decision=self.strategy.evaluate(previous,event)
        # Existing owned positions get the quote before any new entry decision.
        # This stages their protection on the same owner/writer without waiting
        # in a broker loop or delegating protection to the strategy worker. A
        # repeated/regressive source identity cannot become a new protection
        # event, consume the next half stage or refresh an old quote's lifetime.
        stale={"DUPLICATE_MARKET_EVENT","OUT_OF_ORDER_MARKET_EVENT","OUT_OF_ORDER_SOURCE_TIME"}
        if not stale.intersection(decision["reasons"]):
            ordered=sorted(k._staged.values(),key=lambda c:bool(c['protection']['owned_qty'] or
                any(OrderState.from_dict(o).potential_remaining for o in c['orders'])),reverse=True)
            for cycle in ordered:
                if cycle['plan']['contract_id']!=event['contract_id']:continue
                working=cycle['protection']['owned_qty'] or any(OrderState.from_dict(o).potential_remaining for o in cycle['orders'])
                ex=cycle.get('execution') or {}
                retryable=not ex.get('entry_stopped') and cycle['protection']['entry_filled_qty']<cycle['plan']['initial_open_quantity'] and any(
                    o['effect']=='OPEN' and (o['send_state']=='DEFINITELY_NOT_SENT' or
                    ex.get('cancel_reasons',{}).get(o['child_order_id']) in {'NEW_QUOTE_PRICE_WORSENED','TARGET_QUANTITY_REDUCED'}) for o in cycle['orders'])
                working=working or (cycle['plan']['schema_version'] in ('1.2','1.3') and retryable)
                if not working:continue
                cross_day=self.online and k.simulation.exit_day_ready(cycle,event['trading_day'])
                if cycle['plan']['schema_version'] in ('1.2','1.3') and (cycle['plan']['trading_day']==event['trading_day'] or cross_day):
                    initial=(cycle.get('strategy_metadata') or {}).get('candidate',{}).get('source_time_utc',event['source_time_utc'])
                    current=datetime.fromisoformat(event['source_time_utc']);start=datetime.fromisoformat(event['session_open_utc'])
                    since=int((current-start).total_seconds()*1000)
                    local=start.astimezone(timezone(timedelta(hours=8)))
                    opening=event.get('is_opening_session',(local.hour,local.minute) in {(9,0),(9,30),(21,0)})
                    execution_event={'event_id':'execution-'+content_hash({'cycle':cycle['position_cycle_id'],'market':event}),
                        'source_epoch':event['source_epoch'],'sequence':event['sequence'],
                        'elapsed_ms':max(0,int((current-datetime.fromisoformat(initial)).total_seconds()*1000)),
                        'trading_day':event['trading_day'],'observed_at_utc':event['source_time_utc'],
                        'close_due':event['is_trading'] and event.get('session_policy',{}).get('force_close_at_end',True) and (datetime.fromisoformat(event['session_close_utc'])-current).total_seconds()<=cycle['plan']['close_session']['advance_seconds'],
                        'market':{'complete':event['calendar_complete'],
                            'fresh':True,'is_trading':event['is_trading'],'quality':event['quality'],
                            'last':event['last'],'bid1':event['bid1'],'ask1':event['ask1'],'tick_size':event['tick_size'],
                            'upper_limit':instrument.get('upper_limit'),'lower_limit':instrument.get('lower_limit'),
                            'since_open_ms':since,'near_open':opening and 0<=since<=cycle['plan']['execution_policy']['chase_window_ms']}}
                    scheduler=getattr(k,'_scheduled_runtime',None)
                    if scheduler is not None:
                        execution_event=scheduler.execution_event(cycle,event,instrument)
                        k.execution.tick(cycle['position_cycle_id'],execution_event,_market_received_ns=scheduler.received_for(event['contract_id']))
                    else:k.execution_tick(cycle['position_cycle_id'],execution_event)
                    break
                elif cycle['protection']['owned_qty']:
                    k.observe(cycle['position_cycle_id'],event['last'],bid1=event['bid1'],ask1=event['ask1'],valid=event['quality']=='CONTIGUOUS',observed_at_utc=event['source_time_utc'],
                              _adapter_token=k.port.callback_token if self.online else None)
        stop_result=self._latest[key].get("stop_result")
        money_account=k.simulation.money_fact(k.port.snapshot()) if self.online else k.money.account_from_risk(event,account)
        state=decision["state"];report={"status":"OBSERVED","candidates":decision["candidates"],
            "reasons":list(decision["reasons"]),"source_notes":decision.get("source_notes",[]),
            "profile_hash":self.profile.content_hash,"child_order_id":None}
        if stop_result is not None:report["stops"]=copy.deepcopy(stop_result)
        if state==previous and not decision["candidates"] and stop_result==self._stop_states.get(key):return _ready(report)
        result=Future();self._pending=result
        proposal_id="dsv-proposal-"+uuid4().hex;persisted={"report":None}
        context=k.contexts.issue("dsv-owner-"+str(self.epoch),"ENTRY_PROPOSAL","DSV_ENTRY",event["contract_id"],
            kernel_epoch=str(k.kernel_epoch),strategy_epoch=str(self.epoch),revisions=k.revisions,issued_event_seq=str(k._durable_seq))
        def mutate(conn,seq):
            frozen_report=persisted["report"]
            intent=None;candidate=decision["candidates"][0] if decision["candidates"] else None
            if frozen_report["status"]=="ENTRY_COMMITTED":
                intent=conn.execute("SELECT intent_id FROM order_intents WHERE decision_key=?",(candidate["decision_key"],)).fetchone()[0]
            conn.execute("INSERT INTO strategy_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id,"DECISION" if intent else "STATE_UPDATE",candidate["decision_key"] if intent else None,self.epoch,
                 context["context_id"],content_hash({"state":state,"report":frozen_report}),
                 canonical_bytes({"state":state,"report":frozen_report}).decode(),"COMMITTED",intent,canonical_bytes(frozen_report).decode(),seq))
            observation="dsv-observation-"+content_hash({"account":k.account_key,"contract":event["contract_id"],"day":event["trading_day"]})
            conn.execute("INSERT INTO observation_windows VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET observation_epoch=CASE WHEN session_id!=excluded.session_id OR source_epoch!=excluded.source_epoch THEN observation_epoch+1 ELSE observation_epoch END,observed_from_utc=CASE WHEN session_id!=excluded.session_id OR source_epoch!=excluded.source_epoch THEN excluded.observed_from_utc ELSE observed_from_utc END,session_id=excluded.session_id,source_epoch=excluded.source_epoch,quality=excluded.quality,observed_to_utc=excluded.observed_to_utc,path_state_json=excluded.path_state_json,updated_event_seq=excluded.updated_event_seq",
                (observation,event["contract_id"],event["session_id"],event["source_epoch"],1,
                 "DATA_GAP" if state["coverage_lost"] else "CONTIGUOUS",event["source_time_utc"],event["source_time_utc"],
                 canonical_bytes(state).decode(),seq))
        def accepted():
            self._states[key]=copy.deepcopy(state);self._pending=None
            if stop_result is not None:self._stop_states[key]=copy.deepcopy(stop_result)
            if not result.done():result.set_result(copy.deepcopy(report)|{"durable_seq":str(k._durable_seq)})
        try:
            reasons=self._blocked(event)
            if not _allow_entry:reasons.append('OBSERVATION_ONLY_BACKLOG')
            candidate=decision["candidates"][0] if decision["candidates"] else None
            if candidate is not None:
                reasons+=k.statistics.entry_reasons(event['contract_id'],event['trading_day'],profile=self.profile,
                    direction=candidate['direction'],entry_kind=candidate.get('entry_kind','NORMAL'),now_utc=event['source_time_utc'])
            if stop_result is not None:
                if not stop_result["allow_new_entries"]:reasons+=stop_result["reasons"]
                elif candidate is not None and candidate.get("entry_kind")=="CONTINUATION" and not stop_result["allow_continuation"]:
                    reasons+=stop_result["reasons"]
            if candidate is not None and candidate["decision_key"] in k._decision_keys:reasons.append("OPPORTUNITY_ALREADY_COMMITTED")
            if candidate is not None and not reasons:
                risk=self._risk(candidate,self._latest[key]);report["risk"]=risk;reasons+=risk["reasons"]
                if risk["allowed"]:
                    from online_trader.strategy.plan import build_development_plan
                    compiler=build_development_plan;online_values={}
                    if self.online:
                        from online_trader.strategy.plan import build_simulation_plan
                        compiler=build_simulation_plan;online_values={'grant':k.simulation.grant}
                    plan=compiler(self.profile,candidate,account_key=k.account_key,trading_day=event["trading_day"],
                        config_revision=str(k._config_revision),config_hash=k._configuration["payload_hash"],
                        rule_set_id=k._configuration["rule_set_id"],quantity=risk["quantity"],execution_version='1.2',
                        plan_id="dsv-plan-"+uuid4().hex,position_cycle_id="dsv-cycle-"+uuid4().hex,**online_values)
                    report["status"]="ENTRY_COMMITTED"
                    persisted["report"]=copy.deepcopy(report)
                    hook={"epoch":self.epoch,"money_account":money_account,"metadata":{"profile_hash":self.profile.content_hash,"candidate":candidate,
                        "strategy_state":state,"stop_result":stop_result,"margin_per_lot":risk["metrics"]["margin_per_lot"],"instrument":copy.deepcopy(instrument)},
                        "gate":lambda frozen=copy.deepcopy(instrument):self._final_gate(candidate,previous,risk["quantity"],frozen),"mutate":mutate,"accepted":accepted}
                    if self.online:hook['initial_market']={'event':event,'received_ns':self._latest[key]['received_ns']}
                    child=k.admit_entry(plan,candidate["decision_key"],candidate["limit_price"],context_id=context["context_id"],
                        peer_id="dsv-owner-"+str(self.epoch),_strategy=hook)
                    report["child_order_id"]=child
                    return result
            report["reasons"]=list(dict.fromkeys(report["reasons"]+reasons))
        except TraderError as error:
            report["status"]="OBSERVED";report["reasons"].append(error.code)
        persisted["report"]=copy.deepcopy(report)
        try:
            k._submit("TX_OBSERVATION","DSV_STRATEGY_OBSERVED",{"dsv_strategy_state":state,"dsv_strategy_stops":stop_result,"report":report,'money_account':money_account},mutate,after=accepted)
        except BaseException:
            self._pending=None;raise
        return result
