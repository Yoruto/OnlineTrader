"""Persist the NTP anchor across owner processes in the same operating-system boot."""

from contextlib import closing
import ctypes
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import NamedTemporaryFile
import time
from uuid import UUID, uuid4


MAX_HOLDOVER_SECONDS = 900
MAX_HOLDOVER_ERROR_SECONDS = 1.0
HOLDOVER_DRIFT_PER_SECOND = .0001


def within_holdover_budget(age, error):
    return (0 <= age < MAX_HOLDOVER_SECONDS
            and 0 <= error
            and error + age * HOLDOVER_DRIFT_PER_SECOND <= MAX_HOLDOVER_ERROR_SECONDS)


def boot_identity(ledger, *, create=False):
    """Use a volatile registry marker to guard cross-process monotonic elapsed time."""
    if sys.platform.startswith('linux'):
        return str(UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
    if sys.platform != 'win32':
        return None
    import winreg
    from ctypes import wintypes

    digest = sha256(os.path.normcase(str(ledger)).encode('utf-8')).hexdigest()
    name = 'OnlineTrader.UTC.' + digest
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software', 0,
                        winreg.KEY_CREATE_SUB_KEY if create else winreg.KEY_READ) as parent:
        if not create:
            with winreg.OpenKey(parent, name) as key:
                return winreg.QueryValueEx(key, 'Token')[0]
        api = ctypes.WinDLL('advapi32')
        create_key = api.RegCreateKeyExW
        create_key.argtypes = (wintypes.HKEY, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPWSTR,
                               wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               ctypes.POINTER(wintypes.HKEY), ctypes.POINTER(wintypes.DWORD))
        create_key.restype = wintypes.LONG
        key = wintypes.HKEY()
        disposition = wintypes.DWORD()
        result = create_key(int(parent), name, 0, None, 1,  # REG_OPTION_VOLATILE
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE, None,
                            ctypes.byref(key), ctypes.byref(disposition))
        if result:
            raise OSError(result, 'Cannot create volatile UTC marker')
        close_key = api.RegCloseKey
        close_key.argtypes = (wintypes.HKEY,)
        try:
            if disposition.value == 1:  # REG_CREATED_NEW_KEY
                token = uuid4().hex
                winreg.SetValueEx(key.value, 'Token', 0, winreg.REG_SZ, token)
                return token
            return winreg.QueryValueEx(key.value, 'Token')[0]
        finally:
            close_key(key)


class ProtectionClockAnchor:
    def __init__(self, ledger, account_key):
        self.ledger = Path(ledger).resolve()
        self.path = self.ledger.with_name(self.ledger.name + '.utc-anchor.json')
        self.account_key = account_key

    def _boot(self, *, create=False):
        try:
            return boot_identity(self.ledger, create=create)
        except (AttributeError, OSError, ValueError):
            return None

    def load(self):
        boot = self._boot()
        if not boot:
            return None
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return None
            data = json.loads(raw)
            if (data['schema'] != 2 or data['account_key'] != self.account_key
                    or data['boot_id'] != boot or data['ledger'] != str(self.ledger)):
                return None
            utc, monotonic = data['utc'], data['monotonic']
            reference, correction = data['reference_monotonic'], data['correction']
            error = data['error_seconds']
            now = time.monotonic()
            if (not all(type(value) in (int, float) and math.isfinite(value)
                        for value in (utc, monotonic, reference, correction, error))
                    or not 0 < utc < 4_000_000_000 or not 0 <= reference <= monotonic <= now
                    or not within_holdover_budget(now - reference, error + abs(correction))):
                return None
            return utc, monotonic, reference, correction, error
        except (KeyError, OSError, ValueError, TypeError, OverflowError):
            return None

    def save(self, utc, monotonic, reference_monotonic, correction, error_seconds):
        boot = self._boot(create=True)
        if not boot:
            return False
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, prefix='.utc-anchor-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps({'schema': 2, 'account_key': self.account_key, 'boot_id': boot,
                                         'ledger': str(self.ledger), 'utc': utc,
                                         'monotonic': monotonic, 'reference_monotonic': reference_monotonic,
                                         'correction': correction, 'error_seconds': error_seconds},
                                        allow_nan=False).encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            return True
        except OSError:
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def legacy_protection_pending(grant):
    """Inspect original responsibilities through the read-only ledger before login."""
    ledger = Path(grant.value['ledger_path']).resolve()
    if not ledger.is_file():
        return False
    try:
        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
            return connection.execute("""
                SELECT 1 FROM position_cycles AS cycle
                JOIN protection_plans AS plan USING (position_cycle_id)
                WHERE cycle.lifecycle != 'CLOSED'
                  AND json_extract(plan.plan_json, '$.approval_record_id') = ?
                LIMIT 1
            """, (grant.value['grant_id'],)).fetchone() is not None
    except sqlite3.Error:
        return False
