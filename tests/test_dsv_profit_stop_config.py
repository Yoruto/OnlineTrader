"""Optional source switch compatibility; old canonical identities stay unchanged."""

import copy
import json
import unittest

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.errors import TraderError


# Captured from 426ac0f before changing either the parser or the runner.
EMPTY_SYMBOLS_BASELINE_HASH = '247857e6afee4c4b5d9d43102a94b07c71d161534bfc78209f90068281e4183a'


def original(**trade):
    return DsvProfile.from_dsv_json(json.dumps({'symbols': [], 'trade': trade}))


class ProfitStopConfigurationTests(unittest.TestCase):
    def test_absent_field_preserves_frozen_configuration_hash(self):
        profile = original()
        self.assertEqual(profile.content_hash, EMPTY_SYMBOLS_BASELINE_HASH)
        self.assertNotIn('stop_day_on_profit', profile.to_record()['settings']['trade'])

    def test_old_payload_keeps_identical_canonical_bytes(self):
        payload = original().to_record()
        before = canonical_bytes(payload)
        restored = DsvProfile.from_payload(copy.deepcopy(payload))
        self.assertEqual(canonical_bytes(restored.to_record()), before)
        self.assertEqual(restored.content_hash, EMPTY_SYMBOLS_BASELINE_HASH)

    def test_old_business_export_round_trips_without_materializing_default(self):
        profile = original()
        restored = DsvProfile.from_json(profile.to_json())
        self.assertEqual(restored.to_json(), profile.to_json())
        self.assertEqual(restored.content_hash, EMPTY_SYMBOLS_BASELINE_HASH)

    def test_original_explicit_false_is_retained(self):
        profile = original(stop_day_on_profit=False)
        self.assertIs(profile.to_settings()['trade']['stop_day_on_profit'], False)

    def test_original_explicit_true_is_retained(self):
        profile = original(stop_day_on_profit=True)
        self.assertIs(profile.to_settings()['trade']['stop_day_on_profit'], True)

    def test_business_and_canonical_exports_keep_explicit_false(self):
        profile = original(stop_day_on_profit=False)
        for restored in (DsvProfile.from_json(profile.to_json()),
                         DsvProfile.from_record(profile.to_record()),
                         DsvProfile.from_settings(profile.to_settings())):
            self.assertEqual(restored.content_hash, profile.content_hash)
            self.assertIs(restored.to_settings()['trade']['stop_day_on_profit'], False)

    def test_explicit_switch_has_its_own_identity(self):
        absent, enabled, disabled = original(), original(stop_day_on_profit=True), original(stop_day_on_profit=False)
        self.assertEqual(len({p.content_hash for p in (absent, enabled, disabled)}), 3)

    def test_full_dsv_preview_strips_credentials_not_business_switch(self):
        raw = json.dumps({'account': {'password': 'synthetic-do-not-export'}, 'symbols': [],
                          'trade': {'stop_day_on_profit': False}})
        profile = DsvProfile.from_preview_json(raw)
        self.assertIs(profile.to_settings()['trade']['stop_day_on_profit'], False)
        self.assertNotIn('synthetic-do-not-export', profile.to_json())

    def test_native_input_requires_an_explicit_boolean(self):
        for value in (None, 0, 1, 'false', [], {}):
            with self.subTest(value=value), self.assertRaises(TraderError):
                original(stop_day_on_profit=value)

    def test_canonical_input_requires_an_explicit_boolean(self):
        payload = original().to_record()
        for value in (None, 0, 1, 'false'):
            with self.subTest(value=value):
                candidate = copy.deepcopy(payload)
                candidate['settings']['trade']['stop_day_on_profit'] = value
                with self.assertRaises(TraderError):
                    DsvProfile.from_record(candidate)

    def test_unknown_setting_is_still_rejected(self):
        with self.assertRaises(TraderError):
            original(stop_day_on_profitt=True)

    def test_import_does_not_mutate_original_business_values(self):
        values = original().to_settings()
        values['trade']['stop_day_on_profit'] = False
        before = copy.deepcopy(values)
        DsvProfile.from_settings(values)
        self.assertEqual(values, before)

    def test_existing_pullback_migration_is_not_bypassed(self):
        migrated = original(stop_day_on_profit=False, half_pullback_close=True)
        preserved = original(stop_day_on_profit=False, half_pullback_close=True, half_pullback_migrated=True)
        self.assertIs(migrated.to_settings()['trade']['half_pullback_close'], False)
        self.assertIs(preserved.to_settings()['trade']['half_pullback_close'], True)


if __name__ == '__main__':
    unittest.main()
