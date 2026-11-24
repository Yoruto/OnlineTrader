"""Machine-independent input for the standalone simulation runtime."""

import copy
from pathlib import Path

from online_trader.accounts import AccountStore, account_identity
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import load_profile, publish_replace_bytes
from online_trader.configuration.repository import (
    ParameterConfigRepository, merge_settings as _merge_settings, settings_diff as _settings_diff,
)
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import identifier
from online_trader.errors import TraderError
from online_trader.ledger.store import Ledger
from online_trader.runtime.instance import AccountLock


_MAX_CONFIG_BYTES = 262_144
_ACCOUNT_FIELDS = {'id', 'alias', 'provider', 'broker', 'line'}
_PROVIDERS = {'TQKQ', 'TQ_SIMNOW'}
_KQ_FIELDS = {'username', 'password'}
_SIMNOW_FIELDS = {'simnow_username', 'simnow_password', 'kq_username', 'kq_password'}
_OVERRIDES_FILE = 'OnlineTrader-overrides.json'
_OVERRIDES_SCHEMA = 'ONLINE_TRADER_PARAMETER_OVERRIDES/1'


def _read(root, name):
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


class _ConfiguredCredentials:
    """Read-only backend for the existing provider-specific credential references."""

    def __init__(self):
        self._values = {}

    def add(self, target, username, password):
        self._values[target] = {'username': username, 'password': password}

    def get(self, target):
        try:
            return self._values[target].copy()
        except KeyError:
            raise TraderError('E_CREDENTIALS', 'The selected account lacks a private configuration') from None


