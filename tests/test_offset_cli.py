from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from online_trader.cli import main
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from tests.test_offset_calculator import daily_csv


class OffsetCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.source = self.root / "daily.csv"
        self.source.write_bytes(daily_csv("DCE.p2701", [100] * 30).encode("utf-8-sig"))

    def tearDown(self): self.temp.cleanup()

    def invoke(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err): result = main(["parameters", "calculate", "--daily", str(self.source), "--as-of", "2026-11-10", *map(str, args)])
        return result, out.getvalue(), err.getvalue()

    def test_cli_report_is_preview_only_with_source_hash_and_formula_version(self):
        result, out, error = self.invoke()
        self.assertEqual((result, error), (0, ""))
        record = json.loads(out)
        self.assertFalse(record["runtime_activation"])
        self.assertEqual(record["formula_version"], "DSV_OFFSETS/1")
        self.assertEqual(record["rows"][0]["open"], "45")
        self.assertEqual(len(record["data_hash"]), 64)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_cli_saves_selected_business_profile_without_changing_other_symbols(self):
        target = self.root / "preview.json"
        result, _, error = self.invoke("--apply-output", target, "--contracts", "DCE.p2701")
        self.assertEqual((result, error), (0, ""))
        applied = DsvProfile.from_json(target.read_bytes()).to_record()["settings"]
        self.assertEqual(applied["symbols"][0]["open_offset"], "45")
        self.assertEqual(applied["symbols"][1:], load_confirmed_baseline()[0].to_record()["settings"]["symbols"][1:])

    def test_cli_range_overrides_and_formula_choices(self):
        ranges = self.root / "ranges.json"; ranges.write_text('{"DCE.p2701":"1"}', encoding="utf-8")
        result, out, _ = self.invoke("--ranges", ranges, "--formula", "range3x09_contract", "--floor", "0.3")
        self.assertEqual(result, 0); row = json.loads(out)["rows"][0]
        self.assertEqual(row["open"], "13"); self.assertTrue(row["capped"])

    def test_cli_export_is_exclusive_and_unavailable_selection_writes_nothing(self):
        target = self.root / "report.json"
        self.assertEqual(self.invoke("--output", target)[0], 0); before = target.read_bytes()
        self.assertEqual(self.invoke("--output", target)[0], 2); self.assertEqual(target.read_bytes(), before)
        missing = self.root / "no.json"
        self.assertEqual(self.invoke("--apply-output", missing, "--contracts", "SHFE.ag2612")[0], 2)
        self.assertFalse(missing.exists())

    def test_cli_requires_explicit_selection_and_one_output_per_command(self):
        target = self.root / "no.json"
        for options in (("--apply-output", target), ("--contracts", "DCE.p2701"),
                        ("--output", target, "--apply-output", self.root / "other.json", "--contracts", "DCE.p2701")):
            with self.subTest(options=options): self.assertEqual(self.invoke(*options)[0], 2)
        self.assertFalse(target.exists()); self.assertFalse((self.root / "other.json").exists())

    def test_cli_bad_later_csv_row_reports_line_without_echo_and_no_output(self):
        self.source.write_bytes(self.source.read_bytes() + b"DCE.p2701,2026-11-09,PRIVATE,100,100\r\n")
        target = self.root / "no.json"
        result, out, error = self.invoke("--output", target)
        self.assertEqual(result, 2); self.assertIn("line 32", error)
        self.assertNotIn("PRIVATE", out + error); self.assertFalse(target.exists())

    def test_cli_bad_range_json_and_missing_file_fail_without_database(self):
        ranges = self.root / "ranges.json"
        for raw in ('{"DCE.p2701":1.5}', '{"DCE.p2701":"-1"}', '{"DCE.p2701":"1","DCE.p2701":"2"}'):
            ranges.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw): self.assertEqual(self.invoke("--ranges", ranges)[0], 2)
        self.assertEqual(self.invoke("--ranges", self.root / "missing.json")[0], 2)
        self.assertEqual(list(self.root.rglob("*.db")), [])

    def test_cli_never_starts_kernel_account_lock_or_qt(self):
        with patch("online_trader.kernel.engine.Kernel", side_effect=AssertionError("No kernel")):
            result, _, _ = self.invoke("--output", self.root / "report.json")
        self.assertEqual(result, 0)
        self.assertEqual(list(self.root.rglob("*.db")), [])
