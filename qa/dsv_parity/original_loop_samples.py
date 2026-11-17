"""Write credential-free inputs shared by the original and candidate DSV loops."""

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path

from online_trader.configuration.baseline import load_confirmed_baseline


_CONTRACT = 'DCE.p2701'
_DAY = '2026-11-13'


def _frame(sequence, price, days=16, *, owned=0, fill=None, alias=None, second=None):
    if second is None:
        second = sequence
    wall = f'{_DAY}T01:10:{second:02d}Z'
    market = {
        'contract_id': _CONTRACT, 'long_qty': owned, 'short_qty': 0,
        'opening_session': True, 'session_open': f'{_DAY}T01:00:00Z', 'session_close': f'{_DAY}T02:15:00Z',
        'available': '100000', 'margin_per_lot': '1000', 'multiplier': '10',
        'open_price_long': 1061 if owned else 0,
        'ticks': [{'native_time': wall, 'candle_id': 'bar-1', 'last': str(price),
                   'day_low': '1020', 'day_high': '1088', 'bid1': str(price), 'ask1': str(price),
                   'tick_size': '1', 'lower_limit': '900', 'upper_limit': '1200'}],
    }
    if days is not None:
        market['expiry_date'] = str(date.fromisoformat(_DAY) + timedelta(days=days + 1))
        market['days_to_expiry'] = str(days)
    facts = []
    if fill:
        child, side, offset, fill_price = fill
        common = {'child_order_id': child, 'native_order_id': f'{child}-order',
                  'contract_id': _CONTRACT, 'direction': side, 'native_offset': offset, 'offset': offset}
        facts = [common | {'kind': 'ORDER', 'status': 'FILLED', 'filled_qty': 5},
                 common | {'kind': 'TRADE', 'native_trade_id': f'{child}-fill', 'quantity': 5,
                           'price': str(fill_price)}]
    return {'id': f'round-{sequence}', 'sequence': sequence, 'session_id': 'day-1',
            'wall_time': wall, 'monotonic_ms': second * 1000,
            'market': [market], 'native': facts, 'send_aliases': [alias] if alias else []}


def samples():
    settings = load_confirmed_baseline()[0].to_settings()
    settings['symbols'] = [settings['symbols'][0] | {
        'open_offset': 40, 'stop_offset': 8, 'profit_offset': 50, 'order_volume': 5}]
    settings['trade'].update(no_chase_after_act=False, continuation_entry=False,
                             protect_80_enabled=True, protect_80_ratio=0.8, protect_keep_ratio=0.5,
                             trailing_activate_ratio=0, passive_entry_first=False, month_stop_days=0,
                             no_open_after_open=False, min_days_to_expiry=5)
    no_rollover = deepcopy(settings)
    no_rollover['trade']['rollover_stop_enabled'] = False
    cases = {
        'protection': (settings, [
            _frame(1, 1054), _frame(2, 1060, alias='open'),
            _frame(3, 1061, owned=5, fill=('open', 'BUY', 'OPEN', 1061)),
            _frame(4, 1100, owned=5), _frame(5, 1078, owned=5, alias='close'),
            _frame(6, 1077, fill=('close', 'SELL', 'CLOSE', 1076)), _frame(7, 1077, second=12)]),
        'expiry-blocked': (settings, [_frame(1, 1054, 5), _frame(2, 1060, 6), _frame(3, 1061, 6)]),
        'expiry-allowed': (settings, [_frame(1, 1054, 6), _frame(2, 1060, 5), _frame(3, 1061, 5)]),
        'expiry-late': (settings, [_frame(1, 1054, None), _frame(2, 1060, 5), _frame(3, 1061, None)]),
        'expiry-switch-off': (no_rollover, [_frame(1, 1054, 5), _frame(2, 1060, 5)]),
    }
    return {name: {'trading_day': _DAY, 'settings': config, 'rounds': frames}
            for name, (config, frames) in cases.items()}


def sdk_samples():
    packet = samples()['protection']
    rounds = packet['rounds']
    rounds[1]['sdk_targets'] = [5]
    rounds[4]['sdk_targets'] = [0, 0]
    rounds[5]['sdk_targets'] = [0]
    boundary = deepcopy(packet)
    near = _frame(7, 1090, second=10)
    near['wall_time'] = f'{_DAY}T01:10:10.999Z'
    near['market'][0]['ticks'][0]['native_time'] = near['wall_time']
    near['monotonic_ms'] = 10999
    boundary['rounds'][6:] = [near, _frame(8, 1090, second=11)]

    def update(status, filled, *, child='first', numbered=()):
        order = {'child_order_id': child, 'native_order_id': child + '-order'}
        return [order | {'kind': 'ORDER', 'status': status, 'filled_qty': filled}] + [
            order | {'kind': 'TRADE', 'native_trade_id': trade_id, 'quantity': quantity, 'price': str(price)}
            for trade_id, quantity, price in numbered]

    chase = [_frame(1, 1060, alias='first'), _frame(2, 1060, owned=2),
             _frame(3, 1060, owned=2), _frame(4, 1060, owned=3), _frame(5, 1060, owned=3, alias='replacement'),
             _frame(6, 1060, owned=5)]
    chase[0]['sdk_targets'] = [5]
    for row in chase[1:]:
        row['market'][0]['ticks'][0]['ask1'] = '1061'
    chase[1]['native'] = update('ALIVE', 2, numbered=[('first-a', 2, 1060)])
    chase[1]['native'][0]['exchange_order_id'] = ''
    chase[2]['native'] = update('ALIVE', 2)
    chase[3]['native'] = update('CANCELLED', 3)
    chase[4]['native'] = update('CANCELLED', 3, numbered=[('first-b', 1, 1060)])
    chase[5]['native'] = update('FILLED', 2, child='replacement', numbered=[('second', 2, 1061)])
    return {'sdk-protection': packet, 'sdk-chase': packet | {'rounds': chase},
            'sdk-sync-boundary': boundary}


