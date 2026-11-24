"""Freeze diagnostic evidence, build a bounded ZIP, and perform one best-effort upload."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from online_trader import __version__
from online_trader.configuration.files import publish_new_bytes, publish_replace_bytes
from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.diagnostics.backend import (
    JiHuLabBackend,
    UploadResult,
    failed_upload_result,
    load_upload_config,
)
from online_trader.diagnostics.contract import utc_now


REQUEST_SCHEMA = "ONLINE_TRADER_DIAGNOSTIC_REPORT_REQUEST/1"
MANIFEST_SCHEMA = "ONLINE_TRADER_DIAGNOSTIC_MANIFEST/1"
_MAX_SOURCE_FILE = 8 * 1024 * 1024
_MAX_BUNDLE_INPUT = 32 * 1024 * 1024
_MAX_LOCAL_BYTES = 500 * 1024 * 1024


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _build_manifest_hash(runtime_root: Path) -> str | None:
    for candidate in (runtime_root / "base-build-manifest.json", runtime_root.parent / "base-build-manifest.json"):
        try:
            if candidate.is_file() and candidate.stat().st_size <= 2 * 1024 * 1024:
                return _sha256(candidate)
        except OSError:
            continue
    return None


def create_report_request(
    runtime_root,
    *,
    report_reason: str,
    runtime_boot_id: str | None,
    clean_shutdown: bool,
    runtime_process_instance_id: str | None,
) -> Path:
    diagnostics = Path(runtime_root).resolve() / "diagnostics"
    report_id = "report-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:16]
    request = {
        "schema_version": REQUEST_SCHEMA,
        "report_id": report_id,
        "report_reason": report_reason,
        "requested_at_utc": utc_now(),
        "runtime_boot_id": runtime_boot_id,
        "runtime_process_instance_id": runtime_process_instance_id,
        "clean_shutdown": bool(clean_shutdown),
        "diagnostic_protocol": "1",
    }
    return publish_new_bytes(
        canonical_bytes(request), diagnostics / "report-requests" / f"{report_id}.json"
    )


def launch_reporter(runtime_root, request_path) -> bool:
    """Start a detached, bounded Reporter without delaying Runtime shutdown."""
    root = str(Path(runtime_root).resolve())
    request = str(Path(request_path).resolve())
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--diagnostic-reporter", request, "--diagnostic-runtime-root", root]
    else:
        command = [
            sys.executable,
            "-m",
            "online_trader.diagnostics.reporter",
            "--request",
            request,
            "--runtime-root",
            root,
        ]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        return True
    except OSError:
        return False


def _read_request(path: Path) -> dict:
    value = strict_loads(path.read_bytes())
    expected = {
        "schema_version", "report_id", "report_reason", "requested_at_utc", "runtime_boot_id",
        "runtime_process_instance_id", "clean_shutdown", "diagnostic_protocol",
    }
    if type(value) is not dict or set(value) != expected or value["schema_version"] != REQUEST_SCHEMA:
        raise ValueError("invalid diagnostic report request")
    if not isinstance(value["report_id"], str) or not value["report_id"].startswith("report-"):
        raise ValueError("invalid diagnostic report identity")
    return value


def _claim(path: Path) -> Path:
    claimed = path.with_suffix(".claimed")
    os.replace(path, claimed)
    return claimed


def _segment_sources(diagnostics: Path, request: dict):
    roots = []
    runtime_id = request.get("runtime_process_instance_id")
    if runtime_id:
        candidate = diagnostics / "runtime" / runtime_id
        if candidate.is_dir():
            roots.append(("runtime", candidate))
    workbench_root = diagnostics / "workbench"
    if workbench_root.is_dir():
        candidates = sorted(
            (path for path in workbench_root.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        roots.extend(("workbench", path) for path in candidates[:3])
    files = []
    total = 0
    omitted = []
    streams = []
    for group, root in roots:
        metadata_path = root / "metadata.json"
        health_path = root / "health.json"
        stream = None
        if metadata_path.is_file():
            try:
                stream = strict_loads(metadata_path.read_bytes())
            except Exception:
                omitted.append(f"{group}/{root.name}/metadata.json:INVALID")
        if isinstance(stream, dict):
            streams.append({
                "stream_id": stream.get("stream_id"),
                "process_role": stream.get("process_role"),
                "process_instance_id": stream.get("process_instance_id"),
                "runtime_boot_id": stream.get("runtime_boot_id"),
                "created_at_utc": stream.get("created_at_utc"),
            })
        allowed = [metadata_path, health_path]
        allowed.extend(sorted(root.glob("events-*.segment.json")))
        allowed.extend(sorted(root.glob("events-*.ndjson")))
        for source in allowed:
            if not source.is_file():
                continue
            size = source.stat().st_size
            if size > _MAX_SOURCE_FILE or total + size > _MAX_BUNDLE_INPUT:
                omitted.append(f"{group}/{root.name}/{source.name}:SIZE_LIMIT")
                continue
            if source.suffix == ".ndjson" and not source.with_suffix(".segment.json").is_file():
                omitted.append(f"{group}/{root.name}/{source.name}:OPEN_SEGMENT")
                continue
            target = f"{group}/{root.name}/{source.name}"
            files.append((source, target, size, _sha256(source)))
            total += size
    return files, streams, omitted


def _write_bundle(runtime_root: Path, request: dict):
    diagnostics = runtime_root / "diagnostics"
    files, streams, omitted = _segment_sources(diagnostics, request)
    member_hashes = {target: digest for _, target, _, digest in files}
    known_gaps = bool(omitted)
    health_rows = []
    for source, target, _, _ in files:
        if target.endswith("/health.json"):
            try:
                health_rows.append(strict_loads(source.read_bytes()))
            except Exception:
                known_gaps = True
    dropped = sum(
        sum(int(count) for count in (row.get("dropped_by_priority") or {}).values())
        for row in health_rows if isinstance(row, dict)
    )
    write_failures = sum(
        int(row.get("write_failures") or 0) for row in health_rows if isinstance(row, dict)
    )
    known_gaps = known_gaps or dropped > 0 or write_failures > 0
    capture_status = "PARTIAL" if known_gaps or not files else "COMPLETE_WITHIN_SCOPE"
    if not files:
        capture_status = "FAILED"
    content_sha = hashlib.sha256(canonical_bytes(member_hashes)).hexdigest()
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "diagnostic_protocol": "1",
        "report_id": request["report_id"],
        "report_reason": request["report_reason"],
        "application_version": __version__,
        "build_manifest_hash": _build_manifest_hash(runtime_root),
        "coverage_start": min(
            (row.get("created_at_utc") for row in streams if row.get("created_at_utc")),
            default=request["requested_at_utc"],
        ),
        "coverage_end": request["requested_at_utc"],
        "streams": streams,
        "dropped_events": dropped,
        "write_failures": write_failures,
        "missing_sources": omitted,
        "snapshot_available": any(bool(row.get("snapshot_available")) for row in health_rows if isinstance(row, dict)),
        "clean_shutdown": bool(request["clean_shutdown"]),
        "capture_status": capture_status,
        "redaction_version": "1",
        # A ZIP cannot contain its own byte hash. This is the deterministic hash
        # of every evidence member; the actual ZIP hash is recorded beside and
        # with the upload result.
        "bundle_sha256": content_sha,
        "bundle_hash_scope": "EVIDENCE_MEMBERS_EXCLUDING_MANIFEST",
        "members": member_hashes,
    }
    summary = {
        "schema_version": "ONLINE_TRADER_DIAGNOSTIC_SUMMARY/1",
        "report_id": request["report_id"],
        "capture_status": capture_status,
        "stream_count": len(streams),
        "evidence_file_count": len(files),
        "known_omissions": omitted,
    }
    environment = {
        "schema_version": "ONLINE_TRADER_DIAGNOSTIC_ENVIRONMENT/1",
        "application_version": __version__,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": sys.platform,
    }
    bundles = diagnostics / "bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    final = bundles / f"diagnostic-{request['report_id']}.zip"
    temporary = final.with_suffix(".tmp")
    with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", canonical_bytes(manifest))
        archive.writestr("summary.json", canonical_bytes(summary))
        archive.writestr("environment.json", canonical_bytes(environment))
        for source, target, _, _ in files:
            archive.write(source, target)
    os.replace(temporary, final)
    return final, _sha256(final), manifest


def _retention_units(diagnostics: Path, keep: Path):
    units = []
    bundles = diagnostics / "bundles"
    if bundles.is_dir():
        units.extend(path for path in bundles.glob("*.zip") if path.resolve() != keep.resolve())
    for group in ("runtime", "runtime-view", "workbench"):
        root = diagnostics / group
        if root.is_dir():
            units.extend(path for path in root.iterdir() if path.is_dir())
    return sorted(units, key=lambda path: path.stat().st_mtime_ns)


def enforce_retention(diagnostics: Path, *, keep: Path):
    try:
        total = sum(path.stat().st_size for path in diagnostics.rglob("*") if path.is_file())
        for unit in _retention_units(diagnostics, keep):
            if total <= _MAX_LOCAL_BYTES:
                break
            size = sum(path.stat().st_size for path in unit.rglob("*") if path.is_file()) if unit.is_dir() else unit.stat().st_size
            if unit.is_dir():
                shutil.rmtree(unit)
            else:
                unit.unlink()
            total = max(0, total - size)
    except OSError:
        pass


def _append_result(diagnostics: Path, result: UploadResult):
    path = diagnostics / "upload-results.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_bytes(result.to_record() | {"observed_at_utc": utc_now()}) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(line)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("diagnostic upload result write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def report(runtime_root, request_path) -> int:
    root = Path(runtime_root).resolve()
    diagnostics = root / "diagnostics"
    request_path = Path(request_path).resolve()
    if not request_path.is_relative_to((diagnostics / "report-requests").resolve()):
        raise ValueError("diagnostic report request is outside the Runtime root")
    claimed = _claim(request_path)
    request = _read_request(claimed)
    # Let a concurrently closing Workbench finish its final segment without
    # keeping the Runtime process alive.
    time.sleep(0.75)
    bundle, archive_sha, manifest = _write_bundle(root, request)
    try:
        config = load_upload_config(root)
        if config is None:
            result = UploadResult(
                status="LOCAL_ONLY",
                backend="LOCAL",
                host=None,
                project_id=None,
                report_id=request["report_id"],
                bundle_file=bundle.name,
                bundle_bytes=bundle.stat().st_size,
                bundle_sha256=archive_sha,
            )
        else:
            result = JiHuLabBackend(config).upload(
                bundle, report_id=request["report_id"], bundle_sha256=archive_sha
            )
    except Exception as error:
        result = failed_upload_result(
            bundle=bundle,
            report_id=request["report_id"],
            bundle_sha256=archive_sha,
            error=error,
        )
    try:
        _append_result(diagnostics, result)
    except OSError:
        pass
    completed = claimed.with_suffix(".done.json")
    publish_replace_bytes(canonical_bytes({
        **request,
        "completed_at_utc": utc_now(),
        "capture_status": manifest["capture_status"],
        "bundle_file": bundle.name,
        "bundle_sha256": archive_sha,
        "upload": result.to_record(),
    }), completed)
    claimed.unlink(missing_ok=True)
    enforce_retention(diagnostics, keep=bundle)
    return 0 if result.status in {"UPLOADED", "LOCAL_ONLY"} else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return report(args.runtime_root, args.request)
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
