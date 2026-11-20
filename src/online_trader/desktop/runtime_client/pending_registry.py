"""Process-local metadata for unresolved Runtime Workbench commands."""

from dataclasses import replace
from threading import RLock

from online_trader.desktop.runtime_client.state import PendingCommandStatus
from online_trader.errors import TraderError


class PendingCommandRegistry:
    """Keep command correlation only for the lifetime of this Workbench.

    The broker account and the active Runtime are authoritative after a
    restart. A Workbench command ID therefore must not become a durable local
    recovery responsibility or a cross-device handoff record.
    """

    LIMIT = 64

    def __init__(self, settings_factory=None):
        self._records = {}
        self._lock = RLock()

    @staticmethod
    def _identity_matches(left, right):
        return (left.account_key, left.operation, left.scope_hash) == (
            right.account_key, right.operation, right.scope_hash)

    def restore(self):
        return {}

    def register(self, pending):
        with self._lock:
            existing = self._records.get(pending.command_id)
            if existing is not None and not self._identity_matches(existing, pending):
                raise TraderError('E_STORAGE', 'Runtime command ID conflicts with this Workbench session')
            duplicate = next((item for item in self._records.values()
                              if item.account_key == pending.account_key
                              and item.operation == pending.operation
                              and item.scope_hash in (None, pending.scope_hash)), None)
            if duplicate is not None and duplicate.command_id != pending.command_id:
                raise TraderError('E_PENDING_COMMAND', f'原命令 {duplicate.command_id} 仍在本次会话处理中')
            if existing is None and len(self._records) >= self.LIMIT:
                raise TraderError('E_PENDING_CAPACITY', '本次工作台会话的未决命令已满')
            self._records[pending.command_id] = pending
            return self._records.copy()

    def upsert(self, pending):
        with self._lock:
            existing = self._records.get(pending.command_id)
            if existing is not None and not self._identity_matches(existing, pending):
                raise TraderError('E_STORAGE', 'Runtime pending command identity changed')
            if existing is None and len(self._records) >= self.LIMIT:
                raise TraderError('E_PENDING_CAPACITY', '本次工作台会话的未决命令已满')
            self._records[pending.command_id] = pending
            return self._records.copy()

    def remove(self, command_id, account_key, operation):
        with self._lock:
            existing = self._records.get(command_id)
            if existing is not None and (existing.account_key != account_key
                                         or existing.operation != operation):
                raise TraderError('E_STORAGE', 'A different Runtime command owns this pending ID')
            self._records.pop(command_id, None)
            return self._records.copy()

    def mark_account_unknown(self, account_key, reason):
        with self._lock:
            changed = {}
            for command_id, pending in tuple(self._records.items()):
                if pending.account_key == account_key:
                    value = replace(pending, status=PendingCommandStatus.OUTCOME_UNKNOWN, reason=reason)
                    self._records[command_id] = value
                    changed[command_id] = value
            return self._records.copy(), changed

    def migrate_legacy_slot(self):
        """Ignore old durable pending metadata; broker sync replaces it."""
        return self._records.copy()
