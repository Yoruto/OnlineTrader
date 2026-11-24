"""Automated tests for market-close & exit diagnostic reporting, budget selection, and coverage semantics."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from online_trader.contracts.codec import canonical_bytes
from online_trader.diagnostics import reporter
from online_trader.diagnostics.sink import DiagnosticSink
from online_trader.runtime.target_owner_startup import (
    _build_close_deadlines,
    _maybe_request_market_close,
)
from online_trader.scheduling.calendar import TradingCalendar


class _FakeClock:
    def __init__(self, current: datetime):
        self.current = current

    def now(self) -> datetime:
        return self.current


class _RecordingSink:
    def __init__(self):
        self.records = []

    def record(self, event_type, **kwargs):
        self.records.append((event_type, kwargs))
        return True


def _sample_calendar(contracts=("DCE.l2609", "SHFE.ag2612")):
    rows = []
    for index, code in enumerate(contracts):
        close_utc = "2026-11-24T07:00:00Z" if index == 0 else "2026-11-24T07:15:00Z"
        rows.append({
            "contract_id": code,
            "sessions": [{
                "session_id": f"2026-11-24-day-{index}",
                "trading_day": "2026-11-24",
                "open_utc": "2026-11-24T01:00:00Z",
                "close_utc": close_utc,
                "is_opening_session": True,
                "force_close_at_end": True,
            }],
        })
    return TradingCalendar.from_record({
        "schema_version": "TRADING_CALENDAR/1",
        "calendar_id": "test-cal-2026-11-24",
        "calendar_version": "test-1",
        "complete": True,
        "coverage_from_utc": "2026-11-24T01:00:00Z",
        "coverage_to_utc": "2026-11-24T07:15:01Z",
        "contracts": rows,
    })


def _write_synthetic_stream(
    root: Path,
    *,
    stream_id: str,
    process_role: str,
    process_instance_id: str,
    boot_id: str,
    segments: list[list[dict]],
    created_at_utc: str = "2026-11-24T01:00:00.000000Z",
    finalized: bool = True,
):
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.json").write_bytes(canonical_bytes({
        "schema_version": "ONLINE_TRADER_DIAGNOSTIC_STREAM/1",
        "diagnostic_protocol": "1",
        "stream_id": stream_id,
        "process_role": process_role,
        "process_instance_id": process_instance_id,
        "runtime_boot_id": boot_id,
        "account_pseudonym": None,
        "configuration_revision": "1",
        "created_at_utc": created_at_utc,
    }))
    seq = 0
    seg_meta = []
    for seg_idx, events in enumerate(segments, start=1):
        seg_path = root / f"events-{seg_idx:04}.ndjson"
        lines = []
        first_seq = None
        for ev in events:
            seq += 1
            if first_seq is None:
                first_seq = seq
            row = {
                "schema_version": "ONLINE_TRADER_DIAGNOSTIC_EVENT/1",
                "stream_id": stream_id,
                "event_seq": str(seq),
                "observed_at_utc": ev.get("observed_at_utc", "2026-11-24T06:00:00.000000Z"),
                "monotonic_ns": str(1000 + seq),
                "process_role": process_role,
                "process_instance_id": process_instance_id,
                "runtime_boot_id": ev.get("runtime_boot_id", boot_id),
                "account_pseudonym": None,
                "configuration_revision": "1",
                "component": ev.get("component", "RUNTIME"),
                "event_type": ev.get("event_type", "TICK_HEARTBEAT"),
                "reason_code": None,
                "error_code": None,
                "severity": ev.get("severity", "INFO"),
                "correlation": {},
                "payload": ev.get("payload", {}),
                "diagnostic_protocol": "1",
            }
            lines.append(canonical_bytes(row) + b"\n")
        raw = b"".join(lines)
        seg_path.write_bytes(raw)
        if finalized or seg_idx < len(segments):
            meta = {
                "schema_version": "ONLINE_TRADER_DIAGNOSTIC_SEGMENT/1",
                "stream_id": stream_id,
                "file": seg_path.name,
                "first_event_seq": str(first_seq),
                "last_event_seq": str(seq),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "closed_at_utc": "2026-11-24T07:00:00.000000Z",
            }
            seg_path.with_suffix(".segment.json").write_bytes(canonical_bytes(meta))
            seg_meta.append(meta)
    (root / "health.json").write_bytes(canonical_bytes({
        "schema_version": "ONLINE_TRADER_DIAGNOSTIC_HEALTH/1",
        "stream_id": stream_id,
        "enqueued": seq,
        "persisted": seq,
        "dropped_by_priority": {},
        "write_failures": 0,
        "last_persisted_seq": str(seq) if seq else None,
        "retention_dropped_segments": 0,
        "retention_dropped_events": 0,
        "active": not finalized,
        "clean_shutdown": finalized,
        "snapshot_available": finalized,
        "writer_timeout": False,
        "finalized_at_utc": "2026-11-24T07:00:01.000000Z" if finalized else None,
        "segments": seg_meta,
    }))


class MarketCloseTriggerTests(unittest.TestCase):
    def test_triggers_with_microseconds_and_past_calendar_coverage_with_10s_throttle(self):
        cal = _sample_calendar(("DCE.l2609", "SHFE.ag2612"))
        deadlines = _build_close_deadlines(cal, ("DCE.l2609", "SHFE.ag2612"))
        self.assertEqual(
            deadlines,
            {"2026-11-24": datetime(2026, 11, 24, 7, 15, 0, tzinfo=timezone.utc)},
        )

        # Before latest contract close (07:14:59.999999Z) -> no trigger
        clock = _FakeClock(datetime(2026, 11, 24, 7, 14, 59, 999999, tzinfo=timezone.utc))
        loop_state = {
            "diagnostic_reported_close_keys": set(),
            "diagnostic_close_check_monotonic": 0.0,
        }
        sink = _RecordingSink()
        calls = []
        fired = _maybe_request_market_close(
            close_deadlines=deadlines, clock=clock, loop_state=loop_state,
            boot_id="boot-1", calendar_hash=cal.content_hash,
            diagnostic_sink=sink, diagnostic_report_request=lambda **kw: calls.append(kw),
            now_monotonic=20.0,
        )
        self.assertFalse(fired)
        self.assertEqual(calls, [])

        # Within 10s throttle window -> skipped even if past close
        clock.current = datetime(2026, 11, 24, 7, 15, 0, 500000, tzinfo=timezone.utc)
        fired = _maybe_request_market_close(
            close_deadlines=deadlines, clock=clock, loop_state=loop_state,
            boot_id="boot-1", calendar_hash=cal.content_hash,
            diagnostic_sink=sink, diagnostic_report_request=lambda **kw: calls.append(kw),
            now_monotonic=29.5,
        )
        self.assertFalse(fired)
        self.assertEqual(calls, [])

        # Past close with fractional microseconds AND after 10s throttle -> triggers once
        fired = _maybe_request_market_close(
            close_deadlines=deadlines, clock=clock, loop_state=loop_state,
            boot_id="boot-1", calendar_hash=cal.content_hash,
            diagnostic_sink=sink, diagnostic_report_request=lambda **kw: calls.append(kw),
            now_monotonic=30.0,
        )
        self.assertTrue(fired)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["report_reason"], "MARKET_CLOSE")
        self.assertEqual(sink.records[0][0], "REPORT_CUT_REQUEST")
        self.assertEqual(sink.records[0][1]["payload"]["close_deadline_utc"], "2026-11-24T07:15:00Z")
        self.assertEqual(sink.records[0][1]["payload"]["observed_at_utc"], "2026-11-24T07:15:00.500000Z")

        # Repeated check well past calendar coverage_to_utc -> deduplicated, never fires twice
        clock.current = datetime(2026, 11, 24, 9, 0, 0, 123456, tzinfo=timezone.utc)
        fired = _maybe_request_market_close(
            close_deadlines=deadlines, clock=clock, loop_state=loop_state,
            boot_id="boot-1", calendar_hash=cal.content_hash,
            diagnostic_sink=sink, diagnostic_report_request=lambda **kw: calls.append(kw),
            now_monotonic=100.0,
        )
        self.assertFalse(fired)
        self.assertEqual(len(calls), 1)

    def test_triggers_even_when_started_after_calendar_coverage_without_seeing_active_session(self):
        cal = _sample_calendar(("DCE.l2609",))
        deadlines = _build_close_deadlines(cal, ("DCE.l2609",))
        # Started 2 hours after close, well past coverage_to_utc (07:00:01Z)
        clock = _FakeClock(datetime(2026, 11, 24, 9, 30, 0, 437000, tzinfo=timezone.utc))
        loop_state = {
            "diagnostic_reported_close_keys": set(),
            "diagnostic_close_check_monotonic": 0.0,
        }
        calls = []
        fired = _maybe_request_market_close(
            close_deadlines=deadlines, clock=clock, loop_state=loop_state,
            boot_id="boot-late", calendar_hash=cal.content_hash,
            diagnostic_sink=None, diagnostic_report_request=lambda **kw: calls.append(kw),
            now_monotonic=15.0,
        )
        self.assertTrue(fired)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["trigger_key"], f"boot-late:2026-11-24:{cal.content_hash}")

    def test_market_close_skips_historical_and_unobserved_calendar_days(self):
        cal = TradingCalendar.from_record({
            "schema_version": "TRADING_CALENDAR/1",
            "calendar_id": "multi-day-cal",
            "calendar_version": "test-1",
            "complete": True,
            "coverage_from_utc": "2026-11-23T01:00:00Z",
            "coverage_to_utc": "2026-11-25T07:15:01Z",
            "contracts": [{
                "contract_id": "DCE.l2609",
                "sessions": [
                    {
                        "session_id": "2026-11-23-day", "trading_day": "2026-11-23",
                        "open_utc": "2026-11-23T01:00:00Z", "close_utc": "2026-11-23T07:00:00Z",
                        "is_opening_session": True, "force_close_at_end": True,
                    },
                    {
                        "session_id": "2026-11-24-day", "trading_day": "2026-11-24",
                        "open_utc": "2026-11-24T01:00:00Z", "close_utc": "2026-11-24T07:00:00Z",
                        "is_opening_session": True, "force_close_at_end": True,
                    },
                    {
                        "session_id": "2026-11-25-day", "trading_day": "2026-11-25",
                        "open_utc": "2026-11-25T01:00:00Z", "close_utc": "2026-11-25T07:00:00Z",
                        "is_opening_session": True, "force_close_at_end": True,
                    },
                ],
            }],
        })
        deadlines = _build_close_deadlines(cal, ("DCE.l2609",))
        clock = _FakeClock(datetime(2026, 11, 24, 7, 5, 0, tzinfo=timezone.utc))
        loop_state = {
            "diagnostic_reported_close_keys": set(),
            "diagnostic_close_check_monotonic": 0.0,
        }
        calls = []
        fired = _maybe_request_market_close(
            close_deadlines=deadlines, clock=clock, loop_state=loop_state,
            boot_id="boot-multi", calendar_hash=cal.content_hash,
            diagnostic_sink=None, diagnostic_report_request=lambda **kw: calls.append(kw),
            now_monotonic=20.0, allowed_days={"2026-11-24"},
        )
        self.assertTrue(fired)
        self.assertEqual([c["trigger_key"] for c in calls], [f"boot-multi:2026-11-24:{cal.content_hash}"])


class ReporterEndToEndAndBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime_root = Path(self.temp.name) / "runtime-root"
        self.runtime_root.mkdir(parents=True, exist_ok=True)

    def test_controlled_exit_produces_complete_local_bundle(self):
        diagnostics = self.runtime_root / "diagnostics"
        rt_sink = DiagnosticSink(
            diagnostics, process_role="RUNTIME", process_instance_id="rt-1",
            stream_group="runtime", runtime_boot_id="boot-exit",
        )
        wb_sink = DiagnosticSink(
            diagnostics, process_role="WORKBENCH", process_instance_id="wb-1",
            stream_group="workbench", runtime_boot_id="boot-exit",
        )
        rt_sink.record("RUNTIME_STARTED", component="LIFECYCLE", priority="HIGH")
        wb_sink.record("WORKBENCH_STARTED", component="LIFECYCLE", priority="HIGH")
        rt_sink.finalize(clean_shutdown=True, snapshot_available=True)
        wb_sink.finalize(clean_shutdown=True, snapshot_available=True)

        req_path = reporter.create_report_request(
            self.runtime_root,
            report_reason="CONTROLLED_EXIT",
            runtime_boot_id="boot-exit",
            clean_shutdown=True,
            runtime_process_instance_id="rt-1",
            runtime_view_expected=False,
            workbench_expected=True,
        )
        with patch.object(reporter.time, "sleep", return_value=None):
            rc = reporter.report(self.runtime_root, req_path)
        self.assertEqual(rc, 0)

        done_files = list((diagnostics / "report-requests").glob("*.done.json"))
        self.assertEqual(len(done_files), 1)
        done = json.loads(done_files[0].read_text(encoding="utf-8"))
        self.assertEqual(done["capture_status"], "COMPLETE_WITHIN_SCOPE")
        self.assertEqual(done["upload"]["status"], "LOCAL_ONLY")

        bundle_path = diagnostics / "bundles" / done["bundle_file"]
        self.assertTrue(bundle_path.is_file())
        self.assertTrue(bundle_path.with_suffix(".zip.sha256").is_file())
        with ZipFile(bundle_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["capture_status"], "COMPLETE_WITHIN_SCOPE")
        self.assertEqual(manifest["coverage_semantics"], "OBSERVED_EVENTS")
        self.assertEqual(manifest["requested_at_utc"], done["requested_at_utc"])
        self.assertIsNotNone(manifest["capture_started_at_utc"])
        self.assertIsNotNone(manifest["first_event_at_utc"])
        self.assertIsNotNone(manifest["last_event_at_utc"])
        self.assertEqual(manifest["coverage_start"], manifest["first_event_at_utc"])
        self.assertEqual(manifest["coverage_end"], manifest["last_event_at_utc"])

    def test_market_close_open_stream_is_partial_and_keeps_latest_event_coverage(self):
        diagnostics = self.runtime_root / "diagnostics"
        _write_synthetic_stream(
            diagnostics / "runtime" / "rt-open",
            stream_id="runtime:rt-open:events",
            process_role="RUNTIME",
            process_instance_id="rt-open",
            boot_id="boot-close",
            segments=[[
                {"event_type": "RUNTIME_STARTED", "component": "LIFECYCLE",
                 "observed_at_utc": "2026-11-24T01:00:05.000000Z"},
                # Event written during the 750ms window after requested_at_utc
                {"event_type": "LATE_TICK", "component": "MARKET",
                 "observed_at_utc": "2099-01-01T00:00:05.000000Z"},
            ]],
            created_at_utc="2026-11-24T01:00:00.000000Z",
            finalized=False,
        )
        req_path = reporter.create_report_request(
            self.runtime_root,
            report_reason="MARKET_CLOSE",
            runtime_boot_id="boot-close",
            clean_shutdown=False,
            runtime_process_instance_id="rt-open",
            trigger_key="boot-close:2026-11-24:hash",
            runtime_view_expected=False,
            workbench_expected=False,
        )
        with patch.object(reporter.time, "sleep", return_value=None):
            rc = reporter.report(self.runtime_root, req_path)
        self.assertEqual(rc, 0)
        bundle = next((diagnostics / "bundles").glob("*.zip"))
        with ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["capture_status"], "PARTIAL")
        self.assertEqual(manifest["first_event_at_utc"], "2026-11-24T01:00:05.000000Z")
        self.assertEqual(manifest["last_event_at_utc"], "2099-01-01T00:00:05.000000Z")
        self.assertEqual(manifest["coverage_end"], "2099-01-01T00:00:05.000000Z")
        self.assertGreater(manifest["last_event_at_utc"], manifest["requested_at_utc"])
        self.assertIn("runtime/rt-open:OPEN_PREFIX", manifest["missing_sources"])

    def test_budget_prioritizes_newest_segments_preserves_head_key_events_and_workbench_reserve(self):
        diagnostics = self.runtime_root / "diagnostics"
        pad = "x" * 1600
        # 5 runtime segments (~4KB each = ~20KB total):
        # seg1: startup head; seg2-seg3: middle routine + one CRITICAL error; seg4-seg5: newest fault scene
        rt_segments = [
            [{"event_type": "RUNTIME_STARTED", "component": "LIFECYCLE",
              "observed_at_utc": "2026-11-24T01:00:01.000000Z", "payload": {"tag": "seg1-start"}},
             {"event_type": "TICK", "observed_at_utc": "2026-11-24T01:05:00.000000Z", "payload": {"tag": "seg1-b", "pad": pad}},
             {"event_type": "TICK", "observed_at_utc": "2026-11-24T01:06:00.000000Z", "payload": {"tag": "seg1-c", "pad": pad}}],
            [{"event_type": "TICK", "observed_at_utc": "2026-11-24T02:00:00.000000Z", "payload": {"tag": "seg2-routine", "pad": pad}},
             {"event_type": "OWNER_FAULT", "component": "RUNTIME", "severity": "CRITICAL",
              "observed_at_utc": "2026-11-24T02:05:00.000000Z", "payload": {"tag": "seg2-critical"}}],
            [{"event_type": "TICK", "observed_at_utc": "2026-11-24T03:00:00.000000Z", "payload": {"tag": "seg3-routine-a", "pad": pad}},
             {"event_type": "TICK", "observed_at_utc": "2026-11-24T03:05:00.000000Z", "payload": {"tag": "seg3-routine-b", "pad": pad}}],
            [{"event_type": "TICK", "observed_at_utc": "2026-11-24T06:50:00.000000Z", "payload": {"tag": "seg4-recent", "pad": pad}},
             {"event_type": "TICK", "observed_at_utc": "2026-11-24T06:55:00.000000Z", "payload": {"tag": "seg4-recent-b", "pad": pad}}],
            [{"event_type": "CLOSE_SCENE", "observed_at_utc": "2026-11-24T06:59:50.000000Z", "payload": {"tag": "seg5-newest-a", "pad": pad}},
             {"event_type": "CLOSE_SCENE", "observed_at_utc": "2026-11-24T06:59:59.000000Z", "payload": {"tag": "seg5-newest-b", "pad": pad}}],
        ]
        _write_synthetic_stream(
            diagnostics / "runtime" / "rt-big",
            stream_id="runtime:rt-big:events",
            process_role="RUNTIME",
            process_instance_id="rt-big",
            boot_id="boot-budget",
            segments=rt_segments,
        )
        _write_synthetic_stream(
            diagnostics / "workbench" / "wb-ui",
            stream_id="workbench:wb-ui:events",
            process_role="WORKBENCH",
            process_instance_id="wb-ui",
            boot_id="boot-budget",
            segments=[[
                {"event_type": "WORKBENCH_STARTED", "component": "LIFECYCLE",
                 "observed_at_utc": "2026-11-24T06:59:55.000000Z", "payload": {"tag": "wb-evidence"}},
            ]],
        )

        req_path = reporter.create_report_request(
            self.runtime_root,
            report_reason="CONTROLLED_EXIT",
            runtime_boot_id="boot-budget",
            clean_shutdown=True,
            runtime_process_instance_id="rt-big",
            runtime_view_expected=False,
            workbench_expected=True,
        )
        # Tight 12 KB total budget (runtime gets ~11 KB, workbench reserves its ~500 B)
        with (
            patch.object(reporter, "_MAX_BUNDLE_INPUT", 12 * 1024),
            patch.object(reporter, "_WORKBENCH_RESERVE", 4 * 1024),
            patch.object(reporter, "_HEAD_RESERVE", 2500),
            patch.object(reporter, "_KEY_EVENT_RESERVE", 1500),
            patch.object(reporter.time, "sleep", return_value=None),
        ):
            rc = reporter.report(self.runtime_root, req_path)
        self.assertEqual(rc, 0)
        bundle = next((diagnostics / "bundles").glob("*.zip"))
        with ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            rt_rows = [
                json.loads(line)
                for line in archive.read("runtime/rt-big/events-selected.ndjson").splitlines()
            ]
            wb_rows = [
                json.loads(line)
                for line in archive.read("workbench/wb-ui/events-selected.ndjson").splitlines()
            ]

        tags = [row["payload"].get("tag") for row in rt_rows]
        # Newest segment MUST be kept
        self.assertIn("seg5-newest-a", tags)
        self.assertIn("seg5-newest-b", tags)
        # Startup head MUST be kept
        self.assertIn("seg1-start", tags)
        # Critical event from skipped middle segment MUST be preserved
        self.assertIn("seg2-critical", tags)
        # Routine middle segment MUST be trimmed
        self.assertNotIn("seg3-routine-a", tags)
        # Workbench MUST NOT be starved by Runtime
        self.assertEqual([row["payload"].get("tag") for row in wb_rows], ["wb-evidence"])
        # Trimming is marked honestly without falsely marking source_sequence_complete=False
        rt_stream = next(s for s in manifest["streams"] if s["process_role"] == "RUNTIME")
        self.assertTrue(rt_stream["budget_trimmed"])
        self.assertTrue(rt_stream["source_sequence_complete"])
        self.assertEqual(manifest["capture_status"], "PARTIAL")

    def test_oversized_single_segment_reads_tail_instead_of_head(self):
        diagnostics = self.runtime_root / "diagnostics"
        pad = "z" * 900
        events = [
            {"event_type": "RUNTIME_STARTED", "component": "LIFECYCLE",
             "observed_at_utc": "2026-11-24T01:00:01.000000Z", "payload": {"tag": "old-head", "pad": pad}},
            {"event_type": "TICK", "observed_at_utc": "2026-11-24T02:00:00.000000Z", "payload": {"tag": "mid-1", "pad": pad}},
            {"event_type": "TICK", "observed_at_utc": "2026-11-24T03:00:00.000000Z", "payload": {"tag": "mid-2", "pad": pad}},
            {"event_type": "OWNER_STOPPED", "component": "LIFECYCLE",
             "observed_at_utc": "2026-11-24T06:59:59.000000Z", "payload": {"tag": "newest-tail"}},
        ]
        _write_synthetic_stream(
            diagnostics / "runtime" / "rt-oversize",
            stream_id="runtime:rt-oversize:events",
            process_role="RUNTIME",
            process_instance_id="rt-oversize",
            boot_id="boot-oversize",
            segments=[events],
        )
        req_path = reporter.create_report_request(
            self.runtime_root,
            report_reason="CONTROLLED_EXIT",
            runtime_boot_id="boot-oversize",
            clean_shutdown=True,
            runtime_process_instance_id="rt-oversize",
            runtime_view_expected=False,
            workbench_expected=False,
        )
        with (
            patch.object(reporter, "_MAX_SOURCE_FILE", 2500),
            patch.object(reporter, "_MAX_BUNDLE_INPUT", 64 * 1024),
            patch.object(reporter.time, "sleep", return_value=None),
        ):
            rc = reporter.report(self.runtime_root, req_path)
        self.assertEqual(rc, 0)
        bundle = next((diagnostics / "bundles").glob("*.zip"))
        with ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            rt_rows = [
                json.loads(line)
                for line in archive.read("runtime/rt-oversize/events-selected.ndjson").splitlines()
            ]
        tags = [row["payload"].get("tag") for row in rt_rows]
        self.assertIn("newest-tail", tags)
        self.assertNotIn("old-head", tags)
        self.assertIn("runtime/rt-oversize/events-0001.ndjson:SIZE_LIMIT_TAIL", manifest["missing_sources"])

    def test_historical_workbench_logs_do_not_consume_runtime_budget(self):
        diagnostics = self.runtime_root / "diagnostics"
        pad = "w" * 1600
        # 2 old-boot Workbench streams totaling ~8 KB (> 4 KB reserve)
        for idx in (1, 2):
            _write_synthetic_stream(
                diagnostics / "workbench" / f"wb-old-{idx}",
                stream_id=f"workbench:wb-old-{idx}:events",
                process_role="WORKBENCH",
                process_instance_id=f"wb-old-{idx}",
                boot_id="boot-old",
                segments=[[
                    {"event_type": "WORKBENCH_STARTED", "component": "LIFECYCLE",
                     "payload": {"tag": f"old-{idx}-a", "pad": pad}},
                    {"event_type": "WORKBENCH_STOPPED", "component": "LIFECYCLE",
                     "payload": {"tag": f"old-{idx}-b", "pad": pad}},
                ]],
            )
        # Current boot Runtime stream is ~9 KB (fits in 12 KB total, exceeds 8 KB if old WB ate 4 KB)
        rt_events = [
            {"event_type": "RUNTIME_STARTED", "component": "LIFECYCLE",
             "payload": {"tag": f"rt-{i}", "pad": pad}}
            for i in range(4)
        ]
        _write_synthetic_stream(
            diagnostics / "runtime" / "rt-curr",
            stream_id="runtime:rt-curr:events",
            process_role="RUNTIME",
            process_instance_id="rt-curr",
            boot_id="boot-current",
            segments=[rt_events],
        )
        req_path = reporter.create_report_request(
            self.runtime_root,
            report_reason="CONTROLLED_EXIT",
            runtime_boot_id="boot-current",
            clean_shutdown=True,
            runtime_process_instance_id="rt-curr",
            runtime_view_expected=False,
            workbench_expected=False,
        )
        with (
            patch.object(reporter, "_MAX_BUNDLE_INPUT", 12 * 1024),
            patch.object(reporter, "_WORKBENCH_RESERVE", 4 * 1024),
            patch.object(reporter.time, "sleep", return_value=None),
        ):
            rc = reporter.report(self.runtime_root, req_path)
        self.assertEqual(rc, 0)
        bundle = next((diagnostics / "bundles").glob("*.zip"))
        with ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            rt_rows = [
                json.loads(line)
                for line in archive.read("runtime/rt-curr/events-selected.ndjson").splitlines()
            ]
        self.assertEqual([row["payload"].get("tag") for row in rt_rows], ["rt-0", "rt-1", "rt-2", "rt-3"])
        rt_stream = next(s for s in manifest["streams"] if s["process_role"] == "RUNTIME")
        self.assertFalse(rt_stream["budget_trimmed"])

    def test_key_event_priority_keeps_order_market_anomaly_and_command_events(self):
        diagnostics = self.runtime_root / "diagnostics"
        pad = "k" * 2600
        rt_segments = [
            [{"event_type": "RUNTIME_STARTED", "component": "LIFECYCLE",
              "payload": {"tag": "seg1-start"}},
             {"event_type": "TICK", "payload": {"tag": "seg1-pad", "pad": pad}}],
            [{"event_type": "ORDER_BLOCKED", "component": "ORDER", "severity": "WARNING",
              "payload": {"tag": "seg2-order-blocked"}},
             {"event_type": "MARKET_ANOMALY", "component": "MARKET", "severity": "WARNING",
              "payload": {"tag": "seg2-market-anomaly"}},
             {"event_type": "MARKET_SUMMARY", "component": "MARKET", "severity": "INFO",
              "payload": {"tag": "seg2-market-summary", "pad": pad}},
             {"event_type": "RUNTIME_COMMAND_ACCEPTED", "component": "COMMAND", "severity": "INFO",
              "payload": {"tag": "seg2-command"}}],
            [{"event_type": "TICK", "payload": {"tag": "seg3-routine-a", "pad": pad}},
             {"event_type": "TICK", "payload": {"tag": "seg3-routine-b", "pad": pad}}],
            [{"event_type": "CLOSE_SCENE", "payload": {"tag": "seg4-newest-a", "pad": pad}},
             {"event_type": "CLOSE_SCENE", "payload": {"tag": "seg4-newest-b", "pad": pad}}],
        ]
        _write_synthetic_stream(
            diagnostics / "runtime" / "rt-key",
            stream_id="runtime:rt-key:events",
            process_role="RUNTIME",
            process_instance_id="rt-key",
            boot_id="boot-key",
            segments=rt_segments,
        )
        req_path = reporter.create_report_request(
            self.runtime_root,
            report_reason="CONTROLLED_EXIT",
            runtime_boot_id="boot-key",
            clean_shutdown=True,
            runtime_process_instance_id="rt-key",
            runtime_view_expected=False,
            workbench_expected=False,
        )
        with (
            patch.object(reporter, "_MAX_BUNDLE_INPUT", 12 * 1024),
            patch.object(reporter, "_HEAD_RESERVE", 1024),
            patch.object(reporter, "_KEY_EVENT_RESERVE", 2500),
            patch.object(reporter.time, "sleep", return_value=None),
        ):
            rc = reporter.report(self.runtime_root, req_path)
        self.assertEqual(rc, 0)
        bundle = next((diagnostics / "bundles").glob("*.zip"))
        with ZipFile(bundle) as archive:
            rt_rows = [
                json.loads(line)
                for line in archive.read("runtime/rt-key/events-selected.ndjson").splitlines()
            ]
        tags = [row["payload"].get("tag") for row in rt_rows]
        self.assertIn("seg2-order-blocked", tags)
        self.assertIn("seg2-market-anomaly", tags)
        self.assertIn("seg2-command", tags)
        self.assertNotIn("seg2-market-summary", tags)


if __name__ == "__main__":
    unittest.main()
