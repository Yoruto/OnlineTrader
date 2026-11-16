"""Run unmodified unittest files on Linux; never replace product modules or skip failures.

Use inside an isolated network namespace. Detailed test statuses and failures are
written to JSON; platform errors are reported as errors, not converted to skips.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[2]
TARGETED = {
    'test_dsv_half_pullback_parity.py', 'test_dsv_profit_stop_config.py',
    'test_dsv_profit_stop_runtime.py', 'test_dsv_runtime_diagnostics.py',
    'test_dsv_runtime_diagnostics_serve.py', 'test_dsv_month_stop.py',
    'test_dsv_month_stop_runtime.py', 'test_dsv_control_endpoint.py',
    'test_dsv_control_endpoint_owner.py',
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def record(self, test, status, detail=None):
        self.records.append({'id': test.id(), 'status': status, 'detail': detail})

    def addSuccess(self, test):
        super().addSuccess(test); self.record(test, 'PASS')

    def addSkip(self, test, reason):
        super().addSkip(test, reason); self.record(test, 'SKIP', reason)

    def addFailure(self, test, err):
        super().addFailure(test, err); self.record(test, 'FAIL', self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err); self.record(test, 'ERROR', self._exc_info_to_string(err, test))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err); self.record(test, 'EXPECTED_FAILURE', self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test); self.record(test, 'UNEXPECTED_SUCCESS')

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(subtest, 'SUBTEST_FAIL' if issubclass(err[0], test.failureException) else 'SUBTEST_ERROR',
                        self._exc_info_to_string(err, test))


def worker(name, destination):
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
    started = time.monotonic()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=name)
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    write_json(destination, {
        'file': name, 'tests_run': result.testsRun, 'successful': result.wasSuccessful(),
        'failure_records': len(result.failures), 'error_records': len(result.errors),
        'skip_records': len(result.skipped), 'expected_failures': len(result.expectedFailures),
        'unexpected_successes': len(result.unexpectedSuccesses),
        'seconds': time.monotonic() - started, 'records': result.records,
    })
    return 0 if result.wasSuccessful() else 1


def driver(output, source_sha):
    if sys.platform != 'linux':
        raise SystemExit('This validation entry is Linux-only.')
    output.mkdir(parents=True, exist_ok=True)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    # The validation branch may add QA and a workflow, never alter audited source/tests.
    changed = subprocess.check_output(
        ['git', 'diff', '--name-only', source_sha, 'HEAD', '--', 'src', 'tests', 'pyproject.toml',
         'requirements-core.lock'], cwd=ROOT, text=True).strip()
    if changed:
        raise SystemExit('Audited production/test content differs from requested source SHA: ' + changed)
    environment = {
        'source_sha': source_sha, 'harness_sha': head, 'python': sys.version,
        'platform': platform.platform(), 'sqlite': __import__('sqlite3').sqlite_version,
        'packages': {dist.metadata['Name']: dist.version for dist in importlib.metadata.distributions()},
        'network_interfaces': sorted(p.name for p in Path('/sys/class/net').iterdir()),
        'product_modules_replaced': False, 'product_source_modified': False,
    }
    # /sys may reflect host links; check the actual namespace through ip, not that directory.
    links = subprocess.check_output(['ip', '-o', 'link', 'show'], text=True)
    environment['network_namespace_links'] = links
    if any(line.split(':', 2)[1].strip().split('@')[0] != 'lo' for line in links.splitlines()):
        raise SystemExit('Refusing tests outside loopback-only network namespace.')
    write_json(output / 'environment.json', environment)
    all_files = sorted((ROOT / 'tests').glob('test_*.py'))
    present = {path.name for path in all_files}
    missing = sorted(TARGETED - present)
    ordered = sorted(all_files, key=lambda p: (p.name not in TARGETED, p.name))
    reports = []
    for path in ordered:
        destination = output / 'modules' / (path.stem + '.json')
        log = output / 'logs' / (path.stem + '.txt')
        log.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            with log.open('w', encoding='utf-8') as stream:
                process = subprocess.run(
                    [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--one', path.name,
                     '--output', str(destination)], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                    timeout=90, env=os.environ.copy())
            if destination.is_file():
                report = json.loads(destination.read_text(encoding='utf-8'))
                report['exit_code'] = process.returncode
            else:
                report = {'file': path.name, 'status': 'PROCESS_ERROR', 'exit_code': process.returncode,
                          'successful': False, 'tests_run': 0, 'records': []}
        except subprocess.TimeoutExpired:
            report = {'file': path.name, 'status': 'TIMEOUT', 'successful': False,
                      'tests_run': 0, 'records': [], 'seconds': time.monotonic() - started}
            write_json(destination, report)
        report['targeted'] = path.name in TARGETED
        reports.append(report)
        write_json(output / 'progress.json', {'completed': len(reports), 'total': len(ordered), 'modules': reports})
        print(f"{len(reports)}/{len(ordered)} {path.name}: "
              f"{'OK' if report['successful'] else 'NOT_PASS'} ({report.get('tests_run', 0)} methods)", flush=True)
    counts = {}
    for report in reports:
        for record in report['records']:
            counts[record['status']] = counts.get(record['status'], 0) + 1
    summary = {'source_sha': source_sha, 'harness_sha': head, 'module_count': len(reports),
               'tests_run': sum(r.get('tests_run', 0) for r in reports), 'event_counts': counts,
               'missing_targeted_files': missing, 'successful': not missing and all(r['successful'] for r in reports),
               'modules': reports,
               'count_note': 'tests_run counts methods; failure events can include multiple subtests or class setup errors.'}
    write_json(output / 'results.json', summary)
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for folder in ('src', 'tests') for p in (ROOT / folder).rglob('*')
              if p.is_file() and '__pycache__' not in p.parts}
    write_json(output / 'source-sha256.json', hashes)
    print(json.dumps({k: v for k, v in summary.items() if k != 'modules'}, ensure_ascii=False), flush=True)
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--one')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-sha')
    args = parser.parse_args()
    if args.one:
        if Path(args.one).name != args.one or not args.one.startswith('test_'):
            parser.error('Invalid test filename')
        raise SystemExit(worker(args.one, args.output))
    if not args.source_sha:
        parser.error('--source-sha is required')
    raise SystemExit(driver(args.output.resolve(), args.source_sha))
