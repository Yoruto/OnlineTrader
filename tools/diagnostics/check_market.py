"""Classify the first observed market evidence gap without claiming unexercised behavior passed."""

from __future__ import annotations

import argparse
import json
from zipfile import ZipFile


def check(path):
    events = []
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for name in archive.namelist():
            if name.endswith(".ndjson"):
                events.extend(json.loads(line) for line in archive.read(name).splitlines())
    market = [row for row in events if row.get("component") == "MARKET"]
    anomalies = [row for row in market if row.get("event_type") == "MARKET_ANOMALY"]
    if not market:
        result = "NOT_EXERCISED"
        reason = "NO_MARKET_EVIDENCE"
    elif anomalies:
        result = "DEGRADED"
        reason = anomalies[0].get("reason_code") or "MARKET_ANOMALY"
    elif manifest.get("capture_status") != "COMPLETE_WITHIN_SCOPE":
        result = "INCONCLUSIVE"
        reason = "PARTIAL_CAPTURE"
    else:
        result = "OBSERVED_OK"
        reason = "NO_VIOLATION_IN_OBSERVED_WINDOW"
    return {
        "schema_version": "ONLINE_TRADER_MARKET_DIAGNOSTIC_CHECK/1",
        "result": result,
        "reason": reason,
        "market_event_count": len(market),
        "anomaly_count": len(anomalies),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    args = parser.parse_args(argv)
    print(json.dumps(check(args.bundle), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
