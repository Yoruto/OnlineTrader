import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from online_trader.contracts.codec import payload_hash
from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from qa.recording_port import RecordingPort


BASE = json.loads((Path(__file__).parents[1]/"qa/fixtures/base-plan.json").read_text(encoding="utf-8"))
ACCOUNT = {"scope_type":"ACCOUNT","scope_ids":[]}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/"ledger.db"
        self.account="DEV:control-"+uuid.uuid4().hex;self.port=RecordingPort()
        self.kernel=Kernel(self.path,self.account,development=True,port=self.port,risk_budget="1000")
        self.install_policies()

    def install_policies(self,suffix=""):
        self.authority="authority"+suffix;self.execution="execution"+suffix;self.resume="resume"+suffix
        self.kernel.control.register_policy(self.authority,"AUTHORITY",{"execution_scope":"DEVELOPMENT","allowed_origins":["MANUAL"]})
        self.kernel.control.register_policy(self.execution,"EXECUTION",{"execution_scope":"DEVELOPMENT"})
        self.kernel.control.register_policy(self.resume,"RESUME",{"execution_scope":"DEVELOPMENT"})

    def tearDown(self):
        try:self.kernel.close()
        finally:self.temp.cleanup()

    def plan(self,qty=1):
        p=copy.deepcopy(BASE);p.update(account_key=self.account,position_cycle_id="cycle-"+uuid.uuid4().hex,
            plan_id="plan-"+uuid.uuid4().hex,initial_open_quantity=qty,config_hash=self.kernel._configuration["payload_hash"])
        p["partial_reduction"]={"enabled":False};return p

    def message(self,operation,parameters,scope=None,command_id=None):
        scope=copy.deepcopy(scope or ACCOUNT)
        scope_id=self.kernel.control.context_scope(operation,scope,parameters)
        ctx=self.kernel.contexts.issue("desktop","OPERATOR_COMMAND",operation,scope_id,kernel_epoch=str(self.kernel.kernel_epoch),revisions=self.kernel.revisions)
        message={"protocol_version":"1.0","type":"OperatorCommand","message_id":"message-"+uuid.uuid4().hex,"connection_id":"connection-dev",
            "correlation_id":None,"sent_at_utc":"2026-11-09T00:00:00Z","kernel_boot_id":self.kernel.boot_id,"account_key":self.account,
            "body":{"command_id":command_id or "command-"+uuid.uuid4().hex,"context_id":ctx["context_id"],"kernel_epoch":str(self.kernel.kernel_epoch),
                "expected_revisions":ctx["related_revisions"],"operation":operation,"scope":scope,"parameters":parameters,"payload_hash":"0"*64}}
        message["body"]["payload_hash"]=payload_hash(message);return message

    def run_command(self,operation,parameters,scope=None):
        message=self.message(operation,parameters,scope)
        future=self.kernel.execute_command(message,"desktop");self.kernel.wait_idle()
        future.result()
        return self.kernel.control.get_status(message["body"]["command_id"],message["body"]["payload_hash"]).result()

    def arm(self):
        receipt=self.run_command("ARM_ENTRIES",{"rule_set_id":self.kernel._configuration["rule_set_id"],"config_revision":"1","authority_policy_id":self.authority})
        self.assertEqual(receipt["status"],"SUCCEEDED")

    def open(self,qty=1):
        self.arm();plan=self.plan(qty)
        receipt=self.run_command("MANUAL_OPEN",{"contract_id":plan["contract_id"],"direction":plan["direction"],"quantity":qty,"limit_price":"1000",
            "order_policy_id":self.execution,"protection_plan":plan},{"scope_type":"CONTRACT","scope_ids":[plan["contract_id"]]})
        self.assertEqual(receipt["status"],"NEEDS_RECONCILIATION" if self.port.fail_send else "EXECUTING")
        return plan,self.port.orders[-1]["child_order_id"]

    def fill_open(self,qty=1):
        plan,child=self.open(qty);self.kernel.record_fill(child,"fill-"+uuid.uuid4().hex,qty,"1000");self.kernel.wait_idle()
        self.kernel.observe(plan["position_cycle_id"],"1010",bid1="1010",ask1="1011");self.kernel.wait_idle()
        return plan,child

    def test_pause_command_is_atomic_idempotent_and_replay_survives_context_expiry(self):
        message=self.message("PAUSE_NEW_ENTRIES",{"cancel_owned_entry_remainders":False})
        first=self.kernel.execute_command(message,"desktop")
        self.assertIs(self.kernel.execute_command(message,"desktop"),first)
        self.kernel.wait_idle();receipt=first.result();self.kernel.contexts.revoke_all()
        replay=self.kernel.execute_command(message,"desktop").result()
        self.assertEqual(receipt,replay)
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM operator_commands")),1)
        self.assertEqual(receipt["status"],"SUCCEEDED")

    def test_same_command_changed_payload_is_conflict(self):
        message=self.message("PAUSE_NEW_ENTRIES",{"cancel_owned_entry_remainders":False})
        self.kernel.execute_command(message,"desktop");self.kernel.wait_idle()
        changed=copy.deepcopy(message);changed["body"]["parameters"]["cancel_owned_entry_remainders"]=True;changed["body"]["payload_hash"]=payload_hash(changed)
        with self.assertRaises(TraderError) as result:self.kernel.execute_command(changed,"desktop")
        self.assertEqual(result.exception.code,"E_DUPLICATE_CONFLICT")

    def test_invalid_scope_and_expired_context_are_durable_rejections(self):
        message=self.message("PAUSE_NEW_ENTRIES",{"cancel_owned_entry_remainders":False})
        self.kernel.contexts.revoke_all()
        result=self.kernel.execute_command(message,"desktop");self.kernel.wait_idle()
        self.assertEqual(result.result()["status"],"REJECTED")
        self.assertEqual(result.result()["error_code"],"E_CONTEXT_EXPIRED")

    def test_arm_requires_internal_policy_and_never_approves_business_rules(self):
        denied=self.run_command("ARM_ENTRIES",{"rule_set_id":self.kernel._configuration["rule_set_id"],"config_revision":"1","authority_policy_id":"not-registered"})
        self.assertEqual((denied["status"],denied["error_code"]),("REJECTED","E_RULE_UNAPPROVED"))
        self.arm();self.assertTrue(self.kernel.snapshot()["armed"])
        rows=self.kernel.ledger.query("SELECT DISTINCT status FROM rule_decisions")
        self.assertEqual({r["status"] for r in rows},{"PROPOSED"})

    def test_default_runtime_rejects_arm_without_manufacturing_simulated_facts(self):
        self.kernel.close();self.kernel=Kernel(self.path,self.account)
        denied=self.run_command("ARM_ENTRIES",{"rule_set_id":self.kernel._configuration["rule_set_id"],"config_revision":"1","authority_policy_id":self.authority})
        self.assertEqual(denied["error_code"],"E_RULE_UNAPPROVED")
        self.assertFalse(self.kernel.snapshot()["armed"])

    def test_manual_open_command_and_intent_share_one_atomic_transaction(self):
        plan,child=self.open(2)
        commands=self.kernel.ledger.query("SELECT * FROM operator_commands WHERE operation='MANUAL_OPEN'")
        intent=self.kernel.ledger.query("SELECT * FROM order_intents WHERE position_cycle_id=?",(plan["position_cycle_id"],))[0]
        self.assertEqual(commands[0]["created_event_seq"],intent["created_event_seq"])
        self.assertEqual(len(self.port.orders),1)

    def test_manual_open_rejects_mismatched_plan_and_risk_without_sending(self):
        self.arm();plan=self.plan(2)
        denied=self.run_command("MANUAL_OPEN",{"contract_id":plan["contract_id"],"direction":"SHORT","quantity":2,"limit_price":"1000","order_policy_id":self.execution,"protection_plan":plan},
            {"scope_type":"CONTRACT","scope_ids":[plan["contract_id"]]})
        self.assertEqual(denied["status"],"REJECTED");self.assertEqual(self.port.orders,[])

    def test_manual_close_uses_same_target_and_waits_for_fills(self):
        plan,_=self.fill_open(2);cid=plan["position_cycle_id"]
        receipt=self.run_command("MANUAL_CLOSE",{"position_cycle_id":cid,"quantity":1,"execution_policy_id":self.execution,"external_takeover_id":None},{"scope_type":"CYCLE","scope_ids":[cid]})
        self.assertEqual(receipt["status"],"EXECUTING")
        exit_order=next(o for o in self.port.orders if o["effect"]=="REDUCE")
        self.assertEqual(exit_order["original_qty"],1)
        self.kernel.record_fill(exit_order["child_order_id"],"exit-manual",1,"1010");self.kernel.wait_idle()
        latest=self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()
        self.assertEqual(latest["status"],"SUCCEEDED")
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["protection"]["owned_qty"],1)

    def test_flatten_does_not_succeed_before_full_evidence(self):
        plan,_=self.fill_open()
        receipt=self.run_command("FLATTEN_THEN_STOP",{"execution_policy_id":self.execution})
        self.assertEqual(receipt["status"],"EXECUTING")
        exit_order=next(o for o in self.port.orders if o["effect"]=="REDUCE")
        self.kernel.record_fill(exit_order["child_order_id"] ,"exit-flat",1,"1010");self.kernel.wait_idle()
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"EXECUTING")
        self.kernel.control.register_evidence("flat-proof")
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"SUCCEEDED")
        self.assertFalse(self.kernel.snapshot()["armed"])

    def test_empty_flatten_still_requires_four_account_fact_ranges(self):
        receipt=self.run_command("FLATTEN_THEN_STOP",{"execution_policy_id":self.execution})
        self.assertEqual(receipt["status"],"EXECUTING")
        self.kernel.control.register_evidence("empty-flatten-proof",complete=False)
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"EXECUTING")
        self.kernel.control.register_evidence("empty-flatten-proof")
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"SUCCEEDED")

    def test_selected_cancel_waits_for_quantity_consistent_terminal(self):
        _,child=self.open(2)
        receipt=self.run_command("CANCEL_SELECTED",{"child_order_ids":[child]},{"scope_type":"SELECTED_ORDERS","scope_ids":[child]})
        self.assertEqual(receipt["status"],"EXECUTING")
        self.assertEqual(len(self.port.cancellations),1)
        self.kernel.confirm_terminal(child,"CANCELLED",0);self.kernel.wait_idle()
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"SUCCEEDED")

    def test_owned_cancel_does_not_include_unselected_exit_orders(self):
        _,child=self.open(2)
        receipt=self.run_command("CANCEL_OWNED",{"include_exits":False},{"scope_type":"OWNED_ORDERS","scope_ids":[]})
        self.assertEqual(receipt["status"],"EXECUTING")
        self.assertEqual([o["child_order_id"] for o in self.port.cancellations],[child])

    def test_multiple_cancellations_are_prepared_in_command_transaction(self):
        _,first=self.open();_,second=self.open()
        receipt=self.run_command("CANCEL_SELECTED",{"child_order_ids":[first,second]},
            {"scope_type":"SELECTED_ORDERS","scope_ids":[first,second]})
        self.assertEqual(receipt["status"],"EXECUTING")
        command=self.kernel.ledger.query("SELECT created_event_seq FROM operator_commands WHERE command_id=?",(receipt["command_id"],))[0]
        rows=self.kernel.ledger.query("SELECT created_event_seq FROM cancel_actions WHERE child_order_id IN (?,?)",(first,second))
        self.assertEqual(len(rows),2)
        self.assertEqual({r["created_event_seq"] for r in rows},{command["created_event_seq"]})
        self.assertEqual({o["child_order_id"] for o in self.port.cancellations},{first,second})

    def test_ambiguous_cancel_retains_quantity_and_requires_reconciliation(self):
        _,child=self.open(2)
        def fail_cancel(request):raise RuntimeError("ambiguous cancel")
        self.port.cancel=fail_cancel
        receipt=self.run_command("CANCEL_SELECTED",{"child_order_ids":[child]},{"scope_type":"SELECTED_ORDERS","scope_ids":[child]})
        self.assertEqual((receipt["status"],receipt["error_code"]),("NEEDS_RECONCILIATION","E_UNKNOWN_SEND"))
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["orders"][0]["original_qty"],2)
        self.assertEqual(self.kernel.ledger.query("SELECT status FROM reservations")[0]["status"],"HELD")

    def test_account_cancel_requires_matching_registered_confirmation(self):
        _,child=self.open(2)
        self.kernel.control.register_evidence("account-cancel-proof",confirmation_id="confirm-cancel")
        bad=self.run_command("CANCEL_ACCOUNT_CONFIRMED",{"confirmation_id":"wrong","reconciliation_run_id":"account-cancel-proof","manifest_hash":self.kernel.control.manifest_hash(ACCOUNT)})
        self.assertEqual(bad["status"],"REJECTED");self.assertEqual(self.port.cancellations,[])
        good=self.run_command("CANCEL_ACCOUNT_CONFIRMED",{"confirmation_id":"confirm-cancel","reconciliation_run_id":"account-cancel-proof","manifest_hash":self.kernel.control.manifest_hash(ACCOUNT)})
        self.assertEqual(good["status"],"EXECUTING");self.assertEqual(len(self.port.cancellations),1)

    def test_reconciliation_request_does_not_self_certify_complete(self):
        receipt=self.run_command("REQUEST_RECONCILIATION",{"requested_scope":"FULL_ACCOUNT","position_cycle_id":None})
        self.assertEqual(receipt["status"],"NEEDS_RECONCILIATION")
        run_id=receipt["evidence_ids"][0]
        self.kernel.control.register_evidence(run_id,complete=False)
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"NEEDS_RECONCILIATION")
        self.kernel.control.register_evidence(run_id)
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"SUCCEEDED")

    def test_takeover_pending_keeps_protection_and_confirmed_transfer_stops_old_target(self):
        plan,child=self.fill_open(2);cid=plan["position_cycle_id"];scope={"scope_type":"CYCLE","scope_ids":[cid]}
        begin=self.run_command("BEGIN_TAKEOVER",{"takeover_id":"takeover-1","reason":"explicit development transfer"},scope)
        self.assertEqual(begin["status"],"SUCCEEDED")
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["management_state"],"TAKEOVER_PENDING")
        self.kernel.control.register_evidence("takeover-proof",scope)
        confirm=self.run_command("CONFIRM_TAKEOVER",{"takeover_id":"takeover-1","reconciliation_run_id":"takeover-proof","accepted_order_ids":[],"accepted_unknown_attempt_ids":[],"manifest_hash":self.kernel.control.manifest_hash(scope)},scope)
        self.assertEqual(confirm["status"],"SUCCEEDED")
        self.kernel.observe(cid,"980",bid1="980",ask1="981");self.kernel.wait_idle()
        self.assertEqual(len(self.port.orders),1)
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["management_state"],"TAKEN_OVER")

    def test_takeover_requires_every_unknown_attempt_and_exact_manifest(self):
        self.port.fail_send=True;plan,child=self.open();cid=plan["position_cycle_id"];scope={"scope_type":"CYCLE","scope_ids":[cid]}
        self.run_command("BEGIN_TAKEOVER",{"takeover_id":"takeover-unknown","reason":"unknown ownership transfer"},scope)
        self.kernel.control.register_evidence("unknown-proof",scope)
        order=self.kernel._staged[cid]["orders"][0]
        denied=self.run_command("CONFIRM_TAKEOVER",{"takeover_id":"takeover-unknown","reconciliation_run_id":"unknown-proof","accepted_order_ids":[child],"accepted_unknown_attempt_ids":[],"manifest_hash":self.kernel.control.manifest_hash(scope)},scope)
        self.assertEqual(denied["status"],"REJECTED")
        good=self.run_command("CONFIRM_TAKEOVER",{"takeover_id":"takeover-unknown","reconciliation_run_id":"unknown-proof","accepted_order_ids":[child],"accepted_unknown_attempt_ids":[order["attempt_id"]],"manifest_hash":self.kernel.control.manifest_hash(scope)},scope)
        self.assertEqual(good["status"],"SUCCEEDED")

    def test_changed_takeover_facts_invalidate_old_confirmation(self):
        plan,child=self.open(2);cid=plan["position_cycle_id"];scope={"scope_type":"CYCLE","scope_ids":[cid]}
        self.run_command("BEGIN_TAKEOVER",{"takeover_id":"takeover-changing","reason":"test late fact"},scope)
        self.kernel.control.register_evidence("stale-proof",scope);digest=self.kernel.control.manifest_hash(scope)
        self.kernel.record_fill(child,"late-during-takeover",1,"1000");self.kernel.wait_idle()
        denied=self.run_command("CONFIRM_TAKEOVER",{"takeover_id":"takeover-changing","reconciliation_run_id":"stale-proof","accepted_order_ids":[child],"accepted_unknown_attempt_ids":[],"manifest_hash":digest},scope)
        self.assertEqual(denied["error_code"],"E_FACT_BARRIER")

    def test_maintenance_needs_complete_evidence_and_blocks_rearming(self):
        receipt=self.run_command("ENTER_MAINTENANCE",{"maintenance_id":"maintenance-1","handover_record_id":None})
        self.assertEqual(receipt["status"],"NEEDS_RECONCILIATION")
        self.assertFalse(self.kernel.control.can_increase())
        self.kernel.control.register_evidence("empty-account-proof")
        done=self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()
        self.assertEqual(done["status"],"SUCCEEDED")
        self.assertEqual(self.kernel.ledger.query("SELECT status FROM maintenance_locks")[0]["status"],"SAFE_TO_STOP")
        denied=self.run_command("ARM_ENTRIES",{"rule_set_id":self.kernel._configuration["rule_set_id"],"config_revision":"1","authority_policy_id":self.authority})
        self.assertEqual(denied["status"],"REJECTED")

    def test_maintenance_with_position_cannot_claim_empty_safe_window(self):
        self.fill_open()
        receipt=self.run_command("ENTER_MAINTENANCE",{"maintenance_id":"maintenance-held","handover_record_id":None})
        self.kernel.control.register_evidence("held-account-proof")
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"NEEDS_RECONCILIATION")
        self.assertEqual(self.kernel.ledger.query("SELECT status FROM maintenance_locks")[0]["status"],"QUIESCING")

    def test_cycle_evidence_cannot_prove_account_maintenance(self):
        plan,child=self.open();self.kernel.confirm_terminal(child,"CANCELLED",0);self.kernel.wait_idle()
        receipt=self.run_command("ENTER_MAINTENANCE",{"maintenance_id":"narrow-proof","handover_record_id":None})
        self.kernel.control.register_evidence("cycle-only",{"scope_type":"CYCLE","scope_ids":[plan["position_cycle_id"]]})
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"NEEDS_RECONCILIATION")

    def test_late_fill_invalidates_safe_maintenance_barrier_durably(self):
        _,child=self.open(2);self.kernel.confirm_terminal(child,"CANCELLED",0);self.kernel.wait_idle()
        receipt=self.run_command("ENTER_MAINTENANCE",{"maintenance_id":"late-fill","handover_record_id":None})
        self.kernel.control.register_evidence("safe-before-late")
        safe=self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()
        self.assertEqual(safe["status"],"SUCCEEDED")
        self.kernel.record_fill(child,"late-after-safe",1,"1000");self.kernel.wait_idle()
        changed=self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()
        self.assertEqual((changed["status"],changed["error_code"]),("NEEDS_RECONCILIATION","E_FACT_BARRIER"))
        self.assertGreater(int(changed["receipt_revision"]),int(safe["receipt_revision"]))
        self.assertEqual(self.kernel.ledger.query("SELECT status FROM maintenance_locks")[0]["status"],"QUIESCING")

    def test_receipt_and_command_metadata_survive_restart_without_resend(self):
        plan,child=self.open()
        row=self.kernel.ledger.query("SELECT result_json,payload_json FROM operator_commands WHERE operation='MANUAL_OPEN'")[0]
        receipt=json.loads(row["result_json"]);body=json.loads(row["payload_json"])
        self.kernel.close();self.port=RecordingPort();self.kernel=Kernel(self.path,self.account,development=True,port=self.port,risk_budget="1000")
        self.kernel.wait_idle()
        status=self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()
        self.assertEqual(status["status"],"NEEDS_RECONCILIATION")
        row=self.kernel.ledger.query("SELECT receipt_revision,updated_event_seq,result_json FROM operator_commands WHERE command_id=?",
                                     (receipt["command_id"],))[0]
        self.assertEqual(status["receipt_revision"],str(row["receipt_revision"]))
        self.assertEqual(status["event_seq"],str(row["updated_event_seq"]))
        self.assertEqual(status,json.loads(row["result_json"]))
        self.assertEqual(self.kernel.control.states[body["command_id"]]["metadata"]["order_ids"],[child])
        self.assertEqual(self.port.orders,[])

    def test_restart_repairs_legacy_mismatched_receipt_revision(self):
        from contextlib import closing
        import sqlite3

        self.open()
        row=self.kernel.ledger.query("SELECT command_id,payload_hash,result_json FROM operator_commands WHERE operation='MANUAL_OPEN'")[0]
        old=json.loads(row['result_json'])
        self.kernel.close()
        # Older boots updated the row without advancing its published JSON.
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE operator_commands SET status='NEEDS_RECONCILIATION',receipt_revision=receipt_revision+1 WHERE command_id=?",
                         (row['command_id'],))
        self.port=RecordingPort()
        self.kernel=Kernel(self.path,self.account,development=True,port=self.port,risk_budget="1000")
        self.kernel.wait_idle()
        receipt=self.kernel.control.get_status(row['command_id'],row['payload_hash']).result()
        self.assertEqual(receipt['status'],'NEEDS_RECONCILIATION')
        self.assertEqual(int(receipt['receipt_revision']),int(old['receipt_revision'])+2)
        self.assertEqual(self.port.orders,[])
        self.assertFalse(self.kernel._failed)

    def test_restart_invalidates_old_safe_window_until_fresh_proof(self):
        receipt=self.run_command("ENTER_MAINTENANCE",{"maintenance_id":"reboot-safe","handover_record_id":None})
        self.kernel.control.register_evidence("pre-reboot-safe")
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"SUCCEEDED")
        self.kernel.close();self.kernel=Kernel(self.path,self.account,development=True,port=RecordingPort(),risk_budget="1000");self.kernel.wait_idle()
        self.assertEqual(self.kernel.ledger.query("SELECT status FROM maintenance_locks")[0]["status"],"QUIESCING")
        self.kernel.control.register_evidence("post-reboot-safe")
        self.assertEqual(self.kernel.control.get_status(receipt["command_id"],receipt["payload_hash"]).result()["status"],"SUCCEEDED")

    def test_resume_original_exits_uses_fresh_evidence_without_new_entry_grant(self):
        plan,child=self.fill_open();cid=plan["position_cycle_id"]
        self.kernel.observe(cid,"1080",bid1="1080",ask1="1081");self.kernel.wait_idle();self.kernel.close()
        self.port=RecordingPort();self.kernel=Kernel(self.path,self.account,development=True,port=self.port,risk_budget="1000");self.install_policies("-reboot")
        self.kernel.observe(cid,"1050",bid1="1050",ask1="1051");self.kernel.wait_idle()
        scope={"scope_type":"CYCLE","scope_ids":[cid]};self.kernel.control.register_evidence("resume-proof",scope)
        receipt=self.run_command("RESUME_ORIGINAL_EXITS",{"reconciliation_run_id":"resume-proof","position_cycle_ids":[cid],"recovery_authorization_id":self.resume},scope)
        self.assertEqual(receipt["status"],"SUCCEEDED")
        self.assertFalse(self.kernel.snapshot()["armed"])
        self.assertEqual([o["effect"] for o in self.port.orders],["REDUCE"])

    def test_command_and_intent_rollback_together_on_write_failure(self):
        self.arm();plan=self.plan();original=self.kernel.ledger.submit
        def fail(kind,event,payload,mutate=None,**kwargs):
            if event=="ENTRY_PREPARED":
                def bad(conn,seq):mutate(conn,seq);raise RuntimeError("injected rollback")
                return original(kind,event,payload,bad,**kwargs)
            return original(kind,event,payload,mutate,**kwargs)
        self.kernel.ledger.submit=fail
        msg=self.message("MANUAL_OPEN",{"contract_id":plan["contract_id"],"direction":"LONG","quantity":1,"limit_price":"1000","order_policy_id":self.execution,"protection_plan":plan},{"scope_type":"CONTRACT","scope_ids":[plan["contract_id"]]})
        future=self.kernel.execute_command(msg,"desktop")
        with self.assertRaises(TraderError):self.kernel.wait_idle()
        with self.assertRaises(TraderError):future.result()
        self.assertEqual(self.kernel.ledger.query("SELECT * FROM order_intents"),[])
        self.assertEqual(self.kernel.ledger.query("SELECT * FROM operator_commands WHERE command_id=?",(msg["body"]["command_id"],)),[])
        self.assertEqual(self.port.orders,[])
        with self.assertRaises(TraderError):self.kernel.close()
