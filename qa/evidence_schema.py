"""Unified test & QA evidence bundle schema (PR5)."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

EVIDENCE_BUNDLE_SCHEMA = "ONLINE_TRADER_EVIDENCE_BUNDLE/1"
EVIDENCE_SOURCES = frozenset({
    "OFFLINE_UNITTEST",
    "OFFLINE_FAKE_BROKER",
    "REAL_RUNTIME_IPC",
    "SIMNOW_BROKER",
    "WINDOWS_PACKAGED_EXE",
})


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_unified_evidence_bundle(
    *,
    output_dir: Path,
    run_id: str,
    profile: str,
    evidence_source: str,
    candidate_commit: str,
    candidate_tree_sha256: str,
    candidate_identity_algorithm: str,
    final_candidate_commit: str,
    final_candidate_tree_sha256: str,
    candidate_changed_during_run: bool,
    environment: dict[str, object],
    runner_scope: dict[str, object],
    counts: dict[str, int],
    result: str,
    evidence_status: str,
    artifact_paths: dict[str, Path] | None = None,
    notes: str | None = None,
) -> dict[str, object]:
    """Build and validate a unified ONLINE_TRADER_EVIDENCE_BUNDLE/1 record."""
    if evidence_source not in EVIDENCE_SOURCES:
        raise ValueError(f"Unsupported evidence_source: {evidence_source!r}")
    artifacts: dict[str, dict[str, object]] = {}
    for label, path in sorted((artifact_paths or {}).items()):
        resolved = Path(path).resolve()
        if resolved.is_file():
            artifacts[label] = {
                "path": resolved.relative_to(output_dir.resolve()).as_posix()
                if resolved.is_relative_to(output_dir.resolve())
                else str(resolved),
                "sha256": file_sha256(resolved),
                "bytes": resolved.stat().st_size,
            }
    bundle: dict[str, object] = {
        "schema_version": EVIDENCE_BUNDLE_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "run_id": run_id,
        "profile": profile,
        "evidence_source": evidence_source,
        "evidence_status": evidence_status,
        "reported_result": result,
        "result": result,
        "candidate_commit": candidate_commit,
        "candidate_tree_sha256": candidate_tree_sha256,
        "candidate_identity_algorithm": candidate_identity_algorithm,
        "final_candidate_commit": final_candidate_commit,
        "final_candidate_tree_sha256": final_candidate_tree_sha256,
        "candidate_changed_during_run": bool(candidate_changed_during_run),
        "environment": environment,
        "runner_scope": runner_scope,
        "counts": {
            key: int(counts.get(key, 0))
            for key in ("PASS", "FAIL", "ERROR", "SKIP", "BLOCKED", "NOT_RUN")
        },
        "artifacts": artifacts,
    }
    if notes:
        bundle["notes"] = notes
    validate_evidence_bundle(bundle)
    return bundle


def validate_evidence_bundle(bundle: dict[str, object]) -> None:
    """Validate required fields on an ONLINE_TRADER_EVIDENCE_BUNDLE/1 object."""
    if bundle.get("schema_version") != EVIDENCE_BUNDLE_SCHEMA:
        raise ValueError("Invalid evidence bundle schema_version")
    if bundle.get("evidence_source") not in EVIDENCE_SOURCES:
        raise ValueError("Invalid evidence_source in evidence bundle")
    required = (
        "run_id",
        "profile",
        "evidence_status",
        "result",
        "candidate_commit",
        "candidate_tree_sha256",
        "counts",
        "runner_scope",
        "environment",
    )
    missing = [key for key in required if key not in bundle]
    if missing:
        raise ValueError(f"Evidence bundle missing required fields: {missing}")


def write_evidence_bundle(output_dir: Path, bundle: dict[str, object]) -> Path:
    validate_evidence_bundle(bundle)
    target = Path(output_dir) / "evidence-index.json"
    target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
