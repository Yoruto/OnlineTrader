"""Cold transfer of the original simulation ledger and its portable configuration."""

from contextlib import ExitStack, closing
from hashlib import file_digest
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.domain.values import sequence
from online_trader.errors import TraderError
from online_trader.ledger.store import Ledger
from online_trader.runtime.desktop_bootstrap import _check_existing_ledger
from online_trader.runtime.instance import AccountLock
from online_trader.runtime.portable_config import PortableAccountStore, _read


_SCHEMA = 'ONLINE_TRADER_HANDOFF/1'
_CONFIG_FILES = ('runtime.json', 'OnlineTrader-parameters.json', 'private/OnlineTrader-defaults.json')
_OPERATORS = 'private/operators.json'
_RUNTIME_CONTROL = 'runtime-control.json'


def _digest(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def _inside(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise TraderError('E_RUNTIME_HANDOFF', 'A handoff file is outside the runtime directory')
    return path


def _config_files(root):
    result = list(_CONFIG_FILES)
    if _inside(root, _RUNTIME_CONTROL).is_file():
        result.append(_RUNTIME_CONTROL)
    if _inside(root, _OPERATORS).is_file():
        result.append(_OPERATORS)
    return tuple(result)


def _ledger_entry(root, path, row):
    return {'account_key': row['account_key'], 'provider': row['provider'],
            'path': path.relative_to(root).as_posix()}


def _valid_stop_payload(raw):
    payload = strict_loads(raw)
    facts = payload.get('data') if type(payload) is dict else None
    return (type(facts) is dict and facts.get('positions') == 0 and facts.get('active_orders') == 0
            and bool(facts.get('native_snapshot_id')))


def _verify_ledger(root, entry):
    path = _inside(root, entry['path']).resolve()
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
            scope = connection.execute('SELECT account_key, environment, broker_identity FROM ledger_scope').fetchall()
            if scope != [(entry['account_key'], 'ONLINE_SIM', entry['provider'])]:
                raise TraderError('E_SCOPE', 'The handoff ledger belongs to a different account or provider')
            checkpoint = connection.execute('SELECT coalesce(max(event_seq),0) FROM event_journal').fetchone()[0]
            if str(checkpoint) != entry['checkpoint_event_seq'] or connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise TraderError('E_STORAGE', 'The handoff ledger checkpoint or integrity differs')
            if entry['stop_event_seq'] is not None:
                latest_stop = connection.execute('''SELECT event_seq, payload_json FROM event_journal
                    WHERE event_type = 'ONLINE_DSV_STOP_VERIFIED' AND kernel_boot_id =
                    (SELECT kernel_boot_id FROM kernel_boots ORDER BY kernel_epoch DESC LIMIT 1)
                    ORDER BY event_seq DESC LIMIT 1''').fetchone()
                if (latest_stop is None or str(latest_stop[0]) != entry['stop_event_seq']
                        or not _valid_stop_payload(latest_stop[1])):
                    raise TraderError('E_FACT_BARRIER', 'The handoff stop proof differs from the original ledger')
    except sqlite3.Error:
        raise TraderError('E_STORAGE', 'Cannot verify the original handoff ledger') from None


def _closed_boundary(ledger):
    last_boot = ledger.query('SELECT kernel_boot_id FROM kernel_boots ORDER BY kernel_epoch DESC LIMIT 1')
    checkpoint = ledger.query('SELECT max(event_seq) AS seq FROM event_journal')[0]['seq'] or 0
    if not last_boot:
        return {'checkpoint_event_seq': str(checkpoint), 'stop_event_seq': None}
    stop = ledger.query('''SELECT event_seq, payload_json FROM event_journal
        WHERE kernel_boot_id = ? AND event_type = 'ONLINE_DSV_STOP_VERIFIED' ORDER BY event_seq DESC LIMIT 1''',
        (last_boot[0]['kernel_boot_id'],))
    if not stop:
        raise TraderError('E_FACT_BARRIER', 'Stop the original simulation owner with a complete cabinet flat check')
    if not _valid_stop_payload(stop[0]['payload_json']):
        raise TraderError('E_FACT_BARRIER', 'The original owner did not prove a completely closed account')
    newer = ledger.query('''SELECT 1 FROM event_journal WHERE event_seq > ? AND tx_kind IN
        ('TX_INTENT','TX_FILL','TX_PROTECT','TX_SWITCH','TX_TAKEOVER','TX_CANCEL','TX_ORDER','TX_BOOT') LIMIT 1''',
        (stop[0]['event_seq'],))
    if newer:
        raise TraderError('E_FACT_BARRIER', 'Execution facts changed after the final cabinet check')
    return {'checkpoint_event_seq': str(checkpoint), 'stop_event_seq': str(stop[0]['event_seq'])}


def export_handoff(store, output):
    """Export the entire confirmed runtime directory to a new path while owners are stopped."""
    root, destination = store.directory, Path(output).resolve()
    if destination.exists() or destination.is_relative_to(root):
        raise TraderError('E_RUNTIME_HANDOFF', 'Choose a new destination outside the source runtime directory')
    marker = root / 'handoff-exported.json'
    rows = sorted(store.read()['accounts'], key=lambda row: row['account_key'])
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with ExitStack() as locks, TemporaryDirectory(dir=destination.parent, prefix='.online-trader-handoff-') as work:
            for row in rows:
                locks.enter_context(AccountLock(row['account_key']))
            required = validate_handoff(store)
            staged = Path(work) / 'runtime'
            staged.mkdir()
            hashes = {}
            for relative in _config_files(root):
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_inside(root, relative), target)
                hashes[relative] = _digest(target)

            accounts = []
            for row in rows:
                path = store.ledger_path(row)
                if not path.is_file():
                    if (path.exists() or path.with_name('initialized.json').exists()
                            or row['account_key'] in required):
                        raise TraderError('E_STORAGE', 'An existing source ledger is unavailable')
                    accounts.append(_ledger_entry(root, path, row) | {'initialized': False})
                    continue
                _check_existing_ledger(store, row)
                entry = _ledger_entry(root, path, row)
                target = staged / entry['path']
                with Ledger(path, row['account_key'], environment=row['environment'], broker_identity=row['provider'],
                            existing_only=True) as ledger:
                    boundary = _closed_boundary(ledger)
                    ledger.backup(target)
                hashes[entry['path']] = _digest(target)
                accounts.append(entry | {'initialized': True, **boundary})
            if not any(entry['initialized'] for entry in accounts):
                raise TraderError('E_RUNTIME_HANDOFF', 'Use the configuration files for an account that has never started')

            manifest = {'schema_version': _SCHEMA, 'handoff_id': 'handoff-' + uuid4().hex,
                        'accounts': accounts, 'files': hashes}
            publish_new_bytes(canonical_bytes(manifest), staged / 'handoff.json')
            validate_handoff(PortableAccountStore(staged), require_checksums=True)
            receipt = {'handoff_id': manifest['handoff_id'], 'destination': str(destination)}
            publish_new_bytes(canonical_bytes(receipt), marker)
            try:
                staged.rename(destination)
            except OSError:
                marker.unlink()
                raise
            return {'status': 'EXPORTED', 'destination': str(destination), 'handoff_id': manifest['handoff_id']}
    except OSError:
        raise TraderError('E_RUNTIME_HANDOFF', 'Cannot publish the consistent portable handoff') from None


def validate_handoff(store, *, require_checksums=False):
    """Validate exported paths and file hashes before the first connection on the target."""
    root = store.directory
    if (root / 'handoff-exported.json').exists():
        raise TraderError('E_RUNTIME_HANDOFF', 'This source runtime has handed off its execution state')
    if not (root / 'handoff.json').exists():
        if require_checksums or (root / 'handoff-imported.json').exists():
            raise TraderError('E_RUNTIME_HANDOFF', 'The handoff manifest is missing')
        return frozenset()
    manifest = _read(root, 'handoff.json')
    if (type(manifest) is not dict or manifest.keys() != {'schema_version', 'handoff_id', 'accounts', 'files'}
            or manifest['schema_version'] != _SCHEMA or type(manifest['handoff_id']) is not str
            or not manifest['handoff_id'].startswith('handoff-') or type(manifest['accounts']) is not list
            or type(manifest['files']) is not dict):
        raise TraderError('E_RUNTIME_HANDOFF', 'The handoff manifest format is invalid')
    marker = root / 'handoff-imported.json'
    if not require_checksums and marker.exists():
        imported = _read(root, marker.name)
        if (type(imported) is not dict or imported.keys() != {'handoff_id', 'required_accounts'}
                or imported['handoff_id'] != manifest['handoff_id']
                or type(imported['required_accounts']) is not list
                or any(type(account) is not str for account in imported['required_accounts'])):
            raise TraderError('E_RUNTIME_HANDOFF', 'This directory already imported a different handoff')
        return frozenset(imported['required_accounts'])
    expected = {row['account_key']: _ledger_entry(root, store.ledger_path(row), row)
                for row in store.read()['accounts']}
    if len(expected) != len(manifest['accounts']):
        raise TraderError('E_RUNTIME_HANDOFF', 'The handoff accounts differ from the runtime configuration')
    entries = {}
    files = set(_config_files(root))
    for entry in manifest['accounts']:
        if type(entry) is not dict or type(entry.get('account_key')) is not str or type(entry.get('initialized')) is not bool:
            raise TraderError('E_RUNTIME_HANDOFF', 'A handoff account record is invalid')
        account = entry['account_key']
        template = expected.get(account)
        fields = {'initialized', *template} if template else set()
        if entry['initialized']:
            fields.update(('checkpoint_event_seq', 'stop_event_seq'))
        if (template is None or account in entries or entry.keys() != fields
                or any(entry[key] != value for key, value in template.items())):
            raise TraderError('E_RUNTIME_HANDOFF', 'A handoff ledger binding differs from configuration')
        if entry['initialized']:
            checkpoint = sequence(entry['checkpoint_event_seq'])
            stop = entry['stop_event_seq']
            if (stop is None and checkpoint != 0) or (stop is not None and sequence(stop, positive=True) > checkpoint):
                raise TraderError('E_RUNTIME_HANDOFF', 'A handoff ledger stop point is invalid')
            files.add(entry['path'])
        entries[account] = entry
    if manifest['files'].keys() != files:
        raise TraderError('E_RUNTIME_HANDOFF', 'The handoff file list is incomplete')
    try:
        for relative, digest in manifest['files'].items():
            if type(digest) is not str or _digest(_inside(root, relative)) != digest:
                raise TraderError('E_RUNTIME_HANDOFF', 'A configuration or ledger differs from the exported digest')
    except OSError:
        raise TraderError('E_RUNTIME_HANDOFF', 'A configuration or ledger file is missing or unreadable') from None
    for entry in entries.values():
        if entry['initialized']:
            _verify_ledger(root, entry)
    required = sorted(account for account, entry in entries.items() if entry['initialized'])
    if not require_checksums:
        imported = {'handoff_id': manifest['handoff_id'], 'required_accounts': required}
        try:
            publish_new_bytes(canonical_bytes(imported), marker)
        except TraderError as error:
            if error.code != 'E_DUPLICATE_CONFLICT' or _read(root, marker.name) != imported:
                raise
    return frozenset(required)
