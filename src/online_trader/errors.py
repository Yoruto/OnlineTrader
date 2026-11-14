"""Typed, safe errors crossing internal and IPC boundaries."""

from hashlib import sha256
import hmac
from pathlib import Path
import secrets
import traceback
from uuid import uuid4


_diagnostic_key = secrets.token_bytes(32)


def exception_record(error):
    """Retain the call stack without broker text, local variables or source lines."""
    stack = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        stack.append({
            'type': type(current).__name__,
            'code': current.code if isinstance(current, TraderError) else None,
            'frames': [
                {'file': Path(frame.f_code.co_filename).name, 'line': line, 'function': frame.f_code.co_name}
                for frame, line in traceback.walk_tb(current.__traceback__)
            ],
        })
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
