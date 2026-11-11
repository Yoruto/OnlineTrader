"""OT-CJ-1 canonical JSON and bounded OTIP 1.0 byte-stream frames."""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

from online_trader.errors import TraderError

HEADER = struct.Struct("<4sHHI")
MAGIC = b"OTIP"
MAX_FRAME_BYTES = 1_048_576
MAX_DEPTH = 32
MAX_JSON_INTEGER = 2_147_483_647
_HASHED_TYPES = frozenset({"OperatorCommand", "DecisionProposal", "StrategyStateProposal", "SwitchStrategy"})


def _check(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise TraderError("E_SCHEMA", "JSON nesting exceeds the protocol limit")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not 0 <= value <= MAX_JSON_INTEGER:
            raise TraderError("E_SCHEMA", "JSON integer is outside the protocol range")
        return
    if type(value) is str:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise TraderError("E_SCHEMA", "JSON contains a non-scalar Unicode value")
        return
    if type(value) is list:
        for item in value:
            _check(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TraderError("E_SCHEMA", "JSON object keys must be strings")
            _check(key, depth + 1)
            _check(item, depth + 1)
        return
    raise TraderError("E_SCHEMA", "JSON supports only protocol data types")


def _quoted(value: str) -> str:
    escaped = []
    for char in value:
        point = ord(char)
        if char == '"':
            escaped.append('\\"')
        elif char == "\\":
            escaped.append("\\\\")
        elif point < 32:
            escaped.append(f"\\u{point:04x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _canonical(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return _quoted(value)
    if type(value) is list:
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    return "{" + ",".join(_quoted(key) + ":" + _canonical(value[key]) for key in sorted(value)) + "}"


def canonical_bytes(value: Any) -> bytes:
    """Encode OT-CJ-1, including literal UTF-8 and uniform C0 escapes."""
    _check(value)
    return _canonical(value).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def payload_hash(envelope: dict[str, Any]) -> str:
    """Hash business identity/content, excluding retry-specific transport fields."""
    if envelope.get("type") not in _HASHED_TYPES or type(envelope.get("body")) is not dict:
        raise TraderError("E_SCHEMA", "Message has no business hash contract")
    return content_hash({
        "account_key": envelope.get("account_key"),
        "message_type": envelope["type"],
        "payload": {key: value for key, value in envelope["body"].items()
                    if key not in {"context_id", "payload_hash"}},
    })


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise TraderError("E_SCHEMA", "Duplicate JSON object key")
        result[key] = value
    return result


def _integer(token: str) -> int:
    if token.startswith("-") or len(token) > 10:
        raise TraderError("E_SCHEMA", "JSON integer is outside the protocol range")
    value = int(token)
    if value > MAX_JSON_INTEGER:
        raise TraderError("E_SCHEMA", "JSON integer is outside the protocol range")
    return value


def _not_number(_token: str) -> None:
    raise TraderError("E_SCHEMA", "Floating point, exponent and non-finite JSON numbers are forbidden")


def strict_loads(raw: bytes | str) -> Any:
    """Parse protocol JSON without float coercion, duplicate keys or scalar repair."""
    if type(raw) not in {str, bytes}:
        raise TraderError("E_SCHEMA", "Expected UTF-8 JSON bytes")
    try:
        text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
        if text.startswith("\ufeff"):
            raise TraderError("E_SCHEMA", "JSON BOM is forbidden")
        value = json.loads(text, object_pairs_hook=_pairs, parse_int=_integer,
                           parse_float=_not_number, parse_constant=_not_number)
        _check(value)
        return value
    except TraderError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError) as exc:
        raise TraderError("E_SCHEMA", "Malformed protocol JSON") from exc


def encode_frame(message: dict[str, Any]) -> bytes:
    if type(message) is not dict:
        raise TraderError("E_FRAME", "A frame must contain a JSON object")
    body = canonical_bytes(message)
    if not 1 <= len(body) <= MAX_FRAME_BYTES:
        raise TraderError("E_FRAME", "Frame length is outside the protocol limit")
    return HEADER.pack(MAGIC, 1, 0, len(body)) + body


class FrameDecoder:
    """Incremental decoder; allocates a body only after its bounded header passes."""

    def __init__(self) -> None:
        self._header = bytearray()
        self._body = bytearray()
        self._length: int | None = None
        self._failed = False

    @property
    def pending_bytes(self) -> int:
        return len(self._header) + len(self._body)

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        if self._failed:
            raise TraderError("E_FRAME", "Decoder is invalid after a framing failure")
        if type(data) is not bytes:
            raise TraderError("E_FRAME", "Frame input must be bytes")
        result: list[dict[str, Any]] = []
        offset = 0
        try:
            while offset < len(data):
                if self._length is None:
                    take = min(HEADER.size - len(self._header), len(data) - offset)
                    self._header.extend(data[offset:offset + take]); offset += take
                    if len(self._header) < HEADER.size:
                        continue
                    magic, major, minor, length = HEADER.unpack(self._header)
                    if magic != MAGIC:
                        raise TraderError("E_FRAME", "Invalid frame signature")
                    if (major, minor) != (1, 0):
                        raise TraderError("E_VERSION", "Unsupported frame version")
                    if not 1 <= length <= MAX_FRAME_BYTES:
                        raise TraderError("E_FRAME", "Frame length is outside the protocol limit")
                    self._length = length
                take = min(self._length - len(self._body), len(data) - offset)
                self._body.extend(data[offset:offset + take]); offset += take
                if len(self._body) == self._length:
                    message = strict_loads(bytes(self._body))
                    if type(message) is not dict:
                        raise TraderError("E_FRAME", "Frame payload must be an object")
                    result.append(message)
                    self._header.clear(); self._body.clear(); self._length = None
            return result
        except TraderError:
            self._failed = True
            raise
