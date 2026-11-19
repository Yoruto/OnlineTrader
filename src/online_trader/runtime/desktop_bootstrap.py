"""User-confirmed desktop startup over the existing finite simulation owner.

The GUI snapshots business parameters and a selected account only. This child
process acquires read-only market metadata, closes that reader, and derives the
bounded internal files before entering the original strategy execution path.
Request construction is entirely offline and does not read the credential vault.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import uuid

from online_trader.accounts import AccountStore
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import publish_new_bytes
from online_trader.configuration.offsets import dsv_startup_profile
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import decimal_text, decimal_value, identifier
from online_trader.errors import TraderError, exception_record
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime import dsv_simulation
from online_trader.runtime.instance import AccountLock, ClockActivity
from online_trader.scheduling.calendar import TradingCalendar, _utc
from online_trader.scheduling.holdover import ProtectionClockAnchor
from online_trader.scheduling.reference import CalibratedClock


REQUEST_SCHEMA = 'DESKTOP_START/1'
REQUEST_LIFETIME = timedelta(minutes=10)
MAX_REQUEST_BYTES = 1024 * 1024
_REQUEST_KEYS = {'schema_version', 'request_id', 'created_at_utc', 'expires_at_utc',
    'account_store', 'account_id', 'account_key', 'provider', 'profile', 'profile_hash',
    'arm', 'confirmed'}
_PHASE_MESSAGES = {
    'STARTUP_CHECKING': '正在核对账号与本次启动请求',
    'STARTUP_READING_MARKET': '正在连接行情并核对交易日、交易时段和合约信息',
    'STARTUP_PREPARED': '交易时段已核对，正在启动所选模拟盘策略',
    'STARTUP_RECOVERING': '正在连接柜台并恢复原持仓保护，开仓权限保持关闭',
}
ERROR_MESSAGES = {
    'E_DESKTOP_START_REQUEST': '启动请求已过期或内容发生变化，请重新点击启动策略。',
    'E_DESKTOP_START_REPLAY': '本次启动请求已经执行，请查看原运行状态，避免重复启动。',
    'E_DESKTOP_START_LIMITS': '请在交易参数中设置有效的最大手数和每品种最大报单数。',
    'E_DESKTOP_START_RECOVERY': '该账号有未核清的原运行，请恢复原范围并核对持仓、委托及回报。',
    'E_INSTANCE_EXISTS': '该账号的策略执行器已在运行，请刷新工作台查看。',
    'E_FACT_BARRIER': '该账号有持仓、委托或未知结果需要核对，请先恢复原策略运行。',
    'E_SCOPE': '所选账号或运行范围已变化，请重新确认。',
    'E_CALENDAR_INCOMPLETE': '当前交易日或合约时段未能完整核对，策略尚未启动。',
    'E_CREDENTIALS': '账号凭据不完整或不可用，请在账号信息中重新保存。',
    'E_GATEWAY_CONNECT': '模拟账号或行情连接未成功，请检查账号和网络后重新启动。',
    'E_STARTUP_METADATA': '未能取得完整的交易日历或合约资料，请检查行情连接和品种代码。',
    'E_STARTUP_METADATA_TIMEOUT': '读取交易日历或合约资料超时，请检查行情网络后重新启动。',
    'E_STARTUP_METADATA_CLOSE': '行情资料连接未正常关闭，策略启动已停止。',
    'E_DEPENDENCY': '模拟运行组件不可用，请检查已安装的程序环境。',
    'E_CLOCK': '无法取得应用授时，请检查授时网络连接。',
}


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TraderError('E_DESKTOP_START_REQUEST', 'An explicit aware startup time is required')
    return value.astimezone(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _selected(store, account_id):
    row = store.get(account_id)
    if store.read()['selected_id'] != account_id or row['environment'] != 'ONLINE_SIM' or row['provider'] not in {'TQKQ', 'TQ_SIMNOW'}:
        raise TraderError('E_SCOPE', 'Only the selected simulation account may start')
    return row


def timed_start_trigger(profile, now, *, on_launch=False, metadata=None):
    """Select the DSV local trigger and, when given SDK evidence, verify its session."""
    from online_trader.runtime.launch import scheduled_minute
    from online_trader.strategy.entry import CHINA_TIME
    settings = profile.to_record()['settings']
    local = now.astimezone(CHINA_TIME)
    if local.weekday() >= 5 or local.date().isoformat() in settings['trade']['holidays']:
        return None
    trigger = 'AUTO_START_ON_LAUNCH' if on_launch and settings['trade']['auto_start_on_launch'] else (
        scheduled_minute(settings['schedule'], local))
    if trigger and metadata is not None:
        calendar = TradingCalendar.from_record(metadata['calendar'])
        sessions = [calendar.query(code, _stamp(now)) for code in metadata['instruments']]
        near = []
        for window in sessions:
            session = window.get('session') or window.get('next_session')
            if (window.get('complete') and session and session['trading_day'] == metadata['trading_day']
                    and (window['is_trading'] or timedelta(0) <= _utc(session['open_utc']) - now <= timedelta(minutes=30))):
                near.append(session)
        if not near:
            return None
        if trigger != 'AUTO_START_ON_LAUNCH':
            night = trigger.endswith('_night')
            if not any((_utc(session['open_utc']).astimezone(CHINA_TIME).hour >= 18) == night for session in near):
                return None
    return trigger


def _limits(profile):
    settings = profile.to_record()['settings']
    maximum = settings['trade']['max_volume']
    per_contract = settings['trade']['max_daily_orders']
    count = len(settings['symbols'])
    if not 1 <= count <= 11 or maximum <= 0 or per_contract <= 0 or per_contract > 2147483647:
        raise TraderError('E_DESKTOP_START_LIMITS', ERROR_MESSAGES['E_DESKTOP_START_LIMITS'])
    return maximum, per_contract


def build_start_request(store, account_id, profile, *, arm=True, now=None, timed_trigger=None, manual=False,
                        scheduled_operator=None):
    """Called after the user's start confirmation. Never connects or reads secrets."""
    if not isinstance(profile, DsvProfile) or type(arm) is not bool:
        raise TraderError('E_DESKTOP_START_REQUEST', 'Expected a business profile and explicit startup intent')
    if manual and (arm or timed_trigger is not None):
        raise TraderError('E_DESKTOP_START_REQUEST', 'Manual mode requires explicit arming after startup')
    row = _selected(store, account_id)
    _limits(profile)
    instant = _now(now)
    request = {'schema_version': REQUEST_SCHEMA, 'request_id': 'desktop-' + uuid.uuid4().hex,
        'created_at_utc': _stamp(instant), 'expires_at_utc': _stamp(instant + REQUEST_LIFETIME),
        'account_store': str(store.directory.resolve()), 'account_id': account_id,
        'account_key': row['account_key'], 'provider': row['provider'],
        'profile': profile.to_record(), 'profile_hash': profile.content_hash,
        'arm': arm, 'confirmed': True}
    if timed_trigger is not None:
        request['timed_trigger'] = timed_trigger
    if scheduled_operator is not None:
        if timed_trigger is None:
            raise TraderError('E_DESKTOP_START_REQUEST', 'Scheduled operator identity requires a timed request')
        request['scheduled_operator'] = identifier(scheduled_operator)
    if manual:
        request['manual'] = True
    return request


