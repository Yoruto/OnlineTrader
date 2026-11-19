"""Start the selected simulation directly and report progress in the workbench."""
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.configuration.files import publish_new_bytes
from online_trader.errors import TraderError, exception_record


START_ERRORS = {
    'E_DSV_PROFILE': '无法核对 DSV 参数，请在策略主控选择 DSV 配置。',
    'E_DSV_PROFILE_CHANGED': 'DSV 参数已变化，请核对列表后重新启动。若保留着本地编辑，请先重读 DSV 参数。',
    'E_DEPENDENCY': '交易连接组件不可用，请检查本机已配置的交易环境。',
    'E_CREDENTIALS': '账号或密码未保存完整，请到「账号信息 → 账户管理」核对。',
    'E_GATEWAY_CONNECT': '模拟盘连接失败，请核对账号、密码及网络后重试。',
    'E_CLOCK': '无法取得应用授时，请检查授时网络连接。',
    'E_ACCOUNT_LOCKED': '该账号已有运行程序，请先查看原来的运行状态。',
    'E_INSTANCE_EXISTS': '该账号已有运行程序，请先查看原来的运行状态。',
    'E_INSTANCE_LOCK': '账号状态暂时无法锁定，请稍后重试。',
    'E_ALREADY_RUNNING': '该账号已有运行程序，请先查看原来的运行状态。',
    'E_SCOPE': '账号或运行设置已变化，请重新核对后启动。',
    'E_REVISION_CONFLICT': '账号或参数已变化，请重新点击启动策略。',
    'E_CALENDAR_INPUT': '没有取得完整交易时段，策略尚未启动，请稍后重试。',
    'E_CALENDAR_INCOMPLETE': '没有取得完整交易时段，策略尚未启动，请稍后重试。',
    'E_ONLINE_SCOPE': '本次启动已过期或不在可用交易时段，请重新启动。',
    'E_FACT_BARRIER': '该账号有原运行记录需要恢复或核对，未开启新策略。',
    'E_CONFIG': '交易参数不完整，请到「交易参数」中核对。',
    'E_METADATA': '没有取得完整的合约资料和交易时段，请检查网络后重试。',
    'E_STARTUP_EXITED': '启动进程已退出，策略未能启动，请查看运行日志后重试。',
    'E_RUN_OUTPUT': '运行目录已使用或无法写入，请用新的可写目录启动。',
}


def launch_desktop_request(store, request):
    from online_trader.runtime.startup_environment import launch_bootstrap

    directory = store.directory / 'desktop-starts' / request['request_id']
    request_path = publish_new_bytes(canonical_bytes(request), directory / 'request.json')
    output = directory / 'run'
    return {'run_directory': str(output), 'launch': launch_bootstrap(request_path, output)}


class _StartWorker(QThread):
    started_run = Signal(object)
    failed = Signal(object)

    def __init__(self, store, account_id, profile, parent, *, timed_trigger=None, now=None):
        super().__init__(parent)
        self.store, self.account_id, self.profile = store, account_id, profile
        self.timed_trigger, self.now = timed_trigger, now

    def run(self):
        try:
            from online_trader.runtime.desktop_bootstrap import _original_scope, build_start_request
            from online_trader.scheduling.holdover import ProtectionClockAnchor
            from online_trader.scheduling.reference import CalibratedClock

            recovering = False
            instant = self.now
            if instant is None:
                row = self.store.get(self.account_id)
                anchor = ProtectionClockAnchor(self.store.ledger_path(row), row['account_key'])

                def original_scope_available():
                    if self.timed_trigger is not None:
                        return False
                    try:
                        _original_scope(self.store, row)
                    except TraderError:
                        return False
                    return True

                clock = CalibratedClock(recovery_anchor=anchor, recovering=original_scope_available)
                instant = clock.now()
                recovering = not clock.entry_ready()
            request = build_start_request(self.store, self.account_id, self.profile, arm=not recovering,
                                          now=instant, timed_trigger=self.timed_trigger)
            if recovering:
                request['recover_original'] = True
            self.started_run.emit(launch_desktop_request(self.store, request))
        except (TraderError, OSError, ValueError) as error:
            self.failed.emit({'code': getattr(error, 'code', 'E_DEPENDENCY'), 'exception': exception_record(error)})
        except Exception as error:
            self.failed.emit({'code': 'E_STARTUP', 'exception': exception_record(error)})


class QuoteReferenceWorker(QThread):
    received = Signal(object)
    failed = Signal(str)

    def __init__(self, store, account_id, codes, parent, *, catalog=False, offset_query=None):
        super().__init__(parent)
        self.store, self.account_id, self.codes = store, account_id, codes
        self.catalog = catalog
        self.offset_query = offset_query

    def run(self):
        from online_trader.runtime.startup_metadata import read_contract_catalog, read_startup_metadata
        try:
            if self.offset_query is not None:
                from online_trader.runtime.startup_environment import fetch_offset_inputs
                result = fetch_offset_inputs(self.store, self.account_id, *self.offset_query)
            elif self.catalog:
                result = read_contract_catalog(self.store,self.account_id)
            else:
                result = read_startup_metadata(self.store,self.account_id,self.codes,quote_preview=True)
        except (TraderError, OSError, ValueError) as error:
            if not self.isInterruptionRequested():
                self.failed.emit(getattr(error,'code','E_STARTUP_METADATA'))
        else:
            if not self.isInterruptionRequested():
                self.received.emit(result)


