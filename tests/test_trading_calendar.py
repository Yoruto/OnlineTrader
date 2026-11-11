"""Explicit calendar facts never infer a trading day or a safe closed period."""

import copy
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError
from online_trader.scheduling.calendar import TradingCalendar


CONTRACT = "DCE.p2701"


def session(identity, opened, closed, *, day="2026-11-09", opening=False, force=False):
    return {"session_id": identity, "trading_day": day, "open_utc": opened, "close_utc": closed,
            "is_opening_session": opening, "force_close_at_end": force}


def record():
    return {"schema_version": "TRADING_CALENDAR/1", "calendar_id": "calendar-offline",
        "calendar_version": "v1", "complete": True,
        "coverage_from_utc": "2026-11-08T00:00:00Z", "coverage_to_utc": "2026-11-11T00:00:00Z",
        "contracts": [{"contract_id": CONTRACT, "sessions": [
            session("night", "2026-11-08T13:00:00Z", "2026-11-08T18:30:00Z", opening=True, force=True),
            session("day-open", "2026-11-09T01:00:00Z", "2026-11-09T02:15:00Z", opening=True),
            session("morning-return", "2026-11-09T02:30:00Z", "2026-11-09T03:30:00Z"),
            session("afternoon", "2026-11-09T05:30:00Z", "2026-11-09T07:00:00Z", force=True),
        ]}]}


