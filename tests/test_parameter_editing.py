import copy
from pathlib import Path
import tempfile
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.editor import EditError, form_values, profile_from_form
from online_trader.configuration.files import export_profile
from online_trader.configuration.templates import TemplateStore


class ParameterEditingTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_confirmed_baseline()[0]
        self.values = form_values(self.profile)
        self.original = self.profile.to_record()

    def tearDown(self): self.assertEqual(self.profile.to_record(), self.original)

    def rejects(self, path=None):
        with self.assertRaises(EditError) as caught: profile_from_form(self.profile, self.values)
        if path: self.assertEqual(caught.exception.path, path)
        return caught.exception

    def test_all_69_trade_fields_and_all_other_sections_roundtrip(self):
        self.assertEqual(len(self.values["trade"]), 69)
        self.assertEqual(set(self.values), {"trade", "symbols", "auto_offset", "schedule"})
        self.assertEqual(profile_from_form(self.profile, self.values).content_hash, self.profile.content_hash)

    def test_every_scalar_field_can_be_changed_in_its_own_type(self):
        for section in ("trade", "auto_offset", "schedule"):
            for key, value in self.values[section].items():
                if key in {"holidays", "coef"}: continue
                values = copy.deepcopy(self.values)
                changed = (not value if type(value) is bool else "07:01" if ":" in value else
                    "range3_main" if key == "formula" else "最新价" if key == "close_price_mode" else
                    str(int(value) + 1) if type(self.original["settings"][section][key]) is int else "0.123")
                values[section][key] = changed
                with self.subTest(section=section, key=key):
                    candidate = profile_from_form(self.profile, values)
                    self.assertNotEqual(candidate.content_hash, self.profile.content_hash)

    def test_disabled_parameters_still_edit_without_enabling_switch(self):
        self.values["trade"]["live_max_lots"] = "8"
        candidate = profile_from_form(self.profile, self.values).to_record()["settings"]["trade"]
        self.assertEqual(candidate["live_max_lots"], 8); self.assertFalse(candidate["live_protect"])

    def test_precise_decimal_and_trailing_zero_normalization(self):
        self.values["trade"]["risk_reference_equity"] = "123456789012345678.123456789012"
        self.values["trade"]["risk_per_trade_ratio"] = "0.0020"
        result = profile_from_form(self.profile, self.values).to_record()["settings"]["trade"]
        self.assertEqual(result["risk_reference_equity"], self.values["trade"]["risk_reference_equity"])
        self.assertEqual(result["risk_per_trade_ratio"], "0.002")

    def test_explicit_booleans_cannot_be_truthy_text_or_integer(self):
        for value in ("False", 0, 1, None):
            self.values["trade"]["live_protect"] = value
            with self.subTest(value=value): self.rejects("trade.live_protect")

    def test_negative_zero_nonfinite_exponent_blank_and_range_rejected(self):
        for value in ("-0", "-1", "NaN", "Infinity", "1e-3", "", "1.01", "PRIVATE-INPUT"):
            self.values["trade"]["risk_per_trade_ratio"] = value
            with self.subTest(value=value):
                error = self.rejects("trade.risk_per_trade_ratio")
                self.assertNotIn("PRIVATE-INPUT", str(error))

    def test_integer_fraction_overflow_bool_or_missing_refused(self):
        for value in ("1.0", "2147483648", "", "-1", True):
            self.values["trade"]["max_volume"] = value
            with self.subTest(value=value): self.rejects("trade.max_volume")

    def test_times_holidays_and_duplicate_dates_validated(self):
        self.values["schedule"]["day_start_time"] = "25:00"; self.rejects("schedule.day_start_time")
        self.values = form_values(self.profile)
        for value in ('["2026-02-30"]', '["2026-11-10","2026-11-10"]', '[true]', '{}'):
            self.values["trade"]["holidays"] = value
            with self.subTest(value=value): self.rejects("trade.holidays")

    def test_holiday_addition_and_chinese_coefficient_map_roundtrip(self):
        self.values["trade"]["holidays"] = '["2026-11-10"]'
        self.values["auto_offset"]["coef"] = '{"棕榈油":0.47,"沥青":"0","20号胶":"0.36"}'
        candidate = profile_from_form(self.profile, self.values)
        self.assertEqual(candidate.to_record()["settings"]["auto_offset"]["coef"], {"棕榈油": "0.47", "沥青": "0", "20号胶": "0.36"})
        self.assertEqual(DsvProfile.from_json(candidate.to_json()).content_hash, candidate.content_hash)

    def test_coefficient_duplicate_unsafe_key_nonfinite_negative_or_boolean_refused(self):
        for value in ('{"p":1,"p":2}', '{"p;execute":1}', '{"p":NaN}', '{"p":-0}', '{"p":true}', '{"p":1e99999999999999999999}', '{"p":null}', '[]'):
            self.values["auto_offset"]["coef"] = value
            with self.subTest(value=value): self.rejects("auto_offset.coef")

    def test_coefficient_and_holiday_json_size_encoding_and_depth_rejected(self):
        for raw in (" " * 32769, "\ud800", "[" * 2000):
            self.values["auto_offset"]["coef"] = raw
            with self.subTest(raw=raw[:10]): self.rejects("auto_offset.coef")

    def test_symbol_aliases_candidates_decimal_and_auto_edit(self):
        row = self.values["symbols"][4]
        row.update(code="白银2612", code_new="ag2702", open_offset="12.50", order_volume="自动")
        result = profile_from_form(self.profile, self.values).to_record()["settings"]["symbols"][4]
        self.assertEqual((result["code"], result["code_new"], result["open_offset"], result["order_volume"]), ("SHFE.ag2612", "SHFE.ag2702", "12.5", 0))

    def test_explicit_candidate_empty_removes_preview_field(self):
        self.values["symbols"][4]["code_new"] = ""
        self.assertNotIn("code_new", profile_from_form(self.profile, self.values).to_record()["settings"]["symbols"][4])

    def test_symbol_add_remove_empty_list_and_duplicate_batch(self):
        new = copy.deepcopy(self.values["symbols"][0]); new.update(name="铜", code="cu2701", code_new="")
        self.values["symbols"].append(new)
        self.assertEqual(len(profile_from_form(self.profile, self.values).to_record()["settings"]["symbols"]), 6)
        self.values["symbols"].append(copy.deepcopy(new)); self.rejects("symbols")
        self.values["symbols"] = []
        self.assertEqual(profile_from_form(self.profile, self.values).to_record()["settings"]["symbols"], [])

    def test_symbol_invalid_late_row_does_not_return_partial_profile(self):
        self.values["symbols"][0]["open_offset"] = "99"
        self.values["symbols"][4]["stop_offset"] = "-1"
        self.rejects("symbols[4].stop_offset")

    def test_symbol_ratio_size_and_unknown_fields_refused(self):
        self.values["symbols"][0]["half_close_ratio"] = "50"; self.rejects("symbols[0]")
        self.values = form_values(self.profile); self.values["symbols"] *= 13; self.rejects("symbols")
        self.values = form_values(self.profile); self.values["symbols"][0]["password"] = "PRIVATE"; self.rejects("symbols[0]")

    def test_missing_and_unknown_top_or_section_fields_refused(self):
        self.values["account"] = {}; self.rejects("settings")
        self.values = form_values(self.profile); del self.values["schedule"]["enabled"]; self.rejects("schedule")

    def test_complete_edited_profile_exports_and_template_keeps_only_dsv_subset(self):
        self.values["trade"].update(max_volume="12", half_stage2=True)
        self.values["auto_offset"]["coef"] = '{"棕榈油":"0.5"}'
        candidate = profile_from_form(self.profile, self.values)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); export_profile(candidate, root / "profile.json")
            self.assertEqual(DsvProfile.from_json((root / "profile.json").read_bytes()).content_hash, candidate.content_hash)
            store = TemplateStore(root / "templates"); snapshot = store.save("样例", candidate, expected_revision=0)
            applied = snapshot.apply("样例", self.profile).to_record()["settings"]
            self.assertEqual(applied["trade"]["max_volume"], 12)
            self.assertFalse(applied["trade"]["half_stage2"])
            self.assertEqual(applied["auto_offset"]["coef"], {})

    def test_form_values_are_fresh_without_mutable_alias(self):
        self.values["symbols"][0]["name"] = "修改"
        self.assertNotEqual(form_values(self.profile)["symbols"][0]["name"], "修改")
