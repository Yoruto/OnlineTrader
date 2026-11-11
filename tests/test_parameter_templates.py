from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import publish_new_bytes
from online_trader.configuration.templates import (TemplateStore, TEMPLATE_KEYS, MAX_REVISION_BYTES,
    MAX_TEMPLATES, MAX_REVISIONS)
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError


class ParameterTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.directory = self.root / "templates"; self.store = TemplateStore(self.directory)
        self.profile = load_confirmed_baseline()[0]

    def tearDown(self): self.temp.cleanup()

    def altered(self, **values):
        record = self.profile.to_record(); record["settings"]["trade"].update(values)
        return DsvProfile.from_record(record)

    def error(self, call, code):
        with self.assertRaises(TraderError) as caught: call()
        self.assertEqual(caught.exception.code, code)

    def test_read_empty_neither_creates_directory_nor_kernel(self):
        with patch("online_trader.kernel.engine.Kernel", side_effect=AssertionError("No kernel")):
            snapshot = self.store.read()
        self.assertEqual(snapshot.summary()["names"], [])
        self.assertEqual(snapshot.revision, 0)
        self.assertFalse(snapshot.summary()["runtime_activation"])
        self.assertFalse(self.directory.exists())

    def test_save_only_exact_dsv_33_trade_keys_never_symbols_or_authority(self):
        snapshot = self.store.save("DSV默认", self.profile, expected_revision=0)
        self.assertEqual(len(TEMPLATE_KEYS), 33)
        self.assertEqual(set(snapshot.templates["DSV默认"]), TEMPLATE_KEYS)
        raw = next(self.directory.glob("*.json")).read_text("utf-8")
        for forbidden in ("account_key", "password", "symbols", "schedule", "auto_offset", "authority", "runtime_activation"):
            self.assertNotIn('"' + forbidden + '":', raw)
        self.assertEqual(self.store.read().journal_hash, snapshot.journal_hash)

    def test_apply_overlay_keeps_current_symbols_protection_and_non_template_fields(self):
        source = self.altered(max_volume=17, live_protect=True, half_close_enabled=True)
        self.store.save("甲", source, expected_revision=0)
        current = self.altered(max_volume=11, half_stage2=True)
        record = current.to_record(); record["settings"]["symbols"] = []
        record["settings"]["schedule"]["enabled"] = True
        current = DsvProfile.from_record(record)
        result = self.store.read().apply("甲", current).to_record()["settings"]
        self.assertEqual(result["trade"]["max_volume"], 17)
        self.assertFalse(result["trade"]["live_protect"])
        self.assertFalse(result["trade"]["half_close_enabled"])
        self.assertTrue(result["trade"]["half_stage2"])
        for section in ("symbols", "schedule", "auto_offset"): self.assertEqual(result[section], record["settings"][section])
        self.assertEqual(current.to_record(), record)

    def test_disabled_switches_and_precise_large_decimal_survive_restart(self):
        source = self.altered(risk_reference_equity="123456789012345678.123456789012", min_one_lot_fallback=False)
        self.store.save("精确参数", source, expected_revision=0)
        result = TemplateStore(self.directory).read().apply("精确参数", self.profile)
        self.assertEqual(result.to_record()["settings"]["trade"]["risk_reference_equity"], "123456789012345678.123456789012")
        self.assertFalse(result.to_record()["settings"]["trade"]["min_one_lot_fallback"])

    def test_overwrite_is_explicit_and_preserves_immutable_history(self):
        first = self.store.save("甲", self.profile, expected_revision=0)
        before = (self.directory / "00000001.json").read_bytes()
        self.error(lambda: self.store.save("甲", self.altered(max_volume=18), expected_revision=1), "E_DUPLICATE_CONFLICT")
        second = self.store.save("甲", self.altered(max_volume=18), expected_revision=1, overwrite=True)
        self.assertEqual(second.revision, 2)
        self.assertEqual((self.directory / "00000001.json").read_bytes(), before)
        self.assertNotEqual(first.journal_hash, second.journal_hash)
        self.assertEqual(first.apply("甲", self.profile).content_hash, self.profile.content_hash)
        self.assertEqual(second.templates["甲"]["max_volume"], 18)

    def test_delete_keeps_prior_values_and_can_recreate_same_name(self):
        old = self.store.save("甲", self.profile, expected_revision=0)
        deleted = self.store.delete("甲", expected_revision=1)
        self.assertEqual(deleted.templates, {})
        self.assertEqual(len(list(self.directory.glob("*.json"))), 2)
        self.assertEqual(old.apply("甲", self.profile).content_hash, self.profile.content_hash)
        recreated = self.store.save("甲", self.altered(max_volume=19), expected_revision=2)
        self.assertEqual(recreated.templates["甲"]["max_volume"], 19)

    def test_stale_save_overwrite_and_delete_leave_all_files_unchanged(self):
        self.store.save("甲", self.profile, expected_revision=0)
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        for action in (lambda: self.store.save("乙", self.profile, expected_revision=0),
                       lambda: self.store.save("甲", self.profile, expected_revision=0, overwrite=True),
                       lambda: self.store.delete("甲", expected_revision=0)):
            self.error(action, "E_TEMPLATE_CONFLICT")
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_missing_apply_overwrite_or_delete_refused(self):
        for action in (lambda: self.store.read().apply("未知", self.profile),
                       lambda: self.store.save("未知", self.profile, expected_revision=0, overwrite=True),
                       lambda: self.store.delete("未知", expected_revision=0)):
            self.error(action, "E_TEMPLATE_MISSING")
        self.assertFalse(self.directory.exists())

    def test_names_cannot_be_empty_paths_controls_or_ambiguous_unicode(self):
        for name in ("", " ", " 空格", "空格 ", ".", "..", "../甲", "甲/乙", "甲\\乙", "x\ny", "x\x00y", "x\u200by", "e\u0301", "x" * 65):
            with self.subTest(name=name): self.error(lambda: self.store.save(name, self.profile, expected_revision=0), "E_TEMPLATE")
        self.assertFalse(self.directory.exists())

    def test_literal_unicode_and_punctuation_names_are_keys_not_paths(self):
        snapshot = self.store.save("模板：α.1", self.profile, expected_revision=0)
        self.assertEqual(snapshot.summary()["names"], ["模板：α.1"])
        self.assertEqual([p.name for p in self.directory.iterdir()], ["00000001.json"])

    def test_revision_bool_negative_overflow_and_noninteger_refused(self):
        for value in (True, -1, MAX_REVISIONS, "0", 1.0):
            with self.subTest(value=value): self.error(lambda: self.store.save("甲", self.profile, expected_revision=value), "E_TEMPLATE")

    def test_invalid_profile_does_not_create_store(self):
        self.error(lambda: self.store.save("甲", {}, expected_revision=0), "E_TEMPLATE")
        self.assertFalse(self.directory.exists())

    def test_returned_snapshot_has_no_mutable_alias(self):
        snapshot = self.store.save("甲", self.profile, expected_revision=0)
        copy = snapshot.templates; copy["甲"]["max_volume"] = 999; copy["乙"] = {}
        self.assertNotIn("乙", snapshot.templates)
        self.assertNotEqual(snapshot.templates["甲"]["max_volume"], 999)

    def test_two_concurrent_writers_claim_one_revision(self):
        barrier = threading.Barrier(2)
        def publish(raw, path):
            barrier.wait(timeout=5)
            return publish_new_bytes(raw, path)
        def save(name):
            try: return self.store.save(name, self.profile, expected_revision=0)
            except TraderError as error: return error.code
        with patch("online_trader.configuration.templates.publish_new_bytes", side_effect=publish):
            with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(save, ("甲", "乙")))
        self.assertEqual(sum(r == "E_TEMPLATE_CONFLICT" for r in results), 1)
        self.assertEqual(self.store.read().revision, 1)
        self.assertEqual(len(self.store.read().templates), 1)

    def test_publication_failure_keeps_previous_revision_and_cleans_temporary(self):
        first = self.store.save("甲", self.profile, expected_revision=0)
        with patch("online_trader.configuration.files.os.link", side_effect=OSError("Injected failure")):
            with self.assertRaises(TraderError): self.store.delete("甲", expected_revision=1)
        self.assertEqual(self.store.read().journal_hash, first.journal_hash)
        self.assertEqual([p.name for p in self.directory.iterdir()], ["00000001.json"])

    def test_unpublished_temporary_is_ignored_and_retry_can_succeed(self):
        self.directory.mkdir(); (self.directory / ".online-trader-parameters-interrupted.tmp").write_bytes(b"partial")
        self.assertEqual(self.store.read().revision, 0)
        self.assertEqual(self.store.save("甲", self.profile, expected_revision=0).revision, 1)

    def test_revision_gap_prevents_load_and_write(self):
        self.store.save("甲", self.profile, expected_revision=0)
        (self.directory / "00000001.json").rename(self.directory / "00000002.json")
        self.error(self.store.read, "E_TEMPLATE")
        self.error(lambda: self.store.save("乙", self.profile, expected_revision=0), "E_TEMPLATE")

    def test_corrupt_and_oversize_journal_not_treated_as_empty(self):
        self.directory.mkdir(); path = self.directory / "00000001.json"
        for raw in (b"\xff", b"{}", b'{"revision":1,"revision":1}', b"x" * (MAX_REVISION_BYTES + 1)):
            path.write_bytes(raw)
            with self.subTest(raw=raw[:20]): self.error(self.store.read, "E_TEMPLATE")

    def test_hash_chain_and_unsupported_record_fields_are_checked(self):
        self.store.save("甲", self.profile, expected_revision=0)
        self.store.save("乙", self.profile, expected_revision=1)
        path = self.directory / "00000002.json"
        from online_trader.contracts.codec import strict_loads
        record = strict_loads(path.read_bytes())
        for change in ({"previous_hash": "f" * 64}, {"action": "ACTIVATE"}, {"action": []}, {"password": "PRIVATE"}, {"revision": True}):
            path.write_bytes(canonical_bytes(record | change))
            with self.subTest(change=change): self.error(self.store.read, "E_TEMPLATE")

    def test_template_trade_keys_and_values_are_revalidated(self):
        self.store.save("甲", self.profile, expected_revision=0)
        path = self.directory / "00000001.json"
        from online_trader.contracts.codec import strict_loads
        original = strict_loads(path.read_bytes())
        for key, value in (("max_volume", True), ("max_risk_ratio", "NaN"), ("password", "PRIVATE")):
            record = strict_loads(canonical_bytes(original)); record["trade"][key] = value
            path.write_bytes(canonical_bytes(record))
            with self.subTest(key=key): self.error(self.store.read, "E_TEMPLATE")

    def test_template_count_cap_before_publication(self):
        with patch("online_trader.configuration.templates.MAX_TEMPLATES", 2):
            self.store.save("甲", self.profile, expected_revision=0)
            self.store.save("乙", self.profile, expected_revision=1)
            self.error(lambda: self.store.save("丙", self.profile, expected_revision=2), "E_TEMPLATE")
        self.assertEqual(self.store.read().revision, 2)

    def test_store_file_and_io_failure_have_specific_error(self):
        self.directory.write_bytes(b"not-directory")
        self.error(self.store.read, "E_TEMPLATE")
        with patch.object(Path, "glob", side_effect=OSError("PRIVATE")):
            with self.assertRaises(TraderError) as caught: TemplateStore(self.root).read()
        self.assertEqual(caught.exception.code, "E_TEMPLATE_IO")
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_saved_keys_and_alias_data_match_independent_static_dsv_evidence(self):
        import json
        from online_trader.configuration.symbol_csv import ALIASES, HEADERS
        evidence = json.loads((Path(__file__).parents[1] / "docs/references/dsv-parameter-workspace-20261110.json").read_text("utf-8"))
        self.assertEqual(TEMPLATE_KEYS, set(evidence["template_save_keys"]))
        self.assertEqual(dict(ALIASES), {name.casefold(): tuple(value) for name, value in evidence["symbol_aliases"].items()})
        self.assertEqual(HEADERS[:8], tuple(evidence["csv_headers"]))
        self.assertEqual(evidence["gui_sha256"], "07c82c9ada765284ed5a929227024cbf192f5f72192636900740d9a5764032ea")
