from datetime import date, timedelta
from fractions import Fraction
from io import StringIO
from pathlib import Path
import csv
import json
import tempfile
import unittest
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.offsets import (DailyDataset, DailyCsvError, FORMULAS, COEFFICIENTS,
    STOPS, calculate_offsets, export_calculation, formula_source, load_daily, MAX_DAILY_BYTES)
from online_trader.errors import TraderError


def daily_csv(series, ranges, *, first="2026-09-01", rows=None):
    stream = StringIO(newline=""); writer = csv.writer(stream)
    writer.writerow(("series_id", "date", "high", "low", "close"))
    for index, amplitude in enumerate(ranges):
        day = (date.fromisoformat(first) + timedelta(days=index)).isoformat()
        writer.writerow([series, day, 100 + amplitude, 100, 100])
    if rows: writer.writerows(rows)
    return stream.getvalue()


def single_profile(name="棕榈油", code="DCE.p2701", **auto):
    record = load_confirmed_baseline()[0].to_record()
    symbol = record["settings"]["symbols"][0]
    symbol.update(name=name, code=code)
    record["settings"]["symbols"] = [symbol]
    record["settings"]["auto_offset"].update(auto)
    return DsvProfile.from_record(record)


class OffsetCalculatorTests(unittest.TestCase):
    def setUp(self): self.profile = single_profile()

    def result(self, ranges, *, series="DCE.p2701", profile=None, **options):
        calculation = calculate_offsets(profile or self.profile, DailyDataset.from_csv(daily_csv(series, ranges)), as_of_date="2026-11-10", **options)
        return calculation, calculation.to_record()["rows"][0]

    def test_minrange_uses_smaller_completed_short_and_long_window(self):
        _, row = self.result([100] * 27 + [200] * 3)
        self.assertEqual((row["base"], row["open"], row["stop"], row["profit"]), ("110", "50", "16", "45"))
        self.assertEqual(row["windows"], [3, 30]); self.assertFalse(row["short_window_only"])

    def test_minrange_preserves_dsv_skipna_short_window_fallback(self):
        _, row = self.result([200] * 3)
        self.assertEqual(row["open"], "90"); self.assertTrue(row["short_window_only"])
        _, unavailable = self.result([200] * 2)
        self.assertEqual(unavailable["reason"], "INSUFFICIENT_COMPLETED_BARS")

    def test_bitumen_uses_main_twenty_day_mean_and_038(self):
        profile = single_profile("沥青", "SHFE.bu2701")
        _, row = self.result([100] * 20, profile=profile, series="KQ.m@SHFE.bu")
        self.assertEqual((row["source"], row["windows"], row["coefficient"], row["open"], row["stop"], row["profit"]), ("main", [20, 20], "0.38", "38", "12", "34"))
        _, missing = self.result([100] * 19, profile=profile, series="KQ.m@SHFE.bu")
        self.assertEqual(missing["status"], "UNAVAILABLE")

    def test_all_six_formula_variants_have_separate_versioned_identity(self):
        expected = {"minrange": ("45", "40"), "range3x09_contract": ("45", "40"), "range3x09_main": ("45", "40"),
                    "atr20_main": ("39", "35"), "atr_mixw_main": ("38", "34"), "range3_main": ("38", "34")}
        for formula in FORMULAS:
            series = "DCE.p2701" if formula in {"minrange", "range3x09_contract"} else "KQ.m@DCE.p"
            with self.subTest(formula=formula):
                result, row = self.result([100] * 30, series=series, formula=formula)
                self.assertEqual((row["open"], row["profit"]), expected[formula])
                self.assertEqual(result.to_record()["formula_version"], "DSV_OFFSETS/1")

    def test_atr20_uses_previous_close_gap_and_arithmetic_mean(self):
        raw = daily_csv("KQ.m@DCE.p", [10] * 20, rows=[["KQ.m@DCE.p", "2026-09-21", "160", "150", "155"]])
        # The first 20 closes are 100: final TR=max(10,60,50)=60.
        row = calculate_offsets(self.profile, DailyDataset.from_csv(raw), as_of_date="2026-11-10", formula="atr20_main").to_record()["rows"][0]
        self.assertEqual(row["base"], "12.5")
        self.assertEqual(row["open"], "5")

    def test_wilder_mixture_seeds_first_range_and_uses_adjust_false(self):
        raw = "series_id,date,high,low,close\nKQ.m@DCE.p,2026-11-01,110,100,105\nKQ.m@DCE.p,2026-11-02,130,120,125\nKQ.m@DCE.p,2026-11-03,112,102,107\n"
        row = calculate_offsets(self.profile, DailyDataset.from_csv(raw), as_of_date="2026-11-04", formula="atr_mixw_main").to_record()["rows"][0]
        exact = (Fraction("15") + Fraction("12.65") + Fraction("11.3625")) / 3
        self.assertEqual(Fraction(int(row["base_exact"]["numerator"]), int(row["base_exact"]["denominator"])), exact)
        self.assertEqual((row["open"], row["profit"]), ("5", "4"))

    def test_wilder_one_bar_is_valid_like_dsv_ewm(self):
        _, row = self.result([100], series="KQ.m@DCE.p", formula="atr_mixw_main")
        self.assertEqual(row["status"], "CALCULATED")
        self.assertEqual(row["base"], "100")

    def test_exact_ties_even_before_cap_and_profit_uses_unrounded_base(self):
        for base, expected_open, expected_profit in ((5, "2", "2"), (7, "4", "3"), (35, "18", "16")):
            profile = single_profile(coef={"棕榈油": "0.5"})
            with self.subTest(base=base):
                _, row = self.result([base] * 3, profile=profile)
                self.assertEqual((row["open"], row["profit"]), (expected_open, expected_profit))

    def test_repeating_mean_exact_half_does_not_round_through_float(self):
        profile = single_profile(coef={"棕榈油": "0.465"})
        _, row = self.result([33, 33, 34], profile=profile)
        self.assertEqual(row["base_exact"], {"numerator": "100", "denominator": "3"})
        self.assertEqual(row["open"], "16")

    def test_amplitude_cap_is_bounded_by_configured_open_floor(self):
        _, row = self.result([200] * 30, realized_ranges={"DCE.p2701": "10"})
        self.assertEqual((row["open_cfg"], row["open"], row["profit"]), ("90", "36", "81"))
        self.assertTrue(row["capped"]); self.assertTrue(row["cap_used"])

    def test_cap_truncates_final_positive_fraction_after_max_and_min(self):
        _, row = self.result([200] * 30, realized_ranges={"DCE.p2701": "10"}, floor="0.405")
        self.assertEqual(row["open"], "36")
        _, row = self.result([200] * 30, realized_ranges={"DCE.p2701": "101"}, floor="0")
        self.assertEqual(row["open"], "50")

    def test_absent_zero_amplitude_or_zero_ceiling_skips_cap(self):
        for options in ({}, {"realized_ranges": {"DCE.p2701": "0"}}, {"realized_ranges": {"DCE.p2701": "1"}, "ceiling": "0"}):
            with self.subTest(options=options):
                _, row = self.result([200] * 30, **options)
                self.assertEqual(row["open"], "90"); self.assertFalse(row["cap_used"])

    def test_zero_range_minimum_one_and_floor_above_one_clamps_to_original(self):
        _, row = self.result([0] * 30)
        self.assertEqual((row["open"], row["profit"]), ("1", "1"))
        _, row = self.result([200] * 30, realized_ranges={"DCE.p2701": "1"}, floor="2")
        self.assertEqual(row["open"], "90")

    def test_chinese_coefficient_override_and_zero_follow_dsv_value_fallback(self):
        _, row = self.result([100] * 3, profile=single_profile(coef={"棕榈油": "0.6"}))
        self.assertEqual((row["coefficient"], row["open"]), ("0.6", "60"))
        _, row = self.result([100] * 3, profile=single_profile(coef={"棕榈油": "0"}))
        self.assertEqual((row["coefficient"], row["open"]), ("0.45", "45"))

    def test_unknown_name_uses_explicit_dsv_035_and_fixed_15_fallback(self):
        _, row = self.result([100] * 3, profile=single_profile("自用品种"))
        self.assertEqual((row["coefficient"], row["stop"], row["open"]), ("0.35", "15", "35"))

    def test_name_based_bitumen_exception_is_not_guessed_from_contract(self):
        self.assertEqual(formula_source("minrange", "沥青", "SHFE.bu2701"), ("main", "KQ.m@SHFE.bu"))
        self.assertEqual(formula_source("minrange", "沥青自用", "SHFE.bu2701"), ("contract", "SHFE.bu2701"))

    def test_today_future_excluded_and_never_falls_back_to_incomplete_bars(self):
        raw = daily_csv("DCE.p2701", [100] * 3, first="2026-11-10")
        row = calculate_offsets(self.profile, DailyDataset.from_csv(raw), as_of_date="2026-11-10").to_record()["rows"][0]
        self.assertEqual(row["reason"], "NO_COMPLETED_SOURCE_BARS")

    def test_unfinished_today_cannot_inflate_base(self):
        raw = daily_csv("DCE.p2701", [100, 100, 100, 9000], first="2026-11-06")
        row = calculate_offsets(self.profile, DailyDataset.from_csv(raw), as_of_date="2026-11-09").to_record()["rows"][0]
        self.assertEqual(row["base"], "100"); self.assertEqual(row["completed_bars"], 3)

    def test_missing_main_source_never_uses_contract_series(self):
        _, row = self.result([100] * 30, formula="atr20_main")
        self.assertEqual(row["reason"], "NO_COMPLETED_SOURCE_BARS")

    def test_immutable_dataset_cache_does_not_follow_file_rewrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily.csv"; path.write_text(daily_csv("DCE.p2701", [100] * 3), encoding="utf-8", newline="")
            data = load_daily(path); before = data.content_hash
            path.write_text("bad", encoding="utf-8")
            rows = data.rows; rows[0]["high"] = "999"
            result = calculate_offsets(self.profile, data, as_of_date="2026-11-10")
            self.assertEqual(result.to_record()["data_hash"], before)
            self.assertEqual(result.to_record()["rows"][0]["open"], "45")

    def test_application_changes_selected_offsets_and_calculator_settings_only(self):
        profile = load_confirmed_baseline()[0]
        result = calculate_offsets(profile, DailyDataset.from_csv(daily_csv("DCE.p2701", [100] * 3)), as_of_date="2026-11-10", floor="0.3")
        applied = result.apply(profile, ["DCE.p2701"]).to_record()["settings"]
        original = profile.to_record()["settings"]
        self.assertEqual(applied["symbols"][0]["open_offset"], "45")
        self.assertEqual(applied["symbols"][1:], original["symbols"][1:])
        for section in ("trade", "schedule"): self.assertEqual(applied[section], original[section])
        self.assertEqual(applied["auto_offset"]["floor"], "0.3")
        self.assertEqual(applied["symbols"][0]["order_volume"], original["symbols"][0]["order_volume"])

    def test_stale_profile_unknown_duplicate_or_unavailable_selected_row_rejected(self):
        result, _ = self.result([100] * 3)
        changed = self.profile.to_record(); changed["settings"]["trade"]["max_volume"] = 1
        with self.assertRaises(TraderError) as caught: result.apply(DsvProfile.from_record(changed), ["DCE.p2701"])
        self.assertEqual(caught.exception.code, "E_OFFSET_CONFLICT")
        for selected in ([], ["DCE.x2701"], ["DCE.p2701"] * 2, [True]):
            with self.subTest(selected=selected), self.assertRaises(TraderError): result.apply(self.profile, selected)
        missing, _ = self.result([100])
        with self.assertRaises(TraderError): missing.apply(self.profile, ["DCE.p2701"])

    def test_negative_or_unknown_ranges_formula_and_dates_refused(self):
        for options in ({"realized_ranges": {"other": "10"}}, {"realized_ranges": {"DCE.p2701": "-1"}},
                        {"ceiling": "NaN"}, {"floor": "-0"}, {"formula": "exec"}):
            with self.subTest(options=options), self.assertRaises(TraderError): self.result([100] * 3, **options)
        for day in ("2026-02-30", "20261110", None):
            with self.subTest(day=day), self.assertRaises(TraderError): calculate_offsets(self.profile, DailyDataset.from_csv(daily_csv("DCE.p2701", [100] * 3)), as_of_date=day)

    def test_csv_unknown_duplicate_or_missing_header_whole_batch_refused(self):
        for raw in ("", "date,high,low,close\n", "series_id,date,high,low,password\n", "series_id,date,high,low,high\n"):
            with self.subTest(raw=raw), self.assertRaises(DailyCsvError): DailyDataset.from_csv(raw)

    def test_csv_encoding_quoting_size_and_row_count_error(self):
        for raw in (b"\xff", b"x" * (MAX_DAILY_BYTES + 1), "series_id,date,high,low,close\n\"bad", "series_id,date,high,low,close\n\n"):
            with self.subTest(raw=raw[:20]), self.assertRaises(DailyCsvError): DailyDataset.from_csv(raw)

    def test_csv_inconsistent_ohlc_negative_nan_and_exponent_rejected_at_line(self):
        for values in (("90", "100", "100"), ("110", "100", "111"), ("0", "0", "0"), ("NaN", "100", "100"), ("1e3", "100", "100"), ("110", "-1", "100")):
            raw = "series_id,date,high,low,close\nDCE.p2701,2026-11-01," + ",".join(values) + "\n"
            with self.subTest(values=values), self.assertRaises(DailyCsvError) as caught: DailyDataset.from_csv(raw)
            self.assertEqual(caught.exception.line, 2)

    def test_duplicate_and_descending_series_date_rejected(self):
        raw = daily_csv("DCE.p2701", [100] * 3)
        for day in ("2026-09-02", "2026-09-03"):
            with self.subTest(day=day), self.assertRaises(DailyCsvError): DailyDataset.from_csv(raw + f"DCE.p2701,{day},200,100,100\n")

    def test_series_identity_and_future_date_validation_not_silently_skipped(self):
        for identity, day in (("p2701", "2026-11-01"), ("KQ.m@SHFE", "2026-11-01"), ("DCE.p2701", "9999-99-99")):
            with self.subTest(identity=identity), self.assertRaises(DailyCsvError): DailyDataset.from_csv(f"series_id,date,high,low,close\n{identity},{day},200,100,100\n")

    def test_series_bar_capacity_and_missing_file_refused(self):
        with self.assertRaises(DailyCsvError): DailyDataset.from_csv(daily_csv("DCE.p2701", [100] * 513))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TraderError): load_daily(Path(directory) / "missing.csv")

    def test_result_hash_copy_text_and_export_are_precise_and_exclusive(self):
        result, row = self.result([100] * 3)
        record = result.to_record(); record["rows"][0]["open"] = "999"
        self.assertEqual(result.to_record()["rows"][0]["open"], row["open"])
        self.assertIn("45/16/40", result.text())
        self.assertFalse(result.to_record()["runtime_activation"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"; export_calculation(result, path)
            self.assertEqual(json.loads(path.read_bytes()), result.to_record())
            before = path.read_bytes()
            with self.assertRaises(TraderError): export_calculation(result, path)
            self.assertEqual(path.read_bytes(), before)

    def test_result_export_failure_has_no_partial_file(self):
        result, _ = self.result([100] * 3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            with patch("online_trader.configuration.files.os.link", side_effect=OSError("failure")):
                with self.assertRaises(TraderError): export_calculation(result, path)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_all_formula_coefficients_and_stops_match_independent_static_dsv_facts(self):
        facts = json.loads((Path(__file__).parents[1] / "docs/references/dsv-offset-formulas-20261110.json").read_text("utf-8"))
        names = ("塑料", "沥青", "菜油", "棕榈油", "PTA")
        self.assertEqual(set(FORMULAS), set(facts["formula_labels"]))
        for formula, values in COEFFICIENTS.items(): self.assertEqual(dict(zip(names, values)), facts["coefficients"][formula])
        self.assertEqual(dict(STOPS), facts["fixed_stops"])
        self.assertEqual(facts["module_marshaled_sha256"], "26af1a7bd803b26c81a2274bbabf682d11af43e0ca41da21325101284887f412")