class TradingCalendarTests(unittest.TestCase):
    def test_active_window_includes_open_and_excludes_close(self):
        calendar = TradingCalendar.from_record(record())
        start = calendar.query(CONTRACT, "2026-11-09T01:00:00Z")
        self.assertTrue(start["complete"]); self.assertTrue(start["is_trading"])
        self.assertEqual(start["session"]["session_id"], "day-open")
        self.assertEqual(start["since_open_ms"], 0)
        self.assertEqual(start["until_close_ms"], 75 * 60 * 1000)
        end = calendar.query(CONTRACT, "2026-11-09T02:15:00Z")
        self.assertTrue(end["complete"]); self.assertFalse(end["is_trading"])
        self.assertIsNone(end["session"])
        self.assertEqual(end["next_session"]["session_id"], "morning-return")

    def test_midday_break_is_known_closed_and_does_not_relabel_day(self):
        result = TradingCalendar.from_record(record()).query(CONTRACT, "2026-11-09T04:00:00Z")
        self.assertEqual(result["quality"], "COMPLETE")
        self.assertFalse(result["is_trading"])
        self.assertIsNone(result["trading_day"])
        self.assertIsNone(result["next_forced_close_utc"])
        self.assertFalse(result["next_session"]["is_opening_session"])

    def test_night_session_uses_the_explicit_cabinet_day_and_crosses_local_midnight(self):
        calendar = TradingCalendar.from_record(record())
        # The session is Sunday night and Monday morning in Shanghai. No weekday
        # heuristic can establish this fact; the fixture labels its day explicitly.
        result = calendar.query(CONTRACT, "2026-11-08T17:00:00Z")
        self.assertTrue(result["is_trading"])
        self.assertEqual(result["trading_day"], "2026-11-09")
        self.assertEqual(result["next_forced_close_utc"], "2026-11-08T18:30:00Z")

    def test_utc_midnight_does_not_change_an_explicit_day_label(self):
        value = record()
        value["contracts"][0]["sessions"] = [session("cross-utc", "2026-11-09T23:00:00Z",
            "2026-11-10T01:00:00Z", day="2026-11-12", force=True)]
        calendar = TradingCalendar.from_record(value)
        for timestamp in ("2026-11-09T23:59:59Z", "2026-11-10T00:00:00Z"):
            self.assertEqual(calendar.query(CONTRACT, timestamp)["trading_day"], "2026-11-12")

    def test_explicit_empty_sessions_prove_covered_holiday_only(self):
        value = record(); value["contracts"][0]["sessions"] = []
        calendar = TradingCalendar.from_record(value)
        result = calendar.query(CONTRACT, "2026-11-09T01:05:00Z")
        self.assertTrue(result["complete"]); self.assertFalse(result["is_trading"])
        self.assertIsNone(result["next_session"])
        self.assertIsNone(calendar.query("DCE.l2701", "2026-11-09T01:05:00Z")["is_trading"])

    def test_absent_contract_is_unknown_even_in_a_complete_calendar(self):
        result = TradingCalendar.from_record(record()).query("DCE.l2701", "2026-11-09T01:05:00Z")
        self.assertFalse(result["complete"]); self.assertIsNone(result["is_trading"])
        self.assertEqual(result["reasons"], ["CONTRACT_COVERAGE_UNKNOWN"])

    def test_coverage_end_is_exclusive_and_outside_is_unknown(self):
        calendar = TradingCalendar.from_record(record())
        for timestamp in ("2026-11-07T23:59:59Z", "2026-11-11T00:00:00Z", "2026-11-12T01:00:00Z"):
            with self.subTest(timestamp=timestamp):
                result = calendar.query(CONTRACT, timestamp)
                self.assertIsNone(result["is_trading"])
                self.assertEqual(result["reasons"], ["CALENDAR_COVERAGE_UNKNOWN"])
        self.assertTrue(calendar.query(CONTRACT, "2026-11-08T00:00:00Z")["complete"])

    def test_incomplete_calendar_cannot_claim_an_active_session(self):
        value = record(); value["complete"] = False
        result = TradingCalendar.from_record(value).query(CONTRACT, "2026-11-09T01:05:00Z")
        self.assertFalse(result["complete"]); self.assertIsNone(result["session"])
        self.assertIsNone(result["is_trading"])
        self.assertEqual(result["reasons"], ["CALENDAR_INCOMPLETE"])

    def test_query_and_session_have_bound_calendar_identity(self):
        calendar = TradingCalendar.from_record(record())
        result = calendar.query(CONTRACT, "2026-11-09T01:05:00Z")
        self.assertEqual(result["calendar_hash"], calendar.content_hash)
        self.assertEqual(result["calendar_id"], calendar.calendar_id)
        self.assertEqual(result["calendar_version"], calendar.calendar_version)
        for field in ("calendar_id", "calendar_version", "calendar_hash"):
            self.assertEqual(result["session"][field], result[field])
            self.assertEqual(result["next_session"][field], result[field])
        canonical_bytes(result)

    def test_input_and_output_mutations_do_not_change_calendar_facts(self):
        original = record(); calendar = TradingCalendar.from_record(original)
        digest = calendar.content_hash
        original["contracts"][0]["sessions"][0]["trading_day"] = "2099-01-01"
        output = calendar.to_record(); output["complete"] = False
        result = calendar.query(CONTRACT, "2026-11-09T01:05:00Z")
        result["session"]["session_id"] = "forged"
        self.assertEqual(calendar.content_hash, digest)
        self.assertTrue(calendar.to_record()["complete"])
        self.assertEqual(calendar.query(CONTRACT, "2026-11-09T01:05:00Z")["session"]["session_id"], "day-open")
        with self.assertRaises(FrozenInstanceError):
            calendar._record_bytes = b"{}"
        with self.assertRaises(TypeError):TradingCalendar()

    def test_first_forced_endpoint_skips_intraday_recesses(self):
        calendar = TradingCalendar.from_record(record())
        morning = calendar.query(CONTRACT, "2026-11-09T02:14:40Z")
        self.assertFalse(morning["session"]["force_close_at_end"])
        self.assertEqual(morning["until_close_ms"], 20_000)
        self.assertEqual(morning["next_forced_close_utc"], "2026-11-09T07:00:00Z")
        afternoon = calendar.query(CONTRACT, "2026-11-09T06:59:40Z")
        self.assertTrue(afternoon["session"]["force_close_at_end"])
        self.assertEqual(afternoon["next_forced_close_utc"], "2026-11-09T07:00:00Z")

    def test_forced_endpoint_cannot_be_borrowed_from_a_different_day(self):
        value = record()
        value["contracts"][0]["sessions"][-1]["trading_day"] = "2026-11-10"
        result = TradingCalendar.from_record(value).query(CONTRACT, "2026-11-09T01:05:00Z")
        self.assertIsNone(result["next_forced_close_utc"])

    def test_unknown_top_or_session_fields_and_unsupported_version_are_refused(self):
        variants = []
        value = record(); value["callback"] = "anything"; variants.append(value)
        value = record(); del value["complete"]; variants.append(value)
        value = record(); value["schema_version"] = "TRADING_CALENDAR/2"; variants.append(value)
        value = record(); value["contracts"][0]["sessions"][0]["weekday"] = 7; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(TraderError):TradingCalendar.from_record(value)

    def test_dates_and_boolean_flags_must_be_explicit_and_well_typed(self):
        for field, bad in (("trading_day", "20261109"), ("trading_day", "2026-02-30"),
                           ("is_opening_session", 1), ("force_close_at_end", "true")):
            value = record(); value["contracts"][0]["sessions"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(TraderError):TradingCalendar.from_record(value)
        value = record(); value["complete"] = 1
        with self.assertRaises(TraderError):TradingCalendar.from_record(value)
        value = record(); del value["contracts"][0]["sessions"][0]["force_close_at_end"]
        with self.assertRaises(TraderError):TradingCalendar.from_record(value)

    def test_timestamps_require_real_utc_z_with_at_most_six_fractional_digits(self):
        for timestamp in ("2026-11-09T01:00:00+00:00", "2026-11-09T01:00:00", "2026-11-09 01:00:00Z",
                          "2026-02-30T01:00:00Z", "2026-11-09T25:00:00Z", "2026-11-09T01:00:00.1234567Z"):
            with self.subTest(timestamp=timestamp), self.assertRaises(TraderError):
                TradingCalendar.from_record(record()).query(CONTRACT, timestamp)

    def test_session_order_overlap_and_duplicate_identity_are_refused(self):
        for kind in ("unordered", "overlap", "duplicate"):
            value = record(); rows = value["contracts"][0]["sessions"]
            if kind == "unordered":rows[1], rows[2] = rows[2], rows[1]
            elif kind == "overlap":rows[2]["open_utc"] = "2026-11-09T02:00:00Z"
            else:rows[2]["session_id"] = rows[1]["session_id"]
            with self.subTest(kind=kind), self.assertRaises(TraderError):TradingCalendar.from_record(value)

    def test_adjacent_sessions_are_valid_and_boundary_belongs_to_the_next(self):
        value = record(); value["contracts"][0]["sessions"][2]["open_utc"] = "2026-11-09T02:15:00Z"
        result = TradingCalendar.from_record(value).query(CONTRACT, "2026-11-09T02:15:00Z")
        self.assertTrue(result["is_trading"])
        self.assertEqual(result["session"]["session_id"], "morning-return")

    def test_session_bounds_must_be_forward_and_inside_coverage(self):
        for key, value in (("open_utc", "2026-11-07T13:00:00Z"), ("close_utc", "2026-11-12T00:00:00Z"),
                           ("close_utc", "2026-11-08T13:00:00Z")):
            data = record(); data["contracts"][0]["sessions"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(TraderError):TradingCalendar.from_record(data)
        data = record(); data["coverage_to_utc"] = data["coverage_from_utc"]
        with self.assertRaises(TraderError):TradingCalendar.from_record(data)

    def test_contract_coverage_cannot_be_duplicated_but_venue_sessions_can_coincide(self):
        value = record(); value["contracts"].append(copy.deepcopy(value["contracts"][0]))
        with self.assertRaises(TraderError):TradingCalendar.from_record(value)
        value["contracts"][1]["contract_id"] = "DCE.l2701"
        calendar = TradingCalendar.from_record(value)
        self.assertEqual(calendar.query(CONTRACT, "2026-11-09T01:05:00Z")["session"],
                         calendar.query("DCE.l2701", "2026-11-09T01:05:00Z")["session"])

    def test_active_duration_excludes_every_explicit_break(self):
        calendar = TradingCalendar.from_record(record())
        self.assertEqual(calendar.active_milliseconds(CONTRACT, "2026-11-09T01:00:00Z", "2026-11-09T07:00:00Z"),
                         (75 + 60 + 90) * 60 * 1000)
        self.assertEqual(calendar.active_milliseconds(CONTRACT, "2026-11-09T03:30:00Z", "2026-11-09T05:30:00Z"), 0)
        self.assertEqual(calendar.active_milliseconds(CONTRACT, "2026-11-09T02:00:00Z", "2026-11-09T02:45:00Z"),
                         30 * 60 * 1000)

    def test_active_duration_aggregates_microseconds_before_flooring(self):
        value = record(); value["contracts"][0]["sessions"] = [
            session("fraction-a", "2026-11-09T01:00:00.0001Z", "2026-11-09T01:00:00.0009Z"),
            session("fraction-b", "2026-11-09T01:00:00.0011Z", "2026-11-09T01:00:00.0019Z")]
        calendar = TradingCalendar.from_record(value)
        self.assertEqual(calendar.active_milliseconds(CONTRACT, "2026-11-09T01:00:00Z", "2026-11-09T01:00:00.002Z"), 1)
        result = calendar.query(CONTRACT, "2026-11-09T01:00:00.0005Z")
        self.assertEqual((result["since_open_ms"], result["until_close_ms"]), (0, 0))

    def test_active_duration_needs_complete_scope_and_accepts_a_zero_interval(self):
        calendar = TradingCalendar.from_record(record())
        self.assertEqual(calendar.active_milliseconds(CONTRACT, "2026-11-11T00:00:00Z", "2026-11-11T00:00:00Z"), 0)
        for contract, start, end in (("DCE.l2701", "2026-11-09T01:00:00Z", "2026-11-09T02:00:00Z"),
                (CONTRACT, "2026-11-07T00:00:00Z", "2026-11-09T02:00:00Z"),
                (CONTRACT, "2026-11-09T01:00:00Z", "2026-11-12T00:00:00Z"),
                (CONTRACT, "2026-11-09T02:00:00Z", "2026-11-09T01:00:00Z")):
            with self.subTest(contract=contract, start=start), self.assertRaises(TraderError):
                calendar.active_milliseconds(contract, start, end)
        value = record(); value["complete"] = False
        with self.assertRaises(TraderError):
            TradingCalendar.from_record(value).active_milliseconds(CONTRACT, "2026-11-09T01:00:00Z", "2026-11-09T02:00:00Z")

    def test_resource_capacity_is_checked_before_accepting_calendar_facts(self):
        with patch("online_trader.scheduling.calendar.MAX_CONTRACTS", 0), self.assertRaises(TraderError):
            TradingCalendar.from_record(record())
        with patch("online_trader.scheduling.calendar.MAX_SESSIONS_PER_CONTRACT", 3), self.assertRaises(TraderError):
            TradingCalendar.from_record(record())
        with patch("online_trader.scheduling.calendar.MAX_SESSIONS", 3), self.assertRaises(TraderError):
            TradingCalendar.from_record(record())
        with patch("online_trader.scheduling.calendar.MAX_RECORD_BYTES", 100), self.assertRaises(TraderError):
            TradingCalendar.from_record(record())


if __name__ == "__main__":unittest.main()
