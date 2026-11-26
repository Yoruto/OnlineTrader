"""Phase-4 multi-day cross-trading-day endurance and failure shrinking entrypoints."""

from __future__ import annotations

from pathlib import Path

from qa.scenario_engine import (
    BrokerMatchMode,
    FailureShrinker,
    MarketCorpus,
    MultiDayConfig,
    MultiDayEnduranceRunner,
    ScenarioConfig,
)

RECORDED_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "market" / "recorded"


def run_multiday_endurance(
    work_dir: Path,
    *,
    corpus_root: Path = RECORDED_CORPUS_ROOT,
    days: int = 3,
    contracts: tuple[str, ...] = ("DCE.p2701", "SHFE.ag2702", "SHFE.bu2701"),
    bucket_seconds: int = 2,
    max_rounds_per_day: int = 120,
    seed: int = 20261125,
) -> dict[str, object]:
    """Run consecutive virtual trading days across discovered recorded fixtures."""
    corpus = MarketCorpus(corpus_root)
    if not corpus.databases:
        raise FileNotFoundError(f"No recorded SQLite fixtures found under {corpus_root}")
    cfg = MultiDayConfig(
        scenario_name=f"E2E-MULTIDAY-{days}D-seed-{seed}",
        fixture_dbs=tuple(corpus.databases),
        days=days,
        contracts=contracts,
        bucket_seconds=bucket_seconds,
        max_rounds_per_day=max_rounds_per_day,
        match_mode=BrokerMatchMode.BOOK_CONSTRAINED,
        seed=seed,
    )
    return MultiDayEnduranceRunner(cfg, work_dir=work_dir).run()


def shrink_failed_config(
    config: ScenarioConfig,
    *,
    work_dir: Path,
    max_attempts: int = 24,
) -> dict[str, object]:
    """Minimize a failing ScenarioConfig into a compact reproducible regression artifact."""
    return FailureShrinker(work_dir=work_dir, max_attempts=max_attempts).shrink(config)
