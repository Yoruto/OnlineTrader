"""Windows workbench entry point. Opening the window is an offline action."""
import argparse
from pathlib import Path
import os

from PySide6.QtWidgets import QApplication, QMessageBox

from online_trader.accounts import AccountStore
from online_trader.desktop.workbench import run
from online_trader.errors import TraderError
from online_trader.runtime.launch import SessionLauncher


def main(argv=None):
    parser = argparse.ArgumentParser(description='OnlineTrader 交易工作台')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--simnow-packet', type=Path)
    source.add_argument('--account-store', type=Path)
    parser.add_argument('--compare-fills', nargs='?', const='', metavar='CSV',
                        help='打开本地成交对照，或预填指定 CSV')
    args = parser.parse_args(argv)
    if args.compare_fills is not None and args.simnow_packet is not None:
        parser.error('--compare-fills and --simnow-packet cannot be combined')
    app = QApplication.instance() or QApplication([])
    app.setApplicationName('OnlineTrader')
    try:
        if args.simnow_packet is not None:
            launcher = SessionLauncher.for_simnow(args.simnow_packet)
            return run(launcher.review()['pipe'], paired_launcher=launcher)
        store = AccountStore(args.account_store or Path(os.environ['LOCALAPPDATA']) / 'OnlineTrader' / 'accounts')
        account_id = store.read()['selected_id']
        account = store.get(account_id) if account_id is not None else None
        if args.compare_fills is not None:
            from online_trader.desktop.reconciliation import run as run_comparison
            return run_comparison(store.ledger_path(account) if account else None,
                                  account['account_key'] if account else None,
                                  statement_path=Path(args.compare_fills) if args.compare_fills else None)
        pipe = store.pipe_name(account) if account else None
        return run(pipe, store=store, account_id=account_id)
    except (TraderError, OSError) as error:
        QMessageBox.critical(None, 'OnlineTrader 启动失败', str(error))
        return 2
