"""Drive the frozen TargetPosTask and its child tasks with local SDK updates."""

import argparse
import ast
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import hashlib
from inspect import isfunction
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import MethodType, SimpleNamespace
from zipfile import ZipFile

from qa.dsv_parity.sdk_reference_probe import MEMBER, VERSION_MEMBER, method
from qa.dsv_parity.original_loop import _deny_external_effects
from qa.dsv_parity.verify_reference import MANIFEST, verify


_CLOSED = object()


class Channel:
    def __init__(self, _api, *, last_only=False):
        self.queue = asyncio.Queue()
        self.last_only = last_only

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.recv()
        if value is _CLOSED:
            raise StopAsyncIteration
        return value

    async def recv(self):
        return await self.queue.get()

    def recv_latest(self, value):
        while not self.queue.empty():
            value = self.queue.get_nowait()
        return value

    def send_nowait(self, value):
        if self.last_only:
            self.recv_latest(None)
        self.queue.put_nowait(value)

    async def send(self, value):
        self.send_nowait(value)

    async def close(self):
        self.send_nowait(_CLOSED)


class Entity(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class OfflineApi:
    def __init__(self, symbol):
        self.symbol = symbol
        self._backtest = None
        self.quote = Entity(open_min_market_order_volume=1, open_min_limit_order_volume=1, datetime='')
        self.orders = {}
        self.position = SimpleNamespace(pos=0, pos_long=0, pos_short=0, pos_long_today=0, pos_short_today=0,
                                        pos_long_his=0, pos_short_his=0, orders=self.orders)
        self.notifiers = []
        self.tasks = []
        self.trace = []
        self.child_count = 0

    def new_id(self, prefix):
        self.child_count += 1
        return f'{prefix}_{self.child_count}'

    def create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def get_quote(self, symbol):
        if symbol != self.symbol:
            raise ValueError(f'Unexpected contract {symbol}')
        return self.quote

    def insert_order(self, symbol, direction, offset, volume, price, *, order_id, account):
        order = Entity(order_id=order_id, direction=direction, offset=offset, status='ALIVE', is_dead=False,
                       exchange_order_id='', volume_orign=volume, volume_left=volume, trade_records={},
                       last_msg='')
        self.orders[order_id] = order
        self.trace.append(('SEND', order_id, direction, offset, volume, price))
        return order

    def get_order(self, order_id, *, account):
        return self.orders[order_id]

    def cancel_order(self, order_id, *, account):
        self.trace.append(('CANCEL', order_id))

    @asynccontextmanager
    async def register_update_notify(self, *, chan=None):
        channel = chan if chan is not None else Channel(self)
        self.notifiers.append(channel)
        try:
            yield channel
        finally:
            self.notifiers.remove(channel)

    def apply(self, frame, identities):
        market = frame['market'][0]
        self.position.pos_long = self.position.pos_long_today = market['long_qty']
        self.position.pos_short = self.position.pos_short_today = market['short_qty']
        self.position.pos = market['long_qty'] - market['short_qty']
        if market['ticks']:
            tick = market['ticks'][-1]
            self.quote.update(ask_price1=float(tick['ask1']), bid_price1=float(tick['bid1']),
                              last_price=float(tick['last']), datetime=tick['native_time'])
        for fact in frame['native']:
            order = self.orders[identities[fact['child_order_id']]]
            if fact['kind'] == 'ORDER':
                dead = fact['status'] != 'ALIVE'
                order.update(status='FINISHED' if dead else 'ALIVE', is_dead=dead,
                             exchange_order_id=fact.get('exchange_order_id', 'fixture'),
                             volume_left=order.volume_orign - fact['filled_qty'])
            else:
                order.trade_records[fact['native_trade_id']] = Entity(
                    volume=fact['quantity'], price=float(fact['price']))
        for channel in self.notifiers:
            channel.send_nowait(None)


def _frozen_source(archive):
    expected = json.loads(MANIFEST.read_text(encoding='utf-8'))['dsv_reference']
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected['archive_sha256']:
            raise ValueError('Frozen archive SHA-256 mismatch')
        with ZipFile(stream) as bundle:
            version = bundle.read(VERSION_MEMBER)
            source = bundle.read(MEMBER)
    verify(VERSION_MEMBER, version, expected['sdk_version_file_sha256'])
    verify(MEMBER, source, expected['sdk_target_task_sha256'])
    return source


def bind_target(api, source, price='ACTIVE'):
    tree = ast.parse(b'from __future__ import annotations\n' + source)
    namespace = {'asyncio': asyncio, 'gather': asyncio.gather, 'isfunction': isfunction, 'TqChan': Channel,
                 'utils': SimpleNamespace(_generate_uuid=api.new_id), 'TqBacktest': type('Backtest', (), {}),
                 '_is_in_trading_time': lambda *_args: True, '_check_volume_limit': lambda low, high: (low, high)}
    namespace.update({f'_check_{key}': lambda value: value for key in ('volume', 'direction', 'offset', 'price')})
    classes = {'InsertOrderUntilAllTradedTask', 'InsertOrderTask'}
    nodes = [tree.body[0], *(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in classes)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<frozen-sdk-3.10.2>', 'exec'), namespace)
    target = SimpleNamespace(_api=api, _account='offline', _symbol=api.symbol, _exchange=api.symbol.split('.')[0],
                             _pos=api.position, _pos_chan=Channel(api, last_only=True), _offset_priority='今昨,开',
                             _price=price, _local_time_record=0, _support_open_min_volume=True, _min_volume=None,
                             _max_volume=None, _trade_chan=None, _trade_objs_chan=None, _target_volume_chan=None)
    for name in ('set_target_volume', '_target_pos_task', '_get_order'):
        setattr(target, name, MethodType(method(tree, 'TargetPosTask', name, namespace), target))
    target._task = api.create_task(target._target_pos_task())
    return target


async def drain_tasks(api):
    for _ in range(32):
        await asyncio.sleep(0)
    for task in api.tasks:
        if task.done() and not task.cancelled():
            task.result()


async def stop_tasks(api):
    for task in api.tasks:
        task.cancel()
    await asyncio.gather(*api.tasks, return_exceptions=True)


async def replay_sdk(source, frames):
    api = OfflineApi(frames[0]['market'][0]['contract_id'])
    target = bind_target(api, source)
    rows, identities = [], {}
    try:
        for frame in frames:
            api.trace.clear()
            api.apply(frame, identities)
            for volume in frame.get('sdk_targets', ()):
                target.set_target_volume(volume)
            await drain_tasks(api)
            sends = [event for event in api.trace if event[0] == 'SEND']
            for alias, event in zip(frame.get('send_aliases', ()), sends):
                identities[alias] = event[1]
            rows.append({'id': frame['id'], 'effects': list(api.trace),
                         'identities': dict(identities),
                         'orders': {alias: {'state': api.orders[key].status,
                                            'remaining': api.orders[key].volume_left,
                                            'trade_qty': sum(t.volume for t in api.orders[key].trade_records.values())}
                                    for alias, key in identities.items()}})
        return rows
    finally:
        await stop_tasks(api)


def compare_sdk(packet, reference, candidate):
    rows = []
    candidate_ids = {}
    for frame, original, current in zip(packet['rounds'], reference, candidate, strict=True):
        if not frame['id'] == original['id'] == current['id']:
            raise ValueError('SDK and candidate round identities differ')
        original_ids = {value: key for key, value in original['identities'].items()}
        sends = [call for call in current['calls'] if call['kind'] == 'SEND']
        for alias, call in zip(frame.get('send_aliases', ()), sends, strict=True):
            candidate_ids[call['child_order_id']] = alias
        raw, expected, actual = [], [], []
        for kind, identity, *parameters in original['effects']:
            alias = original_ids[identity]
            action = [kind, alias, *parameters]
            raw.append(action)
            if kind == 'SEND':
                action = [kind, alias, *parameters[:-1], Decimal(str(parameters[-1]))]
            # The old price watcher can enqueue a cancel after the terminal
            # update has already closed that order. Record the raw call.
            if kind != 'CANCEL' or original['orders'][alias]['state'] != 'FINISHED':
                expected.append(action)
        for call in current['calls']:
            alias = candidate_ids[call['child_order_id']]
            if call['kind'] == 'SEND':
                actual.append(['SEND', alias, call['direction'], call['native_offset'], call['quantity'],
                               Decimal(call['limit_price'])])
            else:
                actual.append(['CANCEL', alias])
        actual_orders = {}
        for identity, alias in candidate_ids.items():
            source = current['orders'][identity]
            actual_orders[alias] = {
                'state': 'FINISHED' if source['terminal_confirmed'] else 'ALIVE',
                'remaining': source['original_qty'] - (source['reported_fill_qty'] or 0),
                'trade_qty': source['filled_qty'],
            }
        rows.append({'id': frame['id'], 'original_raw_calls': raw, 'original_effective_calls': expected,
                     'candidate_calls': actual, 'original_orders': original['orders'],
                     'candidate_orders': actual_orders,
                     'match': expected == actual and original['orders'] == actual_orders})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--compare', action='store_true')
    args = parser.parse_args()
    packet = json.loads(args.input.read_text(encoding='utf-8'))
    source = _frozen_source(args.archive)
    loop = asyncio.new_event_loop()
    running_reference = True

    def audit(event, details):
        if running_reference or event != 'ctypes.dlopen':
            _deny_external_effects(event, details)

    sys.addaudithook(audit)
    try:
        reference = loop.run_until_complete(replay_sdk(source, packet['rounds']))
    finally:
        loop.close()
        running_reference = False
    if not args.compare:
        print(json.dumps(reference))
        return 0
    from qa.dsv_parity.compare_original_loop import candidate_rounds
    with TemporaryDirectory(prefix='dsv-sdk-compare-') as directory:
        rows = compare_sdk(packet, reference, candidate_rounds(packet, Path(directory) / 'facts.db'))
    print(json.dumps(rows, default=str))
    return int(any(not row['match'] for row in rows))


if __name__ == '__main__':
    raise SystemExit(main())
