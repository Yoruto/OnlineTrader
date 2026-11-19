"""Standalone runtime IPC rehearsal using only native-shaped offline facts."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from secrets import token_urlsafe
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace
from uuid import uuid4

from online_trader.errors import TraderError
from online_trader.kernel.server import ControlServer
from online_trader.runtime.operator_api import RuntimeCommandClient
from online_trader.runtime.view_process import ViewProcess
from online_trader.transport.runtime_view import RuntimeViewClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_online_dsv import OnlineDsvTests


@contextmanager
def fixture(output):
    case = OnlineDsvTests()
    case.setUp()
    case.temp.cleanup()
    case.temp = SimpleNamespace(name=str(output), cleanup=lambda: None)
    case.path = output / 'ledger.db'
    try:
        case.start(quantity=5)
        case.quote()
        orders = case.api.inserted
        if len(orders) != 1 or orders[0]['volume'] != 5:
            raise AssertionError('The fixed fixture must submit an opening target of five')
        case.api.fill(orders[0]['order_id'], 3, 1055, finish=False)
        case.session.step()
        yield case
    finally:
        case.tearDown()


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
    account = case.kernel.account_key
    pipe = 'OnlineTrader.RuntimeReplay.' + uuid4().hex
    token = token_urlsafe(48)
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
    private = output / 'private'
    private.mkdir()
    (private / 'operators.json').write_text(json.dumps({'schema_version': 'ONLINE_TRADER_OPERATORS/1', 'operators': [
        {'id': 'qa', 'token_sha256': sha256(token.encode()).hexdigest(), 'account_keys': [account],
         'operations': ['MANUAL_CLOSE'], 'expires_at_utc': expiry}]}), 'utf-8')
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
            more_fills.set()
            holding = wait_view(view_client, lambda view: any(c['remaining_quantity'] == 5 for c in view['cycles']), deadline)
            observations['complete_seq'] = holding['checkpoint_event_seq']
            cycle = next(c for c in holding['cycles'] if c['remaining_quantity'] == 5)
            policy = next(row['policy_id'] for row in holding['policies'] if row['policy_kind'] == 'EXECUTION')
            cycle_id = cycle['position_cycle_id']
            command = {'command_id': 'qa-close-five', 'operation': 'MANUAL_CLOSE',
                       'scope': {'scope_type': 'CYCLE', 'scope_ids': [cycle_id]},
                       'parameters': {'position_cycle_id': cycle_id, 'quantity': 5,
                                      'execution_policy_id': policy, 'external_takeover_id': None}}
            client = RuntimeCommandClient(pipe)
            observations['submit'] = client.request('submit', command, operator='qa', token=token)
            closed = wait_view(view_client, lambda view: any(
                c['position_cycle_id'] == cycle_id and c['remaining_quantity'] == 0 for c in view['cycles']), deadline)
            observations['close_seq'] = closed['checkpoint_event_seq']
            observations['receipt'] = client.request('receipt', {'command_id': 'qa-close-five'}, operator='qa', token=token)
            if observations['receipt']['status'] != 'SUCCEEDED':
                raise AssertionError('The close did not reach a proved terminal receipt')
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
            case.api.fill(case.api.inserted[0]['order_id'], 2, 1055)
        for order in case.api.inserted[1:]:
            if order['offset'] in {'CLOSE', 'CLOSETODAY'} and case.api.orders[order['order_id']].volume_left:
                case.api.fill(order['order_id'], case.api.orders[order['order_id']].volume_left, 1055)
        case.quote(milliseconds=20)

    interface = ViewProcess(pipe, account, output, case.path)
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
    with fixture(output) as case:
        result = replay(case, output)
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