def _request(path, instant=None):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        value = strict_loads(raw)
        if (len(raw) > MAX_REQUEST_BYTES or type(value) is not dict
                or set(value) - {'timed_trigger', 'recover_original', 'manual', 'scheduled_operator'} != _REQUEST_KEYS):
            raise ValueError()
        if value['schema_version'] != REQUEST_SCHEMA or value['confirmed'] is not True or type(value['arm']) is not bool:
            raise ValueError()
        identifier(value['request_id'])
        created, expiry = _utc(value['created_at_utc']), _utc(value['expires_at_utc'])
        if expiry - created != REQUEST_LIFETIME or (instant is not None and not created <= instant < expiry):
            raise ValueError()
        if 'recover_original' in value and (value['recover_original'] is not True or value['arm']
                                             or 'timed_trigger' in value):
            raise ValueError()
        if 'manual' in value and (value['manual'] is not True or value['arm'] or 'timed_trigger' in value):
            raise ValueError()
        if 'scheduled_operator' in value:
            if 'timed_trigger' not in value or not value['arm']:
                raise ValueError()
            identifier(value['scheduled_operator'])
        directory = Path(value['account_store'])
        if not directory.is_absolute():
            raise ValueError()
        profile = DsvProfile.from_record(value['profile'])
        if value['profile_hash'] != profile.content_hash:
            raise ValueError()
        trigger = value.get('timed_trigger')
        if trigger is not None and trigger != timed_start_trigger(profile, created, on_launch=trigger=='AUTO_START_ON_LAUNCH'):
            raise ValueError()
        _limits(profile)
        return value, profile
    except (OSError, ValueError, TypeError, KeyError, TraderError) as error:
        if isinstance(error, TraderError) and error.code == 'E_DESKTOP_START_LIMITS':
            raise
        raise TraderError('E_DESKTOP_START_REQUEST', ERROR_MESSAGES['E_DESKTOP_START_REQUEST']) from None


