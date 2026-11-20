"""Standalone runtime IPC rehearsal using only native-shaped offline facts."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from threading import Event, Thread
import time
from uuid import uuid4

from online_trader.errors import TraderError
from online_trader.kernel.server import ControlServer
from online_trader.runtime.operator_api import RuntimeWorkbenchClient
from online_trader.runtime.view_process import ViewProcess
from online_trader.transport.runtime_account import RuntimeAccountClient
from online_trader.transport.runtime_view import RuntimeViewClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qa.runtime_fixture import OfflineRuntimeFixture


def wait_view(client, predicate, deadline):
    while time.monotonic() < deadline:
        try:
            result = client.read()
        except TraderError:
            time.sleep(.05)
            continue
        if result['kind'] == 'BASELINE':
            view = result['body']['view']
            if predicate(view):
                return view
        time.sleep(.05)
    raise TimeoutError('The expected durable quantity was not visible through the public interface')


def replay(case, output):
    account = case.account_key
    pipe = 'OnlineTrader.RuntimeReplay.' + uuid4().hex
    more_fills = Event()
    stop = Event()
    observations = {}
    failures = []
    deadline = time.monotonic() + 30

    def observe_and_close():
        view_client = RuntimeViewClient(pipe)
        try:
            partial = wait_view(view_client, lambda view: any(c['remaining_quantity'] == 3 for c in view['cycles']), deadline)
            observations['partial_seq'] = partial['checkpoint_event_seq']
            try:
                observations['history'] = view_client.history(limit=5)
            except TraderError as error:
                # History is a rebuildable broker cache. Its absence or damage
                # must stay visible without aborting the execution-owner replay.
                observations['history'] = {
                    'coverage': 'UNAVAILABLE', 'error_code': error.code, 'message': str(error),
                }
            account_client = RuntimeAccountClient(pipe, expected_account_key=account)
            while time.monotonic() < deadline:
                account_view = account_client.read()
                if account_view['status'] == 'READY':
                    observations['account'] = account_view
                    break
                time.sleep(.05)
            if 'account' not in observations:
                raise TimeoutError('The attached Runtime account view did not become READY')
            more_fills.set()
            holding = wait_view(view_client, lambda view: any(c['remaining_quantity'] == 5 for c in view['cycles']), deadline)
            observations['complete_seq'] = holding['checkpoint_event_seq']
            cycle = next(c for c in holding['cycles'] if c['remaining_quantity'] == 5)
            policy = next(row['policy_id'] for row in holding['policies'] if row['policy_kind'] == 'EXECUTION')
            cycle_id = cycle['position_cycle_id']
            command = {'command_id': 'qa-close-five', 'operation': 'MANUAL_CLOSE',
                       'scope': {'scope_type': 'CYCLE', 'scope_ids': [cycle_id]},
                       'parameters': {'position_cycle_id': cycle_id, 'quantity': 5,
                                      'execution_policy_id': policy, 'external_takeover_id': None},
                       'binding': {'account_key': holding['account_key'],
                                   'kernel_boot_id': holding['kernel_boot_id'],
                                   'config_revision': holding['configuration']['config_revision']}}
            client = RuntimeWorkbenchClient(pipe)
            observations['control_channel'] = 'SAME_USER_WORKBENCH'
            observations['submit'] = client.request('submit', command)
            closed = wait_view(view_client, lambda view: any(
                c['position_cycle_id'] == cycle_id and c['remaining_quantity'] == 0 for c in view['cycles']), deadline)
            observations['close_seq'] = closed['checkpoint_event_seq']
            observations['receipt'] = client.request('receipt', {'command_id': 'qa-close-five'})
            if observations['receipt']['status'] != 'SUCCEEDED':
                raise AssertionError('The close did not reach a proved terminal receipt')
            observations['duplicate'] = client.request('submit', command)
            conflict = command | {'parameters': command['parameters'] | {'quantity': 4}}
            try:
                client.request('submit', conflict)
            except TraderError as error:
                observations['duplicate_conflict'] = error.code
            if observations.get('duplicate_conflict') != 'E_DUPLICATE_CONFLICT':
                raise AssertionError('A changed body reused the command ID without a conflict')
        except (AssertionError, OSError, TimeoutError, TraderError, KeyError, StopIteration) as error:
            failures.append(str(error))
        finally:
            stop.set()

    def tick():
        if time.monotonic() >= deadline:
            stop.set()
            return
        if more_fills.is_set():
            more_fills.clear()
            case.fill(case.api.inserted[0]['order_id'], 2, 1055)
        for order in case.api.inserted[1:]:
            if order['offset'] in {'CLOSE', 'CLOSETODAY'} and case.api.orders[order['order_id']].volume_left:
                case.fill(order['order_id'], case.api.orders[order['order_id']].volume_left, 1055)
        case.quote(milliseconds=20)

    interface = ViewProcess(pipe, account, output, case.path, output / 'account-history.db')
    observer = Thread(target=observe_and_close, daemon=True)
    try:
        server = ControlServer(case.kernel, pipe, trusted_client_pid=interface.trusted_pid)
        observer.start()
        server.serve(stop, owner_tick=tick, publish_workbench=True)
        observer.join(timeout=4)
    finally:
        interface.close()
    complete = not failures and 'receipt' in observations
    return {'status': 'PASS' if complete else 'FAIL', 'source': 'OFFLINE_NATIVE_SHAPED_FACTS',
            'broker_connected': False, 'real_orders_sent': 0, 'observations': observations, 'errors': failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixture', choices=('partial-fill-and-close',), default='partial-fill-and-close')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    case = OfflineRuntimeFixture(output, quantity=5)
    try:
        case.quote()
        orders = case.api.inserted
        if len(orders) != 1 or orders[0]['volume'] != 5:
            raise AssertionError('The fixed fixture must submit an opening target of five')
        case.fill(orders[0]['order_id'], 3, 1055, finish=False)
        result = replay(case, output)
    finally:
        case.close()
    boundary = ROOT / 'qa/fixtures/runtime/runtime-boundaries.json'
    result['fixture'] = {'path': boundary.relative_to(ROOT).as_posix(),
                         'sha256': sha256(boundary.read_bytes()).hexdigest()}
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
