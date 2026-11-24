"""CLI runner for V3.1 offline E2E trading scenarios (PR1)."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
for entry in (str(ROOT), str(SRC_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qa.e2e import assert_offline_qa_environment


DEFAULT_SCENARIOS = (
    ROOT / "qa" / "scenarios" / "smoke" / "entry_001_5lot_partial_stop_loss.py",
)


def _load_scenario_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"qa_e2e_scenario_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load scenario module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise RuntimeError(f"Scenario module lacks callable run(work_dir): {path}")
    return module


def run_scenarios(paths: list[Path], *, work_root: Path | None = None) -> int:
    assert_offline_qa_environment(block_tqsdk=True)
    failures = 0
    for path in paths:
        module = _load_scenario_module(path)
        if work_root is not None:
            scenario_dir = work_root / path.stem
            scenario_dir.mkdir(parents=True, exist_ok=True)
            report = module.run(scenario_dir)
        else:
            with tempfile.TemporaryDirectory(prefix=f"e2e-{path.stem}-") as tmp:
                report = module.run(Path(tmp))
        status = report.get("status", "UNKNOWN")
        print(f"[{status}] {report.get('scenario')} (rounds={report.get('rounds_recorded')})")
        if status != "PASS":
            failures += 1
            print(f"  Failure: {report.get('failure')}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OnlineTrader V3.1 Offline TradingE2E Runner")
    parser.add_argument(
        "--scenario",
        action="append",
        type=Path,
        help="Path to one or more scenario .py files (defaults to PR1 smoke scenario)",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        help="Optional directory to preserve evidence artifacts",
    )
    args = parser.parse_args(argv)
    paths = [p.resolve() for p in args.scenario] if args.scenario else list(DEFAULT_SCENARIOS)
    return run_scenarios(paths, work_root=args.work_root.resolve() if args.work_root else None)


if __name__ == "__main__":
    raise SystemExit(main())
