"""Multi-dataset market corpus manager and cross-version performance comparator (Phase 4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qa.scenario_engine.monitor import DegradationThresholds, compare_with_baseline
from qa.scenario_engine.replay_source import RecordedReplaySource
from qa.scenario_engine.segment_index import SegmentFeature, SegmentIndex, SegmentRegime


class MarketCorpus:
    """Discovers and indexes all recorded MARKET_REPLAY_FIXTURE/1 databases in a corpus root."""

    def __init__(self, corpus_root: str | Path) -> None:
        self.corpus_root = Path(corpus_root).resolve()
        self.databases: list[Path] = sorted(self.corpus_root.rglob("market_fixture.sqlite3"))
        self.sources: list[RecordedReplaySource] = [RecordedReplaySource(p) for p in self.databases]
        self.segments: list[tuple[Path, SegmentFeature]] = []

    def summary(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for src in self.sources:
            rows.append({
                "db_path": str(src.db_path),
                "trading_day": src.trading_day,
                "coverage_scope": src.metadata.get("coverage_scope", "UNKNOWN"),
                "contracts": list(src.available_contracts),
                "sha256": src.sha256,
            })
        return rows

    def build_unified_index(
        self,
        *,
        window_minutes: tuple[int, ...] = (5, 15),
        min_ticks: int = 10,
    ) -> list[tuple[Path, SegmentFeature]]:
        """Build a unified segment library across all discovered recorded fixtures."""
        unified: list[tuple[Path, SegmentFeature]] = []
        for src in self.sources:
            idx = SegmentIndex(src)
            segs = idx.build(window_minutes=window_minutes, min_ticks=min_ticks)
            for seg in segs:
                unified.append((src.db_path, seg))
        self.segments = unified
        return unified

    def query(
        self,
        *,
        symbol: str | None = None,
        window_minutes: int | None = None,
        regime: SegmentRegime | None = None,
    ) -> list[tuple[Path, SegmentFeature]]:
        out: list[tuple[Path, SegmentFeature]] = []
        for db_path, seg in self.segments:
            if symbol is not None and seg.symbol != symbol:
                continue
            if window_minutes is not None and seg.window_minutes != window_minutes:
                continue
            if regime is not None and seg.regime != regime:
                continue
            out.append((db_path, seg))
        return out


def compare_version_stress_reports(
    report_paths: list[str | Path],
    *,
    output_dir: str | Path | None = None,
    thresholds: DegradationThresholds | None = None,
) -> dict[str, object]:
    """Compare a chronological series of stress-report.json files across software commits."""
    if not report_paths:
        raise ValueError("report_paths must be non-empty")

    loaded: list[dict[str, Any]] = []
    for p in report_paths:
        path = Path(p).resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        loaded.append({"path": str(path), "data": data})

    baseline_entry = loaded[0]
    baseline_perf = baseline_entry["data"].get("performance") or {}
    rows: list[dict[str, object]] = []

    for idx, entry in enumerate(loaded):
        d = entry["data"]
        perf = d.get("performance") or {}
        diff = (
            {"degraded": False, "regressions": []}
            if idx == 0
            else compare_with_baseline(perf, baseline_perf, thresholds=thresholds)
        )
        rows.append({
            "index": idx,
            "report_path": entry["path"],
            "git_commit": d.get("git_commit"),
            "scenario_name": d.get("scenario_name"),
            "verdict": d.get("verdict"),
            "ipc_p95_ms": (perf.get("ipc_rtt_ms") or {}).get("p95"),
            "frame_apply_p95_ms": (perf.get("frame_apply_ms") or {}).get("p95"),
            "ui_lag_p95_ms": (perf.get("ui_event_loop_lag_ms") or {}).get("p95"),
            "owner_mem_max_mib": (perf.get("owner_memory_mib") or {}).get("max"),
            "degraded_vs_first": diff["degraded"],
            "regressions": diff["regressions"],
        })

    comparison = {
        "schema_version": "MARKET_VERSION_PERF_COMPARISON/1",
        "baseline_commit": baseline_entry["data"].get("git_commit"),
        "baseline_report": baseline_entry["path"],
        "versions_compared": len(rows),
        "rows": rows,
    }

    if output_dir is not None:
        out_root = Path(output_dir).resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / "version-perf-comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        md_lines = [
            "# Historical Version Performance Comparison",
            "",
            "| # | Commit | Verdict | IPC P95 (ms) | Frame Apply P95 (ms) | UI Lag P95 (ms) | Max Mem (MiB) | Degraded |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
        for r in rows:
            sha_short = str(r["git_commit"] or "-")[:10]
            md_lines.append(
                f"| {r['index']} | `{sha_short}` | {r['verdict']} | {r['ipc_p95_ms']} | "
                f"{r['frame_apply_p95_ms']} | {r['ui_lag_p95_ms']} | {r['owner_mem_max_mib']} | "
                f"{'YES' if r['degraded_vs_first'] else 'NO'} |"
            )
        (out_root / "version-perf-comparison.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    return comparison
