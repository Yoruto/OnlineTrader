"""Operator messages from accepted DSV decisions and committed execution facts."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import Future
from pathlib import Path
import os
import sqlite3
from threading import Thread
import tempfile
import time

from online_trader.domain.orders import OrderState

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.errors import TraderError, exception_record
from online_trader.gateway.dsv_facts import utc


BEIJING = timezone(timedelta(hours=8))
TIMING_CHECKS = {'OPENING_WAIT', 'CLOSE_SESSION_NO_OPEN', 'DSV_QUOTE_AGE_EXCEEDED'}
WAIT_REASONS = {'OBSERVATION_ONLY_BACKLOG', 'TRIGGER_NOT_REACHED', 'DUPLICATE_MARKET_EVENT',
                'OUT_OF_ORDER_MARKET_EVENT', 'OUT_OF_ORDER_SOURCE_TIME'}
BLOCK_MESSAGES = {
    'ENTRY_AUTHORITY_DISARMED': '尚未启用策略开仓',
    'ENTRY_RESUME_MARKET_PATH': '重连后等待连续实时行情及日内区间',
    'ENTRY_RESUME_NATIVE_QUOTE': '重连后等待当前盘次的有效原生报价',
    'ENTRY_RESUME_QUOTE_AGE_POLICY': '重连报价年龄超过开仓配置值',
    'ENTRY_RESUME_ACCOUNT_SYNC': '重连后等待柜台状态核清及原权限恢复',
    'RECONCILIATION_REQUIRED': '等待柜台委托和持仓核对',
    'CONTROL_BARRIER': '操作或维护限制新增',
    'STRATEGY_EPOCH_REVOKED': '策略运行身份失效',
    'SCHEDULE_CURRENT_SESSION_UNPROVEN': '当前交易时段待核实',
    'OWNED_EXPOSURE_EXISTS': '当前品种已有仓位或未结开仓单',
    'ACCOUNT_VALUES_UNKNOWN': '账户资金字段缺失',
    'PRICE_LIMITS_UNKNOWN': '涨跌停价格缺失',
    'MARKET_PATH_INCOMPLETE': '当前盘段行情路径不完整',
    'CANDLE_OPPORTUNITY_CONSUMED': '当前分钟机会已使用',
    'QUOTE_NOT_TICK_ALIGNED': '报价未对齐最小变动价位',
    'LEGACY_MINUTE_CONSUMPTION_UNPROVED': '旧记录的分钟消费来源待核实，等待下一根实时分钟',
    'ENTRY_RECHECK_QUOTE_MISSING': '复核时缺少最新行情',
    'ENTRY_RECHECK_ACCOUNT_CHANGED': '复核时账户数据版本已变化',
    'ENTRY_RECHECK_QUOTE_STALE': '复核时行情已过期',
    'ENTRY_RECHECK_CONFIGURATION_CHANGED': '复核时策略配置已变化',
    'ENTRY_RECHECK_SESSION_CHANGED': '复核时交易时段已变化',
    'ENTRY_RECHECK_PATH_INCOMPLETE': '复核时行情路径不完整',
    'ENTRY_RECHECK_INSTRUMENT_CHANGED': '复核时合约乘数或最小价位已变化',
    'ENTRY_RECHECK_CANDIDATE_CHANGED': '复核时方向、触发价或报单价已变化',
    'ENTRY_RECHECK_QUANTITY_CHANGED': '复核时可开手数已变化',
    'DSV_QUOTE_AGE_EXCEEDED': '报价年龄超过配置值',
    'E_NATIVE_QUOTE_NOT_CURRENT': '原生报价已变动或无有效价量',
    'E_GATEWAY_SYNC': '当前柜台数据未同步',
    'E_ENTRY_WINDOW_EXPIRED': '已超过开仓授权时间',
    'E_ENTRY_ATTEMPT_LIMIT': '本次授权的子单额度已用完',
}
DAY_STOPS = {'DAY_STOP_ACTIVE', 'MONTH_STOP_ACTIVE', 'PROTECTION_DAY_STOP', 'DSV_PERSISTED_DAY_OR_MONTH_STOP'}
ENTRY_FILTERS = {
    'STOP_COOLDOWN': ('跳过', '止损后同方向冷却中，本次不开仓', 300),
    'STOP_TRIGGER_UNCHANGED': ('跳过', '触发价仍是止损时的触发价（未出现新高低点），本次不开仓', 300),
    'HYSTERESIS_NOT_READY': ('跳过', '等待价格反向达到滞回点，本次不开仓', None),
    'FIRST_MINUTE_JUMP_DAY_STOP': ('风控', '开盘首分钟跳空，当日停止开仓', 'day'),
    'TAKE_PROFIT_OUTSIDE_PRICE_LIMIT': ('风控', '理论止盈超过对应涨跌停，无法触及，跳过开仓', None),
    'LAST_NEAR_PRICE_LIMIT': ('风控', '最新价接近涨跌停，跳过开仓', None),
    'SAME_DIRECTION_WITHIN_ONE_SECOND': ('风控', '同方向在 1 秒内重复信号，已忽略', None),
    'INSUFFICIENT_MARGIN': ('风控', '资金不足，跳过开仓', None),
    'AVAILABLE_NOT_POSITIVE': ('风控', '可用资金不足，跳过开仓', None),
    'EFFECTIVE_RISK_CAP': ('风控', '账户风险度已达上限，跳过开仓', None),
    'LIVE_DAILY_LOSS_LIMIT': ('风控', '实盘日亏已达上限，停止开仓', 'day'),
    'DAILY_ORDER_LIMIT': ('规则', '报单次数已达上限，当日停止开仓', 'day'),
    'EXPIRY_ENTRY_BLOCK': ('规则', '合约临近到期，停止开仓', 'day'),
    'ACTIVE_SYMBOL_LIMIT': ('风控', '已达持仓品种数上限，跳过开仓', None),
}
EXIT_REASONS = {
    'STOP_LOSS': '止损', 'TAKE_PROFIT': '止盈', 'PROTECTED_FLOOR': '80%保护',
    'TRAILING_STOP': '移动止盈', 'BREAK_EVEN': '保本', 'SESSION_CLOSE': '收盘平仓',
    'ADVERSE_SLIPPAGE': '开仓滑点超限强平', 'DATA_GAP_EXIT': '行情断流强平',
    'MANUAL_EXIT': '手动平仓',
}
SESSION_EVENTS = {
    'ONLINE_SIMULATION_ARMED': ('执行', '已启动策略'),
    'DSV_STRATEGY_STOPPED': ('执行', '已停止开仓'),
    'ONLINE_DSV_STOP_VERIFIED': ('执行', '账户仓位及活动委托已核清，策略停止'),
    'CONTROL_PAUSED': ('执行', '已暂停开仓'),
    'ONLINE_GATEWAY_DISCONTINUITY': ('警告', '行情或交易连接中断'),
    'ONLINE_DSV_GATEWAY_RESYNCED': ('执行', '连接已恢复，柜台状态已同步'),
    'ONLINE_DSV_ENTRY_RECOVERY_PENDING': ('等待', '开仓暂挂，等待柜台核对与实时行情'),
    'ONLINE_DSV_ENTRIES_RESUMED': ('恢复', '柜台和行情已核清，恢复原开仓权限'),
    'SCHEDULER_FACT_BARRIER': ('警告', '时钟或盘次证据异常，原持仓目标待核对'),
    'CLOCK_RESET_REQUIRES_FRESH_FACTS': ('恢复', '时钟已重新核对，等待新行情及 F5 启动'),
    'ONLINE_DSV_DAY_CHANGE_REQUIRES_RECONCILIATION': ('警告', '柜台换日，等待核对原持仓及委托'),
    'ONLINE_DSV_FACT_BARRIER': ('警告', '柜台事实待核对'),
    'ONLINE_ARCHIVED_ORDER_CHANGED': ('警告', '历史委托收到不同的柜台回报，已暂停新增并保留原委托身份'),
    'ONLINE_NATIVE_ORDER_REJECTION_CONFLICT': ('警告', '柜台拒单标记与委托或成交数量冲突，等待核对'),
}
COMMITTED_EVENT_TYPES = frozenset(SESSION_EVENTS) | {
    'ENTRY_PREPARED', 'EXIT_PREPARED', 'REPLACEMENT_PREPARED', 'FILL_CONFIRMED',
    'SEND_RESULT', 'SEND_ABORTED', 'ORDER_TERMINAL', 'EXECUTION_CANCEL_TIMEOUT',
    'CANCEL_PREPARED', 'CANCEL_RESULT', 'CANCEL_UNSENT', 'ONLINE_SIMULATION_RECONCILED',
    'ONLINE_WORKING_ORDER_PROVED', 'REPLACEMENT_BLOCKED', 'NATIVE_ACCOUNT_CANCEL_RESULTS',
    'CONTROL_CANCEL_RESULT', 'CONTROL_CANCEL_ERRORS', 'SCHEDULER_FACT_BARRIER', 'CLOCK_RESET_REQUIRES_FRESH_FACTS',
    'EXECUTION_OBSERVATION', 'PROTECTION_OBSERVATION', 'SCHEDULED_DATA_GAP', 'DSV_PARAMETERS_SAVED',
}
OPERATION_EVENT_TYPES = tuple(sorted(COMMITTED_EVENT_TYPES | {'DSV_STRATEGY_OBSERVED'}))


def publish_log_recovery(path, record):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='operation-log-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(canonical_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class DsvEntryLog:
    def __init__(self, kernel, output, profile, *, boot_id=None, from_seq=None):
        self.kernel = kernel
        self.path = Path(output) / 'strategy-log.jsonl'
        self.boot_id = boot_id or kernel.boot_id
        self.start_seq = kernel._durable_seq if from_seq is None else from_seq
        self.source = kernel.ledger
        self.last_event_seq = self.start_seq
        self.recovery_path = Path(output).with_name(Path(output).name + '.operation-log-recovery.json')
        self.registry = self.source.path.with_name(self.source.path.name + '.operation-log-recovery')
        self.registry_path = self.registry / (self.boot_id + '.json')
        self.recovery = None
        self.next_recovery = 0
        self._rebuilding = None
        self._prior_runs = []
        self._prior_rebuild = None
        self._next_prior_recovery = 0
        self._recovered_receipt = None
        self._target = self.path
        self._file_state = None
        self._event = None
        self._pending = None
        settings = profile.to_record()['settings']
        self.profile = profile
        self._identity = {'account_key': kernel.account_key, 'kernel_boot_id': self.boot_id,
                          'profile_hash': profile.content_hash}
        self._run_record = self._identity | {'run_directory': str(Path(output).resolve()),
                                            'from_event_seq': str(self.start_seq)}
        self.trade = settings['trade']
        self.symbols = {row['code']: row for row in settings['symbols']}
        self.geometry_days = set()
        self.last_time = {}
        self.last_hint = {}
        self.active_blocks = {}
        self.cycle_markers = {}
        self.order_markers = {}
        self.last_slow_loop_ns = None

    def start(self):
        self.path.touch(exist_ok=False)
        self._verify_file(opening=True)
        self.registry.mkdir(exist_ok=True)
        publish_log_recovery(self.registry_path, self._run_record |
                             {'schema_version': 'DSV_OPERATION_LOG_RECOVERY/1', 'status': 'TRACKING'})

    def registration(self):
        return self._run_record.copy()

    def find_prior_failures(self):
        candidates = {path.stem: (path, None) for path in self.registry.glob('boot-*.json')}
        directory = self.path.parent.resolve()
        root = directory.parent
        # Older builds did not register the directory in SQLite. Only look
        # for run identity files in the same CLI or desktop launch layout.
        if directory.name == 'run' and root.parent.name == 'desktop-starts':
            root = root.parent
            identities = root.glob('*/run/run-identity.json')
        else:
            identities = root.glob('*/run-identity.json')
        for path in identities:
            identity = self._read_recovery_record(path)
            if (identity is None or identity.get('schema_version') != 'DSV_RUN_IDENTITY/1'
                    or identity.get('account_key') != self.kernel.account_key):
                continue
            boot_id = identity.get('kernel_boot_id')
            if not isinstance(boot_id, str) or not boot_id.startswith('boot-') or boot_id == self.boot_id:
                continue
            try:
                run = path.parent.resolve(strict=True)
                if not run.is_relative_to(root):
                    continue
                if identity.get('run_directory') != str(run):
                    continue
            except (OSError, ValueError, RuntimeError):
                continue
            registered = candidates.get(boot_id, (None, None))[0]
            candidates[boot_id] = (registered, {'run_directory': str(run)})
        for row in self.source.query(
                "SELECT kernel_boot_id,payload_json FROM event_journal WHERE event_type='DSV_OPERATION_LOG_STARTED'"):
            boot_id = row['kernel_boot_id']
            try:
                data = strict_loads(row['payload_json'])['data']
                if ((data['account_key'], data['kernel_boot_id']) != (self.kernel.account_key, boot_id)
                        or boot_id == self.boot_id):
                    continue
                registered = candidates.get(boot_id, (None, None))[0]
                candidates[boot_id] = (registered, data)
            except (ValueError, KeyError, TypeError):
                continue
        for boot_id, (registered, registration) in candidates.items():
            if boot_id == self.boot_id or self.source.query(
                    'SELECT account_key FROM kernel_boots WHERE kernel_boot_id=?', (boot_id,)) != [
                        {'account_key': self.kernel.account_key}]:
                continue
            try:
                record = self._read_recovery_record(registered) if registered is not None else None
                if not self._matches_recovery(record, boot_id, registration):
                    record = None
                if registration is not None:
                    run = Path(registration['run_directory']).resolve(strict=True)
                    # A run path comes from the original ledger or launch
                    # layout, before the fallback receipt is opened.
                    receipt = self._read_recovery_record(run.with_name(run.name + '.operation-log-recovery.json'))
                    if (receipt is not None and receipt.get('finalized') is True
                            and self._matches_recovery(receipt, boot_id, registration)):
                        continue
                    if (self._matches_recovery(receipt, boot_id, registration)
                            and (record is None or record.get('status') == 'TRACKING')
                            and (receipt.get('status') != 'RECOVERED' or receipt.get('finalized') is False)):
                        record = receipt
                if record is None or record.get('finalized') is True:
                    continue
                if 'observed_at_utc' in record:
                    record['observed_at_utc'] = utc(datetime.fromisoformat(record['observed_at_utc']).astimezone(timezone.utc))
                run = Path(record['run_directory']).resolve(strict=True)
                if not run.is_dir() or run == directory or str(run) != record['run_directory']:
                    continue
                identity_path = run / 'run-identity.json'
                if identity_path.is_file():
                    identity = self._read_recovery_record(identity_path)
                    if (identity is None or identity.get('schema_version') != 'DSV_RUN_IDENTITY/1'
                            or (identity.get('account_key'), identity.get('kernel_boot_id'), identity.get('run_directory'))
                                != (self.kernel.account_key, boot_id, str(run))):
                        continue
                from_seq = int(record['from_event_seq'])
                if not self.source.query('SELECT 1 FROM event_journal WHERE kernel_boot_id=? AND event_seq=?',
                                         (boot_id, from_seq)):
                    continue
                profile = self.profile
                if record['profile_hash'] != profile.content_hash:
                    from online_trader.configuration.dsv import DsvProfile
                    rows = self.source.query(
                        "SELECT payload_json FROM config_revisions WHERE created_event_seq<=? "
                        "AND json_extract(payload_json,'$.profile_hash')=? ORDER BY config_revision DESC LIMIT 1",
                        (from_seq, record['profile_hash']))
                    if not rows:
                        continue
                    profile = DsvProfile.from_record(strict_loads(rows[0]['payload_json'])['profile'])
                old = DsvEntryLog(self.kernel, run, profile, boot_id=boot_id, from_seq=from_seq)
                self._prior_runs.append((old, record))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                continue

    def _matches_recovery(self, record, boot_id, registration):
        return (record is not None and record.get('schema_version') == 'DSV_OPERATION_LOG_RECOVERY/1'
                and (record.get('account_key'), record.get('kernel_boot_id')) == (self.kernel.account_key, boot_id)
                and record.get('status') in ('DEGRADED', 'REBUILDING', 'RECOVERED', 'TRACKING')
                and all(record.get(key) == value for key, value in (registration or {}).items()))

    @staticmethod
    def _read_recovery_record(path):
        try:
            with path.open('rb') as stream:
                raw = stream.read(65_537)
            data = strict_loads(raw) if len(raw) <= 65_536 else None
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _recover_prior_run(self):
        old, record = self._prior_runs[0]
        try:
            if self._prior_rebuild is None:
                through = self.source.query('SELECT MAX(event_seq) AS seq FROM event_journal WHERE kernel_boot_id=?',
                                            (old.boot_id,))[0]['seq']
                if record['status'] == 'TRACKING':
                    if not self.source.query(
                            'SELECT 1 FROM event_journal WHERE kernel_boot_id=? AND event_seq>? LIMIT 1',
                            (old.boot_id, old.start_seq)):
                        old.registry_path.unlink(missing_ok=True)
                        self._prior_runs.pop(0)
                        return
                    record = record | {'status': 'REBUILDING', 'first_event_seq': str(old.start_seq + 1),
                                       'observed_at_utc': utc(datetime.now(timezone.utc)),
                                       'auxiliary_rows_unavailable': True}
                elif record['status'] == 'RECOVERED':
                    previous = int(record['recovered_through_event_seq'])
                    record = record | {
                        'first_event_seq': str(previous + 1 if previous < through else old.start_seq + 1),
                        'observed_at_utc': utc(datetime.now(timezone.utc)),
                        'first_decision_id': None, 'operation': 'recover_interrupted_run',
                    }
                record = record | {'status': 'REBUILDING', 'through_event_seq': str(through)}
                self._prior_runs[0] = (old, record)
                old.recovery = record
                old._persist_recovery()
                self._prior_rebuild = self._start_rebuild(old, through)
            elif self._prior_rebuild.done():
                replacement = self._prior_rebuild.result()
                old.recovery = record | {'status': 'RECOVERED', 'recovered_through_event_seq': str(replacement.last_event_seq)}
                old._finish_recovery(archive=True)
                self._prior_runs.pop(0)
                self._prior_rebuild = None
        except (OSError, sqlite3.Error, TraderError, ValueError, KeyError, TypeError):
            # Retry this archive later without preventing the current run's log.
            self._prior_runs.append(self._prior_runs.pop(0))
            self._prior_rebuild = None
            self._next_prior_recovery = time.monotonic() + 30

    @staticmethod
    def _signature(stream):
        stat = os.fstat(stream.fileno())
        stream.seek(max(0, stat.st_size - 128))
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stream.read(128)

    def _verify_file(self, *, opening=False):
        with self.path.open('rb') as stream:
            signature = self._signature(stream)
        if not opening and self._file_state != signature:
            raise TraderError('E_OPERATION_LOG_CHANGED', 'Operation log was truncated, replaced or changed')
        self._file_state = signature

    def _events(self, through, *, query=None):
        select = query or self.source.query
        placeholders = ','.join('?' for _ in OPERATION_EVENT_TYPES)
        return select(
            'SELECT event_seq,event_type,observed_at_utc,payload_json FROM event_journal '
            f'WHERE kernel_boot_id=? AND event_seq>? AND event_seq<=? AND event_type IN ({placeholders}) '
            'ORDER BY event_seq LIMIT 256',
            (self.boot_id, self.last_event_seq, through, *OPERATION_EVENT_TYPES))

    def _consume(self, row):
        kind = row['event_type']
        data = strict_loads(row['payload_json'])
        data = data.get('data', data)
        if kind == 'DSV_PARAMETERS_SAVED':
            from online_trader.configuration.dsv import DsvProfile
            settings = DsvProfile.from_record(data['dsv_execution']['profile']).to_record()['settings']
            self.trade = settings['trade']
            self.symbols = {item['code']: item for item in settings['symbols']}
        report = data.get('report', {})
        strategy = (data.get('cycle') or {}).get('strategy_metadata') or {}
        self._event = row['event_seq'], report.get('decision_id') or strategy.get('decision_id')
        if (market := report.get('market_log')) is not None:
            self.market(market, report)
        if kind in COMMITTED_EVENT_TYPES:
            self.committed(kind, row['event_seq'], row['observed_at_utc'], data)
        self._event = None
        self._pending = None

    def poll(self):
        if self.recovery is not None:
            if time.monotonic() < self.next_recovery:
                return
            self._recover()
            return
        self._verify_file()
        through = self.kernel._durable_seq
        rows = self._events(through)
        for row in rows:
            self._consume(row)
            self.last_event_seq = row['event_seq']
        if len(rows) < 256:
            self.last_event_seq = through
        if self._prior_runs and time.monotonic() >= self._next_prior_recovery:
            self._recover_prior_run()

    def _persist_recovery(self):
        try:
            self.registry.mkdir(exist_ok=True)
            publish_log_recovery(self.registry_path, self.recovery)
        except OSError:
            publish_log_recovery(self.recovery_path, self.recovery)
        else:
            try:
                publish_log_recovery(self.recovery_path, self.recovery)
            except OSError:
                pass

    def _finish_recovery(self, *, archive):
        # Keep the original run registered until its normal close persists
        # the tail. A recovered file may still receive new committed events.
        self.recovery['finalized'] = archive
        if not archive:
            self._persist_recovery()
        publish_log_recovery(self.recovery_path, self.recovery)
        if archive:
            self.registry_path.unlink(missing_ok=True)

    def failed(self, operation, error, emit):
        details = error.code if isinstance(error, TraderError) else None
        record = exception_record(error)
        through = self.kernel._durable_seq
        event_seq, decision_id = self._event or (None, None)
        self._event = None
        self._pending = None
        stage = 'DEGRADED' if self.recovery is None else 'REBUILDING'
        if self.recovery is None:
            self.recovery = {'schema_version': 'DSV_OPERATION_LOG_RECOVERY/1', **self._run_record,
                             'status': stage, 'operation': operation, 'file': self.path.name,
                             'first_event_seq': str(event_seq or (self.last_event_seq + 1)),
                             'through_event_seq': str(through), 'first_decision_id': decision_id,
                             'exception': record, 'error_code': details,
                             'observed_at_utc': utc(datetime.now(timezone.utc)),
                             'auxiliary_rows_unavailable': True,
                             'repeat_count': 1}
        else:
            self.recovery.update(status=stage, through_event_seq=str(through),
                                 repeat_count=self.recovery['repeat_count'] + 1,
                                 last_error_code=details, last_error_type=type(error).__name__)
        self.kernel._dsv_diagnostics['operation_log'] = self.recovery.copy()
        delay = min(30, 2 ** min(self.recovery['repeat_count'] - 1, 5))
        self.next_recovery = time.monotonic() + delay
        try:
            self._persist_recovery()
        except OSError:
            if self.recovery['repeat_count'] == 1:
                try:
                    emit({key: value for key, value in self.recovery.items() if key != 'run_directory'} |
                         {'phase': 'DSV_OPERATION_LOG_DEGRADED', 'error_code': 'E_OPERATION_LOG'})
                except (OSError, ValueError):
                    pass

    def _recover(self):
        if self._rebuilding is None:
            through = self.kernel._durable_seq
            self.recovery.update(status='REBUILDING', through_event_seq=str(through))
            self.kernel._dsv_diagnostics['operation_log'] = self.recovery.copy()
            self._persist_recovery()
            self._rebuilding = self._start_rebuild(self, through)
            return
        if not self._rebuilding.done():
            return
        done, self._rebuilding = self._rebuilding, None
        replacement = done.result()
        self._verify_file(opening=True)
        self.last_event_seq = replacement.last_event_seq
        for name in ('geometry_days', 'last_time', 'last_hint', 'active_blocks', 'cycle_markers', 'order_markers'):
            setattr(self, name, getattr(replacement, name))
        self.recovery.update(status='RECOVERED', recovered_through_event_seq=str(self.last_event_seq))
        self._finish_recovery(archive=False)
        self._recovered_receipt = self.recovery.copy()
        self.kernel._dsv_diagnostics['operation_log'] = self.recovery.copy()
        self.recovery = None

    def close(self):
        if self.recovery is None and self.last_event_seq >= self.kernel._durable_seq:
            self._verify_file()
            with self.path.open('r+b') as stream:
                os.fsync(stream.fileno())
            if self._recovered_receipt is not None:
                publish_log_recovery(self.recovery_path, self._recovered_receipt | {'finalized': True})
            self.registry_path.unlink(missing_ok=True)

    @staticmethod
    def _start_rebuild(log, through):
        result = Future()

        def rebuild():
            try:
                result.set_result(log.rebuild(through))
            except Exception as error:
                result.set_exception(error)

        Thread(target=rebuild, name='OnlineTrader-operation-log-rebuild', daemon=True).start()
        return result

    def rebuild(self, through):
        """Recreate the workbench log from this boot's committed journal rows."""
        replacement = DsvEntryLog(self.kernel, self.path.parent, self.profile,
                                  boot_id=self.boot_id, from_seq=self.start_seq)
        with tempfile.TemporaryDirectory(dir=self.path.parent, prefix='.strategy-log-') as location:
            replacement._target = Path(location) / self.path.name
            replacement._target.touch(exist_ok=False)
            connection = sqlite3.connect(self.source.path.as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                connection.row_factory = sqlite3.Row
                while rows := replacement._events(through, query=lambda sql, params: connection.execute(sql, params).fetchall()):
                    for row in rows:
                        replacement._consume(row)
                        replacement.last_event_seq = row['event_seq']
                replacement.last_event_seq = through
            finally:
                connection.close()
            with replacement._target.open('r+b') as stream:
                os.fsync(stream.fileno())
            os.replace(replacement._target, self.path)
        return replacement

    def write(self, rows):
        if not rows:
            return
        rows = [row | self._identity for row in rows]
        if self._event is not None:
            seq, _ = self._event
            first = self._pending or 0
            rows = [row | {'journal_event_seq': str(seq), 'journal_row': first + index}
                    for index, row in enumerate(rows)]
            self._pending = first + len(rows)
        raw = b''.join(canonical_bytes(row) + b'\n' for row in rows)
        with self._target.open('r+b') as stream:
            if self._target == self.path and self._signature(stream) != self._file_state:
                raise TraderError('E_OPERATION_LOG_CHANGED', 'Operation log was truncated, replaced or changed')
            stream.seek(0, os.SEEK_END)
            if stream.write(raw) != len(raw):
                raise OSError('Incomplete operation log append')
            stream.flush()
            if self._target == self.path:
                self._file_state = self._signature(stream)

    def auxiliary(self, rows):
        if self.recovery is None:
            self.write(rows)

    def due(self, key, now, seconds):
        previous = self.last_time.get(key)
        if previous is not None and (now - previous).total_seconds() < seconds:
            return False
        self.last_time[key] = now
        return True

    def market(self, event, decision):
        checks = decision.get('entry_log', ())
        reasons = dict.fromkeys(decision.get('reasons', ()))
        code, day = event['contract_id'], event['trading_day']
        symbol = self.symbols[code]
        now = datetime.fromisoformat(event['decision_time_utc'])
        prefix = f"{symbol['name']}({code})"
        rows = []
        observation = decision.get('event_mode') == 'OBSERVATION' or 'OBSERVATION_ONLY_BACKLOG' in reasons
        if observation:
            return

        def append(kind, tag, message, **details):
            rows.append({'observed_at_utc': now.isoformat(), 'contract_id': code, 'trading_day': day,
                         'kind': kind, 'tag': tag, 'origin': '策略', 'message': f'{prefix} {message}',
                         'decision_id': decision.get('decision_id'), 'authority_revision': decision.get('authority_revision'),
                         'account_revision': decision.get('account_revision'),
                         'reason_code': kind, **details})

        previous = self.active_blocks.get(code, {})
        active = {}
        for reason in reasons:
            kind = reason.split(':')[-1]
            if kind in WAIT_REASONS or kind == 'GEOMETRY_BLOCK' or kind.endswith('NO_CHASE_AFTER_ACT'):
                continue
            if kind == 'NET_LOSS_LIMIT':
                stop = (decision.get('stops') or {}).get('state')
                message = (f'当前净亏 {Decimal(stop["net_loss_points"]):.0f} 点，达到停开阈值'
                           if stop else '达到净亏停开阈值')
            else:
                message = BLOCK_MESSAGES.get(kind)
                if kind == 'ACCOUNT_VALUES_UNKNOWN':
                    missing = (decision.get('risk') or {}).get('metrics', {}).get('missing_fields', [])
                    if missing:
                        message += ' · ' + '、'.join(missing)
            old = previous.get(reason)
            if old and old['day'] == day:
                repeats = old['repeats'] + 1
                interval = 300 if kind in {'OPENING_WAIT', 'CLOSE_SESSION_NO_OPEN'} else 60
                if (now - old['written']).total_seconds() >= interval:
                    append(reason, '等待', f'仍受阻 · {message or kind}，累计 {repeats} 次',
                           repeat_count=repeats)
                    active[reason] = {'day': day, 'written': now, 'repeats': repeats}
                else:
                    active[reason] = old | {'repeats': repeats}
                continue
            if kind in TIMING_CHECKS:
                self.last_time.pop((code, kind), None)
                self.timing_message(code, kind, now, event, append)
            elif message:
                append(reason, '等待', message)
            else:
                self.filter_message(code, day, reason, now, event, symbol, decision, append)
            active[reason] = {'day': day, 'written': now, 'repeats': 1}

        for reason, old in previous.items():
            if reason not in active:
                label = BLOCK_MESSAGES.get(reason.split(':')[-1], reason)
                append('ENTRY_BLOCK_CLEARED', '恢复', f'已解除 · {label}',
                       cleared_reason_code=reason, repeat_count=old['repeats'])

        if checks and checks[0] not in TIMING_CHECKS and not reasons.keys() & DAY_STOPS:
            hint_rows = len(rows)
            self.trigger_messages(code, day, symbol, now, event, checks, append)
            rows = rows[hint_rows:] + rows[:hint_rows]
        self.write(rows)
        self.active_blocks[code] = active

    def timing_message(self, code, kind, now, event, append):
        if kind == 'OPENING_WAIT' and self.due((code, kind), now, 300):
            minutes = Decimal(self.trade['no_open_after_open_minutes'])
            append(kind, '执行', f'开盘后不足 {minutes:.0f} 分钟，本次信号跳过（开盘后禁开）')
        elif kind == 'CLOSE_SESSION_NO_OPEN' and self.due((code, kind), now, 300):
            minutes = Decimal(self.trade['no_open_before_close_minutes'])
            append(kind, '执行', f'距收盘不足 {minutes:.0f} 分钟，不再开新仓')
        elif kind == 'DSV_QUOTE_AGE_EXCEEDED' and self.due((code, kind), now, 60):
            seconds = (now - datetime.fromisoformat(event['source_time_utc']).replace(microsecond=0)).total_seconds()
            limit = Decimal(self.trade['max_quote_age_seconds'])
            append(kind, '风控', f'行情已滞后 {seconds:.0f} 秒（>{limit:.0f} 秒），暂停开新仓')

    def trigger_messages(self, code, day, symbol, now, event, checks, append):
        opening, stop, profit = (Decimal(symbol[key]) for key in ('open_offset', 'stop_offset', 'profit_offset'))
        high, low, last = (Decimal(event[key]) for key in ('day_high', 'day_low', 'last'))
        long_trigger, short_trigger = low + opening, high - opening
        geometry = 'GEOMETRY_BLOCK' in checks
        if geometry and (code, day) not in self.geometry_days:
            self.geometry_days.add((code, day))
            limit = 2 * opening - stop
            append('GEOMETRY_BLOCK', '提示', f'当日振幅 {high-low:.0f} 点 > 上限 {limit:.0f} 点'
                   f'（2×开仓偏移{opening} − 止损{stop}）→ 多空触发价交叉，本品种今日不建仓（几何条件，不是故障）')
        signature = (f'{long_trigger:.1f}', f'{short_trigger:.1f}', f'{last:.1f}', geometry)
        if signature != self.last_hint.get(code) and self.due((code, 'TRIGGER_HINT'), now, 60):
            self.last_hint[code] = signature
            suffix = '（几何条件不满足，暂不建仓）' if geometry else ''
            append('TRIGGER_HINT', '提示', f'当日 高{high:.0f}/低{low:.0f}（振幅 {high-low:.0f}，开仓偏移 {opening}）'
                   f'→ 多开触发 {long_trigger:.1f} / 空开触发 {short_trigger:.1f}，现价 {last:.1f}{suffix}')
        activation = checks[-1]
        if activation.endswith(':NO_CHASE_AFTER_ACT') and self.due((code, day, 'ACTIVATION_PASSED'), now, 600):
            ratio_key = 'protect_80_ratio' if self.trade['protect_80_enabled'] else 'trailing_activate_ratio'
            ratio = Decimal(self.trade[ratio_key])
            is_long = activation.startswith('LONG:')
            direction, trigger = ('多', long_trigger) if is_long else ('空', short_trigger)
            level = trigger + profit * ratio if is_long else trigger - profit * ratio
            append(activation, '跳过', f'当日行情已跑过{direction}头激活位（触发 {trigger:.1f} '
                   f'∓ 止盈×{ratio:.2f} = {level:.1f}），本次不开仓')

    def filter_message(self, code, day, reason, now, event, symbol, decision, append):
        direction, sep, kind = reason.partition(':')
        if not sep:
            kind, direction = direction, ''
        side = {'LONG': '多头', 'SHORT': '空头'}.get(direction, '')
        rule = ENTRY_FILTERS.get(kind)
        if rule is not None:
            tag, message, _ = rule
            if kind == 'EFFECTIVE_RISK_CAP':
                cap = (decision.get('risk') or {}).get('metrics', {}).get('effective_risk_cap')
                if cap is not None:
                    message = f'账户风险度已达上限 {Decimal(cap):.2%}，跳过开仓'
            append(reason, tag, side + (' ' if side else '') + message)
        elif kind == 'CHASE_CAP_EXCEEDED' and direction in {'LONG', 'SHORT'}:
            opening = Decimal(symbol['open_offset'])
            is_long = direction == 'LONG'
            trigger = Decimal(event['day_low']) + opening if is_long else Decimal(event['day_high']) - opening
            raw = event.get('ask1' if is_long else 'bid1')
            try:
                opponent = Decimal(str(raw))
            except ArithmeticError:
                opponent = Decimal(0)
            present = opponent.is_finite() and opponent > 0
            label = f'{opponent:.1f}' if present else '缺失'
            opponent = opponent if present else Decimal(0)
            last = Decimal(event['last'])
            started = datetime.fromisoformat(event['session_open_utc'])
            elapsed = (now - started).total_seconds()
            window = float(self.trade['chase_window_minutes']) * 60
            opening_session = event.get('is_opening_session')
            if opening_session is None:
                opening_session = started.astimezone(BEIJING).strftime('%H:%M') in {'09:00', '09:30', '21:00'}
            ratio = self.trade['chase_slip_ratio' if opening_session and 0 <= elapsed <= window else 'entry_slip_ratio']
            cap = max(1, round(opening * Decimal(ratio)))
            over = max(opponent, last) - trigger - cap if is_long else trigger - min(opponent, last) - cap
            append(reason, '跳过', f'{side} 对手价 {label} / 最新价 {last:.1f}，触发价 {trigger:.1f}，'
                   f'超出上限 {over:.1f} 点（单边上限 {cap:.1f} 点，有利方向除外），本次不开仓')

        else:
            append(reason, '等待', f'开仓条件受阻 · {reason}')

    def monitor(self, monitor, loop_start, timings):
        if self.recovery is not None:
            return
        now_ns = time.monotonic_ns()
        summary = monitor.summary(now_ns)
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        rows = []
        if monitor.recoveries:
            threshold = Decimal(self.trade.get('resume_gap_seconds', '60'))
            cycles = self.kernel._committed.values()
            for code, gap in monitor.recoveries.items():
                if Decimal(str(gap)) <= threshold:
                    continue
                position = self.kernel.port.position(code)
                owned = [cycle for cycle in cycles if cycle['plan']['contract_id'] == code and cycle['lifecycle']!='CLOSED']
                if not (position['long'] or position['short'] or any(
                        OrderState.from_dict(order).potential_remaining for cycle in owned for order in cycle['orders'])):
                    continue
                rows.append({'observed_at_utc':now, 'kind':'QUOTE_RESUMED', 'tag':'恢复', 'contract_id':code,
                    'trading_day':monitor.trading_day,
                    'message':f'{code} 行情断档 {gap:.0f} 秒后恢复：对账结果 账户多{position["long"]}/空{position["short"]}'})
        if summary:
            rows.append({'observed_at_utc': now, 'kind': 'TICK_DIAGNOSTIC', 'tag': '行情诊断',
                         'message': '60 秒行情更新 · ' + summary})
        if now_ns - loop_start > 2_000_000_000 and (
                self.last_slow_loop_ns is None or now_ns - self.last_slow_loop_ns >= 60_000_000_000):
            self.last_slow_loop_ns = now_ns
            detail = '，'.join(f'{name} {span/1e9:.3f} 秒' for name, span in timings.items())
            rows.append({'observed_at_utc': now, 'kind': 'SLOW_DSV_LOOP', 'tag': '慢',
                         'message': f'本轮耗时 {(now_ns-loop_start)/1e9:.1f} 秒' + (f'（{detail}）' if detail else '')})
        self.write(rows)

    def committed(self, kind, event_seq, observed_at_utc, data):
        """Describe an event once from committed state, preserving its provenance."""
        rows = []

        def append(tag, message, *, origin='策略', code=None, day=None, category=None, **facts):
            if code:
                name = self.symbols.get(code, {}).get('name', code)
                message = f'{name}({code}) {message}'
            rows.append({'observed_at_utc': observed_at_utc, 'kind': kind, 'event_seq': str(event_seq),
                         'tag': tag, 'category': category, 'origin': origin, 'message': message,
                         'contract_id': code, 'trading_day': day, **facts})

        session = SESSION_EVENTS.get(kind)
        if session is not None:
            append(*session)
        if kind == 'DSV_PARAMETERS_SAVED':
            append('参数', '已保存运行参数，下一笔新鲜行情使用新版本判断开仓',
                   profile_hash=data['dsv_execution']['profile_hash'])
        cycle = data.get('cycle')
        if isinstance(cycle, dict):
            self.cycle_event(kind, cycle, data.get('fill'), append)
        exceptions = data.get('exceptions', [])
        if data.get('exception'):
            exceptions = [data['exception']]
        for item in exceptions:
            metadata = (cycle.get('strategy_metadata') or {}) if cycle else {}
            plan = cycle['plan'] if cycle else {}
            append('警告', f"{item['stack'][0]['type']} · {item['exception_id']}",
                   code=plan.get('contract_id'), day=plan.get('trading_day'), category='error',
                   child_order_id=item.get('child_order_id', data.get('child_order_id')),
                   native_order_id=item.get('native_order_id'), decision_id=metadata.get('decision_id'),
                   exception_id=item['exception_id'], exception=item)
        self.write(rows)

    def cycle_event(self, kind, cycle, fill, append):
        plan, protection = cycle['plan'], cycle['protection']
        code, day = plan['contract_id'], plan['trading_day']
        side = '多头' if plan['direction'] == 'LONG' else '空头'
        identity = cycle['position_cycle_id']
        previous = self.cycle_markers.get(identity, (None, None, None, None))
        current = (protection.get('protected_event_seq'), protection.get('phase1_target'),
                   protection.get('phase2_target'), cycle['lifecycle'])
        self.cycle_markers[identity] = current

        def report(tag, text, **fields):
            decision_id = (cycle.get('strategy_metadata') or {}).get('decision_id')
            append(tag, text, code=code, day=day, decision_id=decision_id, **fields)

        if kind == 'REPLACEMENT_BLOCKED':
            review = cycle['last_replacement_block']
            reasons = review['reason_codes']
            report('等待', '重报受阻 · ' + ' / '.join(BLOCK_MESSAGES.get(reason, reason) for reason in reasons),
                   reason_codes=reasons, recheck=review, category='error')

        if current[0] is not None and previous[0] != current[0]:
            settings = plan['profit_protection']
            ratio = Decimal(settings['activation_ratio'])
            keep = Decimal(settings['floor_ratio'])
            offset = Decimal(plan['fixed_take_profit']['distance_points'])
            base = Decimal(plan['theoretical_entry_price'])
            floor = base + offset * keep * (1 if plan['direction'] == 'LONG' else -1)
            report('保护', f'浮盈达止盈 {ratio:.0%}（最高 {protection["max_favorable_move"]} 点）'
                   f' → 保护线 {floor:.1f}（止盈×{keep:.0%}）')
            if settings.get('block_new_for_trading_day'):
                report('规则', f'浮盈曾达止盈 {ratio:.0%} → 当日停止开仓')
        for stage in (1, 2):
            if current[stage] is not None and previous[stage] != current[stage]:
                report('半仓', f'{side}第 {stage} 档减仓，目标剩余 {current[stage]} 手', category='half')
        if isinstance(fill, dict):
            order = next((row for row in cycle['orders'] if row['child_order_id'] == fill['child_order_id']), None)
            opening = fill['effect'] == 'OPEN'
            manual = cycle.get('entry_origin') == 'MANUAL' if opening else (
                fill['child_order_id'] in (cycle.get('execution') or {}).get('manual_reduction_children', {}))
            target = '开仓' if opening else '平仓'
            partial = '部分成交' if order and order['filled_qty'] < order['original_qty'] else '成交'
            stamp = (fill.get('native_day_evidence') or {}).get('source_time_ns')
            if stamp:
                seconds, nanos = divmod(int(stamp), 1_000_000_000)
                native_time = datetime.fromtimestamp(seconds,timezone.utc) + timedelta(microseconds=nanos//1000)
                native_time = native_time.isoformat().replace('+00:00','Z')
            else:
                native_time = None
            report('柜台成交', f'{side}{target} {partial} {fill["quantity"]} 手 @ {fill["price"]}',
                   origin='柜台', category='manual' if manual else 'open' if opening else 'close',
                   child_order_id=fill['child_order_id'], trade_key=fill['trade_key'],
                   native_trade_id=fill.get('native_trade_id'),
                   native_time_utc=native_time)
        if current[3] == 'CLOSED' and previous[3] != 'CLOSED':
            exit_reason = (cycle.get('nominal_exit') or {}).get('exit_reason')
            reason = EXIT_REASONS.get(exit_reason, '仓位与委托已核对')
            report('平仓', f'{side}{reason}，周期已结案', category='manual' if exit_reason == 'MANUAL_EXIT' else 'close')
            for order in cycle['orders']:
                self.order_markers.pop(order['child_order_id'], None)
            return
        if current[3] == 'CLOSED':
            return
        for order in cycle['orders']:
            self.order_event(kind, cycle, order, side, report)

    def order_event(self, kind, cycle, order, side, report):
        child = order['child_order_id']
        current = (order['send_state'], order['cancel_status'], order['terminal_confirmed'], order['broker_state'])
        previous = self.order_markers.get(child, (None, None, False, None))
        self.order_markers[child] = current
        opening = order['effect'] == 'OPEN'
        manual = cycle.get('entry_origin') == 'MANUAL' if opening else (
            child in (cycle.get('execution') or {}).get('manual_reduction_children', {}))
        category = 'manual' if manual else 'open' if opening else 'close'
        label = f'{side}{"开仓" if opening else "平仓"}'
        details = f'{label} {order["original_qty"]} 手 @ {order["limit_price"]}'
        fields = {'child_order_id': child, 'native_order_id': cycle.get('native_order_keys', {}).get(child)}
        if previous[0] is None and current[0] == 'SEND_POSSIBLE':
            candidate = (cycle.get('strategy_metadata') or {}).get('candidate') or {}
            if opening and candidate.get('theoretical_price') is not None:
                details += f'，理论开仓 {candidate["theoretical_price"]}'
            if kind == 'REPLACEMENT_PREPARED':
                details = '撤单终态后重报 · ' + details
            report('手动' if manual else '开仓' if opening else '平仓', details + '，准备报单',
                   category=category, **fields)
        elif current[0] != previous[0] and current[0] in {'CALL_RETURNED', 'UNKNOWN', 'DEFINITELY_NOT_SENT'}:
            status = {'CALL_RETURNED':'报单请求已提交', 'UNKNOWN':'报单结果待核对',
                      'DEFINITELY_NOT_SENT':'发送条件变化，报单未发出'}[current[0]]
            abort = cycle.get('last_dispatch_abort') or {}
            if current[0] == 'DEFINITELY_NOT_SENT' and abort.get('child_order_id') == child:
                labels = {'ORIGINAL_BOOT':'启动身份变化', 'CURRENT_SESSION':'盘次待确认',
                          'ENTRY_AUTHORITY':'开仓权限变化', 'RISK_BUDGET':'风险预算不足',
                          'NOMINAL_DAY_STOP':'当日停开', 'STRATEGY_RECHECK':'策略条件复核未通过',
                          'ORIGINAL_EXIT_FACTS':'原平仓行情或回报待核对', 'ORIGINAL_EXIT_PRICE':'原平仓价格变化',
                          'ORIGINAL_EXIT_QUANTITY':'原平仓可用手数变化', 'EXECUTION_PRICE':'委托价格复核未通过',
                          'E_CLOCK':'时钟偏差超限', 'E_OWNERSHIP':'柜台归属待核对',
                          'E_FACT_BARRIER':'柜台同步待核对', 'E_GATEWAY_SCOPE':'行情、时窗或范围条件未通过'}
                status += ' · ' + ' / '.join(dict.fromkeys(
                    labels.get(reason, BLOCK_MESSAGES.get(reason, reason)) for reason in abort['reasons']))
                fields.update(reason_codes=abort['reasons'], recheck=abort.get('recheck'))
            report('执行' if current[0] == 'CALL_RETURNED' else '警告', f'{details}，{status}',
                   origin='执行器', category=category if current[0] == 'CALL_RETURNED' else 'error', **fields)
        if kind == 'ONLINE_WORKING_ORDER_PROVED' and current[0] == 'CONFIRMED' and previous[0] != 'CONFIRMED':
            report('柜台委托', f'{details}，柜台确认活动委托', origin='柜台', category=category, **fields)
        if current[1] != previous[1] and current[1] in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}:
            status = {'SEND_POSSIBLE':'准备撤单', 'CALL_RETURNED':'撤单请求已提交',
                      'UNKNOWN':'撤单结果待核对'}[current[1]]
            report('执行' if current[1] != 'UNKNOWN' else '警告', f'{label} {status}',
                   origin='执行器', category=category if current[1] != 'UNKNOWN' else 'error', **fields)
        if current[2] and not previous[2] and current[3] in {'FILLED', 'CANCELLED', 'REJECTED'}:
            status = {'FILLED':'已确认全成', 'CANCELLED':'已确认撤单', 'REJECTED':'柜台拒单'}[current[3]]
            report('柜台委托', f'{label} {status}，累计成交 {order["filled_qty"]} 手',
                   origin='柜台', category='error' if current[3] == 'REJECTED' else category, **fields)
        if kind == 'EXECUTION_CANCEL_TIMEOUT' and not current[2] and current[1] in {'CALL_RETURNED', 'UNKNOWN'}:
            report('警告', f'{label} 撤单超时，等待柜台核对', category='error', **fields)
