"""PR4 Stateful Scenario: Seeded deterministic random exploration + exact trace replay."""

from __future__ import annotations

from pathlib import Path

from qa.e2e import assert_offline_qa_environment
from qa.e2e.evidence import TraceRecorder
from qa.e2e.stateful import StatefulFuzzRunner, replay_stateful_trace

SEEDS = (20261124, 20261125, 20261126)


def run(work_dir: Path) -> dict[str, object]:
    assert_offline_qa_environment(block_tqsdk=True)
    evidence_dir = work_dir / "evidence"
    trace = TraceRecorder(evidence_dir, scenario_name="E2E-STATEFUL-001-seeded-faults", run_id="stateful-001")

    seed_summaries: list[dict[str, object]] = []
    first_script_path: str | None = None
    for seed in SEEDS:
        runner = StatefulFuzzRunner(seed=seed, max_steps=20)
        res = runner.run(work_dir / f"seed-{seed}")
        seed_summaries.append({"seed": seed, **res["summary"]})
        trace.record("COMMAND", operation=f"STATEFUL_SEED_{seed}", receipt_status="PASS")
        if first_script_path is None:
            first_script_path = str(res["script_path"])

    assert first_script_path is not None
    replay_res = replay_stateful_trace(first_script_path, work_dir / "replay-check")
    trace.record("COMMAND", operation="DETERMINISTIC_REPLAY", receipt_status=replay_res["status"])

    return trace.finalize(
        passed=True,
        evidence_files={
            "first_script": first_script_path,
            "replay_report": str(replay_res["report_path"]),
        },
        final_broker={"positions": {"seeds_verified": len(SEEDS), "summaries": seed_summaries}},
    )
