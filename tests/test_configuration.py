import copy
from dataclasses import FrozenInstanceError
from decimal import Decimal
import json
from pathlib import Path
import unittest

from online_trader.configuration import DsvProfile
from online_trader.configuration.dsv import MAX_SOURCE_BYTES, MAX_SYMBOLS
from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.errors import TraderError


BASE = json.loads((Path(__file__).parents[1] / "docs/references/dsv-settings-20261109.json").read_text(encoding="utf-8"))["settings"]


class DsvProfileTests(unittest.TestCase):
    def profile(self):
        return DsvProfile.from_settings(copy.deepcopy(BASE))

    def rejected(self, settings):
        with self.assertRaises(TraderError) as result:
            DsvProfile.from_settings(settings)
        self.assertEqual(result.exception.code, "E_CONFIG")

    def test_entire_confirmed_snapshot_round_trips_without_new_defaults(self):
        profile = self.profile()
        self.assertEqual(profile.to_settings(), BASE)
        restored = DsvProfile.from_json(profile.to_json())
        self.assertEqual(restored.to_settings(), BASE)
        self.assertEqual(restored.content_hash, profile.content_hash)
        for key, expected in BASE["trade"].items():
            if type(expected) is bool:
                self.assertIs(profile.to_settings()["trade"][key], expected)

    def test_explicit_full_dsv_import_discards_private_and_ui_sections(self):
        source = copy.deepcopy(BASE) | {
            "account": {"username": "PRIVATE-ACCOUNT", "password": "PRIVATE-PASSWORD"},
            "notify": {"dingtalk_secret": "PRIVATE-SECRET", "dingtalk_webhook": "PRIVATE-WEBHOOK"},
            "accounts": [{"credentials": "PRIVATE-CREDENTIALS"}],
            "legal": {"agreed_machine": "PRIVATE-DEVICE"},
            "ui": {"window_geometry": "PRIVATE-GEOMETRY", "x": -1920, "y": -1080, "offset": -0.5},
        }
        raw = json.dumps(source)
        profile = DsvProfile.from_dsv_json(raw)
        expected = copy.deepcopy(BASE)
        expected['symbols'][4]['code'] = expected['symbols'][4].pop('code_new')
        self.assertEqual(profile.to_settings(), expected)
        self.assertNotIn("PRIVATE", profile.to_json())
        self.assertNotIn(b"PRIVATE", canonical_bytes(profile.to_record()))
        self.assertNotIn(b"PRIVATE", canonical_bytes(profile.summary()))
        with self.assertRaises(TraderError):
            DsvProfile.from_json(raw)

    def test_dsv_legacy_import_applies_one_time_markers_and_supplies_original_defaults(self):
        old = {'trade':{'max_volume':20, 'max_daily_orders':20, 'max_quote_age_seconds':120,
                        'chase_window_minutes':3, 'entry_slip_ratio':0.05, 'chase_slip_ratio':0.03,
                        'no_open_after_open':False, 'half_pullback_close':True,
                        'passive_wait_seconds':1.25, 'resume_gap_seconds':0, 'stale_close_day_stop':True},
               'schedule':{'enabled':False}, 'symbols':[]}
        imported = DsvProfile.from_dsv_json(json.dumps(old))
        settings = imported.to_settings()
        self.assertEqual(settings['symbols'], [])
        self.assertTrue(settings['schedule']['enabled'])
        trade = settings['trade']
        self.assertEqual((trade['max_volume'],trade['max_daily_orders'],trade['max_quote_age_seconds']), (30,200,0))
        self.assertEqual((trade['chase_window_minutes'],trade['entry_slip_ratio'],trade['chase_slip_ratio']), (1,.03,.05))
        self.assertFalse(trade['no_open_after_open'])
        self.assertFalse(DsvProfile.from_dsv_json(b'{}').to_settings()['trade']['no_open_after_open'])
        self.assertFalse(trade['half_pullback_close'])
        self.assertFalse(trade['stale_close_day_stop'])
        self.assertEqual((trade['passive_wait_seconds'],trade['resume_gap_seconds']), (1.25,0))
        self.assertEqual(DsvProfile.from_json(imported.to_json()).content_hash, imported.content_hash)
        old['trade'].update(chase_slip_migrated=True, no_open_migrated=True, half_pullback_migrated=True)
        trade = DsvProfile.from_dsv_json(json.dumps(old)).to_settings()['trade']
        self.assertEqual((trade['chase_slip_ratio'],trade['no_open_after_open'],trade['half_pullback_close']), (.03,False,True))

    def test_legacy_rollover_ignores_blank_candidate_and_preserves_same_contract(self):
        for candidate in ("", "   ", "SHFE.ag2612"):
            with self.subTest(candidate=candidate):
                source = copy.deepcopy(BASE)
                source["symbols"][-1]["code_new"] = candidate
                saved = DsvProfile.from_dsv_json(json.dumps(source)).to_settings()["symbols"][-1]
                self.assertEqual(saved["code"], "SHFE.ag2612")
                self.assertEqual(saved.get("code_new"), candidate.strip() or None)

    def test_missing_or_unknown_business_fields_are_not_defaulted_or_ignored(self):
        for section in BASE:
            settings = copy.deepcopy(BASE); del settings[section]
            self.rejected(settings)
        for section in ("trade", "auto_offset", "schedule"):
            for key in BASE[section]:
                settings = copy.deepcopy(BASE); del settings[section][key]
                with self.subTest(section=section, key=key):
                    self.rejected(settings)
            settings = copy.deepcopy(BASE); settings[section]["password"] = "DO-NOT-ECHO"
            self.rejected(settings)
        settings = copy.deepcopy(BASE); settings["symbols"][0]["password"] = "DO-NOT-ECHO"
        self.rejected(settings)
        with self.assertRaises(TraderError) as result:
            DsvProfile.from_dsv_json(json.dumps(BASE | {"new_business_mode": {"token": "DO-NOT-ECHO"}}))
        self.assertNotIn("DO-NOT-ECHO", str(result.exception))

    def test_every_boolean_requires_an_actual_boolean(self):
        for key, value in BASE["trade"].items():
            if type(value) is not bool:
                continue
            settings = copy.deepcopy(BASE); settings["trade"][key] = 1
            with self.subTest(key=key):
                self.rejected(settings)
        settings = copy.deepcopy(BASE); settings["schedule"]["enabled"] = "false"
        self.rejected(settings)

    def test_numbers_reject_boolean_string_and_negative_coercion(self):
        for key, value in BASE["trade"].items():
            if type(value) not in {int, float}:
                continue
            for bad in (True, "0", -1):
                settings = copy.deepcopy(BASE); settings["trade"][key] = bad
                with self.subTest(key=key, bad=bad):
                    self.rejected(settings)
        for key in ("open_offset", "stop_offset", "profit_offset", "order_volume", "half_close_ratio", "hysteresis_points"):
            settings = copy.deepcopy(BASE); settings["symbols"][0][key] = True
            self.rejected(settings)

    def test_nonfinite_values_excess_precision_and_huge_exponents_are_rejected(self):
        for bad in (float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity"), -0.0,
                    Decimal("1e1000000"), Decimal("1e-1000000"), Decimal("0.1234567890123")):
            settings = copy.deepcopy(BASE); settings["trade"]["risk_reference_equity"] = bad
            with self.subTest(value=str(bad)):
                self.rejected(settings)
        settings = copy.deepcopy(BASE); settings["trade"]["risk_per_trade_ratio"] = 1.01
        self.rejected(settings)
        settings = copy.deepcopy(BASE); settings["trade"]["max_volume"] = 2_147_483_648
        self.rejected(settings)

    def test_duplicate_keys_and_nonfinite_json_tokens_are_rejected(self):
        original = json.dumps(BASE, separators=(",", ":"))
        duplicate = original.replace('"max_volume":30', '"max_volume":30,"max_volume":31', 1)
        for raw in (duplicate, *(original.replace('"risk_per_trade_ratio":0.002', '"risk_per_trade_ratio":' + token, 1)
                                  for token in ("NaN", "Infinity", "-Infinity")),
                    original.replace('"hysteresis_points":0', '"hysteresis_points":-0', 1)):
            with self.subTest(raw=raw[:50]), self.assertRaises(TraderError):
                DsvProfile.from_json(raw)
        with self.assertRaises(TraderError):
            DsvProfile.from_dsv_json(original[:-1] + ',"account":{"x":1,"x":2}}')

    def test_zero_and_disabled_values_remain_distinct_and_preserved(self):
        trade = self.profile().to_settings()["trade"]
        self.assertEqual(trade["hysteresis_points"], 0)
        self.assertEqual(trade["max_quote_age_seconds"], 0)
        self.assertEqual(trade["risk_reference_equity"], 0)
        self.assertIs(trade["min_one_lot_fallback"], True)
        self.assertIs(trade["require_trigger_update"], False)
        self.assertIs(trade["continuation_ignore_no_open"], False)
        self.assertIs(trade["live_protect"], False)
        self.assertEqual(trade["live_max_lots"], 2)
        self.assertEqual(trade["live_daily_loss_limit"], 5000)

    def test_profiles_are_immutable_and_all_exports_are_detached(self):
        settings = copy.deepcopy(BASE); profile = DsvProfile.from_settings(settings)
        digest = profile.content_hash
        settings["symbols"][0]["open_offset"] = 123
        settings["trade"]["holidays"].append("2026-11-09")
        for output in (profile.to_record(), profile.to_payload()):
            output["settings"]["symbols"][0]["open_offset"] = "777"
        output = profile.to_settings(); output["trade"]["live_protect"] = True
        output = profile.summary(); output["symbols"][0]["code"] = "DCE.fake9999"
        self.assertEqual(profile.content_hash, digest)
        self.assertEqual(profile.to_settings(), BASE)
        with self.assertRaises(FrozenInstanceError):
            profile._record_bytes = b"{}"
        with self.assertRaises(TypeError):
            DsvProfile()

    def test_canonical_record_and_summary_use_no_float_and_restore_exactly(self):
        profile = self.profile(); record = profile.to_record()
        self.assertEqual(record, profile.to_payload())
        self.assertEqual(record["schema_version"], "DSV_SETTINGS/1")
        self.assertEqual(record["settings"]["trade"]["risk_per_trade_ratio"], "0.002")
        self.assertEqual(record["settings"]["trade"]["hysteresis_points"], "0")
        self.assertEqual(profile.content_hash, content_hash(record))
        canonical_bytes(record); canonical_bytes(profile.summary()); canonical_bytes(profile.diagnostics())
        self.assertEqual(DsvProfile.from_record(record).to_settings(), BASE)
        self.assertEqual(DsvProfile.from_payload(record).content_hash, profile.content_hash)

    def test_canonical_record_refuses_noncanonical_numbers_and_extra_metadata(self):
        for bad in (0.002, True, "0.0020", "2e-3", "-0", "NaN", "-0.1"):
            record = self.profile().to_record(); record["settings"]["trade"]["risk_per_trade_ratio"] = bad
            with self.subTest(value=bad), self.assertRaises(TraderError):
                DsvProfile.from_record(record)
        record = self.profile().to_record(); record["schema_version"] = "DSV_SETTINGS/2"
        with self.assertRaises(TraderError): DsvProfile.from_record(record)
        record = self.profile().to_record(); record["token"] = "DO-NOT-ECHO"
        with self.assertRaises(TraderError): DsvProfile.from_record(record)

    def test_inactive_parameters_still_change_the_content_hash(self):
        changed = copy.deepcopy(BASE); changed["trade"]["live_daily_loss_limit"] = 5001
        changed["trade"]["live_protect"] = False
        self.assertNotEqual(DsvProfile.from_settings(changed).content_hash, self.profile().content_hash)
        equivalent = copy.deepcopy(BASE); equivalent["trade"]["risk_per_trade_ratio"] = Decimal("0.0020")
        self.assertEqual(DsvProfile.from_settings(equivalent).content_hash, self.profile().content_hash)

    def test_four_configured_symbols_and_silver_are_diagnosed_without_enabling_them(self):
        summary = self.profile().summary()
        self.assertEqual((summary["symbol_count"], summary["configured_symbol_count"], summary["incomplete_symbol_count"]), (5, 4, 1))
        self.assertFalse(summary["execution_ready"])
        self.assertTrue(summary["values_only"])
        silver = summary["symbols"][4]
        self.assertEqual(silver["code"], "SHFE.ag2612")
        self.assertEqual(silver["code_new"], "SHFE.ag2702")
        self.assertEqual(silver["parameter_status"], "INCOMPLETE")
        self.assertEqual([silver[k] for k in ("open_offset", "stop_offset", "profit_offset")], ["0"] * 3)
        diagnostics = summary["diagnostics"]
        self.assertEqual([d["symbol_code"] for d in diagnostics if d["code"] == "SYMBOL_OFFSETS_INCOMPLETE"], ["SHFE.ag2612"])
        dormant = {d["path"] for d in diagnostics if d["code"] == "DORMANT_SAVED_PARAMETERS"}
        self.assertTrue({"trade.live_protect", "trade.max_symbols_enabled", "trade.half_close_enabled"} <= dormant)

    def test_global_and_symbol_half_parameters_are_not_merged(self):
        settings = self.profile().to_settings()
        self.assertEqual(settings["trade"]["half_close_ratio"], 0.5)
        self.assertTrue(all(row["half_close_ratio"] == 0.4 for row in settings["symbols"]))
        self.assertFalse(settings["trade"]["half_close_enabled"])
        self.assertFalse(settings["trade"]["half_stage2"])
        self.assertFalse(settings["trade"]["half_breakeven"])
        self.assertIn("HALF_RATIO_SCOPE_UNVERIFIED", {d["code"] for d in self.profile().diagnostics()})

    def test_symbol_shape_duplicate_identity_and_capacity_are_bounded(self):
        bad_symbols = (None, {}, [copy.deepcopy(BASE["symbols"][0])] * 2)
        for symbols in bad_symbols:
            settings = copy.deepcopy(BASE); settings["symbols"] = symbols
            self.rejected(settings)
        settings = copy.deepcopy(BASE)
        settings["symbols"] = [copy.deepcopy(BASE["symbols"][0]) | {"code": f"DCE.p{i + 1000}"} for i in range(MAX_SYMBOLS + 1)]
        self.rejected(settings)
        for bad in ("DCE.p2701\n", "not-a-contract", "DCE.p2701/../../x", True):
            settings = copy.deepcopy(BASE); settings["symbols"][0]["code"] = bad
            self.rejected(settings)
        settings = copy.deepcopy(BASE); settings["symbols"][0]["name"] = "name\ncontrol"
        self.rejected(settings)

    def test_dates_times_and_coefficients_reject_invalid_inputs(self):
        for bad in ("24:00", "9:00", "20:99", True):
            settings = copy.deepcopy(BASE); settings["schedule"]["day_start_time"] = bad
            self.rejected(settings)
        for bad in (["2026-02-30"], ["2026-11-09", "2026-11-09"], [True], {}):
            settings = copy.deepcopy(BASE); settings["trade"]["holidays"] = bad
            self.rejected(settings)
        settings = copy.deepcopy(BASE); settings["auto_offset"]["coef"] = {"p": True}
        self.rejected(settings)
        settings = copy.deepcopy(BASE); settings["auto_offset"]["coef"] = {"p;execute": 1}
        self.rejected(settings)

    def test_export_retains_exact_large_decimal_instead_of_silently_rounding(self):
        settings = copy.deepcopy(BASE)
        exact = Decimal("123456789012345678.123456789012")
        settings["auto_offset"]["coef"] = {"p": exact}
        profile = DsvProfile.from_settings(settings)
        exported = json.loads(profile.to_json(), parse_float=Decimal)
        self.assertEqual(exported["auto_offset"]["coef"]["p"], exact)
        self.assertEqual(DsvProfile.from_json(profile.to_json()).content_hash, profile.content_hash)
        with self.assertRaises(TraderError): profile.to_settings()

    def test_source_json_is_bounded_unicode_strict_and_handles_dsv_bom(self):
        encoded = json.dumps(BASE, ensure_ascii=False).encode("utf-8")
        self.assertEqual(DsvProfile.from_json(b"\xef\xbb\xbf" + encoded).to_settings(), BASE)
        for bad in (b"\xff", " " * MAX_SOURCE_BYTES + encoded.decode("utf-8"), "\ud800", {"settings": BASE}):
            with self.subTest(type=type(bad).__name__), self.assertRaises(TraderError):
                DsvProfile.from_json(bad)


if __name__ == "__main__":
    unittest.main()
