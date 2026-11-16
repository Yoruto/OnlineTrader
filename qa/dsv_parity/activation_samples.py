"""Credential-free rejection samples for the frozen source and candidate loops.

The first case uses the reported morning prices/offsets. Account, session and
order-free environment are synthetic; this is not the full morning recording.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from online_trader.configuration.baseline import load_confirmed_baseline

CODE = 'DCE.p2701'
DAY = '2026-11-16'


def samples():
    settings = load_confirmed_baseline()[0].to_settings()
    settings['symbols'] = [settings['symbols'][0] | {
        'code': CODE, 'open_offset': 32, 'stop_offset': 16, 'profit_offset': 73, 'order_volume': 5}]
    settings['trade'].update(no_chase_after_act=True, continuation_entry=False, no_open_after_open=False,
                             protect_80_enabled=True, protect_80_ratio=0.8, month_stop_days=0)
    def frame(seq, at, minute, last, high=9073, low=8949):
        wall = DAY + 'T' + at + 'Z'
        tick = {'native_time': wall, 'candle_id': DAY + 'T' + minute + ':00Z', 'last': str(last),
                'day_high': str(high), 'day_low': str(low), 'bid1': str(last), 'ask1': str(last),
                'tick_size': '1', 'lower_limit': '8000', 'upper_limit': '10000'}
        market = {'contract_id': CODE, 'long_qty': 0, 'short_qty': 0, 'opening_session': False,
                  'session_open': DAY + 'T02:30:00Z', 'session_close': DAY + 'T03:30:00Z',
                  'available': '100000', 'margin_per_lot': '1000', 'multiplier': '10', 'ticks': [tick]}
        return {'id': 'activation-'+str(seq), 'sequence': seq, 'session_id': 'day-1', 'wall_time': wall,
                'monotonic_ms': seq*1000, 'market': [market], 'native': [], 'send_aliases': []}
    morning = {'trading_day': DAY, 'settings': settings, 'rounds': [
        frame(1, '02:55:09', '02:55', 9003), frame(2, '02:55:25', '02:55', 8970),
        frame(3, '02:56:01', '02:56', 8970), frame(4, '02:57:01', '02:57', 8970)]}
    off = deepcopy(morning)
    off['settings']['trade']['no_chase_after_act'] = False
    long_equal, short_equal = deepcopy(morning), deepcopy(morning)
    for packet in (long_equal, short_equal): packet['settings']['symbols'][0]['profit_offset'] = 70
    long_equal['rounds'] = [frame(1, '02:55:09', '02:55', 9003, high=9037)]
    short_equal['rounds'] = [frame(1, '02:55:09', '02:55', 9003, low=8985)]
    return {'morning-activation': morning, 'activation-disabled': off,
            'long-activation-equality': long_equal, 'short-activation-equality': short_equal}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, packet in samples().items():
        (args.output / (name+'.json')).write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__': main()
