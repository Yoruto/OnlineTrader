"""Build a bounded cross-process timeline from a validated diagnostic bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_bundle import validate


def analyze(path):
    validation = validate(path)
    if validation["result"] != "VALID":
        return {"result": "INCONCLUSIVE", "validation": validation, "timeline": []}
    timeline = []
    with ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith(".ndjson"):
                continue
            for line in archive.read(name).splitlines():
                row = json.loads(line)
                timeline.append({
                    "observed_at_utc": row.get("observed_at_utc"),
                    "monotonic_ns": row.get("monotonic_ns"),
                    "process_role": row.get("process_role"),
                    "runtime_boot_id": row.get("runtime_boot_id"),
                    "event_type": row.get("event_type"),
                    "reason_code": row.get("reason_code"),
                    "error_code": row.get("error_code"),
                    "correlation": row.get("correlation") or {},
                })
    timeline.sort(key=lambda row: (row["observed_at_utc"] or "", row["process_role"] or "", row["monotonic_ns"] or ""))
    errors = [row for row in timeline if row["error_code"]]
    return {
        "schema_version": "ONLINE_TRADER_DIAGNOSTIC_ANALYSIS/1",
        # Timeline construction alone never proves a behavior rule.
        "result": "INCONCLUSIVE",
        "validation": validation,
        "event_count": len(timeline),
        "error_count": len(errors),
        "timeline": timeline,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = analyze(args.bundle)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.write_text(text, encoding="utf-8")
    return 0 if result["validation"]["result"] == "VALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())
