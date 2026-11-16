"""Compare DSV text notices with one interval of OnlineTrader's read-only fact journal."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3


BEIJING = timezone(timedelta(hours=8))
_LINE = re.compile(r'^\[(\d\d:\d\d:\d\d)\]\s*\[([^\]]+)\]')
_QUOTE = re.compile(r'\(([A-Z]+\.[A-Za-z0-9]+)\) 当日 高([0-9.]+)/低([0-9.]+).*?现价 ([0-9.]+)')
_ACTIVATION = re.compile(r'\(([A-Z]+\.[A-Za-z0-9]+)\) 当日行情已跑过([多空])头激活位'
                         r'（触发 ([0-9.]+).*? = ([0-9.]+)）')
_NATIVE_MINUTE = re.compile(r'\d{8}T\d{4}')


def read_rounds(db):
    """Copy bounded batches through a fixed watermark before parsing any records."""
    with closing(db.execute('SELECT COALESCE(MAX(seq), 0) FROM events')) as cursor:
        end, = cursor.fetchone()
    after = 0
    while after < end:
        with closing(db.execute(
                "SELECT seq,payload FROM events WHERE seq>? AND seq<=? AND kind='ROUND' ORDER BY seq LIMIT 32",
                (after, end))) as cursor:
            batch = cursor.fetchall()
        if not batch:
            break
        after = batch[-1][0]
        # A suspended consumer holds no SQLite read statement.
        yield from batch


def _reference(path, start, end):
    quotes, activations, tags = defaultdict(set), [], Counter()
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        header = _LINE.match(line)
        if not header:
            continue
        second, tag = header.groups()
        at = datetime.fromisoformat(f'{start.date()}T{second}+08:00')
        if not start <= at < end:
            continue
        tags[tag] += 1
        if row := _QUOTE.search(line):
            contract, *prices = row.groups()
            quotes[second, contract].add(tuple(map(Decimal, prices)))
        if tag == '跳过' and (row := _ACTIVATION.search(line)):
            contract, side, trigger, level = row.groups()
            activations.append({'time': second, 'contract': contract, 'direction': 'LONG' if side == '多' else 'SHORT',
                                'trigger': trigger, 'activation': level})
    return quotes, activations, tags


def _candidate_minute(decision):
    consumed = decision.get('consume')
    if not isinstance(consumed, str):
        return None
    contract, _, stamp = consumed.rpartition(':')
    if contract != decision['contract_id'] or not _NATIVE_MINUTE.fullmatch(stamp):
        return None
    try:
        native_at = datetime.strptime(stamp, '%Y%m%dT%H%M').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return native_at.astimezone(BEIJING)


def compare(run_dir, start, end):
    start, end = start.astimezone(BEIJING), end.astimezone(BEIJING)
    log_path = run_dir / 'original-dsv' / 'logs' / f'trade_{start.date()}.log'
    quotes, reference_activations, tags = _reference(log_path, start, end)
    expected_minutes = {(row['time'][:5], row['contract']) for row in reference_activations}
    candidate_quotes, candidate_activations = defaultdict(set), defaultdict(set)
    reasons = defaultdict(Counter)
    rounds = effects = 0
    uri = (run_dir / 'candidate.db').resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
        for _, raw in read_rounds(db):
            event = json.loads(raw)
            at = datetime.fromisoformat(event['input']['wall_time']).astimezone(BEIJING)
            if at.date() != start.date() or at >= end:
                continue
            in_interval = at >= start
            for decision in event['receipt']['decisions']:
                contract, reason = decision.get('contract_id'), decision['reason']
                if contract is None:
                    continue
                if in_interval:
                    reasons[contract][reason] += 1
                if reason == 'NO_CHASE_AFTER_ACT':
                    minute = _candidate_minute(decision)
                    if minute is None or minute.date() != start.date():
                        continue
                    key = minute.strftime('%H:%M'), contract
                    trigger, level = (decision.get(field) for field in ('trigger_price', 'activation_price'))
                    if key in expected_minutes and trigger is not None and level is not None:
                        candidate_activations[key].add((decision.get('direction'), f'{float(trigger):.1f}', f'{float(level):.1f}'))
            if not in_interval:
                continue
            rounds += 1
            effects += len(event['receipt']['effects'])
            for market in event['input']['market']:
                key = at.strftime('%H:%M:%S'), market['contract_id']
                if key not in quotes or not market['ticks']:
                    continue
                tick = market['ticks'][-1]
                prices = [tick.get(field) for field in ('day_high', 'day_low', 'last')]
                if all(price is not None and price != 'NaN' for price in prices):
                    candidate_quotes[key].add(tuple(map(Decimal, prices)))
        native = {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in ('native_orders', 'native_trades')}

    for row in reference_activations:
        observed = candidate_activations.get((row['time'][:5], row['contract']), ())
        expected = row['direction'], row['trigger'], row['activation']
        row['status'] = 'MATCH' if expected in observed else 'DIFFERENT_DETAIL' if observed else 'MISSING_REASON'
    snapshots = {'dsv': len(quotes), 'same_second': len(candidate_quotes), 'same_second_high_low': 0,
                 'same_second_all_three': 0}
    for key, candidate in candidate_quotes.items():
        snapshots['same_second_all_three'] += bool(quotes[key] & candidate)
        snapshots['same_second_high_low'] += bool({row[:2] for row in quotes[key]} & {row[:2] for row in candidate})
    runtime = run_dir / 'candidate-run'
    status_path = runtime / 'status.json'
    diagnostic_path = runtime / 'runtime-diagnostics.jsonl'
    status = json.loads(status_path.read_bytes()) if status_path.is_file() else {}
    diagnostics = 0
    if diagnostic_path.is_file():
        with diagnostic_path.open(encoding='utf-8') as stream:
            diagnostics = sum(bool(line.strip()) for line in stream)
    return {'start': start.isoformat(), 'end': end.isoformat(), 'dsv_message_tags': tags,
            'quote_samples': snapshots, 'activation_matches': reference_activations,
            'candidate_rounds': rounds, 'candidate_decisions': reasons, 'candidate_effects_in_interval': effects,
            'candidate_native_counts_at_read': native, 'runtime_diagnostics_at_read': diagnostics,
            'runtime_status_at_read': {key: status.get(key) for key in ('phase', 'entry_authority', 'error_code')}}


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('start', type=datetime.fromisoformat)
    parser.add_argument('end', type=datetime.fromisoformat)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.start.utcoffset() is None or args.end.utcoffset() is None:
        parser.error('The interval requires explicit UTC offsets')
    if args.end <= args.start or args.start.astimezone(BEIJING).date() != args.end.astimezone(BEIJING).date():
        parser.error('Provide an increasing interval within one Beijing civil day')
    if args.end > datetime.now(timezone.utc):
        parser.error('The observation interval has not finished')
    report = json.dumps(compare(args.run_dir, args.start, args.end), ensure_ascii=False, indent=2)
    if args.output is not None:
        with args.output.open('x', encoding='utf-8') as handle:
            handle.write(report + '\n')
    else:
        print(report)


if __name__ == '__main__':
    main()
