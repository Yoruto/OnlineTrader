"""Build one bounded, attributable diagnostic bundle and attempt one upload."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock, Thread
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


REQUEST_SCHEMA = "ONLINE_TRADER_DIAGNOSTIC_REPORT_REQUEST/2"
LEGACY_REQUEST_SCHEMA = "ONLINE_TRADER_DIAGNOSTIC_REPORT_REQUEST/1"
MANIFEST_SCHEMA = "ONLINE_TRADER_DIAGNOSTIC_MANIFEST/1"
_MAX_SOURCE_FILE = 8 * 1024 * 1024
_MAX_BUNDLE_INPUT = 32 * 1024 * 1024
# Group reservations; whatever one group does not need is shared with the other.
_RUNTIME_RESERVE = 20 * 1024 * 1024
_WORKBENCH_RESERVE = _MAX_BUNDLE_INPUT - _RUNTIME_RESERVE
# Per-stream evidence kept even when recent segments consume the budget.
_HEAD_RESERVE = 1 * 1024 * 1024
_KEY_EVENT_RESERVE = 1 * 1024 * 1024
_MAX_KEY_SCAN = 64 * 1024 * 1024
_MAX_WORKBENCH_SCAN = 64 * 1024 * 1024
_MAX_WORKBENCH_SCAN_SECONDS = 3.0
_KEY_COMPONENTS = frozenset({"LIFECYCLE", "OWNER", "STORAGE", "ORDER", "COMMAND"})
_KEY_SEVERITIES = frozenset({"ERROR", "CRITICAL"})
_KEY_EVENT_TYPES = frozenset({
    "MARKET_ANOMALY",
    "WORKBENCH_CONNECTION_CHANGED",
    "WORKBENCH_SNAPSHOT_CHANGED",
    "WORKBENCH_SNAPSHOT_EXPIRED",
    "WORKBENCH_SNAPSHOT_RECOVERED",
    "STARTUP_TARGET_INFERENCE",
    "CONVERGENCE_ACTION",
    "CONVERGENCE_DEFERRED",
})
_KEY_LINE_TOKENS = (
    b'"LIFECYCLE"', b'"OWNER"', b'"STORAGE"', b'"ORDER"', b'"COMMAND"',
    b'"ERROR"', b'"CRITICAL"',
    b'"MARKET_ANOMALY"',
    b'"WORKBENCH_CONNECTION_CHANGED"',
    b'"WORKBENCH_SNAPSHOT_CHANGED"',
    b'"WORKBENCH_SNAPSHOT_EXPIRED"',
    b'"WORKBENCH_SNAPSHOT_RECOVERED"',
    b'"STARTUP_TARGET_INFERENCE"',
    b'"CONVERGENCE_ACTION"',
    b'"CONVERGENCE_DEFERRED"',
)
_MAX_LOCAL_BYTES = 500 * 1024 * 1024
_CONTEXT_EVENTS = frozenset({
    "WORKBENCH_STARTED", "WORKBENCH_STOPPED", "WORKBENCH_CONNECTION_CHANGED",
    "QT_UNHANDLED_EXCEPTION",
})
_VIEW_LINE = re.compile(
    r"^(\S+) (?:SUPERVISOR )?("
    r"START pid=\d+|EXIT code=-?\d+ uptime_ms=\d+|START_FAILED error=[A-Za-z0-9_]+|"
    r"VIEW_CLIENT pid=\d+ boot=[A-Za-z0-9._-]+|"
    r"COMMAND source=(?:WORKBENCH|OPERATOR) .* boot=[A-Za-z0-9._-]+ .*"
    r"result=(?:ACCEPTED|REJECTED) code=[A-Za-z0-9._-]+)$"
)
_BOOT_TOKEN = re.compile(r"(?:^| )boot=([A-Za-z0-9._-]+)(?: |$)")
_async_lock = Lock()
_async_keys = set()


def _utc_key(value):
    """Order UTC text by instant; strings with/without fractions do not sort correctly."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _build_manifest_hash(runtime_root: Path) -> str | None:
    for candidate in (
        runtime_root / "base-build-manifest.json",
        runtime_root.parent / "base-build-manifest.json",
    ):
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
    trigger_key: str | None = None,
    runtime_view_expected: bool = True,
    workbench_expected: bool = False,
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
        "trigger_key": trigger_key,
        "expected_sources": {
            "runtime": bool(runtime_process_instance_id),
            "runtime_view": bool(runtime_view_expected),
            "workbench": bool(workbench_expected),
        },
        "diagnostic_protocol": "1",
    }
    return publish_new_bytes(
        canonical_bytes(request), diagnostics / "report-requests" / f"{report_id}.json"
    )