class StrategyStarter(QObject):
    """No window or startup side effects until the user clicks Start/F5."""

    status_changed = Signal(str)

    def __init__(self, store, account_id, profile, parent=None):
        super().__init__(parent)
        self.store, self.account_id, self.profile = store, account_id, profile
        self.account = store.get(account_id)
        self.run_directory = None
        self._worker = None
        self._launched = False
        self._process_id = None
        self.status_text = ''
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(1000)
        self.status_timer.timeout.connect(self.update_launch_status)

    def _set_status(self, message):
        self.status_text = message
        self.status_changed.emit(message)
        if self.parent() is not None:
            self.parent().show_home_feedback(message)

    def is_preparing(self):
        return self._worker is not None and self._worker.isRunning()

    def _set_busy(self, busy):
        parent = self.parent()
        if parent is not None:
            parent._startup_in_progress = busy
            parent._enable_commands(not parent._stale and parent.view is not None and not parent.view['truncated'])
            parent.dashboard.reload_accounts()

    def start(self, *, timed_trigger=None, now=None):
        try:
            self._start_current(timed_trigger=timed_trigger, now=now)
        except (TraderError, OSError, ValueError) as error:
            self._failed({'code': getattr(error, 'code', 'E_STARTUP')})

    def _start_current(self, *, timed_trigger=None, now=None):
        if self._worker is not None or self._launched:
            return
        parent = self.parent()
        state = self.store.read()
        if state['selected_id'] != self.account_id:
            self._set_status('已切换账号，请重新选择当前账号后启动。')
            return
        if parent is not None:
            if parent.selected_account is None or parent.selected_account['account_id'] != self.account_id:
                self._set_status('已切换账号，请重新选择当前账号后启动。')
                return
            self.profile = parent.current_start_profile()
            if parent.view is not None:
                self._set_status('已取得原执行器状态，请在主控页启动原策略。')
                return
        self.status_timer.stop()
        self.run_directory = None
        self._process_id = None
        self._set_status('正在准备交易连接环境…')
        self._set_busy(True)
        worker = _StartWorker(self.store, self.account_id, self.profile, self, timed_trigger=timed_trigger, now=now)
        self._worker = worker
        worker.started_run.connect(self._started)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._finished)
        worker.start()

    def _started(self, result):
        self._launched = True
        self.run_directory = Path(result['run_directory'])
        self._process_id = result['launch']['pid']
        self._set_status('正在连接行情并核对交易时段…')
        self.status_timer.start()

    def _failed(self, result):
        code = result['code']
        from online_trader.runtime.desktop_bootstrap import ERROR_MESSAGES
        message = ERROR_MESSAGES.get(code) or START_ERRORS.get(code, '启动未完成，请查看运行日志或检查账号设置。')
        self.status_timer.stop()
        self._set_status(message + f'（{code}）')
        self._launched = False
        self._set_busy(False)

    def _finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        parent = self.parent()
        if parent is not None and parent._closing:
            QTimer.singleShot(0, parent.close)

    def update_launch_status(self):
        if self.run_directory is None:
            return
        failure = self.run_directory / 'startup-failure.json'
        paths = list(self.run_directory.glob('phase-*.json'))
        path = failure if failure.is_file() else max(paths, key=lambda item: item.stat().st_mtime_ns, default=None)
        if path is not None:
            try:
                if path.stat().st_size > 65536:
                    return
                record = strict_loads(path.read_bytes())
            except (OSError, TraderError, ValueError):
                return
            if failure.is_file():
                self.status_timer.stop()
                self._failed({'code': record.get('error_code', 'E_STARTUP')})
                return
            phase = record.get('phase') or record.get('status')
            message = {
                'STARTUP_CHECKING': '正在核对账号与当前交易参数…',
                'STARTUP_READING_MARKET': '正在连接行情并核对交易日、交易时段和合约信息…',
                'STARTUP_PREPARED': '交易时段已核对，正在启动策略并同步柜台持仓…',
                'DSV_OBSERVING': '已连接模拟盘，正在同步行情并等待策略信号。',
                'DSV_MANAGING_OWNED': '原持仓已恢复，继续管理已有持仓。',
            }.get(phase)
            if message:
                self._set_status(message)
        if self._process_id is not None:
            from online_trader.runtime.startup_environment import process_running
            ended = process_running(self._process_id) is False
            if ended:
                if (self.run_directory / 'result.json').is_file():
                    self._set_status('本次策略运行已结束。')
                    self._launched = False
                    self.status_timer.stop()
                    self._set_busy(False)
                    return
                self.status_timer.stop()
                self._failed({'code': 'E_STARTUP_EXITED'})
                return
        parent = self.parent()
        if parent is not None:
            view = parent.view
            if view is not None and view['account_key'] == self.account['account_key'] and not parent._stale:
                if view['entry_authority'] == 'ARMED':
                    self._set_status('策略已启动，正在监控所选合约。')
                    self._set_busy(False)
                elif view['configuration'].get('strategy_stop_requested'):
                    self._set_status(view['configuration'].get('strategy_stop_wait') or '策略已停止开仓，正在核对退出条件。')
                    self._set_busy(False)
                else:
                    self._set_status('已连接模拟盘，正在核对行情、持仓及开仓条件。')
                    self._set_busy(False)
            parent.show_home_feedback(self.status_text)
