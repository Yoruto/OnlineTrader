"""Deterministic failure scenario shrinker (Delta Debugging / binary search).

Given a failing ScenarioConfig, systematically minimizes:
1. max_rounds (binary search for the shortest failing prefix),
2. fault_rules (drop non-essential fault rules while preserving the same failure),
3. contracts (drop unrelated contracts while preserving the same failure),
and exports a compact regression artifact for permanent replay.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from qa.scenario_engine.models import ScenarioConfig, ScenarioResult, Verdict
from qa.scenario_engine.runner import MarketScenarioRunner


def _failure_signature(result: ScenarioResult) -> tuple[str, str | None] | None:
    if result.verdict != Verdict.FAIL:
        return None
    failure = result.failure or {}
    inv = failure.get("invariant_id") or failure.get("reason")
    return (result.verdict.value, None if inv is None else str(inv))


class FailureShrinker:
    """Minimizes a failing ScenarioConfig while preserving its failure signature."""

    def __init__(self, *, work_dir: str | Path, max_attempts: int = 24) -> None:
        self.work_dir = Path(work_dir).resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max(4, int(max_attempts))
        self._attempt_seq = 0

    def _probe(self, config: ScenarioConfig, *, label: str) -> ScenarioResult:
        self._attempt_seq += 1
        case_dir = self.work_dir / f"attempt-{self._attempt_seq:03d}-{label}"
        return MarketScenarioRunner(config, work_dir=case_dir).run()

    def shrink(self, original_config: ScenarioConfig) -> dict[str, object]:
        """Run shrinking passes and write ``shrunk-repro.json``."""
        initial = self._probe(original_config, label="initial")
        target_sig = _failure_signature(initial)
        if target_sig is None:
            summary = {
                "schema_version": "MARKET_SCENARIO_SHRINK/1",
                "shrunk": False,
                "reason": f"Initial run did not fail (verdict={initial.verdict.value})",
                "initial_result": initial.to_dict(),
            }
            (self.work_dir / "shrunk-repro.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            return summary

        best_cfg = original_config
        best_res = initial

        # Pass 1: Binary search on max_rounds to find shortest failing prefix
        upper = best_res.rounds_executed
        lower = 2
        while lower < upper and self._attempt_seq < self.max_attempts:
            mid = (lower + upper) // 2
            cand_cfg = replace(best_cfg, max_rounds=mid, scenario_name=f"{original_config.scenario_name}-r{mid}")
            res = self._probe(cand_cfg, label=f"rounds-{mid}")
            if _failure_signature(res) == target_sig:
                best_cfg = cand_cfg
                best_res = res
                upper = mid
            else:
                lower = mid + 1

        # Pass 2: Drop non-essential fault rules one by one
        if len(best_cfg.fault_rules) > 1:
            kept = list(best_cfg.fault_rules)
            idx = 0
            while idx < len(kept) and self._attempt_seq < self.max_attempts:
                trial_rules = tuple(kept[:idx] + kept[idx + 1:])
                cand_cfg = replace(best_cfg, fault_rules=trial_rules)
                res = self._probe(cand_cfg, label=f"drop-rule-{idx}")
                if _failure_signature(res) == target_sig:
                    best_cfg = cand_cfg
                    best_res = res
                    kept = list(trial_rules)
                else:
                    idx += 1

        # Pass 3: Drop unrelated contracts one by one (keep at least 1 contract)
        if len(best_cfg.contracts) > 1:
            kept_c = list(best_cfg.contracts)
            idx = 0
            while idx < len(kept_c) and len(kept_c) > 1 and self._attempt_seq < self.max_attempts:
                trial_c = tuple(kept_c[:idx] + kept_c[idx + 1:])
                cand_cfg = replace(best_cfg, contracts=trial_c)
                res = self._probe(cand_cfg, label=f"drop-contract-{idx}")
                if _failure_signature(res) == target_sig:
                    best_cfg = cand_cfg
                    best_res = res
                    kept_c = list(trial_c)
                else:
                    idx += 1

        # Export final minimized reproduction bundle with generated stream
        final_cfg = replace(
            best_cfg,
            scenario_name=f"{original_config.scenario_name}-shrunk",
            export_generated_stream_jsonl=True,
        )
        final_res = self._probe(final_cfg, label="final")

        summary = {
            "schema_version": "MARKET_SCENARIO_SHRINK/1",
            "shrunk": True,
            "target_signature": list(target_sig),
            "attempts": self._attempt_seq,
            "original_rounds": initial.rounds_executed,
            "shrunk_rounds": final_res.rounds_executed,
            "original_rule_count": len(original_config.fault_rules),
            "shrunk_rule_count": len(final_cfg.fault_rules),
            "original_contracts": list(original_config.contracts),
            "shrunk_contracts": list(final_cfg.contracts),
            "shrunk_fault_rule_ids": [r.rule_id for r in final_cfg.fault_rules],
            "final_result": final_res.to_dict(),
        }
        (self.work_dir / "shrunk-repro.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return summary
