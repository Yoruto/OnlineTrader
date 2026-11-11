"""Offline held-position IPC reproduction; never imports or connects a broker SDK."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import patch

import online_trader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # External fixtures are never in the product wheel.
from tests.test_online_simulation import FakeOnlinePort, grant_for
from online_trader.kernel import Kernel
from online_trader.kernel.server import ControlServer
from online_trader.runtime.simulation import SimulationSession


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expect-timeout', action='store_true')
    parser.add_argument('--closing-read', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    grant = grant_for(out / 'ledger.db'); port = FakeOnlinePort(grant)
    port.clock.monotonic_anchor = time.monotonic() - (0 if args.closing_read else 121)
    port.poll_updates = lambda seconds: time.sleep(.005)
    started = threading.Event(); finished = threading.Event(); stop = threading.Event()
    outcomes = []; failures = []
    pipe = 'OnlineTrader.Holding.' + uuid.uuid4().hex
    installed = Path(online_trader.__file__).resolve().parent
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (
        installed/'runtime/simulation.py', installed/'kernel/server.py', installed/'transport/status_client.py')}

    def slow_reference():
        started.set()
        time.sleep(6.2)  # Four 1.5-second failed NTP samples exceed the client budget.
        finished.set()
        from online_trader.errors import TraderError
        raise TraderError('E_CLOCK', 'Injected offline reference timeout')

    with Kernel(grant.value['ledger_path'], grant.account_key, port=port,
                simulation_grant=grant, risk_budget='1000') as kernel:
        sim = kernel.simulation; sim.initialize(); sim.arm(); kernel.wait_idle()
        plan = sim.plan('DCE.p2701', '1000', stop_points='16', take_profit_points='50')
        context = kernel.issue_entry_context('fixture', 'DCE.p2701')
        child = kernel.admit_entry(plan, 'offline-held-position', '1000',
                                   context_id=context['context_id'], peer_id='fixture')
        kernel.wait_idle(); port.fill(child); sim.tick()
        if args.closing_read: kernel.pause(); kernel.wait_idle()
        session = SimulationSession(kernel, out/'session', emit=lambda event: None)
        server = ControlServer(kernel, pipe)

        def client():
            try:
                if not args.closing_read: assert started.wait(5), 'Reference refresh never started'
                for command in (('status',) if args.closing_read else ('status', 'pause', 'status')):
                    begin = time.monotonic()
                    run = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-m', 'online_trader', command,
                                          '--pipe', pipe], capture_output=True, encoding='utf-8', timeout=12,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    row = {'command': command, 'returncode': run.returncode,
                           'elapsed_seconds': time.monotonic()-begin, 'stdout': run.stdout, 'stderr': run.stderr,
                           'refresh_finished': finished.is_set()}
                    outcomes.append(row)
                    if run.returncode == 0:
                        data = json.loads(run.stdout)
                        if command == 'status':
                            cycle = next(r for r in data['records'] if r['record_type'] == 'CYCLE')
                            assert cycle['remaining_quantity'] == 1, data
                        else:
                            assert data['status'] == 'SUCCEEDED', data
                    elif not (args.expect_timeout and len(outcomes) == 1 and 'E_TIMEOUT' in run.stderr):
                        failures.append(row)
                if not args.closing_read: assert finished.wait(8), 'Reference worker did not finish'
            except BaseException as error:
                failures.append(repr(error))
            finally:
                stop.set()

        worker = threading.Thread(target=client); worker.start()
        begin = time.monotonic()
        def tick():
            session.step()
            if args.closing_read and any(peer.ready for peer in server._peer_snapshot()): stop.set()
            if time.monotonic()-begin > 25:
                failures.append('Owner rehearsal deadline'); stop.set()
        with patch('online_trader.runtime.simulation.measure_reference', slow_reference):
            try: server.serve(stop, owner_tick=tick)
            finally:
                worker.join(timeout=12)
                if hasattr(session, 'close'): session.close()
        final = kernel.snapshot()
        assert not final['armed'] and final['cycles'][0]['protection']['owned_qty'] == 1
        assert len(port.sent) == 1  # The offline recorded entry only; no real send.
        expected = bool(outcomes and outcomes[0]['returncode'] == 0) if args.closing_read else bool(
            outcomes and outcomes[0]['returncode'] != 0) if args.expect_timeout else bool(
            len(outcomes) == 3 and all(r['returncode'] == 0 for r in outcomes)
            and not outcomes[0]['refresh_finished'] and not outcomes[1]['refresh_finished'])
        report = {'status': 'PASS' if expected and not failures else 'FAIL',
                  'scope': 'OFFLINE_INJECTED_FACTS_REAL_WINDOWS_PIPE_SEPARATE_CLIENT_PROCESS',
                  'historical_online_cause_proven': False, 'broker_connected': False,
                  'version': online_trader.__version__, 'installed_root': str(installed),
                  'source_sha256': source_hashes, 'expected_timeout': args.expect_timeout,
                  'read_overlapped_owner_stop': args.closing_read,
                  'outcomes': outcomes, 'failures': failures, 'final_snapshot': final,
                  'remaining_owned_quantity': 1, 'entry_authority': 'DISARMED',
                  'sqlite_integrity': kernel.ledger.query('PRAGMA integrity_check')}
        (out/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('status', 'version', 'expected_timeout', 'broker_connected')}))
        return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
