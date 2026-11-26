"""Synthetic market stream generator derived from real recorded tick deltas.

Implements:
- RAW_REPLAY: unaltered recorded ticks
- DERIVED_TRANSFORM: q'_t = q'_{t-1} + round(a * delta_q_t + b_t) in exact tick units
- SEGMENT_RECOMPOSE: stitches multiple real windows while preserving multi-contract
  correlation by default (or intentionally breaking it for risk-isolation tests)
- TARGETED: explicit directional/limit-lock/illiquidity stress waves

Strictly maintains tick-size alignment, limit bounds, running day_low/day_high,
nonnegative liquidity, and preserves missing bid1/ask1 as None.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import random

from online_trader.domain.values import decimal_text
from qa.market_data.schema import BEIJING_TZ, ns_to_utc_iso
from qa.scenario_engine.models import InstrumentSpec, MarketTickTruth
from qa.scenario_engine.replay_source import RecordedReplaySource


class GenerationMode(str, Enum):
    RAW_REPLAY = "RAW_REPLAY"
    DERIVED_TRANSFORM = "DERIVED_TRANSFORM"
    SEGMENT_RECOMPOSE = "SEGMENT_RECOMPOSE"
    TARGETED = "TARGETED"
    RANDOM_CONSTRAINED = "RANDOM_CONSTRAINED"


@dataclass(frozen=True)
class SegmentSlice:
    """One real time window selected from the recorded dataset."""

    start_utc: str
    end_utc: str
    volatility_scale: float = 1.0
    drift_ticks_per_step: float = 0.0
    liquidity_scale: float = 1.0
    force_limit_side: str | None = None  # None | "LIMIT_UP" | "LIMIT_DOWN"
    reverse_trend_at_ratio: float | None = None  # e.g. 0.5 flips drift sign halfway


@dataclass(frozen=True)
class SynthesisSpec:
    """Declarative specification for deriving synthetic market truth."""

    mode: GenerationMode = GenerationMode.RAW_REPLAY
    volatility_scale: float = 1.0
    drift_ticks_per_step: float = 0.0
    liquidity_scale: float = 1.0
    reverse_trend_at_ratio: float | None = None
    force_limit_side: str | None = None  # None | "LIMIT_UP" | "LIMIT_DOWN"
    limit_lock_after_ratio: float = 0.6
    break_correlation: bool = False
    segments: tuple[SegmentSlice, ...] = ()
    per_contract_drift: dict[str, float] = field(default_factory=dict)
    per_contract_vol_scale: dict[str, float] = field(default_factory=dict)
    seed: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "volatility_scale": self.volatility_scale,
            "drift_ticks_per_step": self.drift_ticks_per_step,
            "liquidity_scale": self.liquidity_scale,
            "reverse_trend_at_ratio": self.reverse_trend_at_ratio,
            "force_limit_side": self.force_limit_side,
            "limit_lock_after_ratio": self.limit_lock_after_ratio,
            "break_correlation": self.break_correlation,
            "segments": [
                {
                    "start_utc": s.start_utc,
                    "end_utc": s.end_utc,
                    "volatility_scale": s.volatility_scale,
                    "drift_ticks_per_step": s.drift_ticks_per_step,
                    "liquidity_scale": s.liquidity_scale,
                    "force_limit_side": s.force_limit_side,
                    "reverse_trend_at_ratio": s.reverse_trend_at_ratio,
                }
                for s in self.segments
            ],
            "per_contract_drift": dict(self.per_contract_drift),
            "per_contract_vol_scale": dict(self.per_contract_vol_scale),
            "seed": self.seed,
        }


def _to_grid(price: str, tick_size: Decimal) -> int:
    return int((Decimal(price) / tick_size).to_integral_value(rounding=ROUND_HALF_UP))


def _from_grid(q: int, tick_size: Decimal) -> str:
    return decimal_text(Decimal(q) * tick_size)


def _clamp_grid(q: int, *, lower_q: int | None, upper_q: int | None) -> int:
    if lower_q is not None and q < lower_q:
        q = lower_q
    if upper_q is not None and q > upper_q:
        q = upper_q
    return max(1, q)


def _scale_volume(vol: int | None, scale: float) -> int | None:
    if vol is None:
        return None
    if scale <= 0.0:
        return 0
    return max(0, int(round(vol * scale)))


class MarketSynthesizer:
    """Transforms recorded market truth into consistent synthetic truth streams."""

    def __init__(self, source: RecordedReplaySource) -> None:
        self.source = source

    def _transform_stream(
        self,
        raw_rounds: list[tuple[int, str, dict[str, MarketTickTruth]]],
        *,
        contracts: tuple[str, ...],
        spec: SynthesisSpec,
        anchor_start_ns: int | None = None,
    ) -> list[tuple[int, str, dict[str, MarketTickTruth]]]:
        if not raw_rounds:
            return []

        rng = random.Random(spec.seed)
        total = len(raw_rounds)
        instruments: dict[str, InstrumentSpec] = {c: self.source.instruments[c] for c in contracts}
        prev_raw_q: dict[str, int] = {}
        cur_syn_q: dict[str, int] = {}
        drift_accum: dict[str, float] = {c: 0.0 for c in contracts}
        running_low_q: dict[str, int] = {}
        running_high_q: dict[str, int] = {}

        # Optional independent signs when break_correlation=True
        contract_sign: dict[str, int] = {}
        for idx, sym in enumerate(contracts):
            if spec.break_correlation:
                contract_sign[sym] = 1 if (idx % 2 == 0) else -1
            else:
                contract_sign[sym] = 1

        time_shift_ns = 0
        if anchor_start_ns is not None:
            time_shift_ns = anchor_start_ns - raw_rounds[0][0]

        out: list[tuple[int, str, dict[str, MarketTickTruth]]] = []

        for step_idx, (raw_ns, _raw_utc, truth_map) in enumerate(raw_rounds):
            ratio = step_idx / max(1, total - 1)
            target_ns = raw_ns + time_shift_ns
            target_utc = ns_to_utc_iso(target_ns)
            dt_cst = (
                datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=target_ns // 1_000)
            ).astimezone(BEIJING_TZ)
            candle_id = f"bar-{dt_cst.strftime('%H%M')}"

            new_map: dict[str, MarketTickTruth] = {}
            for sym, truth in truth_map.items():
                inst = instruments[sym]
                tick_size = inst.price_tick
                lower_str = truth.lower_limit or inst.lower_limit
                upper_str = truth.upper_limit or inst.upper_limit
                lower_q = _to_grid(lower_str, tick_size) if lower_str is not None else None
                upper_q = _to_grid(upper_str, tick_size) if upper_str is not None else None

                raw_q = _to_grid(truth.last, tick_size)
                if sym not in prev_raw_q:
                    prev_raw_q[sym] = raw_q
                    cur_syn_q[sym] = _clamp_grid(raw_q, lower_q=lower_q, upper_q=upper_q)
                    delta_q = 0
                else:
                    delta_q = raw_q - prev_raw_q[sym]
                    prev_raw_q[sym] = raw_q

                a = spec.per_contract_vol_scale.get(sym, spec.volatility_scale)
                b = spec.per_contract_drift.get(sym, spec.drift_ticks_per_step) * contract_sign[sym]
                if spec.reverse_trend_at_ratio is not None and ratio >= spec.reverse_trend_at_ratio:
                    b = -b
                if spec.break_correlation and spec.mode == GenerationMode.RANDOM_CONSTRAINED:
                    # Slight independent jitter when correlation is intentionally broken
                    b += rng.uniform(-0.15, 0.15)

                # Accumulate fractional drift so small per-step drift still produces exact tick steps
                drift_accum[sym] += b
                whole_drift = int(round(drift_accum[sym]))
                drift_accum[sym] -= whole_drift

                step_ticks = int(round(a * delta_q)) + whole_drift
                next_q = _clamp_grid(cur_syn_q[sym] + step_ticks, lower_q=lower_q, upper_q=upper_q)

                # Targeted limit-lock phase
                limit_locked_up = False
                limit_locked_down = False
                if spec.force_limit_side == "LIMIT_UP" and ratio >= spec.limit_lock_after_ratio and upper_q is not None:
                    next_q = upper_q
                    limit_locked_up = True
                elif spec.force_limit_side == "LIMIT_DOWN" and ratio >= spec.limit_lock_after_ratio and lower_q is not None:
                    next_q = lower_q
                    limit_locked_down = True

                cur_syn_q[sym] = next_q
                running_low_q[sym] = min(running_low_q[sym], next_q) if sym in running_low_q else next_q
                running_high_q[sym] = max(running_high_q[sym], next_q) if sym in running_high_q else next_q

                # Preserve original bid1/ask1 distance from last; NEVER fabricate missing book levels
                bid1_str: str | None = None
                ask1_str: str | None = None
                bid_vol: int | None = None
                ask_vol: int | None = None

                if limit_locked_up:
                    bid1_str = _from_grid(next_q, tick_size)
                    bid_vol = _scale_volume(truth.bid_volume1 or 20, spec.liquidity_scale)
                    ask1_str = None
                    ask_vol = None
                elif limit_locked_down:
                    bid1_str = None
                    bid_vol = None
                    ask1_str = _from_grid(next_q, tick_size)
                    ask_vol = _scale_volume(truth.ask_volume1 or 20, spec.liquidity_scale)
                else:
                    if truth.bid1 is not None:
                        bid_offset = _to_grid(truth.bid1, tick_size) - raw_q
                        bid_q = _clamp_grid(next_q + min(0, bid_offset), lower_q=lower_q, upper_q=upper_q)
                        bid1_str = _from_grid(bid_q, tick_size)
                        bid_vol = _scale_volume(truth.bid_volume1, spec.liquidity_scale)
                    if truth.ask1 is not None:
                        ask_offset = _to_grid(truth.ask1, tick_size) - raw_q
                        ask_q = _clamp_grid(next_q + max(0, ask_offset), lower_q=lower_q, upper_q=upper_q)
                        ask1_str = _from_grid(ask_q, tick_size)
                        ask_vol = _scale_volume(truth.ask_volume1, spec.liquidity_scale)
                    # If synthetic price hits exchange limit naturally, drop the blocked side
                    if upper_q is not None and next_q >= upper_q:
                        ask1_str = None
                        ask_vol = None
                    if lower_q is not None and next_q <= lower_q:
                        bid1_str = None
                        bid_vol = None

                win = self.source.session_for(sym, target_ns)
                new_map[sym] = MarketTickTruth(
                    symbol=sym,
                    trading_day=truth.trading_day,
                    source_time_ns=target_ns,
                    source_time_utc=target_utc,
                    last=_from_grid(next_q, tick_size),
                    bid1=bid1_str,
                    bid_volume1=bid_vol,
                    ask1=ask1_str,
                    ask_volume1=ask_vol,
                    day_low=_from_grid(running_low_q[sym], tick_size),
                    day_high=_from_grid(running_high_q[sym], tick_size),
                    lower_limit=lower_str,
                    upper_limit=upper_str,
                    candle_id=candle_id,
                    opening_session=win.is_opening_session if win is not None else truth.opening_session,
                    market_session_active=(
                        win is not None if self.source.sessions_by_symbol.get(sym) else truth.market_session_active
                    ),
                    session_open_utc=win.open_utc if win is not None else truth.session_open_utc,
                    session_close_utc=win.close_utc if win is not None else truth.session_close_utc,
                )
            out.append((target_ns, target_utc, new_map))

        return out

    def generate_rounds(
        self,
        *,
        contracts: tuple[str, ...],
        spec: SynthesisSpec,
        start_utc: str | None = None,
        end_utc: str | None = None,
        bucket_seconds: int = 0,
        max_rounds: int | None = None,
    ) -> Iterator[tuple[int, str, dict[str, MarketTickTruth]]]:
        """Generate synthetic rounds matching RecordedReplaySource.iter_rounds signature."""
        if spec.mode == GenerationMode.RAW_REPLAY:
            yield from self.source.iter_rounds(
                contracts=contracts,
                start_utc=start_utc,
                end_utc=end_utc,
                bucket_seconds=bucket_seconds,
                max_rounds=max_rounds,
            )
            return

        if spec.mode == GenerationMode.SEGMENT_RECOMPOSE and spec.segments:
            stitched: list[tuple[int, str, dict[str, MarketTickTruth]]] = []
            cursor_ns: int | None = None
            for seg_idx, seg in enumerate(spec.segments):
                seg_raw = list(
                    self.source.iter_rounds(
                        contracts=contracts,
                        start_utc=seg.start_utc,
                        end_utc=seg.end_utc,
                        bucket_seconds=bucket_seconds,
                    )
                )
                if not seg_raw:
                    continue
                if cursor_ns is None:
                    cursor_ns = seg_raw[0][0]
                else:
                    cursor_ns += max(1, bucket_seconds) * 1_000_000_000
                sub_spec = SynthesisSpec(
                    mode=GenerationMode.DERIVED_TRANSFORM,
                    volatility_scale=seg.volatility_scale,
                    drift_ticks_per_step=seg.drift_ticks_per_step,
                    liquidity_scale=seg.liquidity_scale,
                    reverse_trend_at_ratio=seg.reverse_trend_at_ratio,
                    force_limit_side=seg.force_limit_side,
                    break_correlation=spec.break_correlation,
                    seed=spec.seed + seg_idx,
                )
                transformed = self._transform_stream(
                    seg_raw,
                    contracts=contracts,
                    spec=sub_spec,
                    anchor_start_ns=cursor_ns,
                )
                if transformed:
                    stitched.extend(transformed)
                    cursor_ns = transformed[-1][0]
                if max_rounds is not None and len(stitched) >= max_rounds:
                    stitched = stitched[:max_rounds]
                    break
            for item in stitched:
                yield item
            return

        raw_rounds = list(
            self.source.iter_rounds(
                contracts=contracts,
                start_utc=start_utc,
                end_utc=end_utc,
                bucket_seconds=bucket_seconds,
                max_rounds=max_rounds,
            )
        )
        for item in self._transform_stream(raw_rounds, contracts=contracts, spec=spec):
            yield item
