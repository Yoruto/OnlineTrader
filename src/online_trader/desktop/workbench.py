"""Trading workbench over the original owner IPC. Closing is local only."""
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sys
import time
import uuid
from PySide6.QtCore import QEvent,QEventLoop,QItemSelectionModel,QSettings,QStandardPaths,Qt,QThread,QTimer,Signal
from PySide6.QtWidgets import (QApplication,QLabel,QLineEdit,QPushButton,QTableWidget,
    QTableWidgetItem,QInputDialog,QFileDialog,QMessageBox,QProgressDialog)

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import strict_loads
from online_trader.desktop.app import StatusWindow
from online_trader.desktop.dingtalk import WorkbenchDingTalk
from online_trader.desktop.notifications import WorkbenchSounds
from online_trader.desktop.preferences import workbench_preferences
from online_trader.desktop.runtime_client.state import OWNER_RECHECK_OPERATIONS
from online_trader.desktop.tray import WorkbenchTray
from online_trader.desktop.workbench_layout import QUOTE_TIME_MESSAGES, WorkbenchLayout, format_reason_text, sorted_orders
from online_trader.errors import TraderError
from online_trader.runtime.market_monitor import append_latency
from online_trader.transport.status_client import StatusClient
from online_trader.transport.workbench_snapshot import MAX_AGE_NS, read_workbench_publication


RECONNECT_TIMEOUT_NS = 30_000_000_000
CONNECTION_ERRORS = frozenset({'E_TIMEOUT', 'E_PIPE', 'E_PIPE_CLOSED'})


class _RuntimeShutdownWorker(QThread):
    completed = Signal(bool, object)

    def __init__(self, shutdown_fn, pipe_name, *, pid, view, timeout):
        super().__init__()
        self._shutdown_fn = shutdown_fn
        self._pipe_name = pipe_name
        self._pid = pid
        self._view = view
        self._timeout = timeout

    def run(self):
        try:
            ok, error = self._shutdown_fn(
                self._pipe_name, pid=self._pid, view=self._view, timeout=self._timeout
            )
            self.completed.emit(bool(ok), error)
        except Exception as error:
            self.completed.emit(False, str(error))


class _WorkbenchRequest(QThread):
    result=Signal(object);failed=Signal(object)
    def __init__(self,pipe,factory,action,args,account_key,boot,*,publication_after_ns=0,direct_refresh=False):
        super().__init__();self.pipe=pipe;self.factory=factory;self.action=action;self.args=args
        self.account_key=account_key;self.boot=boot
        self.publication_after_ns = publication_after_ns
        self.direct_refresh = direct_refresh
        self._arm_receipt_from_lookup = False
    def run(self):
        client=None
        previous_receipt = None
        try:
            if self.action == 'refresh' and self.factory is StatusClient and not self.direct_refresh:
                value = read_workbench_publication(self.pipe, self.account_key)
                if value is not None and value['published_ns'] >= self.publication_after_ns:
                    if not self.isInterruptionRequested():
                        self.result.emit({'action': self.action, 'value': value})
                    return
            client=self.factory(self.pipe,expected_account_key=self.account_key,expected_kernel_boot_id=self.boot)
            if self.action=='refresh':
                requested_ns = time.monotonic_ns()
                view=client.workbench();metadata=client.last_metadata
                base=view.get('status_projection')
                if base is None:
                    client.expected_kernel_boot_id=metadata['kernel_boot_id']
                    base=client.snapshot();metadata=client.last_metadata
                accounts = [row for row in base['records'] if row['record_type'] == 'ACCOUNT']
                if (len(accounts) != 1 or accounts[0]['record_id'] != view['account_key']
                        or accounts[0]['record_revision'] != view['checkpoint_event_seq']
                        or metadata['kernel_boot_id'] != view['kernel_boot_id']):
                    raise TraderError('E_SNAPSHOT', '账户与工作台快照版本不一致，请重新刷新。')
                value={'base':base,'metadata':metadata,'view':view,'published_ns':requested_ns}
            elif self.action=='status':value=client.command_status(self.args)
            elif self.action=='monitor':
                value = {'monitor': client.workbench_monitor(), 'metadata': client.last_metadata}
            elif self.action=='close_positions':
                value = client.workbench_positions(self.args[1])
            elif self.action == 'recover':
                from online_trader.desktop.recovery import recover_workbench
                value = recover_workbench(client, *self.args, self.isInterruptionRequested)
            else:
                if self.args[0] != 'ARM_ENTRIES':
                    value = client.command(*self.args)
                else:
                    value = self._submit_arm(client)
                if (self.args[0] == 'ARM_ENTRIES' and value['status'] == 'REJECTED'
                        and value['error_code'] == 'E_CONTEXT_EXPIRED' and not self._arm_receipt_from_lookup
                        and not self.isInterruptionRequested()):
                    try:
                        view = client.workbench()
                    except TraderError:
                        view = None
                    if view is not None and self._can_reissue_arm(view) and not self.isInterruptionRequested():
                        previous_receipt = value
                        client.last_command = None
                        value = self._submit_arm(client)
            if not self.isInterruptionRequested():
                self.result.emit({'action':self.action,'value':value,'previous_receipt':previous_receipt})
        except Exception as error:
            if not self.isInterruptionRequested():self.failed.emit({'code':getattr(error,'code','E_INTERNAL'),
                'identity':None if client is None else client.last_command,
                'reason':str(error) if isinstance(error, TraderError) else None,
                'previous_receipt':previous_receipt})

    def _submit_arm(self, client):
        try:
            return client.command(*self.args)
        except TraderError as error:
            if error.code not in CONNECTION_ERRORS or client.last_command is None or self.isInterruptionRequested():
                raise
            receipt = client.command_status(client.last_command)
            # A timeout only permits reading the original receipt.
            self._arm_receipt_from_lookup = True
            return receipt

    def _can_reissue_arm(self, view):
        parameters = self.args[2]
        config, connection = view['configuration'], view['connection']
        return (self.account_key == view['account_key'] and self.boot == view['kernel_boot_id']
                and view['entry_authority'] == 'DISARMED' and not view['truncated']
                and config['rule_set_id'] == parameters['rule_set_id']
                and config['config_revision'] == parameters['config_revision']
                and {'policy_kind': 'AUTHORITY', 'policy_id': parameters['authority_policy_id']} in view['policies']
                and not config.get('strategy_stop_requested') and not config.get('stop_cancel_pending')
                and connection['broker_connected'] and connection['synchronized']
                and not any(connection[key] for key in ('fault', 'recovery_required', 'storage_failed', 'clock_fault')))


TABLES={
 'external_positions':(['合约','方向','柜台手数','自有手数','差额','今仓','昨仓','参考开仓价','可调整','核对异常'],
            ['contract_id','direction','native_quantity','owned_quantity','difference','today','historical','reference_price','eligible','review_error']),
 'markets':(['合约','名称','允许该品种新增','最新','买一','卖一','日高','日低','交易日','来源时间','新鲜','路径完整',
             '原始报价时间','应用 UTC 减源时间（毫秒）','报价时间状态','阻挡原因'],
            ['contract_id','name','enabled','last','bid1','ask1','day_high','day_low','trading_day','source_time_utc','fresh','path_complete',
             'source_time_raw','source_age_raw_ns','quote_time_reason','entry_reasons']),
 'orders':(['合约','子单','柜台单号','柜台日','开平','手数','已成','在途占量','价格','原生开平','发送','柜台','撤单'],
            ['contract_id','child_order_id','native_order_id','native_trading_day','effect','original_qty','filled_qty','potential_remaining','limit_price','native_offset','send_state','broker_state','cancel_status']),
 'trades':(['合约','成交身份','方向','开平','手数','价格','交易日'],['contract_id','trade_key','direction','effect','quantity','price','trading_day']),
 'protection':(['合约','周期','模式','质量','第一档','第二档','保本','最高有利变动','剩量','目标','管理权','今昨仓路由核对'],
               ['contract_id','position_cycle_id','mode','quality','phase1_status','phase2_status','break_even_status','max_favorable_move','remaining_quantity','target_remaining_quantity','management_state','close_route_review']),
 'blocks':(['原因','生效','合约'],['reason_code','active','contract_id']),
 'commands':(['命令','状态','错误','说明','回执版本'],['command_id','status','error_code','reason','receipt_revision'])}

DISPLAY={'LONG':'多','SHORT':'空','OPEN':'开仓','REDUCE':'减仓','NORMAL':'普通','PROTECTED':'利润保护',
    'CONTIGUOUS':'连续','DATA_GAP':'断流','UNKNOWN':'未知','NOT_TRIGGERED':'未触发','DONE':'完成','ACTIVE':'生效',
    'OFF':'关闭','WAITING_FILL':'等待成交','WAITING_ENTRY_TERMINAL':'等待开仓终态','SUPPRESSED_PROTECTED':'利润保护优先',
    'MANAGED':'原内核管理','TAKEN_OVER':'已交接','TAKEOVER_PENDING':'交接待确认','NONE':'无',
    'EXIT_REDUCTION_TERMINAL_UNFILLED':'平仓已终态，剩余目标待核对',
    'CLOSETODAY':'平今','CLOSEYESTERDAY':'平昨','CLOSE':'平仓',
    'STRATEGY':'策略','PROTECTION':'保护','CONVERGENCE':'自动收敛','MANUAL':'手动',
    'HOLDING':'持仓中','ENTRY_PENDING':'开仓待成','EXIT_PENDING':'平仓待成','CLOSED':'已平仓',
    'ALIVE':'挂单','ACKED':'已报','WORKING':'挂单中','PARTIALLY_FILLED':'部分成交',
    'FILLED':'全部成交','CANCELLED':'已撤单','REJECTED':'已拒绝','FINISHED':'已结束','NOT_SEEN':'等待回报',
    'NATIVE_SEEN':'柜台已见','CONFIRMED':'已确认','DEFINITELY_NOT_SENT':'确认未发单',
    'SEND_POSSIBLE':'请求发送中','CALL_RETURNED':'已调用接口',
    'SUCCEEDED':'成功','FAILED':'失败','READY':'就绪','RECONCILING':'核对中',
    'ARMED':'已启动','DISARMED':'未启动'}


def display_cell(key,value):
    if key=='source_age_raw_ns' and value is not None:
        return f'{int(value)/1_000_000:+.3f}'
    if key=='quote_time_reason':
        return QUOTE_TIME_MESSAGES.get(value, value or '—')
    if key=='close_route_review' and isinstance(value,dict):
        if value.get('status')=='BLOCKED':return '待核对 · '+value['error_code']
        if value.get('status')=='NO_OWNED_QUANTITY':return '无自有持仓'
        routes=' / '.join(('平今' if r['native_offset']=='CLOSETODAY' else '平仓')+' '+str(r['quantity'])+'手' for r in value.get('routes',[]))
        return f"今{value['owned_today']} / 昨{value['owned_historical']} · {routes or '已预占'}（核对预览）"
    if key=='entry_reasons' and isinstance(value,list):
        return ' / '.join(format_reason_text(reason) for reason in value) if value else '无新增原因'
    if key in {'reason','reason_code'} and isinstance(value,str):
        return DISPLAY[value] if value in DISPLAY else format_reason_text(value)
    if value is None:return '原周期' if key=='management_state' else '未知'
    if type(value) is bool:return '是' if value else '否'
    return DISPLAY.get(str(value),str(value))


