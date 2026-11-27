"""Bounded Runtime I/O lanes and the single Qt command bus."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
from queue import Empty, Full, Queue
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from online_trader.contracts.codec import content_hash
from online_trader.errors import TraderError
from online_trader.runtime.instance import runtime_owner_exists
from online_trader.runtime.instance_registry import read_runtime_instance
from online_trader.runtime.operator_api import RuntimeWorkbenchClient
from online_trader.transport.runtime_account import RuntimeAccountClient
from online_trader.transport.runtime_view import RuntimeViewClient, ViewReplica
from online_trader.desktop.runtime_client.pending_registry import PendingCommandRegistry
from online_trader.desktop.runtime_client.state import PendingCommandState, PendingCommandStatus
from online_trader.desktop.runtime_logs import read_record, supplied_run_directory


class RuntimeSubscriptionWorker(QThread):
    state_received = Signal(object, object, object)
    publication_observed = Signal(object)
    connection_changed = Signal(str, str)
    lifecycle_changed = Signal(object)

    _PROBE_INTERVAL_NS = 1_500_000_000

    def __init__(self, pipe, parent=None):
        super().__init__(parent)
        self.pipe = pipe
        self.client = RuntimeViewClient(pipe)
        self._last_account_key = None
        self._last_boot_id = None
        self._last_run_dir = None
        self._initial_instance_id = None
        self._last_lifecycle = None
        self._last_lifecycle_key = None
        self._last_probe_ns = 0
        try:
            inst = read_runtime_instance()
            if isinstance(inst, dict) and inst.get('pipe') == pipe:
                self._initial_instance_id = inst.get('instance_id')
                self._last_account_key = inst.get('account_key')
                rt_root = inst.get('runtime_root')
                if isinstance(rt_root, str) and rt_root and isinstance(self._last_account_key, str) and self._last_account_key:
                    self._last_run_dir = Path(rt_root) / self._last_account_key / 'run'
        except Exception:
            pass

    def _capture_identity(self, state):
        if not isinstance(state, dict):
            return
        view = state.get('view') if isinstance(state.get('view'), dict) else state
        acct = view.get('account_key')
        if isinstance(acct, str) and acct:
            self._last_account_key = acct
        if isinstance(view.get('configuration'), dict):
            run_dir = supplied_run_directory(view)
            if run_dir is not None:
                self._last_run_dir = run_dir
        boot_id = view.get('runtime_boot_id')
        if not boot_id and self._last_run_dir is not None:
            status_rec = read_record(self._last_run_dir / 'target-owner-status.json')
            if isinstance(status_rec, dict) and isinstance(status_rec.get('runtime_boot_id'), str):
                boot_id = status_rec['runtime_boot_id']
        if not boot_id:
            boot_id = view.get('kernel_boot_id')
        if isinstance(boot_id, str) and boot_id:
            self._last_boot_id = boot_id

    def _read_matched_record(self, path, *, require_boot_match=True):
        if path is None or not path.exists():
            return None
        record = read_record(path)
        if not isinstance(record, dict):
            return None
        if self._last_account_key and record.get('account_key') not in (None, self._last_account_key):
            return None
        if self._last_boot_id:
            rec_boot = record.get('runtime_boot_id')
            if require_boot_match:
                if rec_boot != self._last_boot_id:
                    return None
            elif rec_boot not in (None, self._last_boot_id):
                return None
        return record

    def _emit_lifecycle(self, payload, *, force=False):
        key = (
            payload.get('state'),
            payload.get('error_code'),
            payload.get('runtime_boot_id'),
            payload.get('pipe'),
        )
        self._last_lifecycle = payload
        if force or key != self._last_lifecycle_key:
            self._last_lifecycle_key = key
            self.lifecycle_changed.emit(dict(payload))

    def _probe_lifecycle(self, *, pipe_connected):
        now_ns = time.monotonic_ns()
        if not force_probe_needed(self._last_probe_ns, now_ns, self._PROBE_INTERVAL_NS) and self._last_lifecycle is not None:
            return self._last_lifecycle
        self._last_probe_ns = now_ns
        try:
            reg = read_runtime_instance()
        except Exception:
            reg = None
        try:
            owner_locked = runtime_owner_exists()
        except Exception:
            owner_locked = False
        if isinstance(reg, dict):
            reg_pipe = reg.get('pipe')
            reg_instance = reg.get('instance_id')
            if self._last_run_dir is None and isinstance(reg.get('runtime_root'), str) and isinstance(reg.get('account_key'), str):
                self._last_run_dir = Path(reg['runtime_root']) / reg['account_key'] / 'run'
            if owner_locked and (
                (isinstance(reg_pipe, str) and reg_pipe and reg_pipe != self.pipe)
                or (self._initial_instance_id and isinstance(reg_instance, str) and reg_instance != self._initial_instance_id)
            ):
                return {
                    'state': 'OWNER_REPLACED',
                    'owner_locked': True,
                    'pipe': reg_pipe,
                    'instance_id': reg_instance,
                    'account_key': reg.get('account_key'),
                    'runtime_root': reg.get('runtime_root'),
                }
        if owner_locked:
            if pipe_connected:
                return {
                    'state': 'OWNER_UNRESPONSIVE',
                    'owner_locked': True,
                    'runtime_boot_id': self._last_boot_id,
                    'account_key': self._last_account_key,
                    'detail': 'Runtime 进程锁仍被持有，但视图快照未按期更新（无法确认主循环健康状态）',
                }
            return {
                'state': 'VIEW_RECONNECTING',
                'owner_locked': True,
                'runtime_boot_id': self._last_boot_id,
                'account_key': self._last_account_key,
                'detail': 'Runtime 进程锁仍被持有，正在重连视图通道（尚无法确认主循环状态）',
            }
        run_dir = self._last_run_dir
        runtime_failure = self._read_matched_record(run_dir / 'runtime-failure.json') if run_dir is not None else None
        if runtime_failure is not None:
            return {
                'state': 'OWNER_CRASHED',
                'owner_locked': False,
                'runtime_boot_id': runtime_failure.get('runtime_boot_id') or self._last_boot_id,
                'account_key': runtime_failure.get('account_key') or self._last_account_key,
                'error_code': runtime_failure.get('error_code') or 'E_RUNTIME_OWNER_EXITED',
                'detail': runtime_failure.get('detail') or runtime_failure.get('message') or '',
                'observed_at_utc': runtime_failure.get('observed_at_utc'),
            }
        status_rec = self._read_matched_record(run_dir / 'target-owner-status.json') if run_dir is not None else None
        if status_rec is not None:
            phase = status_rec.get('phase')
            if phase == 'STOPPED':
                return {
                    'state': 'OWNER_STOPPED',
                    'owner_locked': False,
                    'runtime_boot_id': status_rec.get('runtime_boot_id') or self._last_boot_id,
                    'account_key': status_rec.get('account_key') or self._last_account_key,
                    'observed_at_utc': status_rec.get('observed_at_utc'),
                }
            if phase == 'NEEDS_REVIEW':
                return {
                    'state': 'OWNER_CRASHED',
                    'owner_locked': False,
                    'runtime_boot_id': status_rec.get('runtime_boot_id') or self._last_boot_id,
                    'account_key': status_rec.get('account_key') or self._last_account_key,
                    'error_code': status_rec.get('error_code') or 'E_RUNTIME_OWNER_EXITED',
                    'detail': status_rec.get('detail') or '',
                    'observed_at_utc': status_rec.get('observed_at_utc'),
                }
        startup_failure = (
            self._read_matched_record(run_dir / 'startup-failure.json', require_boot_match=self._last_boot_id is not None)
            if run_dir is not None else None
        )
        if startup_failure is not None:
            return {
                'state': 'OWNER_CRASHED',
                'owner_locked': False,
                'runtime_boot_id': startup_failure.get('runtime_boot_id') or self._last_boot_id,
                'account_key': startup_failure.get('account_key') or self._last_account_key,
                'error_code': startup_failure.get('error_code') or 'E_RUNTIME_OWNER_EXITED',
                'detail': startup_failure.get('detail') or startup_failure.get('message') or '',
                'observed_at_utc': startup_failure.get('observed_at_utc'),
            }
        return {
            'state': 'OWNER_CRASHED',
            'owner_locked': False,
            'runtime_boot_id': self._last_boot_id,
            'account_key': self._last_account_key,
            'error_code': 'E_RUNTIME_OWNER_EXITED',
            'detail': '未检测到 Runtime 进程锁，也未找到正常停机状态记录',
        }

    def _watch_for_new_runtime(self):
        while not self.isInterruptionRequested():
            for _ in range(4):
                if self.isInterruptionRequested():
                    return None
                self.msleep(500)
            try:
                owner_locked = runtime_owner_exists()
                reg = read_runtime_instance() if owner_locked else None
            except Exception:
                continue
            if owner_locked and isinstance(reg, dict) and isinstance(reg.get('pipe'), str) and reg['pipe']:
                return reg
        return None

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
                            verdict = self._probe_lifecycle(pipe_connected=True)
                            self._emit_lifecycle(verdict)
                            if verdict['state'] == 'OWNER_REPLACED':
                                return
                            continue
                        if replica.apply(message):
                            self._capture_identity(replica.state)
                            if self._last_lifecycle_key is not None and self._last_lifecycle_key[0] != 'HEALTHY':
                                self._emit_lifecycle({
                                    'state': 'HEALTHY',
                                    'runtime_boot_id': self._last_boot_id,
                                    'account_key': self._last_account_key,
                                })
                            self.state_received.emit(replica.state, replica.cursor, int(message['published_ns']))
                        else:
                            self.publication_observed.emit(int(message['published_ns']))
            except TraderError as error:
                if self.isInterruptionRequested():
                    return
                state = 'INCOMPATIBLE' if error.code == 'E_SCHEMA' else 'RESYNC_REQUIRED'
                self.connection_changed.emit(state, error.code)
                replica = ViewReplica()
            except (OSError, KeyError, TypeError, ValueError):
                if self.isInterruptionRequested():
                    return
                self._last_probe_ns = 0
                verdict = self._probe_lifecycle(pipe_connected=False)
                if verdict['state'] == 'OWNER_REPLACED':
                    self._emit_lifecycle(verdict, force=True)
                    return
                if verdict['state'] in {'OWNER_CRASHED', 'OWNER_STOPPED'}:
                    self.connection_changed.emit(
                        'RESYNC_REQUIRED',
                        verdict.get('error_code') or 'E_RUNTIME_OWNER_EXITED',
                    )
                    self._emit_lifecycle(verdict, force=True)
                    replica = ViewReplica()
                    replacement = self._watch_for_new_runtime()
                    if replacement is None:
                        return
                    if replacement['pipe'] != self.pipe:
                        self._emit_lifecycle({
                            'state': 'OWNER_REPLACED',
                            'owner_locked': True,
                            'pipe': replacement['pipe'],
                            'instance_id': replacement.get('instance_id'),
                            'account_key': replacement.get('account_key'),
                            'runtime_root': replacement.get('runtime_root'),
                        }, force=True)
                        return
                    self._initial_instance_id = replacement.get('instance_id')
                    self._last_boot_id = None
                    self._last_lifecycle_key = None
                    self._emit_lifecycle({
                        'state': 'VIEW_RECONNECTING',
                        'owner_locked': True,
                        'account_key': replacement.get('account_key'),
                        'detail': '检测到新启动的 Runtime 实例，正在重新连接',
                    }, force=True)
                    continue
                self.connection_changed.emit('STALE', 'E_RUNTIME_VIEW')
                self._emit_lifecycle(verdict)
                replica = ViewReplica()
            self.msleep(300)

    def shutdown(self):
        self.requestInterruption()
        self.client.close()


def force_probe_needed(last_probe_ns, now_ns, interval_ns):
    return last_probe_ns == 0 or now_ns - last_probe_ns >= interval_ns


class RuntimeCommandWorker(QThread):
    result = Signal(object)
    failed = Signal(object)

    def __init__(self, pipe, parent=None):
        super().__init__(parent)
        self.client = RuntimeWorkbenchClient(pipe)
        self.queue = Queue(maxsize=32)

    def enqueue(self, request):
        try:
            self.queue.put_nowait(request)
        except Full:
            raise TraderError('E_BUSY', 'Runtime command queue is full') from None

    def run(self):
        while not self.isInterruptionRequested():
            try:
                request = self.queue.get(timeout=.2)
            except Empty:
                continue
            if request is None or self.isInterruptionRequested():
                return
            try:
                value = self.client.request(request['action'], request['body'])
                self.result.emit(request | {'value': value})
            except TraderError as error:
                if self.isInterruptionRequested():
                    return
                self.failed.emit(request | {'code': error.code, 'message': str(error)})
            except OSError as error:
                if self.isInterruptionRequested():
                    return
                self.failed.emit(request | {'code': 'E_RUNTIME_CONNECTION', 'message': str(error)})

    def shutdown(self):
        self.requestInterruption()
        try:
            self.queue.put_nowait(None)
        except Exception:
            pass
        self.client.close()


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
        except Full:
            raise TraderError('E_BUSY', 'Runtime query queue is full') from None

    def run(self):
        while not self.isInterruptionRequested():
            try:
                request = self.queue.get(timeout=.2)
            except Empty:
                continue
            if request is None or self.isInterruptionRequested():
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
                if self.isInterruptionRequested():
                    return
                self.failed.emit(request | {'code': getattr(error, 'code', 'E_RUNTIME_QUERY'),
                                             'message': str(error)})

    def shutdown(self):
        self.requestInterruption()
        try:
            self.queue.put_nowait(None)
        except Exception:
            pass
        self.view.close()
        self.account.close()


class RuntimeClientCoordinator(QObject):
    command_result = Signal(object)
    command_failed = Signal(object)
    query_result = Signal(object)
    query_failed = Signal(object)
    publication_observed = Signal(object)
    lifecycle_changed = Signal(object)

    def __init__(self, pipe, store, parent=None):
        super().__init__(parent)
        self.pipe, self.store = pipe, store
        self.subscription = RuntimeSubscriptionWorker(pipe, self)
        self.commands = RuntimeCommandWorker(pipe, self)
        self.queries = RuntimeQueryWorker(pipe, self)
        self.subscription.state_received.connect(store.apply)
        self.subscription.publication_observed.connect(self.publication_observed)
        self.subscription.connection_changed.connect(store.set_connection)
        self.subscription.lifecycle_changed.connect(self.lifecycle_changed)
        self.commands.result.connect(self.command_result)
        self.commands.failed.connect(self.command_failed)
        self.queries.result.connect(self.query_result)
        self.queries.failed.connect(self.query_failed)
        self._closed = False

    def start(self):
        if self._closed:
            raise TraderError('E_PIPE_CLOSED', 'Runtime client coordinator is closed')
        self.subscription.start()
        self.commands.start()
        self.queries.start()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for worker in (self.subscription, self.commands, self.queries):
            worker.shutdown()
        for worker in (self.subscription, self.commands, self.queries):
            worker.wait()


class CommandBus(QObject):
    command_changed = Signal(object)
    feedback = Signal(str)
    config_previewed = Signal(object)

    _TERMINAL = {'SUCCEEDED', 'FAILED', 'REJECTED', 'SUPERSEDED'}
    _RECEIPT_STATUSES = _TERMINAL | {'COMMITTED', 'ACCEPTED', 'EXECUTING', 'NEEDS_RECONCILIATION'}
    _DEFINITE_SUBMIT_REJECTIONS = {
        'E_AUTH', 'E_BUSY', 'E_CAPABILITY', 'E_CONFIG_EDIT', 'E_CONTEXT_EXPIRED',
        'E_BOOT_MISMATCH', 'E_NOT_READY', 'E_REVISION_CONFLICT', 'E_SCHEMA', 'E_SCOPE',
        'E_OWNERSHIP', 'E_RULE_UNAPPROVED', 'E_UNSUPPORTED', 'E_DUPLICATE_CONFLICT',
        'E_STARTUP_METADATA', 'E_CALENDAR_INCOMPLETE', 'E_ONLINE_SCOPE',
        'E_DSV_COMMAND', 'E_DSV_SCOPE', 'E_DSV_SDK_CLOCK', 'E_DSV_SDK_SCOPE',
        'E_DSV_SDK_SYNC', 'E_DSV_SYNC', 'E_DSV_DUPLICATE_CONFLICT',
        'E_DSV_NATIVE_ID', 'E_DSV_OWNERSHIP',
    }

    def __init__(self, coordinator, store, parent=None):
        super().__init__(parent)
        self.coordinator, self.store = coordinator, store
        self._requests = {}
        coordinator.command_result.connect(self._result)
        coordinator.command_failed.connect(self._failed)
        store.owner_boot_changed.connect(self._owner_boot_changed)
        self._pending_storage_error = None
        self._registry = PendingCommandRegistry()
        self._restore_pending()

    def submit(self, operation, scope, parameters):
        self._require_pending_storage()
        view = self.store.view
        if view is None or not self.store.allows_owner_recheck_submission(operation):
            raise TraderError('E_NOT_READY', '请先取得当前完整 Runtime 状态')
        allowed, reason = self.store.capability(operation)
        if not allowed:
            raise TraderError('E_CAPABILITY', reason or '当前 Runtime 不允许此操作')
        scope_hash = content_hash({'operation': operation, 'scope': scope})
        command_id = 'qt-' + uuid4().hex
        body = {'command_id': command_id, 'operation': operation, 'scope': scope,
                'parameters': parameters, 'binding': {
                    'account_key': view['account_key'], 'kernel_boot_id': view['kernel_boot_id'],
                    'config_revision': view['configuration']['config_revision']}}
        request_id = 'client-' + uuid4().hex
        pending = PendingCommandState(command_id, view['account_key'], operation,
                                      PendingCommandStatus.SUBMITTING, scope_hash=scope_hash)
        try:
            records = self._registry.register(pending)
        except TraderError as error:
            self._record_storage_error(error)
            raise
        self._replace_pending(records, preserve={command_id: pending})
        request = {'kind': 'submit', 'command_id': command_id,
                   'account_key': view['account_key'], 'operation': operation,
                   'scope_hash': scope_hash}
        self._requests[request_id] = request
        self.command_changed.emit(pending)
        try:
            self.coordinator.commands.enqueue({'request_id': request_id, 'action': 'submit', 'body': body})
        except TraderError as error:
            self._requests.pop(request_id, None)
            not_enqueued = replace(pending, status=PendingCommandStatus.LOCAL_NOT_ENQUEUED,
                                   reason='LOCAL_QUEUE_NOT_ENQUEUED')
            self.store.pending[command_id] = not_enqueued
            self.command_changed.emit(not_enqueued)
            try:
                records = self._registry.remove(command_id, pending.account_key, pending.operation)
            except TraderError as storage_error:
                self._record_storage_error(storage_error)
                self.store.pending[command_id] = not_enqueued
                raise TraderError('E_STORAGE',
                                  '命令未进入本地队列，但本次会话状态无法安全清理；已禁止新命令') from None
            self._replace_pending(records)
            raise error
        return command_id

    def preview_config(self, profile):
        view = self.store.view
        if view is None or self.store.connection_state.value != 'READY':
            raise TraderError('E_NOT_READY', '请先取得当前完整 Runtime 状态')
        request_id = 'client-' + uuid4().hex
        body = {'profile': profile.to_record(), 'binding': {
            'account_key': view['account_key'], 'kernel_boot_id': view['kernel_boot_id'],
            'config_revision': view['configuration']['config_revision']}}
        self._requests[request_id] = {
            'kind': 'preview-config',
            'profile_hash': profile.content_hash,
            'account_key': view['account_key'],
            'kernel_boot_id': view['kernel_boot_id'],
            'config_revision': view['configuration']['config_revision'],
        }
        try:
            self.coordinator.commands.enqueue({'request_id': request_id, 'action': 'preview-config', 'body': body})
        except TraderError:
            self._requests.pop(request_id, None)
            raise
        self.feedback.emit('正在由 Runtime 预览并校验参数草稿。')

    def query(self, command_id):
        prior = self.store.pending.get(command_id)
        if prior is None:
            raise TraderError('E_OWNERSHIP', '只能查询本次工作台会话中保留的原命令 ID')
        request_id = 'client-' + uuid4().hex
        self._requests[request_id] = {'kind': 'receipt', 'command_id': command_id,
                                      'account_key': prior.account_key,
                                      'operation': prior.operation,
                                      'scope_hash': prior.scope_hash}
        try:
            self.coordinator.commands.enqueue({'request_id': request_id, 'action': 'receipt',
                                               'body': {'command_id': command_id}})
        except TraderError:
            self._requests.pop(request_id, None)
            raise
        self.feedback.emit(f'正在查询原命令 {command_id}。')

    def acknowledge_unknown(self, command_id):
        """Explicitly release an unresolved UNKNOWN command after user verification."""
        self._require_pending_storage()
        prior = self.store.pending.get(command_id)
        if prior is None:
            raise TraderError('E_OWNERSHIP', '只能解除本次工作台会话中保留的原命令 ID')
        if prior.status not in {PendingCommandStatus.OUTCOME_UNKNOWN, PendingCommandStatus.SUBMISSION_UNKNOWN}:
            raise TraderError('E_NOT_READY', '该命令仍在处理中，暂不允许解除锁定')
        try:
            records = self._registry.remove(command_id, prior.account_key, prior.operation)
        except TraderError as error:
            self._record_storage_error(error)
            raise
        cleared = replace(prior, status=PendingCommandStatus.REJECTED, reason='USER_ACKNOWLEDGED_UNKNOWN')
        self._replace_pending(records)
        self.command_changed.emit(cleared)
        self.feedback.emit(f'已人工确认解除未决命令 {command_id} 的本地锁定；请核对持仓与委托后再继续操作。')

    @property
    def has_inflight(self):
        return bool(self._requests)

    def migrate_legacy_pending(self):
        """Ignore legacy durable pending slots under broker-authoritative startup."""
        if self._pending_storage_error is not None:
            return
        try:
            records = self._registry.migrate_legacy_slot()
        except TraderError as error:
            self._record_storage_error(error)
            return
        self._replace_pending(records)

    def _result(self, response):
        request = self._requests.pop(response.get('request_id'), None)
        if request is None:
            self.feedback.emit('忽略无法关联到本机请求的 Runtime 回应。')
            return
        value = response.get('value')
        if request.get('kind') == 'preview-config':
            if (not isinstance(value, dict)
                    or value.get('profile_hash') != request.get('profile_hash')
                    or value.get('account_key') != request.get('account_key')
                    or value.get('kernel_boot_id') != request.get('kernel_boot_id')
                    or value.get('config_revision') != request.get('config_revision')):
                self.feedback.emit('Runtime 参数预览哈希与本地草稿不一致。')
                return
            self.config_previewed.emit(value)
            self.feedback.emit(f"Runtime 参数预览通过 · 配置版本 {value['config_revision']}")
            return
        expected_id = request.get('command_id')
        prior = self.store.pending.get(expected_id)
        identity_matches = (isinstance(value, dict) and prior is not None
                            and prior.account_key == request.get('account_key')
                            and value.get('command_id') == expected_id
                            and value.get('account_key') == request.get('account_key')
                            and value.get('operation') == request.get('operation')
                            and value.get('status') in self._RECEIPT_STATUSES)
        if not identity_matches:
            self._retain_unknown(request, 'E_RESPONSE_IDENTITY')
            self.feedback.emit(
                f'Runtime 回应与原请求身份不一致；已保留原命令 {expected_id}，禁止据此清理责任。')
            return
        command_id = expected_id
        status = value.get('status')
        terminal_commit = (status == 'COMMITTED'
                           and str(value.get('tx_id', '')).startswith('target-'))
        is_terminal = status in self._TERMINAL or terminal_commit
        mapped = (PendingCommandStatus.COMPLETED if (status in {'SUCCEEDED', 'SUPERSEDED'} or terminal_commit) else
                  PendingCommandStatus.REJECTED if status in {'FAILED', 'REJECTED'} else
                  PendingCommandStatus.OUTCOME_UNKNOWN if status == 'NEEDS_RECONCILIATION' else
                  PendingCommandStatus.PROCESSING)
        pending = PendingCommandState(command_id, request['account_key'], request['operation'],
                                      mapped, value.get('reason'), _freeze_receipt(value),
                                      request.get('scope_hash'))
        try:
            if is_terminal:
                records = self._registry.remove(
                    command_id, request['account_key'], request['operation'])
            else:
                records = self._registry.upsert(pending)
        except TraderError as error:
            self._record_storage_error(error)
            if is_terminal:
                pending = replace(pending, status=PendingCommandStatus.OUTCOME_UNKNOWN,
                                  reason='E_PENDING_STORAGE')
                self.store.pending[command_id] = pending
            self.command_changed.emit(pending)
            self.feedback.emit(
                f'已收到命令 {command_id} 的回执，但本机注册表无法同步；已禁止新命令。')
            return
        self._replace_pending(records, preserve={} if is_terminal else {command_id: pending})
        self.command_changed.emit(pending)
        self.feedback.emit(f"命令回执 · {status} · {value.get('reason', '')}")

    def _failed(self, response):
        request = self._requests.pop(response.get('request_id'), None)
        if request is None:
            self.feedback.emit('忽略无法关联到本机请求的 Runtime 失败回应。')
            return
        command_id = request.get('command_id')
        if command_id:
            prior = self.store.pending.get(command_id)
            if (prior is None or prior.account_key != request.get('account_key')
                    or prior.operation != request.get('operation')):
                self.feedback.emit(
                    f'Runtime 失败回应与本机 pending 不匹配；未改写命令 {command_id}。')
                return
            if request.get('kind') == 'receipt':
                self._retain_unknown(request, response.get('code', 'E_RUNTIME_CONNECTION'))
                self.feedback.emit(
                    f'原命令 {command_id} 的回执查询未完成；结果仍未知，已保留原命令 ID。')
                return
            code = response.get('code', 'E_RUNTIME_CONNECTION')
            rejected = request.get('kind') == 'submit' and code in self._DEFINITE_SUBMIT_REJECTIONS
            pending = PendingCommandState(command_id, request['account_key'], request['operation'],
                PendingCommandStatus.REJECTED if rejected else PendingCommandStatus.SUBMISSION_UNKNOWN,
                code, scope_hash=request.get('scope_hash'))
            try:
                if rejected:
                    records = self._registry.remove(
                        command_id, request['account_key'], request['operation'])
                else:
                    records = self._registry.upsert(pending)
            except TraderError as error:
                self._record_storage_error(error)
                if rejected:
                    pending = replace(pending, status=PendingCommandStatus.OUTCOME_UNKNOWN,
                                      reason='E_PENDING_STORAGE')
                    self.store.pending[command_id] = pending
                self.command_changed.emit(pending)
                self.feedback.emit(
                    f'Runtime 已返回 {code}，但本机注册表无法同步；已禁止新命令。')
                return
            self._replace_pending(records, preserve={} if rejected else {command_id: pending})
            self.command_changed.emit(pending)
            if not rejected:
                self.feedback.emit('Runtime 命令结果未知；已保留原命令 ID，请查询回执，禁止重复提交。')
                return
        self.feedback.emit(f"Runtime 操作未完成 · {response.get('code', 'E_RUNTIME_CONNECTION')}")

    def _restore_pending(self):
        try:
            records = self._registry.restore()
        except TraderError as error:
            self.store.pending.clear()
            self._record_storage_error(error)
            return
        self._replace_pending(records)

    def _require_pending_storage(self):
        if self._pending_storage_error is not None:
            raise TraderError('E_STORAGE',
                              f'本机未决命令注册表不可安全使用：{self._pending_storage_error}；禁止新命令')

    def _retain_unknown(self, request, reason):
        command_id = request.get('command_id')
        prior = self.store.pending.get(command_id)
        if prior is None or prior.account_key != request.get('account_key'):
            return
        pending = replace(prior, status=PendingCommandStatus.OUTCOME_UNKNOWN, reason=reason)
        try:
            records = self._registry.upsert(pending)
        except TraderError as error:
            self._record_storage_error(error)
            self.store.pending[command_id] = pending
        else:
            self._replace_pending(records, preserve={command_id: pending})
        self.command_changed.emit(pending)

    def _owner_boot_changed(self, previous_boot, current_boot):
        view = self.store.view
        account_key = view.get('account_key') if view is not None else None
        if not account_key:
            return
        try:
            records, changed = self._registry.mark_account_unknown(account_key, 'OWNER_RESTARTED')
        except TraderError as error:
            self._record_storage_error(error)
            self.feedback.emit('Owner 已变化，但本机未决命令注册表无法同步；已禁止新命令。')
            return
        self._replace_pending(records, preserve=changed)
        for value in changed.values():
            self.command_changed.emit(value)
        if changed:
            self.feedback.emit(
                f'Runtime Owner 已从 {previous_boot} 切换到 {current_boot}；'
                f'已保留 {len(changed)} 个原命令 ID，重新同步后请查询回执。')

    def _replace_pending(self, records, *, preserve=None):
        values = dict(records)
        if preserve:
            values.update(preserve)
        self.store.pending.clear()
        self.store.pending.update(values)

    def _record_storage_error(self, error):
        if isinstance(error, TraderError) and error.code == 'E_STORAGE':
            self._pending_storage_error = str(error)


def _freeze_receipt(value):
    from types import MappingProxyType
    return MappingProxyType(dict(value))
