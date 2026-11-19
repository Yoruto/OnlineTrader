"""In-memory, boot-bound contexts using only server monotonic deadlines."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import threading
import time
from typing import Callable, Mapping

from online_trader.errors import TraderError

_PURPOSES = frozenset({"OPERATOR_COMMAND", "ENTRY_PROPOSAL", "STRATEGY_SWITCH", "NOTIFICATION_RESULT", "STRATEGY_STATE_UPDATE"})
_REVISIONS = frozenset({"state_revision", "risk_revision", "config_revision", "authority_revision"})


@dataclass(frozen=True)
class _Context:
    peer_id: str
    purpose: str
    operation: str
    scope_id: str
    kernel_epoch: str
    strategy_epoch: str | None
    revisions: tuple[tuple[str, str], ...]
    deadline: float
    body: dict


class ContextRegistry:
    def __init__(self, kernel_boot_id: str, account_key: str, *, clock: Callable[[], float] = time.monotonic,
                 capacity: int = 4096) -> None:
        if not kernel_boot_id or not account_key or type(capacity) is not int or capacity < 1:
            raise TraderError("E_SCHEMA", "Invalid context registry binding")
        self.kernel_boot_id = kernel_boot_id
        self.account_key = account_key
        self._clock = clock
        self._capacity = capacity
        self._contexts: dict[str, _Context] = {}
        self._revoked: dict[str, tuple[str, int]] = {}
        self._lock = threading.RLock()

    def issue(self, peer_id: str, purpose: str, operation: str, scope_id: str, *, kernel_epoch: str = "1",
              strategy_epoch: str | None = None, revisions: Mapping[str, str] | None = None,
              issued_event_seq: str = "0", ttl_ms: int = 30000) -> dict:
        revisions = dict(revisions) if revisions is not None else {key: "1" for key in _REVISIONS}
        if (not peer_id or purpose not in _PURPOSES or not operation or not scope_id or
                type(ttl_ms) is not int or not 1 <= ttl_ms <= 30000 or set(revisions) != _REVISIONS):
            raise TraderError("E_SCHEMA", "Invalid context request")
        for value in [kernel_epoch, *revisions.values(), *([] if strategy_epoch is None else [strategy_epoch])]:
            if type(value) is not str or not value.isascii() or not value.isdigit() or value.startswith("0") or int(value) > 9_223_372_036_854_775_807:
                raise TraderError("E_SCHEMA", "Invalid context revision")
        if type(issued_event_seq) is not str or not issued_event_seq.isascii() or not issued_event_seq.isdigit() or (issued_event_seq.startswith("0") and issued_event_seq != "0") or int(issued_event_seq) > 9_223_372_036_854_775_807:
            raise TraderError("E_SCHEMA", "Invalid context event boundary")
        now = self._clock()
        with self._lock:
            self._contexts = {key: ctx for key, ctx in self._contexts.items() if now < ctx.deadline}
            if len(self._contexts) >= self._capacity:
                raise TraderError("E_QUEUE_OVERFLOW", "Context capacity reached")
            context_id = secrets.token_hex(32)
            body = {"context_id": context_id, "purpose": purpose, "scope_id": scope_id,
                    "kernel_epoch": kernel_epoch, "strategy_epoch": strategy_epoch,
                    "related_revisions": revisions.copy(), "issued_event_seq": issued_event_seq,
                    "valid_for_ms": ttl_ms,
                    "expires_at_utc": (datetime.now(timezone.utc) + timedelta(milliseconds=ttl_ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
            self._contexts[context_id] = _Context(peer_id, purpose, operation, scope_id, kernel_epoch,
                                                 strategy_epoch, tuple(sorted(revisions.items())),
                                                 now + ttl_ms / 1000, body)
            return {**body, "related_revisions": revisions.copy()}

    def validate(self, context_id: str, peer_id: str, purpose: str, operation: str, scope_id: str, *,
                 kernel_boot_id: str | None = None, account_key: str | None = None,
                 revisions: Mapping[str, str] | None = None, kernel_epoch: str | None = None,
                 strategy_epoch: str | None = None) -> dict:
        if kernel_boot_id is not None and kernel_boot_id != self.kernel_boot_id:
            raise TraderError("E_BOOT_MISMATCH", "Context belongs to another kernel boot")
        if account_key is not None and account_key != self.account_key:
            raise TraderError("E_SCOPE", "Context belongs to another account")
        with self._lock:
            ctx = self._contexts.get(context_id)
            now = self._clock()
            if ctx is None:
                revoked = self._revoked.get(context_id)
                reason = (f'命令授权已撤销 · {revoked[0]} · 签发后 {revoked[1]} ms'
                          if revoked is not None else '执行器未找到该命令的授权')
                raise TraderError("E_CONTEXT_EXPIRED", reason)
            if now >= ctx.deadline:
                raise TraderError("E_CONTEXT_EXPIRED", f'命令授权超过有效期 {ctx.body["valid_for_ms"]} ms')
            if (peer_id, purpose, operation, scope_id) != (ctx.peer_id, ctx.purpose, ctx.operation, ctx.scope_id):
                raise TraderError("E_AUTH", "Context is not transferable")
            if kernel_epoch is not None and kernel_epoch != ctx.kernel_epoch:
                raise TraderError("E_EPOCH_REVOKED", "Kernel authority changed")
            if strategy_epoch is not None and strategy_epoch != ctx.strategy_epoch:
                raise TraderError("E_EPOCH_REVOKED", "Strategy authority changed")
            if revisions is not None and tuple(sorted(revisions.items())) != ctx.revisions:
                raise TraderError("E_REVISION_CONFLICT", "Related state changed")
            return {**ctx.body, "related_revisions": dict(ctx.revisions)}

    def revoke_peer(self, peer_id: str) -> None:
        with self._lock:
            self._remember_revoked((key for key, ctx in self._contexts.items() if ctx.peer_id == peer_id),
                                   'PEER_DISCONNECTED')
            self._contexts = {key: ctx for key, ctx in self._contexts.items() if ctx.peer_id != peer_id}

    def _remember_revoked(self, keys, reason):
        now = self._clock()
        for key in keys:
            ctx = self._contexts[key]
            if now < ctx.deadline:
                age = ctx.body['valid_for_ms'] - round((ctx.deadline - now) * 1000)
                self._revoked[key] = (reason, max(0, age))
        while len(self._revoked) > self._capacity:
            del self._revoked[next(iter(self._revoked))]

    def revoke_all(self, *, reason='AUTHORITY_OR_STATE_CHANGED') -> None:
        with self._lock:
            if self._contexts:
                self._remember_revoked(self._contexts, reason)
            self._contexts.clear()
