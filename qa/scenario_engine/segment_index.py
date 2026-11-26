"""Lightweight statistical segment indexer over recorded MARKET_REPLAY_FIXTURE/1 SQLite data.

Classifies 1m / 5m / 15m / 30m windows into transparent rule-based market regimes
without machine learning or external dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import json
import math
from pathlib import Path
from statistics import median
from typing import Any

from qa.market_data.schema import ns_to_utc_iso
from qa.scenario_engine.replay_source import RecordedReplaySource


class SegmentRegime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    HIGH_FREQ_CHOP = "HIGH_FREQ_CHOP"
    LOW_VOL_SIDEWAYS = "LOW_VOL_SIDEWAYS"
    FAST_REVERSAL = "FAST_REVERSAL"
    GAP_JUMP = "GAP_JUMP"


@dataclass(frozen=True)
class SegmentFeature:
    symbol: str
    trading_day: str
    window_minutes: int
    start_ns: int
    end_ns: int
    start_utc: str
    end_utc: str
    tick_count: int
    ticks_per_minute: float
    net_change_ticks: int
    range_ticks: int
    max_step_ticks: int
    volatility_ticks: float
    trend_persistence: float
    median_spread_ticks: float | None
    max_gap_ms: int
    volume_delta: int | None
    regime: SegmentRegime

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "trading_day": self.trading_day,
            "window_minutes": self.window_minutes,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "tick_count": self.tick_count,
            "ticks_per_minute": round(self.ticks_per_minute, 3),
            "net_change_ticks": self.net_change_ticks,
            "range_ticks": self.range_ticks,
            "max_step_ticks": self.max_step_ticks,
            "volatility_ticks": round(self.volatility_ticks, 4),
            "trend_persistence": round(self.trend_persistence, 4),
            "median_spread_ticks": (
                None if self.median_spread_ticks is None else round(self.median_spread_ticks, 3)
            ),
            "max_gap_ms": self.max_gap_ms,
            "volume_delta": self.volume_delta,
            "regime": self.regime.value,
        }


def _classify_regime(
    *,
    net_change_ticks: int,
    range_ticks: int,
    max_step_ticks: int,
    trend_persistence: float,
    ticks_per_minute: float,
    reversal_ratio: float,
) -> SegmentRegime:
    if max_step_ticks >= max(6, int(range_ticks * 0.45)) and range_ticks >= 8:
        return SegmentRegime.GAP_JUMP
    if range_ticks >= 8 and reversal_ratio >= 0.65 and abs(net_change_ticks) <= max(2, int(range_ticks * 0.25)):
        return SegmentRegime.FAST_REVERSAL
    if trend_persistence >= 0.35 and abs(net_change_ticks) >= max(4, int(range_ticks * 0.55)):
        return SegmentRegime.TREND_UP if net_change_ticks > 0 else SegmentRegime.TREND_DOWN
    if range_ticks <= 4 and abs(net_change_ticks) <= 2:
        return SegmentRegime.LOW_VOL_SIDEWAYS
    if ticks_per_minute >= 30.0 and range_ticks >= 4:
        return SegmentRegime.HIGH_FREQ_CHOP
    return SegmentRegime.TREND_UP if net_change_ticks > 0 else (
        SegmentRegime.TREND_DOWN if net_change_ticks < 0 else SegmentRegime.LOW_VOL_SIDEWAYS
    )


class SegmentIndex:
    """Builds and queries a lightweight index of candidate real-market windows."""

    DEFAULT_WINDOWS: tuple[int, ...] = (1, 5, 15, 30)

    def __init__(self, source: RecordedReplaySource) -> None:
        self.source = source
        self.segments: list[SegmentFeature] = []

    def build(
        self,
        *,
        contracts: tuple[str, ...] | None = None,
        window_minutes: tuple[int, ...] = DEFAULT_WINDOWS,
        min_ticks: int = 10,
    ) -> list[SegmentFeature]:
        selected = tuple(contracts) if contracts else self.source.available_contracts
        conn = self.source._connect_ro()
        results: list[SegmentFeature] = []
        try:
            for sym in selected:
                spec = self.source.instruments[sym]
                tick_size = spec.price_tick
                rows = conn.execute(
                    "SELECT source_timestamp_ns, last_price, bid_price1, ask_price1, volume "
                    "FROM market_ticks WHERE symbol = ? AND last_price IS NOT NULL "
                    "ORDER BY source_timestamp_ns ASC, sequence ASC",
                    (sym,),
                ).fetchall()
                if len(rows) < min_ticks:
                    continue

                for win_min in window_minutes:
                    win_ns = int(win_min) * 60 * 1_000_000_000
                    buckets: dict[int, list[tuple[int, Decimal, Decimal | None, Decimal | None, int | None]]] = {}
                    for ts_ns, last_s, bid1_s, ask1_s, vol in rows:
                        b_start = (int(ts_ns) // win_ns) * win_ns
                        buckets.setdefault(b_start, []).append((
                            int(ts_ns),
                            Decimal(str(last_s)),
                            Decimal(str(bid1_s)) if bid1_s is not None else None,
                            Decimal(str(ask1_s)) if ask1_s is not None else None,
                            int(vol) if vol is not None else None,
                        ))

                    for b_start in sorted(buckets):
                        items = buckets[b_start]
                        if len(items) < min_ticks:
                            continue
                        grid = [int((item[1] / tick_size).to_integral_value()) for item in items]
                        deltas = [grid[i] - grid[i - 1] for i in range(1, len(grid))]
                        net_ticks = grid[-1] - grid[0]
                        high_q = max(grid)
                        low_q = min(grid)
                        range_ticks = high_q - low_q
                        abs_path = sum(abs(d) for d in deltas)
                        persistence = abs(net_ticks) / abs_path if abs_path > 0 else 0.0
                        max_step = max((abs(d) for d in deltas), default=0)

                        # Reversal ratio: how far price moved to an extremum and retraced back
                        peak_disp = max(abs(high_q - grid[0]), abs(grid[0] - low_q))
                        retrace = max(abs(high_q - grid[-1]), abs(grid[-1] - low_q))
                        reversal_ratio = (min(peak_disp, retrace) / range_ticks) if range_ticks > 0 else 0.0

                        mean_d = sum(deltas) / len(deltas) if deltas else 0.0
                        var_d = (
                            sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
                            if deltas
                            else 0.0
                        )
                        vol_ticks = math.sqrt(var_d)

                        spreads = [
                            float((item[3] - item[2]) / tick_size)
                            for item in items
                            if item[2] is not None and item[3] is not None and item[3] >= item[2]
                        ]
                        med_spread = median(spreads) if spreads else None

                        gaps_ms = [
                            max(0, (items[i][0] - items[i - 1][0]) // 1_000_000)
                            for i in range(1, len(items))
                        ]
                        max_gap_ms = max(gaps_ms, default=0)

                        vols = [item[4] for item in items if item[4] is not None]
                        vol_delta = max(0, vols[-1] - vols[0]) if len(vols) >= 2 else None

                        span_min = max(1e-6, (items[-1][0] - items[0][0]) / 60_000_000_000)
                        tpm = len(items) / span_min

                        regime = _classify_regime(
                            net_change_ticks=net_ticks,
                            range_ticks=range_ticks,
                            max_step_ticks=max_step,
                            trend_persistence=persistence,
                            ticks_per_minute=tpm,
                            reversal_ratio=reversal_ratio,
                        )
                        results.append(
                            SegmentFeature(
                                symbol=sym,
                                trading_day=spec.trading_day,
                                window_minutes=int(win_min),
                                start_ns=items[0][0],
                                end_ns=items[-1][0],
                                start_utc=ns_to_utc_iso(items[0][0]),
                                end_utc=ns_to_utc_iso(items[-1][0]),
                                tick_count=len(items),
                                ticks_per_minute=tpm,
                                net_change_ticks=net_ticks,
                                range_ticks=range_ticks,
                                max_step_ticks=max_step,
                                volatility_ticks=vol_ticks,
                                trend_persistence=persistence,
                                median_spread_ticks=med_spread,
                                max_gap_ms=max_gap_ms,
                                volume_delta=vol_delta,
                                regime=regime,
                            )
                        )
        finally:
            conn.close()
        self.segments = results
        return results

    def query(
        self,
        *,
        symbol: str | None = None,
        window_minutes: int | None = None,
        regime: SegmentRegime | None = None,
    ) -> list[SegmentFeature]:
        out: list[SegmentFeature] = []
        for seg in self.segments:
            if symbol is not None and seg.symbol != symbol:
                continue
            if window_minutes is not None and seg.window_minutes != window_minutes:
                continue
            if regime is not None and seg.regime != regime:
                continue
            out.append(seg)
        return out

    def save_json(self, path: str | Path) -> Path:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "schema_version": "MARKET_SEGMENT_INDEX/1",
            "dataset_sha256": self.source.sha256,
            "trading_day": self.source.trading_day,
            "segment_count": len(self.segments),
            "segments": [s.to_dict() for s in self.segments],
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target