class WorkbenchWindow(StatusWindow):
    def __init__(self,pipe_name,*,store=None,account_id=None,client_factory=StatusClient,paired_launcher=None,
                 dsv_config=None, use_desktop_dsv=False, autostart=False, notification=None, runtime_view=False,
                 update_root=None, update_restart=None, runtime_root=None, runtime_account=None):
        super().__init__(pipe_name,client_factory=client_factory)
        self._runtime_mode = runtime_view
        self._runtime_root = Path(runtime_root).resolve() if runtime_root is not None else None
        self._runtime_account = runtime_account
        self._diagnostic_sink = None
        self._diagnostic_sink_holder = {'sink': None}
        self._diagnostic_account_pseudonym = None
        self._diagnostic_snapshot_signature = None
        self._diagnostic_snapshot_summary_ns = 0
        self._diagnostic_connection_signature = None
        self._diagnostic_previous_excepthook = None
        self._diagnostic_excepthook = None
        if self._runtime_root is not None:
            from online_trader.diagnostics import open_sink
            process_id = f'workbench-{os.getpid()}-' + uuid.uuid4().hex
            self._diagnostic_sink = open_sink(
                self._runtime_root / 'diagnostics',
                process_role='WORKBENCH',
                process_instance_id=process_id,
                stream_group='workbench',
            )
            self._diagnostic_sink_holder['sink'] = self._diagnostic_sink
            holder = self._diagnostic_sink_holder
            self.destroyed.connect(lambda *_args, _h=holder: (
                _h['sink'].finalize(clean_shutdown=False, snapshot_available=False, timeout=1.0)
                if _h.get('sink') is not None else None
            ))
            self._diagnostic_sink.record(
                'WORKBENCH_STARTED', component='LIFECYCLE', priority='HIGH',
                payload={'runtime_mode': bool(runtime_view)},
            )
            previous_excepthook = sys.excepthook
            self._diagnostic_previous_excepthook = previous_excepthook
            holder = self._diagnostic_sink_holder
            def diagnostic_excepthook(exception_type, exception, traceback):
                sink = holder.get('sink')
                if sink is not None:
                    sink.record(
                        'QT_UNHANDLED_EXCEPTION', component='LIFECYCLE',
                        severity='ERROR', priority='CRITICAL',
                        error_code='E_QT_UNHANDLED',
                        payload={'exception_type': exception_type.__name__},
                    )
                previous_excepthook(exception_type, exception, traceback)
            self._diagnostic_excepthook = diagnostic_excepthook
            sys.excepthook = diagnostic_excepthook
        self._runtime_shutdown_verified = False
        self._runtime_shutdown_in_progress = False
        self._update_restart_in_progress = False
        self._runtime_bootstrap = None
        self._runtime_client_store = None
        self._runtime_coordinator = None
        self._runtime_command_bus = None
        self._runtime_receipt_restored_for = None
        self._pending_parameter_command_ids = set()
        self._pending_hot_add_profile = None
        self._pending_parameter_fallback = {}
        self._pending_focus_contracts = ()
        self.store=store;self.selected_account=store.get(account_id) if store is not None and account_id is not None else None
        self.paired_launcher=paired_launcher
        self.paired_account_key=None
        if paired_launcher is not None:
            self.paired_account_key=paired_launcher.review()['account_key']
        self.view=None;self.receipt_identity=None;self._boot=None
        self.snapshot_published_ns = None
        self._publication_after_ns = 0
        self.connection_recovery_source = None
        self._recovery_launch = None
        self._connection_interrupted_ns = 0
        self._connection_retry_deadline_ns = 0
        self._connection_retry_pending = False
        self._connection_retry_timed_out = False
        self._owner_lifecycle = None
        self._owner_crash_notified_boot = None
        self._finished_run = None
        self._startup_in_progress = False
        self._guard_launch = autostart
        self._deferred_request=None;self._control_notice=None
        self._close_positions_result = None
        self.home_feedback = ''
        self.home_issue = ''
        self._account_login = self._read_account_login()
        preferences = workbench_preferences()
        dingtalk = WorkbenchDingTalk(QSettings('OnlineTrader', 'Workbench'), log=self._notification_log,
                                   initial_config=notification)
        self._sounds = WorkbenchSounds(preferences, dingtalk=dingtalk)
        self._tray = WorkbenchTray(self, preferences)
        self.setWindowTitle('OnlineTrader · 交易工作台');self.resize(1360,820)
        self.tables={}
        for name,(headers,_) in TABLES.items():
            table=QTableWidget(0,len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectRows)
            self.tables[name]=table
        self.connection_detail=QLabel('尚未连接 · 等待柜台同步')
        self.filter_input=QLineEdit()
        self.filter_input.setPlaceholderText('搜索合约、名称或月份')
        self.filter_input.textChanged.connect(self.filter_markets)
        self.accounts_button=QPushButton('账户管理')
        self.attach_button=QPushButton('查看所选账户')
        self.accounts_button.clicked.connect(self.open_accounts)
        self.attach_button.clicked.connect(self.attach_selected)
        self.data_button=QPushButton('历史与交易日历')
        self.data_button.clicked.connect(self.open_data)
        self.session_button=QPushButton('连接 SimNow 模拟盘' if paired_launcher else '启动模拟策略')
        self.session_button.clicked.connect(self.open_session)
        self._session_dialog=None
        self._strategy_starter=None
        self._scheduled_start=None
        self._schedule_clock = None
        self.command_buttons={}
        for name,text,callback in (
            ('arm','启动策略 (F5)',self.start_strategy),('close','平选定自有仓',self.close_selected),
            ('flatten','平仓后停开',self.flatten),('cancel','撤所选自有单',self.cancel_selected),
            ('cancel_entries','撤自有开仓单',self.cancel_entries),('buy','手动开多',lambda:self.manual_open('LONG')),
            ('sell','手动开空',lambda:self.manual_open('SHORT')),('reconcile','请求完整核对',self.reconcile),
            ('receipt','查询上次回执',self.receipt),('adopt','核对并调整所选持仓',self.adopt_selected),
            ('lot_review','核对等量换仓',lambda:self.adopt_selected(lot_review=True)),
            ('cancel_account','账户全部撤单',self.cancel_account),('historical_orders','回补历史原单',self.import_historical_orders),
            ('order_archive','查询原单留档',self.query_order_archive),
            ('symbol_enable','启用品种新增',lambda:self.set_symbol_entries(True)),
            ('symbol_pause','停用品种新增',lambda:self.set_symbol_entries(False)),
        ):
            button=QPushButton(text)
            button.clicked.connect(callback)
            self.command_buttons[name]=button
        if paired_launcher:
            self.setWindowTitle('OnlineTrader · SimNow 交易工作台')
        self.pause_button.setText('停止策略 (F6)')
        if use_desktop_dsv and paired_launcher is None and dsv_config is None:
            saved = preferences.value('dsv_config_path', '', str)
            desktop = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
            if saved:
                dsv_config = Path(saved)
            elif desktop:
                candidate = Path(desktop) / 'trading' / 'config.json'
                if candidate.parent.is_dir():
                    dsv_config = candidate
        self.update_controller = None
        if update_root is not None and update_restart is not None:
            from online_trader.desktop.update_ui import ManagedUpdateController
            self.update_controller = ManagedUpdateController(self, update_root, update_restart)
        self.dashboard=WorkbenchLayout(self, dsv_config=dsv_config, require_reviewed_profile=use_desktop_dsv)
        self._tray.restore_geometry()
        self._tray.sync_icon()
        self.timer=QTimer(self);self.timer.setInterval(2500);self.timer.timeout.connect(self.refresh);self.timer.start()
        self.monitor_worker = None
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(1000)
        self.monitor_timer.timeout.connect(self.refresh_monitor)
        if not runtime_view:
            self.monitor_timer.start()
        self.connection_retry_timer = QTimer(self)
        self.connection_retry_timer.setInterval(1000)
        self.connection_retry_timer.timeout.connect(self._update_connection_recovery)
        self.monitor_diagnostics = None
        self._diagnostic_instance = uuid.uuid4().hex[:12]
        self._diagnostic_write_failed = False
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(30_000)
        self.schedule_timer.timeout.connect(self.check_timed_start)
        if self._guard_launch and self.store is not None:
            QTimer.singleShot(0, self.check_timed_start)
        self._enable_commands(False)
        if runtime_view and pipe_name:
            self._attach_runtime_pipe(pipe_name)

    def _attach_runtime_pipe(self, pipe_name):
        from online_trader.desktop.runtime_client import (
            ClientStateStore, CommandBus, RuntimeClientCoordinator)
        self._detach_runtime_client()
        self.pipe_name = pipe_name
        self._runtime_shutdown_verified = False
        self._owner_lifecycle = None
        self._owner_crash_notified_boot = None
        client_store = ClientStateStore(self)
        coordinator = RuntimeClientCoordinator(pipe_name, client_store, self)
        command_bus = CommandBus(coordinator, client_store, self)
        command_bus.migrate_legacy_pending()
        client_store.runtime_changed.connect(self._runtime_client_state_changed)
        client_store.connection_changed.connect(self._runtime_client_connection_changed)
        coordinator.publication_observed.connect(self._runtime_observed)
        coordinator.lifecycle_changed.connect(self._runtime_lifecycle_changed)
        coordinator.query_result.connect(self._runtime_query_result)
        coordinator.query_failed.connect(self._runtime_query_failed)
        command_bus.command_changed.connect(self._runtime_command_changed)
        command_bus.feedback.connect(self.show_home_feedback)
        self._runtime_client_store = client_store
        self._runtime_coordinator = coordinator
        self._runtime_command_bus = command_bus
        # The first generic disable pass ran before the Runtime command
        # path existed and therefore preserved the legacy "start without
        # a view" affordance.  Re-evaluate with the fail-closed Runtime
        # store before any subscription baseline can arrive.
        self._enable_commands(False)
        coordinator.start()

    def _detach_runtime_client(self):
        self._pending_parameter_command_ids.clear()
        self._pending_hot_add_profile = None
        self._pending_parameter_fallback.clear()
        if self._runtime_coordinator is not None:
            self._runtime_coordinator.close()
            self._runtime_coordinator.deleteLater()
        self._runtime_coordinator = None
        if self._runtime_command_bus is not None:
            self._runtime_command_bus.deleteLater()
        self._runtime_command_bus = None
        if self._runtime_client_store is not None:
            self._runtime_client_store.deleteLater()
        self._runtime_client_store = None

    def _resolve_runtime_root(self):
        if self._runtime_root is not None:
            return self._runtime_root
        run_dir = (self.view or {}).get('configuration', {}).get('run_directory')
        if isinstance(run_dir, str) and run_dir:
            candidate = Path(run_dir).resolve()
            if (candidate.parent.parent.name == 'desktop-starts'
                    and (candidate.parent.parent.parent / 'runtime.json').is_file()):
                return candidate.parent.parent.parent
        return None

    def _portable_store_and_account(self):
        root = self._resolve_runtime_root()
        if root is None:
            return None, None
        from online_trader.runtime.portable_config import PortableAccountStore
        store = PortableAccountStore(root, self._runtime_account)
        state = store.read()
        account_id = state['selected_id']
        if self.view is not None:
            account_key = self.view.get('account_key')
            matched = next((row['account_id'] for row in state['accounts']
                            if row['account_key'] == account_key), None)
            if matched is not None:
                account_id = matched
        return store, account_id

    def _read_account_login(self):
        account = self.selected_account
        try:
            if account is not None:
                value = self.store.credential_reference(account['account_id']).resolve()
                return value.get('simnow_username') or value['username']
            packet = getattr(self.paired_launcher, 'packet', None)
            if packet is not None:
                from online_trader.gateway.simnow import account_fingerprint, credentials
                username = credentials(packet / 'simnow-credentials.json')['simnow_username']
                if self.paired_account_key == 'SIM:SIMNOW:' + account_fingerprint(username):
                    return username
        except (TraderError, OSError):
            pass
        return account['masked_login'] if account is not None else None

    def display_account(self):
        key = self.view['account_key'] if self.view else self.paired_account_key
        display = self.view.get('account_display') if self.view else None
        if isinstance(display, Mapping):
            provider = {'TQ_SIMNOW': 'SimNow 模拟', 'TQKQ': '快期模拟'}.get(
                display.get('provider'), display.get('provider'))
            masked = display.get('masked_login')
            if provider and masked:
                return provider, masked
        account = self.selected_account
        if account and key in (None, account['account_key']):
            environment = account['environment']
            return {'ONLINE_SIM': '模拟账户', 'LIVE': '正常账户'}.get(environment, '账户类型未知'), self._account_login or account['masked_login']
        if key:
            account_type = '模拟账户' if key.startswith('SIM:') else '正常账户' if key.startswith('LIVE:') else '账户类型未知'
            login = self._account_login if key == self.paired_account_key else None
            return account_type, login or '未提供'
        if self._runtime_mode and self.view is None:
            return '未连接', '未同步'
        return '', '未选择'

    def show_home_feedback(self, message):
        self.home_feedback = message
        self.dashboard.render_home()

    def _enable_commands(self,enabled):
        for button in self.command_buttons.values():button.setEnabled(enabled)
        runtime_store = self._runtime_client_store
        lifecycle_state = (self._owner_lifecycle or {}).get('state')
        lifecycle_dead = lifecycle_state in {'OWNER_CRASHED', 'OWNER_STOPPED'}
        runtime_recheck_ok = (
            not lifecycle_dead
            and (
                runtime_store is None
                or runtime_store.connection_state.value in {'READY', 'STALE', 'SYNCHRONIZING'}
            )
        )
        owner_controls = (
            not self._closing and not lifecycle_dead and self._worker is None and self.view is not None
            and (self._runtime_command_bus is None or enabled or runtime_recheck_ok)
        )
        for name in ('close', 'flatten', 'cancel', 'cancel_entries', 'reconcile'):
            self.command_buttons[name].setEnabled(owner_controls)
        self.command_buttons['arm'].setEnabled(not self._startup_in_progress and ((enabled and self.view['entry_authority']!='ARMED'
            and not self.view['configuration'].get('stop_cancel_pending')
            and not self.view['configuration'].get('strategy_stop_requested')) or
            (self.view is None and not self._closing and self._runtime_command_bus is None)))
        self.pause_button.setEnabled(self._scheduled_start is not None or (owner_controls and (
            self.view['entry_authority']=='ARMED' or ('cancel_on_stop' in self.view['configuration']
            and not self.view['configuration'].get('strategy_stop_requested')))))
        self.dashboard.update_manual_controls(enabled, owner_controls=owner_controls)
        manual_open = bool(enabled and self.view is not None
                           and self.view['entry_authority'] == 'ARMED'
                           and self.view['configuration'].get('manual_workbench'))
        for name in ('buy', 'sell'):
            self.command_buttons[name].setEnabled(manual_open)
        receipt_enabled = (not self._closing and self.receipt_identity is not None
                           and (enabled or self._runtime_command_bus is not None))
        self.command_buttons['receipt'].setEnabled(receipt_enabled)
        if self._runtime_command_bus is not None and self.view is not None:
            operation_by_button = {
                'arm': 'ARM_ENTRIES',
                'close': 'MANUAL_CLOSE',
                'flatten': 'FLATTEN_THEN_STOP',
                'cancel': 'CANCEL_SELECTED',
                'cancel_entries': 'CANCEL_OWNED',
                'buy': 'MANUAL_OPEN',
                'sell': 'MANUAL_OPEN',
                'reconcile': 'REQUEST_RECONCILIATION',
                'adopt': 'ADOPT_EXTERNAL_POSITION',
                'lot_review': 'ADOPT_EXTERNAL_POSITION',
                'cancel_account': 'CANCEL_NATIVE_ACCOUNT',
                'historical_orders': 'REQUEST_RECONCILIATION',
                'symbol_enable': 'SET_ENTRY_POLICY',
                'symbol_pause': 'SET_ENTRY_POLICY',
            }
            for name, operation in operation_by_button.items():
                allowed, _ = self._runtime_client_store.capability(operation)
                self.command_buttons[name].setEnabled(
                    self.command_buttons[name].isEnabled() and allowed)
            pause_operation = ('STOP_DSV_STRATEGY' if 'cancel_on_stop' in self.view['configuration']
                               else 'PAUSE_NEW_ENTRIES')
            pause_allowed, _ = self._runtime_client_store.capability(pause_operation)
            self.pause_button.setEnabled(self.pause_button.isEnabled() and pause_allowed)

    def _start_job(self,action,args=None):
        if self._closing:return
        if self._runtime_command_bus is not None:
            if action == 'refresh':
                if self._runtime_client_store.connection_state.value != 'READY':
                    self.show_home_feedback('正在等待 Runtime 状态流重新同步。')
            elif action == 'command':
                self._start_runtime_submit(*args)
            elif action == 'status':
                self._start_runtime_receipt(args)
            else:
                self.show_home_feedback('此操作尚未提供独立运行时入口。')
            return
        if self._runtime_mode:
            if action == 'refresh':
                self.show_home_feedback('当前尚未连接到 Runtime，请切换账号或重新启动连接。')
            else:
                self.show_home_feedback('当前尚未连接到 Runtime，无法提交控制操作。')
            return
        if (action == 'command' and self.published_snapshot_expired()
                and args[0] not in OWNER_RECHECK_OPERATIONS):
            self._stale = True
            self._enable_commands(False)
            self.show_home_feedback('上次确认的快照已过期，请等待更新。')
            return
        if self._worker is not None:
            if action!='refresh':
                if self._worker.action=='refresh' and self._deferred_request is None and self.view is not None:
                    self._deferred_request=(self._command_binding(),action,args)
                    self.notice_label.setText('请求已接收，等待当前刷新完成。')
                else:self.notice_label.setText('当前请求仍在处理，请等待回执。')
                self.show_home_feedback(self.notice_label.text())
            return
        if self.pipe_name is None:
            self.notice_label.setText('请在账户管理中新增并选择账户。')
            return
        if action!='refresh':
            self.refresh_button.setEnabled(False);self._enable_commands(False)
        account=self.paired_account_key or (self.selected_account['account_key'] if self.selected_account is not None else self.view['account_key'] if self.view is not None else None)
        direct_refresh = action == 'refresh' and self._connection_retry_pending
        if direct_refresh:
            self._connection_retry_pending = False
        boot = args[2][1] if action == 'recover' else None if action == 'refresh' else self._boot
        worker=_WorkbenchRequest(self.pipe_name,self._factory,action,args,account,boot,
                                 publication_after_ns=self._publication_after_ns,direct_refresh=direct_refresh)
        self._worker=worker;worker.result.connect(self._job_result);worker.failed.connect(self._job_failed);worker.finished.connect(self._finished);worker.start()

    def refresh(self):self._start_job('refresh')

    def _runtime_updated(self, result):
        incoming = result.get('value', {}).get('view', {})
        if self._boot is not None and incoming.get('kernel_boot_id') != self._boot:
            self.show_home_feedback('Runtime 已重启，正在重新核对当前状态。')
        self._job_result(result)
        self._sync_runtime_pending()
        self.dashboard.ensure_account_history()
        self._enable_commands(not self._closing and not self._stale and not self.view['truncated'])

    def _runtime_client_state_changed(self, state):
        if self._closing:
            return
        stamp = self._runtime_client_store.published_ns
        value = dict(state) | {
            'published_ns': stamp,
            'metadata': {
                'kernel_boot_id': state['view']['kernel_boot_id'],
                'received_at_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            },
        }
        if self._diagnostic_sink is not None:
            view = state['view']
            boot_id = view.get('kernel_boot_id')
            if self._diagnostic_account_pseudonym is None and self._runtime_root is not None:
                account_key = view.get('account_key')
                if isinstance(account_key, str) and account_key:
                    try:
                        from online_trader.diagnostics import account_pseudonym
                        self._diagnostic_account_pseudonym = account_pseudonym(
                            self._runtime_root, account_key)
                    except Exception:
                        pass
            configuration = view.get('configuration') or {}
            signature = (
                boot_id,
                str(configuration.get('config_revision') or '0'),
                view.get('entry_authority'),
                bool(view.get('truncated')),
                len(view.get('cycles') or ()),
                len(view.get('orders') or ()),
            )
            now_ns = time.monotonic_ns()
            changed = signature != self._diagnostic_snapshot_signature
            if changed or now_ns - self._diagnostic_snapshot_summary_ns >= 30_000_000_000:
                self._diagnostic_sink.record(
                    'WORKBENCH_SNAPSHOT_CHANGED' if changed else 'WORKBENCH_SNAPSHOT_SUMMARY',
                    component='PROJECTION', priority='HIGH' if changed else 'LOW',
                    runtime_boot_id=boot_id,
                    account_pseudonym=self._diagnostic_account_pseudonym,
                    configuration_revision=signature[1],
                    correlation={
                        'snapshot_id': str(view.get('snapshot_id') or view.get('checkpoint_event_seq') or '0'),
                    },
                    payload={
                        'entry_authority': signature[2],
                        'truncated': signature[3],
                        'cycle_count': signature[4],
                        'order_count': signature[5],
                    },
                )
                self._diagnostic_snapshot_signature = signature
                self._diagnostic_snapshot_summary_ns = now_ns
        self._runtime_updated({'action': 'refresh', 'value': value})

    def _runtime_client_connection_changed(self, state, reason):
        signature = (str(state), str(reason or ''))
        if self._diagnostic_sink is not None and signature != self._diagnostic_connection_signature:
            self._diagnostic_sink.record(
                'WORKBENCH_CONNECTION_CHANGED', component='IPC',
                severity='INFO' if state == 'READY' else 'WARNING', priority='HIGH',
                runtime_boot_id=self._boot,
                account_pseudonym=self._diagnostic_account_pseudonym,
                payload={'connection_state': str(state), 'reason': str(reason or '')},
            )
            self._diagnostic_connection_signature = signature
        if self._closing or state == 'READY':
            if state == 'READY':
                self._owner_lifecycle = None
            return
        self._runtime_unavailable(reason or state)

    def _runtime_command_changed(self, pending):
        if self._closing:
            return
        if self._diagnostic_sink is not None:
            receipt = dict(pending.receipt) if pending.receipt is not None else {}
            self._diagnostic_sink.record(
                'WORKBENCH_COMMAND_CHANGED', component='COMMAND', priority='HIGH',
                runtime_boot_id=self._boot,
                account_pseudonym=self._diagnostic_account_pseudonym,
                correlation={'command_id': pending.command_id},
                payload={
                    'operation': pending.operation,
                    'status': pending.status.value,
                    'reason': pending.reason,
                    'receipt_status': receipt.get('status'),
                },
            )
        if pending.receipt is not None:
            receipt = dict(pending.receipt)
            self.receipt_identity = {key: receipt[key] for key in ('command_id', 'payload_hash') if key in receipt}
            self._publication_after_ns = time.monotonic_ns()
            self._show_receipt(receipt)
            if pending.status.value in {'COMPLETED', 'REJECTED'}:
                self.receipt_identity = None
        elif pending.status.value != 'LOCAL_NOT_ENQUEUED':
            self.receipt_identity = {'command_id': pending.command_id}
        is_param_cmd = (pending.command_id in self._pending_parameter_command_ids
                        or pending.operation == 'SAVE_DSV_PARAMETERS')
        if is_param_cmd and pending.status.value in {'COMPLETED', 'REJECTED'}:
            self._pending_parameter_command_ids.discard(pending.command_id)
            fallback = self._pending_parameter_fallback.pop(pending.command_id, None)
            if pending.status.value == 'COMPLETED':
                if fallback is not None:
                    self.dashboard.set_parameter_feedback('新合约已加入运行中策略，正在监控面板同步行情。')
                else:
                    self.dashboard.set_parameter_feedback('参数已保存并立即生效。')
            else:
                if fallback is not None:
                    self._pending_focus_contracts = ()
                reason = (pending.receipt or {}).get('reason') or pending.reason or '保存未通过校验'
                self.dashboard.set_parameter_feedback(f'参数保存未生效：{reason}', error=True)
                store, _ = self._portable_store_and_account() if fallback is not None else (None, None)
                if store is not None:
                    box = QMessageBox(self)
                    box.setIcon(QMessageBox.Icon.Warning)
                    box.setWindowTitle('热新增合约未生效')
                    box.setText(
                        f'运行时未能直接加入新合约：{reason}\n\n'
                        '是否先保存到本地启动配置（OnlineTrader-overrides.json），待下次安全重启后生效？'
                    )
                    save_btn = box.addButton('保存为下次启动配置', QMessageBox.ButtonRole.AcceptRole)
                    box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
                    box.exec()
                    if box.clickedButton() is save_btn:
                        self._persist_next_boot_profile(store, fallback)
                elif self.dashboard.tabs.currentWidget() is self.dashboard.parameter_tab:
                    QMessageBox.warning(self, '保存参数未生效', f'后台拒绝了本次参数保存：\n{reason}')
        self._sync_runtime_pending()
        self._enable_commands(not self._closing and not self._stale and self.view is not None
                              and not self.view['truncated'])

    def _sync_runtime_pending(self):
        bus = self._runtime_command_bus
        if bus is None or self.view is None:
            return
        rows = sorted((item for item in bus.store.pending.values()
                       if item.account_key == self.view['account_key']),
                      key=lambda item: item.command_id)
        if rows:
            current = next((item for item in rows if self.receipt_identity is not None
                            and item.command_id == self.receipt_identity.get('command_id')), rows[0])
            self.receipt_identity = {'command_id': current.command_id}
        elif self.receipt_identity is not None and 'payload_hash' not in self.receipt_identity:
            self.receipt_identity = None

    def enqueue_runtime_history(self, request):
        coordinator = self._runtime_coordinator
        if coordinator is None or self._closing:
            raise TraderError('E_RUNTIME_CONNECTION', 'Runtime history query is unavailable')
        coordinator.queries.enqueue(request)

    def _runtime_query_result(self, response):
        if not self._closing and response.get('kind') == 'history':
            self.dashboard.apply_account_history(response)

    def _runtime_query_failed(self, response):
        if not self._closing and response.get('kind') == 'history':
            self.dashboard.fail_account_history(response)

    def _runtime_observed(self, stamp):
        if self._closing or self.view is None:
            return
        self.snapshot_published_ns = stamp
        if self._stale and not self.published_snapshot_expired():
            self._stale = False
            if self._diagnostic_sink is not None:
                self._diagnostic_sink.record(
                    'WORKBENCH_SNAPSHOT_RECOVERED', component='PROJECTION', priority='HIGH',
                    runtime_boot_id=self._boot,
                    account_pseudonym=self._diagnostic_account_pseudonym,
                )
            self.render_view()
            self._enable_commands(not self.view['truncated'])

    def _runtime_unavailable(self, code):
        if self._closing:
            return
        # A subscription reconnect must not make a still-current publication
        # flash between READY and STALE.  The subscription retries continuously; if
        # the outage persists, a later failure will cross MAX_AGE_NS and take
        # the existing fail-closed path below.
        if self.view is not None and not self.published_snapshot_expired():
            return
        became_stale = not self._stale
        self._stale = True
        if self.connection_recovery_source == 'broker':
            self._observe_connection_recovery(None)
        if became_stale and self._diagnostic_sink is not None:
            self._diagnostic_sink.record(
                'WORKBENCH_SNAPSHOT_EXPIRED', component='PROJECTION',
                severity='WARNING', priority='HIGH', reason_code='SNAPSHOT_EXPIRED',
                runtime_boot_id=self._boot,
                account_pseudonym=self._diagnostic_account_pseudonym,
                payload={'snapshot_age_seconds': self.published_snapshot_age()},
            )
        self._enable_commands(False)
        lifecycle_state = (self._owner_lifecycle or {}).get('state')
        if lifecycle_state not in {'OWNER_CRASHED', 'OWNER_STOPPED'}:
            self.show_home_feedback(f'工作台未收到最新投影，正在确认后台 Runtime 状态 · {code}')
        # A stale client projection is a Workbench/view condition, not a market
        # quote verdict.  Repaint immediately so the last owner quote cannot
        # remain labelled as current or be mistaken for an owner-side entry
        # refusal while the subscription reconnects.
        if self.view is not None:
            self.render_view()

    def _runtime_lifecycle_changed(self, lifecycle):
        if self._closing or not isinstance(lifecycle, dict):
            return
        state = lifecycle.get('state')
        if state == 'OWNER_REPLACED':
            pipe = lifecycle.get('pipe')
            if isinstance(pipe, str) and pipe:
                self._owner_lifecycle = None
                self._stale = True
                self._observe_connection_recovery(None)
                self.show_home_feedback('检测到新启动的 Runtime 实例，正在连接...')
                QTimer.singleShot(0, lambda p=pipe: self._attach_runtime_pipe(p))
            return
        if state == 'HEALTHY':
            self._owner_lifecycle = None
            return
        self._owner_lifecycle = dict(lifecycle)
        if state in {'OWNER_CRASHED', 'OWNER_STOPPED'}:
            self._observe_connection_recovery(None)
            self.connection_retry_timer.stop()
            self._stale = True
            self._enable_commands(False)
            if state == 'OWNER_CRASHED':
                code = str(lifecycle.get('error_code') or 'E_RUNTIME_OWNER_EXITED')
                boot_key = str(lifecycle.get('runtime_boot_id') or code)
                self._tray.set_status('error')
                if boot_key != self._owner_crash_notified_boot:
                    self._owner_crash_notified_boot = boot_key
                    self._sounds.observe_request_error()
                    if self._tray.icon is not None:
                        self._tray.icon.showMessage(
                            'OnlineTrader · Runtime 异常退出',
                            f'后台交易服务已异常退出（{code}），请立即核对期货账户实际持仓与委托。',
                        )
                self.show_home_feedback(f'Runtime 后台服务已异常退出 · {code}')
            else:
                self._tray.set_status('idle')
                self.show_home_feedback('Runtime 后台服务已停止。')
        elif state in {'OWNER_UNRESPONSIVE', 'VIEW_RECONNECTING'}:
            if self.connection_recovery_source == 'broker':
                self._observe_connection_recovery(None)
            self._stale = True
            self._enable_commands(False)
        if self.view is not None:
            self.render_view()

    def _start_runtime_submit(self, operation, scope, parameters):
        hot_add_profile = (
            self._pending_hot_add_profile if operation == 'SAVE_DSV_PARAMETERS' else None
        )
        if operation == 'SAVE_DSV_PARAMETERS':
            self._pending_hot_add_profile = None
        stale_or_truncated = (
            self.view is None or self._stale or self.published_snapshot_expired() or self.view['truncated']
        )
        if self.view is None or (stale_or_truncated and operation not in OWNER_RECHECK_OPERATIONS):
            if hot_add_profile is not None:
                self._pending_focus_contracts = ()
            self.show_home_feedback('请先取得当前完整 Runtime 状态，再提交命令。')
            if self._diagnostic_sink is not None:
                self._diagnostic_sink.record(
                    'WORKBENCH_COMMAND_BLOCKED', component='COMMAND', priority='HIGH',
                    reason_code='SNAPSHOT_UNAVAILABLE', runtime_boot_id=self._boot,
                    account_pseudonym=self._diagnostic_account_pseudonym,
                    payload={'operation': operation},
                )
            return
        bus = self._runtime_command_bus
        if bus is None:
            if hot_add_profile is not None:
                self._pending_focus_contracts = ()
            self.show_home_feedback('Runtime CommandBus 尚未就绪；命令未发送。')
            if self._diagnostic_sink is not None:
                self._diagnostic_sink.record(
                    'WORKBENCH_COMMAND_BLOCKED', component='COMMAND', priority='HIGH',
                    reason_code='COMMAND_BUS_UNAVAILABLE', runtime_boot_id=self._boot,
                    account_pseudonym=self._diagnostic_account_pseudonym,
                    payload={'operation': operation},
                )
            return
        try:
            command_id = bus.submit(operation, scope, parameters)
        except TraderError as error:
            if hot_add_profile is not None:
                self._pending_focus_contracts = ()
            self.show_home_feedback(f'Runtime 命令未提交 · {error.code} · {error}')
            if operation == 'SAVE_DSV_PARAMETERS':
                self.dashboard.set_parameter_feedback(f'参数未提交 · {error.code} · {error}', error=True)
            if self._diagnostic_sink is not None:
                self._diagnostic_sink.record(
                    'WORKBENCH_COMMAND_SUBMIT_FAILED', component='COMMAND',
                    severity='WARNING', priority='HIGH',
                    error_code=error.code if error.code.startswith('E_') else 'E_COMMAND_SUBMIT',
                    runtime_boot_id=self._boot,
                    account_pseudonym=self._diagnostic_account_pseudonym,
                    payload={'operation': operation},
                )
            return
        if self._diagnostic_sink is not None:
            self._diagnostic_sink.record(
                'WORKBENCH_COMMAND_SUBMITTED', component='COMMAND', priority='HIGH',
                runtime_boot_id=self._boot,
                account_pseudonym=self._diagnostic_account_pseudonym,
                correlation={'command_id': command_id},
                payload={'operation': operation, 'scope_type': scope.get('scope_type')},
            )
        if operation == 'SAVE_DSV_PARAMETERS':
            self._pending_parameter_command_ids.add(command_id)
            if hot_add_profile is not None:
                self._pending_parameter_fallback[command_id] = hot_add_profile
            self.dashboard.set_parameter_feedback('正在提交参数到运行中的 Runtime…')
        self.receipt_identity = {'command_id': command_id}
        self.show_home_feedback(f'正在提交 Runtime 命令 · {command_id}')
        self._enable_commands(False)

    def _start_runtime_receipt(self, identity):
        command_id = identity.get('command_id') if isinstance(identity, dict) else None
        if not command_id:
            self.show_home_feedback('没有可查询的 Runtime 命令。')
            return
        bus = self._runtime_command_bus
        if bus is None:
            self.show_home_feedback('Runtime CommandBus 尚未就绪；回执未查询。')
            return
        try:
            bus.query(command_id)
        except TraderError as error:
            self.show_home_feedback(f'Runtime 回执未查询 · {error.code} · {error}')
    def force_recover_workbench(self):
        if self.paired_launcher is not None or self.store is None or self.selected_account is None:
            self._recovery_feedback('请选择普通工作台的模拟账号。')
            return
        if self._closing or self._startup_in_progress or self._worker is not None or self._deferred_request is not None:
            self._recovery_feedback('当前请求仍在处理，请等到回执后再恢复。')
            return
        if self.home_issue and self.receipt_identity is not None:
            self._recovery_feedback('存在结果待核对的命令，请先查询上次回执。')
            return
        if self.view is not None:
            config = self.view['configuration']
            identity = (self.view['account_key'], self._boot, config['rule_set_id'], config['config_revision'])
            run = self.dashboard.latest_run()
        elif self._finished_run is not None:
            account, boot, run = self._finished_run
            identity = (account, boot, None, None)
        else:
            self._connection_retry_pending = True
            self.refresh()
            self._recovery_feedback('正在直连查询原执行器，请在取得运行状态后再恢复。')
            return
        if run is None:
            self._recovery_feedback('找不到原执行器的运行记录，请在日志与诊断核对。')
            return
        self.cancel_timed_start()
        self._recovery_feedback('正在直查原执行器，核对空仓、委托和账本。')
        self._start_job('recover', (self.store, self.selected_account['account_id'], identity, run))

    def _recovery_feedback(self, message):
        self.dashboard.recovery_status.setText(message)
        self.dashboard.add_activity('强制恢复工作台 · ' + message)

    def _recovery_sync_timeout(self, launch):
        if self._recovery_launch == launch and not self._closing:
            self._recovery_launch = None
            self._startup_in_progress = False
            self._recovery_feedback('恢复进程启动后尚未取得新执行器的同步回执，请查看运行日志。')
            self._enable_commands(False)

    def _observe_connection_recovery(self, source):
        if source is None:
            self.connection_retry_timer.stop()
            self.connection_recovery_source = None
            self._connection_retry_pending = False
            self._connection_retry_timed_out = False
        else:
            if self.connection_recovery_source is None:
                now = time.monotonic_ns()
                self._connection_interrupted_ns = now
                self._connection_retry_deadline_ns = now + RECONNECT_TIMEOUT_NS
                self.connection_retry_timer.start()
            self.connection_recovery_source = source
        self._update_connection_recovery()

    def reconnect(self):
        if self._closing or self.connection_recovery_source is None:
            return
        self._restart_connection_query(time.monotonic_ns(), timed_out=False)
        self._update_connection_recovery()

    def _restart_connection_query(self, now, *, timed_out):
        self._connection_retry_timed_out = timed_out
        self._connection_retry_deadline_ns = now + RECONNECT_TIMEOUT_NS
        self._connection_retry_pending = True
        self.refresh()

    def _update_connection_recovery(self):
        lifecycle_state = (self._owner_lifecycle or {}).get('state')
        if (
            self._closing
            or self.connection_recovery_source is None
            or lifecycle_state in {'OWNER_CRASHED', 'OWNER_STOPPED'}
            or (self.pipe_name is not None and self._stale)
        ):
            self.dashboard.show_connection_recovery('')
            return
        now = time.monotonic_ns()
        if now >= self._connection_retry_deadline_ns:
            self._restart_connection_query(now, timed_out=True)
        remaining = (self._connection_retry_deadline_ns - now + 999_999_999) // 1_000_000_000
        elapsed = (now - self._connection_interrupted_ns) // 1_000_000_000
        source = ('等待执行器响应' if self.connection_recovery_source == 'owner'
                  else '等待柜台恢复同步')
        action = '上次等待超时 · ' if self._connection_retry_timed_out else ''
        self.dashboard.show_connection_recovery(
            f'{source} {elapsed} 秒 · {action}{remaining} 秒后重新发起连接查询')

    def published_snapshot_expired(self):
        return (self.snapshot_published_ns is not None
                and time.monotonic_ns() - self.snapshot_published_ns >= MAX_AGE_NS)

    def published_snapshot_age(self):
        if self.snapshot_published_ns is not None:
            return (time.monotonic_ns() - self.snapshot_published_ns) // 1_000_000_000

    def snapshot_age_tier(self):
        if self.snapshot_published_ns is None:
            return 'UNKNOWN_BACKEND', None
        age_ns = max(0, time.monotonic_ns() - self.snapshot_published_ns)
        age_sec = age_ns // 1_000_000_000
        if age_ns < 2_000_000_000:
            return 'NORMAL', age_sec
        if age_ns < 5_000_000_000:
            return 'SLOW', age_sec
        if age_ns < 15_000_000_000:
            return 'STALE', age_sec
        return 'UNKNOWN_BACKEND', age_sec

    def refresh_monitor(self):
        view = self.view
        if (self._closing or self._stale or self.monitor_worker is not None
                or (self._worker is not None and self._worker.action != 'refresh')
                or not self.pipe_name or view is None or 'strategy_monitor' not in view['configuration']):
            return
        worker = _WorkbenchRequest(self.pipe_name, self._factory, 'monitor', None, view['account_key'], self._boot)
        worker.setParent(self)
        worker.result.connect(self._monitor_result)
        worker.failed.connect(self._monitor_failed)
        worker.finished.connect(self._monitor_finished)
        worker.finished.connect(worker.deleteLater)
        self.monitor_worker = worker
        worker.start()

    def _monitor_result(self, result):
        worker = self.sender()
        value = result['value']['monitor']
        if (self._closing or self._stale or worker is not self.monitor_worker or self.view is None
                or worker.pipe != self.pipe_name or worker.account_key != self.view['account_key']
                or worker.boot != self._boot):
            return
        if value is not None and int(value['checkpoint_event_seq']) < int(self.view['checkpoint_event_seq']):
            return
        if value is None:
            return
        if (value['account_key'], value['kernel_boot_id']) != (worker.account_key, worker.boot):
            return
        self.dashboard.render_strategy_monitor(value)
        metadata = result['value']['metadata']
        if value['diagnostics'] is not None:
            self.monitor_diagnostics = value['diagnostics'] | {
                'account_key': value['account_key'], 'kernel_boot_id': value['kernel_boot_id'],
                'request_started_monotonic_ns': metadata['request_started_monotonic_ns'],
                'reply_sent_at_utc': metadata['reply_sent_at_utc'],
                'received_at_utc': metadata['received_at_utc'],
                'received_monotonic_ns': metadata['received_monotonic_ns'],
                'qt_rendered_monotonic_ns': str(time.monotonic_ns()),
                'qt_rendered_at_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
            directory = self.dashboard.latest_run()
            if directory is not None and directory.is_dir():
                try:
                    path = directory / f'workbench-latency-{self._diagnostic_instance}.jsonl'
                    append_latency(path, self.monitor_diagnostics)
                except (OSError, TraderError):
                    if not self._diagnostic_write_failed:
                        self.dashboard.add_activity('行情诊断写入失败', category='error')
                    self._diagnostic_write_failed = True
        else:
            self.monitor_diagnostics = None

    def _monitor_failed(self, error):
        worker = self.sender()
        if (not self._closing and worker is self.monitor_worker and self.view is not None
                and worker.pipe == self.pipe_name and worker.boot == self._boot
                and worker.account_key == self.view['account_key'] and self.snapshot_published_ns is None):
            self.dashboard.render_strategy_monitor(None)

    def _monitor_finished(self):
        worker = self.sender()
        if worker is self.monitor_worker:
            self.monitor_worker = None
            if self._closing:
                QTimer.singleShot(0,self.close)

    def create_sound_settings(self):return self._sounds.settings_panel()

    def _quote_reference_received(self, rows):
        self.dashboard.reference_quotes_received(rows)

    def _quote_reference_failed(self, code):
        self.dashboard.reference_quotes_failed(code)

    def _quote_reference_finished(self):
        self.dashboard.reference_quotes_finished()
        if self._closing:
            QTimer.singleShot(0,self.close)

    def _notification_log(self, message):
        try:
            self.dashboard.add_activity(message)
            self.dashboard._schedule_log_update()
        except RuntimeError:
            return  # A pending network result may arrive after the workbench closes.

    def pause_entries(self):
        self.cancel_timed_start()
        if self.view is None:
            return
        scope={'scope_type':'ACCOUNT','scope_ids':[]}
        config=self.view['configuration']
        if 'cancel_on_stop' not in config:
            if self.view['entry_authority']=='ARMED':
                self.send('PAUSE_NEW_ENTRIES',scope,{'cancel_owned_entry_remainders':False})
            return
        if config.get('strategy_stop_requested'):
            return
        params={}
        review=config.get('native_cancel_review',{})
        if (not self._stale and not self.published_snapshot_expired() and config['cancel_on_stop']
                and review.get('eligible') and review.get('external_count')):
            identity=self._command_binding()
            contracts='、'.join(f'{code} {count} 笔' for code,count in sorted(review['by_contract'].items()))
            message=f"停止策略时，是否同时撤销当前 {review['external_count']} 笔外部委托？\n{contracts}\n选择否仍会停止策略，并按设置处理自有委托。"
            confirm=QMessageBox.question(self,'停止策略并撤单',message,QMessageBox.Yes | QMessageBox.No,QMessageBox.No)
            if self._stale or self.view is None or identity!=self._command_binding():
                self.notice_label.setText('停止请求未发送。请刷新并核对当前账户。')
                self.show_home_feedback(self.notice_label.text())
                return
            if confirm==QMessageBox.Yes:
                params['evidence_hash']=review['evidence_hash']
        self.send('STOP_DSV_STRATEGY',scope,params)

    def _show_receipt(self,receipt):
        notice=f"命令回执：{receipt['status']} · {receipt['error_code']} · {receipt['reason']}"
        if notice!=self._control_notice:
            error=receipt['status'] in {'FAILED','REJECTED','NEEDS_RECONCILIATION'}
            self.dashboard.add_activity(notice,category='error' if error else None)
        self._control_notice=notice
        self.notice_label.setText(notice)
        status = receipt['status']
        self.home_issue = '命令结果待核对，请查看交易详情中的命令回执' if status == 'NEEDS_RECONCILIATION' else ''
        self.home_feedback = f"命令回执 · {receipt['reason']}"

    def _job_result(self,result):
        if self._closing:return
        if result['action']=='refresh':
            value=result['value']
            if self._worker is not None and self._worker.direct_refresh:
                # A cached view captured before this reply must not undo a direct refresh.
                self._publication_after_ns = max(self._publication_after_ns, time.monotonic_ns())
            if self._stale and self.receipt_identity is None:
                self.home_feedback = ''
            if self._boot is not None and self._boot!=value['view']['kernel_boot_id']:
                if not self._runtime_mode:
                    self.receipt_identity=None
                self._control_notice=None
                self.home_feedback = self.home_issue = ''
                self.monitor_diagnostics = None
                self._diagnostic_write_failed = False
            selected=(self._selected_identities() if self._boot==value['view']['kernel_boot_id']
                      and self.view is not None and self.view['account_key']==value['view']['account_key'] else {})
            self._last_account_record=next(row for row in value['base']['records'] if row['record_type']=='ACCOUNT')
            super()._result({'action':'snapshot','value':value['base'],'metadata':value['metadata']})
            self._base_cycle_ids=[r['record_id'] for r in value['base']['records'] if r['record_type']=='CYCLE']
            self.view=value['view'];self._boot=self.view['kernel_boot_id']
            self.snapshot_published_ns = value.get('published_ns')
            self._stale = self.published_snapshot_expired()
            connection = self.view['connection']
            launch = self._recovery_launch
            if (launch is not None and self._boot != launch[0] and self.view['account_key'] == launch[1]
                    and not self._stale and not self.view['truncated']
                    and connection['broker_connected'] and connection['synchronized']):
                self._recovery_launch = None
                self._startup_in_progress = False
                state = ('开仓保持停用。' if self.view['entry_authority'] == 'DISARMED'
                         else '新执行器已同步，但开仓权限已被其他操作修改。')
                self._recovery_feedback('新执行器柜台同步已完成。' + state)
            if self._stale or self.view['truncated']:
                if self.connection_recovery_source is not None:
                    self._observe_connection_recovery('owner')
            elif not connection['broker_connected'] or not connection['synchronized']:
                self._observe_connection_recovery('broker')
            else:
                self._observe_connection_recovery(None)
            if self.receipt_identity:
                receipt=next((row for row in self.view['commands'] if all(
                    row.get(key)==expected for key,expected in self.receipt_identity.items())),None)
                if receipt:self._show_receipt(receipt)
            self.render_view()
            self._restore_selection(selected)
        elif result['action'] == 'close_positions':
            self._close_positions_result = (self._worker.args, result['value'])
        elif result['action'] == 'recover':
            self._stale = True
            self._connection_retry_pending = True
            self._publication_after_ns = time.monotonic_ns()
            _, _, identity, _ = self._worker.args
            self._recovery_launch = (identity[1], identity[0], result['value']['launch']['pid'])
            self._startup_in_progress = True
            launch = self._recovery_launch
            QTimer.singleShot(30_000, lambda: self._recovery_sync_timeout(launch))
            self._recovery_feedback('恢复进程已启动，正在等待柜台同步。开仓保持停用。')
        else:
            if result.get('previous_receipt') is not None:
                self._show_receipt(result['previous_receipt'])
            receipt=result['value'];self.receipt_identity={k:receipt[k] for k in ('command_id','payload_hash')}
            self._publication_after_ns = time.monotonic_ns()
            self._stale=True
            self._show_receipt(receipt)
            self.dashboard.render_home()

    def _selectable_rows(self):
        if self.view is None:return {}
        return {
            'cycles':(self.cycles,self._base_cycle_ids),
            'orders':(self.tables['orders'],[row['child_order_id'] for row in sorted_orders(self.view['orders'])]),
            'markets':(self.tables['markets'],[row['contract_id'] for row in self.view['markets']]),
            'external_positions':(self.tables['external_positions'],[(row['contract_id'],row['direction'])
                for row in self.view.get('external_positions',[])]),
        }

    def _selected_identities(self):
        return {name:{identities[index.row()] for index in table.selectionModel().selectedRows()
                if index.row()<len(identities) and not table.isRowHidden(index.row())}
                for name,(table,identities) in self._selectable_rows().items()}

    def _restore_selection(self,selected):
        for name,(table,identities) in self._selectable_rows().items():
            table.clearSelection()
            selection=table.selectionModel()
            for row,identity in enumerate(identities):
                if identity in selected.get(name,()) and not table.isRowHidden(row):
                    selection.setCurrentIndex(table.model().index(row,0),QItemSelectionModel.Select | QItemSelectionModel.Rows)
                    # Current row and selection must reference the same new snapshot.
                    if name in {'cycles','external_positions'}:break

    def _job_failed(self,error):
        if error.get('previous_receipt') is not None:
            self._show_receipt(error['previous_receipt'])
        if self._worker is not None and self._worker.action == 'recover':
            if error['identity'] is not None:
                self.receipt_identity = error['identity']
                self.home_issue = '恢复退出命令结果待核对，请查询原回执'
            self._recovery_feedback(f"{error.get('reason') or '恢复未完成，请核对运行日志。'} · {error['code']}")
            self._connection_retry_pending = True
            return
        if self._worker is not None and self._worker.action == 'close_positions':
            message = f"未能从执行器查询可平持仓 · {error['code']}"
            self.notice_label.setText(message)
            self.show_home_feedback(message)
            return
        if self.view is not None and error['code'] in CONNECTION_ERRORS:
            from online_trader.desktop.runtime_logs import read_record, supplied_run_directory
            run = self.dashboard.latest_run() or supplied_run_directory(self.view)
            result = read_record(run/'result.json', limit=4 * 1024 * 1024) if run is not None else None
            if result is None and run is not None:
                try:phase_path = max(run.glob('phase-*.json'), key=lambda path:path.stat().st_mtime_ns, default=None)
                except OSError:phase_path = None
                phase = read_record(phase_path) if phase_path is not None else None
                if phase and phase.get('phase') == 'DSV_STOPPED':
                    result = phase | {'status':'DSV_STOPPED'}
                else:
                    target_status = read_record(run / 'target-owner-status.json')
                    if target_status and target_status.get('phase') == 'STOPPED':
                        result = {
                            'account_key': target_status.get('account_key'),
                            'kernel_boot_id': target_status.get('runtime_boot_id'),
                            'status': 'DSV_STOPPED',
                        }
            identity = (result.get('snapshot') or result) if result else None
            if (isinstance(identity, dict) and isinstance(self._boot, str)
                    and (identity.get('account_key'), identity.get('kernel_boot_id')) == (
                        self.view['account_key'], self._boot)
                    and result.get('status') in {'DSV_STOPPED','DSV_SCOPE_FINISHED'}):
                self._finished_run = (self.view['account_key'], self._boot, run)
                self.view = None
                self.snapshot_published_ns = None
                self._publication_after_ns = 0
                self._observe_connection_recovery(None)
                self._boot = None
                self.receipt_identity = None
                self._control_notice = None
                self.monitor_diagnostics = None
                self._stale = True
                self._last_account_record = {}
                self._base_cycle_ids = []
                self.dashboard.reset_account_view(preserve_logs=True)
                self._tray.set_status('idle')
                self.connection_label.setText('本次策略运行已结束')
                self.connection_detail.setText('已退出柜台连接')
                self.notice_label.setText('策略运行已结束，可再次启动。')
                self.show_home_feedback('可用当前账号和参数再次启动。')
                return
        if self.view is not None or error['identity'] is not None:
            self._sounds.observe_request_error()
        if self.view is not None:self._tray.set_status('error')
        if error['identity'] is not None:
            self.receipt_identity=error['identity']
            self._publication_after_ns = time.monotonic_ns()
        self._failed(error['code'])
        if (self.view is not None and self._worker is not None and self._worker.action == 'refresh'
                and error['code'] in CONNECTION_ERRORS):
            self._observe_connection_recovery('owner')
        if self.view is not None:self.dashboard.add_activity('状态刷新失败 · '+error['code'],category='error')
        if self.view is not None:
            self.dashboard.quote_status.setText('合约行情 · 上次快照已过期')
            self.dashboard.render_strategy_monitor(None)
            self.dashboard.manual_status.setText('连接或请求失败 · 等待重新同步')
        if self.paired_launcher is not None and self.view is None and error['code']=='E_TIMEOUT':
            self.connection_label.setText('SimNow 执行器尚未连接')
            self.notice_label.setText('通过「连接 SimNow 模拟盘」核对并启动今晚的范围。')
        if error['identity'] is not None:
            self._control_notice='请求结果未知，已保留原命令身份。请查询上次回执。'
            self.notice_label.setText(self._control_notice)
            self.home_issue = '命令结果待核对，请查看交易详情中的命令回执'
        if self.view is None and error['identity'] is None and error['code'] in CONNECTION_ERRORS:
            self.connection_label.setText('尚未连接模拟盘')
            self.notice_label.setText('点击「启动策略」后，程序会按所选账号和当前参数准备连接。')
            if self._strategy_starter is not None and self._strategy_starter.status_text:
                self.show_home_feedback(self._strategy_starter.status_text)
            elif self._session_dialog is not None and hasattr(self._session_dialog, 'status'):
                self.show_home_feedback(self._session_dialog.status.text())
            else:
                self.show_home_feedback('尚未连接模拟盘')
        else:
            self.show_home_feedback(f"未取得最新状态 · {error['code']}")

    def _finished(self):
        action=self._worker.action if self._worker is not None else None
        super()._finished()
        self._enable_commands(not self._closing and not self._stale and self.view is not None and not self.view['truncated'])
        self.command_buttons['receipt'].setEnabled(not self._closing and self._worker is None and self.receipt_identity is not None)
        positions_result, self._close_positions_result = self._close_positions_result, None
        deferred,self._deferred_request=self._deferred_request,None
        if self._closing:return
        if positions_result is not None:
            self._action(lambda: self._review_close_positions(*positions_result))
        if deferred is None:
            if self._connection_retry_pending or (action in ('command','status') and self.timer.isActive()):
                self.refresh()
            return
        identity,action,args=deferred
        if (self.view is None or identity!=self._command_binding() or
                (action=='command' and (self._stale or self.view['truncated'])
                 and args[0] not in OWNER_RECHECK_OPERATIONS)):
            self._control_notice='请求未发送。请核对当前账户、执行器与参数状态。'
            self.notice_label.setText(self._control_notice)
            self.show_home_feedback(self._control_notice)
        elif not (action=='command' and args[0]=='ARM_ENTRIES' and args[1]['scope_type']=='ACCOUNT'
                  and self.view['entry_authority']=='ARMED'):
            self._control_notice=None
            self._start_job(action,args)

    def render_view(self):
        v=self.view;c=v['connection']
        self._tray.set_status('error' if self._stale or c['fault'] or c['recovery_required'] or c['clock_fault'] else
                              'running' if v['entry_authority']=='ARMED' else 'idle')
        authority_label='人工开仓权限' if v['configuration'].get('manual_workbench') else '自动开仓权限'
        diagnostics = v['configuration'].get('strategy_diagnostics', {})
        diagnostic_notice = ''
        if diagnostics.get('comparison'):
            diagnostic_notice += ' · DSV 对照不可用或参数不一致'
        if diagnostics.get('ntp') in {'NTP_UNAVAILABLE', 'NTP_SKEW'}:
            diagnostic_notice += ' · 请核对本机时钟'
        operation_log = diagnostics.get('operation_log')
        if isinstance(operation_log, dict):
            from online_trader.desktop.runtime_logs import operation_log_message
            diagnostic_notice += ' · ' + operation_log_message(operation_log)
        if any(diagnostics.get(key) for key in ('status', 'run_identity')) or isinstance(operation_log, str):
            diagnostic_notice += ' · 诊断日志写入失败'
        market_health = v['configuration'].get('market_health') or {}
        feed_status = market_health.get('status')
        if feed_status == 'FEED_SUSPECTED_STALLED':
            diagnostic_notice += ' · 多合约行情疑似断流（暂停新开仓）'
        elif feed_status == 'OWNER_STALLED':
            diagnostic_notice += ' · 后台主循环调度偏慢'
        elif feed_status == 'RECONNECTING':
            diagnostic_notice += ' · 行情/柜台连接恢复中'
        if v['configuration'].get('age_filter_disabled'):
            diagnostic_notice += ' · 开仓报价年龄检查已关闭(0秒)'
        self.connection_detail.setText(f"账户连接：{'已连接' if c['broker_connected'] else '未连接'} · 事实同步：{'完整' if c['synchronized'] else '不完整'} · 通道：{'故障' if c['fault'] else '无已知故障'} · 持仓核对：{'待处理' if c['recovery_required'] else '无未决屏障'} · 时钟：{'故障' if c['clock_fault'] else '无已知故障'} · {authority_label}：{v['entry_authority']}" + diagnostic_notice)
        for name,table in self.tables.items():
            if name in {'orders', 'trades'}:
                # WorkbenchLayout owns the user-facing columns for these tables.
                # Writing the raw projection first made every Runtime refresh repaint them twice.
                continue
            rows = v['cycles'] if name == 'protection' else v.get(name, [])
            resized = table.rowCount() != len(rows)
            table.setRowCount(len(rows))
            for i,row in enumerate(rows):
                for j,key in enumerate(TABLES[name][1]):
                    value = row.get(key)
                    label = display_cell(key, value)
                    item = table.item(i, j) if self._runtime_mode else None
                    if item is None:
                        item = QTableWidgetItem(label)
                        item.setToolTip(str(value))
                        table.setItem(i, j, item)
                        resized = True
                    else:
                        if item.text() != label:
                            item.setText(label)
                        if item.toolTip() != str(value):
                            item.setToolTip(str(value))
            if resized:
                table.resizeColumnsToContents()
                for column in range(table.columnCount()):
                    table.setColumnWidth(column, min(260, table.columnWidth(column)))
        self.filter_markets()
        self.command_buttons['arm'].setText('启动策略 (F5)')
        self.pause_button.setText('停止策略 (F6)')
        self.command_buttons['arm'].setToolTip(v['configuration'].get('strategy_stop_wait') or
            ('等待原停止指令的撤单结果' if v['configuration'].get('stop_cancel_pending') else ''))
        self.dashboard.render(v)
        if not self._stale:
            self._sounds.observe(v)
        history_notice = ('历史列表已限量显示，省略 ' +
                          str(sum(v.get('history_omitted', {}).values())) + ' 条。' if v.get('history_truncated') else '')
        capacity_notice = ('当前明细超过容量，省略 ' + str(sum(v.get('detail_omitted', {}).values())) +
                           ' 条。可停止策略、撤自有开仓单或平仓。平仓时向执行器查询可操作持仓。')
        source_notice = ('状态来自目标执行 Owner' if
                         v['configuration'].get('execution_core') == 'DSV_TARGET_EXECUTION'
                         else '状态来自原内核')
        age_tier, age_sec = self.snapshot_age_tier()
        stale_notice = (
            f'无法确认后台最新状态（已超过 {age_sec} 秒未收到刷新），请核查运行进程/日志；仍可向 Owner 提交停策略、撤单或平仓请求。'
            if self._stale and age_tier == 'UNKNOWN_BACKEND' and age_sec is not None else
            f'工作台快照已滞后 {age_sec} 秒（显示上次确认数据；后台策略由 Owner 独立维持，可提交降风险请求）。'
            if self._stale and age_sec is not None else
            f'界面更新偏慢（快照已滞后 {age_sec} 秒）。'
            if age_tier == 'SLOW' and age_sec is not None else ''
        )
        self.notice_label.setText(capacity_notice if v.get('detail_unavailable') else
            '当前摘要不完整，请通过执行器核对控制请求。' if v['truncated'] else
            stale_notice or
            history_notice or
            self._control_notice or source_notice + '；行情来源时间与新鲜度单独显示。关闭工作台保留原持仓保护。')

    def filter_markets(self):
        table=self.tables['markets'];term=self.filter_input.text().casefold()
        for row in range(table.rowCount()):table.setRowHidden(row,not any(term in table.item(row,col).text().casefold() for col in (0,1)))

    def policy(self,kind):
        if self.view is None:raise TraderError('E_NOT_READY','请先刷新状态')
        items=[row['policy_id'] for row in self.view['policies'] if row['policy_kind']==kind]
        if len(items)!=1:raise TraderError('E_RULE_UNAPPROVED','缺少唯一适用的执行政策')
        return items[0]

    def send(self,operation,scope,parameters):
        if self.view is None or ((self._stale or self.view['truncated']) and operation not in OWNER_RECHECK_OPERATIONS):
            self.notice_label.setText('状态未知或已过期，请先刷新。')
            self.show_home_feedback(self.notice_label.text())
            return
        self._control_notice=None
        self.show_home_feedback('等待命令回执')
        self._start_job('command',(operation,scope,parameters))

    def _action(self,callback):
        try:callback()
        except TraderError as error:
            self.notice_label.setText(str(error)+' · '+error.code)
            self.show_home_feedback(self.notice_label.text())
        except (OSError,KeyError,ValueError,TypeError):
            self.notice_label.setText('输入文件或当前选中记录不完整，请重新核对。')
            self.show_home_feedback(self.notice_label.text())

    def start_strategy(self):
        if self.view is None:
            if self.selected_account is None and self.paired_launcher is None:
                self.dashboard.show_account_settings()
                self.notice_label.setText('请先在账户管理中选用模拟账户。')
            else:
                self.cancel_timed_start()
                self.open_session()
        elif not self._stale:
            self.arm()

    def _command_binding(self):
        config=self.view['configuration']
        return (self.pipe_name,self.view['account_key'],self._boot,config['rule_set_id'],config['config_revision'])

    def arm(self):
        def action():
            identity=self._command_binding();policy=self.policy('AUTHORITY')
            if self._stale or self.view is None or identity!=self._command_binding():
                self.notice_label.setText('启动请求未发送。请刷新并重新核对账户与参数。')
                self.show_home_feedback(self.notice_label.text())
                return
            if self.view['entry_authority']=='ARMED':return
            config = self.view['configuration']
            if config.get('strategy_stop_requested'):
                self.show_home_feedback(config.get('strategy_stop_wait') or '正在核对原策略的退出条件。')
                return
            symbol_dirty = self.dashboard.has_unsaved_symbol_changes()
            panel = getattr(self.dashboard, 'trade_params_panel', None)
            param_dirty = panel is not None and panel.has_unsaved_changes()
            if symbol_dirty or param_dirty:
                title = '存在未保存的品种草稿' if symbol_dirty and not param_dirty else '存在未保存的策略参数'
                target_desc = (
                    '「策略参数 → 监控合约」中有尚未保存生效的品种修改'
                    if symbol_dirty and not param_dirty
                    else '「策略参数」中有尚未保存生效的参数修改'
                )
                answer = QMessageBox.question(
                    self,
                    title,
                    f'您在{target_desc}。\n\n'
                    '如果直接启动策略，将按当前运行中的合约与参数执行。\n是否仍按当前运行配置启动策略？',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self.dashboard.show_details(
                        self.dashboard.symbol_tab if (symbol_dirty and not param_dirty) else self.dashboard.parameter_tab
                    )
                    return
            self.send('ARM_ENTRIES',{'scope_type':'ACCOUNT','scope_ids':[]},
                {'authority_policy_id':policy,'rule_set_id':identity[-2],'config_revision':identity[-1]})
        self._action(action)

    def _target_execution_core(self):
        return (self.view is not None
                and self.view.get('configuration', {}).get('execution_core') == 'DSV_TARGET_EXECUTION')

    def _cycle_target_revision(self, row):
        rev = row.get('plan_revision')
        if rev is None and isinstance(row.get('target_execution'), Mapping):
            rev = row['target_execution'].get('target_revision')
        if rev is None and self.view is not None:
            cid = row.get('position_cycle_id')
            for item in self.view.get('cycles', ()):
                if item.get('position_cycle_id') == cid and item.get('plan_revision') is not None:
                    rev = item['plan_revision']
                    break
            if rev is None:
                records = (self.view.get('status_projection') or {}).get('records', ())
                for rec in records:
                    if rec.get('record_type') == 'CYCLE' and rec.get('position_cycle_id') == cid:
                        rev = rec.get('plan_revision')
                        break
        return None if rev is None else str(rev)

    def _manual_close_params(self, cycle_id, quantity, row, *, include_price_mode=True):
        target_rev = self._cycle_target_revision(row)
        takeover_token = f'target-rev-{target_rev}' if self._target_execution_core() and target_rev is not None else None
        params = {
            'position_cycle_id': cycle_id, 'quantity': quantity,
            'execution_policy_id': self.policy('EXECUTION'), 'external_takeover_id': takeover_token,
        }
        if include_price_mode and self.view is not None and 'manual_close_price_mode' in self.view['configuration']:
            params['price_mode'] = self.dashboard.manual_close_price_mode.currentText()
        return params

    def close_selected(self):
        if self.view is not None and self.view.get('detail_unavailable'):
            self._request_close_positions()
            return

        def action():
            selected=self.cycles.selectionModel().selectedRows()
            if len(selected)!=1:raise TraderError('E_SCOPE','请选择一个自有持仓周期')
            i=selected[0].row()
            # Base STATUS row identities, not a potentially different detail ordering.
            cid=self._base_cycle_ids[i]
            row=next(r for r in self.view['cycles'] if r['position_cycle_id']==cid)
            if row['remaining_quantity']<=0:raise TraderError('E_SCOPE','所选周期没有可减持仓')
            params = self._manual_close_params(cid, 1, row, include_price_mode=False)
            qty,ok=QInputDialog.getInt(self,'平选定自有仓',row['contract_id']+' · 平仓手数',1,1,row['remaining_quantity'])
            if ok:
                params['quantity'] = qty
                self.send('MANUAL_CLOSE',{'scope_type':'CYCLE','scope_ids':[cid]},params)
        self._action(action)

    def close_manual_cycle(self, cycle_id, quantity=None):
        def action():
            if self.view is None:
                raise TraderError('E_NOT_READY', '请先刷新持仓')
            cycle = next((row for row in self.view['cycles'] if row['position_cycle_id'] == cycle_id), None)
            if cycle is None or cycle['remaining_quantity'] <= 0:
                raise TraderError('E_SCOPE', '所选周期没有可平的自有持仓')
            remaining = cycle['remaining_quantity']
            amount = remaining if quantity is None else min(quantity, remaining)
            params = self._manual_close_params(cycle_id, amount, cycle, include_price_mode=True)
            self.send('MANUAL_CLOSE', {'scope_type': 'CYCLE', 'scope_ids': [cycle_id]}, params)
        self._action(action)

    def manual_close_all(self):
        def action():
            if self.view is None or 'manual_close_price_mode' not in self.view['configuration']:
                raise TraderError('E_NOT_READY','请先连接支持批量平仓的执行器')
            if self.view.get('detail_unavailable'):
                self._request_close_positions(bulk=True)
                return
            cycles = [row for row in self.view['cycles'] if row['remaining_quantity'] > 0
                      and row.get('management_state') not in {'TAKEOVER_PENDING', 'TAKEN_OVER'}]
            self._confirm_manual_close_all(cycles)
        self._action(action)

    def _request_close_positions(self, *, after=None, bulk=False):
        if self._runtime_command_bus is not None and self.view is not None:
            records = (self.view.get('status_projection') or {}).get('records', ())
            rows = sorted(
                ({'position_cycle_id': rec['position_cycle_id'], 'contract_id': rec['contract_id'],
                  'direction': rec['direction'], 'remaining_quantity': rec['remaining_quantity'],
                  'plan_revision': rec.get('plan_revision')}
                 for rec in records
                 if rec.get('record_type') == 'CYCLE' and int(rec.get('remaining_quantity', 0)) > 0
                 and rec.get('takeover_state') not in {'TAKEOVER_PENDING', 'TAKEN_OVER'}
                 and (after is None or rec['position_cycle_id'] > after)),
                key=lambda item: item['position_cycle_id'],
            )
            page = {'positions': rows[:64], 'has_more': len(rows) > 64}
            self._review_close_positions((self._command_binding(), after, bulk), page)
            return
        self.show_home_feedback('正在向执行器查询可平持仓。')
        self._start_job('close_positions', (self._command_binding(), after, bulk))

    def _review_close_positions(self, request, page):
        binding, after, bulk = request
        if self.view is None or binding != self._command_binding():
            raise TraderError('E_SCOPE', '账户、执行器或参数已变化，请重新查询持仓。')
        rows = page['positions']
        if bulk:
            if page['has_more']:
                raise TraderError('E_WORKBENCH_CAPACITY', '可平持仓超过单次批量平仓上限，请按持仓周期操作。')
            self._confirm_manual_close_all(rows)
            return
        if not rows and after is None:
            raise TraderError('E_SCOPE', '没有可平的自有持仓')
        choices = [f"{row['contract_id']} · {display_cell('direction', row['direction'])} · "
                   f"{row['remaining_quantity']} 手 · {row['position_cycle_id']}" for row in rows]
        if page['has_more']:
            choices.append('下一页')
        if after is not None:
            choices.append('返回第一页')
        choice, ok = QInputDialog.getItem(self, '选择自有持仓', '持仓周期', choices, editable=False)
        if not ok:
            return
        if self.view is None or binding != self._command_binding():
            raise TraderError('E_SCOPE', '账户、执行器或参数已变化，请重新查询持仓。')
        if choice in {'下一页', '返回第一页'}:
            self._request_close_positions(after=rows[-1]['position_cycle_id'] if choice == '下一页' else None)
            return
        cycle = rows[choices.index(choice)]
        maximum = cycle['remaining_quantity']
        cycle_id = cycle['position_cycle_id']
        parameters = self._manual_close_params(cycle_id, 1, cycle, include_price_mode=True)
        qty, ok = QInputDialog.getInt(self, '平选定自有仓', cycle['contract_id'] + ' · 平仓手数', 1, 1, maximum)
        if not ok:
            return
        if self.view is None or binding != self._command_binding():
            raise TraderError('E_SCOPE', '账户、执行器或参数已变化，请重新查询持仓。')
        parameters['quantity'] = qty
        if 'manual_close_price_mode' in self.view['configuration']:
            parameters['price_mode'] = self.dashboard.manual_close_price_mode.currentText()
        self.send('MANUAL_CLOSE', {'scope_type': 'CYCLE', 'scope_ids': [cycle_id]}, parameters)

    def _confirm_manual_close_all(self, cycles):
        if not cycles:
            raise TraderError('E_SCOPE', '没有可平的自有持仓')
        if len(cycles) > 64:
            raise TraderError('E_WORKBENCH_CAPACITY', '可平持仓超过单次批量平仓上限，请按持仓周期操作。')
        target_core = self._target_execution_core()
        positions = {}
        for row in cycles:
            cid = row['position_cycle_id']
            rev = self._cycle_target_revision(row)
            key = f'{cid}:rev:{rev}' if target_core and rev is not None else cid
            positions[key] = row['remaining_quantity']
        identity = self._command_binding()
        mode = self.dashboard.manual_close_price_mode.currentText()
        policy = self.policy('EXECUTION')
        message = f"按{mode}平掉 {len(positions)} 个自有持仓周期，共 {sum(positions.values())} 手？"
        if QMessageBox.question(self, '平所有自有持仓', message) != QMessageBox.Yes:
            return
        if self.view is None or identity != self._command_binding():
            raise TraderError('E_SCOPE', '账户、执行器或参数已变化，请重新查询持仓。')
        self.send('MANUAL_CLOSE_ALL', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                  {'positions': positions, 'price_mode': mode, 'execution_policy_id': policy})

    def set_symbol_entries(self,enabled):
        def action():
            table=self.tables['markets']
            selected=sorted({index.row() for index in table.selectedIndexes() if not table.isRowHidden(index.row())})
            if not selected:raise TraderError('E_SCOPE','请在行情页选择要启停的品种')
            codes=sorted({self.view['markets'][index]['contract_id'] for index in selected})
            scope={'scope_type':'CONTRACT','scope_ids':codes}
            if self._runtime_command_bus is not None:
                if len(codes) != 1:
                    raise TraderError('E_SCOPE', '请一次确认一个合约的开仓权限')
                origin = 'MANUAL' if self.view['configuration'].get('manual_workbench') else 'STRATEGY'
                state = self.view.get('entry_policies', {}).get(f'{origin}:{codes[0]}', {})
                reason, confirmed = QInputDialog.getText(self, '调整开仓权限', '请输入操作原因')
                if confirmed and reason.strip():
                    self.send('SET_ENTRY_POLICY', scope, {'origin': origin, 'enabled': enabled,
                        'expected_policy_revision': state.get('revision', '0'), 'reason': reason.strip(),
                        'cancel_owned_entry_remainders': not enabled})
                return
            if enabled:
                config=self.view['configuration']
                self.send('ARM_ENTRIES',scope,{'authority_policy_id':self.policy('AUTHORITY'),
                    'rule_set_id':config['rule_set_id'],'config_revision':config['config_revision']})
            else:self.send('PAUSE_NEW_ENTRIES',scope,{'cancel_owned_entry_remainders':True})
        self._action(action)

    def flatten(self):
        def action():
            policy=self.policy('EXECUTION')
            is_standalone = bool(self.view and (self.view.get('connection') or {}).get('execution_mode') == 'STANDALONE')
            prompt = ('将当前账户自有周期剩余目标设为0并停止策略？' if is_standalone
                      else '将当前账户所有自有周期的统一剩余目标设为0，并停止新增？外部持仓不会认领。')
            if QMessageBox.question(self,'平仓后停开',prompt)==QMessageBox.Yes:
                self.send('FLATTEN_THEN_STOP',{'scope_type':'ACCOUNT','scope_ids':[]},{'execution_policy_id':policy})
        self._action(action)

    def cancel_selected(self):
        table=self.tables['orders']
        rows=sorted({index.row() for index in table.selectedIndexes() if not table.isRowHidden(index.row())})
        ordered = sorted_orders(self.view['orders']) if self.view else []
        self.cancel_manual_children([ordered[i]['child_order_id'] for i in rows] if rows else [])

    def cancel_manual_children(self, ids):
        if not ids:
            self.notice_label.setText('请选择自有委托。')
            return
        available = {order['child_order_id'] for order in (self.view or {}).get('orders', [])}
        if not set(ids) <= available:
            self.notice_label.setText('所选自有委托已变化，请重新选择。')
            return
        self.send('CANCEL_SELECTED', {'scope_type': 'SELECTED_ORDERS', 'scope_ids': ids}, {'child_order_ids': ids})

    def cancel_entries(self):self.send('CANCEL_OWNED',{'scope_type':'OWNED_ORDERS','scope_ids':[]},{'include_exits':False})
    def cancel_account(self):
        def action():
            review=self.view['configuration'].get('native_cancel_review',{})
            if not review.get('eligible'):
                raise TraderError('E_FACT_BARRIER','需要完整的柜台委托范围，且全部活动单必须在人工授权合约内')
            message=f"暂停新增，并撤销账户当前 {review['active_count']} 笔活动委托（含外部单和保护委托）？已有持仓的保护目标继续生效。"
            message+='\n'+'、'.join(f'{code} {count} 笔' for code,count in sorted(review['by_contract'].items()))
            if QMessageBox.question(self,'确认账户全部撤单',message)==QMessageBox.Yes:
                self.send('CANCEL_NATIVE_ACCOUNT',{'scope_type':'ACCOUNT','scope_ids':[]},{'evidence_hash':review['evidence_hash']})
        self._action(action)

    def reconcile(self):self.send('REQUEST_RECONCILIATION',{'scope_type':'ACCOUNT','scope_ids':[]},{'requested_scope':'FULL_ACCOUNT','position_cycle_id':None})

    def query_order_archive(self):
        if QMessageBox.question(self,'查询原单留档','查找缺失原单已有的完整终态记录。核对通过后，原内核可以继续管理原退出。')==QMessageBox.Yes:
            self.send('REQUEST_RECONCILIATION',{'scope_type':'ACCOUNT','scope_ids':[]},
                {'requested_scope':'FULL_ACCOUNT','position_cycle_id':None,'query_order_archive':True})

    def import_historical_orders(self):
        def action():
            filename,_=QFileDialog.getOpenFileName(self,'选择已核对的柜台终态证据','','JSON (*.json)')
            if not filename:return
            with Path(filename).open('rb') as stream:raw=stream.read(131073)
            if len(raw)>131072:raise TraderError('E_SCHEMA','历史证据文件过大')
            packet=strict_loads(raw)
            if packet['account_key']!=self.view['account_key']:raise TraderError('E_SCOPE','文件账户与当前工作台不符')
            message=f"柜台来源 {packet['statement_reference']}\n核对 {len(packet['orders'])} 笔历史原单及其逐笔成交。确认这些记录来自当前账户的柜台终态证据？"
            if QMessageBox.question(self,'确认历史原单证据',message)==QMessageBox.Yes:
                self.send('REQUEST_RECONCILIATION',{'scope_type':'ACCOUNT','scope_ids':[]},
                    {'requested_scope':'FULL_ACCOUNT','position_cycle_id':None,'historical_orders':packet})
        self._action(action)
    def receipt(self):
        if self.receipt_identity is not None:self._start_job('status',self.receipt_identity)

    def manual_open(self,direction):
        def action():
            config=self.view['configuration']
            if config.get('manual_workbench'):
                code,ok=QInputDialog.getItem(self,'人工开仓','合约',config['scoped_contracts'],0,False)
                if not ok:return
                qty,ok=QInputDialog.getInt(self,'人工开仓',code+' · 手数',1,1,2147483647)
                if not ok:return
                price,ok=QInputDialog.getText(self,'人工限价',f"{code} · {'开多' if direction=='LONG' else '开空'} {qty}手 · 限价")
                if ok:self.send('MANUAL_OPEN',{'scope_type':'CONTRACT','scope_ids':[code]},{'contract_id':code,
                    'direction':direction,'quantity':qty,'limit_price':price,'order_policy_id':self.policy('EXECUTION'),'protection_plan':None})
                return
            file,_=QFileDialog.getOpenFileName(self,'选择已审批的人工计划（不从预览参数生成权限）','','JSON (*.json)')
            if not file:return
            path=Path(file)
            if path.stat().st_size>65536:raise TraderError('E_SCHEMA','计划文件过大')
            plan=strict_loads(path.read_bytes())
            if plan.get('direction')!=direction or plan.get('account_key')!=self.view['account_key']:raise TraderError('E_SCOPE','计划账户或方向不符')
            price,ok=QInputDialog.getText(self,'人工限价',f"{plan['contract_id']} · {plan['initial_open_quantity']}手 · 限价")
            if ok:self.send('MANUAL_OPEN',{'scope_type':'CONTRACT','scope_ids':[plan['contract_id']]},{'contract_id':plan['contract_id'],
                'direction':direction,'quantity':plan['initial_open_quantity'],'limit_price':price,'order_policy_id':self.policy('EXECUTION'),'protection_plan':plan})
        self._action(action)

    def submit_manual(self,code,direction,qty,price):
        def action():
            from online_trader.domain.values import decimal_value
            if self._stale or not self.view or not self.view['configuration'].get('manual_workbench'):
                raise TraderError('E_NOT_READY','当前执行范围未启用人工开仓')
            if code not in self.view['configuration']['scoped_contracts']:
                raise TraderError('E_SCOPE','请选择已授权的合约')
            limit = price
            if price in {'', '0', 'OPPONENT'}:
                quote = next((row for row in self.view.get('markets', [])
                              if row['contract_id'] == code and row.get('fresh')), None)
                field = ('ask1' if direction == 'LONG' else 'bid1') if price == 'OPPONENT' else 'last'
                if quote is None or quote.get(field) is None:
                    raise TraderError('E_NOT_READY', '所选合约没有新鲜的下单报价')
                limit = quote[field]
            decimal_value(limit,positive=True)
            if QMessageBox.question(self,'确认人工开仓',
                    f"{code} · {'开多' if direction=='LONG' else '开空'} {qty} 手 · 限价 {limit}")==QMessageBox.Yes:
                self.send('MANUAL_OPEN',{'scope_type':'CONTRACT','scope_ids':[code]},{'contract_id':code,
                    'direction':direction,'quantity':qty,'limit_price':limit,'order_policy_id':self.policy('EXECUTION'),
                    'protection_plan':None})
        self._action(action)

    def adopt_selected(self,*,lot_review=False):
        def action():
            selected=self.tables['external_positions'].selectionModel().selectedRows()
            if len(selected)!=1:raise TraderError('E_SCOPE','请选择一条柜台持仓')
            index=selected[0].row()
            rows=self.view.get('external_positions',[])
            field='lot_review_available' if lot_review else 'eligible'
            if not 0<=index<len(rows) or not rows[index].get(field,False):
                raise TraderError('E_SCOPE','请选择一条当前可接管的柜台持仓')
            row=rows[index];code=row['contract_id'];positions=[]
            params={key:row[key] for key in ('contract_id','direction','evidence_hash')}
            if lot_review:
                filename,_=QFileDialog.getOpenFileName(self,'选择已审核的原批次存续明细','','JSON (*.json)')
                if not filename:return
                with Path(filename).open('rb') as stream:raw=stream.read(131073)
                if len(raw)>131072:raise TraderError('E_SCHEMA','批次证据文件过大')
                proof=strict_loads(raw)
                if proof['contract_id']!=code or proof['account_key']!=self.view['account_key']:
                    raise TraderError('E_SCOPE','文件与所选账户或合约不符')
                params['lot_review']=proof
                positions=[f"柜台来源 {proof['statement_reference']}"]
                for item in proof['original_lots']:
                    lot=row['original_lots'][item['lot_key']]
                    if lot['quantity']!=item['remaining_quantity']:
                        positions.append(f"原批次 {item['lot_key']} · {lot['quantity']} → {item['remaining_quantity']} 手")
                positions.extend(f"替换成交 {item['trade_id']} · {item['remaining_quantity']} 手" for item in proof['replacement_lots'])
            for side,bucket in row['additions'].items():
                label='多' if side=='long' else '空'
                positions.append(f"新增{label}仓周期 · 今 {bucket['today']} 手 · 昨 {bucket['historical']} 手 · 保护参考价 {bucket['reference_price']}")
            for reduction in row['reductions']:
                label='今' if reduction['bucket']=='today' else '昨'
                positions.append(f"调减 {reduction['position_cycle_id']} · {label}仓 {reduction['quantity']} 手")
            message=f"确认 {code} 的柜台持仓差异并暂停新增？\n"+'\n'.join(positions or ['持仓数量一致，仅确认本次外部委托变化。'])
            if QMessageBox.question(self,'确认持仓归属调整',message)==QMessageBox.Yes:
                self.send('ADOPT_EXTERNAL_POSITION',{'scope_type':'CONTRACT','scope_ids':[code]},
                    params)
        self._action(action)

    def open_accounts(self):
        if self.store is None:self.notice_label.setText('请通过 --account-store 指定本产品账户目录。');return
        if self.paired_launcher is not None:return
        from online_trader.desktop.accounts import AccountDialog
        dialog = AccountDialog(self.store, self, use_account=self.use_account)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()
        self.dashboard.reload_accounts()

    def select_account(self):
        if self._runtime_mode:
            account_id = self.dashboard.account_picker.currentData()
            if account_id is not None:
                self.switch_runtime_account(account_id)
            return
        if self.store is None or self.paired_launcher is not None:
            return
        account_id = self.dashboard.account_picker.currentData()
        if account_id is None:
            return
        try:
            self.use_account(account_id)
        except TraderError as error:
            self.notice_label.setText(f'账号未切换 · {error} · {error.code}')
            self.dashboard.reload_accounts()

    def _inspect_runtime_for_maintenance(self):
        from online_trader.runtime.instance import runtime_owner_exists
        from online_trader.runtime.shutdown import inspect_runtime_for_exit
        view, pid, inspect_error = inspect_runtime_for_exit(self.pipe_name) if self.pipe_name else (None, None, None)
        if view is None and (pid is not None or runtime_owner_exists()):
            return None, pid, (inspect_error or '后台交易服务仍在运行或运行锁尚未释放，暂无法确认状态。')
        return view, pid, None

    def switch_runtime_account(self, target_account_id):
        if (self._closing or self._runtime_shutdown_in_progress
                or self._runtime_bootstrap is not None
                or (self._runtime_command_bus is not None and self._runtime_command_bus.has_inflight)):
            QMessageBox.information(self, '切换交易账号', '请等待当前操作或连接步骤结束后再切换账号。')
            return False
        root = self._resolve_runtime_root()
        if root is None:
            QMessageBox.warning(self, '切换交易账号', '当前会话未绑定 runtime 目录，无法执行账号切换。')
            return False
        from online_trader.desktop.runtime_bootstrap import BootstrapController
        from online_trader.runtime.portable_config import PortableAccountStore
        from online_trader.runtime.shutdown import maintenance_stop_runtime_and_wait
        try:
            store = PortableAccountStore(root)
            account = store.get(target_account_id)
        except (TraderError, OSError, KeyError) as error:
            QMessageBox.warning(self, '切换交易账号', f'所选账号配置无效：{error}')
            self.dashboard.reload_accounts()
            return False

        if self.view is not None and self.view.get('account_key') == account['account_key']:
            self.show_home_feedback('当前已连接到所选账号。')
            self.dashboard.reload_accounts()
            return True

        view, pid, inspect_error = self._inspect_runtime_for_maintenance()
        if inspect_error is not None:
            QMessageBox.warning(
                self,
                '未能切换账号',
                f'{inspect_error}\n\n为确保账户安全，未切换账号。',
            )
            self.dashboard.reload_accounts()
            return False
        if view is not None:
            resp = view.get('responsibility')
            if isinstance(resp, Mapping) and not resp.get('maintenance_allowed'):
                blockers = '、'.join(str(code) for code in (resp.get('blockers') or ())) or '存在未结交易责任'
                QMessageBox.warning(
                    self,
                    '当前不可切换账号',
                    f'当前账户仍处于交易或保护收尾状态，不能安全切换（{blockers}）。\n\n'
                    '请先停止策略并等待所有已激活持仓与在途委托结清后再试；系统不会自动强行平仓。',
                )
                self.dashboard.reload_accounts()
                return False
            ok, stop_error = self._run_runtime_shutdown(
                lambda pipe, **kw: maintenance_stop_runtime_and_wait(
                    pipe, reason='SWITCH_ACCOUNT', **kw,
                ),
                pid=pid,
                view=view,
                title='OnlineTrader · 切换交易账号',
                label=f"正在安全停止当前会话并切换到 {account['alias']}…",
            )
            if not ok:
                QMessageBox.critical(
                    self,
                    '当前会话未能安全停止',
                    f'{stop_error or "未能确认原后台服务安全退出。"}\n\n原会话与状态保持不变。',
                )
                self.dashboard.reload_accounts()
                return False

        self._detach_runtime_client()
        self.view = None
        self._boot = None
        self._stale = True
        self._account_key = None
        self.snapshot_published_ns = None
        self._publication_after_ns = 0
        self.receipt_identity = None
        self._control_notice = None
        self._last_account_record = {}
        self._base_cycle_ids = []
        self._runtime_account = account['account_id']
        self.pipe_name = store.pipe_name(account)
        self.home_issue = ''
        if self._reports_window is not None:
            self._reports_window.close()
            self._reports_window = None
        for grid in (*self.tables.values(), self.cycles, *self.dashboard.manual_tables.values()):
            grid.setRowCount(0)
        self.funds_label.setText('资金未知 · 等待所选账号同步')
        self.connection_label.setText('等待所选账户同步')
        self.connection_detail.setText('尚未连接 · 等待柜台同步')
        self.kernel_label.setText('内核：未知 · 新仓权限：未知')
        self.dashboard.reset_account_view(preserve_logs=True)
        self.dashboard.render_home()
        self.dashboard.reload_accounts()
        self.show_home_feedback(f"正在切换到 {account['alias']}（仅监控，不自动开仓）…")

        controller = BootstrapController(
            store.directory,
            account=account['account_id'],
            attach_policy='attach-exact',
            expected_account_key=account['account_key'],
            parent=self,
        )
        self._runtime_bootstrap = controller

        def cleanup():
            if self._runtime_bootstrap is controller:
                self._runtime_bootstrap = None
            controller.deleteLater()

        def on_ready(new_pipe):
            cleanup()
            if self._closing:
                return
            self._attach_runtime_pipe(new_pipe)
            try:
                store.select_saved_account(account['account_id'])
                self.show_home_feedback(f"已切换到 {account['alias']}（监控模式，新增开仓保持关闭）。")
            except (TraderError, OSError):
                self.show_home_feedback(
                    f"已连接到 {account['alias']}（监控模式），但未能保存默认启动选择，可稍后重试。",
                )
            self.dashboard.reload_accounts()

        def on_failed(error):
            cleanup()
            code = getattr(error, 'code', 'E_RUNTIME_START')
            self.home_issue = f'{error}\n\n错误代码：{code}'
            self.show_home_feedback(f'切换账号失败（{code}）；当前未连接，可重新选择账号或检查配置。')
            self.dashboard.render_home()
            self.dashboard.reload_accounts()
            QMessageBox.critical(self, '切换交易账号失败', self.home_issue)

        def on_cancelled():
            cleanup()
            self.show_home_feedback('已取消等待新账号连接；当前处于未连接状态。')
            self.dashboard.render_home()
            self.dashboard.reload_accounts()

        controller.ready.connect(on_ready)
        controller.failed.connect(on_failed)
        controller.cancelled.connect(on_cancelled)
        controller.start()
        return True

    def attach_selected(self):
        if self.store is None or self.paired_launcher is not None:return
        try:
            self.use_account()
        except TraderError as error:
            self.notice_label.setText(f'账号未切换 · {error} · {error.code}')

    def use_account(self, account_id=None, expected_revision=None):
        if (self._closing or self._worker is not None or self._startup_in_progress
                or (self._strategy_starter is not None and self._strategy_starter.is_preparing())):
            raise TraderError('E_ACCOUNT_BUSY', '请等待当前账户操作结束')
        state = self.store.read()
        if expected_revision is not None and state['revision'] != expected_revision:
            raise TraderError('E_REVISION_CONFLICT', '账户目录已变化，请刷新')
        if account_id is None:
            account_id = state['selected_id']
        if account_id is None:
            self.notice_label.setText('请先在账户管理中选择账户。')
            return
        attached_id = self.selected_account['account_id'] if self.selected_account else None
        if state['selected_id'] != account_id or attached_id not in (None, account_id):
            state = self.store.change('select', expected_revision=state['revision'], account_id=account_id,
                                      attached_account_id=attached_id)
        account = next(row for row in state['accounts'] if row['account_id'] == account_id)
        if attached_id == account_id and self.pipe_name == self.store.pipe_name(account):
            self.selected_account = account
            self._account_login = self._read_account_login()
            self.dashboard.reload_accounts()
            self.refresh()
            return
        self._attach_account(account)

    def _attach_account(self, account):
        self.cancel_timed_start()
        self.selected_account=account;self.pipe_name=self.store.pipe_name(account)
        if self._strategy_starter is not None:
            self._strategy_starter.status_timer.stop()
            self._strategy_starter.deleteLater()
            self._strategy_starter = None
        if self._session_dialog is not None:
            self._session_dialog.status_timer.stop()
            self._session_dialog.close()
            self._session_dialog.deleteLater()
            self._session_dialog = None
        self._account_login = self._read_account_login()
        self._boot=None;self.view=None;self.receipt_identity=None;self._stale=True;self._account_key=None
        self.snapshot_published_ns = None
        self._publication_after_ns = 0
        self._observe_connection_recovery(None)
        self._finished_run = None
        self._control_notice=None
        self.home_feedback = self.home_issue = ''
        self._last_account_record = {}
        self._base_cycle_ids = []
        self.dashboard.rendered_view = None
        self.monitor_diagnostics = None
        self._diagnostic_write_failed = False
        for grid in (*self.tables.values(), self.cycles, *self.dashboard.manual_tables.values()):
            grid.setRowCount(0)
        self.account_label.setText('账户 · ' + self.display_account()[1])
        self.funds_label.setText('资金未知 · 等待所选账号同步')
        self.connection_label.setText('等待所选账户同步')
        self.connection_detail.setText('尚未连接 · 等待柜台同步')
        self.kernel_label.setText('内核：未知 · 新仓权限：未知')
        self.dashboard.reload_accounts()
        self.dashboard.reset_account_view()
        self._enable_commands(False)
        self._tray.set_status('idle')
        # Child report windows are account-bound and must not silently follow.
        if self._reports_window is not None:
            self._reports_window.close()
            self._reports_window = None
        self.refresh()

    def open_data(self):
        if self.store is None:
            self.notice_label.setText('请通过 --account-store 指定本产品账户目录，或使用 data desktop。');return
        from online_trader.marketdata import ReferenceStore
        from online_trader.desktop.data import DataDialog
        self._data_dialog=DataDialog(ReferenceStore(self.store.directory/'reference'),self);self._data_dialog.show()

    def arrange_timed_start(self):
        if self._scheduled_start is not None:
            self.cancel_timed_start()
            return
        if self.store is None or self.selected_account is None or self.paired_launcher is not None:
            self.show_home_feedback('请先选择要定时启动的模拟账户。')
            return
        def arrange():
            from online_trader.scheduling.reference import CalibratedClock
            profile = self.current_start_profile()
            settings = profile.to_record()['settings']
            schedule = settings['schedule']
            on_launch = settings['trade']['auto_start_on_launch']
            if not (schedule['enabled'] or on_launch):
                self.show_home_feedback('当前参数未启用定时启动或软件启动时自动启动。')
                return
            message = (f"模拟账户 {self.selected_account['masked_login']}，{len(settings['symbols'])} 个品种。\n"
                       f"北京时间日盘 {schedule['day_start_time']}、夜盘 {schedule['night_start_time']}，"
                       f"补启动 {schedule['catch_up_minutes']} 分钟。\n"
                       "确认后仅启动一次，有效至授时当日北京时间 24:00。")
            if QMessageBox.question(self, '安排定时启动', message) != QMessageBox.Yes:
                return
            self._scheduled_start = {'account_id':self.selected_account['account_id'],
                                     'profile_hash':profile.content_hash, 'ends':None, 'on_launch':on_launch}
            self._schedule_clock = CalibratedClock(lazy=True)
            self.dashboard.timed_start_button.setText('取消定时启动')
            self.pause_button.setEnabled(True)
            self.show_home_feedback('已安排本次定时启动。')
            self.schedule_timer.start()
            self.check_timed_start(on_launch=on_launch)
        self._action(arrange)

    def cancel_timed_start(self, *, message='已取消本次定时启动。'):
        if self._scheduled_start is not None:
            self._scheduled_start = None
            self._schedule_clock = None
            self.schedule_timer.stop()
            self.dashboard.timed_start_button.setText('安排定时启动')
            self.show_home_feedback(message)
            if self.view is None:
                self.pause_button.setEnabled(False)

    def check_timed_start(self, now=None, *, on_launch=False):
        scheduled = self._scheduled_start
        if scheduled is None:
            if self._guard_launch:
                self.show_home_feedback('守护已打开工作台，请安排本次策略启动。')
                self._guard_launch = False
            return
        if self._closing or self.selected_account is None or (
                self.selected_account['account_id'] != scheduled['account_id']):
            self.cancel_timed_start()
            return
        if self.view is not None:
            self.cancel_timed_start()
            self.show_home_feedback('原执行器已连接，请在主控核对策略状态。')
            return
        if self._startup_in_progress or self._worker is not None:
            return
        if now is None:
            clock = self._schedule_clock
            if clock is None:
                return
            clock.poll()
            if not clock.entry_ready():
                self.show_home_feedback('等待应用授时后开始核对定时启动。')
                return
            now = clock.now()
        if scheduled['ends'] is None:
            from online_trader.strategy.entry import CHINA_TIME
            local = now.astimezone(CHINA_TIME)
            scheduled['ends'] = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= scheduled['ends']:
            self.cancel_timed_start()
            return
        on_launch = scheduled.pop('on_launch', on_launch)
        from online_trader.runtime.desktop_bootstrap import timed_start_trigger
        try:
            profile = self.current_start_profile()
            if profile.content_hash != scheduled['profile_hash']:
                raise TraderError('E_REVISION_CONFLICT', '已安排的账号或策略参数发生变化。')
            trigger = timed_start_trigger(profile, now, on_launch=on_launch)
        except TraderError as error:
            self.cancel_timed_start()
            self.show_home_feedback(str(error))
            return
        if trigger:
            self.cancel_timed_start(message=f'定时启动已触发 · {trigger}')
            self.open_session(timed_trigger=trigger, now=now)

    def open_session(self, *, timed_trigger=None, now=None):
        if self.paired_launcher is not None:
            if self._session_dialog is not None and self._session_dialog.scheduled_request is not None:
                self._session_dialog.show()
                self._session_dialog.raise_()
                return
            from online_trader.desktop.session import SessionDialog
            self._session_dialog=SessionDialog(None,None,self,paired_launcher=self.paired_launcher);self._session_dialog.show()
            return
        if self.store is None or self.selected_account is None:
            self.notice_label.setText('请先通过本产品账户管理选择模拟账户。');return
        if self.view is not None:
            self.start_strategy()
            return
        def start_current():
            from online_trader.desktop.startup import StrategyStarter
            if self._strategy_starter is None:
                self._strategy_starter = StrategyStarter(self.store, self.selected_account['account_id'],
                                                        self.dashboard.profile, self)
            self._strategy_starter.start(timed_trigger=timed_trigger, now=now)
        self._action(start_current)

    def current_start_profile(self):
        return self.dashboard.checked_start_profile()

    def _result(self,result):super()._result(result)

    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() == QEvent.Type.WindowStateChange and hasattr(self, '_tray')
                and self.isMinimized() and not event.oldState() & Qt.WindowState.WindowMinimized):
            self._tray.on_minimized()

    def _confirm_and_stop_runtime(self):
        if (not self._runtime_mode or not self.pipe_name
                or self._runtime_shutdown_verified or self._update_restart_in_progress):
            return True
        from online_trader.runtime.shutdown import inspect_runtime_for_exit, shutdown_runtime_and_wait

        view, pid, inspect_error = inspect_runtime_for_exit(self.pipe_name)
        if view is None and pid is None:
            self._runtime_shutdown_verified = True
            return True
        if view is None:
            QMessageBox.warning(
                self,
                '交易服务仍在运行',
                f'{inspect_error or "无法读取后台交易服务状态。"}\n\n为确保账户安全，桌面工作台未关闭；请恢复连接后再试。',
            )
            return False

        open_cycles = [row for row in view.get('cycles', ()) if row.get('remaining_quantity', 0) > 0]
        active_orders = [row for row in view.get('orders', ()) if row.get('potential_remaining', 0) > 0]
        external_positions = view.get('external_positions') or []
        connection = view.get('connection') or {}
        recovery_required = bool(connection.get('recovery_required'))
        clock_fault = bool(connection.get('clock_fault'))
        if open_cycles or active_orders or external_positions or recovery_required or clock_fault:
            total_lots = sum(int(row.get('remaining_quantity', 0)) for row in open_cycles)
            detail = (
                f'当前后台交易服务仍在运行：\n'
                f'· 自有持仓周期：{len(open_cycles)} 个（共 {total_lots} 手）\n'
                f'· 未完结委托：{len(active_orders)} 笔\n'
                f'· 外部持仓差异：{len(external_positions)} 项'
            )
            if recovery_required or clock_fault:
                detail += '\n· 当前存在连接/持仓核对或时钟异常'
            detail += (
                '\n\n严格安全停机需先执行「全部平仓并停止」，等待所有自有持仓平清、活动撤单确认且后台 '
                'Runtime 与视图进程完全退出后才会关闭界面。\n\n是否执行全部平仓并安全退出？'
            )
            answer = QMessageBox.warning(
                self,
                '确认安全平仓并退出 OnlineTrader',
                detail,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False

        ok, stop_error = self._run_runtime_shutdown(
            shutdown_runtime_and_wait, pid=pid, view=view,
            title='OnlineTrader · 安全退出',
            label='正在停止后台交易服务并等待进程完全退出…',
        )
        if not ok:
            QMessageBox.critical(
                self,
                '交易服务仍在运行，未完成退出',
                f'{stop_error or "未能确认后台交易服务退出。"}\n\n为确保账户和持仓安全，桌面工作台保持打开，请检查实时持仓与状态后再试。',
            )
            return False
        self._runtime_shutdown_verified = True
        return True

    def _run_runtime_shutdown(self, shutdown_fn, *, pid=None, view=None, title, label):
        progress = QProgressDialog(label, '', 0, 0, self)
        progress.setWindowTitle(title)
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        self._runtime_shutdown_in_progress = True
        outcome = [False, None]
        done = [False]
        loop = QEventLoop(self)
        worker = _RuntimeShutdownWorker(
            shutdown_fn, self.pipe_name, pid=pid, view=view, timeout=20.0
        )

        def finished(ok, stop_error):
            outcome[0] = ok
            outcome[1] = stop_error
            done[0] = True
            loop.quit()

        worker.completed.connect(finished)
        worker.finished.connect(loop.quit)
        try:
            worker.start()
            while not done[0]:
                loop.exec()
                if not done[0] and worker.isFinished():
                    QApplication.processEvents()
                    if not done[0]:
                        break
            worker.wait()
        finally:
            self._runtime_shutdown_in_progress = False
            progress.close()
        return tuple(outcome)

    def save_runtime_parameters(self, candidate_profile):
        if not isinstance(candidate_profile, DsvProfile):
            return False
        settings = candidate_profile.to_record()['settings']
        symbols = settings['symbols']
        if not 1 <= len(symbols) <= 11:
            message = '监控合约数量必须在 1 到 11 个之间，请调整后再保存。'
            self.dashboard.set_parameter_feedback(message, error=True)
            QMessageBox.warning(self, '保存策略参数', message)
            return False
        if settings['trade']['max_volume'] <= 0 or not 0 < settings['trade']['max_daily_orders'] <= 2147483647:
            message = '请在交易参数中设置有效的最大手数和每品种最大报单数。'
            self.dashboard.set_parameter_feedback(message, error=True)
            QMessageBox.warning(self, '保存策略参数', message)
            return False

        current_profile = self.dashboard.profile
        current_settings = current_profile.to_record()['settings']
        new_codes = [row['code'] for row in symbols]
        old_codes = [row['code'] for row in current_settings['symbols']]
        added = [row for row in symbols if row['code'] not in set(old_codes)]
        removed = [row for row in current_settings['symbols'] if row['code'] not in set(new_codes)]
        startup_only_changed = (
            settings['schedule'] != current_settings['schedule']
            or any(settings['trade'][field] != current_settings['trade'][field]
                   for field in ('holidays', 'night_session_close'))
        )

        if not added and not removed and not startup_only_changed:
            if candidate_profile.content_hash == current_profile.content_hash:
                if self.dashboard._saved_next_boot_hash is not None:
                    store, _ = self._portable_store_and_account()
                    if store is None:
                        message = '未能恢复启动配置：当前未挂载可写配置存储。'
                        self.dashboard.set_parameter_feedback(message, error=True)
                        QMessageBox.critical(self, '保存配置失败', message)
                        return False
                    try:
                        store.save_profile(candidate_profile)
                    except (TraderError, OSError) as error:
                        message = f'未能恢复启动配置：{error}'
                        self.dashboard.set_parameter_feedback(message, error=True)
                        QMessageBox.critical(self, '保存配置失败', message)
                        return False
                    self.dashboard.clear_next_boot_saved(candidate_profile)
                    self.dashboard.set_parameter_feedback('已将启动配置恢复为当前运行配置。')
                    return True
                self.dashboard.set_parameter_feedback('当前参数与运行中一致，无需重复保存。')
                return True
            self.send('SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                      {'profile': candidate_profile.to_record()})
            return True

        view = self.view
        fresh_view = (self._runtime_mode and bool(self.pipe_name) and view is not None
                      and not self._stale and not self.published_snapshot_expired()
                      and not view.get('truncated'))
        live_allowed = bool(view and (view.get('configuration') or {}).get('live_parameter_save'))
        if (added and not removed and not startup_only_changed and fresh_view
                and live_allowed and self._runtime_command_bus is not None):
            self._pending_focus_contracts = tuple(row['code'] for row in added)
            self._pending_hot_add_profile = candidate_profile
            self.send('SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                      {'profile': candidate_profile.to_record()})
            return True

        store, _ = self._portable_store_and_account()
        if store is None:
            message = '当前连接未绑定可写 Runtime 配置目录，无法保存新增合约。\n请通过带 runtime 目录的正常入口启动工作台。'
            self.dashboard.set_parameter_feedback('未绑定可写 Runtime 目录，无法持久化启动配置。', error=True)
            QMessageBox.warning(self, '保存策略参数', message)
            return False

        changes = []
        if added:
            changes.append('新增合约：' + '、'.join(f"{row['name']}({row['code']})" for row in added))
        if removed:
            changes.append('移除合约：' + '、'.join(f"{row['name']}({row['code']})" for row in removed))
        if startup_only_changed:
            changes.append('已修改交易日历或定时设置')
        summary = '\n'.join(f'· {line}' for line in changes)

        resp = (view or {}).get('responsibility')
        busy_reasons = []
        if not fresh_view:
            busy_reasons.append('当前后台状态快照未就绪或已过期')
        if isinstance(resp, Mapping):
            if not resp.get('maintenance_allowed'):
                blockers = [str(code) for code in (resp.get('blockers') or ())]
                busy_reasons.append('存在未结交易责任或异常：' + '、'.join(blockers or ['NOT_READY']))
        else:
            open_cycles = [row for row in (view or {}).get('cycles', ()) if row.get('remaining_quantity', 0) > 0]
            active_orders = [row for row in (view or {}).get('orders', ()) if row.get('potential_remaining', 0) > 0]
            external_positions = (view or {}).get('external_positions') or []
            conn = (view or {}).get('connection') or {}
            unaligned_positions = [
                row for row in (conn.get('account_positions') or ())
                if (not row.get('configured') and (row.get('actual_long') or row.get('actual_short')))
                or row.get('excess_long') or row.get('excess_short')
            ]
            armed = bool(view and view.get('entry_authority') == 'ARMED')
            if armed:
                busy_reasons.append('策略当前已启用开仓（请先按 F6 停止策略）')
            if open_cycles:
                busy_reasons.append(f'存在 {len(open_cycles)} 个未平仓持仓周期')
            if active_orders:
                busy_reasons.append(f'存在 {len(active_orders)} 笔未终结委托')
            if external_positions or unaligned_positions or conn.get('recovery_required') or conn.get('clock_fault'):
                busy_reasons.append('存在未收敛持仓或连接/时钟异常')

        if busy_reasons:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle('保存为下次启动配置')
            box.setText(
                f'{summary}\n\n由于{"、".join(busy_reasons)}，当前不能立即重启后台服务。\n'
                '是否先将以上修改保存到本地启动配置（OnlineTrader-overrides.json），待下次安全重启后生效？'
            )
            save_btn = box.addButton('保存为下次启动配置', QMessageBox.ButtonRole.AcceptRole)
            box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not save_btn:
                return False
            return self._persist_next_boot_profile(store, candidate_profile)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle('保存并应用合约变更')
        box.setText(
            f'{summary}\n\n以上变更需要重启后台交易服务，以重新加载完整交易日历并订阅行情。\n'
            '当前处于安全停开且空仓状态，建议立即重启使其在监控面板生效。'
        )
        restart_btn = box.addButton('保存并立即重启生效', QMessageBox.ButtonRole.AcceptRole)
        later_btn = box.addButton('仅保存，下次启动生效', QMessageBox.ButtonRole.ActionRole)
        box.addButton('取消', QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is later_btn:
            return self._persist_next_boot_profile(store, candidate_profile)
        if clicked is not restart_btn:
            return False
        return self._restart_runtime_with_profile(
            store, candidate_profile, focus_contracts=[row['code'] for row in added]
        )

    def _persist_next_boot_profile(self, store, candidate_profile):
        try:
            store.save_profile(candidate_profile)
        except (TraderError, OSError) as error:
            message = f'未能保存启动配置：{error}'
            self.dashboard.set_parameter_feedback(message, error=True)
            QMessageBox.critical(self, '保存配置失败', message)
            return False
        self.dashboard.mark_next_boot_saved(candidate_profile)
        self.show_home_feedback('已保存新合约到启动配置（OnlineTrader-overrides.json），重启后台服务后生效。')
        return True

    def _restart_runtime_with_profile(self, store, candidate_profile, *, focus_contracts=()):
        if self._runtime_shutdown_in_progress or self._runtime_bootstrap is not None:
            return False
        from online_trader.desktop.runtime_bootstrap import BootstrapController
        from online_trader.runtime.shutdown import maintenance_stop_runtime_and_wait

        view, pid, inspect_error = self._inspect_runtime_for_maintenance()
        if inspect_error is not None:
            QMessageBox.warning(
                self,
                '后台服务状态待核对',
                f'{inspect_error}\n\n为确保账户安全，原配置与监控状态保持不变。',
            )
            return False
        if view is not None or self.pipe_name:
            ok, stop_error = self._run_runtime_shutdown(
                lambda pipe, **kw: maintenance_stop_runtime_and_wait(
                    pipe, reason='RESTART_CONFIG', **kw,
                ),
                pid=pid, view=view or self.view,
                title='OnlineTrader · 应用新合约配置',
                label='正在安全停止当前后台服务并保存新合约配置…',
            )
            if not ok:
                QMessageBox.critical(
                    self,
                    '后台服务未能安全停止',
                    f'{stop_error or "未能确认原后台服务安全退出。"}\n\n为确保账户安全，原配置与监控状态保持不变。',
                )
                return False
        try:
            store.save_profile(candidate_profile)
        except (TraderError, OSError) as error:
            QMessageBox.critical(
                self,
                '保存新配置失败',
                f'后台服务已停止，但写入本地配置失败：{error}',
            )
            return False

        self._detach_runtime_client()
        self.view = None
        self._boot = None
        self._stale = True
        self.snapshot_published_ns = None
        self._publication_after_ns = 0
        self.receipt_identity = None
        self._control_notice = None
        self._last_account_record = {}
        self._base_cycle_ids = []
        self._pending_focus_contracts = tuple(focus_contracts)
        self.dashboard.reset_account_view(preserve_logs=True)
        self.dashboard.set_parameter_feedback('配置已保存，正在重启后台服务并同步最新合约…')
        self.show_home_feedback('正在重启后台交易服务并初始化新合约订阅…')

        controller = BootstrapController(store.directory, account=self._runtime_account, parent=self)
        self._runtime_bootstrap = controller

        def cleanup():
            if self._runtime_bootstrap is controller:
                self._runtime_bootstrap = None
            controller.deleteLater()

        def on_ready(new_pipe):
            cleanup()
            if self._closing:
                return
            self._attach_runtime_pipe(new_pipe)
            self.dashboard.set_parameter_feedback('后台服务已重启，新合约已同步到监控面板。')
            self.show_home_feedback('新合约配置已生效，正在获取最新行情。')

        def on_failed(error):
            cleanup()
            message = f'{error}\n\n错误代码：{getattr(error, "code", "E_RUNTIME_START")}'
            self.dashboard.set_parameter_feedback('后台重启失败，请核对新合约代码或网络后重试。', error=True)
            QMessageBox.critical(self, '重启交易服务失败', message)

        def on_cancelled():
            cleanup()
            self.dashboard.set_parameter_feedback('已取消等待后台重启；新配置已保存，下次启动生效。')
            self.show_home_feedback('已取消等待重启；新合约配置已保存，下次启动时生效。')

        controller.ready.connect(on_ready)
        controller.failed.connect(on_failed)
        controller.cancelled.connect(on_cancelled)
        controller.start()
        return True

    def closeEvent(self,event):
        if self._runtime_shutdown_in_progress:
            event.ignore()
            return
        if self._runtime_bootstrap is not None:
            self._runtime_bootstrap.shutdown()
            self._runtime_bootstrap = None
        if self._strategy_starter is not None and self._strategy_starter.is_preparing():
            self._closing = True
            self.hide()
            event.ignore()
            return
        if self._session_dialog is not None and getattr(self._session_dialog, 'is_preparing', lambda: False)():
            self._closing = True
            self.hide()
            self._session_dialog.hide()
            event.ignore()
            return
        self._tray.save_geometry()
        if not self._closing and self._tray.minimize_on_close():
            event.ignore()
            return
        if not self._confirm_and_stop_runtime():
            self._tray.cancel_exit()
            event.ignore()
            return
        if self._runtime_coordinator is not None:
            self._runtime_coordinator.close()
        if self.monitor_worker is not None and self.monitor_worker.isRunning():
            self._closing = True
            self.hide()
            self.monitor_worker.requestInterruption()
            event.ignore()
            return
        if self.dashboard.quote_worker is not None and self.dashboard.quote_worker.isRunning():
            self._closing = True
            self.hide()
            self.dashboard.quote_worker.requestInterruption()
            event.ignore()
            return
        if self._session_dialog is not None:
            self._session_dialog.clear_scheduled_start()
        if self._strategy_starter is not None:
            self._strategy_starter.status_timer.stop()
        self.cancel_timed_start()
        self.monitor_timer.stop()
        self.connection_retry_timer.stop()
        self.dashboard.log_timer.stop()
        if hasattr(self.dashboard, 'hang_monitor'):
            self.dashboard.hang_monitor.stop()
        self.timer.stop()
        self._finalize_diagnostic_sink(clean_shutdown=True)
        super().closeEvent(event)

    def _finalize_diagnostic_sink(self, *, clean_shutdown=False):
        """Flush and release the optional Workbench evidence sink idempotently."""
        holder = getattr(self, '_diagnostic_sink_holder', None)
        if holder is not None:
            holder['sink'] = None
        hook = getattr(self, '_diagnostic_excepthook', None)
        previous = getattr(self, '_diagnostic_previous_excepthook', None)
        if hook is not None and sys.excepthook is hook and previous is not None:
            sys.excepthook = previous
        self._diagnostic_excepthook = None
        sink = getattr(self, '_diagnostic_sink', None)
        if sink is None:
            return
        self._diagnostic_sink = None
        if clean_shutdown:
            sink.record(
                'WORKBENCH_STOPPED', component='LIFECYCLE', priority='CRITICAL',
                runtime_boot_id=self._boot,
                account_pseudonym=self._diagnostic_account_pseudonym,
                payload={'runtime_shutdown_verified': bool(self._runtime_shutdown_verified)},
            )
        sink.finalize(
            clean_shutdown=clean_shutdown, snapshot_available=self.view is not None, timeout=1.0)

    def prepare_update_restart(self):
        if (self._startup_in_progress or self._worker is not None
                or self._runtime_bootstrap is not None
                or self._runtime_shutdown_in_progress
                or (self._runtime_command_bus is not None and self._runtime_command_bus.has_inflight)
                or (self._strategy_starter is not None and self._strategy_starter.is_preparing())
                or (self._session_dialog is not None
                    and getattr(self._session_dialog, 'is_preparing', lambda: False)())):
            raise TraderError('E_ACCOUNT_BUSY', '请等待当前账户操作结束后再更新')
        if not self._runtime_mode:
            return
        from online_trader.runtime.shutdown import maintenance_stop_runtime_and_wait
        view, pid, inspect_error = self._inspect_runtime_for_maintenance()
        if inspect_error is not None:
            raise TraderError('E_RUNTIME_STOP', inspect_error)
        if view is not None:
            resp = view.get('responsibility')
            if isinstance(resp, Mapping) and not resp.get('maintenance_allowed'):
                blockers = '、'.join(str(code) for code in (resp.get('blockers') or ())) or '存在未结交易责任'
                raise TraderError(
                    'E_NOT_READY',
                    f'当前存在未结交易责任或策略仍在运行（{blockers}），请先停止策略并结清持仓/委托后再更新',
                )
            ok, stop_error = self._run_runtime_shutdown(
                lambda pipe, **kw: maintenance_stop_runtime_and_wait(
                    pipe, reason='UPDATE', **kw,
                ),
                pid=pid,
                view=view,
                title='OnlineTrader · 准备软件更新',
                label='正在核对账户无未结责任并安全退出后台服务…',
            )
            if not ok:
                raise TraderError('E_RUNTIME_STOP', stop_error or '未能确认后台交易服务安全退出')
        self._update_restart_in_progress = True
        self._runtime_shutdown_verified = True


def run(pipe_name,*,store=None,account_id=None,paired_launcher=None,dsv_config=None,autostart=False,
        update_root=None, notification=None, runtime_view=False, activation_server=None):
    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    restart_for_update = False
    window = None

    def request_restart():
        nonlocal restart_for_update
        window.prepare_update_restart()
        restart_for_update = True
        window._tray.close_window()

    window=WorkbenchWindow(pipe_name,store=store,account_id=account_id,paired_launcher=paired_launcher,
                           dsv_config=dsv_config,use_desktop_dsv=not runtime_view,autostart=autostart,
                           notification=notification,runtime_view=runtime_view,update_root=update_root,
                           update_restart=request_restart if update_root is not None else None)
    window.setAttribute(Qt.WA_DeleteOnClose)
    window.destroyed.connect(app.quit)
    if activation_server is not None:
        activation_server.activation_requested.connect(window._tray.show_window)
    window.show();QTimer.singleShot(0,window.refresh)
    result = app.exec()
    if restart_for_update:
        from online_trader.update_install import RESTART_FOR_UPDATE
        return RESTART_FOR_UPDATE
    return result
