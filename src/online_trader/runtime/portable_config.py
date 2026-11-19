"""Machine-independent input for the standalone simulation runtime."""

from contextlib import closing
from pathlib import Path
import sqlite3

from online_trader.accounts import AccountStore, account_identity
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import load_profile, publish_new_bytes
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import identifier
from online_trader.errors import TraderError
from online_trader.ledger.store import Ledger


_MAX_CONFIG_BYTES = 262_144
_ACCOUNT_FIELDS = {'id', 'alias', 'provider', 'broker', 'line'}
_PROVIDERS = {'TQKQ', 'TQ_SIMNOW'}
_KQ_FIELDS = {'username', 'password'}
_SIMNOW_FIELDS = {'simnow_username', 'simnow_password', 'kq_username', 'kq_password'}
_RUNTIME_CONTROL_FILE = 'runtime-control.json'
_RUNTIME_CONTROL_SCHEMA = 'ONLINE_TRADER_RUNTIME_CONTROL/1'
_DEFAULT_RUNTIME_CONTROL = {
    'schema_version': _RUNTIME_CONTROL_SCHEMA,
    'scheduled_start': {'enabled': False},
}


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


def read_runtime_control(root):
    """Read the hot-reloadable supervisor gate; durable trading facts stay in SQLite."""
    value = _read(Path(root).resolve(), _RUNTIME_CONTROL_FILE)
    try:
        scheduled = value['scheduled_start']
        if (type(value) is not dict or value.keys() != {'schema_version', 'scheduled_start'}
                or value['schema_version'] != _RUNTIME_CONTROL_SCHEMA or type(scheduled) is not dict
                or scheduled.keys() != {'enabled'} or type(scheduled['enabled']) is not bool):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise TraderError('E_RUNTIME_CONFIG', 'The runtime control configuration is invalid') from None
    return value


def ensure_runtime_control(root):
    """Create the safe disabled supervisor gate when a handoff source lacks it."""
    directory = Path(root).resolve()
    path = (directory / _RUNTIME_CONTROL_FILE).resolve()
    if not path.is_relative_to(directory):
        raise TraderError('E_RUNTIME_CONFIG', 'runtime-control.json must resolve inside the runtime directory')
    if not path.exists():
        try:
            publish_new_bytes(canonical_bytes(_DEFAULT_RUNTIME_CONTROL), path)
        except TraderError as error:
            if error.code != 'E_DUPLICATE_CONFLICT':
                raise TraderError('E_RUNTIME_CONFIG', 'Cannot create the default runtime control configuration') from None
    return read_runtime_control(directory)


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
            rows = [self._account(item, private['accounts']) for item in config['accounts']]
            if private['accounts'].keys() != self._aliases.keys():
                raise ValueError()
            default_id = self._aliases[config['selected_account']]
            self._state = {'revision': 1, 'previous_hash': '0' * 64, 'accounts': rows, 'selected_id': default_id}
            self._validate(self._state)
            if selected_id is not None:
                self._state['selected_id'] = self._aliases[selected_id]
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

    def change(self, *args, **kwargs):
        raise TraderError('E_RUNTIME_CONFIG', 'Edit the runtime account configuration through its local entry')

    save_credentials = forget_credentials = initialize_simulation_accounts = change

    def runtime_control(self):
        return read_runtime_control(self.directory)

    def profile(self):
        path = (self.directory / 'OnlineTrader-parameters.json').resolve()
        if not path.is_relative_to(self.directory):
            raise TraderError('E_RUNTIME_CONFIG', 'The parameter file must resolve inside the runtime directory')
        return load_profile(path)

    def ledger_path(self, row):
        path = super().ledger_path(row).resolve()
        if not path.is_relative_to(self.directory):
            raise TraderError('E_RUNTIME_CONFIG', 'The execution ledger must resolve inside the runtime directory')
        return path

    def initialize_ledger(self, row, lease):
        """Validate or initialize the original ledger before the first SDK connection."""
        from online_trader.runtime.handoff import validate_handoff

        if not lease.matches(row['account_key']):
            raise TraderError('E_INSTANCE_LOCK', 'The original account lock is required to open its ledger')
        handoff_accounts = validate_handoff(self)
        path = self.ledger_path(row)
        marker = path.with_name('initialized.json')
        identity = {'account_key': row['account_key'], 'environment': row['environment']}
        if marker.exists() and _read(self.directory, marker.relative_to(self.directory).as_posix()) != identity:
            raise TraderError('E_SCOPE', 'The initialized ledger marker belongs to a different account')
        existing = path.is_file()
        if not existing and (marker.exists() or row['account_key'] in handoff_accounts):
            raise TraderError('E_STORAGE', 'The existing execution ledger is missing')
        if path.exists() and not existing:
            raise TraderError('E_STORAGE', 'The execution ledger path is not a file')
        with Ledger(path, row['account_key'], environment=row['environment'], broker_identity=row['provider'],
                    existing_only=existing):
            pass
        if not marker.exists():
            publish_new_bytes(canonical_bytes(identity), marker)

    def effective_profile(self, row, default):
        """Recover the original owner's committed profile after the ledger identity check."""
        path = self.ledger_path(row)
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as stream:
                record = stream.execute('''SELECT config.payload_json, config.payload_hash
                    FROM authority JOIN config_revisions AS config USING (config_revision)
                    WHERE authority.singleton = 1''').fetchone()
            if record is None:
                return default
            value = strict_loads(record[0])
            if value.get('kind') != 'DSV_ONLINE_SIMULATION_EXECUTION':
                return default
            profile = DsvProfile.from_record(value['profile'])
            if content_hash(value) != record[1] or profile.content_hash != value['profile_hash']:
                raise ValueError()
            return profile
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            raise TraderError('E_RUNTIME_CONFIG', 'The committed execution profile could not be recovered') from None