def scheduled_samples():
    examples = sdk_samples()
    protect = examples['sdk-protection']
    protect['rounds'][2]['wire_aliases'] = ['open']
    protect['rounds'][5]['wire_aliases'] = ['close']
    previous = examples['sdk-chase']['rounds']
    rows = [_frame(1, 1054), _frame(2, 1060), _frame(3, 1060), _frame(4, 1060, owned=2),
            _frame(5, 1060, owned=3), _frame(6, 1060, owned=3), _frame(7, 1060, owned=5),
            _frame(8, 1060, owned=5)]
    for row in rows[2:]:
        row['market'][0]['ticks'][0]['ask1'] = '1061'
    for target_index, source_index in ((3, 1), (4, 3), (5, 4), (6, 5)):
        rows[target_index]['native'] = deepcopy(previous[source_index]['native'])
    rows[3]['native'][0]['exchange_order_id'] = 'ack-first'
    rows[2]['wire_aliases'] = ['first']
    rows[6]['wire_aliases'] = ['replacement']
    chase = examples['sdk-chase'] | {'rounds': rows}

    timeout = deepcopy(chase)
    empty = _frame(3, 1060, second=2)
    empty['wall_time'] = f'{_DAY}T01:10:02.500Z'
    empty['monotonic_ms'] = 2500
    empty['no_update'] = True
    empty['wire_aliases'] = ['first']
    timeout['rounds'][2].pop('wire_aliases')
    timeout['rounds'].insert(2, empty)
    for index, row in enumerate(timeout['rounds'], 1):
        row['sequence'], row['id'] = index, f'round-{index}'
        row['market'][0]['ticks'][0]['native_time'] = row['wall_time']
    slow_partial = deepcopy(timeout)
    for index, row in enumerate(slow_partial['rounds'], 1):
        row['wall_time'] = f'{_DAY}T01:10:{index:02d}Z'
        row['monotonic_ms'] = index * 1000
        row['market'][0]['ticks'][0]['native_time'] = row['wall_time']
    final_close = deepcopy(protect)
    final_close['settings']['trade'].update(close_before_seconds=30, stale_force_close_seconds=0,
                                            no_open_before_close_minutes=0, no_open_after_open=False)
    rounds = final_close['rounds'][:3]
    rounds.extend(_frame(index, 1060, owned=5, second=0) for index in range(4, 81))
    initial = datetime.fromisoformat(f'{_DAY}T06:59:20Z')
    for index, round_input in enumerate(rounds):
        moment = (initial + timedelta(milliseconds=500 * index)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        round_input.update(wall_time=moment, monotonic_ms=500 * index)
        market, = round_input['market']
        market.update(session_open=f'{_DAY}T05:30:00Z', session_close=f'{_DAY}T07:00:00Z',
                      opening_session=False, force_close_at_end=True)
        market['ticks'][0].update(native_time=moment, candle_id=f'{_DAY}T06:59:00Z')
    rounds[21]['wire_aliases'] = ['first-close']
    final_close['rounds'] = rounds
    late_close = deepcopy(final_close)
    rounds = late_close['rounds']
    for number in (22, 23, 25, 29, 30, 32, 36, 40, 44, 48, 50, 54, 58, 60, 62, 64, 65, 67, 72, 74, 76, 78, 80):
        rounds[number - 1]['no_update'] = True
    rounds[66].pop('no_update')
    rounds[66]['native'] = [{'kind': 'ORDER', 'child_order_id': 'first-close', 'native_order_id': 'staged-close',
                             'status': 'CANCELLED', 'filled_qty': 0}]
    rounds[67]['wire_aliases'] = ['second-close']
    return {'pipeline-chase': chase, 'pipeline-protect': protect, 'pipeline-timeout': timeout,
            'late-partial-ownership': slow_partial, 'pipeline-final-close': final_close, 'pipeline-close-late-ack': late_close}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--sdk', action='store_true')
    modes.add_argument('--scheduled', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    packets = scheduled_samples() if args.scheduled else sdk_samples() if args.sdk else samples()
    for name, packet in packets.items():
        (args.output / f'{name}.json').write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
