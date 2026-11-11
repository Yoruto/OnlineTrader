import copy
from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from qa.recording_port import RecordingPort


BASE=json.loads((Path(__file__).parents[1]/"qa/fixtures/base-plan.json").read_text(encoding="utf-8"))


def plan_for(kernel, qty=5):
    plan=copy.deepcopy(BASE)
    plan.update(account_key=kernel.account_key,position_cycle_id="cycle-"+uuid.uuid4().hex,
                plan_id="plan-"+uuid.uuid4().hex,initial_open_quantity=qty,
                config_hash=kernel._configuration["payload_hash"])
    plan["partial_reduction"]={"enabled":False}
    return plan


def admit(kernel,plan,decision=None):
    context=kernel.issue_entry_context("qa",plan["contract_id"])
    return kernel.admit_entry(plan,decision or "decision-"+uuid.uuid4().hex,"1000",context_id=context["context_id"])


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"ledger.db"
        self.account="DEV:account-"+uuid.uuid4().hex
        self.port=RecordingPort()
        self.kernel=Kernel(self.path,self.account,development=True,port=self.port,risk_budget="1000")

    def tearDown(self):
        try:self.kernel.close()
        finally:self.temp.cleanup()

    def arm(self):
        self.kernel.arm_development();self.kernel.wait_idle()

    def test_default_entrypoint_cannot_arm_unapproved_rules(self):
        self.kernel.close()
        self.kernel=Kernel(self.path,self.account)
        with self.assertRaises(TraderError) as result:self.kernel.arm_development()
        self.assertEqual(result.exception.code,"E_RULE_UNAPPROVED")

    def test_no_sender_before_owner_accepts_commit_ack(self):
        self.arm();plan=plan_for(self.kernel)
        child=admit(self.kernel,plan)
        self.assertEqual(self.port.orders,[])
        self.kernel.wait_idle()
        self.assertEqual(len(self.port.orders),1)
        self.assertEqual(self.port.orders[0]["child_order_id"],child)
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["protection"]["owned_qty"],0)

    def test_pending_reservations_prevent_double_budget(self):
        self.kernel.budget=__import__('decimal').Decimal("80")
        self.arm();admit(self.kernel,plan_for(self.kernel,3))
        other=plan_for(self.kernel,2);other["contract_id"]="DEV.TEST002"
        with self.assertRaises(TraderError) as result:admit(self.kernel,other)
        self.assertEqual(result.exception.code,"E_RISK")
        self.assertEqual(self.port.orders,[])
        self.kernel.wait_idle();self.assertEqual(len(self.port.orders),1)

    def test_later_valid_ack_waits_for_earlier_future_before_any_send(self):
        self.arm();before=self.kernel.snapshot()["durable_seq"]
        original=self.kernel.ledger.submit;held=[]
        def delayed(kind,event,payload,mutate=None,**kwargs):
            committed=original(kind,event,payload,mutate,**kwargs)
            if event!="ENTRY_PREPARED":return committed
            visible=Future();held.append((committed,visible));return visible
        self.kernel.ledger.submit=delayed
        first=admit(self.kernel,plan_for(self.kernel,1))
        second=admit(self.kernel,plan_for(self.kernel,1))
        try:
            # Both real transactions commit, but their valid Ack deliveries complete in reverse order.
            first_ack=held[0][0].result(timeout=3)
            second_ack=held[1][0].result(timeout=3)
            held[1][1].set_result(second_ack)
            self.assertEqual(self.kernel.poll(),0)
            self.assertEqual(self.kernel.snapshot()["durable_seq"],before)
            self.assertEqual(self.port.orders,[])
        finally:
            if not held[0][1].done():held[0][1].set_result(held[0][0].result(timeout=3))
            if not held[1][1].done():held[1][1].set_result(held[1][0].result(timeout=3))
        self.kernel.wait_idle()
        self.assertEqual([o["child_order_id"] for o in self.port.orders],[first,second])
        self.assertFalse(self.kernel.snapshot()["storage_failed"])

    def test_pause_while_storage_pending_prevents_send(self):
        self.arm();gate=threading.Event();entered=threading.Event()
        original=self.kernel.ledger.submit
        def delayed(kind,event,payload,mutate=None,**kwargs):
            if event=="ENTRY_PREPARED":
                def delayed_mutate(conn,seq):
                    entered.set()
                    if not gate.wait(5):raise RuntimeError("test deadline")
                    mutate(conn,seq)
                return original(kind,event,payload,delayed_mutate,**kwargs)
            return original(kind,event,payload,mutate,**kwargs)
        self.kernel.ledger.submit=delayed
        try:
            admit(self.kernel,plan_for(self.kernel))
            self.assertTrue(entered.wait(2))
            self.kernel.pause()
            self.assertEqual(self.port.orders,[])
        finally:gate.set()
        self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["orders"][0]["send_state"],"DEFINITELY_NOT_SENT")

    def test_partial_fill_protected_late_fill_and_single_exit_target(self):
        self.arm();plan=plan_for(self.kernel);cid=plan["position_cycle_id"]
        entry=admit(self.kernel,plan);self.kernel.wait_idle()
        self.kernel.record_fill(entry,"trade-entry-1",1,"1000");self.kernel.wait_idle()
        self.kernel.observe(cid,"1080",bid1="1080",ask1="1081");self.kernel.wait_idle()
        state=self.kernel.snapshot()["cycles"][0]["protection"]
        self.assertEqual(state["mode"],"PROTECTED")
        self.assertEqual(len(self.port.cancellations),1)
        self.kernel.observe(cid,"1050",bid1="1050",ask1="1051");self.kernel.wait_idle()
        exits=[o for o in self.port.orders if o["effect"]=="REDUCE"]
        self.assertEqual([o["original_qty"] for o in exits],[1])
        self.kernel.record_fill(entry,"trade-entry-late",2,"1000");self.kernel.wait_idle()
        exits=[o for o in self.port.orders if o["effect"]=="REDUCE"]
        self.assertEqual(sum(o["original_qty"] for o in exits),3)
        self.kernel.confirm_terminal(entry,"CANCELLED",3);self.kernel.wait_idle()
        for index,item in enumerate(exits):
            self.kernel.record_fill(item["child_order_id"],f"trade-exit-{index}",item["original_qty"],"1050")
            self.kernel.wait_idle()
        self.kernel.reconcile_development_cycle(cid,close=True);self.kernel.wait_idle()
        snap=self.kernel.snapshot()["cycles"][0]
        self.assertEqual(snap["lifecycle"],"CLOSED")
        self.assertEqual(snap["protection"]["owned_qty"],0)
        self.assertEqual(snap["protection"]["mode"],"PROTECTED")
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM fill_facts")),4)

    def test_fill_duplicate_is_idempotent_and_conflict_is_visible(self):
        self.arm();entry=admit(self.kernel,plan_for(self.kernel));self.kernel.wait_idle()
        self.assertTrue(self.kernel.record_fill(entry,"trade-same",1,"1000"));self.kernel.wait_idle()
        before=self.kernel.snapshot()["durable_seq"]
        self.assertFalse(self.kernel.record_fill(entry,"trade-same",1,"1000"));self.kernel.wait_idle()
        self.assertEqual(self.kernel.snapshot()["durable_seq"],before)
        with self.assertRaises(TraderError):self.kernel.record_fill(entry,"trade-same",2,"1000")
        self.kernel.wait_idle()
        self.assertFalse(self.kernel.snapshot()["armed"])
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM fill_facts")),1)

    def test_cancel_request_and_quantity_gap_do_not_release(self):
        self.arm();entry=admit(self.kernel,plan_for(self.kernel));self.kernel.wait_idle()
        self.kernel.request_cancel(entry);self.kernel.wait_idle()
        cycle=self.kernel.snapshot()["cycles"][0]
        self.assertEqual(cycle["orders"][0]["cancel_status"],"CALL_RETURNED")
        with self.assertRaises(TraderError):self.kernel.confirm_terminal(entry,"CANCELLED",1)
        reservation=self.kernel.ledger.query("SELECT status,amount FROM reservations")[0]
        self.assertEqual(reservation["status"],"HELD")
        self.assertEqual(reservation["amount"],"100")

    def test_restart_keeps_unknown_identity_and_never_replays_send(self):
        self.arm();entry=admit(self.kernel,plan_for(self.kernel));self.kernel.wait_idle()
        self.kernel.close();port=RecordingPort()
        self.kernel=Kernel(self.path,self.account,development=True,port=port,risk_budget="1000")
        order=self.kernel.snapshot()["cycles"][0]["orders"][0]
        self.assertEqual(order["child_order_id"],entry)
        self.assertEqual(order["send_state"],"UNKNOWN")
        self.assertEqual(port.orders,[])
        with self.assertRaises(TraderError):self.kernel.arm_development()

    def test_protected_restart_requires_explicit_reconciliation_before_exit(self):
        self.arm();plan=plan_for(self.kernel,1);cid=plan["position_cycle_id"]
        entry=admit(self.kernel,plan);self.kernel.wait_idle()
        self.kernel.record_fill(entry,"entry-full",1,"1000");self.kernel.wait_idle()
        self.kernel.observe(cid,"1080",bid1="1080",ask1="1081");self.kernel.wait_idle()
        self.kernel.close();port=RecordingPort()
        self.kernel=Kernel(self.path,self.account,development=True,port=port,risk_budget="1000")
        self.kernel.observe(cid,"1050",bid1="1050",ask1="1051");self.kernel.wait_idle()
        self.assertEqual(port.orders,[])
        self.kernel.reconcile_development_cycle(cid);self.kernel.wait_idle()
        self.assertEqual(len(port.orders),1)
        self.assertEqual(port.orders[0]["effect"],"REDUCE")

    def test_lost_send_result_preserves_unknown_and_budget(self):
        self.arm();self.port.fail_send=True
        admit(self.kernel,plan_for(self.kernel));self.kernel.wait_idle()
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["orders"][0]["send_state"],"UNKNOWN")
        self.assertEqual(self.kernel.ledger.query("SELECT status FROM reservations")[0]["status"],"UNKNOWN")

    def test_scope_lock_cannot_be_bypassed_with_another_data_path(self):
        with self.assertRaises(TraderError):Kernel(Path(self.temp.name)/"other.db",self.account)

    def test_state_mutation_from_foreign_thread_is_rejected(self):
        errors=[]
        def other():
            try:self.kernel.pause()
            except TraderError as exc:errors.append(exc.code)
        thread=threading.Thread(target=other);thread.start();thread.join()
        self.assertEqual(errors,["E_OWNER"])

    def test_context_expiring_after_admission_prevents_dispatch(self):
        self.arm();clock=[0.0];self.kernel.contexts._clock=lambda:clock[0]
        admit(self.kernel,plan_for(self.kernel));clock[0]=31.0
        self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["orders"][0]["send_state"],"DEFINITELY_NOT_SENT")

    def test_cancel_unsubmitted_order_never_sends_it_first(self):
        self.arm();child=admit(self.kernel,plan_for(self.kernel))
        self.kernel.request_cancel(child);self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.port.cancellations,[])

    def test_first_fill_evaluates_existing_liquidation_quote(self):
        self.arm();plan=plan_for(self.kernel,1);plan["price_field"]="LIQUIDATION_SIDE"
        entry=admit(self.kernel,plan);self.kernel.wait_idle()
        self.kernel.observe(plan["position_cycle_id"],"1079",bid1="1080",ask1="1081");self.kernel.wait_idle()
        self.kernel.record_fill(entry,"entry-current-quote",1,"1000");self.kernel.wait_idle()
        self.assertEqual(self.kernel.snapshot()["cycles"][0]["protection"]["mode"],"PROTECTED")

    def test_invalid_quote_persists_gap_and_removes_executable_quote(self):
        self.arm();plan=plan_for(self.kernel,1);entry=admit(self.kernel,plan);self.kernel.wait_idle()
        self.kernel.record_fill(entry,"entry-gap",1,"1000");self.kernel.wait_idle()
        self.kernel.observe(plan["position_cycle_id"],"1080",bid1="1080",ask1="1081");self.kernel.wait_idle()
        self.kernel.observe(plan["position_cycle_id"],"NaN",valid=False);self.kernel.wait_idle()
        cycle=self.kernel.snapshot()["cycles"][0]
        self.assertIsNone(cycle["last_quote"])
        self.assertEqual(cycle["protection"]["quality"],"DATA_GAP")
        self.assertEqual(len(self.port.orders),1)

    def test_command_replay_after_expired_context_does_not_repeat_effect(self):
        from online_trader.contracts.codec import payload_hash
        context=self.kernel.contexts.issue("desktop","OPERATOR_COMMAND","PAUSE_NEW_ENTRIES",self.account,
            kernel_epoch=str(self.kernel.kernel_epoch),revisions=self.kernel.revisions)
        message={"protocol_version":"1.0","type":"OperatorCommand","message_id":"message-1","connection_id":"connection-1",
            "correlation_id":None,"sent_at_utc":"2026-11-09T00:00:00Z","kernel_boot_id":self.kernel.boot_id,"account_key":self.account,
            "body":{"command_id":"pause-same","context_id":context["context_id"],"kernel_epoch":str(self.kernel.kernel_epoch),
                "expected_revisions":context["related_revisions"],"operation":"PAUSE_NEW_ENTRIES","scope":{"scope_type":"ACCOUNT","scope_ids":[]},
                "parameters":{"cancel_owned_entry_remainders":False},"payload_hash":"0"*64}}
        message["body"]["payload_hash"]=payload_hash(message)
        first=self.kernel.execute_pause_command(message,"desktop")
        self.assertIs(self.kernel.execute_pause_command(message,"desktop"),first)
        self.kernel.wait_idle();receipt=first.result()
        replay=self.kernel.execute_pause_command(message,"desktop").result()
        self.assertEqual(replay,receipt)
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM operator_commands")),1)

    def test_failed_intent_transaction_never_dispatches_or_advances_durable(self):
        self.arm();before=self.kernel.snapshot()["durable_seq"]
        original=self.kernel.ledger.submit
        def failed(kind,event,payload,mutate=None,**kwargs):
            if event=="ENTRY_PREPARED":
                def bad(conn,seq):
                    mutate(conn,seq)
                    raise RuntimeError("Injected storage failure before COMMIT")
                return original(kind,event,payload,bad,**kwargs)
            return original(kind,event,payload,mutate,**kwargs)
        self.kernel.ledger.submit=failed
        admit(self.kernel,plan_for(self.kernel))
        with self.assertRaises(TraderError):self.kernel.wait_idle()
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.kernel.snapshot()["durable_seq"],before)
        self.assertEqual(self.kernel.ledger.query("SELECT * FROM order_intents"),[])
        with self.assertRaises(TraderError):self.kernel.close()

    def test_close_does_not_dispatch_pending_opening(self):
        self.arm();admit(self.kernel,plan_for(self.kernel))
        self.kernel.close()
        self.assertEqual(self.port.orders,[])

    def test_exit_quote_changed_during_commit_does_not_send_old_price(self):
        import time
        self.arm();plan=plan_for(self.kernel,1);cid=plan["position_cycle_id"]
        entry=admit(self.kernel,plan);self.kernel.wait_idle()
        self.kernel.record_fill(entry,"entry-price-binding",1,"1000");self.kernel.wait_idle()
        self.kernel.observe(cid,"1080",bid1="1080",ask1="1081");self.kernel.wait_idle()
        entered=threading.Event();gate=threading.Event();original=self.kernel.ledger.submit
        def delayed(kind,event,payload,mutate=None,**kwargs):
            if event=="EXIT_PREPARED" and not entered.is_set():
                def held(conn,seq):
                    entered.set()
                    if not gate.wait(5):raise RuntimeError("test deadline")
                    mutate(conn,seq)
                return original(kind,event,payload,held,**kwargs)
            return original(kind,event,payload,mutate,**kwargs)
        self.kernel.ledger.submit=delayed
        try:
            self.kernel.observe(cid,"1050",bid1="1050",ask1="1051")
            until=time.monotonic()+2
            while not entered.is_set() and time.monotonic()<until:self.kernel.poll();time.sleep(0.001)
            self.assertTrue(entered.is_set())
            self.kernel.observe(cid,"1000",bid1="1000",ask1="1001")
        finally:gate.set()
        self.kernel.wait_idle()
        exits=[o for o in self.port.orders if o["effect"]=="REDUCE"]
        self.assertEqual([o["limit_price"] for o in exits],["1000"])
