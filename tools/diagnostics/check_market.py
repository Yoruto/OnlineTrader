"""Evaluate market continuity evidence without claiming unexercised behavior passed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from zipfile import BadZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_bundle import load_validated_bundle


def _cite(row):
    return {
        "stream_id": row.get("stream_id"),
        "event_seq": row.get("event_seq"),
        "event_type": row.get("event_type"),
        "reason_code": row.get("reason_code"),
        "observed_at_utc": row.get("observed_at_utc"),
    }


def evaluate_market(validation, manifest, events):
    """Evaluate C02 market continuity strictly from validated bundle evidence."""
    if validation.get("result") != "VALID":
        return {
            "schema_version": "ONLINE_TRADER_MARKET_DIAGNOSTIC_CHECK/1",
            "result": "INCONCLUSIVE",
            "reason": "BUNDLE_INVALID",
            "market_event_count": 0,
            "summary_count": 0,
            "anomaly_count": 0,
            "evidence_gaps": list(validation.get("errors") or ()),
            "citations": [],
        }

    market = [row for row in events if row.get("component") == "MARKET"]
    summaries = [row for row in market if row.get("event_type") == "MARKET_SUMMARY"]
    anomalies = [row for row in market if row.get("event_type") == "MARKET_ANOMALY"]
    feed_blocks = [
        row for row in events
        if row.get("event_type") == "ORDER_BLOCKED"
        and row.get("reason_code") in {"FEED_HEALTH_BLOCK", "QUOTE_AGE"}
    ]
    lifecycle = [row for row in events if row.get("component") == "LIFECYCLE"]
    failed = any(
        row.get("event_type") in {"RUNTIME_FAILED", "OWNER_FAILED"} or row.get("error_code")
        for row in lifecycle
    )

    gaps = []
    if manifest.get("capture_status") != "COMPLETE_WITHIN_SCOPE":
        gaps.append(f"capture_status={manifest.get('capture_status')}")
    if validation.get("sequence_gaps"):
        gaps.append("stream_sequence_gaps")
    if int(manifest.get("dropped_events") or 0) > 0:
        gaps.append(f"dropped_events={manifest.get('dropped_events')}")

    citations = [_cite(row) for row in (anomalies + feed_blocks)[:10]]
    sdk_polls = sum(int((row.get("payload") or {}).get("sdk_polls") or 0) for row in summaries)
    queue_drops = sum(int((row.get("payload") or {}).get("queue_drops") or 0) for row in summaries)
    if queue_drops > 0:
        gaps.append(f"market_diag_queue_drops={queue_drops}")

    if anomalies or feed_blocks:
        first = anomalies[0] if anomalies else feed_blocks[0]
        result = "DEGRADED"
        reason = first.get("reason_code") or first.get("event_type") or "MARKET_ANOMALY"
    elif not market:
        clean_idle = (
            manifest.get("capture_status") == "COMPLETE_WITHIN_SCOPE"
            and not failed
            and any(row.get("event_type") in {"RUNTIME_STOPPED", "OWNER_STOPPED"} for row in lifecycle)
        )
        result = "NOT_EXERCISED" if clean_idle else "INCONCLUSIVE"
        reason = "NO_MARKET_EVIDENCE" if clean_idle else "MISSING_MARKET_EVIDENCE"
    elif not summaries or sdk_polls <= 0:
        result = "INCONCLUSIVE"
        reason = "NO_MARKET_SUMMARY_SAMPLES"
        if not citations:
            citations = [_cite(row) for row in market[:5]]
    elif gaps:
        result = "INCONCLUSIVE"
        reason = "PARTIAL_CAPTURE"
        if not citations:
            citations = [_cite(summaries[0]), _cite(summaries[-1])] if len(summaries) > 1 else [_cite(summaries[0])]
    else:
        result = "OBSERVED_OK"
        reason = "NO_VIOLATION_IN_OBSERVED_WINDOW"
        citations = [_cite(summaries[0]), _cite(summaries[-1])] if len(summaries) > 1 else [_cite(summaries[0])]

    return {
        "schema_version": "ONLINE_TRADER_MARKET_DIAGNOSTIC_CHECK/1",
        "result": result,
        "reason": reason,
        "market_event_count": len(market),
        "summary_count": len(summaries),
        "sdk_polls_total": sdk_polls,
        "anomaly_count": len(anomalies),
        "feed_block_count": len(feed_blocks),
        "evidence_gaps": gaps,
        "citations": citations,
    }


def check(path):
    try:
        validation, manifest, events, _, _ = load_validated_bundle(path)
    except (OSError, ValueError, BadZipFile, KeyError, json.JSONDecodeError) as error:
        return {
            "schema_version": "ONLINE_TRADER_MARKET_DIAGNOSTIC_CHECK/1",
            "result": "INCONCLUSIVE",
            "reason": type(error).__name__,
            "market_event_count": 0,
            "summary_count": 0,
            "anomaly_count": 0,
            "evidence_gaps": [type(error).__name__],
            "citations": [],
        }
    return evaluate_market(validation, manifest, events)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    args = parser.parse_args(argv)
    print(json.dumps(check(args.bundle), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
