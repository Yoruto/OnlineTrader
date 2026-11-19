"""Bounded Runtime I/O lanes and the single Qt command bus."""

from contextlib import closing
from queue import Empty, Queue
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.desktop.preferences import workbench_preferences
from online_trader.errors import TraderError
from online_trader.runtime.operator_api import RuntimeCommandClient
from online_trader.transport.runtime_account import RuntimeAccountClient
from online_trader.transport.runtime_view import RuntimeViewClient, ViewReplica
from online_trader.desktop.runtime_client.state import PendingCommandState, PendingCommandStatus


class RuntimeSubscriptionWorker(QThread):
    state_received = Signal(object, object, object)
    connection_changed = Signal(str, str)

    def __init__(self, pipe, parent=None):
        super().__init__(parent)
        self.client = RuntimeViewClient(pipe)

    def run(self):
        replica = ViewReplica()
        self.connection_changed.emit('CONNECTING', '')
        while not self.isInterruptionRequested():
            try:
                self.connection_changed.emit('SYNCHRONIZING', '')
                with closing(self.client.subscribe(replica.cursor)) as updates:
                    for message in updates:
                        if self.isInterruptionRequested():
                            return
                        if message['kind'] == 'UNAVAILABLE':
                            self.connection_changed.emit('STALE', 'E_RUNTIME_VIEW_UNAVAILABLE')
                            continue
                        if replica.apply(message):
                            self.state_received.emit(replica.state, replica.cursor, int(message['published_ns']))
                            self.connection_changed.emit('READY', '')
            except TraderError as error:
                state = 'INCOMPATIBLE' if error.code == 'E_SCHEMA' else 'RESYNC_REQUIRED'
                self.connection_changed.emit(state, error.code)
                replica = ViewReplica()
            except (OSError, KeyError, TypeError, ValueError):
                self.connection_changed.emit('STALE', 'E_RUNTIME_VIEW')
                replica = ViewReplica()
            self.msleep(300)


class RuntimeCommandWorker(QThread):
    result = Signal(object)
    failed = Signal(object)

    def __init__(self, pipe, parent=None):
        super().__init__(parent)
        self.client = RuntimeCommandClient(pipe)
        self.queue = Queue(maxsize=32)

    def enqueue(self, request):
        try:
            self.queue.put_nowait(request)
        except Exception:
            raise TraderError('E_BUSY', 'Runtime command queue is full') from None

    def run(self):
        while not self.isInterruptionRequested():
            try:
                request = self.queue.get(timeout=.2)
            except Empty:
                continue
            if request is None:
                return
            try:
                value = self.client.request(request['action'], request['body'], operator=request['operator'],
                                            token=request['token'])
                self.result.emit(request | {'value': value})
            except TraderError as error:
                self.failed.emit(request | {'code': error.code, 'message': str(error)})
            except OSError as error:
                self.failed.emit(request | {'code': 'E_RUNTIME_CONNECTION', 'message': str(error)})


class RuntimeQueryWorker(QThread):
    result = Signal(object)
    failed = Signal(object)

    def __init__(self, pipe, parent=None):
        super().__init__(parent)
        self.view = RuntimeViewClient(pipe)
        self.account = RuntimeAccountClient(pipe)
        self.queue = Queue(maxsize=8)

    def enqueue(self, request):
        try:
            self.queue.put_nowait(request)
        except Exception:
            raise TraderError('E_BUSY', 'Runtime query queue is full') from None

    def run(self):
        while not self.isInterruptionRequested():
            try:
                request = self.queue.get(timeout=.2)
            except Empty:
                continue
            if request is None:
                return
            try:
                if request['kind'] == 'account':
                    value = self.account.read()
                elif request['kind'] == 'history':
                    value = self.view.history_days(**request['parameters'])
                elif request['kind'] == 'runtime-log':
                    value = self.view.runtime_log(**request['parameters'])
                else:
                    raise TraderError('E_SCHEMA', 'Unknown Runtime query')
                self.result.emit(request | {'value': value})
            except (TraderError, OSError) as error:
                self.failed.emit(request | {'code': getattr(error, 'code', 'E_RUNTIME_QUERY'),
                                             'message': str(error)})