def _check_existing_ledger(store, row):
    ledger = store.ledger_path(row)
    if not ledger.exists():
        return
    try:
        store._settled(row)
        connection = sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=1)
        with closing(connection):
            pending = connection.execute("""
                SELECT 1 FROM child_orders AS child
                LEFT JOIN send_attempts AS attempt USING (child_order_id)
                LEFT JOIN cancel_actions AS cancel USING (child_order_id)
                WHERE (attempt.send_state IS NULL OR attempt.send_state NOT IN ('CONFIRMED','DEFINITELY_NOT_SENT')
                    OR child.terminal_evidence_json IS NULL
                    OR json_extract(child.terminal_evidence_json,'$.confirmed_fill_qty') IS NOT child.confirmed_fill_qty
                    OR (cancel.status IS NOT NULL AND cancel.status NOT IN ('CONFIRMED','REJECTED','NOT_NEEDED')))
                LIMIT 1
                """).fetchone()
            if pending:
                raise TraderError('E_FACT_BARRIER', 'Prior orders or cancellations still need reconciliation')
    except sqlite3.Error:
        raise TraderError('E_DESKTOP_START_RECOVERY', ERROR_MESSAGES['E_DESKTOP_START_RECOVERY']) from None
    except TraderError as error:
        if error.code != 'E_FACT_BARRIER':
            raise
        raise TraderError('E_DESKTOP_START_RECOVERY', ERROR_MESSAGES['E_DESKTOP_START_RECOVERY']) from None


def _original_scope(store, row):
    ledger = store.ledger_path(row).resolve()
    try:
        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
            approvals = connection.execute("""
                SELECT DISTINCT approval.approval_record_id, approval.manifest_hash
                FROM position_cycles AS cycle
                JOIN protection_plans AS plan USING (position_cycle_id)
                JOIN approval_manifests AS approval
                  ON approval.approval_record_id = json_extract(plan.plan_json, '$.approval_record_id')
                WHERE cycle.lifecycle != 'CLOSED'
            """).fetchall()
        if len(approvals) != 1:
            raise ValueError()
        grant_id, manifest_hash = approvals[0]
        identifier(grant_id)
        if not grant_id.startswith('desktop-'):
            raise ValueError()
        original = store.directory / 'desktop-starts' / grant_id / 'run'
        paths = tuple(original / name for name in ('scope.json', 'profile.json', 'calendar.json'))
        grant, profile, calendar = dsv_simulation.load_inputs(*paths)
        if (grant.content_hash != manifest_hash or grant.value['grant_id'] != grant_id
                or grant.account_key != row['account_key'] or grant.provider != row['provider']
                or Path(grant.value['ledger_path']).resolve() != ledger or grant.replacement):
            raise ValueError()
        return grant, profile, calendar
    except (OSError, sqlite3.Error, ValueError, TraderError):
        raise TraderError('E_DESKTOP_START_RECOVERY', ERROR_MESSAGES['E_DESKTOP_START_RECOVERY']) from None


