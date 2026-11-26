"""Phase-2 synthetic market generation and constrained seeded random batch scenarios."""

from __future__ import annotations

import json
from pathlib import Path

from qa.scenario_engine import (
    BrokerMatchMode,
    ChannelFaultKind,
    ConstrainedRandomGenerator,
    FaultRule,
    FaultSeverity,
    GenerationMode,
    MarketScenarioRunner,
    ScenarioConfig,
    SynthesisSpec,
    TriggerKind,
)

DEFAULT_FIXTURE_DB = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "market"
    / "recorded"
    / "2026-11-25"
    / "market_fixture.sqlite3"
)

DEFAULT_CONTRACTS = (
    "DCE.p2701",
    "SHFE.ag2612",
    "SHFE.ag2702",
)


def run_targeted_limit_down(
    work_dir: Path,
    *,
    fixture_db: Path = DEFAULT_FIXTURE_DB,
    contracts: tuple[str, ...] = DEFAULT_CONTRACTS,
    bucket_seconds: int = 1,
    max_rounds: int = 240,
) -> dict[str, object]:
    """Targeted scenario: amplify real volatility, reverse trend, and hit limit-down one-sided book."""
    synthesis = SynthesisSpec(
        mode=GenerationMode.TARGETED,
        volatility_scale=2.0,
        drift_ticks_per_step=0.25,
        reverse_trend_at_ratio=0.45,
        force_limit_side="LIMIT_DOWN",
        limit_lock_after_ratio=0.72,
        liquidity_scale=0.5,
        seed=20261125,
    )
    rules = (
        FaultRule(
            rule_id="STOP_LOSS_LIMIT_DOWN_LOCK",
            trigger_kind=TriggerKind.STOP_LOSS_TRIGGERED,
            fault_kind=ChannelFaultKind.ONE_SIDED_BOOK,
            one_sided_side="LIMIT_DOWN",
            duration_seconds=15,
            required=False,
        ),
    )
    config = ScenarioConfig(
        scenario_name="E2E-REPLAY-003-targeted-limit-down",
        fixture_db=fixture_db,
        contracts=contracts,
        start_utc="2026-11-25T01:00:00Z",
        end_utc="2026-11-25T01:30:00Z",
        bucket_seconds=bucket_seconds,
        max_rounds=max_rounds,
        match_mode=BrokerMatchMode.BOOK_CONSTRAINED,
        fault_rules=rules,
        order_volume=3,
        max_quote_age_seconds=30,
        seed=20261125,
        synthesis=synthesis,
        export_generated_stream_jsonl=True,
    )
    return MarketScenarioRunner(config, work_dir=work_dir).run().to_dict()


def run_seeded_batch(
    work_dir: Path,
    *,
    fixture_db: Path = DEFAULT_FIXTURE_DB,
    seed: int = 42,
    count: int = 5,
    severity: FaultSeverity = FaultSeverity.MEDIUM,
    contracts: tuple[str, ...] = DEFAULT_CONTRACTS,
    bucket_seconds: int = 1,
    max_rounds: int = 180,
) -> dict[str, object]:
    """Generate and execute a reproducible batch of constrained random scenarios."""
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    generator = ConstrainedRandomGenerator(fixture_db, seed=seed)
    configs = generator.generate_batch(
        count=count,
        severity=severity,
        contracts=contracts,
        bucket_seconds=bucket_seconds,
        max_rounds=max_rounds,
    )
    results: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    for cfg in configs:
        case_dir = work_dir / cfg.scenario_name
        res = MarketScenarioRunner(cfg, work_dir=case_dir).run().to_dict()
        verdict = str(res["verdict"])
        counts[verdict] = counts.get(verdict, 0) + 1
        results.append(res)

    summary = {
        "schema_version": "MARKET_SCENARIO_BATCH/1",
        "seed": seed,
        "severity": severity.value,
        "count": len(results),
        "verdict_counts": counts,
        "results": results,
    }
    (work_dir / "batch-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return summary
