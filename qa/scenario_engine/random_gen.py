"""Constrained seeded random scenario generator for Market Scenario Engine (Phase 2).

Generates reproducible ScenarioConfig instances across LIGHT / MEDIUM / HEAVY
severity levels while respecting domain constraints (valid trading sessions,
no impossible fault combinations, explicit coverage rules).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import random

from qa.market_data.schema import ns_to_utc_iso
from qa.scenario_engine.models import (
    BrokerMatchMode,
    ChannelFaultKind,
    FaultRule,
    ScenarioConfig,
    TriggerKind,
)
from qa.scenario_engine.replay_source import RecordedReplaySource
from qa.scenario_engine.segment_index import SegmentIndex
from qa.scenario_engine.synthesizer import GenerationMode, SegmentSlice, SynthesisSpec


class FaultSeverity(str, Enum):
    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"


class ConstrainedRandomGenerator:
    """Produces reproducible, domain-constrained ScenarioConfig batches from a seed."""

    def __init__(self, fixture_db: str | Path, *, seed: int = 42) -> None:
        self.fixture_db = Path(fixture_db).resolve()
        self.source = RecordedReplaySource(self.fixture_db)
        self.seed = int(seed)
        self._index: SegmentIndex | None = None

    def _ensure_index(self, contracts: tuple[str, ...]) -> SegmentIndex:
        if self._index is None:
            idx = SegmentIndex(self.source)
            idx.build(contracts=contracts, window_minutes=(5, 15))
            self._index = idx
        return self._index

    def generate_batch(
        self,
        *,
        count: int = 10,
        severity: FaultSeverity = FaultSeverity.MEDIUM,
        contracts: tuple[str, ...] | None = None,
        bucket_seconds: int = 1,
        max_rounds: int = 300,
    ) -> list[ScenarioConfig]:
        selected = tuple(contracts) if contracts else self.source.available_contracts
        index = self._ensure_index(selected)
        anchor_sym = selected[0]
        candidates = index.query(symbol=anchor_sym, window_minutes=15) or index.query(symbol=anchor_sym, window_minutes=5)
        if not candidates:
            raise RuntimeError(f"No candidate segments indexed for {anchor_sym} in {self.fixture_db}")

        rng = random.Random(self.seed)
        configs: list[ScenarioConfig] = []

        for i in range(1, count + 1):
            case_seed = rng.randint(1, 2**31 - 1)
            case_rng = random.Random(case_seed)
            seg = case_rng.choice(candidates)
            effective_end_ns = seg.end_ns
            if max_rounds is not None and max_rounds > 4:
                cap_span_ns = max(10, (max_rounds - 4) * max(1, bucket_seconds)) * 1_000_000_000
                effective_end_ns = min(seg.end_ns, seg.start_ns + cap_span_ns)
            seg_mid_ns = (seg.start_ns + effective_end_ns) // 2
            fault_start_utc = ns_to_utc_iso(seg_mid_ns)
            fault_end_utc = ns_to_utc_iso(min(effective_end_ns, seg_mid_ns + case_rng.randint(8, 25) * 1_000_000_000))

            rules: list[FaultRule] = []
            match_mode = BrokerMatchMode.BOOK_CONSTRAINED
            synthesis: SynthesisSpec

            if severity == FaultSeverity.LIGHT:
                vol_scale = round(case_rng.uniform(0.8, 1.5), 2)
                drift = round(case_rng.uniform(-0.15, 0.15), 3)
                synthesis = SynthesisSpec(
                    mode=GenerationMode.DERIVED_TRANSFORM,
                    volatility_scale=vol_scale,
                    drift_ticks_per_step=drift,
                    seed=case_seed,
                )
                fault_kind = case_rng.choice([
                    ChannelFaultKind.DELAY,
                    ChannelFaultKind.DROP,
                    ChannelFaultKind.FREEZE,
                    ChannelFaultKind.BURST,
                ])
                rules.append(
                    FaultRule(
                        rule_id=f"GEN_{i:03d}_CHANNEL_{fault_kind.value}",
                        trigger_kind=TriggerKind.TIME_RANGE,
                        fault_kind=fault_kind,
                        contracts=(case_rng.choice(selected),),
                        start_utc=fault_start_utc,
                        end_utc=fault_end_utc,
                        delay_seconds=case_rng.randint(5, 20) if fault_kind == ChannelFaultKind.DELAY else 0,
                        required=True,
                    )
                )

            elif severity == FaultSeverity.MEDIUM:
                match_mode = case_rng.choice([BrokerMatchMode.BOOK_CONSTRAINED, BrokerMatchMode.CONSERVATIVE, BrokerMatchMode.FAULTY])
                vol_scale = round(case_rng.uniform(1.2, 2.2), 2)
                drift = round(case_rng.choice([-1, 1]) * case_rng.uniform(0.1, 0.35), 3)
                synthesis = SynthesisSpec(
                    mode=GenerationMode.DERIVED_TRANSFORM,
                    volatility_scale=vol_scale,
                    drift_ticks_per_step=drift,
                    reverse_trend_at_ratio=round(case_rng.uniform(0.45, 0.65), 2),
                    liquidity_scale=round(case_rng.uniform(0.4, 1.0), 2),
                    seed=case_seed,
                )
                rules.append(
                    FaultRule(
                        rule_id=f"GEN_{i:03d}_TIME_OUTAGE",
                        trigger_kind=TriggerKind.TIME_RANGE,
                        fault_kind=case_rng.choice([ChannelFaultKind.BURST, ChannelFaultKind.DELAY, ChannelFaultKind.DROP]),
                        contracts=(case_rng.choice(selected),),
                        start_utc=fault_start_utc,
                        end_utc=fault_end_utc,
                        delay_seconds=12,
                        required=True,
                    )
                )
                rules.append(
                    FaultRule(
                        rule_id=f"GEN_{i:03d}_ON_FIRST_OPEN",
                        trigger_kind=TriggerKind.FIRST_OPEN_ORDER,
                        fault_kind=ChannelFaultKind.NONE,
                        duration_seconds=15,
                        broker_partial_fill_qty=2 if match_mode == BrokerMatchMode.FAULTY else None,
                        broker_hold_facts=(match_mode == BrokerMatchMode.FAULTY),
                        required=False,
                    )
                )

            else:  # HEAVY
                match_mode = BrokerMatchMode.FAULTY
                seg2 = case_rng.choice(candidates)
                limit_side = case_rng.choice([None, "LIMIT_UP", "LIMIT_DOWN"])
                synthesis = SynthesisSpec(
                    mode=GenerationMode.SEGMENT_RECOMPOSE,
                    break_correlation=case_rng.choice([False, True]),
                    segments=(
                        SegmentSlice(
                            start_utc=seg.start_utc,
                            end_utc=seg.end_utc,
                            volatility_scale=round(case_rng.uniform(1.5, 3.0), 2),
                            drift_ticks_per_step=round(case_rng.choice([-1, 1]) * case_rng.uniform(0.2, 0.5), 3),
                            liquidity_scale=round(case_rng.uniform(0.2, 0.7), 2),
                            reverse_trend_at_ratio=0.55,
                        ),
                        SegmentSlice(
                            start_utc=seg2.start_utc,
                            end_utc=seg2.end_utc,
                            volatility_scale=round(case_rng.uniform(1.5, 2.8), 2),
                            drift_ticks_per_step=round(case_rng.choice([-1, 1]) * case_rng.uniform(0.2, 0.5), 3),
                            liquidity_scale=round(case_rng.uniform(0.15, 0.6), 2),
                            force_limit_side=limit_side,
                        ),
                    ),
                    seed=case_seed,
                )
                rules.extend([
                    FaultRule(
                        rule_id=f"GEN_{i:03d}_HEAVY_BURST",
                        trigger_kind=TriggerKind.TIME_RANGE,
                        fault_kind=ChannelFaultKind.BURST,
                        contracts=selected[:2],
                        start_utc=fault_start_utc,
                        end_utc=fault_end_utc,
                        broker_hold_facts=True,
                        broker_release_reverse=True,
                        required=True,
                    ),
                    FaultRule(
                        rule_id=f"GEN_{i:03d}_HEAVY_PARTIAL",
                        trigger_kind=TriggerKind.FIRST_OPEN_ORDER,
                        fault_kind=ChannelFaultKind.NONE,
                        duration_seconds=20,
                        broker_partial_fill_qty=2,
                        required=False,
                    ),
                    FaultRule(
                        rule_id=f"GEN_{i:03d}_HEAVY_CANCEL_RACE",
                        trigger_kind=TriggerKind.CANCEL_REQUESTED,
                        fault_kind=ChannelFaultKind.NONE,
                        duration_seconds=10,
                        broker_late_fill_qty=1,
                        broker_replay_trade=True,
                        required=False,
                    ),
                    FaultRule(
                        rule_id=f"GEN_{i:03d}_HEAVY_RECOVERY_REORDER",
                        trigger_kind=TriggerKind.FEED_RECOVERED,
                        fault_kind=ChannelFaultKind.REORDER_DUPLICATE,
                        duration_seconds=6,
                        required=True,
                    ),
                ])

            configs.append(
                ScenarioConfig(
                    scenario_name=f"GEN-{i:03d}-{severity.value.lower()}-seed-{case_seed}",
                    fixture_db=self.fixture_db,
                    contracts=selected,
                    start_utc=seg.start_utc,
                    end_utc=seg.end_utc,
                    bucket_seconds=bucket_seconds,
                    max_rounds=max_rounds,
                    match_mode=match_mode,
                    fault_rules=tuple(rules),
                    order_volume=5 if severity == FaultSeverity.HEAVY else 3,
                    max_quote_age_seconds=30,
                    seed=case_seed,
                    synthesis=synthesis,
                )
            )
        return configs