def compile_start_scope(request, store, metadata, *, now=None):
    """Compile supplied native facts; no connection, time guesses, or writes."""
    instant = _now(now)
    source_profile = DsvProfile.from_record(request['profile'])
    if source_profile.content_hash != request['profile_hash']:
        raise TraderError('E_DESKTOP_START_REQUEST', ERROR_MESSAGES['E_DESKTOP_START_REQUEST'])
    row = _selected(store, request['account_id'])
    if row['account_key'] != request['account_key'] or row['provider'] != request['provider']:
        raise TraderError('E_SCOPE', ERROR_MESSAGES['E_SCOPE'])
    codes = [symbol['code'] for symbol in source_profile.to_record()['settings']['symbols']]
    try:
        if metadata['provider'] != row['provider'] or set(metadata['instruments']) != set(codes):
            raise ValueError()
        observed = _utc(metadata['observed_at_utc'])
        if not timedelta(0) <= instant - observed <= timedelta(minutes=2):
            raise ValueError()
        calendar = TradingCalendar.from_record(metadata['calendar'])
        record = calendar.to_record()
        day = metadata['trading_day']
        if not record['complete'] or {item['contract_id'] for item in record['contracts']} != set(codes):
            raise ValueError()
        ends = []
        for item in record['contracts']:
            sessions = item['sessions']
            if not sessions or any(session['trading_day'] != day for session in sessions):
                raise ValueError()
            closes = [_utc(s['close_utc']) for s in sessions if s['force_close_at_end']]
            if not closes or max(closes) <= instant:
                raise ValueError()
            ends.extend(closes)
        end = max(ends)
        # A single cabinet day can begin on Friday night and finish on Monday.
        # The metadata reader proves the intervening trading-date flags and
        # rejects unconfirmed holiday-night gaps; do not truncate that day at
        # an arbitrary 36-hour wall-clock boundary.
        if end - instant > timedelta(days=4):
            raise ValueError()
        if not _utc(record['coverage_from_utc']) <= instant < end <= _utc(record['coverage_to_utc']):
            raise ValueError()
        effective = dsv_startup_profile(source_profile)
        maximum, targets = _limits(effective)
        components = []
        for symbol in effective.to_record()['settings']['symbols']:
            multiplier = decimal_value(metadata['instruments'][symbol['code']]['multiplier'], positive=True)
            exposure = decimal_value(symbol['stop_offset'], positive=True) * multiplier * maximum
            components.append({'contract_id': symbol['code'], 'stop_distance': symbol['stop_offset'],
                'multiplier': decimal_text(multiplier), 'maximum_lots': maximum,
                'nominal_stop_exposure': decimal_text(exposure)})
        risk_budget = decimal_text(sum((Decimal(item['nominal_stop_exposure']) for item in components), Decimal(0)))
        decimal_value(risk_budget, positive=True)
    except (KeyError, TypeError, ValueError, TraderError):
        raise TraderError('E_CALENDAR_INCOMPLETE', ERROR_MESSAGES['E_CALENDAR_INCOMPLETE']) from None
    value = {'schema_version': 'SIMNOW_DSV_DESKTOP/1' if row['provider'] == 'TQ_SIMNOW' else 'KQ_DSV_DESKTOP/1',
        'grant_id': request['request_id'], 'account_fingerprint': row['account_key'].split(':')[-1],
        'ledger_path': str(store.ledger_path(row)), 'contracts': codes, 'trading_day': day,
        'trading_days': [day], 'resume_entries_on_day_change': False,
        'not_before_utc': _stamp(instant), 'not_after_utc': _stamp(end),
        'max_order_quantity': maximum, 'daily_targets_per_contract': targets, 'risk_budget': risk_budget,
        'profile_hash': effective.content_hash, 'calendar_hash': calendar.content_hash,
        'authorization_reference': 'DESKTOP_CONFIRM:' + content_hash(request)}
    if request.get('manual'):
        value['schema_version'] = 'SIMNOW_WORKBENCH_SIM/1' if row['provider'] == 'TQ_SIMNOW' else 'KQ_WORKBENCH_SIM/1'
        value['opening_attempts'] = value.pop('daily_targets_per_contract')
        value['allow_external_allocation'] = False
    grant = DsvSimulationGrant.parse(canonical_bytes(value))
    dsv_simulation.validate_inputs(grant, effective, calendar)
    details = {'request_hash': content_hash(request), 'source_profile_hash': source_profile.content_hash,
        'effective_profile_hash': effective.content_hash, 'calendar_hash': calendar.content_hash,
        'trading_day': day, 'provider': row['provider'], 'nominal_risk_ceiling': risk_budget,
        'nominal_risk_components': components, 'max_order_quantity_source': 'DSV max_volume',
        'daily_targets_source': 'DSV max_daily_orders per contract and cabinet day',
        'startup_offset_policy': 'DSV original integer conversion and zero repair 100/15/60',
        'metadata_observed_at_utc': metadata['observed_at_utc'],
        'calendar_source': metadata.get('source', 'Injected metadata for offline verification'),
        'existing_plans_changed': False,
        'source_parameters_changed': False, 'entry_authority': 'DISARMED'}
    return grant, effective, calendar, details