class RuntimeClientCoordinator(QObject):
    command_result = Signal(object)
    command_failed = Signal(object)
    query_result = Signal(object)
    query_failed = Signal(object)

    def __init__(self, pipe, store, parent=None):
        super().__init__(parent)
        self.pipe, self.store = pipe, store
        self.subscription = RuntimeSubscriptionWorker(pipe, self)
        self.commands = RuntimeCommandWorker(pipe, self)
        self.queries = RuntimeQueryWorker(pipe, self)
        self.subscription.state_received.connect(store.apply)
        self.subscription.connection_changed.connect(store.set_connection)
        self.commands.result.connect(self.command_result)
        self.commands.failed.connect(self.command_failed)
        self.queries.result.connect(self.query_result)
        self.queries.failed.connect(self.query_failed)

    def start(self):
        self.subscription.start()
        self.commands.start()
        self.queries.start()

    def close(self):
        for worker in (self.subscription, self.commands, self.queries):
            worker.requestInterruption()
            try:
                worker.queue.put_nowait(None)
            except (AttributeError, Exception):
                pass
        for worker in (self.subscription, self.commands, self.queries):
            worker.wait(4000)


class CommandBus(QObject):
    authentication_changed = Signal(bool, str)
    command_changed = Signal(object)
    feedback = Signal(str)
    config_previewed = Signal(object)

    _TERMINAL = {'SUCCEEDED', 'FAILED', 'REJECTED', 'SUPERSEDED'}
    _CONNECTION_ERRORS = {'E_RUNTIME_CONNECTION', 'E_TIMEOUT', 'E_INTERNAL', 'E_PIPE_CLOSED'}

    def __init__(self, coordinator, store, parent=None):
        super().__init__(parent)
        self.coordinator, self.store = coordinator, store
        self.operator_id = None
        self.token = None
        self.operations = frozenset()
        self.expiry = None
        self._requests = {}
        coordinator.command_result.connect(self._result)
        coordinator.command_failed.connect(self._failed)
        self._restore_pending()

    def authenticate(self, operator, token):
        if not operator or not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise TraderError('E_AUTH', '操作员 ID 或令牌格式无效')
        request_id = 'client-' + uuid4().hex
        self._requests[request_id] = {'kind': 'authenticate', 'operator': operator, 'token': token}
        self.coordinator.commands.enqueue({'request_id': request_id, 'action': 'authenticate', 'body': {},
                                           'operator': operator, 'token': token})
        self.feedback.emit('正在核对 Runtime 操作员授权。')

    def lock(self):
        self.operator_id = self.token = self.expiry = None
        self.operations = frozenset()
        self.authentication_changed.emit(False, '')

    def submit(self, operation, scope, parameters):
        view = self.store.view
        if view is None or self.store.connection_state.value != 'READY':
            raise TraderError('E_NOT_READY', '请先取得当前完整 Runtime 状态')
        if self.token is None or self.operator_id is None:
            raise TraderError('E_AUTH', '请先解锁 Runtime 操作员')
        if operation not in self.operations:
            raise TraderError('E_AUTH', f'当前操作员没有 {operation} 权限')
        allowed, reason = self.store.capability(operation)
        if not allowed:
            raise TraderError('E_CAPABILITY', reason or '当前 Runtime 不允许此操作')
        command_id = 'qt-' + uuid4().hex
        body = {'command_id': command_id, 'operation': operation, 'scope': scope,
                'parameters': parameters, 'binding': {
                    'account_key': view['account_key'], 'kernel_boot_id': view['kernel_boot_id'],
                    'config_revision': view['configuration']['config_revision']}}
        request_id = 'client-' + uuid4().hex
        pending = PendingCommandState(command_id, view['account_key'], operation,
                                      PendingCommandStatus.SUBMITTING)
        self.store.pending[command_id] = pending
        self._requests[request_id] = {'kind': 'submit', 'command_id': command_id,
                                      'account_key': view['account_key'], 'operation': operation}
        self._persist_pending()
        self.command_changed.emit(pending)
        self.coordinator.commands.enqueue({'request_id': request_id, 'action': 'submit', 'body': body,
                                           'operator': self.operator_id, 'token': self.token})
        return command_id

    def preview_config(self, profile):
        view = self.store.view
        if view is None or self.store.connection_state.value != 'READY':
            raise TraderError('E_NOT_READY', '请先取得当前完整 Runtime 状态')
        if self.token is None or self.operator_id is None:
            raise TraderError('E_AUTH', '请先解锁 Runtime 操作员')
        if 'SAVE_DSV_PARAMETERS' not in self.operations:
            raise TraderError('E_AUTH', '当前操作员没有参数修改权限')
        request_id = 'client-' + uuid4().hex
        body = {'profile': profile.to_record(), 'binding': {
            'account_key': view['account_key'], 'kernel_boot_id': view['kernel_boot_id'],
            'config_revision': view['configuration']['config_revision']}}
        self._requests[request_id] = {'kind': 'preview-config', 'profile_hash': profile.content_hash,
                                      'account_key': view['account_key']}
        self.coordinator.commands.enqueue({'request_id': request_id, 'action': 'preview-config', 'body': body,
                                           'operator': self.operator_id, 'token': self.token})
        self.feedback.emit('正在由 Runtime 预览并校验参数草稿。')

    def query(self, command_id):
        if self.token is None or self.operator_id is None:
            raise TraderError('E_AUTH', '请先解锁 Runtime 操作员')
        request_id = 'client-' + uuid4().hex
        self._requests[request_id] = {'kind': 'receipt', 'command_id': command_id}
        self.coordinator.commands.enqueue({'request_id': request_id, 'action': 'receipt',
                                           'body': {'command_id': command_id},
                                           'operator': self.operator_id, 'token': self.token})
        self.feedback.emit(f'正在查询原命令 {command_id}。')

    def _result(self, response):
        request = self._requests.pop(response['request_id'], {})
        value = response['value']
        if request.get('kind') == 'authenticate':
            view = self.store.view
            if view is None or value.get('account_key') != view['account_key']:
                self.lock()
                self.feedback.emit('操作员授权账号与当前 Runtime 不一致。')
                return
            self.operator_id = value['operator']
            self.token = request['token']
            self.operations = frozenset(value['operations'])
            self.expiry = value['expires_at_utc']
            workbench_preferences().setValue('runtime/operator_id', self.operator_id)
            self.authentication_changed.emit(True, self.operator_id)
            self.feedback.emit(f'操作员已解锁 · {self.operator_id}')
            return
        if request.get('kind') == 'preview-config':
            if value.get('profile_hash') != request.get('profile_hash'):
                self.feedback.emit('Runtime 参数预览哈希与本地草稿不一致。')
                return
            self.config_previewed.emit(value)
            self.feedback.emit(f"Runtime 参数预览通过 · 配置版本 {value['config_revision']}")
            return
        command_id = value['command_id']
        status = value.get('status')
        mapped = (PendingCommandStatus.COMPLETED if status in {'SUCCEEDED', 'SUPERSEDED'} else
                  PendingCommandStatus.REJECTED if status in {'FAILED', 'REJECTED'} else
                  PendingCommandStatus.OUTCOME_UNKNOWN if status == 'NEEDS_RECONCILIATION' else
                  PendingCommandStatus.PROCESSING)
        view = self.store.view
        account_key = view['account_key'] if view else request.get('account_key', '')
        pending = PendingCommandState(command_id, account_key, value.get('operation') or request.get('operation', ''),
                                      mapped, value.get('reason'), _freeze_receipt(value))
        self.store.pending[command_id] = pending
        if status in self._TERMINAL:
            self.store.pending.pop(command_id, None)
        self._persist_pending()
        self.command_changed.emit(pending)
        self.feedback.emit(f"命令回执 · {status} · {value.get('reason', '')}")

    def _failed(self, response):
        request = self._requests.pop(response['request_id'], {})
        if response['code'] == 'E_AUTH':
            self.lock()
        command_id = request.get('command_id')
        if command_id:
            unknown = request.get('kind') == 'submit' and response['code'] in self._CONNECTION_ERRORS
            prior = self.store.pending.get(command_id)
            pending = PendingCommandState(command_id, request.get('account_key', ''),
                request.get('operation', prior.operation if prior else ''),
                PendingCommandStatus.SUBMISSION_UNKNOWN if unknown else PendingCommandStatus.REJECTED,
                response['code'])
            self.store.pending[command_id] = pending
            if not unknown:
                self.store.pending.pop(command_id, None)
            self._persist_pending()
            self.command_changed.emit(pending)
            if unknown:
                self.feedback.emit('Runtime 命令结果未知；已保留原命令 ID，请查询回执，禁止重复提交。')
                return
        self.feedback.emit(f"Runtime 操作未完成 · {response['code']}")

    def _persist_pending(self):
        rows = [{'account_key': item.account_key, 'command_id': item.command_id, 'operation': item.operation}
                for item in list(self.store.pending.values())[-64:]]
        workbench_preferences().setValue('runtime/pending_receipts_v2', canonical_bytes(rows).decode())

    def _restore_pending(self):
        raw = workbench_preferences().value('runtime/pending_receipts_v2', '', str)
        try:
            rows = strict_loads(raw) if raw else []
            if not isinstance(rows, list):
                return
            for row in rows[-64:]:
                if isinstance(row, dict) and all(isinstance(row.get(key), str)
                                                 for key in ('account_key', 'command_id', 'operation')):
                    self.store.pending[row['command_id']] = PendingCommandState(
                        row['command_id'], row['account_key'], row['operation'],
                        PendingCommandStatus.OUTCOME_UNKNOWN, 'RESTORED_PENDING_RECEIPT')
        except (ValueError, TypeError):
            pass


def _freeze_receipt(value):
    from types import MappingProxyType
    return MappingProxyType(dict(value))
