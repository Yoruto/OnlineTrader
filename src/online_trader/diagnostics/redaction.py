"""Installation-local deterministic pseudonyms for diagnostic evidence."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import secrets

from online_trader.configuration.files import publish_new_bytes
from online_trader.errors import TraderError


_KEY_BYTES = 32


def _read_key(path: Path) -> bytes:
    raw = path.read_bytes()
    if len(raw) != _KEY_BYTES:
        raise ValueError("invalid diagnostic HMAC key")
    return raw


def installation_key(runtime_root) -> bytes:
    """Return one installation key without turning key failure into a Runtime failure."""
    root = Path(runtime_root).resolve()
    path = root / "private" / "diagnostics-hmac.key"
    try:
        return _read_key(path)
    except (OSError, ValueError):
        pass
    generated = secrets.token_bytes(_KEY_BYTES)
    try:
        publish_new_bytes(generated, path)
        return generated
    except (OSError, TraderError):
        try:
            return _read_key(path)
        except (OSError, ValueError):
            raise OSError("diagnostic HMAC key is unavailable") from None


def account_pseudonym(runtime_root, account_key: str) -> str:
    if not isinstance(account_key, str) or not account_key:
        raise ValueError("account key is required for a diagnostic pseudonym")
    digest = hmac.new(installation_key(runtime_root), account_key.encode("utf-8"), hashlib.sha256)
    return "acct-" + digest.hexdigest()[:32]
