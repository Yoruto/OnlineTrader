from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

release = Path(SPECPATH)
root = release.parent
package = root / 'src/online_trader'
# Ledger boot and DSV configuration fingerprints use the exact source bytes.
# PyInstaller's PYZ contains bytecode, so keep these files beside their modules.
fingerprinted_sources = (
    'accounting/book.py', 'accounting/report.py', 'contracts/validation.py',
    'dsv_core/business_statistics.py', 'dsv_core/entry_rules.py', 'dsv_core/price_rules.py',
    'dsv_core/protection_rules.py', 'dsv_core/sizing_rules.py',
    'execution/policy.py', 'kernel/dsv_strategy.py', 'kernel/engine.py',
    'kernel/execution.py', 'kernel/money.py', 'kernel/scheduler.py',
    'kernel/statistics.py', 'protection/reducer.py', 'scheduling/calendar.py',
    'scheduling/clock.py', 'statement_workflow.py', 'strategy/entry.py',
    'strategy/plan.py', 'strategy/risk.py', 'strategy/statistics.py', 'strategy/stops.py',
)
# The original five-symbol DSV fixture stays with the source. The immutable
# approval and schema resources are sufficient to validate saved ledgers.
resource_root = package / 'resources'
resources = [
    (str(path), (Path('online_trader/resources') / path.relative_to(resource_root).parent).as_posix())
    for path in sorted(resource_root.rglob('*'))
    if path.is_file() and path != resource_root / 'dsv-baseline-20261109.json'
]
analysis = Analysis(
    [str(release / 'entry.py')],
    pathex=[str(root / 'src')],
    datas=resources
          + [(str(package / path), str(Path('online_trader', path).parent)) for path in fingerprinted_sources]
          + collect_data_files('tqsdk') + copy_metadata('tqsdk'),
)
archive = PYZ(analysis.pure)
desktop = EXE(archive, analysis.scripts, [], exclude_binaries=True,
              name='OnlineTrader', console=False, contents_directory='_internal', upx=False)
console = EXE(archive, analysis.scripts, [], exclude_binaries=True,
              name='OnlineTraderCLI', console=True, contents_directory='_internal', upx=False)
distribution = COLLECT(desktop, console, analysis.binaries, analysis.datas, name='OnlineTrader', upx=False)
