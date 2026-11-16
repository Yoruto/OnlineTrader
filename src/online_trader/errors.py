"""Typed, safe errors crossing internal and IPC boundaries."""

from hashlib import sha256
import hmac
from pathlib import Path
import secrets
import sqlite3
import traceback
from uuid import uuid4


_diagnostic_key = secrets.token_bytes(32)


def sqlite_exception_metadata(error):
    """Export SQLite numeric/enumerated context, never arbitrary exception text."""
    if not isinstance(error, sqlite3.Error):
        return {}
    result = {}
    code, name = getattr(error, 'sqlite_errorcode', None), getattr(error, 'sqlite_errorname', None)
    if type(code) is int and 0 <= code <= 2_147_483_647:
        result['sqlite_errorcode'] = code
        if (type(name) is str and name.startswith('SQLITE_') and
                type(getattr(sqlite3, name, None)) is int and getattr(sqlite3, name) == code):
            result['sqlite_errorname'] = name
    context = getattr(error, 'storage_context', None)
    if type(context) is dict:
        safe = {}
        for key, allowed in (('transaction_stage', {'BEGIN', 'BODY', 'COMMIT'}),
                             ('journal_mode', {'delete', 'truncate', 'persist', 'wal', 'memory', 'unknown'})):
            value = context.get(key)
            if type(value) is str and value in allowed:
                safe[key] = value
        value = context.get('busy_timeout_ms')
        if type(value) is int and 0 <= value <= 60_000:
            safe['busy_timeout_ms'] = value
        for key in ('in_transaction', 'rollback_failed'):
            if type(context.get(key)) is bool:
                safe[key] = context[key]
        if safe:
            result['storage_context'] = safe
    return result


def exception_record(error):
    """Retain the call stack without broker text, local variables or source lines."""
    stack = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        item = {
            'type': type(current).__name__,
            'code': current.code if isinstance(current, TraderError) else None,
            'frames': [
                {'file': Path(frame.f_code.co_filename).name, 'line': line, 'function': frame.f_code.co_name}
                for frame, line in traceback.walk_tb(current.__traceback__)
            ],
        }
        item.update(sqlite_exception_metadata(current))
        stack.append(item)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    # The keyed fingerprint distinguishes same-site failures without exporting
    # the exception message or allowing offline guesses of embedded credentials.
    message = repr((stack, str(error))).encode('utf-8', errors='replace')
    return {'exception_id': 'exception-' + uuid4().hex,
            'fingerprint': hmac.new(_diagnostic_key, message, sha256).hexdigest(),
            'stack': stack}


class TraderError(ValueError):
    def __init__(self, code: str, message: str = "Request refused") -> None:
        self.code = code
        super().__init__(message)
