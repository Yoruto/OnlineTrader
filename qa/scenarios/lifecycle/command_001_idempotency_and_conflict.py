"""PR4 Scenario: Command idempotency/conflict (INV-011) & client disconnect safety (INV-012)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from online_trader.errors import TraderError
from online_trader.transport.status_client import StatusClient
from qa.e2e.assertions import assert_entry_authority, assert_no_active_orders
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def run(work_dir: Path) -> dict[str, object]:
    with TradingScenario(
        work_dir,
        scenario_name="E2E-COMMAND-001-idempotency-and-disconnect",
        contract=CONTRACT,
        trading_day=DAY,
    ) as scenario:
        assert scenario.client is not None

        # 1. Baseline sync
        scenario.step({"wall_time": f"{DAY}T01:10:00Z", "last": "1054", "bid1": "1053", "ask1": "1054", "candle_id": "bar-0110"})

        # 2. INV-011: Duplicate command_id with same payload is idempotent; differing payload conflicts
        fixed_cmd_id = f"cmd-idem-{uuid4().hex[:8]}"
        scope = {"scope_type": "ACCOUNT", "scope_ids": []}
        payload = {
            "authority_policy_id": "target-authority-current-boot",
            "rule_set_id": "DSV_TARGET_EXECUTION",
            "config_revision": "1",
        }
        r1 = scenario.client.command("ARM_ENTRIES", scope, payload, command_id=fixed_cmd_id, expected_config_revision="1")
        r2 = scenario.client.command("ARM_ENTRIES", scope, payload, command_id=fixed_cmd_id, expected_config_revision="1")
        if r1.get("status") != "SUCCEEDED" or r2.get("status") != "SUCCEEDED":
            raise AssertionError(f"Idempotent command failed: r1={r1}, r2={r2}")
        scenario.oracle.set_authority_expectation("ARMED")

        conflict_raised = False
        try:
            scenario.client.command(
                "PAUSE_NEW_ENTRIES",
                scope,
                {"config_revision": "1"},
                command_id=fixed_cmd_id,
                expected_config_revision="1",
            )
        except TraderError:
            conflict_raised = True
        if not conflict_raised:
            raise AssertionError("INV-011 violation: same command_id with different operation/payload was not rejected")

        # 3. INV-012: Opening & closing additional StatusClient sessions produces zero orders
        extra_status = StatusClient(
            scenario.process.pipe,
            timeout=5.0,
            expected_server_pid=scenario.process.owner_pid,
            expected_account_key=scenario.process.account_key,
            expected_kernel_boot_id=scenario.process.boot_id,
        )
        _ = extra_status.workbench()
        _ = extra_status.workbench_positions()
        del extra_status

        r_after = scenario.step({"wall_time": f"{DAY}T01:10:05Z", "last": "1055", "bid1": "1054", "ask1": "1055", "candle_id": "bar-0110"})
        assert_entry_authority(r_after.workbench, "ARMED")
        if r_after.outbound_events:
            raise AssertionError(f"INV-012 violation: client connect/disconnect triggered orders: {r_after.outbound_events}")
        assert_no_active_orders(broker_snapshot=r_after.broker_snapshot, workbench_view=r_after.workbench)

        return scenario.finish(passed=True)
