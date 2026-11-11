"""Exact clock parsing and discontinuity measurements, without sleeps."""

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from online_trader.errors import TraderError
from online_trader.scheduling.clock import ClockGuard, ClockSample, SystemClock


def record(utc="2026-11-09T01:00:00Z", ns="1000000000"):
    return {"now_utc": utc, "monotonic_ns": ns}


class ClockSampleTests(unittest.TestCase):
    def test_sample_preserves_explicit_evidence_and_exposes_exact_types(self):
        for text in ("2026-11-09T01:00:00Z", "2026-11-09T01:00:00.1Z",
                     "2026-11-09T01:00:00.123456Z", "2026-11-09T01:00:00.000000Z"):
            with self.subTest(timestamp=text):
                source = record(text, "9223372036854775807")
                sample = ClockSample.from_record(source)
                self.assertEqual(sample.to_record(), source)
                self.assertEqual(sample.utc.tzinfo, timezone.utc)
                self.assertEqual(sample.monotonic, 9223372036854775807)
                source["now_utc"] = "changed"
                self.assertEqual(sample.now_utc, text)

    def test_zero_monotonic_origin_is_valid(self):
        self.assertEqual(ClockSample.from_record(record(ns="0")).monotonic, 0)

    def test_bad_timestamps_are_not_normalized_into_valid_facts(self):
        for text in (None, True, 42, "2026-11-09 01:00:00Z", "2026-11-09T01:00:00+00:00",
                     "2026-11-09T01:00:00z", "2026-11-09T01:00:00.Z", "2026-11-09T01:00:00.1234567Z",
                     "2026-02-29T01:00:00Z", "2026-11-09T24:00:00Z", "2026-11-09T01:00:60Z",
                     "0000-11-09T01:00:00Z", "2026-11-09T01:00:00Z\n"):
            with self.subTest(timestamp=text), self.assertRaises(TraderError) as caught:
                ClockSample.from_record(record(text))
            self.assertEqual(caught.exception.code, "E_CLOCK_INPUT")

    def test_bad_nanoseconds_cannot_use_number_or_exponent_coercion(self):
        for ns in (None, 1, True, 1.0, "", "-0", "-1", "01", "+1", "1.0", "1e3",
                   "9223372036854775808", "99999999999999999999", "1\n", "１"):
            with self.subTest(nanoseconds=ns), self.assertRaises(TraderError):
                ClockSample.from_record(record(ns=ns))

    def test_unknown_or_missing_sample_fields_are_refused(self):
        for source in (None, [], {}, {"now_utc": "2026-11-09T01:00:00Z"}, record() | {"fresh": True}):
            with self.subTest(source=source), self.assertRaises(TraderError):
                ClockSample.from_record(source)

    def test_direct_constructor_has_the_same_validation(self):
        with self.assertRaises(TraderError):
            ClockSample("2026-11-09T01:00:00+08:00", "1")

    def test_system_clock_reads_a_sample_without_sleep_or_product_effect(self):
        with patch("online_trader.scheduling.clock.time.monotonic_ns", return_value=123456789):
            sample = ClockSample.from_record(SystemClock().sample())
        self.assertEqual(sample.monotonic, 123456789)
        self.assertIsInstance(sample.utc, datetime)


