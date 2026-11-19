"""Validate delivery inputs and verify the assembled Windows portable archive."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from online_trader.accounts import validate_simulation_defaults
from online_trader.errors import TraderError
from online_trader.configuration.baseline import APPROVAL_RESOURCE, BASELINE_RESOURCE
from online_trader.configuration.dsv import DsvProfile
from online_trader.runtime.handoff import validate_handoff
from online_trader.runtime.operator_api import operator_records
from online_trader.runtime.portable_config import PortableAccountStore


DEFAULTS = 'OnlineTrader-defaults.json'
PARAMETERS = 'OnlineTrader-parameters.json'
EXECUTABLES = ('OnlineTrader.exe', 'OnlineTraderCLI.exe')
STARTUP_FILES = ('OnlineTrader.ini', 'update-source.json')
RUNTIME_STARTUP_FILES = ('start-trading-runtime.cmd', 'open-runtime-workbench.ps1')
SILVER_CONTRACT = re.compile(r'(?<![A-Za-z0-9])SHFE\.ag[0-9]*(?![A-Za-z0-9])', re.IGNORECASE)


def reject_silver_in_config(raw, name):
    def contains_silver(value):
        if isinstance(value, str):
            return '白银' in value or SILVER_CONTRACT.search(value) is not None
        if isinstance(value, list):
            return any(contains_silver(item) for item in value)
        if isinstance(value, dict):
            return contains_silver(list(value.keys())) or contains_silver(list(value.values()))
        return False

    try:
        has_silver = contains_silver(json.loads(raw.decode('utf-8-sig')))
    except (ValueError, RecursionError):
        raise ValueError(f'{name} must contain readable JSON for the silver configuration check.') from None
    if has_silver:
        raise ValueError(f'{name} contains a silver configuration.')


def validate_config(directory):
    defaults = directory / DEFAULTS
    try:
        with defaults.open('rb') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError
        document = json.loads(raw.decode('utf-8-sig'))
        validate_simulation_defaults(document['simulation'])
    except (OSError, ValueError, RecursionError, KeyError, TypeError, TraderError):
        raise ValueError(f'{DEFAULTS} must contain all four valid SimNow and KQ simulation fields.') from None
    parameters = directory / PARAMETERS
    try:
        with parameters.open('rb') as stream:
            parameter_bytes = stream.read(262145)
        if len(parameter_bytes) > 262144:
            raise ValueError
        DsvProfile.from_dsv_json(parameter_bytes)
    except (OSError, ValueError, RecursionError, TypeError):
        raise ValueError(f'{PARAMETERS} must contain valid DSV business settings.') from None
    reject_silver_in_config(raw, DEFAULTS)
    reject_silver_in_config(parameter_bytes, PARAMETERS)
    return [defaults, parameters]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _runtime_files(directory, kind):
    directory = directory.resolve()
    store = PortableAccountStore(directory)
    store.profile()
    store.runtime_control()
    operators = operator_records(directory)
    now = datetime.now(timezone.utc)
    bound_accounts = {account for _, accounts, _, expiry in operators.values() if expiry > now for account in accounts}
    if any(row['account_key'] not in bound_accounts for row in store.read()['accounts']):
        raise ValueError('A delivered simulation account lacks a current local operator grant.')
    names = ['runtime.json', 'runtime-control.json', PARAMETERS, 'private/' + DEFAULTS, 'private/operators.json']
    for name in names:
        reject_silver_in_config((directory / name).read_bytes(), name)
    if kind == 'handoff':
        expected = validate_handoff(store, require_checksums=True)
        names.append('handoff.json')
        names.extend(store.ledger_path(row).relative_to(directory).as_posix()
                     for row in store.read()['accounts'] if row['account_key'] in expected)
        if not expected:
            raise ValueError('A handoff delivery requires an original ledger checkpoint.')
    else:
        if (directory / 'handoff.json').exists() or (directory / 'handoff-exported.json').exists():
            raise ValueError('A fresh runtime delivery cannot reuse a handoff.')
        for row in store.read()['accounts']:
            ledger = store.ledger_path(row)
            if ledger.exists() or ledger.with_name('initialized.json').exists():
                raise ValueError('A fresh runtime delivery must use configuration-only inputs.')
    return names


def verify_bundle(bundle, directory, runtime_kind=None):
    if not all((bundle / name).is_file() for name in (*EXECUTABLES, *STARTUP_FILES)) or not (bundle / '_internal').is_dir():
        raise ValueError('Portable bundle is missing required executables, startup files or the shared runtime.')
    resources = bundle / '_internal' / 'online_trader' / 'resources'
    if (not (resources / APPROVAL_RESOURCE).is_file()
            or any(path.name.casefold() == BASELINE_RESOURCE.casefold() for path in bundle.rglob('*'))):
        raise ValueError('Portable bundle must contain the DSV approval with no historical trading preset.')
    defaults = [path for path in bundle.rglob('*') if path.name.casefold() == DEFAULTS.casefold()]
    if runtime_kind is not None:
        if defaults != [bundle / 'runtime' / 'private' / DEFAULTS]:
            raise ValueError('The standalone runtime requires one private account config in runtime/private.')
        if not all((bundle / name).is_file() for name in RUNTIME_STARTUP_FILES):
            raise ValueError('The standalone runtime bundle is missing its startup scripts.')
        runtime = bundle / 'runtime'
        names = _runtime_files(directory, runtime_kind)
        if set(names) != set(_runtime_files(runtime, runtime_kind)):
            raise ValueError('The delivered runtime file list differs from the input.')
        if any(digest(directory / name) != digest(runtime / name) for name in names):
            raise ValueError('A delivered runtime configuration or ledger differs from the authorized input.')
        return (*RUNTIME_STARTUP_FILES, *('runtime/' + name for name in names))
    parameters = [path for path in bundle.rglob('*') if path.name.casefold() == PARAMETERS.casefold()]
    if directory is None:
        if defaults:
            raise ValueError('Generic portable bundles must not include account defaults.')
        for path in parameters:
            reject_silver_in_config(path.read_bytes(), path.relative_to(bundle).as_posix())
        return
    if defaults != [bundle / DEFAULTS]:
        raise ValueError('Configured portable bundles require one account defaults file at the bundle root.')
    if parameters != [bundle / PARAMETERS]:
        raise ValueError('Configured portable bundles require one parameter file at the bundle root.')
    for source in validate_config(directory):
        if digest(source) != digest(bundle / source.name):
            raise ValueError(f'Bundled {source.name} differs from the delivery input.')
    validate_config(bundle)


def build_archive(bundle, destination, *, configured, output=None, important_members=()):
    if destination.exists():
        raise ValueError('Choose a new archive file.')
    named_configured = destination.name.casefold().endswith('-configured.zip')
    if destination.suffix.casefold() != '.zip' or named_configured != configured:
        raise ValueError('Use a .zip filename with the -Configured suffix only for Configured deliveries.')
    prefix = destination.stem + '/' if configured else ''
    source_files = {prefix + file.relative_to(bundle).as_posix(): file
                    for file in sorted(bundle.rglob('*')) if file.is_file()}
    important = {prefix + name for name in (*EXECUTABLES, *STARTUP_FILES, DEFAULTS, PARAMETERS, *important_members)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, prefix='.online-trader-', suffix='.tmp', delete=False) as stream:
        staging = Path(stream.name)
    try:
        with ZipFile(staging, 'w', compression=ZIP_DEFLATED, compresslevel=5) as archive:
            for member, source in source_files.items():
                archive.write(source, member)
        with ZipFile(staging) as archive:
            if set(archive.namelist()) != source_files.keys() or archive.testzip() is not None:
                raise ValueError('Portable ZIP content or CRC verification failed.')
            for member in important & source_files.keys():
                with archive.open(member) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != digest(source_files[member]):
                        raise ValueError('Portable ZIP member verification failed.')
        archive_sha256 = digest(staging)
        if output is not None:
            bundle.rename(output)
        try:
            staging.rename(destination)
        except OSError:
            if output is not None:
                output.rename(bundle)
            raise
        return archive_sha256
    finally:
        staging.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-directory', type=Path)
    parser.add_argument('--runtime-kind', choices=('fresh', 'handoff'))
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output is not None and (args.bundle is None or args.archive is None):
        parser.error('--output requires both --bundle and --archive')
    if args.runtime_kind is not None and args.config_directory is None:
        parser.error('--runtime-kind requires --config-directory')
    try:
        if args.runtime_kind is not None:
            names = _runtime_files(args.config_directory, args.runtime_kind)
            if args.bundle is not None:
                for name in names:
                    destination = args.bundle / 'runtime' / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(args.config_directory / name, destination)
        if args.bundle is None:
            if args.runtime_kind is None:
                validate_config(args.config_directory)
        else:
            important = verify_bundle(args.bundle, args.config_directory, args.runtime_kind)
            if args.archive is not None:
                archive_sha256 = build_archive(args.bundle, args.archive, output=args.output,
                                               configured=args.config_directory is not None,
                                               important_members=important or ())
                print(f'ZIP SHA-256: {archive_sha256}')
    except (OSError, ValueError, TraderError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
