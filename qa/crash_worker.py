"""QA-only breakpoints in real child processes. Never installed in the wheel."""

import argparse
import copy
import json
import os
from pathlib import Path
import time
import uuid

from online_trader.kernel import Kernel
from online_trader.kernel.strategy import StrategyCoordinator
from qa.fault_report import atomic_json
from qa.recording_port import RecordingPort


BASE = Path(__file__).parent / "fixtures" / "base-plan.json"


def fixture_plan(kernel, quantity=1):
    plan = json.loads(BASE.read_text(encoding="utf-8"))
    plan.update(account_key=kernel.account_key, position_cycle_id="fault-cycle-" + uuid.uuid4().hex,
                plan_id="fault-plan-" + uuid.uuid4().hex, initial_open_quantity=quantity,
                config_hash=kernel._configuration["payload_hash"])
    plan["partial_reduction"] = {"enabled": False}
    return plan


def seed_position(kernel):
    kernel.arm_development(); kernel.wait_idle()
    plan = fixture_plan(kernel)
    context = kernel.issue_entry_context("fault-qa", plan["contract_id"])
    child = kernel.admit_entry(plan, "fault-decision-" + uuid.uuid4().hex, "1000", context_id=context["context_id"], peer_id="fault-qa")
    kernel.wait_idle()
    kernel.record_fill(child, "fault-entry-fill", 1, "1000"); kernel.wait_idle()
    return plan, child


def _park():
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        time.sleep(0.02)
    raise RuntimeError("QA parent did not terminate the checkpointed child")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--account-key", required=True)
    parser.add_argument("--mode", choices=["intent-committed", "send-entered", "protection-before-commit", "protection-committed", "switch-before-commit", "switch-committed"], required=True)
    parser.add_argument("--stage-file", type=Path, required=True)
    args = parser.parse_args(argv)
    port = RecordingPort()
    kernel = Kernel(args.db, args.account_key, development=True, port=port, risk_budget="1000")

    def checkpoint(phase, **data):
        atomic_json(args.stage_file, {"phase": phase, "pid": os.getpid(), "ppid": os.getppid(),
                    "kernel_boot_id": kernel.boot_id, "account_key": kernel.account_key, **data})

    if args.mode in {"intent-committed", "send-entered"}:
        kernel.arm_development(); kernel.wait_idle()
        plan = fixture_plan(kernel, 3)
        context = kernel.issue_entry_context("fault-qa", plan["contract_id"])
        if args.mode == "send-entered":
            original_send = port.send
            def entered(request):
                original_send(request)
                checkpoint("send-entered", cycle_id=plan["position_cycle_id"], plan_id=plan["plan_id"],
                           child_order_id=request["child_order_id"], attempt_id=request["attempt_id"], effects=len(port.orders))
                _park()
            port.send = entered
        child = kernel.admit_entry(plan, "fault-decision", "1000", context_id=context["context_id"], peer_id="fault-qa")
        if args.mode == "intent-committed":
            ack = kernel._pending[-1].future.result(timeout=3)
            checkpoint("intent-committed", cycle_id=plan["position_cycle_id"], plan_id=plan["plan_id"],
                       child_order_id=child, durable_event_seq=ack.event_seq, effects=len(port.orders))
            _park()
        kernel.wait_idle(timeout=7)
        raise AssertionError("send-entered breakpoint was not reached")

    plan, child = seed_position(kernel)
    if args.mode.startswith("protection"):
        before = kernel._durable_seq
        if args.mode == "protection-before-commit":
            original_submit = kernel.ledger.submit
            def hold(kind, event, payload, mutate=None, **kwargs):
                if event == "PROTECTION_OBSERVATION":
                    def held(conn, seq):
                        mutate(conn, seq)
                        checkpoint("protection-before-commit", cycle_id=plan["position_cycle_id"], plan_id=plan["plan_id"],
                                   child_order_id=child, before_durable_seq=before, pending_event_seq=seq, effects=len(port.orders))
                        _park()
                    return original_submit(kind, event, payload, held, **kwargs)
                return original_submit(kind, event, payload, mutate, **kwargs)
            kernel.ledger.submit = hold
        future = kernel.observe(plan["position_cycle_id"], "1080", bid1="1080", ask1="1081")
        if args.mode == "protection-committed":
            ack = future.result(timeout=3)
            checkpoint("protection-committed", cycle_id=plan["position_cycle_id"], plan_id=plan["plan_id"],
                       child_order_id=child, before_durable_seq=before, durable_event_seq=ack.event_seq, effects=len(port.orders))
            _park()
        kernel.wait_idle(timeout=7)
        raise AssertionError("protection breakpoint was not reached")

    kernel.observe(plan["position_cycle_id"], "1080", bid1="1080", ask1="1081"); kernel.wait_idle()
    coordinator = StrategyCoordinator(kernel)
    _, state_hash = coordinator.checkpoint()
    old = coordinator.register_shadow("fault-old", state_hash)
    barrier, state_hash = coordinator.checkpoint()
    coordinator.switch(old, None, barrier, state_hash); kernel.wait_idle()
    _, state_hash = coordinator.checkpoint()
    new = coordinator.register_shadow("fault-new", state_hash)
    barrier, state_hash = coordinator.checkpoint()
    if args.mode == "switch-before-commit":
        original_submit = kernel.ledger.submit
        def hold_switch(kind, event, payload, mutate=None, **kwargs):
            if event == "DEVELOPMENT_STRATEGY_SWITCH":
                def held(conn, seq):
                    mutate(conn, seq)
                    checkpoint("switch-before-commit", cycle_id=plan["position_cycle_id"], plan_id=plan["plan_id"],
                               child_order_id=child, old_epoch=old, new_epoch=new, barrier_event_seq=barrier, pending_event_seq=seq, effects=len(port.orders))
                    _park()
                return original_submit(kind, event, payload, held, **kwargs)
            return original_submit(kind, event, payload, mutate, **kwargs)
        kernel.ledger.submit = hold_switch
    future = coordinator.switch(new, old, barrier, state_hash)
    if args.mode == "switch-committed":
        ack = future.result(timeout=3)
        checkpoint("switch-committed", cycle_id=plan["position_cycle_id"], plan_id=plan["plan_id"], child_order_id=child,
                   old_epoch=old, new_epoch=new, barrier_event_seq=barrier, durable_event_seq=ack.event_seq,
                   owner_active_epoch=kernel.active_epoch, effects=len(port.orders))
        _park()
    kernel.wait_idle(timeout=7)
    raise AssertionError("switch breakpoint was not reached")


if __name__ == "__main__":
    main()
