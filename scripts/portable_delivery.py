"""Validate delivery inputs and verify the assembled Windows portable archive."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import perf_counter
from zipfile import ZIP_DEFLATED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from online_trader.accounts import validate_simulation_defaults
from online_trader.errors import TraderError
from online_trader.configuration.baseline import APPROVAL_RESOURCE, BASELINE_RESOURCE
from online_trader.configuration.dsv import DsvProfile
from online_trader.runtime.portable_config import PortableAccountStore
from online_trader.updates import (
    PAYLOAD_MARKER,
    PAYLOAD_SCHEMA,
    UpdateError,
    archive_name,
    extract_bundle,
    load_update_source,
)


DEFAULTS = 'OnlineTrader-defaults.json'
PARAMETERS = 'OnlineTrader-parameters.json'
PARAMETER_OVERRIDES = 'OnlineTrader-overrides.json'
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


def update_build_manifest(bundle, directory, names, *, deliverable):
    path = bundle / 'base-build-manifest.json'
    try:
        document = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, TypeError):
        raise ValueError('The base bundle is missing a readable build manifest.') from None
    document.update({
        'artifact_class': 'DELIVERY' if deliverable else 'TEST-ONLY',
        'deliverable': deliverable,
        'delivery': {
            'mode': 'RuntimeConfigured',
            'config_hashes': {name: digest(directory / name) for name in sorted(names)},
            'silver_check': 'PASS',
            'retired_file_check': 'PASS',
        },
    })
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _runtime_files(directory, kind):
    directory = directory.resolve()
    store = PortableAccountStore(directory)
    store.profile()
    names = ['runtime.json', PARAMETERS, 'private/' + DEFAULTS]
    if (directory / PARAMETER_OVERRIDES).is_file():
        names.append(PARAMETER_OVERRIDES)
    for name in names:
        reject_silver_in_config((directory / name).read_bytes(), name)
    if kind != 'fresh':
        raise ValueError('Runtime delivery no longer accepts execution-ledger handoff state.')
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
            raise ValueError('A delivered runtime configuration differs from the authorized input.')
        return (*RUNTIME_STARTUP_FILES, *('runtime/' + name for name in names))
    parameters = [path for path in bundle.rglob('*') if path.name.casefold() == PARAMETERS.casefold()]
    if directory is None:
        raise ValueError('A portable delivery must include an explicitly confirmed configuration.')
    if defaults != [bundle / DEFAULTS]:
        raise ValueError('Configured portable bundles require one account defaults file at the bundle root.')
    if parameters != [bundle / PARAMETERS]:
        raise ValueError('Configured portable bundles require one parameter file at the bundle root.')
    for source in validate_config(directory):
        if digest(source) != digest(bundle / source.name):
            raise ValueError(f'Bundled {source.name} differs from the delivery input.')
    validate_config(bundle)


def build_archive(bundle, destination, *, configured, output=None, important_members=(), metrics=None):
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
        archive_started = perf_counter()
        with ZipFile(staging, 'w', compression=ZIP_DEFLATED, compresslevel=5) as archive:
            for member, source in source_files.items():
                archive.write(source, member)
        if metrics is not None:
            metrics['archive_seconds'] = round(perf_counter() - archive_started, 6)
        verify_started = perf_counter()
        with ZipFile(staging) as archive:
            if set(archive.namelist()) != source_files.keys() or archive.testzip() is not None:
                raise ValueError('Portable ZIP content or CRC verification failed.')
            for member in important & source_files.keys():
                with archive.open(member) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != digest(source_files[member]):
                        raise ValueError('Portable ZIP member verification failed.')
        archive_sha256 = digest(staging)
        if metrics is not None:
            metrics['archive_verify_seconds'] = round(perf_counter() - verify_started, 6)
            metrics['archive_members'] = len(source_files)
            metrics['archive_sha256'] = archive_sha256
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


def apply_update_source(bundle, *, version=None, host=None, project_id=None, channel=None, read_token_file=None):
    source_path = bundle / 'update-source.json'
    document = json.loads(source_path.read_text(encoding='utf-8-sig')) if source_path.is_file() else {}
    if not isinstance(document, dict):
        raise ValueError('Base update-source.json must be a JSON object.')
    if version is not None:
        document['portable_version'] = version
    if host is not None:
        document['host'] = host
    if project_id is not None:
        document['project_id'] = project_id
    if channel is not None:
        document['channel'] = channel
    if read_token_file is not None:
        token = Path(read_token_file).read_text(encoding='utf-8-sig').strip()
        if not token:
            raise ValueError('Read token file must not be empty.')
        document['read_token'] = token
    source_path.write_text(json.dumps(document, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    load_update_source(bundle)


def prepare_update_payload(bundle, version):
    for rel in (
        'OnlineTrader.ini',
        'update-source.json',
        'active.json',
        'base-build-manifest.json',
        DEFAULTS,
        PARAMETERS,
        PARAMETER_OVERRIDES,
        *RUNTIME_STARTUP_FILES,
    ):
        target = bundle / rel
        if target.is_file():
            target.unlink()
    for folder in ('runtime', 'updates', 'versions'):
        target = bundle / folder
        if target.is_dir():
            shutil.rmtree(target)
    if not all((bundle / name).is_file() for name in EXECUTABLES) or not (bundle / '_internal').is_dir():
        raise ValueError('Update payload bundle is missing required executables or _internal.')
    resources = bundle / '_internal' / 'online_trader' / 'resources'
    if (not (resources / APPROVAL_RESOURCE).is_file()
            or any(path.name.casefold() == BASELINE_RESOURCE.casefold() for path in bundle.rglob('*'))):
        raise ValueError('Update payload must contain the DSV approval with no historical trading preset.')
    marker = bundle / PAYLOAD_MARKER
    marker.write_text(
        json.dumps(
            {
                'schema_version': PAYLOAD_SCHEMA,
                'version': version,
                'standalone': False,
                'requires_managed_root': True,
            },
            ensure_ascii=False,
            indent=2,
        ) + '\n',
        encoding='utf-8',
    )
    for path in sorted(bundle.rglob('*.json')):
        if path.relative_to(bundle).parts[0].casefold() == '_internal':
            continue
        reject_silver_in_config(path.read_bytes(), path.relative_to(bundle).as_posix())
    return (PAYLOAD_MARKER,)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-directory', type=Path)
    parser.add_argument('--runtime-kind', choices=('fresh',))
    parser.add_argument('--update-payload', action='store_true')
    parser.add_argument('--version')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--performance-report', type=Path)
    parser.add_argument('--update-host')
    parser.add_argument('--update-project-id', type=int)
    parser.add_argument('--update-channel')
    parser.add_argument('--update-read-token-file', type=Path)
    args = parser.parse_args()
    if args.output is not None and (args.bundle is None or args.archive is None):
        parser.error('--output requires both --bundle and --archive')
    if args.update_payload:
        if args.bundle is None or args.archive is None or not args.version:
            parser.error('--update-payload requires --bundle, --archive, and --version')
        if args.runtime_kind is not None or args.config_directory is not None:
            parser.error('--update-payload cannot be combined with --runtime-kind or --config-directory')
    else:
        if args.runtime_kind is not None and args.config_directory is None:
            parser.error('--runtime-kind requires --config-directory')
        if args.bundle is not None and args.runtime_kind is None:
            parser.error('assembled delivery bundles require --runtime-kind fresh')
    metrics = {'schema_version': 'ONLINE_TRADER_DELIVERY_PERFORMANCE/1', 'result': 'FAIL'}
    total_started = perf_counter()
    try:
        if args.update_payload:
            assembly_started = perf_counter()
            if args.archive.name != archive_name(args.version):
                raise ValueError(f'Update payload archive must be named {archive_name(args.version)}.')
            important = prepare_update_payload(args.bundle, args.version)
            metrics['assembly_seconds'] = round(perf_counter() - assembly_started, 6)
            archive_sha256 = build_archive(
                args.bundle,
                args.archive,
                output=args.output,
                configured=False,
                important_members=important,
                metrics=metrics,
            )
            verify_started = perf_counter()
            with TemporaryDirectory(prefix='.verify-payload-') as verify_dir:
                extract_bundle(args.archive, Path(verify_dir), expected_version=args.version)
            metrics['bundle_verify_seconds'] = round(perf_counter() - verify_started, 6)
            print(f'ZIP SHA-256: {archive_sha256}')
        else:
            if args.runtime_kind is not None:
                assembly_started = perf_counter()
                names = _runtime_files(args.config_directory, args.runtime_kind)
                if args.bundle is not None:
                    for name in names:
                        destination = args.bundle / 'runtime' / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(args.config_directory / name, destination)
                    if any(
                        value is not None
                        for value in (args.update_host, args.update_project_id, args.update_channel, args.update_read_token_file)
                    ):
                        apply_update_source(
                            args.bundle,
                            host=args.update_host,
                            project_id=args.update_project_id,
                            channel=args.update_channel,
                            read_token_file=args.update_read_token_file,
                        )
                    update_build_manifest(
                        args.bundle, args.config_directory, names, deliverable=args.archive is not None
                    )
                metrics['assembly_seconds'] = round(perf_counter() - assembly_started, 6)
            if args.bundle is None:
                if args.runtime_kind is None:
                    validate_config(args.config_directory)
            else:
                verify_started = perf_counter()
                important = verify_bundle(args.bundle, args.config_directory, args.runtime_kind)
                metrics['bundle_verify_seconds'] = round(perf_counter() - verify_started, 6)
                if args.archive is not None:
                    archive_sha256 = build_archive(args.bundle, args.archive, output=args.output,
                                                   configured=args.config_directory is not None,
                                                   important_members=important or (), metrics=metrics)
                    print(f'ZIP SHA-256: {archive_sha256}')
        metrics['result'] = 'PASS'
    except (OSError, ValueError, TraderError, UpdateError) as error:
        print(error, file=sys.stderr)
        return 2
    finally:
        metrics['total_seconds'] = round(perf_counter() - total_started, 6)
        if args.performance_report is not None:
            args.performance_report.parent.mkdir(parents=True, exist_ok=True)
            args.performance_report.write_text(
                json.dumps(metrics, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
            )
    return 0


if __name__ == '__main__':
    sys.exit(main())
