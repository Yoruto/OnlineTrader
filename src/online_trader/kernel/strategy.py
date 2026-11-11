"""Development-only strategy handoff skeleton.

This coordinates durable epochs and business checkpoints, not executable loading,
signature verification, cross-process catch-up or production authorization.  All
methods run on the kernel owner.  Shadow registration is an administrative,
synchronous operation and must not be called from the market/SDK hot path.
"""

from __future__ import annotations

import copy
from concurrent.futures import Future
import re
from uuid import uuid4

from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import identifier
from online_trader.errors import TraderError


class StrategyCoordinator:
    def __init__(self, kernel) -> None:
        kernel._check_owner()
        self.kernel = kernel
        if not hasattr(kernel, "_strategy_switch_pending"):
            kernel._strategy_switch_pending = False

    def _check(self, *, idle: bool = False) -> None:
        self.kernel._writable()
        if not self.kernel.development and not getattr(self.kernel.simulation,'strategy_enabled',False):
            raise TraderError("E_RULE_UNAPPROVED", "Strategy handoff requires an isolated development session")
        if idle and (self.kernel._pending or self.kernel._strategy_switch_pending):
            raise TraderError("E_FACT_BARRIER", "Strategy handoff requires an accepted durable boundary")

    @staticmethod
    def _epoch(value: int) -> int:
        if type(value) is not int or not 1 <= value <= 9_223_372_036_854_775_807:
            raise TraderError("E_SCHEMA", "Invalid strategy epoch")
        return value

    @staticmethod
    def _hash(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise TraderError("E_SCHEMA", "Invalid strategy state hash")
        return value

    def _business_state(self) -> dict:
        # Administrative journal positions, boot-local context deadlines and pipe
        # identity are deliberately excluded.  Actual orders, protection and
        # consumed opportunities remain part of the checkpoint.
        cycle_keys = (
            "position_cycle_id", "plan", "protection", "orders", "decision_key",
            "risk_per_lot", "lifecycle", "revision", "entry_reconciliation_id",
            "actual_average_entry_price", "reasons", "cancel_entries",
            "entry_origin","strategy_epoch","strategy_metadata","instrument","execution","schedule_binding","scheduler_state",
            "nominal_exit","statistics_profile","statistics_entry_id","statistics_close_id",
            "statistics_review_required","statistics_error",
            "financial_instrument","money_quote",
        )
        cycles = []
        for cycle_id in sorted(self.kernel._committed):
            source = self.kernel._committed[cycle_id]
            cycle = {key: copy.deepcopy(source[key]) for key in cycle_keys if key in source}
            cycle["orders"] = sorted(cycle.get("orders", []), key=lambda order: order["child_order_id"])
            quote = source.get("last_quote")
            cycle["last_quote"] = None if quote is None else {key: quote.get(key) for key in ("price", "bid1", "ask1")}
            cycles.append(cycle)
        material = {
            "schema_version": "1",
            "account_key": self.kernel.account_key,
            "config_hash": self.kernel._configuration["payload_hash"],
            "rule_set_id": self.kernel._configuration["rule_set_id"],
            "decision_keys": sorted(self.kernel._decision_keys),
            "cycles": cycles,
            "nominal_statistics": self.kernel.statistics.business_state(),
            "money_facts":self.kernel.money.business_state(),
        }
        if hasattr(self.kernel,"_dsv_runtime"):
            material["dsv_strategy_state"]=self.kernel._dsv_runtime.business_state()
        if hasattr(self.kernel,'_scheduled_runtime'):
            material['scheduled_runtime_state']=self.kernel._scheduled_runtime.business_state()
        return material

    def checkpoint(self) -> tuple[int, str]:
        self._check(idle=True)
        return self.kernel._durable_seq, content_hash(self._business_state())

    def register_shadow(self, instance_id: str, checkpoint_hash: str) -> int:
        self._check(idle=True)
        instance_id = identifier(instance_id)
        checkpoint_hash = self._hash(checkpoint_hash)
        barrier, actual = self.checkpoint()
        if checkpoint_hash != actual:
            raise TraderError("E_STATE_HASH", "Shadow checkpoint does not match current business facts")
        if self.kernel.ledger.query("SELECT 1 FROM strategy_instances WHERE instance_id=?", (instance_id,)):
            raise TraderError("E_DUPLICATE_CONFLICT", "Strategy instance identity is already registered")
        epoch = self._epoch(self.kernel.ledger.query("SELECT COALESCE(MAX(strategy_epoch),0)+1 AS next_epoch FROM strategy_instances")[0]["next_epoch"])
        material = self._business_state()
        checkpoint_id = "shadow-checkpoint-" + uuid4().hex
        rule_set_id = self.kernel._configuration["rule_set_id"]
        online=bool(getattr(self.kernel.simulation,'strategy_enabled',False))
        metadata = {"development_only": not online, "instance_id": instance_id,
                    "strategy_epoch": str(epoch), "checkpoint_hash": actual,
                    "checkpoint_event_seq": str(barrier), "signature_verified": False}
        if online:metadata.update(execution_scope='ONLINE_SIMULATION',builtin_dsv=True)
        executable_hash='0'*64
        if online:
            configuration=self.kernel.ledger.query('SELECT payload_json FROM config_revisions WHERE config_revision=?',(self.kernel._config_revision,))[0]
            executable_hash=strict_loads(configuration['payload_json'])['code_hashes']['runtime']

        def mutate(conn, seq):
            conn.execute(
                "INSERT INTO strategy_instances(strategy_epoch,instance_id,executable_hash,status,rule_set_id,config_revision,barrier_event_seq,state_revision,updated_event_seq) VALUES (?,?,?,'SHADOW',?,?,?,1,?)",
                (epoch, instance_id, executable_hash, rule_set_id,self.kernel._config_revision, barrier, seq),
            )
            conn.execute(
                "INSERT INTO strategy_checkpoints(checkpoint_id,strategy_epoch,event_seq,state_schema_version,state_json,state_hash,observation_coverage_json) VALUES (?,?,?,1,?,?,?)",
                (checkpoint_id, epoch, seq, canonical_bytes(material).decode(), actual,
                 canonical_bytes({"development_only": not online, "checkpoint_event_seq": str(barrier)}).decode()),
            )

        self.kernel._submit("TX_SWITCH", "BUILTIN_DSV_SHADOW_REGISTERED" if online else "DEVELOPMENT_SHADOW_REGISTERED", metadata, mutate)
        self.kernel.wait_idle()
        return epoch

    def switch(self, epoch: int, expected_old_epoch: int | None,
               barrier_seq: int, state_hash: str) -> Future:
        self._check(idle=True)
        epoch = self._epoch(epoch)
        if expected_old_epoch is not None:
            self._epoch(expected_old_epoch)
        if type(barrier_seq) is not int or barrier_seq < 1:
            raise TraderError("E_SCHEMA", "Invalid strategy barrier")
        state_hash = self._hash(state_hash)
        current_barrier, current_hash = self.checkpoint()
        if barrier_seq != current_barrier:
            raise TraderError("E_FACT_BARRIER", "Strategy barrier is not the current accepted durable boundary")
        if state_hash != current_hash:
            raise TraderError("E_STATE_HASH", "Strategy handoff state changed")
        if self.kernel.active_epoch != expected_old_epoch:
            raise TraderError("E_EPOCH_REVOKED", "The expected strategy no longer owns this instance")
        rows = self.kernel.ledger.query("SELECT * FROM strategy_instances WHERE strategy_epoch=?", (epoch,))
        if not rows or rows[0]["status"] != "SHADOW":
            raise TraderError("E_EPOCH_REVOKED", "Candidate is not an eligible shadow")
        candidate = rows[0]
        checkpoints = self.kernel.ledger.query(
            "SELECT state_hash,state_json FROM strategy_checkpoints WHERE strategy_epoch=? ORDER BY event_seq DESC LIMIT 1", (epoch,))
        if not checkpoints or checkpoints[0]["state_hash"] != state_hash or content_hash(strict_loads(checkpoints[0]["state_json"])) != state_hash:
            raise TraderError("E_STATE_HASH", "Candidate has not proved the current checkpoint")
        authority = self.kernel.ledger.query("SELECT * FROM authority WHERE singleton=1")[0]
        if authority["strategy_epoch"] != expected_old_epoch or authority["kernel_boot_id"] != self.kernel.boot_id:
            raise TraderError("E_EPOCH_REVOKED", "Durable strategy ownership does not match this boot")
        if expected_old_epoch is not None and epoch <= expected_old_epoch:
            raise TraderError("E_EPOCH_REVOKED", "Strategy epochs must advance")
        old_revision = authority["state_revision"]
        self.kernel._armed = False
        self.kernel.contexts.revoke_all()
        self.kernel._state_revision += 1
        self.kernel._authority_revision += 1
        self.kernel._strategy_switch_pending = True
        new_authority = self.kernel._authority()
        update_id = "strategy-switch-" + uuid4().hex
        online=bool(getattr(self.kernel.simulation,'strategy_enabled',False))
        detail = {"development_only": not online, "signature_verified": False,
                  "old_strategy_epoch": None if expected_old_epoch is None else str(expected_old_epoch),
                  "new_strategy_epoch": str(epoch), "barrier_event_seq": str(barrier_seq),
                  "checkpoint_hash": state_hash}

        def mutate(conn, seq):
            live = conn.execute("SELECT * FROM authority WHERE singleton=1").fetchone()
            if live["kernel_boot_id"] != self.kernel.boot_id or live["strategy_epoch"] != expected_old_epoch or live["state_revision"] != old_revision:
                raise TraderError("E_REVISION_CONFLICT", "Authority changed before the switch committed")
            if expected_old_epoch is not None:
                changed = conn.execute("UPDATE strategy_instances SET status='REVOKED',state_revision=state_revision+1,updated_event_seq=? WHERE strategy_epoch=? AND status='ACTIVE'", (seq, expected_old_epoch)).rowcount
                if changed != 1:
                    raise TraderError("E_EPOCH_REVOKED", "The original strategy is not active")
            changed = conn.execute("UPDATE strategy_instances SET status='ACTIVE',barrier_event_seq=?,state_revision=state_revision+1,updated_event_seq=? WHERE strategy_epoch=? AND status='SHADOW' AND state_revision=?", (barrier_seq, seq, epoch, candidate["state_revision"])).rowcount
            if changed != 1:
                raise TraderError("E_REVISION_CONFLICT", "Shadow state changed before commit")
            self.kernel._write_authority(conn, seq, new_authority)
            conn.execute("UPDATE authority SET strategy_epoch=? WHERE singleton=1", (epoch,))
            conn.execute(
                "INSERT INTO update_transactions(update_id,update_level,status,old_strategy_epoch,new_strategy_epoch,barrier_event_seq,manifest_hash,detail_json,updated_event_seq) VALUES (?,'U3','COMMITTED',?,?,?,?,?,?)",
                (update_id, expected_old_epoch, epoch, barrier_seq, content_hash(detail), canonical_bytes(detail).decode(), seq),
            )

        def accepted():
            self.kernel.active_epoch = epoch
            self.kernel._strategy_switch_pending = False

        return self.kernel._submit("TX_SWITCH", "BUILTIN_DSV_STRATEGY_SWITCH" if online else "DEVELOPMENT_STRATEGY_SWITCH", detail, mutate, after=accepted)

    def assert_active(self, epoch: int) -> None:
        self._check()
        epoch = self._epoch(epoch)
        if self.kernel._strategy_switch_pending or self.kernel.active_epoch != epoch:
            raise TraderError("E_EPOCH_REVOKED", "Strategy does not own the accepted active epoch")
        authority = self.kernel.ledger.query("SELECT strategy_epoch,kernel_boot_id FROM authority WHERE singleton=1")[0]
        rows = self.kernel.ledger.query("SELECT status FROM strategy_instances WHERE strategy_epoch=?", (epoch,))
        if authority["kernel_boot_id"] != self.kernel.boot_id or authority["strategy_epoch"] != epoch or not rows or rows[0]["status"] != "ACTIVE":
            raise TraderError("E_EPOCH_REVOKED", "Durable strategy ownership is not active")
