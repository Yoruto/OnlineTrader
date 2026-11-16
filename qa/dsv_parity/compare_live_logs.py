"""Compare DSV text notices with one interval of OnlineTrader's read-only fact journal."""

from argparse import ArgumentParser
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
import sys


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
    candidate_quotes, candidate_activations = defaultdict(set), defaultdict(list)
    consumption_issues, candles = [], {}
    previous_session = None
    audit_start = start.replace(second=0, microsecond=0)
    reasons = defaultdict(Counter)
    rounds = effects = 0
    uri = (run_dir / 'candidate.db').resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
        for event_seq, raw in read_rounds(db):
            event = json.loads(raw)
            at = datetime.fromisoformat(event['input']['wall_time']).astimezone(BEIJING)
            if at.date() != start.date() or at >= end:
                continue
            in_interval = at >= start
            session = event['input'].get('session_id')
            if session != previous_session or event['input'].get('synchronized') is False:
                candles.clear()
            previous_session = session
            for market in event['input']['market']:
                if market['ticks']:
                    candles[market['contract_id']] = market['ticks'][-1].get('candle_id')
            for decision in event['receipt']['decisions']:
                contract, reason = decision.get('contract_id'), decision['reason']
                if contract is None:
                    continue
                if in_interval:
                    reasons[contract][reason] += 1
                if reason == 'NO_CHASE_AFTER_ACT' and at >= audit_start:
                    minute = _candidate_minute(decision)
                    issue = {'event_seq': event_seq, 'contract_id': contract, 'consume': decision.get('consume')}
                    if minute is None or minute.date() != start.date():
                        consumption_issues.append(issue | {'code': 'INVALID_CONSUME_ID'})
                        continue
                    key = minute.strftime('%H:%M'), contract
                    valid = True
                    if candles.get(contract) is None:
                        consumption_issues.append(issue | {'code': 'CANDLE_EVIDENCE_MISSING'})
                        valid = False
                    elif decision['consume'] != candles[contract]:
                        consumption_issues.append(issue | {'code': 'CANDLE_ID_MISMATCH'})
                        valid = False
                    try:
                        if any(type(decision.get(field)) is not str for field in ('trigger_price', 'activation_price')):
                            raise ValueError('Activation prices must be source text')
                        detail = (decision['direction'], Decimal(decision['trigger_price']),
                                  Decimal(decision['activation_price']))
                        if detail[0] not in ('LONG', 'SHORT') or any(not n.is_finite() for n in detail[1:]):
                            raise ValueError('Invalid activation detail')
                    except (KeyError, TypeError, ValueError, InvalidOperation):
                        consumption_issues.append(issue | {'code': 'INVALID_ACTIVATION_DETAIL'})
                        valid, detail = False, None
                    if candidate_activations[key]:
                        consumption_issues.append(issue | {'code': 'DUPLICATE_CONSUMPTION'})
                    candidate_activations[key].append({'event_seq': event_seq, 'valid': valid, 'detail': detail})
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
        expected = row['direction'], Decimal(row['trigger']), Decimal(row['activation'])
        if not observed:
            status = 'MISSING_REASON'
        elif len(observed) != 1:
            status = 'DUPLICATE_CONSUMPTION'
        elif not observed[0]['valid']:
            status = 'INVALID_CANDIDATE'
        else:
            status = 'MATCH' if observed[0]['detail'] == expected else 'DIFFERENT_DETAIL'
        row['status'] = status
    mismatches = any(row['status'] != 'MATCH' for row in reference_activations)
    verdict = ('MISMATCH' if mismatches or consumption_issues else
               'MATCH_AT_LOG_PRECISION' if reference_activations else 'INSUFFICIENT_EVIDENCE')
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
    return {'verdict': verdict, 'consumption_issues': consumption_issues,
            'comparison_scope': 'Activation notices at logged numeric precision and candidate receipt consumption invariants; not full trading parity',
            'candidate_persisted_minute_state_verified': False,
            'start': start.isoformat(), 'end': end.isoformat(), 'dsv_message_tags': tags,
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
    parser.add_argument('--report-only', action='store_true', help='Return zero for report generation only; verdict is unchanged')
    args = parser.parse_args(argv)
    if args.start.utcoffset() is None or args.end.utcoffset() is None:
        parser.error('The interval requires explicit UTC offsets')
    if args.end <= args.start or args.start.astimezone(BEIJING).date() != args.end.astimezone(BEIJING).date():
        parser.error('Provide an increasing interval within one Beijing civil day')
    if args.end > datetime.now(timezone.utc):
        parser.error('The observation interval has not finished')
    result = compare(args.run_dir, args.start, args.end)
    report = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        with args.output.open('x', encoding='utf-8') as handle:
            handle.write(report + '\n')
    else:
        print(report)
    return 0 if args.report_only or result['verdict'] == 'MATCH_AT_LOG_PRECISION' else (
        2 if result['verdict'] == 'INSUFFICIENT_EVIDENCE' else 1)


if __name__ == '__main__':
    sys.exit(main())
