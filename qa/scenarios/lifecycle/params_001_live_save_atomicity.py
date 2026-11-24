"""PR4 Scenario: Live parameter save atomicity & position protection (INV-013, INV-014)."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time
from uuid import uuid4

from online_trader.errors import TraderError
from online_trader.runtime.operator_api import RuntimeWorkbenchClient
from online_trader.transport.runtime_view import RuntimeViewClient
from qa.e2e import assert_offline_qa_environment
from qa.e2e.assembler import FrameAssembler
from qa.e2e.clock import VirtualClock
from qa.e2e.desktop_host import QADesktopHost
from qa.e2e.evidence import TraceRecorder
from qa.e2e.market import FakeMarket
from qa.e2e.oracle import ReferenceOracle
from qa.target_owner.fake_broker import FakeBroker
from qa.target_owner.offline_runtime import CONTRACT, DAY, profile


def run(work_dir: Path) -> dict[str, object]:
    assert_offline_qa_environment(block_tqsdk=True)
    host_dir = work_dir / "desktop-host"
    evidence_dir = work_dir / "evidence"
    trace = TraceRecorder(evidence_dir, scenario_name="E2E-PARAMS-001-live-save-atomicity", run_id="params-001")

    base_profile = profile().to_record()
    with QADesktopHost(host_dir, profile_record=base_profile, trading_day=DAY) as host:
        assert host.account_key and host.boot_id
        broker = FakeBroker(
            account_key=host.account_key,
            contract=CONTRACT,
            trading_day=DAY,
            trace_path=evidence_dir / "broker-events.jsonl",
            run_id="params-001",
        )
        clock = VirtualClock(f"{DAY}T01:09:50Z")
        market = FakeMarket(
            contract_id=CONTRACT,
            trading_day=DAY,
            session_open=f"{DAY}T01:00:00Z",
            session_close=f"{DAY}T02:15:00Z",
        )
        assembler = FrameAssembler(market=market, broker=broker, clock=clock, trading_day=DAY)
        oracle = ReferenceOracle()
        oracle.set_boot_expectation(host.boot_id)
        view_client = RuntimeViewClient(host.pipe)
        wb_client = RuntimeWorkbenchClient(host.pipe)
        cursor = 0
        expected_seq = 1

        def read_workbench_view(*, min_seq: int = 0) -> dict[str, object]:
            deadline = time.monotonic() + 5.0
            while True:
                msg = view_client.read()
                if msg.get("kind") == "BASELINE":
                    view = msg["body"]["view"]
                    if int(view.get("checkpoint_event_seq", 0)) >= min_seq:
                        return view
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for RuntimeViewClient baseline (min_seq={min_seq})")
                time.sleep(0.1)

        def push_round(wall_time: str, last: str) -> list[dict[str, object]]:
            nonlocal cursor, expected_seq
            clock.advance_to(wall_time)
            market.apply_tick(wall_time=clock.now_utc, last=last, candle_id="bar-0110")
            frame = assembler.build()
            fid = str(frame["id"])
            host.send_fact_frame(frame)
            host.wait_for_frame_applied(fid, after=cursor)
            broker.acknowledge_frame(fid)
            expected_seq += 1
            events = host.events[cursor:]
            cursor = len(host.events)
            outbound = [ev for ev in events if ev.get("event_type") in {"OUTBOUND_SEND", "OUTBOUND_CANCEL"}]
            for ev in outbound:
                broker.observe_adapter_event(ev)
            wb = read_workbench_view(min_seq=expected_seq)
            oracle.verify(broker_snapshot=broker.snapshot(), workbench_view=wb, round_seq=int(frame["sequence"]), frame_id=fid)
            return outbound

        def submit_wb(operation: str, parameters: dict[str, object], *, config_revision: str) -> dict[str, object]:
            nonlocal expected_seq
            receipt = wb_client.request(
                "submit",
                {
                    "command_id": f"wb-{uuid4().hex[:10]}",
                    "operation": operation,
                    "scope": {"scope_type": "ACCOUNT", "scope_ids": []},
                    "parameters": parameters,
                    "binding": {
                        "account_key": host.account_key,
                        "kernel_boot_id": host.boot_id,
                        "config_revision": config_revision,
                    },
                },
            )
            if receipt.get("status") == "SUCCEEDED":
                expected_seq += 1
            return receipt

        push_round(f"{DAY}T01:10:00Z", "1054")
        param_file = host_dir / "OnlineTrader-parameters.json"

        # 1. Flat hot-save succeeds: stop_offset 8 -> 10, revision 1 -> 2, disk updated
        updated_flat = deepcopy(base_profile)
        updated_flat["settings"]["symbols"][0]["stop_offset"] = "10"
        receipt_ok = submit_wb("SAVE_DSV_PARAMETERS", {"profile": updated_flat}, config_revision="1")
        if receipt_ok.get("status") != "SUCCEEDED":
            raise AssertionError(f"Expected flat SAVE_DSV_PARAMETERS to succeed, got {receipt_ok}")
        wb_after = read_workbench_view()
        deadline_rev = time.monotonic() + 3.0
        while str(wb_after["configuration"]["config_revision"]) != "2" and time.monotonic() < deadline_rev:
            time.sleep(0.1)
            wb_after = read_workbench_view()
        if str(wb_after["configuration"]["config_revision"]) != "2":
            raise AssertionError(f"Expected config_revision=2, got {wb_after['configuration']['config_revision']}")
        oracle.set_config_revision_expectation("2")
        saved_disk_before = param_file.read_bytes()

        # 2. Arm and open 5 lots -> while holding position, parameter save MUST be rejected (INV-013 & INV-014)
        submit_wb(
            "ARM_ENTRIES",
            {
                "authority_policy_id": "target-authority-current-boot",
                "rule_set_id": "DSV_TARGET_EXECUTION",
                "config_revision": "2",
            },
            config_revision="2",
        )
        oracle.set_authority_expectation("ARMED")
        oracle.set_target_expectation(CONTRACT, direction="LONG", quantity=5)
        sends = push_round(f"{DAY}T01:10:05Z", "1060")
        child_id = str(sends[0]["request"]["child_order_id"])
        broker.accept_order(child_id)
        push_round(f"{DAY}T01:10:06Z", "1060")
        broker.fill(child_id, 5, "1060")
        push_round(f"{DAY}T01:10:07Z", "1061")

        attempted_with_pos = deepcopy(updated_flat)
        attempted_with_pos["settings"]["symbols"][0]["stop_offset"] = "25"
        try:
            receipt_reject = submit_wb("SAVE_DSV_PARAMETERS", {"profile": attempted_with_pos}, config_revision="2")
            if receipt_reject.get("status") == "SUCCEEDED":
                raise AssertionError("INV-014 violation: live parameter save succeeded while holding position")
        except TraderError:
            # Expected E_DSV_SCOPE refusal from Owner
            pass

        # Verify INV-013 & INV-014: revision unchanged (still "2") AND file on disk untouched
        wb_final = read_workbench_view()
        if str(wb_final["configuration"]["config_revision"]) != "2":
            raise AssertionError(f"INV-013 violation: config_revision changed after failed save: {wb_final['configuration']}")
        if param_file.read_bytes() != saved_disk_before:
            raise AssertionError("INV-013 violation: disk parameters file mutated despite rejected save")

    return trace.finalize(passed=True)
