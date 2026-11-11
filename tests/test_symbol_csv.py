import csv
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.symbol_csv import (HEADERS, ALIASES, MAX_CSV_BYTES,
    SymbolCsvError, import_symbols, load_symbols, export_symbols, resolve_contract)
from online_trader.errors import TraderError


def csv_bytes(rows, headers=HEADERS):
    text = StringIO(newline="")
    writer = csv.writer(text); writer.writerow(headers); writer.writerows(rows)
    return text.getvalue().encode("utf-8-sig")


ROW = ["白银", "ag2612", "0", "0", "0", "自动", "0.5", "0", "ag2702"]


class SymbolCsvTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_confirmed_baseline()[0]
        self.original = self.profile.to_record()
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self):
        self.assertEqual(self.profile.to_record(), self.original)
        self.temp.cleanup()

    def invalid(self, rows, *, headers=HEADERS, line=2, field=None):
        with self.assertRaises(SymbolCsvError) as caught:
            import_symbols(csv_bytes(rows, headers), self.profile)
        self.assertEqual(caught.exception.line, line)
        if field: self.assertEqual(caught.exception.field, field)
        return caught.exception

    def test_baseline_csv_roundtrip_includes_candidate_zero_and_dormant_values(self):
        target = self.root / "symbols.csv"
        export_symbols(self.profile, target)
        self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))
        candidate = load_symbols(target, self.profile)
        self.assertEqual(candidate.content_hash, self.profile.content_hash)
        self.assertEqual(candidate.to_record()["settings"]["symbols"][4]["code_new"], "SHFE.ag2702")

    def test_original_eight_chinese_columns_keep_existing_rollover_candidate(self):
        result = import_symbols(csv_bytes([ROW[:8]], HEADERS[:8]), self.profile)
        silver = result.to_record()["settings"]["symbols"][0]
        self.assertEqual(silver["code_new"], "SHFE.ag2702")
        self.assertEqual(silver["order_volume"], 0)
        self.assertEqual(silver["stop_offset"], "0")

    def test_explicit_empty_candidate_removes_only_preview_candidate(self):
        row = ROW.copy(); row[-1] = ""
        result = import_symbols(csv_bytes([row]), self.profile)
        self.assertNotIn("code_new", result.to_record()["settings"]["symbols"][0])

    def test_minimal_legacy_columns_have_only_documented_dsv_defaults(self):
        result = import_symbols(csv_bytes([ROW[:5]], HEADERS[:5]), self.profile)
        symbol = result.to_record()["settings"]["symbols"][0]
        self.assertEqual([symbol[k] for k in ("order_volume", "half_close_ratio", "hysteresis_points")], [0, "0.5", "0"])

    def test_english_columns_and_hyst_alias_fraction_units(self):
        headers = ["name", "code", "open_offset", "stop_offset", "profit_offset", "order_volume", "半仓%", "hyst_pts", "code_new"]
        row = ROW.copy(); row[6:8] = ["0", "0.125"]
        result = import_symbols(csv_bytes([row], headers), self.profile).to_record()["settings"]["symbols"][0]
        self.assertEqual(result["half_close_ratio"], "0")
        self.assertEqual(result["hysteresis_points"], "0.125")

    def test_merge_retains_order_and_other_sections_and_updates_matching_contract(self):
        row = ROW.copy(); row[2] = "12.123456789012"
        result = import_symbols(csv_bytes([row, ["铜", "cu2701", "10", "5", "20", "2", "0.5", "0", ""]]), self.profile, mode="merge")
        settings = result.to_record()["settings"]
        self.assertEqual(len(settings["symbols"]), 6)
        self.assertEqual(settings["symbols"][4]["open_offset"], "12.123456789012")
        self.assertEqual(settings["symbols"][-1]["code"], "SHFE.cu2701")
        for section in ("trade", "auto_offset", "schedule"): self.assertEqual(settings[section], self.original["settings"][section])

    def test_all_144_static_dsv_aliases_resolve_without_fuzzy_matching(self):
        self.assertEqual(len(ALIASES), 144)
        for alias, (exchange, product) in ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(resolve_contract(alias + "701"), f"{exchange}.{product}701")
                self.assertEqual(resolve_contract(alias + "2701"), f"{exchange}.{product}2701")

    def test_exchange_and_product_case_normalization(self):
        self.assertEqual(resolve_contract("shfe.AU2612"), "SHFE.au2612")
        self.assertEqual(resolve_contract("czce.ta701"), "CZCE.TA701")
        self.assertEqual(resolve_contract("沪深3002612"), "CFFEX.IF2612")
        self.assertEqual(resolve_contract("20号胶2612"), "INE.nr2612")

    def test_fully_qualified_future_product_requires_explicit_month(self):
        self.assertEqual(resolve_contract("SHFE.xyz2701"), "SHFE.xyz2701")
        for value in ("xyz2701", "SHFE.xyz", "SHFE.xyz2713"):
            with self.subTest(value=value), self.assertRaises(TraderError): resolve_contract(value)

    def test_main_contract_unknown_alias_and_wrong_exchange_refused(self):
        for value in ("白银", "ag", "ag主连", "KQ.m@SHFE.ag", "DCE.au2612", "UNKNOWN.ag2612", "ag2600", "ag2613", "ag12", "ag202611", "p2601x"):
            with self.subTest(value=value), self.assertRaises(TraderError): resolve_contract(value)

    def test_duplicate_normalized_contract_refuses_whole_batch(self):
        row = ROW.copy(); row[1] = "SHFE.AG2612"
        self.invalid([ROW, row], line=3, field="code")

    def test_later_bad_row_has_physical_line_and_no_partial_result(self):
        row = ROW.copy(); row[1] = "cu2701"; row[2] = "PRIVATE-BAD-VALUE"
        error = self.invalid([ROW, row], line=3, field="open_offset")
        self.assertNotIn("PRIVATE-BAD-VALUE", str(error))

    def test_negative_nan_infinity_scientific_empty_and_signed_zero_refused(self):
        for value in ("-1", "-0", "NaN", "Infinity", "1e3", "", "true", "+1", "1,000", "01", "1.0000000000001"):
            row = ROW.copy(); row[3] = value
            with self.subTest(value=value): self.invalid([row], field="stop_offset")

    def test_large_precise_decimal_does_not_round_through_float(self):
        row = ROW.copy(); row[2] = "123456789012345678.123456789012"
        result = import_symbols(csv_bytes([row]), self.profile).to_record()["settings"]["symbols"][0]
        self.assertEqual(result["open_offset"], row[2])

    def test_fraction_out_of_range_does_not_get_silently_reset(self):
        for value in ("50", "1.001", "-0.5"):
            row = ROW.copy(); row[6] = value
            with self.subTest(value=value): self.invalid([row])

    def test_volume_requires_bounded_integer_or_chinese_auto(self):
        for value in ("1.5", "1.0", "-1", "auto", "", "2147483648", "True"):
            row = ROW.copy(); row[5] = value
            with self.subTest(value=value): self.invalid([row])

    def test_missing_optional_column_differs_from_empty_cell(self):
        row = ROW.copy(); row[7] = ""
        self.invalid([row], field="hysteresis_points")

    def test_header_unknown_duplicate_alias_and_missing_required_refused(self):
        for headers in (list(HEADERS) + ["password"], list(HEADERS) + ["code"], HEADERS[1:]):
            with self.subTest(headers=headers): self.invalid([], headers=headers, line=1, field="header")

    def test_missing_and_extra_cells_and_blank_rows_are_errors(self):
        for row in (ROW[:-1], ROW + ["extra"], []):
            with self.subTest(row=row): self.invalid([row], field="row")

    def test_empty_and_header_only_csv_refused(self):
        for raw in (b"", csv_bytes([])):
            with self.subTest(raw=raw), self.assertRaises(SymbolCsvError): import_symbols(raw, self.profile)

    def test_utf8_size_and_broken_quoting_limits(self):
        for raw in (b"\xff", b"x" * (MAX_CSV_BYTES + 1), csv_bytes([]) + b'"unclosed'):
            with self.subTest(raw=raw[:20]), self.assertRaises(SymbolCsvError): import_symbols(raw, self.profile)

    def test_csv_batch_and_merge_capacity(self):
        rows = [["铜", f"SHFE.cu{year:02}01", "1", "1", "1", "0", "0.5", "0", ""] for year in range(1, 66)]
        self.invalid(rows, line=66, field="row")
        with self.assertRaises(SymbolCsvError): import_symbols(csv_bytes(rows[:64]), self.profile, mode="merge")

    def test_csv_formula_and_apostrophe_names_roundtrip_safely(self):
        for name in ("=SUM(1,2)", "+formula", "-formula", "@formula", "'name", " 铜 ", "  =formula"):
            record = self.profile.to_record(); record["settings"]["symbols"][0]["name"] = name
            profile = DsvProfile.from_record(record)
            path = self.root / f"{len(list(self.root.iterdir()))}.csv"
            export_symbols(profile, path)
            cells = list(csv.reader(StringIO(path.read_text("utf-8-sig"))))
            self.assertTrue(cells[1][0].startswith("'"))
            self.assertEqual(load_symbols(path, profile).content_hash, profile.content_hash)

    def test_invalid_mode_or_profile_rejected(self):
        for profile, mode in ((None, "replace"), (self.profile, "append"), (self.profile, [])):
            with self.assertRaises(TraderError): import_symbols(csv_bytes([ROW]), profile, mode=mode)

    def test_exclusive_export_never_overwrites(self):
        path = self.root / "symbols.csv"; path.write_bytes(b"preserve")
        with self.assertRaises(TraderError): export_symbols(self.profile, path)
        self.assertEqual(path.read_bytes(), b"preserve")

    def test_export_failure_and_missing_import_leave_no_partial_csv(self):
        path = self.root / "symbols.csv"
        with patch("online_trader.configuration.files.os.link", side_effect=OSError("Injected failure")):
            with self.assertRaises(TraderError): export_symbols(self.profile, path)
        self.assertFalse(path.exists()); self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(TraderError): load_symbols(path, self.profile)
