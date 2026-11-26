"""Phase-3 realtime 5D stress scenarios (headless Runtime + optional real Qt Workbench UI)."""

from __future__ import annotations

from pathlib import Path

from qa.scenario_engine import RealtimeStressRunner, StressProfile

DEFAULT_FIXTURE_DB = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "market"
    / "recorded"
    / "2026-11-25"
    / "market_fixture.sqlite3"
)

ALL_RECORDED_CONTRACTS = (
    "CZCE.OI701",
    "DCE.l2701",
    "DCE.p2701",
    "SHFE.ag2612",
    "SHFE.ag2702",
    "SHFE.bu2701",
)


def run_runtime_stress(
    work_dir: Path,
    *,
    fixture_db: Path = DEFAULT_FIXTURE_DB,
    contracts: tuple[str, ...] = ALL_RECORDED_CONTRACTS,
    market_rate_multiplier: float = 3.0,
    broker_event_rate_multiplier: float = 2.0,
    broker_return_delay_seconds: float = 0.5,
    duration_seconds: float = 30.0,
    baseline_path: Path | None = None,
    save_baseline_path: Path | None = None,
) -> dict[str, object]:
    """Run headless 6-contract realtime stress against isolated Standalone QA Owner."""
    profile = StressProfile(
        scenario_name="STRESS-001-runtime-6contracts-realtime",
        fixture_db=fixture_db,
        contracts=contracts,
        start_utc="2026-11-25T01:00:00Z",
        end_utc="2026-11-25T03:30:00Z",
        bucket_seconds=1,
        market_rate_multiplier=market_rate_multiplier,
        broker_event_rate_multiplier=broker_event_rate_multiplier,
        broker_return_delay_seconds=broker_return_delay_seconds,
        workbench_ops_per_minute=0.0,
        duration_seconds=duration_seconds,
        with_workbench_ui=False,
        enable_market_diagnostics=True,
        baseline_path=baseline_path,
        save_baseline_path=save_baseline_path,
    )
    return RealtimeStressRunner(profile, work_dir=work_dir).run()


def run_workbench_ui_stress(
    work_dir: Path,
    *,
    fixture_db: Path = DEFAULT_FIXTURE_DB,
    contracts: tuple[str, ...] = ALL_RECORDED_CONTRACTS,
    market_rate_multiplier: float = 3.0,
    workbench_ops_per_minute: float = 12.0,
    duration_seconds: float = 30.0,
    ui_offscreen: bool = True,
    baseline_path: Path | None = None,
    save_baseline_path: Path | None = None,
) -> dict[str, object]:
    """Run 6-contract realtime stress with real QADesktopHost + Qt Workbench tab/diagnostics switching."""
    profile = StressProfile(
        scenario_name="STRESS-002-workbench-ui-6contracts",
        fixture_db=fixture_db,
        contracts=contracts,
        start_utc="2026-11-25T01:00:00Z",
        end_utc="2026-11-25T03:30:00Z",
        bucket_seconds=1,
        market_rate_multiplier=market_rate_multiplier,
        broker_event_rate_multiplier=2.0,
        broker_return_delay_seconds=0.5,
        workbench_ops_per_minute=workbench_ops_per_minute,
        duration_seconds=duration_seconds,
        with_workbench_ui=True,
        ui_offscreen=ui_offscreen,
        enable_market_diagnostics=True,
        baseline_path=baseline_path,
        save_baseline_path=save_baseline_path,
    )
    return RealtimeStressRunner(profile, work_dir=work_dir).run()
