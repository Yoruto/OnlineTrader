"""Parameter configuration repository and atomic hot-save apply marker."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import load_profile, publish_replace_bytes
from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.errors import TraderError


_MAX_CONFIG_BYTES = 262_144
_OVERRIDES_FILE = 'OnlineTrader-overrides.json'
_OVERRIDES_SCHEMA = 'ONLINE_TRADER_PARAMETER_OVERRIDES/1'
_APPLY_MARKER_FILE = 'OnlineTrader-parameters-pending.json'
_APPLY_MARKER_SCHEMA = 'ONLINE_TRADER_CONFIG_APPLY_MARKER/1'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _read_bounded_json(root: Path, name: str):
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise TraderError('E_RUNTIME_CONFIG', f'{name} must resolve inside the runtime directory')
    try:
        with path.open('rb') as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError()
        return strict_loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError):
        raise TraderError('E_RUNTIME_CONFIG', f'Cannot read a valid {name}') from None


def merge_settings(defaults, overrides):
    if type(defaults) is not dict or type(overrides) is not dict:
        raise ValueError()
    result = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if key not in defaults:
            raise ValueError()
        result[key] = (
            merge_settings(defaults[key], value)
            if type(defaults[key]) is dict and type(value) is dict
            else copy.deepcopy(value)
        )
    return result


def settings_diff(defaults, desired):
    if type(defaults) is not dict or type(desired) is not dict or defaults.keys() != desired.keys():
        raise TraderError('E_RUNTIME_CONFIG', 'The saved parameters differ from the version configuration schema')
    result = {}
    for key in defaults:
        before, after = defaults[key], desired[key]
        if type(before) is dict and type(after) is dict:
            nested = settings_diff(before, after)
            if nested:
                result[key] = nested
        elif before != after:
            result[key] = copy.deepcopy(after)
    return result


class ParameterConfigRepository:
    """Manage version defaults + user overrides merging and hot-save apply markers."""

    def __init__(self, directory: Path, *, defaults_name: str = 'OnlineTrader-parameters.json'):
        self.directory = Path(directory).resolve()
        self.defaults_name = defaults_name

    def defaults_path(self) -> Path:
        path = (self.directory / self.defaults_name).resolve()
        if not path.is_relative_to(self.directory):
            raise TraderError('E_RUNTIME_CONFIG', 'The parameter file must resolve inside the runtime directory')
        return path

    def load_effective(self, defaults_path: Path | None = None) -> DsvProfile:
        path = self.defaults_path() if defaults_path is None else Path(defaults_path).resolve()
        baseline = load_profile(path, dsv=True)
        overrides = self.directory / _OVERRIDES_FILE
        if not overrides.exists():
            return baseline
        value = _read_bounded_json(self.directory, _OVERRIDES_FILE)
        if (type(value) is not dict or value.keys() != {'schema_version', 'overrides'}
                or value['schema_version'] != _OVERRIDES_SCHEMA or type(value['overrides']) is not dict):
            raise TraderError('E_RUNTIME_CONFIG', 'The Runtime parameter overrides are invalid')
        try:
            settings = merge_settings(baseline.to_record()['settings'], value['overrides'])
            return DsvProfile.from_record({'schema_version': 'DSV_SETTINGS/1', 'settings': settings})
        except (KeyError, TypeError, ValueError, TraderError):
            raise TraderError(
                'E_RUNTIME_CONFIG',
                'The saved parameter overrides do not apply to this version defaults',
            ) from None

    def save_overrides(self, profile: DsvProfile) -> dict:
        if not isinstance(profile, DsvProfile):
            raise TraderError('E_RUNTIME_CONFIG', 'Expected validated Runtime parameters')
        baseline = load_profile(self.defaults_path(), dsv=True)
        overrides = settings_diff(baseline.to_record()['settings'], profile.to_record()['settings'])
        value = {'schema_version': _OVERRIDES_SCHEMA, 'overrides': overrides}
        publish_replace_bytes(canonical_bytes(value), self.directory / _OVERRIDES_FILE)
        return value

    def replace_overrides(self, profile: DsvProfile) -> bytes | None:
        path = self.directory / _OVERRIDES_FILE
        try:
            previous = path.read_bytes() if path.exists() else None
        except OSError as error:
            raise TraderError('E_CONFIG_FILE', 'Cannot read the current parameter overrides') from error
        self.save_overrides(profile)
        return previous

    def restore_overrides(self, previous: bytes | None) -> None:
        path = self.directory / _OVERRIDES_FILE
        if previous is None:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                raise TraderError('E_CONFIG_FILE', 'Cannot restore the parameter overrides') from error
            return
        publish_replace_bytes(previous, path)

    def marker_path(self) -> Path:
        return self.directory / _APPLY_MARKER_FILE

    def write_pending_apply_marker(
        self,
        *,
        account_key: str,
        boot_id: str,
        old_hash: str,
        new_hash: str,
        boundary: str = 'HOT_SAVE',
        apply_id: str | None = None,
    ) -> dict:
        marker = {
            'schema_version': _APPLY_MARKER_SCHEMA,
            'apply_id': apply_id or ('apply-' + uuid4().hex),
            'account_key': account_key,
            'boot_id': boot_id,
            'old_hash': old_hash,
            'new_hash': new_hash,
            'boundary': boundary,
            'created_at_utc': _utc_now(),
        }
        try:
            publish_replace_bytes(canonical_bytes(marker), self.marker_path())
        except (OSError, TraderError) as error:
            raise TraderError('E_CONFIG_FILE', 'Cannot write the parameter apply marker') from error
        return marker

    def read_pending_apply_marker(self) -> dict | None:
        path = self.marker_path()
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            if len(raw) > 16_384:
                raise ValueError()
            value = strict_loads(raw.decode('utf-8-sig'))
            if (type(value) is not dict
                    or value.get('schema_version') != _APPLY_MARKER_SCHEMA
                    or not isinstance(value.get('old_hash'), str)
                    or not isinstance(value.get('new_hash'), str)):
                raise ValueError()
            return value
        except (OSError, ValueError) as error:
            raise TraderError('E_CONFIG_FILE', 'The parameter apply marker is corrupted') from error

    def clear_pending_apply_marker(self) -> None:
        try:
            self.marker_path().unlink(missing_ok=True)
        except OSError as error:
            raise TraderError('E_CONFIG_FILE', 'Cannot clear the parameter apply marker') from error
