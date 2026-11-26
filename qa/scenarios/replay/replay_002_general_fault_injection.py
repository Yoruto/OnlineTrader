"""Phase-1 general replay + event-triggered fault injection scenario using MarketScenarioRunner."""

from __future__ import annotations

from pathlib import Path

from qa.scenario_engine import (
    BrokerMatchMode,
    ChannelFaultKind,
    FaultRule,
    MarketScenarioRunner,
    ScenarioConfig,
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

ALL_RECORDED_CONTRACTS = (
    "CZCE.OI701",
    "DCE.l2701",
    "DCE.p2701",
    "SHFE.ag2612",
    "SHFE.ag2702",
    "SHFE.bu2701",
)


def build_config(
    *,
    fixture_db: Path = DEFAULT_FIXTURE_DB,
    contracts: tuple[str, ...] = ALL_RECORDED_CONTRACTS,
    start_utc: str = "2026-11-25T01:00:00Z",
    end_utc: str = "2026-11-25T03:30:00Z",
    bucket_seconds: int = 1,
    order_volume: int = 5,
    max_rounds: int | None = None,
) -> ScenarioConfig:
    """Build a 6-contract recorded replay config with time & event-triggered faults."""
    rules = (
        # Time-triggered burst outage during morning session (buffers ticks then flushes together)
        FaultRule(
            rule_id="TIME_BURST_MORNING",
            trigger_kind=TriggerKind.TIME_RANGE,
            fault_kind=ChannelFaultKind.BURST,
            contracts=("DCE.p2701",),
            start_utc="2026-11-25T01:16:00Z",
            end_utc="2026-11-25T01:16:15Z",
            required=True,
        ),
        # On first open order: cap initial fill to 2 lots (out of 5) so partial-fill triggers
        FaultRule(
            rule_id="OPEN_PARTIAL_FILL",
            trigger_kind=TriggerKind.FIRST_OPEN_ORDER,
            fault_kind=ChannelFaultKind.NONE,
            duration_seconds=20,
            broker_partial_fill_qty=2,
            required=True,
        ),
        # After partial fill: interrupt both market feed and broker return channel for 12s
        FaultRule(
            rule_id="PARTIAL_FILL_FEED_AND_BROKER_OUTAGE",
            trigger_kind=TriggerKind.PARTIAL_FILL,
            fault_kind=ChannelFaultKind.DROP,
            duration_seconds=12,
            broker_hold_facts=True,
            broker_release_reverse=True,
            required=True,
        ),
        # If Runtime cancels during recovery: inject cancel-race late fill + duplicate trade replay
        FaultRule(
            rule_id="CANCEL_RACE_LATE_FILL",
            trigger_kind=TriggerKind.CANCEL_REQUESTED,
            fault_kind=ChannelFaultKind.NONE,
            duration_seconds=10,
            broker_late_fill_qty=1,
            broker_replay_trade=True,
            required=False,
        ),
        # When feed recovers: inject brief out-of-order/duplicate tick burst
        FaultRule(
            rule_id="RECOVERY_REORDER_DUPLICATE",
            trigger_kind=TriggerKind.FEED_RECOVERED,
            fault_kind=ChannelFaultKind.REORDER_DUPLICATE,
            duration_seconds=5,
            required=True,
        ),
    )
    return ScenarioConfig(
        scenario_name="E2E-REPLAY-002-general-fault-injection",
        fixture_db=fixture_db,
        contracts=contracts,
        start_utc=start_utc,
        end_utc=end_utc,
        bucket_seconds=bucket_seconds,
        max_rounds=max_rounds,
        match_mode=BrokerMatchMode.FAULTY,
        fault_rules=rules,
        order_volume=order_volume,
        max_quote_age_seconds=30,
        require_flat_convergence=False,
    )


def run(
    work_dir: Path,
    *,
    fixture_db: Path = DEFAULT_FIXTURE_DB,
    contracts: tuple[str, ...] = ALL_RECORDED_CONTRACTS,
    bucket_seconds: int = 1,
    max_rounds: int | None = None,
) -> dict[str, object]:
    """Run the Phase-1 general fault-injection replay scenario and return structured result."""
    config = build_config(
        fixture_db=fixture_db,
        contracts=contracts,
        bucket_seconds=bucket_seconds,
        max_rounds=max_rounds,
    )
    result = MarketScenarioRunner(config, work_dir=work_dir).run()
    return result.to_dict()