class ClockGuardTests(unittest.TestCase):
    def test_first_and_duplicate_samples_do_not_create_elapsed_time(self):
        guard = ClockGuard()
        first = guard.observe(record())
        duplicate = guard.observe(record())
        self.assertEqual(first, duplicate)
        self.assertTrue(first["valid"])
        self.assertEqual(first["elapsed_ms"], 0)
        self.assertEqual(first["wall_delta_ms"], "0")
        self.assertEqual(first["drift_ms"], "0")
        self.assertEqual(first["reasons"], [])

    def test_elapsed_is_monotonic_delta_not_the_wall_clock_delta(self):
        guard = ClockGuard(max_wall_drift_ms=5)
        guard.observe(record())
        value = guard.observe(record("2026-11-09T01:00:01.002Z", "2000000000"))
        self.assertTrue(value["valid"])
        self.assertEqual(value["elapsed_ms"], 1000)
        self.assertEqual(value["wall_delta_ms"], "1002")
        self.assertEqual(value["drift_ms"], "2")

    def test_exact_nanosecond_and_microsecond_drift_is_not_float_rounded(self):
        guard = ClockGuard(max_wall_drift_ms=0)
        guard.observe(record(ns="9223372036854774806"))
        result = guard.observe(record("2026-11-09T01:00:00.000001Z", "9223372036854775807"))
        self.assertFalse(result["valid"])
        self.assertEqual(result["wall_delta_ms"], "0.001")
        self.assertEqual(result["drift_ms"], "-0.000001")
        self.assertEqual(result["reasons"], ["CLOCK_DRIFT_EXCEEDED"])

    def test_submillisecond_elapsed_is_floor_and_accepted_baseline_advances(self):
        guard = ClockGuard()
        guard.observe(record(ns="0"))
        first = guard.observe(record("2026-11-09T01:00:00.000999Z", "999999"))
        second = guard.observe(record("2026-11-09T01:00:00.002Z", "2000000"))
        self.assertEqual(first["elapsed_ms"], 0)
        self.assertEqual(first["drift_ms"], "-0.000999")
        self.assertEqual(second["elapsed_ms"], 1)
        self.assertEqual(second["drift_ms"], "0.000999")

    def test_drift_boundary_is_inclusive_and_beyond_is_latched(self):
        guard = ClockGuard(max_wall_drift_ms=1000)
        guard.observe(record(ns="0"))
        boundary = guard.observe(record("2026-11-09T01:00:02Z", "1000000000"))
        self.assertTrue(boundary["valid"])
        beyond = guard.observe(record("2026-11-09T01:00:04.000001Z", "2000000000"))
        self.assertFalse(beyond["valid"])
        self.assertTrue(guard.faulted)
        self.assertEqual(beyond["drift_ms"], "1000.001")
        self.assertEqual(beyond["elapsed_ms"], 0)

    def test_wall_regression_latches_even_when_drift_within_tolerance(self):
        guard = ClockGuard()
        guard.observe(record())
        result = guard.observe(record("2026-11-09T00:59:59.999999Z", "1000000001"))
        self.assertFalse(result["valid"])
        self.assertIn("CLOCK_WALL_REGRESSION", result["reasons"])
        self.assertEqual(result["wall_delta_ms"], "-0.001")

    def test_monotonic_regression_latches_even_when_utc_moves_forward(self):
        guard = ClockGuard()
        guard.observe(record())
        result = guard.observe(record("2026-11-09T01:00:00.000001Z", "999999999"))
        self.assertFalse(result["valid"])
        self.assertIn("CLOCK_MONOTONIC_REGRESSION", result["reasons"])

    def test_both_regressions_are_reported(self):
        guard = ClockGuard()
        guard.observe(record())
        result = guard.observe(record("2026-11-09T00:59:59Z", "0"))
        self.assertEqual(result["reasons"], ["CLOCK_WALL_REGRESSION", "CLOCK_MONOTONIC_REGRESSION"])

    def test_later_consistent_observation_does_not_clear_the_first_fault(self):
        guard = ClockGuard(max_wall_drift_ms=1000)
        guard.observe(record())
        first = guard.observe(record("2026-11-09T01:00:05Z", "2000000000"))
        later = guard.observe(record("2026-11-09T01:00:06Z", "7000000000"))
        self.assertFalse(first["valid"])
        self.assertFalse(later["valid"])
        self.assertEqual(later["reasons"], ["CLOCK_DRIFT_EXCEEDED", "CLOCK_FAULT_LATCHED"])
        self.assertEqual(later["elapsed_ms"], 0)
        self.assertEqual(later["wall_delta_ms"], "6000")

    def test_large_interval_is_valid_when_both_clocks_agree(self):
        guard = ClockGuard(max_wall_drift_ms=0)
        guard.observe(record("2026-11-09T00:00:00Z", "0"))
        result = guard.observe(record("2026-12-09T00:00:00Z", "2592000000000000"))
        self.assertTrue(result["valid"])
        self.assertEqual(result["elapsed_ms"], 2592000000)
        self.assertEqual(result["drift_ms"], "0")

    def test_explicit_reset_clears_only_local_measurement_latch(self):
        guard = ClockGuard()
        guard.observe(record())
        guard.observe(record("2026-11-09T01:10:00Z", "2000000000"))
        reset = guard.reset(record("2026-11-09T01:10:00Z", "100"))
        self.assertFalse(guard.faulted)
        self.assertTrue(reset["valid"])
        self.assertEqual(reset["elapsed_ms"], 0)
        self.assertEqual(set(reset), {"valid", "elapsed_ms", "reasons", "now_utc", "monotonic_ns", "wall_delta_ms", "drift_ms"})
        result = guard.observe(record("2026-11-09T01:10:01Z", "1000000100"))
        self.assertEqual(result["elapsed_ms"], 1000)

    def test_invalid_record_does_not_reset_or_advance_measurement(self):
        guard = ClockGuard()
        guard.observe(record())
        with self.assertRaises(TraderError):guard.observe(record(ns="1.0"))
        result = guard.observe(record("2026-11-09T01:00:01Z", "2000000000"))
        self.assertTrue(result["valid"])
        self.assertEqual(result["elapsed_ms"], 1000)
        guard.observe(record("2026-11-09T01:10:00Z", "3000000000"))
        with self.assertRaises(TraderError):guard.reset(record(ns="-1"))
        self.assertTrue(guard.faulted)

    def test_tolerance_is_explicit_bounded_integer_and_zero_is_valid(self):
        self.assertEqual(ClockGuard(0).max_wall_drift_ms, 0)
        for value in (-1, True, 1.0, "1000", None, 2147483648):
            with self.subTest(tolerance=value), self.assertRaises(TraderError):ClockGuard(value)


if __name__ == "__main__":unittest.main()
