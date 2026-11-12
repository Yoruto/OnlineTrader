from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

release = Path(SPECPATH)
root = release.parent
package = root / 'src/online_trader'
# Ledger boot and DSV configuration fingerprints use the exact source bytes.
# PyInstaller's PYZ contains bytecode, so keep these files beside their modules.
fingerprinted_sources = (
    'accounting/book.py', 'accounting/report.py', 'contracts/validation.py',
    'execution/policy.py', 'kernel/dsv_strategy.py', 'kernel/engine.py',
    'kernel/execution.py', 'kernel/money.py', 'kernel/scheduler.py',
    'kernel/statistics.py', 'protection/reducer.py', 'scheduling/calendar.py',
    'scheduling/clock.py', 'statement_workflow.py', 'strategy/entry.py',
    'strategy/plan.py', 'strategy/risk.py', 'strategy/statistics.py', 'strategy/stops.py',
)
analysis = Analysis(
    [str(release / 'entry.py')],
    pathex=[str(root / 'src')],
    datas=[(str(package / 'resources'), 'online_trader/resources')]
          + [(str(package / path), str(Path('online_trader', path).parent)) for path in fingerprinted_sources]
          + collect_data_files('tqsdk') + copy_metadata('tqsdk'),
)
archive = PYZ(analysis.pure)
desktop = EXE(archive, analysis.scripts, [], exclude_binaries=True,
              name='OnlineTrader', console=False, contents_directory='_internal', upx=False)
console = EXE(archive, analysis.scripts, [], exclude_binaries=True,
              name='OnlineTraderCLI', console=True, contents_directory='_internal', upx=False)
distribution = COLLECT(desktop, console, analysis.binaries, analysis.datas, name='OnlineTrader', upx=False)
