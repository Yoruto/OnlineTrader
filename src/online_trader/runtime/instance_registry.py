"""Same-user discovery hint for the one host-wide Standalone Runtime.

The Windows mutex remains the ownership authority.  This record only tells the
desktop which attributable Runtime endpoint it should verify before attaching.
"""

import os
from pathlib import Path
from uuid import uuid4

from online_trader.configuration.files import publish_replace_bytes
from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.errors import TraderError


_SCHEMA = 'ONLINE_TRADER_RUNTIME_INSTANCE/1'
_FIELDS = {
    'schema_version', 'instance_id', 'pid', 'pipe', 'account_key',
    'account_alias', 'masked_login', 'runtime_root',
}
_MAX_BYTES = 16_384


def registry_path():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise TraderError('E_STORAGE', 'LOCALAPPDATA is required for Runtime discovery')
    return Path(base).resolve() / 'OnlineTrader' / 'runtime-instance.json'


def read_runtime_instance():
    """Read a bounded discovery hint; callers must verify its Runtime endpoint."""
    try:
        path = registry_path()
        with path.open('rb') as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return None
        value = strict_loads(raw.decode('utf-8-sig'))
        if (type(value) is not dict or value.keys() != _FIELDS
                or value['schema_version'] != _SCHEMA
                or type(value['instance_id']) is not str or not value['instance_id']
                or type(value['pid']) is not int or value['pid'] <= 0
                or any(type(value[key]) is not str or not value[key]
                       for key in ('pipe', 'account_key', 'account_alias', 'masked_login', 'runtime_root'))):
            return None
        return value
    except (OSError, TraderError, ValueError, UnicodeError):
        return None


class RuntimeInstanceRegistration:
    """Publish and conditionally remove the current Runtime discovery hint."""

    def __init__(self, store, selected, pipe):
        self.path = registry_path()
        self.instance_id = uuid4().hex
        self.value = {
            'schema_version': _SCHEMA,
            'instance_id': self.instance_id,
            'pid': os.getpid(),
            'pipe': pipe,
            'account_key': selected['account_key'],
            'account_alias': selected['alias'],
            'masked_login': selected['masked_login'],
            'runtime_root': str(store.directory.resolve()),
        }

    def __enter__(self):
        try:
            publish_replace_bytes(canonical_bytes(self.value), self.path)
        except TraderError as error:
            raise TraderError('E_STORAGE', 'Cannot publish the host Runtime discovery record') from error
        return self

    def __exit__(self, *_):
        current = read_runtime_instance()
        if current is None or current['instance_id'] != self.instance_id:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A stale hint is harmless: the desktop verifies the endpoint and
            # the next owner atomically replaces the record.
            pass
