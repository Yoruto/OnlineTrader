"""Installation-local deterministic pseudonyms for diagnostic evidence."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import re
import secrets

from online_trader.configuration.files import publish_new_bytes
from online_trader.errors import TraderError


_KEY_BYTES = 32
_SENSITIVE_KEY_TOKENS = (
    "password", "passwd", "secret", "token", "credential", "auth",
    "cookie", "private", "api_key", "apikey", "broker_account", "account_id",
    "account_key", "user_id", "username", "login", "masked_login",
    "investor", "phone", "email", "deploy-token", "private-token", "job-token",
)
_TEXT_SCRUB_PATTERNS = (
    (re.compile(r'(?i)("?(?:password|passwd|secret|token|private[-_]token|deploy[-_]token|job[-_]token|api[-_]?key|username|login|kq_username|simnow_username|kq_password|simnow_password)"?\s*[:=]\s*)"[^"\r\n]*"'), r'\1"[REDACTED]"'),
    (re.compile(r"(?i)('?(?:password|passwd|secret|token|private[-_]token|deploy[-_]token|job[-_]token|api[-_]?key|username|login|kq_username|simnow_username|kq_password|simnow_password)'?\s*[:=]\s*)'[^'\r\n]*'"), r"\1'[REDACTED]'"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|private[-_]token|deploy[-_]token|job[-_]token|api[-_]?key|username|login|kq_username|simnow_username|kq_password|simnow_password)\s*[=:]\s*[^\s,;\"']+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)((?:authorization|private-token|deploy-token|job-token)\s*:\s*)(?:bearer\s+)?\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[CREDENTIALS_REDACTED]@"),
    (re.compile(r"\b(?:SIM|LIVE):[A-Z0-9_]+:[0-9A-Za-z._-]+\b"), "[ACCOUNT_REDACTED]"),
    (re.compile(r"[A-Za-z]:\\[^\s\"']+"), "[PATH_REDACTED]"),
    (re.compile(r"\\\\[A-Za-z0-9._$-]+\\[^\s\"']+"), "[PATH_REDACTED]"),
)
_MAX_TEXT_LEN = 256


def redact_text(value: str, *, max_len: int = _MAX_TEXT_LEN) -> str:
    """Scrub secret tokens, raw account keys, and host paths from diagnostic text."""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    for pattern, replacement in _TEXT_SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def sanitize_diagnostic_mapping(mapping: dict | None, *, allowed_keys: frozenset[str] | None = None) -> dict:
    """Apply field-level allowlist and sensitive-key redaction to diagnostic mappings."""
    if not mapping:
        return {}
    cleaned = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            continue
        lowered = key.lower()
        if any(token in lowered for token in _SENSITIVE_KEY_TOKENS):
            continue
        if allowed_keys is not None and key not in allowed_keys:
            continue
        if isinstance(value, str):
            cleaned[key] = redact_text(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            cleaned[key] = value
        elif isinstance(value, (list, tuple)):
            cleaned[key] = [
                redact_text(item) if isinstance(item, str) else item
                for item in value[:16]
                if isinstance(item, (str, int, float, bool)) or item is None
            ]
    return cleaned


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