class PortableAccountStore(AccountStore):
    """Read-only configured account view for the current simulation owner."""

    def __init__(self, root, selected_id=None):
        super().__init__(root, vault=_ConfiguredCredentials())
        config = _read(self.directory, 'runtime.json')
        private = _read(self.directory, 'private/OnlineTrader-defaults.json')
        try:
            if (type(config) is not dict
                    or config.keys() != {'schema_version', 'selected_account', 'accounts'}
                    or config['schema_version'] != 'ONLINE_TRADER_RUNTIME/1'
                    or type(config['accounts']) is not list or not 1 <= len(config['accounts']) <= 64
                    or type(private) is not dict or private.keys() != {'accounts'}
                    or type(private['accounts']) is not dict):
                raise ValueError()
            self._aliases = {}
            self._config = config
            self._private = private
            rows = [self._account(item, private['accounts']) for item in config['accounts']]
            if private['accounts'].keys() != self._aliases.keys():
                raise ValueError()
            default_id = self._aliases[config['selected_account']]
            self._by_account_id = {row['account_id']: item['id'] for item, row in zip(config['accounts'], rows)}
            self._state = {'revision': 1, 'previous_hash': '0' * 64, 'accounts': rows, 'selected_id': default_id}
            self._validate(self._state)
            if selected_id is not None:
                if selected_id in self._aliases:
                    self._state['selected_id'] = self._aliases[selected_id]
                elif selected_id in self._by_account_id:
                    self._state['selected_id'] = selected_id
                else:
                    raise KeyError(selected_id)
        except (ValueError, KeyError, TypeError):
            raise TraderError('E_RUNTIME_CONFIG', 'Account, provider or private login configuration is invalid') from None

    def _account(self, item, credentials):
        if (type(item) is not dict or item.keys() != _ACCOUNT_FIELDS
                or any(type(value) is not str for value in item.values()) or item['provider'] not in _PROVIDERS):
            raise ValueError()
        name = identifier(item['id'])
        if name in self._aliases:
            raise ValueError()
        provider = item['provider']
        simnow = provider == 'TQ_SIMNOW'
        secret = credentials[name]
        if type(secret) is not dict or secret.keys() != (_SIMNOW_FIELDS if simnow else _KQ_FIELDS):
            raise ValueError()
        username = secret['simnow_username'] if simnow else secret['username']
        password = secret['simnow_password'] if simnow else secret['password']
        key, masked = account_identity(username, provider)
        account_id = 'account-' + content_hash(key)[:32]
        row = {'account_id': account_id, 'environment': 'ONLINE_SIM', 'account_key': key,
               'alias': item['alias'], 'provider': provider, 'masked_login': masked,
               'broker': item['broker'], 'line': item['line']}
        values = self._credential_values(row, username, password,
            secret['kq_username'] if simnow else None, secret['kq_password'] if simnow else None)
        for target, login, password in values:
            self.vault.add(target, login, password)
        self._aliases[name] = account_id
        return row

    def read(self):
        rows = [row.copy() for row in self._state['accounts']]
        return self._state | {'accounts': rows, 'journal_hash': content_hash(self._state)}

    def catalogue_hash(self):
        return content_hash(self._config)

    def select_saved_account(self, alias_or_account_id, *, expected_catalogue_hash=None):
        """Atomically persist ``selected_account`` in runtime.json under the catalogue lock."""
        with AccountLock('CATALOGUE:' + str(self.directory).casefold()):
            config = _read(self.directory, 'runtime.json')
            private = _read(self.directory, 'private/OnlineTrader-defaults.json')
            if (type(config) is not dict
                    or config.keys() != {'schema_version', 'selected_account', 'accounts'}
                    or config['schema_version'] != 'ONLINE_TRADER_RUNTIME/1'
                    or type(config['accounts']) is not list or not 1 <= len(config['accounts']) <= 64
                    or type(private) is not dict or private.keys() != {'accounts'}):
                raise TraderError('E_RUNTIME_CONFIG', 'Account or private configuration is invalid')
            if expected_catalogue_hash is not None and content_hash(config) != expected_catalogue_hash:
                raise TraderError('E_REVISION_CONFLICT', 'Runtime account catalogue changed before selection')
            target_alias = None
            target_row = None
            for item in config['accounts']:
                name = identifier(item['id'])
                simnow = item['provider'] == 'TQ_SIMNOW'
                secret = private['accounts'].get(name)
                if type(secret) is not dict:
                    raise TraderError('E_RUNTIME_CONFIG', 'Selected account lacks private login configuration')
                username = secret['simnow_username'] if simnow else secret['username']
                key, masked = account_identity(username, item['provider'])
                account_id = 'account-' + content_hash(key)[:32]
                if alias_or_account_id in (name, account_id):
                    target_alias = name
                    target_row = {
                        'account_id': account_id, 'environment': 'ONLINE_SIM', 'account_key': key,
                        'alias': item['alias'], 'provider': item['provider'], 'masked_login': masked,
                        'broker': item['broker'], 'line': item['line'],
                    }
                    break
            if target_alias is None or target_row is None:
                raise TraderError('E_RUNTIME_CONFIG', 'The selected account does not exist in runtime.json')
            updated = dict(config, selected_account=target_alias)
            publish_replace_bytes(canonical_bytes(updated), self.directory / 'runtime.json')
            self._config = updated
            self._state['selected_id'] = target_row['account_id']
            return target_row

    def change(self, *args, **kwargs):
        raise TraderError('E_RUNTIME_CONFIG', 'Edit the runtime account configuration through its local entry')

    save_credentials = forget_credentials = initialize_simulation_accounts = change

    def config_repository(self):
        return ParameterConfigRepository(self.directory)

    def profile(self):
        repo = self.config_repository()
        return repo.load_effective(repo.defaults_path())

    def profile_with_defaults(self, path):
        """Validate user overrides against an explicit version-default file."""
        return self.config_repository().load_effective(path)

    def save_profile(self, profile):
        """Persist only explicit differences from the current version defaults."""
        return self.config_repository().save_overrides(profile)

    def replace_profile(self, profile):
        """Persist ``profile`` and return the previous overrides bytes (``None`` when absent)."""
        return self.config_repository().replace_overrides(profile)

    def restore_overrides(self, previous):
        """Undo ``replace_profile`` with the bytes it returned."""
        return self.config_repository().restore_overrides(previous)

    def history_path(self, row):
        base = self.directory / 'data'
        if row['provider'] == 'TQ_SIMNOW':
            base = base / 'simnow'
        return (base / row['account_key'].split(':')[-1] / 'account-history.db').resolve()

    def ledger_path(self, row):
        path = super().ledger_path(row).resolve()
        if not path.is_relative_to(self.directory):
            raise TraderError('E_RUNTIME_CONFIG', 'The execution ledger must resolve inside the runtime directory')
        return path

    def initialize_session_ledger(self, row, lease, path):
        """Create a fresh process-owned execution store for this Runtime boot."""
        if not lease.matches(row['account_key']):
            raise TraderError('E_INSTANCE_LOCK', 'The original account lock is required to open its ledger')
        target = Path(path).resolve()
        if target.name != 'ledger.db' or target.exists():
            raise TraderError('E_STORAGE', 'The Runtime session ledger must be a new ledger.db')
        target.parent.mkdir(parents=True, exist_ok=True)
        with Ledger(target, row['account_key'], environment=row['environment'], broker_identity=row['provider']):
            pass

    def effective_profile(self, row, default):
        """Configuration files, never a prior execution database, define the next boot."""
        return self.profile()
