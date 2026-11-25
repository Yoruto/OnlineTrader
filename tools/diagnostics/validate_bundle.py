"""Validate an OnlineTrader diagnostic ZIP without trusting its conclusions."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile


MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 4096


def _json(archive: ZipFile, name: str):
    raw = archive.read(name)
    if len(raw) > MAX_MEMBER_BYTES:
        raise ValueError(f"oversized member: {name}")
    return json.loads(raw.decode("utf-8"))


def _check_archive_bounds(archive: ZipFile) -> list[str]:
    names = archive.namelist()
    if len(names) > MAX_MEMBERS or len(names) != len(set(names)):
        raise ValueError("invalid diagnostic member set")
    total_size = 0
    for name in names:
        member = PurePosixPath(name)
        info = archive.getinfo(name)
        if member.is_absolute() or ".." in member.parts or info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f"unsafe diagnostic member: {name}")
        total_size += info.file_size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError("diagnostic archive uncompressed size exceeds limit")
    return names


def validate(path):
    errors = []
    gaps = []
    selection_gaps = []
    streams = {}
    with ZipFile(path) as archive:
        names = _check_archive_bounds(archive)
        manifest = _json(archive, "manifest.json")
        if manifest.get("schema_version") != "ONLINE_TRADER_DIAGNOSTIC_MANIFEST/1":
            errors.append("UNSUPPORTED_MANIFEST")
        expected = manifest.get("members") or {}
        stream_contracts = {
            row.get("stream_id"): row for row in (manifest.get("streams") or [])
            if isinstance(row, dict) and row.get("stream_id")
        }
        for name, digest in expected.items():
            if name not in names:
                errors.append(f"MISSING_MEMBER:{name}")
                continue
            actual = hashlib.sha256(archive.read(name)).hexdigest()
            if actual != digest:
                errors.append(f"HASH_MISMATCH:{name}")
        last_by_stream = {}
        for name in names:
            if not name.endswith(".ndjson"):
                continue
            stream_id = None
            for line_number, line in enumerate(archive.read(name).splitlines(), 1):
                try:
                    row = json.loads(line)
                    sequence = int(row["event_seq"])
                    current_stream = row["stream_id"]
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    errors.append(f"INVALID_EVENT:{name}:{line_number}")
                    continue
                if stream_id is None:
                    stream_id = current_stream
                elif stream_id != current_stream:
                    errors.append(f"STREAM_CHANGED:{name}:{line_number}")
                previous = last_by_stream.get(current_stream, 0)
                if previous and sequence != previous + 1:
                    gap = {"member": name, "after": previous, "next": sequence}
                    contract = stream_contracts.get(current_stream) or {}
                    if (contract.get("source_sequence_complete")
                            and int(contract.get("excluded_event_count") or 0) > 0):
                        selection_gaps.append(gap)
                    else:
                        gaps.append(gap)
                last_by_stream[current_stream] = sequence
            if stream_id is not None:
                streams[stream_id] = last_by_stream[stream_id]
        result = "VALID" if not errors else "INVALID"
        coverage = "PARTIAL" if gaps or manifest.get("capture_status") != "COMPLETE_WITHIN_SCOPE" else "COMPLETE_WITHIN_SCOPE"
        return {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_VALIDATION/1",
            "result": result,
            "coverage": coverage,
            "report_id": manifest.get("report_id"),
            "errors": errors,
            "sequence_gaps": gaps,
            "selection_gaps": selection_gaps,
            "streams": streams,
        }


def load_validated_bundle(path):
    """Validate and parse bounded events, runtime-view rows, and bootstrap context."""
    validation = validate(path)
    if validation["result"] != "VALID":
        return validation, {}, [], [], []
    events = []
    view_logs = []
    bootstrap_rows = []
    with ZipFile(path) as archive:
        manifest = _json(archive, "manifest.json")
        for name in archive.namelist():
            if name.endswith(".ndjson"):
                for line in archive.read(name).splitlines():
                    events.append(json.loads(line))
            elif name == "runtime-view/runtime-view-selected.jsonl":
                for line in archive.read(name).splitlines():
                    view_logs.append(json.loads(line))
            elif name == "bootstrap/startup-context.jsonl":
                for line in archive.read(name).splitlines():
                    bootstrap_rows.append(json.loads(line))
    return validation, manifest, events, view_logs, bootstrap_rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    args = parser.parse_args(argv)
    try:
        result = validate(args.bundle)
    except (OSError, ValueError, BadZipFile, KeyError, json.JSONDecodeError) as error:
        result = {
            "schema_version": "ONLINE_TRADER_DIAGNOSTIC_VALIDATION/1",
            "result": "INVALID",
            "coverage": "FAILED",
            "errors": [type(error).__name__],
            "sequence_gaps": [],
            "selection_gaps": [],
            "streams": {},
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "VALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())
