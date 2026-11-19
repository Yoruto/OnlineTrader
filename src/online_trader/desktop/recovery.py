"""Recover one stopped desktop execution owner through its original account lock."""
from contextlib import closing
import sqlite3
import time
from pathlib import Path

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.desktop.runtime_logs import matches_run_identity, read_record
from online_trader.desktop.startup import launch_desktop_request
from online_trader.errors import TraderError
from online_trader.runtime.desktop_bootstrap import _check_existing_ledger, build_start_request
from online_trader.runtime.instance import AccountLock
from online_trader.scheduling.reference import CalibratedClock


def _last_profile(ledger, result, startup_profile):
    try:
        with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
            latest = connection.execute("""
                SELECT payload_json, payload_hash FROM config_revisions
                WHERE json_extract(payload_json, '$.online_grant_hash') IS NOT NULL
                ORDER BY config_revision DESC LIMIT 1
            """).fetchone()
    except sqlite3.Error as error:
        raise TraderError('E_FACT_BARRIER', '无法核对原账本中的策略配置。') from error
    descriptor = strict_loads(latest[0]) if latest is not None else {}
    if (latest is None or content_hash(descriptor) != latest[1] or not result.get('grant_hash')
            or descriptor.get('online_grant_hash') != result['grant_hash']):
        raise TraderError('E_SCOPE', '空仓退出记录与账本最新执行范围不一致。')
    if not descriptor.get('parameters_saved_at_utc'):
        return startup_profile
    profile = DsvProfile.from_record(descriptor.get('profile'))
    if profile.content_hash != descriptor.get('profile_hash'):
        raise TraderError('E_CONFIG', '原账本保存的策略参数校验失败。')
    return profile


def recover_workbench(client, store, account_id, binding, previous_run, interrupted):
    account_key, boot, _, _ = binding
    account = store.get(account_id)
    if account['account_key'] != account_key or store.read()['selected_id'] != account_id:
        raise TraderError('E_SCOPE', '恢复期间选中的账号已变化。')
    expected_root = (store.directory / 'desktop-starts').resolve()
    if (not matches_run_identity(previous_run, (account_key, boot)) or previous_run.name != 'run'
            or previous_run.parent.parent.resolve() != expected_root):
        raise TraderError('E_SCOPE', '无法确认原执行器的运行目录。')
    original = read_record(previous_run / 'confirmed-request.json', limit=4 * 1024 * 1024) or {}
    if (original.get('account_key') != account_key or original.get('account_id') != account_id
            or Path(original.get('account_store') or '').resolve() != store.directory.resolve()):
        raise TraderError('E_SCOPE', '原启动请求的账号或账本目录无法核对。')
    profile = DsvProfile.from_record(original.get('profile'))
    if profile.content_hash != original.get('profile_hash'):
        raise TraderError('E_CONFIG', '原运行的参数校验失败。')

    try:
        view = client.workbench()
    except TraderError as error:
        if error.code not in {'E_TIMEOUT', 'E_PIPE', 'E_PIPE_CLOSED'}:
            raise
        view = None
    if view is not None:
        config = view['configuration']
        if (view['account_key'], view['kernel_boot_id'], config['rule_set_id'], config['config_revision']) != binding:
            raise TraderError('E_SCOPE', '原执行器或策略配置已变化，请刷新后重新核对。')
        if config.get('manual_workbench'):
            raise TraderError('E_SCOPE', '强制恢复只适用于普通模拟策略执行器。')
        if view['entry_authority'] != 'DISARMED':
            raise TraderError('E_NOT_READY', '原执行器已连接，仍允许开仓。请先停止策略。')
        connection = view['connection']
        if (view['truncated'] or not connection['broker_connected'] or not connection['synchronized']
                or connection['fault'] or connection['recovery_required'] or connection['storage_failed']):
            raise TraderError('E_FACT_BARRIER', '原执行器仍有柜台同步、持仓核对或账本故障待处理。')
        if not config.get('strategy_stop_requested'):
            if interrupted():
                raise TraderError('E_NOT_READY', '工作台已退出，恢复请求已中止。')
            try:
                receipt = client.command('STOP_DSV_STRATEGY', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                                         {'recover_flat': True})
            except TraderError as error:
                if error.code == 'E_SCHEMA':
                    raise TraderError('E_SCHEMA', '原执行器版本不支持空仓恢复请求，请从原工作台正常停止。') from error
                if error.code not in {'E_TIMEOUT', 'E_PIPE', 'E_PIPE_CLOSED'} or client.last_command is None:
                    raise
                receipt = client.command_status(client.last_command)
                if receipt['status'] not in {'SUCCEEDED', 'REJECTED', 'FAILED'}:
                    raise TraderError('E_NOT_READY', '原恢复退出命令尚无终态，请查询原回执。')
            client.last_command = None
            if receipt['status'] != 'SUCCEEDED':
                if receipt['error_code'] == 'E_SCHEMA':
                    raise TraderError('E_SCHEMA', '原执行器版本不支持空仓恢复请求，请从原工作台正常停止。')
                raise TraderError(receipt['error_code'] if receipt['error_code'] != 'NONE' else 'E_NOT_READY',
                                  receipt['reason'])

    deadline = time.monotonic() + 15
    while not interrupted():
        result = read_record(previous_run / 'result.json') or {}
        proof = result.get('verification') or {}
        if (result.get('status') == 'DSV_STOPPED' and result.get('execution_scope') == 'ONLINE_SIMULATION'
                and (result.get('account_key'), result.get('kernel_boot_id')) == (account_key, boot)
                and proof.get('whole_account_flat_claimed') is True):
            try:
                with AccountLock(account_key):
                    _check_existing_ledger(store, account)
                    profile = _last_profile(store.ledger_path(account), result, profile)
            except TraderError as error:
                if error.code not in {'E_INSTANCE_EXISTS', 'E_ACCOUNT_LOCKED'}:
                    raise
            else:
                break
        if time.monotonic() >= deadline:
            raise TraderError('E_NOT_READY', '尚未取得原执行器的空仓退出回执，或账号锁仍被持有。')
        time.sleep(.25)
    if interrupted():
        raise TraderError('E_NOT_READY', '工作台已退出，恢复请求已中止。')

    clock = CalibratedClock()
    request = build_start_request(store, account_id, profile, arm=False, now=clock.now())
    if interrupted():
        raise TraderError('E_NOT_READY', '工作台已退出，恢复请求已中止。')
    return launch_desktop_request(store, request)
