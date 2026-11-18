"""Validate delivery inputs and verify the assembled Windows portable archive."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from online_trader.accounts import validate_simulation_defaults
from online_trader.errors import TraderError
from online_trader.runtime.comparison import read_dsv_profile


DEFAULTS = 'OnlineTrader-defaults.json'
PARAMETERS = 'OnlineTrader-parameters.json'
EXECUTABLES = ('OnlineTrader.exe', 'OnlineTraderCLI.exe')


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
    files = [defaults]
    parameters = directory / PARAMETERS
    if parameters.exists():
        try:
            read_dsv_profile(parameters)
        except (OSError, ValueError, TraderError):
            raise ValueError(f'{PARAMETERS} must contain valid DSV business settings.') from None
        files.append(parameters)
    return files


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_bundle(bundle, directory):
    if not all((bundle / name).is_file() for name in EXECUTABLES) or not (bundle / '_internal').is_dir():
        raise ValueError('Portable bundle is missing required executables or the shared runtime.')
    defaults = [path for path in bundle.rglob('*') if path.name.casefold() == DEFAULTS.casefold()]
    if directory is None:
        if defaults:
            raise ValueError('Generic portable bundles must not include account defaults.')
        return
    if defaults != [bundle / DEFAULTS]:
        raise ValueError('Configured portable bundles require one account defaults file at the bundle root.')
    for source in validate_config(directory):
        if digest(source) != digest(bundle / source.name):
            raise ValueError(f'Bundled {source.name} differs from the delivery input.')
    validate_config(bundle)


def build_archive(bundle, destination, *, configured):
    if destination.exists():
        raise ValueError('Choose a new archive file.')
    named_configured = destination.name.casefold().endswith('-configured.zip')
    if destination.suffix.casefold() != '.zip' or named_configured != configured:
        raise ValueError('Use a .zip filename with the -Configured suffix only for Configured deliveries.')
    prefix = destination.stem + '/' if configured else ''
    source_files = {prefix + file.relative_to(bundle).as_posix(): file
                    for file in sorted(bundle.rglob('*')) if file.is_file()}
    important = {prefix + name for name in (*EXECUTABLES, DEFAULTS, PARAMETERS)}
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
        staging.rename(destination)
    finally:
        staging.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-directory', type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    try:
        if args.bundle is None:
            validate_config(args.config_directory)
        else:
            verify_bundle(args.bundle, args.config_directory)
            if args.archive is not None:
                build_archive(args.bundle, args.archive, configured=args.config_directory is not None)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
