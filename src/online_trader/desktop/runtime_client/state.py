"""Versioned Runtime state, local UI state and incremental Qt table models."""

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, Signal


class ClientConnectionState(StrEnum):
    DISCONNECTED = 'DISCONNECTED'
    CONNECTING = 'CONNECTING'
    HANDSHAKING = 'HANDSHAKING'
    SYNCHRONIZING = 'SYNCHRONIZING'
    READY = 'READY'
    STALE = 'STALE'
    RESYNC_REQUIRED = 'RESYNC_REQUIRED'
    INCOMPATIBLE = 'INCOMPATIBLE'


class PendingCommandStatus(StrEnum):
    LOCAL_DRAFT = 'LOCAL_DRAFT'
    LOCAL_NOT_ENQUEUED = 'LOCAL_NOT_ENQUEUED'
    SUBMITTING = 'SUBMITTING'
    SUBMISSION_UNKNOWN = 'SUBMISSION_UNKNOWN'
    REJECTED = 'REJECTED'
    ACCEPTED = 'ACCEPTED'
    PROCESSING = 'PROCESSING'
    COMPLETED = 'COMPLETED'
    OUTCOME_UNKNOWN = 'OUTCOME_UNKNOWN'


@dataclass(frozen=True)
class PendingCommandState:
    command_id: str
    account_key: str
    operation: str
    status: PendingCommandStatus
    reason: str | None = None
    receipt: MappingProxyType | None = None
    scope_hash: str | None = None


@dataclass
class UiState:
    selected_account_key: str | None = None
    selection_generation: int = 0
    filters: dict = field(default_factory=dict)
    selected_ids: dict = field(default_factory=dict)

    def select_account(self, account_key):
        if account_key != self.selected_account_key:
            self.selected_account_key = account_key
            self.selection_generation += 1
            self.selected_ids.clear()


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


class ClientStateStore(QObject):
    connection_changed = Signal(str, str)
    runtime_changed = Signal(object)
    markets_changed = Signal(object)
    orders_changed = Signal(object)
    trades_changed = Signal(object)
    positions_changed = Signal(object)
    authority_changed = Signal(object)
    configuration_changed = Signal(object)
    diagnostics_changed = Signal(object)
    receipts_changed = Signal(object)
    owner_boot_changed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.connection_state = ClientConnectionState.DISCONNECTED
        self.connection_reason = ''
        self.runtime = None
        self.cursor = None
        self.published_ns = None
        self.ui = UiState()
        self.pending = {}

    def set_connection(self, state, reason=''):
        state = ClientConnectionState(state)
        if state != self.connection_state or reason != self.connection_reason:
            self.connection_state, self.connection_reason = state, reason
            self.connection_changed.emit(state.value, reason)

    def mark_stale(self, reason):
        if self.runtime is not None:
            self.set_connection(ClientConnectionState.STALE, reason)
        else:
            self.set_connection(ClientConnectionState.SYNCHRONIZING, reason)

    def apply(self, state, cursor, published_ns):
        view = state['view']
        account_key = view['account_key']
        boot_id = view['kernel_boot_id']
        previous_boot = None
        if self.runtime is not None:
            previous = self.runtime['view']
            if previous['account_key'] != account_key:
                self.set_connection(ClientConnectionState.RESYNC_REQUIRED, 'E_ACCOUNT_CHANGED')
                return False
            if previous['kernel_boot_id'] != boot_id:
                previous_boot = previous['kernel_boot_id']
        old = self.runtime
        self.runtime = _freeze(state)
        self.cursor = _freeze(cursor)
        self.published_ns = published_ns
        self.ui.select_account(account_key)
        self.set_connection(ClientConnectionState.READY)
        if previous_boot is not None:
            self.owner_boot_changed.emit(previous_boot, boot_id)
        self.runtime_changed.emit(self.runtime)
        changed = lambda key: old is None or old['view'].get(key) != state['view'].get(key)
        for key, signal in (
                ('markets', self.markets_changed), ('orders', self.orders_changed),
                ('trades', self.trades_changed), ('cycles', self.positions_changed),
                ('diagnostics', self.diagnostics_changed), ('commands', self.receipts_changed)):
            if changed(key):
                signal.emit(self.runtime['view'].get(key, ()))
        if old is None or any(changed(key) for key in ('entry_authority', 'entry_policies', 'capabilities')):
            self.authority_changed.emit(self.runtime['view'])
        if changed('configuration'):
            self.configuration_changed.emit(self.runtime['view']['configuration'])
        return True

    @property
    def view(self):
        return None if self.runtime is None else self.runtime['view']

    def capability(self, operation):
        view = self.view
        if self.connection_state != ClientConnectionState.READY or view is None:
            return False, self.connection_reason or 'RUNTIME_NOT_READY'
        for row in view.get('capabilities', {}).get('operations', ()):
            if row.get('operation') == operation:
                return bool(row.get('allowed')), row.get('reason')
        return False, 'CAPABILITY_UNAVAILABLE'


class RuntimeTableModel(QAbstractTableModel):
    """Incremental row model keyed by an immutable Runtime business identity."""

    def __init__(self, columns, identity, formatter=None, parent=None):
        super().__init__(parent)
        self.columns = tuple(columns)
        self.identity = identity
        self.formatter = formatter or (lambda row, key: row.get(key))
        self._rows = []
        self._by_id = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        key, _ = self.columns[index.column()]
        value = self.formatter(self._rows[index.row()], key)
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return '—' if value is None or value == '' else str(value)
        if role == Qt.UserRole:
            return self._rows[index.row()].get(self.identity)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.columns):
            return self.columns[section][1]
        return super().headerData(section, orientation, role)

    def row_identity(self, row):
        return self._rows[row].get(self.identity) if 0 <= row < len(self._rows) else None

    def row_value(self, row):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def replace(self, rows):
        values = [dict(row) for row in rows]
        identities = [row.get(self.identity) for row in values]
        if any(value is None for value in identities) or len(set(identities)) != len(identities):
            self.beginResetModel()
            self._rows = values
            self._by_id = {row.get(self.identity): row for row in values}
            self.endResetModel()
            return
        old_ids = [row[self.identity] for row in self._rows]
        if old_ids != identities:
            self.beginResetModel()
            self._rows = values
            self._by_id = dict(zip(identities, values))
            self.endResetModel()
            return
        changed = []
        for index, row in enumerate(values):
            if self._rows[index] != row:
                changed.append(index)
        self._rows = values
        self._by_id = dict(zip(identities, values))
        for index in changed:
            self.dataChanged.emit(self.index(index, 0), self.index(index, len(self.columns) - 1))
