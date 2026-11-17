"""Content identity of the code running this owner, without account or path data."""

from functools import lru_cache
from hashlib import file_digest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sqlite3
import sys
import time

from online_trader.contracts.codec import content_hash
from online_trader.scheduling.clock import TIME_SEMANTICS


def _digest(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


@lru_cache(maxsize=1)
def _code_identity():
    if getattr(sys, 'frozen', False):
        try:
            return {'commit_sha': 'unknown', 'artifact_sha256': _digest(Path(sys.executable)),
                    'artifact_kind': 'RUNNING_EXECUTABLE'}
        except OSError:
            return {'commit_sha': 'unknown', 'artifact_sha256': 'unknown'}
    root = Path(__file__).resolve().parents[1]
    try:
        manifest = {path.relative_to(root).as_posix(): _digest(path) for path in sorted(root.rglob('*'))
                    if path.is_file() and path.suffix in {'.py', '.json', '.sql'}}
    except OSError:
        return {'commit_sha': 'unknown', 'worktree_manifest_sha256': 'unknown'}
    return {'commit_sha': 'unknown', 'worktree_manifest_sha256': content_hash(manifest)}


def runtime_identity(entry_point, engine_kind, clock_epoch, feed_epoch, instance_id, *, calendar, profile, grant):
    try:
        sdk = version('tqsdk')
    except PackageNotFoundError:
        sdk = None
    return {'schema_version': 'OT_RUNTIME_IDENTITY/1', **_code_identity(),
            'entry_point': entry_point, 'engine_kind': engine_kind, 'time_semantics_version': TIME_SEMANTICS,
            'python_version': '.'.join(map(str, sys.version_info[:3])), 'sqlite_version': sqlite3.sqlite_version,
            'tqsdk_version': sdk, 'clock_implementation': time.get_clock_info('monotonic').implementation,
            'platform': sys.platform, 'calendar_hash': calendar.content_hash, 'profile_hash': profile.content_hash,
            'authorization_hash': grant.content_hash, 'clock_epoch': clock_epoch,
            'feed_epoch': feed_epoch, 'instance_id': instance_id}
