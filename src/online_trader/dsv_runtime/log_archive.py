"""Rebuild one historical operation log into an independent directory."""

from hashlib import sha256
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.dsv_runtime.migration import _readonly, _source_schema
from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock
from online_trader.runtime.strategy_log import DsvEntryLog


def _checked_record(path):
    if not path.is_file() or path.resolve(strict=True) != path:
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Expected an original file without indirection')
    with path.open('rb') as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Original identity or receipt exceeds its supported size')
    return strict_loads(raw)


def _sequence(value):
    if type(value) is not str or not re.fullmatch('0|[1-9][0-9]{0,18}', value):
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Expected a canonical legacy event sequence')
    return int(value)


def archive_log(source, root, boot_id, profile_record, target):
    if not re.fullmatch('boot-[a-zA-Z0-9-]{1,80}', boot_id):
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Expected an explicit legacy boot identity')
    source, root = source.resolve(strict=True), root.resolve(strict=True)
    target = Path(os.path.abspath(target))
    if target.exists() or target.is_symlink() or target.resolve().is_relative_to(root):
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Use a new output directory outside the legacy run tree')
    profile = DsvProfile.from_record(profile_record)
    with _readonly(source) as db:
        _source_schema(db)
        scope = db.execute('SELECT account_key FROM ledger_scope WHERE singleton=1').fetchone()
        if scope is None:
            raise TraderError('E_DSV_LOG_ARCHIVE', 'Missing legacy account binding')
    with AccountLock(scope[0]), _readonly(source) as db:
        row = db.execute('SELECT account_key FROM kernel_boots WHERE kernel_boot_id=?', (boot_id,)).fetchone()
        if row is None or row[0] != scope[0]:
            raise TraderError('E_DSV_LOG_ARCHIVE', 'Boot identity is absent from the legacy account journal')
        return _publish_archive(source, root, target, db, scope[0], boot_id, profile)


def _publish_archive(source, root, target, db, account, boot_id, profile):
    starts = db.execute("SELECT payload_json FROM event_journal WHERE kernel_boot_id=? "
                        "AND event_type='DSV_OPERATION_LOG_STARTED'", (boot_id,)).fetchall()
    if len(starts) > 1:
        raise TraderError('E_DSV_LOG_ARCHIVE', 'More than one registration exists for this legacy boot')
    registration = strict_loads(starts[0][0])['data'] if starts else None
    binding = {'account_key': account, 'kernel_boot_id': boot_id, 'profile_hash': profile.content_hash}
    if registration:
        run = Path(registration['run_directory'])
    else:
        candidates = []
        for pattern in ('*/run-identity.json', '*/run/run-identity.json'):
            for path in root.glob(pattern):
                if path.resolve() != path:
                    continue
                value = _checked_record(path)
                if (value.get('account_key'), value.get('kernel_boot_id')) == (account, boot_id):
                    candidates.append(path.parent)
        if len(candidates) != 1:
            raise TraderError('E_DSV_LOG_ARCHIVE', 'Legacy run identity is ambiguous or missing')
        run, = candidates
    if not run.is_dir() or run.resolve(strict=True) != run or run == root or not run.is_relative_to(root):
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Legacy run is outside the selected original directory')
    identity = _checked_record(run / 'run-identity.json')
    expected = {'schema_version': 'DSV_RUN_IDENTITY/1', 'account_key': account,
                'kernel_boot_id': boot_id, 'run_directory': str(run)}
    if any(identity.get(key) != value for key, value in expected.items()):
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Run identity does not agree with the account, boot and original layout')
    expected = binding | {'run_directory': str(run)}
    if registration and any(registration.get(key) != value for key, value in expected.items()):
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Journal registration has a different run or profile')
    registry = source.with_name(source.name + '.operation-log-recovery') / (boot_id + '.json')
    fallback = run.with_name(run.name + '.operation-log-recovery.json')
    receipts = [_checked_record(path) for path in (registry, fallback) if path.exists() or path.is_symlink()]
    if not registration and not receipts:
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Missing legacy run registration and recovery receipt')
    first = _sequence((registration or receipts[0])['from_event_seq'])
    through = db.execute('SELECT MAX(event_seq) FROM event_journal WHERE kernel_boot_id=?', (boot_id,)).fetchone()[0]
    if not db.execute('SELECT 1 FROM event_journal WHERE kernel_boot_id=? AND event_seq=?', (boot_id, first)).fetchone():
        raise TraderError('E_DSV_LOG_ARCHIVE', 'Original event watermark cannot be proved')
    watermarks = []
    for receipt in receipts:
        status = receipt.get('status')
        if (receipt.get('schema_version') != 'DSV_OPERATION_LOG_RECOVERY/1' or
                any(receipt.get(key) != value for key, value in expected.items()) or
                _sequence(receipt['from_event_seq']) != first or
                status not in {'TRACKING', 'DEGRADED', 'REBUILDING', 'RECOVERED'} or
                'finalized' in receipt and type(receipt['finalized']) is not bool):
            raise TraderError('E_DSV_LOG_ARCHIVE', 'Recovery receipt has a different or invalid run binding')
        upper = _sequence(receipt.get('recovered_through_event_seq', receipt.get('through_event_seq', str(first))))
        if not first <= upper <= through:
            raise TraderError('E_DSV_LOG_ARCHIVE', 'Recovery receipt claims an inconsistent event watermark')
        watermarks.append({'status': status, 'finalized': receipt.get('finalized') is True,
                           'through_event_seq': str(upper)})

    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=target.parent, prefix='.dsv-log-archive-') as location:
        stage = Path(location) / 'archive'
        stage.mkdir()
        view = SimpleNamespace(account_key=account, ledger=SimpleNamespace(path=source))
        log = DsvEntryLog(view, stage, profile, boot_id=boot_id, from_seq=first)
        log._target = stage / 'operation-log.jsonl'
        log._target.touch(exist_ok=False)
        while rows := log._events(through, query=lambda sql, args: db.execute(sql, args).fetchall()):
            for row in rows:
                log._consume(row)
                log.last_event_seq = row['event_seq']
        digest = sha256()
        with log._target.open('r+b') as stream:
            for block in iter(lambda: stream.read(64 * 1024), b''):
                digest.update(block)
            os.fsync(stream.fileno())
        record = {'type': 'DSV_OFFLINE_LOG_ARCHIVE/1', 'boot_id': boot_id, 'account_hash': content_hash(account),
                  'profile_hash': profile.content_hash, 'from_event_seq': str(first), 'through_event_seq': str(through),
                  'source_receipts': watermarks, 'operation_log_sha256': digest.hexdigest(),
                  'auxiliary_rows_unavailable': True}
        with (stage / 'archive-receipt.json').open('xb') as stream:
            stream.write(canonical_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())
        # Windows rename refuses an existing destination, including a concurrent publication.
        stage.rename(target)
    return record
