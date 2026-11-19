"""Windows workbench entry point. Opening the window is an offline action."""
import argparse
import json
from contextlib import nullcontext
from pathlib import Path
import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from online_trader import __version__
from online_trader.accounts import AccountStore
from online_trader.desktop.dingtalk import DingTalkSettings
from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock
from online_trader.runtime.launch import SessionLauncher


def license_status_text():
    lines = ('程序：OnlineTrader', f'版本：{__version__}', '内核来源：OnlineTrader',
             '机器码：—', '状态：本地模拟功能可用', '到期：—', '功能：快期模拟、SimNow 模拟',
             '实盘开新仓：未启用')
    return '\n'.join(lines) + '\n'


def bundled_defaults():
    if not getattr(sys, 'frozen', False):
        return None, None, None
    directory = Path(sys.executable).parent
    from online_trader.update_install import managed_root
    root = managed_root(sys.executable)
    if root is not None and (root / 'OnlineTrader.exe').is_file():
        directory = root
    profile = directory / 'OnlineTrader-parameters.json'
    config = directory / 'OnlineTrader-defaults.json'
    if not config.is_file():
        return profile if profile.is_file() else None, None, None
    try:
        data = json.loads(config.read_text(encoding='utf-8-sig'))
        accounts = data.get('simulation')
        ding = data.get('dingtalk')
        notification = DingTalkSettings.parse(**ding) if ding is not None else None
    except (ValueError, AttributeError, TypeError, TraderError):
        raise TraderError('E_CONFIG_FILE', '请检查程序目录中的 OnlineTrader-defaults.json。') from None
    return profile if profile.is_file() else None, accounts, notification


def main(argv=None):
    parser = argparse.ArgumentParser(description='OnlineTrader 交易工作台')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--simnow-packet', type=Path)
    source.add_argument('--account-store', type=Path)
    source.add_argument('--runtime-pipe', help='通过独立接口附着交易运行时')
    parser.add_argument('--dsv-config', type=Path, help='读取原 DSV 已保存的业务参数')
    parser.add_argument('--allow-multi', action='store_true', help='允许打开多个工作台窗口')
    parser.add_argument('--autostart', action='store_true', help='打开工作台并显示守护启动反馈')
    parser.add_argument('--license', action='store_true', help='写入本机授权状态诊断后退出')
    parser.add_argument('--compare-fills', nargs='?', const='', metavar='CSV',
                        help='打开本地成交对照，或预填指定 CSV')
    args = parser.parse_args(argv)
    if args.license:
        directory = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path.cwd()
        try:
            (directory/'授权状态.txt').write_text(license_status_text(), encoding='utf-8')
        except OSError:
            return 2
        return 0
    if args.dsv_config is not None and args.simnow_packet is not None:
        parser.error('--dsv-config and --simnow-packet cannot be combined')
    if args.compare_fills is not None and args.simnow_packet is not None:
        parser.error('--compare-fills and --simnow-packet cannot be combined')
    app = QApplication.instance() or QApplication([])
    app.setApplicationName('OnlineTrader')
    try:
        if args.runtime_pipe is not None:
            with (nullcontext() if args.allow_multi else AccountLock('DESKTOP:ONLINE-TRADER')):
                # Runtime attachment deliberately reuses the established dev
                # WorkbenchWindow/WorkbenchLayout.  Only the state and command
                # transport changes; users keep the exact same interface.
                from online_trader.desktop.workbench import run
                return run(args.runtime_pipe, runtime_view=True)
        from online_trader.desktop.workbench import run
        if args.simnow_packet is not None:
            launcher = SessionLauncher.for_simnow(args.simnow_packet)
            with (nullcontext() if args.allow_multi else AccountLock('DESKTOP:ONLINE-TRADER')):
                return run(launcher.review()['pipe'], paired_launcher=launcher)
        store = AccountStore(args.account_store or Path(os.environ['LOCALAPPDATA']) / 'OnlineTrader' / 'accounts')
        profile, accounts, notification = (None, None, None)
        if args.compare_fills is None and args.account_store is None:
            profile, accounts, notification = bundled_defaults()
        account_id = store.read()['selected_id']
        account = store.get(account_id) if account_id is not None else None
        if args.compare_fills is not None:
            from online_trader.desktop.reconciliation import run as run_comparison
            return run_comparison(store.ledger_path(account) if account else None,
                                  account['account_key'] if account else None,
                                  statement_path=Path(args.compare_fills) if args.compare_fills else None)
        pipe = store.pipe_name(account) if account else None
        with (nullcontext() if args.allow_multi else AccountLock('DESKTOP:ONLINE-TRADER')):
            if accounts is not None:
                account_id = store.initialize_simulation_accounts(accounts, packaged=True)['selected_id']
                pipe = store.pipe_name(store.get(account_id)) if account_id is not None else None
            if (getattr(sys, 'frozen', False) and not args.allow_multi
                    and args.account_store is None and args.dsv_config is None):
                from online_trader.update_install import managed_root
                root = managed_root(sys.executable)
                if root is not None:
                    return run(pipe, store=store, account_id=account_id,
                               dsv_config=profile, autostart=args.autostart, update_root=root,
                               notification=notification)
            return run(pipe, store=store, account_id=account_id, dsv_config=args.dsv_config or profile,
                       autostart=args.autostart, notification=notification)
    except (TraderError, OSError) as error:
        if isinstance(error, TraderError) and error.code == 'E_INSTANCE_EXISTS' and args.autostart:
            return 0
        QMessageBox.critical(None, 'OnlineTrader 启动失败', str(error))
        return 2