def run_desktop_start(request_path, output, *, now=None):
    """Explicit child entry; called only after desktop confirmation, never by preview."""
    return run_confirmed_start(request_path, output, now=now)


def run_confirmed_start(request_path, output, *, now=None, configured_store=None, trusted_control_pid=None,
                        use_portable_profile_file=False, check_scheduled_authorization=None):
    """Run the original owner using the selected desktop or portable account inputs."""
    directory = Path(output).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    try:
        request, source_profile = _request(request_path, now)
        if 'manual' in request and configured_store is None:
            raise TraderError('E_AUTH', 'Standalone configuration is required for this manual startup request')
        if 'scheduled_operator' in request and (configured_store is None or check_scheduled_authorization is None
                or check_scheduled_authorization() != request['scheduled_operator']):
            raise TraderError('E_AUTH', 'The scheduled start requires its original operator capability')
        store = configured_store if configured_store is not None else AccountStore(request['account_store'])
        if store.directory != Path(request['account_store']).resolve():
            raise TraderError('E_SCOPE', 'The confirmed start belongs to another account directory')
        row = _selected(store, request['account_id'])
        if row['account_key'] != request['account_key'] or row['provider'] != request['provider']:
            raise TraderError('E_SCOPE', ERROR_MESSAGES['E_SCOPE'])
        def phase(index, name, **extra):
            publish_new_bytes(canonical_bytes({'phase': name, 'message': _PHASE_MESSAGES[name],
                'entry_authority': 'DISARMED', **extra}), directory / f'phase-{index:03}-bootstrap.json')
        phase(0, 'STARTUP_CHECKING')
        with ClockActivity(), AccountLock(row['account_key']) as lease:
            runtime_options = {}
            if configured_store is not None:
                configured_store.initialize_ledger(row, lease)
                runtime_options.update(_existing_ledger=True, _trusted_control_pid=trusted_control_pid or -1)
                if not use_portable_profile_file:
                    source_profile = configured_store.effective_profile(row, source_profile)
                    request.update(profile=source_profile.to_record(), profile_hash=source_profile.content_hash)
            recovering = request.get('recover_original', False)
            if not recovering:
                try:
                    _check_existing_ledger(store, row)
                except TraderError as error:
                    if error.code != 'E_DESKTOP_START_RECOVERY' or 'timed_trigger' in request:
                        raise
                    recovering = True
            if recovering:
                grant, effective, calendar = _original_scope(store, row)
                anchor = ProtectionClockAnchor(grant.value['ledger_path'], row['account_key'])
                clock = CalibratedClock(recovery_anchor=anchor, recovering=True)
            else:
                clock = CalibratedClock() if now is None else None
            instant = clock.now() if clock is not None else _now(now)
            if not _utc(request['created_at_utc']) <= instant < _utc(request['expires_at_utc']):
                raise TraderError('E_DESKTOP_START_REQUEST', ERROR_MESSAGES['E_DESKTOP_START_REQUEST'])
            claim = store.directory / 'desktop-start-claims' / (request['request_id'] + '.json')
            if claim.exists():
                raise TraderError('E_DESKTOP_START_REPLAY', ERROR_MESSAGES['E_DESKTOP_START_REPLAY'])
            publish_new_bytes(canonical_bytes({'request_hash': content_hash(request),
                'output': str(directory), 'created_at_utc': _stamp(instant)}), claim)
            publish_new_bytes(canonical_bytes(request), directory / 'confirmed-request.json')
            if recovering:
                for name, record in (('scope.json', grant.value), ('profile.json', effective.to_record()),
                                     ('calendar.json', calendar.to_record())):
                    publish_new_bytes(canonical_bytes(record), directory / name)
                phase(1, 'STARTUP_RECOVERING', account_key=row['account_key'],
                      trading_day=grant.value['trading_day'], pipe=store.pipe_name(row))
                return dsv_simulation.run(directory / 'scope.json', store.credential_reference(row['account_id']),
                    directory / 'profile.json', directory / 'calendar.json', directory, pipe=store.pipe_name(row), arm=False,
                    _execution_lock=lease, _reference=clock, **runtime_options)
            phase(1, 'STARTUP_READING_MARKET')
            from online_trader.runtime.startup_metadata import read_metadata
            codes = [symbol['code'] for symbol in source_profile.to_record()['settings']['symbols']]
            reference = store.credential_reference(row['account_id'])
            metadata = read_metadata(reference, codes, now=instant)
            current = clock.now() if clock is not None else _now(now)
            if current >= _utc(request['expires_at_utc']):
                raise TraderError('E_DESKTOP_START_REQUEST', ERROR_MESSAGES['E_DESKTOP_START_REQUEST'])
            trigger = request.get('timed_trigger')
            if trigger and trigger != timed_start_trigger(source_profile, _utc(request['created_at_utc']),
                    on_launch=trigger=='AUTO_START_ON_LAUNCH', metadata=metadata):
                raise TraderError('E_CALENDAR_INCOMPLETE', '定时启动未找到匹配的当前或临近交易时段。')
            if 'scheduled_operator' in request:
                if check_scheduled_authorization() != request['scheduled_operator']:
                    raise TraderError('E_AUTH', 'The scheduled start operator changed during connection preparation')
            grant, effective, calendar, details = compile_start_scope(request, store, metadata, now=current)
            for name, record in (('scope.json', grant.value), ('profile.json', effective.to_record()),
                                 ('calendar.json', calendar.to_record()), ('startup-preparation.json', details)):
                publish_new_bytes(canonical_bytes(record), directory / name)
            phase(2, 'STARTUP_PREPARED', account_key=row['account_key'],
                  trading_day=grant.value['trading_day'], pipe=store.pipe_name(row))
            # The metadata connection has closed. Transfer the still-held
            # account lease to the original owner so another window cannot
            # switch accounts or race a second login between these phases.
            row = _selected(store, request['account_id'])
            return dsv_simulation.run(directory / 'scope.json', reference, directory / 'profile.json',
                directory / 'calendar.json', directory, pipe=store.pipe_name(row), arm=request['arm'],
                _execution_lock=lease, **({'_reference': clock} if clock is not None else {}), **runtime_options)
    except Exception as error:
        code = error.code if isinstance(error, TraderError) else 'E_DESKTOP_START'
        failure = {'status': 'STARTUP_FAILED', 'error_code': code, 'entry_authority': 'DISARMED',
            'exception': exception_record(error),
            'message': ERROR_MESSAGES.get(code, '启动未完成，请检查网络与模拟连接状态后重试。')}
        path = directory / 'startup-failure.json'
        if not path.exists():
            publish_new_bytes(canonical_bytes(failure), path)
        return 2