def _append_line(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_bytes(record) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(line)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("diagnostic append made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record_failure(diagnostics: Path, *, report_id, stage, error):
    try:
        _append_line(diagnostics / "report-failures.ndjson", {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_REPORT_FAILURE/1",
            "report_id": report_id,
            "stage": stage,
            "error_code": "E_DIAGNOSTIC_REPORTER",
            "error_type": type(error).__name__,
            "observed_at_utc": utc_now(),
        })
    except OSError:
        pass


def launch_reporter(runtime_root, request_path) -> bool:
    """Start a detached Reporter without delaying Runtime work."""
    root = str(Path(runtime_root).resolve())
    request = str(Path(request_path).resolve())
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--diagnostic-reporter", request, "--diagnostic-runtime-root", root]
    else:
        command = [sys.executable, "-m", "online_trader.diagnostics.reporter",
                   "--request", request, "--runtime-root", root]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
        )
        return True
    except OSError as error:
        _record_failure(Path(root) / "diagnostics", report_id=Path(request).stem,
                        stage="LAUNCH", error=error)
        return False


def request_report_async(runtime_root, **request):
    """Create one process-local close report request off the Owner thread."""
    key = request.get("trigger_key")
    identity = (str(Path(runtime_root).resolve()), key or uuid4().hex)
    with _async_lock:
        if key is not None and identity in _async_keys:
            return False
        _async_keys.add(identity)

    def create_and_launch():
        diagnostics = Path(runtime_root).resolve() / "diagnostics"
        try:
            path = create_report_request(runtime_root, **request)
            launch_reporter(runtime_root, path)
        except Exception as error:
            _record_failure(diagnostics, report_id="uncreated-" + uuid4().hex[:12],
                            stage="REQUEST", error=error)

    Thread(target=create_and_launch, name="OnlineTrader-diagnostic-close-report", daemon=True).start()
    return True


def _read_request(path: Path) -> dict:
    value = strict_loads(path.read_bytes())
    if type(value) is not dict:
        raise ValueError("invalid diagnostic report request")
    schema = value.get("schema_version")
    legacy = {
        "schema_version", "report_id", "report_reason", "requested_at_utc", "runtime_boot_id",
        "runtime_process_instance_id", "clean_shutdown", "diagnostic_protocol",
    }
    current = legacy | {"trigger_key", "expected_sources"}
    if schema == LEGACY_REQUEST_SCHEMA and set(value) == legacy:
        value = value | {
            "trigger_key": None,
            "expected_sources": {"runtime": bool(value.get("runtime_process_instance_id")),
                                 "runtime_view": True,
                                 "workbench": False},
        }
    elif schema != REQUEST_SCHEMA or set(value) != current:
        raise ValueError("invalid diagnostic report request")
    if not isinstance(value["report_id"], str) or not value["report_id"].startswith("report-"):
        raise ValueError("invalid diagnostic report identity")
    expected = value.get("expected_sources")
    if (type(expected) is not dict or set(expected) != {"runtime", "runtime_view", "workbench"}
            or type(expected["runtime"]) is not bool or type(expected["runtime_view"]) is not bool
            or type(expected["workbench"]) is not bool):
        raise ValueError("invalid diagnostic expected source contract")
    return value


def _claim(path: Path) -> Path:
    claimed = path.with_suffix(".claimed")
    os.replace(path, claimed)
    return claimed


def _json_snapshot(path: Path):
    try:
        raw = path.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            return None
        value = strict_loads(raw)
        return value if type(value) is dict else None
    except Exception:
        return None


def _line_snapshot(path: Path, *, limit: int, from_end: bool = False):
    """Read complete lines from a fixed-size snapshot.

    ``from_end`` keeps the newest bytes of a larger file and drops the leading
    partial line, so recent evidence wins when the file exceeds ``limit``.
    """
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        take = min(size, limit)
        if from_end and take < size:
            stream.seek(size - take)
        raw = stream.read(take)
    limited = size > take
    tail_discarded = False
    if from_end and limited and raw:
        start = raw.find(b"\n")
        raw = b"" if start < 0 else raw[start + 1:]
    if raw and not raw.endswith(b"\n"):
        end = raw.rfind(b"\n")
        raw = b"" if end < 0 else raw[:end + 1]
        tail_discarded = True
    return raw, size, limited, tail_discarded


def _stage(staging: Path, target: str, raw: bytes, files: list):
    destination = staging / Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    files.append((destination, target, len(raw), hashlib.sha256(raw).hexdigest()))


def _is_key_event(row) -> bool:
    return (row.get("component") in _KEY_COMPONENTS
            or row.get("severity") in _KEY_SEVERITIES
            or row.get("event_type") in _KEY_EVENT_TYPES)


def _scan_key_events(path: Path, budget: int):
    """Stream one skipped segment and keep only bounded key diagnostic lines."""
    kept = []
    used = 0
    scanned = 0
    with path.open("rb") as stream:
        for line in stream:
            scanned += len(line)
            if scanned > _MAX_SOURCE_FILE or not line.endswith(b"\n"):
                break
            if not any(token in line for token in _KEY_LINE_TOKENS):
                continue
            if used + len(line) > budget:
                break
            kept.append(line)
            used += len(line)
    return kept, used, scanned


def _plan_sources(sources, budget):
    """Choose which segments to read: newest first, plus the oldest head.

    Returns ``(plan, key_budget)`` where ``plan`` maps a segment to
    ``("FULL"|"TAIL"|"HEAD", byte_limit)``; unplanned segments are skipped and
    only scanned for key events within ``key_budget``.
    """
    plan = {}
    if not sources or budget <= 0:
        return plan, 0
    sizes = {}
    for source in sources:
        try:
            sizes[source] = source.stat().st_size
        except OSError:
            sizes[source] = 0
    total_size = sum(min(size, _MAX_SOURCE_FILE) for size in sizes.values())
    if total_size <= budget:
        return {
            source: ("FULL" if sizes[source] <= _MAX_SOURCE_FILE else "TAIL", _MAX_SOURCE_FILE)
            for source in sources
        }, 0
    multiple = len(sources) > 1
    head_reserve = min(_HEAD_RESERVE, budget // 8) if multiple else 0
    key_reserve = min(_KEY_EVENT_RESERVE, budget // 8) if len(sources) > 2 else 0
    recent_budget = budget - head_reserve - key_reserve
    oldest = sources[0]
    for source in reversed(sources):
        if recent_budget <= 0:
            break
        size = sizes[source]
        take = min(size, _MAX_SOURCE_FILE, recent_budget)
        if take <= 0:
            continue
        plan[source] = ("FULL" if take >= size else "TAIL", take)
        recent_budget -= take
    if oldest not in plan:
        head_budget = head_reserve + max(0, recent_budget)
        if head_budget > 0:
            plan[oldest] = ("HEAD", min(head_budget, _MAX_SOURCE_FILE))
    return plan, key_reserve


def _collect_event_stream(staging, group, root, target_boot, budget, files, omissions,
                          *, max_selected_bytes=None):
    metadata = _json_snapshot(root / "metadata.json")
    health = _json_snapshot(root / "health.json")
    rows = []
    source_first = None
    source_last = None
    source_sequence_complete = True
    source_open = False
    tail_discarded = False
    invalid_events = 0
    raw_bytes = 0
    budget_trimmed = False
    key_event_count = 0
    sources = sorted(root.glob("events-*.ndjson"))
    plan, key_budget = _plan_sources(sources, budget)
    key_scanned = 0
    boundary_break = False

    def accept(line):
        nonlocal invalid_events
        try:
            row = strict_loads(line)
            sequence = int(row["event_seq"])
            if type(row) is not dict or row.get("stream_id") != (metadata or {}).get("stream_id"):
                raise ValueError
        except Exception:
            invalid_events += 1
            return None, None
        return row, sequence

    for source in sources:
        step = plan.get(source)
        if step is None:
            budget_trimmed = True
            boundary_break = True
            omissions.append(f"{group}/{root.name}/{source.name}:BUNDLE_LIMIT")
            if key_budget > 0 and key_scanned < _MAX_KEY_SCAN:
                try:
                    lines, used, scanned = _scan_key_events(source, key_budget)
                except OSError:
                    omissions.append(f"{group}/{root.name}/{source.name}:UNREADABLE")
                    continue
                key_budget -= used
                key_scanned += scanned
                raw_bytes += used
                for line in lines:
                    row, _ = accept(line)
                    if row is not None and _is_key_event(row):
                        rows.append(row)
                        key_event_count += 1
            continue
        mode, limit = step
        try:
            raw, _, limited, tail = _line_snapshot(source, limit=limit, from_end=(mode == "TAIL"))
        except OSError:
            omissions.append(f"{group}/{root.name}/{source.name}:UNREADABLE")
            boundary_break = True
            continue
        raw_bytes += len(raw)
        sidecar = source.with_suffix(".segment.json")
        closed = sidecar.is_file()
        source_open = source_open or not closed
        tail_discarded = tail_discarded or tail
        if limited:
            budget_trimmed = True
            omissions.append(f"{group}/{root.name}/{source.name}:SIZE_LIMIT_{mode}")
        if closed and not limited:
            segment = _json_snapshot(sidecar)
            try:
                integrity_ok = (
                    segment is not None and segment.get("file") == source.name
                    and int(segment.get("bytes")) == len(raw)
                    and segment.get("sha256") == hashlib.sha256(raw).hexdigest()
                )
            except (TypeError, ValueError):
                integrity_ok = False
            if not integrity_ok:
                omissions.append(f"{group}/{root.name}/{source.name}:SEGMENT_INTEGRITY")
                source_sequence_complete = False
        if mode == "TAIL":
            boundary_break = True
        for line in raw.splitlines():
            row, sequence = accept(line)
            if row is None:
                continue
            # Deliberate budget trimming is reported as BUNDLE_LIMIT/SIZE_LIMIT,
            # not as a source sequence defect.
            if source_last is not None and not boundary_break and sequence != source_last + 1:
                source_sequence_complete = False
            boundary_break = False
            source_first = sequence if source_first is None else source_first
            source_last = sequence
            rows.append(row)
        if mode == "HEAD" and limited:
            boundary_break = True
    rows.sort(key=lambda row: int(row["event_seq"]))
    exact = [row for row in rows if row.get("runtime_boot_id") == target_boot]
    context = []
    if group == "workbench" and exact:
        context = [row for row in rows if row.get("runtime_boot_id") is None
                   and row.get("event_type") in _CONTEXT_EVENTS]
    selected = exact if group == "runtime" else sorted(
        [*exact, *context], key=lambda row: int(row["event_seq"])
    )
    if group == "workbench" and not exact:
        return None, raw_bytes, 0
    if group == "runtime" and not exact:
        omissions.append(f"runtime/{root.name}:NO_TARGET_BOOT_EVENTS")
    staged_selected_bytes = 0
    if selected:
        encoded = [canonical_bytes(row) + b"\n" for row in selected]
        if max_selected_bytes is not None:
            kept_rows = []
            kept_encoded = []
            kept_size = 0
            for row, line in zip(reversed(selected), reversed(encoded)):
                if kept_size + len(line) > max_selected_bytes and kept_encoded:
                    budget_trimmed = True
                    continue
                if kept_size + len(line) > max_selected_bytes:
                    budget_trimmed = True
                    continue
                kept_rows.append(row)
                kept_encoded.append(line)
                kept_size += len(line)
            if len(kept_rows) < len(selected):
                omissions.append(f"{group}/{root.name}:BUNDLE_LIMIT")
                selected = list(reversed(kept_rows))
                encoded = list(reversed(kept_encoded))
                exact = [row for row in selected if row.get("runtime_boot_id") == target_boot]
                if group == "workbench" and not exact:
                    return None, raw_bytes, 0
                context = [row for row in selected if row.get("runtime_boot_id") is None]
        payload_bytes = b"".join(encoded)
        staged_selected_bytes = len(payload_bytes)
        _stage(staging, f"{group}/{root.name}/events-selected.ndjson", payload_bytes, files)
    if metadata is not None:
        _stage(staging, f"{group}/{root.name}/metadata.json", canonical_bytes(metadata), files)
    else:
        omissions.append(f"{group}/{root.name}/metadata.json:INVALID_OR_MISSING")
    if health is not None:
        _stage(staging, f"{group}/{root.name}/health.json", canonical_bytes(health), files)
    else:
        omissions.append(f"{group}/{root.name}/health.json:INVALID_OR_MISSING")
    finalized = bool(health and not health.get("active") and health.get("finalized_at_utc"))
    dropped = sum(int(count) for count in (health or {}).get("dropped_by_priority", {}).values())
    write_failures = int((health or {}).get("write_failures") or 0)
    retention_dropped = int((health or {}).get("retention_dropped_events") or 0)
    if source_open:
        omissions.append(f"{group}/{root.name}:OPEN_PREFIX")
    if tail_discarded:
        omissions.append(f"{group}/{root.name}:TAIL_DISCARDED")
    if invalid_events:
        omissions.append(f"{group}/{root.name}:INVALID_EVENTS:{invalid_events}")
    if not source_sequence_complete:
        omissions.append(f"{group}/{root.name}:SOURCE_SEQUENCE_GAP")
    if not finalized:
        omissions.append(f"{group}/{root.name}:NOT_FINALIZED")
    if dropped or write_failures or retention_dropped:
        omissions.append(f"{group}/{root.name}:SINK_GAPS")
    observed = [row.get("observed_at_utc") for row in selected if row.get("observed_at_utc")]
    return {
        "stream_id": (metadata or {}).get("stream_id"),
        "process_role": (metadata or {}).get("process_role"),
        "process_instance_id": (metadata or {}).get("process_instance_id", root.name),
        "runtime_boot_id": target_boot,
        "created_at_utc": (metadata or {}).get("created_at_utc"),
        "first_event_at_utc": min(observed, key=_utc_key, default=None),
        "last_event_at_utc": max(observed, key=_utc_key, default=None),
        "budget_trimmed": budget_trimmed,
        "key_event_count": key_event_count,
        "source_first_event_seq": None if source_first is None else str(source_first),
        "source_last_event_seq": None if source_last is None else str(source_last),
        "source_sequence_complete": source_sequence_complete,
        "selected_event_count": len(exact),
        "unattributed_context_count": len(context),
        "excluded_event_count": max(0, len(rows) - len(selected)),
        "source_was_open": source_open,
        "tail_discarded": tail_discarded,
        "finalized": finalized,
        "dropped_events": dropped + retention_dropped,
        "write_failures": write_failures,
        "snapshot_available": bool((health or {}).get("snapshot_available")),
    }, raw_bytes, staged_selected_bytes


def _collect_view_log(staging, runtime_root, request, coverage_start, files, omissions):
    rows = []
    unparsed = 0
    target_boot = request.get("runtime_boot_id")
    logs = runtime_root / "logs"
    found = False
    for path in (logs / "runtime-view.log.1", logs / "runtime-view.log"):
        if not path.is_file():
            continue
        found = True
        try:
            raw, _, limited, tail = _line_snapshot(path, limit=_MAX_SOURCE_FILE, from_end=True)
        except OSError:
            omissions.append(f"runtime-view/{path.name}:UNREADABLE")
            continue
        if limited:
            omissions.append(f"runtime-view/{path.name}:SIZE_LIMIT")
        if tail:
            omissions.append(f"runtime-view/{path.name}:TAIL_DISCARDED")
        for raw_line in raw.decode("utf-8", errors="replace").splitlines():
            match = _VIEW_LINE.fullmatch(raw_line)
            if match is None:
                if raw_line[:1].isdigit():
                    unparsed += 1
                continue
            occurred, message = match.groups()
            if not _utc_key(coverage_start) <= _utc_key(occurred) <= _utc_key(request["requested_at_utc"]):
                continue
            boot_match = _BOOT_TOKEN.search(message)
            boot = boot_match.group(1) if boot_match else None
            if boot is not None and boot != target_boot:
                continue
            rows.append({
                "schema_version": "ONLINE_TRADER_RUNTIME_VIEW_LOG/1",
                "occurred_at_utc": occurred,
                "runtime_boot_id": boot,
                "attribution": "EXACT_BOOT" if boot is not None else "UNATTRIBUTED_PROCESS_CONTEXT",
                "message": message,
            })
    if request["expected_sources"]["runtime_view"] and not found:
        omissions.append("runtime-view:MISSING_EXPECTED_SOURCE")
    if unparsed:
        omissions.append(f"runtime-view:UNPARSED_LINES:{unparsed}")
    if rows:
        _stage(staging, "runtime-view/runtime-view-selected.jsonl",
               b"".join(canonical_bytes(row) + b"\n" for row in rows), files)
    workbench_expected = any(
        row["runtime_boot_id"] == target_boot and row["message"].startswith("VIEW_CLIENT ")
        for row in rows
    )
    return rows, workbench_expected


def _collect_sources(staging: Path, runtime_root: Path, request: dict):
    diagnostics = runtime_root / "diagnostics"
    files = []
    streams = []
    omissions = []
    target_boot = request.get("runtime_boot_id")
    runtime_id = request.get("runtime_process_instance_id")
    runtime_directory = diagnostics / "runtime" / str(runtime_id)
    workbench_root = diagnostics / "workbench"
    workbench_candidates = []
    if workbench_root.is_dir():
        def modified(path):
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return -1
        workbench_candidates = sorted(
            (path for path in workbench_root.iterdir() if path.is_dir()),
            key=modified,
            reverse=True,
        )[:64]

    # Filter recent Workbench streams by target boot first within bounded scan
    # caps so unrelated historical Workbench logs do not steal Runtime's budget.
    wb_files = []
    wb_streams = []
    wb_omissions = []
    wb_budget_remaining = min(_WORKBENCH_RESERVE, _MAX_BUNDLE_INPUT)
    wb_used = 0
    wb_scan_remaining = _MAX_WORKBENCH_SCAN
    wb_scan_deadline = time.monotonic() + _MAX_WORKBENCH_SCAN_SECONDS
    workbench_count = 0
    for directory in workbench_candidates:
        if wb_budget_remaining <= 0:
            wb_omissions.append("workbench:BUNDLE_LIMIT")
            break
        if wb_scan_remaining <= 0 or time.monotonic() >= wb_scan_deadline:
            wb_omissions.append("workbench:SCAN_LIMIT")
            break
        read_budget = min(wb_scan_remaining, _MAX_BUNDLE_INPUT)
        row, raw_read, selected_used = _collect_event_stream(
            staging, "workbench", directory, target_boot,
            read_budget, wb_files, wb_omissions,
            max_selected_bytes=wb_budget_remaining,
        )
        wb_scan_remaining = max(0, wb_scan_remaining - raw_read)
        if row is not None:
            wb_used += selected_used
            wb_budget_remaining = max(0, wb_budget_remaining - selected_used)
            wb_streams.append(row)
            workbench_count += 1

    runtime_budget = max(0, _MAX_BUNDLE_INPUT - wb_used)
    if runtime_id and runtime_directory.is_dir():
        row, _, _ = _collect_event_stream(
            staging, "runtime", runtime_directory, target_boot,
            runtime_budget, files, omissions,
        )
        if row is not None:
            streams.append(row)
    elif request["expected_sources"]["runtime"]:
        omissions.append(f"runtime/{runtime_id}:MISSING_EXPECTED_SOURCE")
    coverage_start = min(
        (row.get("created_at_utc") for row in streams if row.get("created_at_utc")),
        key=_utc_key,
        default=request["requested_at_utc"],
    )
    view_rows, workbench_expected = _collect_view_log(
        staging, runtime_root, request, coverage_start, files, omissions,
    )
    files.extend(wb_files)
    streams.extend(wb_streams)
    omissions.extend(wb_omissions)
    workbench_expected = bool(request["expected_sources"]["workbench"] or workbench_expected)
    if workbench_expected and not workbench_count:
        omissions.append("workbench:MISSING_EXPECTED_SOURCE")
    return files, streams, omissions, view_rows, workbench_expected


def _write_bundle(runtime_root: Path, request: dict):
    diagnostics = runtime_root / "diagnostics"
    with TemporaryDirectory(prefix="online-trader-diagnostic-") as temporary_directory:
        staging = Path(temporary_directory)
        capture_started = utc_now()
        files, streams, omitted, view_rows, workbench_expected = _collect_sources(
            staging, runtime_root, request
        )
        # Coverage describes evidence actually in the bundle, not process lifetime
        # or the trigger instant; events may legitimately follow requested_at_utc.
        event_times = [
            value for row in streams
            for value in (row.get("first_event_at_utc"), row.get("last_event_at_utc")) if value
        ] + [row["occurred_at_utc"] for row in view_rows if row.get("occurred_at_utc")]
        first_event = min(event_times, key=_utc_key, default=None)
        last_event = max(event_times, key=_utc_key, default=None)
        coverage_start = first_event or request["requested_at_utc"]
        coverage_end = last_event or request["requested_at_utc"]
        member_hashes = {target: digest for _, target, _, digest in files}
        dropped = sum(int(row.get("dropped_events") or 0) for row in streams)
        write_failures = sum(int(row.get("write_failures") or 0) for row in streams)
        valid_events = sum(int(row.get("selected_event_count") or 0) for row in streams)
        valid_evidence = valid_events + len(view_rows)
        capture_status = "COMPLETE_WITHIN_SCOPE"
        if valid_evidence <= 0:
            capture_status = "FAILED"
        elif omitted or dropped or write_failures or any(not row.get("finalized") for row in streams):
            capture_status = "PARTIAL"
        if not request["clean_shutdown"]:
            capture_status = "PARTIAL" if valid_evidence else "FAILED"
        content_sha = hashlib.sha256(canonical_bytes(member_hashes)).hexdigest()
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "diagnostic_protocol": "1",
            "report_id": request["report_id"],
            "report_reason": request["report_reason"],
            "trigger_key": request.get("trigger_key"),
            "application_version": __version__,
            "build_manifest_hash": _build_manifest_hash(runtime_root),
            "coverage_start": coverage_start,
            "coverage_end": coverage_end,
            "coverage_semantics": "OBSERVED_EVENTS",
            "requested_at_utc": request["requested_at_utc"],
            "capture_started_at_utc": capture_started,
            "first_event_at_utc": first_event,
            "last_event_at_utc": last_event,
            "target_runtime_boot_id": request.get("runtime_boot_id"),
            "expected_sources": request["expected_sources"] | {
                "workbench_observed_by_runtime_view": workbench_expected,
            },
            "streams": streams,
            "valid_event_count": valid_events,
            "runtime_view_record_count": len(view_rows),
            "dropped_events": dropped,
            "write_failures": write_failures,
            "missing_sources": omitted,
            "snapshot_available": any(bool(row.get("snapshot_available")) for row in streams),
            "clean_shutdown": bool(request["clean_shutdown"]),
            "capture_status": capture_status,
            "redaction_version": "1",
            "bundle_sha256": content_sha,
            "bundle_hash_scope": "EVIDENCE_MEMBERS_EXCLUDING_MANIFEST",
            "members": member_hashes,
        }
        summary = {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_SUMMARY/1",
            "report_id": request["report_id"],
            "capture_status": capture_status,
            "stream_count": len(streams),
            "valid_event_count": valid_events,
            "runtime_view_record_count": len(view_rows),
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
    archive_sha = _sha256(final)
    publish_replace_bytes((archive_sha + "\n").encode("ascii"), final.with_suffix(".zip.sha256"))
    return final, archive_sha, manifest


def _finalized_directory(path: Path) -> bool:
    health = _json_snapshot(path / "health.json")
    return bool(health and not health.get("active") and health.get("finalized_at_utc"))


def _retention_units(diagnostics: Path, keep: Path):
    units = []
    bundles = diagnostics / "bundles"
    if bundles.is_dir():
        units.extend(path for path in bundles.glob("*.zip") if path.resolve() != keep.resolve())
    for group in ("runtime", "runtime-view", "workbench"):
        root = diagnostics / group
        if root.is_dir():
            units.extend(path for path in root.iterdir()
                         if path.is_dir() and _finalized_directory(path))
    return sorted(units, key=lambda path: path.stat().st_mtime_ns)


def enforce_retention(diagnostics: Path, *, keep: Path):
    try:
        total = sum(path.stat().st_size for path in diagnostics.rglob("*") if path.is_file())
        for unit in _retention_units(diagnostics, keep):
            if total <= _MAX_LOCAL_BYTES:
                break
            size = (sum(path.stat().st_size for path in unit.rglob("*") if path.is_file())
                    if unit.is_dir() else unit.stat().st_size)
            if unit.is_dir():
                shutil.rmtree(unit)
            else:
                unit.unlink()
                unit.with_suffix(".zip.sha256").unlink(missing_ok=True)
            total = max(0, total - size)
    except OSError:
        pass


def _append_result(diagnostics: Path, result: UploadResult):
    _append_line(diagnostics / "upload-results.ndjson",
                 result.to_record() | {"observed_at_utc": utc_now()})


def report(runtime_root, request_path) -> int:
    root = Path(runtime_root).resolve()
    diagnostics = root / "diagnostics"
    request_path = Path(request_path).resolve()
    if not request_path.is_relative_to((diagnostics / "report-requests").resolve()):
        raise ValueError("diagnostic report request is outside the Runtime root")
    claimed = _claim(request_path)
    try:
        request = _read_request(claimed)
        time.sleep(0.75)
        bundle, archive_sha, manifest = _write_bundle(root, request)
    except Exception as error:
        _record_failure(diagnostics, report_id=claimed.stem, stage="BUNDLE", error=error)
        try:
            publish_replace_bytes(canonical_bytes({
                "schema_version": "ONLINE_TRADER_DIAGNOSTIC_REPORT_FAILED/1",
                "report_id": claimed.stem,
                "stage": "BUNDLE",
                "error_code": "E_DIAGNOSTIC_REPORTER",
                "error_type": type(error).__name__,
                "observed_at_utc": utc_now(),
            }), claimed.with_suffix(".failed.json"))
            claimed.unlink(missing_ok=True)
        except OSError:
            pass
        return 2
    try:
        try:
            config = load_upload_config(root)
            if config is None:
                result = UploadResult(
                    status="LOCAL_ONLY", backend="LOCAL", host=None, project_id=None,
                    report_id=request["report_id"], bundle_file=bundle.name,
                    bundle_bytes=bundle.stat().st_size, bundle_sha256=archive_sha,
                )
            else:
                result = JiHuLabBackend(config).upload(
                    bundle, report_id=request["report_id"], bundle_sha256=archive_sha
                )
        except Exception as error:
            result = failed_upload_result(bundle=bundle, report_id=request["report_id"],
                                          bundle_sha256=archive_sha, error=error)
        try:
            _append_result(diagnostics, result)
        except OSError:
            pass
        publish_replace_bytes(canonical_bytes({
            **request,
            "completed_at_utc": utc_now(),
            "capture_status": manifest["capture_status"],
            "bundle_file": bundle.name,
            "bundle_sha256": archive_sha,
            "upload": result.to_record(),
        }), claimed.with_suffix(".done.json"))
        claimed.unlink(missing_ok=True)
        enforce_retention(diagnostics, keep=bundle)
        return 0 if result.status in {"UPLOADED", "LOCAL_ONLY"} else 2
    except Exception as error:
        _record_failure(diagnostics, report_id=request["report_id"], stage="COMPLETE", error=error)
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return report(args.runtime_root, args.request)
    except Exception as error:
        _record_failure(args.runtime_root.resolve() / "diagnostics",
                        report_id=args.request.stem, stage="ENTRY", error=error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
