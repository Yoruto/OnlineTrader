"""Compare strategy, SDK pump and outbound packets in the same offline replay."""

import argparse
import ast
import asyncio
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import MethodType, SimpleNamespace
from typing import List, Optional, Union
from uuid import uuid4
from zipfile import ZipFile

from qa.dsv_parity.original_loop import _BEIJING, _Bars, _deny_external_effects, replay_original
from qa.dsv_parity.sdk_reference_probe import method
from qa.dsv_parity.sdk_round_replay import (Entity, OfflineApi, _frozen_source, bind_target, drain_tasks, stop_tasks)
from qa.dsv_parity.verify_reference import verify


_ORIGINAL_API_SHA = '264fff3e3979a156294d8bca745c8aa6c26136c8cbe58ef2bc7ceffd8b811c96'
_CANDIDATE_API_SHA = '3b822aa7acd3212ccb22f0a64679e1bc232c462ede75ca50eff7375ce57efd36'


def _at(value):
    return datetime.fromisoformat(value)


def _ns(value):
    return round(_at(value).timestamp() * 1_000_000_000)


class ScheduledApi(OfflineApi):
    """Run the actual SDK pump over local packet queues and one replay clock."""

    def __init__(self, symbol, source, loop):
        super().__init__(symbol)
        self.loop = self._loop = loop
        self.outputs = []
        self.identities = {}
        self.pending = []
        self.current = None
        self.price_bars = _Bars()
        self.account = Entity(balance=100000, available=100000, margin=0, frozen_margin=0, risk_ratio=0)
        self.position.margin = 0
        self.quote.update(pre_close=1060, expired=False, volume_multiple=10, margin=1000,
                          price_tick=1, lower_limit=900, upper_limit=1200)
        self._serials = {}
        self._sync_diffs, self._pending_diffs = [], []
        self._is_slave = self._pending_peek = False
        self._data = {}
        self._prototype = None
        self._risk_manager = SimpleNamespace(_on_recv_data=lambda _diff: None)
        self._send_chan = SimpleNamespace(send_nowait=self.pending.append)
        self._clear_running_loop = lambda: (None, None)
        self._restore_running_loop = lambda *_args: None
        tree = ast.parse(source)
        namespace = {'Optional': Optional, 'Union': Union, 'List': List, 'asyncio': asyncio,
                     '_merge_diff': self._merge}
        self._sdk_wait = MethodType(method(tree, 'TqApi', 'wait_update', namespace), self)
        for name in ('_wait_update', '_send_pack'):
            setattr(self, name, MethodType(method(tree, 'TqApi', name, namespace), self))

    def wait_update(self, *, deadline):
        self.current['wait_update'] = True
        result = self._sdk_wait(deadline=deadline)
        self.current['wait_returned'] = result
        return result

    def create_task(self, coroutine):
        task = self.loop.create_task(coroutine)
        self.tasks.append(task)
        return task

    def _merge(self, _data, diff, *_args, **_kwargs):
        self.apply_frame(diff['fixture'])
        self.finish_merge(diff['fixture'])

    def _run_until_idle(self, *, async_run):
        self.loop.run_until_complete(drain_tasks(self))
        for packet in self.pending:
            if packet['aid'] in ('insert_order', 'cancel_order'):
                self.current['outbound'].append(packet)
        sends = [p for p in self.current['outbound'] if p['aid'] == 'insert_order']
        for index, packet in enumerate(sends):
            aliases = self.input.get('wire_aliases', ())
            alias = aliases[index] if index < len(aliases) else f'unexpected-{self.input["id"]}-{index}'
            self.identities[alias] = packet['order_id']
        self.pending.clear()

    def _run_until_task_done(self, *, task, deadline):
        self.loop.run_until_complete(task)
        self.tasks.remove(task)

    async def _fetch_msg(self):
        self._pending_diffs = [] if self.input.get('no_update') else [{'fixture': self.input}]

    def receive(self, frame, finish_merge):
        self.input, self.finish_merge = frame, finish_merge
        self.current = {'id': frame['id'], 'wait_update': True, 'targets': [], 'api_calls': [], 'outbound': []}
        self.outputs.append(self.current)
        return self.wait_update(deadline=_at(frame['wall_time']).timestamp() + .5)

    def apply_frame(self, frame):
        self.apply(frame, self.identities)
        self.current['finished'] = [fact['child_order_id'] for fact in frame['native']
                                    if fact['kind'] == 'ORDER' and fact['status'] != 'ALIVE']
        market = frame['market'][0]
        if market['ticks']:
            tick = market['ticks'][-1]
            self.quote.update(highest=float(tick['day_high']), lowest=float(tick['day_low']),
                              datetime=_at(tick['native_time']).astimezone(_BEIJING).isoformat())
            if tick['candle_id'] is not None:
                self.price_bars[:] = [{'datetime': _ns(tick['native_time']) // (60 * 10**9) * (60 * 10**9)}]
        for fact in frame['native']:
            order = self.orders[self.identities[fact['child_order_id']]]
            if fact['kind'] == 'ORDER':
                order['insert_date_time'] = order.insert_date_time or _ns(frame['wall_time'])
            else:
                trade = order.trade_records[fact['native_trade_id']]
                trade.update(trade_id=fact['native_trade_id'], order_id=order.order_id,
                             trade_date_time=_ns(frame['wall_time']), exchange_id=order.exchange_id,
                             instrument_id=order.instrument_id, direction=order.direction, offset=order.offset)
        self.position.pos_long_his = self.position.pos_short_his = 0
        self.quote['expire_datetime'] = market.get('expiry_date')

    def insert_order(self, symbol, direction, offset, volume, limit_price, *, order_id, account):
        order = super().insert_order(symbol, direction, offset, volume, limit_price,
                                     order_id=order_id, account=account)
        exchange, instrument = symbol.split('.', 1)
        order.update(exchange_id=exchange, instrument_id=instrument, limit_price=limit_price,
                     insert_date_time=0, _this_session=True, is_error=False)
        packet = {'aid': 'insert_order', 'order_id': order_id, 'direction': direction, 'offset': offset,
                  'volume': volume, 'limit_price': limit_price}
        self.current['api_calls'].append(packet)
        self._send_pack(packet)
        return order

    def cancel_order(self, order_id, *, account):
        packet = {'aid': 'cancel_order', 'order_id': order_id}
        self.current['api_calls'].append(packet)
        self._send_pack(packet)

    def close(self):
        self.current = {'api_calls': []}
        self.loop.run_until_complete(stop_tasks(self))


class OriginalScheduler:
    def __init__(self, archive, symbol, loop):
        self.source = _frozen_source(archive)
        with ZipFile(archive) as bundle:
            api = bundle.read('trading/_internal/tqsdk/api.py')
        verify('original api.py', api, _ORIGINAL_API_SHA)
        self.api = ScheduledApi(symbol, api, loop)

    def bind_target(self, symbol, price):
        async def bind():
            return bind_target(self.api, self.source, price)
        self.target = self.api.loop.run_until_complete(bind())

    def set_target_volume(self, volume):
        self.api.current['targets'].append(volume)
        self.target.set_target_volume(volume)

    def wait_update(self, frame, apply_frame):
        # Orders created during the pre-fetch pump receive their own replay
        # alias before the incoming native batch is applied.
        return self.api.receive(frame, apply_frame)


class CandidateApi(ScheduledApi):
    def get_quote(self, symbol):
        return self.quote

    def get_kline_serial(self, *_args, **_kwargs):
        return self.price_bars

    def get_position(self, *_args, **_kwargs):
        return self.position

    def get_account(self, **_kwargs):
        return self.account

    def get_order(self, order_id=None, *, account):
        return self.orders if order_id is None else self.orders[order_id]

    def get_trade(self, **_kwargs):
        return {key: trade for order in self.orders.values() for key, trade in order.trade_records.items()}


def _calendar(packet):
    from online_trader.scheduling.calendar import TradingCalendar
    first = packet['rounds'][0]['market'][0]
    starts, ends = first['session_open'], first['session_close']
    window = {'session_id': 'replay', 'trading_day': packet['trading_day'], 'open_utc': starts,
              'close_utc': ends, 'is_opening_session': True, 'force_close_at_end': False}
    return TradingCalendar.from_record({'schema_version': 'TRADING_CALENDAR/1', 'calendar_id': 'offline-replay',
        'calendar_version': '1', 'complete': True, 'coverage_from_utc': starts, 'coverage_to_utc': ends,
        'contracts': [{'contract_id': first['contract_id'], 'sessions': [window]}]})


def candidate(packet, sdk_api, loop, database):
    from online_trader.configuration.dsv import DsvProfile
    from online_trader.dsv_runtime.runner import DsvSession
    from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
    from online_trader.errors import TraderError
    from online_trader.runtime.startup_metadata import SDK_VERSION

    source = sdk_api.read_bytes()
    verify('candidate api.py', source, _CANDIDATE_API_SHA)
    profile = DsvProfile.from_settings(packet['settings'])
    market = packet['rounds'][0]['market'][0]
    api = CandidateApi(market['contract_id'], source, loop)
    clock = lambda: (_at(api.input['wall_time']), api.input['monotonic_ms'])
    account = f'DEV:schedule-{uuid4().hex}'
    adapter = DsvSdkAdapter(api, 'offline-fixture', account_key=account, profile=profile,
                            calendar=_calendar(packet), sdk_version=SDK_VERSION, clock=clock)
    with DsvSession(database, account_key=account, provider='OFFLINE', trading_day=packet['trading_day'],
                    profile=profile, port=adapter) as session:
        session.command({'id': 'start', 'action': 'START'})
        for frame in packet['rounds']:
            api.input, api.finish_merge = frame, lambda _frame: None
            api.current = {'id': frame['id'], 'wait_update': True, 'targets': [], 'api_calls': [], 'outbound': []}
            api.outputs.append(api.current)
            try:
                receipt = adapter.step(session, trading_day=packet['trading_day'], complete=True)
            except TraderError as error:
                api.current['error'] = {'code': error.code, 'message': str(error)}
                break
            for decision in receipt['decisions']:
                if 'target' in decision:
                    goal = decision['target']
                    api.current['targets'].append(goal['quantity'] if goal['direction'] == 'LONG' else -goal['quantity'])
                elif decision.get('remaining') == 0:
                    api.current['targets'].append(0)
            api.current['decisions'] = [row['reason'] for row in receipt['decisions']]
            snapshot = session.snapshot()
            position = snapshot['symbols'][market['contract_id']]['position'] or {}
            if ((requested := position.get('close_request_at')) is not None
                    and _at(requested) == _at(frame['wall_time']) and 0 not in api.current['targets']):
                api.current['targets'].append(0)
            api.current['orders'] = snapshot['orders']
            api.current['position'] = {key: position.get(key) for key in
                                       ('direction', 'quantity', 'entry_filled', 'owned', 'exit_target')}
    return {'rounds': api.outputs, 'identities': api.identities}


def original(packet, archive, loop):
    scheduler = OriginalScheduler(archive, packet['rounds'][0]['market'][0]['contract_id'], loop)
    try:
        rows, logs = replay_original(archive, packet['settings'], packet['rounds'], scheduler)
        return {'rounds': scheduler.api.outputs, 'identities': scheduler.api.identities,
                'strategy': rows, 'logs': logs}
    finally:
        scheduler.api.close()


def compare(reference, current):
    rows = []
    actual = {row['id']: row for row in current['rounds']}
    original_ids = {identity: alias for alias, identity in reference['identities'].items()}
    candidate_ids = {identity: alias for alias, identity in current['identities'].items()}
    dead = set()
    for expected in reference['rounds']:
        observed = actual.get(expected['id'])
        old = [packet | {'order_id': original_ids[packet['order_id']]} for packet in expected['outbound']]
        new = [] if observed is None else [
            packet | {'order_id': candidate_ids[packet['order_id']]} for packet in observed['outbound']]
        terminal_cancels = [packet for packet in old if packet['aid'] == 'cancel_order' and packet['order_id'] in dead]
        effective = [packet for packet in old if packet not in terminal_cancels]
        waits_match = (observed is not None and expected['wait_update'] == observed['wait_update']
                       and expected.get('wait_returned') == observed.get('wait_returned'))
        targets_match = (observed is not None
                         and list(dict.fromkeys(expected['targets'])) == observed['targets'])
        rows.append({'id': expected['id'], 'original': expected, 'candidate': observed, 'original_outbound': old,
                     'candidate_outbound': new, 'terminal_cancels': terminal_cancels,
                     'waits_match': waits_match, 'targets_match': targets_match,
                     'match': waits_match and targets_match and 'error' not in observed and effective == new})
        dead.update(expected.get('finished', ()))
    return rows


def verify_slow_partial(rows):
    """Check the one approved strategy deviation without accepting new ones."""
    if [row['id'] for row in rows] != [f'round-{i}' for i in range(1, 10)]:
        return ['Expected the nine ordered rounds of the slow partial-fill fixture']
    by_id = {row['id']: row for row in rows}
    errors = []
    differences = {row['id'] for row in rows if not row['match']}
    if differences != {'round-7', 'round-9'}:
        errors.append(f'Expected differences only in rounds 7 and 9, got {sorted(differences)}')
    seventh, ninth = by_id['round-7'], by_id['round-9']
    if (not seventh['waits_match'] or seventh['original']['targets'] != [3]
            or seventh['original_outbound']):
        errors.append('Original round 7 must request a target of 3 without an outbound order')
    expected_close = {'aid': 'insert_order', 'direction': 'SELL', 'offset': 'CLOSE', 'volume': 2,
                      'limit_price': 1060}
    original_ninth = [{key: value for key, value in packet.items() if key != 'order_id'}
                      for packet in ninth['original_outbound']]
    if (not ninth['waits_match'] or not ninth['targets_match'] or original_ninth != [expected_close]):
        errors.append('Original round 9 must emit one SELL CLOSE for 2 lots at 1060')
    for row, owned, calls in ((seventh, 3, [('insert_order', 'BUY', 'OPEN', 2)]), (ninth, 5, [])):
        candidate = row['candidate'] or {}
        expected_position = {'direction': 'LONG', 'quantity': 5, 'entry_filled': owned,
                             'owned': owned, 'exit_target': None}
        actions = [(packet['aid'], packet.get('direction'), packet.get('offset'), packet.get('volume'))
                   for packet in candidate.get('api_calls', ())]
        if ('error' in candidate or candidate.get('position') != expected_position
                or candidate.get('targets') != [] or candidate.get('outbound') != [] or actions != calls):
            errors.append(f'{row["id"]} must retain the 5-lot opening target and {owned} proved lots without a close')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--python314', type=Path)
    parser.add_argument('--candidate-api', type=Path)
    parser.add_argument('--verify-slow-partial', action='store_true')
    args = parser.parse_args()
    packet = json.loads(args.input.read_text(encoding='utf-8'))
    if args.python314 is None:
        loop = asyncio.new_event_loop()
        sys.addaudithook(_deny_external_effects)
        try:
            print(json.dumps(original(packet, args.archive, loop), ensure_ascii=True))
        finally:
            loop.close()
        return 0
    if args.candidate_api is None:
        parser.error('Supply the pinned OnlineTrader SDK api.py for comparison')
    source = subprocess.run([str(args.python314.resolve()), '-I', '-c',
        "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
        "runpy.run_module('qa.dsv_parity.scheduled_sdk_replay',run_name='__main__')",
        str(Path(__file__).resolve().parents[2]), '--archive', str(args.archive), '--input', str(args.input)],
        check=True, stdout=subprocess.PIPE, timeout=30)
    reference = json.loads(source.stdout)
    loop = asyncio.new_event_loop()
    sys.addaudithook(lambda event, details: _deny_external_effects(event, details)
                     if event != 'ctypes.dlopen' else None)
    try:
        with TemporaryDirectory(prefix='dsv-scheduled-compare-') as directory:
            current = candidate(packet, args.candidate_api, loop, Path(directory) / 'facts.db')
    finally:
        loop.close()
    rows = compare(reference, current)
    result = {'rounds': rows, 'reference_strategy': reference['strategy'], 'reference_logs': reference['logs']}
    if args.verify_slow_partial:
        errors = verify_slow_partial(rows)
        result['slow_partial_regression'] = {'passed': not errors, 'errors': errors}
        exit_code = int(bool(errors))
    else:
        exit_code = int(any(not row['match'] for row in rows))
    print(json.dumps(result))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
