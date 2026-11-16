"""Compare frozen DSV target decisions and OnlineTrader effects on a shared round packet."""

import argparse
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.runner import DsvSession
from qa.recording_port import RecordingPort
from qa.dsv_parity.entry_evidence import compare_entry_evidence


class _ReplayPort(RecordingPort):
    def __init__(self):
        super().__init__()
        self.calls = []

    def send(self, request):
        self.calls.append(request.copy())
        return super().send(request)

    def cancel(self, request):
        self.calls.append(request.copy())
        return super().cancel(request)


def candidate_rounds(packet, db):
    profile = DsvProfile.from_settings(packet['settings'])
    children = {}
    port = _ReplayPort()
    with DsvSession(db, account_key=f'DEV:loop-{uuid4().hex}', provider='OFFLINE',
                    trading_day=packet['trading_day'], profile=profile, port=port) as session:
        session.command({'id': 'start', 'action': 'START'})
        for step in packet['rounds']:
            frame = deepcopy(step)
            aliases = frame.pop('send_aliases', ())
            frame.pop('sdk_targets', None)
            for fact in frame['native']:
                fact['child_order_id'] = children[fact['child_order_id']]
            port.calls.clear()
            receipt = session.apply_round(frame)
            sends = [effect for effect in receipt['effects'] if effect['kind'] == 'SEND']
            for alias, effect in zip(aliases, sends):
                children[alias] = effect['child_order_id']
            snapshot = session.snapshot()
            yield {'id': frame['id'], 'decisions': receipt['decisions'], 'effects': receipt['effects'],
                   'calls': list(port.calls), 'orders': snapshot['orders'], 'symbols': snapshot['symbols']}


def compare(packet, original, candidate):
    rows = []
    for frame, reference, current in zip(packet['rounds'], original, candidate, strict=True):
        if frame['id'] != reference['id'] or reference['id'] != current['id']:
            raise ValueError('Round identities must agree before comparing decisions')
        market = {item['contract_id']: item for item in frame['market']}
        # The source submits absolute target positions. For this packet each
        # target is a single unambiguous increase or a reduction of owned lots.
        positions = {key: value['long_qty'] - value['short_qty'] for key, value in market.items()}
        expected = []
        position_after_target = dict(positions)
        for item in reference['targets']:
            contract, volume = item['contract_id'], item['volume']
            if volume != position_after_target[contract]:
                expected.append((contract, volume))
                position_after_target[contract] = volume
        actual = []
        for effect in current['effects']:
            if effect['kind'] == 'SEND':
                contract = effect['contract_id']
                positions[contract] += effect['quantity'] * (1 if effect['direction'] == 'BUY' else -1)
                actual.append((contract, positions[contract]))
            else:
                actual.append((effect['contract_id'], 'CANCEL'))
        source_blocked = sorted(key for key, row in reference['state'].items()
                                if (row.get('no_open_reason') or '').startswith('距到期'))
        cutoff = packet['settings']['trade']['min_days_to_expiry']
        actual_blocked = sorted(key for key, row in current['symbols'].items()
                                if row.get('expiry_days_checked', cutoff + 1) <= cutoff)
        reference_loss = {key: Decimal(str(row['net_loss_points'])) for key, row in reference['state'].items()}
        current_loss = {key: Decimal(row['net_loss']) for key, row in current['symbols'].items()}
        differences = compare_entry_evidence(reference, current)
        if expected != actual:
            differences.append('targets')
        if source_blocked != actual_blocked:
            differences.append('expiry_block')
        if reference_loss != current_loss:
            differences.append('nominal_net_loss')
        if any(bool(row['day_stopped']) != current['symbols'][key]['day_stop']
               for key, row in reference['state'].items()):
            differences.append('day_stop')
        for key, row in reference['state'].items():
            position = current['symbols'][key]['position'] or {}
            if bool(row['close_booked']) != ('close_request_at' in position):
                differences.append('strategy_close_sync')
        rows.append({'id': frame['id'], 'original_targets': expected, 'candidate_targets': actual,
                     'original_expiry_block': source_blocked, 'candidate_expiry_block': actual_blocked,
                     'original_net_loss': reference_loss, 'candidate_net_loss': current_loss,
                     'differences': differences, 'original': reference, 'candidate': current})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--python314', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    packet = json.loads(args.input.read_text(encoding='utf-8'))
    reference = subprocess.run(
        [str(args.python314.resolve()), '-I', '-c',
         "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
         "runpy.run_module('qa.dsv_parity.original_loop',run_name='__main__')",
         str(Path(__file__).resolve().parents[2]), '--archive', str(args.archive), '--input', str(args.input)],
        stdout=subprocess.PIPE, check=True, timeout=30)
    original = json.loads(reference.stdout)
    with TemporaryDirectory(prefix='dsv-candidate-replay-') as directory:
        rows = compare(packet, original['rounds'], candidate_rounds(packet, Path(directory) / 'facts.db'))
    print(json.dumps({'rounds': rows, 'original_logs': original['logs']}, ensure_ascii=True, default=str))
    return int(any(row['differences'] for row in rows))


if __name__ == '__main__':
    raise SystemExit(main())
