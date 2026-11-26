"""Core data models for Market Scenario Engine (three-layer truth separation)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


ENGINE_VERSION = "MARKET_SCENARIO_ENGINE/1.0"


class Verdict(str, Enum):
    """Strict scenario outcome classification (NOT_COVERED is never PASS)."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_COVERED = "NOT_COVERED"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    DEGRADED = "DEGRADED"


class ChannelFaultKind(str, Enum):
    """Distinct market-data channel discontinuity / impairment modes."""

    NONE = "NONE"
    DROP = "DROP"
    DELAY = "DELAY"
    FREEZE = "FREEZE"
    BURST = "BURST"
    REORDER_DUPLICATE = "REORDER_DUPLICATE"
    ONE_SIDED_BOOK = "ONE_SIDED_BOOK"


class BrokerMatchMode(str, Enum):
    """Four-tier simulated broker matching behavior."""

    IDEAL = "IDEAL"
    BOOK_CONSTRAINED = "BOOK_CONSTRAINED"
    CONSERVATIVE = "CONSERVATIVE"
    FAULTY = "FAULTY"


class TriggerKind(str, Enum):
    """Time-based or trading-event-based fault trigger conditions."""

    TIME_RANGE = "TIME_RANGE"
    FIRST_OPEN_ORDER = "FIRST_OPEN_ORDER"
    FIRST_FILL = "FIRST_FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    STOP_LOSS_TRIGGERED = "STOP_LOSS_TRIGGERED"
    MULTI_CONTRACT_ORDER = "MULTI_CONTRACT_ORDER"
    FEED_RECOVERED = "FEED_RECOVERED"


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    trading_day: str
    exchange_id: str
    instrument_id: str
    price_tick: Decimal
    volume_multiple: Decimal
    upper_limit: str | None
    lower_limit: str | None
    pre_close: str | None = None
    pre_settlement: str | None = None


@dataclass(frozen=True)
class SessionWindow:
    symbol: str
    trading_day: str
    session_id: str
    session_kind: str
    open_utc: str
    close_utc: str
    open_ns: int
    close_ns: int
    is_opening_session: bool
    force_close_at_end: bool


@dataclass(frozen=True)
class MarketTickTruth:
    """Layer 1: Simulated exchange truth (what actually happened in the market)."""

    symbol: str
    trading_day: str
    source_time_ns: int
    source_time_utc: str
    last: str
    bid1: str | None
    bid_volume1: int | None
    ask1: str | None
    ask_volume1: int | None
    day_low: str
    day_high: str
    lower_limit: str | None
    upper_limit: str | None
    candle_id: str
    opening_session: bool = True
    market_session_active: bool = True
    session_open_utc: str | None = None
    session_close_utc: str | None = None


@dataclass
class ObservedTick:
    """Layer 3: What the software actually receives over the market channel."""

    symbol: str
    source_time_utc: str
    delivery_time_utc: str
    last: str | None
    bid1: str | None = None
    ask1: str | None = None
    day_low: str | None = None
    day_high: str | None = None
    candle_id: str | None = None
    opening_session: bool | None = None
    market_session_active: bool | None = None
    drop_tick: bool = False

    def to_step_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "last": self.last,
            "bid1": self.bid1,
            "ask1": self.ask1,
            "day_low": self.day_low,
            "day_high": self.day_high,
            "candle_id": self.candle_id,
            "native_time": self.source_time_utc,
            "delivery_time": self.delivery_time_utc,
            "opening_session": self.opening_session,
            "market_session_active": self.market_session_active,
            "drop_tick": self.drop_tick,
            "fallback_book_to_last": False,
        }
        return payload


@dataclass
class FaultRule:
    """Declarative fault injection rule bound to a time window or trading event."""

    rule_id: str
    trigger_kind: TriggerKind
    fault_kind: ChannelFaultKind = ChannelFaultKind.NONE
    contracts: tuple[str, ...] = ()
    start_utc: str | None = None
    end_utc: str | None = None
    duration_seconds: int = 10
    delay_seconds: int = 0
    one_sided_side: str | None = None  # "LIMIT_UP" (no ask1) or "LIMIT_DOWN" (no bid1)
    # Broker / return-channel fault parameters
    broker_hold_facts: bool = False
    broker_release_reverse: bool = False
    broker_no_ack: bool = False
    broker_reject: bool = False
    broker_partial_fill_qty: int | None = None
    broker_late_fill_qty: int | None = None
    broker_replay_trade: bool = False
    required: bool = True


@dataclass
class TriggerRecord:
    """Tracks whether an expected fault trigger actually occurred (prevents false PASS)."""

    rule_id: str
    trigger_kind: str
    required: bool
    triggered: bool = False
    trigger_count: int = 0
    first_round_seq: int | None = None
    first_wall_time: str | None = None
    active_until_ns: int | None = None
    resolved: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "trigger_kind": self.trigger_kind,
            "required": self.required,
            "triggered": self.triggered,
            "trigger_count": self.trigger_count,
            "first_round_seq": self.first_round_seq,
            "first_wall_time": self.first_wall_time,
            "resolved": self.resolved,
        }


@dataclass
class ScenarioConfig:
    """Top-level configuration for a Market Scenario Engine run."""

    scenario_name: str
    fixture_db: Path
    contracts: tuple[str, ...] = ()
    start_utc: str | None = None
    end_utc: str | None = None
    bucket_seconds: int = 0
    max_rounds: int | None = None
    match_mode: BrokerMatchMode = BrokerMatchMode.BOOK_CONSTRAINED
    fault_rules: tuple[FaultRule, ...] = ()
    profile_record: dict[str, object] | None = None
    max_quote_age_seconds: int = 30
    order_volume: int = 1
    expected_targets: dict[str, dict[str, object]] = field(default_factory=dict)
    require_flat_convergence: bool = False
    drain_rounds: int = 5
    wall_timeout_seconds: float = 180.0
    seed: int = 0
    synthesis: Any = None
    export_generated_stream_jsonl: bool = False


@dataclass
class ScenarioResult:
    """Structured verdict and reproducible evidence bundle."""

    scenario_name: str
    verdict: Verdict
    engine_version: str
    dataset_sha256: str
    git_commit: str | None
    rounds_executed: int
    contracts: tuple[str, ...]
    trigger_coverage: list[dict[str, object]]
    uncovered_rules: list[str]
    failure: dict[str, object] | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    evidence_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "MARKET_SCENARIO_RESULT/1",
            "engine_version": self.engine_version,
            "scenario_name": self.scenario_name,
            "verdict": self.verdict.value,
            "dataset_sha256": self.dataset_sha256,
            "git_commit": self.git_commit,
            "rounds_executed": self.rounds_executed,
            "contracts": list(self.contracts),
            "trigger_coverage": self.trigger_coverage,
            "uncovered_rules": self.uncovered_rules,
            "failure": self.failure,
            "summary": self.summary,
            "evidence_path": self.evidence_path,
        }
