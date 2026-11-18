"""Read an F5 timing archive and score its declared measurement windows."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from online_trader.runtime.market_latency import p99_budget_result
from online_trader.scheduling.clock import ClockSample, delta_ns

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_M1_LIMIT_NS = 1_000_000_000


def _records(directory, name, additional, problems):
    file = directory / name
    paths = tuple(additional) + (file.with_suffix('.previous.jsonl'), file)
    for path in dict.fromkeys(paths):
        if not path.is_file() and path not in additional:
            continue
        with path.open(encoding='utf-8') as stream:
            for number, line in enumerate(stream, start=1):
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    problems.append({'file': path.name, 'line': number})
                    continue
                if isinstance(row, dict):
                    yield row
                else:
                    problems.append({'file': path.name, 'line': number})


def _distinct_batches(records, schema, coverage):
    """Archives may overlap a previous rotation. Count identical batches once."""
    seen = {}
    for row in records:
        if row.get('schema_version') != schema:
            continue
        boot = row['kernel_boot_id']
        number = int(row['batch_number'])
        if number < 1:
            raise ValueError('Expected a positive batch number')
        numbers = coverage.setdefault(boot, set())
        key = boot, number
        payload = sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode('utf-8')).digest()
        if number in numbers:
            if seen[key] != payload:
                raise ValueError('Diagnostic batch identity changed content')
            continue
        numbers.add(number)
        seen[key] = payload
        yield row


def _index(row, boot, window):
    return (boot, row['contract_id'], str(row['source_epoch']),
            row['trading_day'], row['session_id'], window)


def _proof_index(path):
    if path is None:
        return None, {}, {}
    with path.open(encoding='utf-8') as stream:
        proof = json.load(stream)
    if proof.get('schema_version') != 'F5_LATENCY_TIME_PROOF/1':
        raise ValueError('Unsupported time proof format')
    clocks, sources = defaultdict(list), defaultdict(list)
    for row in proof['application']:
        clocks[row['clock_epoch']].append(_proof_span(row, 'from_monotonic_ns', 'until_monotonic_ns'))
    for row in proof['sources']:
        if row['meaning'] != 'SDK_NATIVE_TICK_EVENT':
            continue
        key = row['contract_id'], str(row['source_epoch'])
        sources[key].append(_proof_span(row, 'from_source_ns', 'until_source_ns'))
    return proof['kernel_boot_id'], clocks, sources


def _proof_span(row, start, end):
    first, last, error = int(row[start]), int(row[end]), int(row['max_error_ns'])
    evidence = row.get('evidence')
    if not 0 <= first <= last or error < 0 or not isinstance(evidence, str) or not evidence:
        raise ValueError('Time proof requires an ordered interval, error bound and evidence reference')
    return first, last, error


def _error_at(spans, first, last):
    proven = [error for start, end, error in spans if start <= first <= last <= end]
    return max(proven) if proven else None


def _m1_interval(event, clocks, sources):
    paired = event['application_clock']
    if not paired or 'sdk_first_seen_monotonic_ns' not in event:
        return None
    sample = ClockSample.from_paired_record(paired)
    start = int(event['strategy_start_monotonic_ns'])
    captured = sample.monotonic
    first_seen = int(event['sdk_first_seen_monotonic_ns'])
    source = int(event['native_time_ns'])
    if not 0 <= first_seen <= start <= captured or source <= 0:
        return None
    clock_error = _error_at(clocks.get(sample.clock_epoch, ()), start, captured)
    source_key = event['contract_id'], str(event['source_epoch'])
    source_error = _error_at(sources.get(source_key, ()), source, source)
    if clock_error is None or source_error is None:
        return None
    elapsed_to_capture = captured - start
    difference = delta_ns(sample.utc, _EPOCH) - elapsed_to_capture - source
    # Do not rely on an exact UTC projection between entering the strategy
    # and sampling its clock. Include the full observed span in the bound.
    error = clock_error + source_error + int(sample.sample_span_ns) + elapsed_to_capture
    if difference + error < 0:
        return None
    return difference - error, difference + error


def _rank_bound(distribution, rank):
    count = 0
    for bound, frequency in sorted(distribution.items()):
        count += frequency
        if count >= rank:
            return str(bound)
    return None


def _percentile_bounds(total, lowers, uppers):
    if total == 0:
        return {}
    quantiles = {}
    for percentile in (50, 95, 99, 100):
        rank = (percentile * total + 99) // 100
        label = 'max' if percentile == 100 else f'p{percentile}'
        quantiles[label] = {'minimum_ns': _rank_bound(lowers, rank),
                            'maximum_ns': _rank_bound(uppers, rank)}
    return quantiles


def _local_bounds(row, total):
    lowers, uppers = defaultdict(int), defaultdict(int)
    last_limit, measured, measured_late = -1, 0, 0
    budget = row['m2_budget_ms'] * 1_000_000
    for limit_ms, count in zip(row['m2_histogram_upper_ms'], row['m2_histogram_counts'], strict=True):
        observed = int(count)
        lowers[last_limit + 1] += observed
        limit = int(row['m2_max_measured_ns'] or 0) if limit_ms is None else limit_ms * 1_000_000
        uppers[limit] += observed
        if last_limit >= budget:
            measured_late += observed
        measured += observed
        last_limit = limit
    unseen_late = max(0, int(row['m2_late']) - measured_late)
    lowers[budget + 1] += unseen_late
    lowers[0] += max(0, total - measured - unseen_late)
    return _percentile_bounds(total, lowers, uppers)


def build_report(directory, proof_path=None, *, trace_files=(), summary_files=()):
    problems = []
    windows = {}
    inconsistent = set()
    summary_losses, trace_losses = set(), set()
    summary_coverage, trace_coverage = {}, {}
    ended_collections, ended_traces = set(), set()
    evidence_boot, clocks, sources = _proof_index(proof_path)
    summary_records = _records(directory, 'market-latency-summary.jsonl', summary_files, problems)
    for record in _distinct_batches(summary_records, 'DSV_MARKET_LATENCY/1', summary_coverage):
        boot = record['kernel_boot_id']
        if int(record['previous_diagnostic_failures']) or int(record['previous_unqueued_batches']):
            summary_losses.add(boot)
        if record.get('collection_ended'):
            ended_collections.add(boot)
        for row in record['windows']:
            key = _index(row, boot, row['first_seen_window'])
            if key in windows:
                inconsistent.add(key)
            else:
                windows[key] = row

    observed = defaultdict(dict)
    trace_records = _records(directory, 'market-quote-trace.jsonl', trace_files, problems)
    for batch in _distinct_batches(trace_records, 'DSV_QUOTE_TRACE/2', trace_coverage):
        boot = batch['kernel_boot_id']
        if any(int(batch[key]) for key in ('dropped_records', 'previous_write_failures', 'previous_unqueued_batches')):
            trace_losses.add(boot)
        if batch['phase'] == 'SESSION_CLOSE':
            ended_traces.add(boot)
        if boot != evidence_boot:
            continue
        for event in batch['records']:
            if event.get('stage') != 'STRATEGY_START' or 'first_seen_window' not in event:
                continue
            try:
                interval = _m1_interval(event, clocks, sources)
                keys = (_index(event, boot, event['first_seen_window']), _index(event, boot, None))
                tick = str(event['native_tick_id']), str(event['native_time_ns'])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if interval is None:
                continue
            for key in keys:
                if key not in windows:
                    continue
                prior = observed[key].get(tick)
                if prior is not None and prior != interval:
                    inconsistent.add(key)
                observed[key][tick] = interval

    results = []
    for key, row in windows.items():
        total = int(row['observed_candidates'])
        intervals = observed[key].values()
        timely = sum(upper <= _M1_LIMIT_NS for _, upper in intervals)
        late = sum(lower > _M1_LIMIT_NS for lower, _ in intervals)
        has_coverage = (key not in inconsistent and not int(row['coverage_losses'])
                        and key[0] not in summary_losses and not problems and len(intervals) <= total)
        result = p99_budget_result(total, timely, late, coverage_proved=has_coverage)
        lows, highs = defaultdict(int), defaultdict(int)
        for lower, upper in intervals:
            lows[max(0, lower)] += 1
            highs[upper] += 1
        lows[0] += max(0, total - len(intervals))
        local_result = p99_budget_result(total, int(row['m2_timely']), int(row['m2_late']),
                                         coverage_proved=key not in inconsistent and not int(row['coverage_losses']))
        results.append(dict(row, kernel_boot_id=key[0], m2_p99_result=local_result, m1_budget_ms=1000,
                            m1_timely=str(timely), m1_late=str(late),
                            m1_unknown=str(max(0, total - timely - late)), m1_result=result,
                            m1_reason=None if result != 'NOT_PROVED' else 'INSUFFICIENT_PROOF_OR_COVERAGE',
                            m1_quantile_bounds_ns=_percentile_bounds(total, lows, highs),
                            m2_quantile_bounds_ns=_local_bounds(row, total)))

    missing_summary_batches = {boot: max(numbers) - len(numbers) for boot, numbers in summary_coverage.items()}
    missing_trace_batches = {boot: max(numbers) - len(numbers) for boot, numbers in trace_coverage.items()}
    gaps = (summary_losses or trace_losses or inconsistent or problems
            or any(missing_summary_batches.values()) or any(missing_trace_batches.values()))
    return {'schema_version': 'F5_LATENCY_REPORT/1',
            'recorded_evidence_gaps': bool(gaps), 'malformed_records': problems,
            'time_proof_sha256': None if proof_path is None else sha256(proof_path.read_bytes()).hexdigest(),
            'ended_diagnostic_collections': sorted(ended_collections),
            'ended_trace_collections': sorted(ended_traces),
            'missing_summary_batches_through_last_observed': missing_summary_batches,
            'missing_trace_batches_through_last_observed': missing_trace_batches, 'windows': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--time-proof', type=Path)
    parser.add_argument('--trace-file', type=Path, action='append', default=[])
    parser.add_argument('--summary-file', type=Path, action='append', default=[])
    args = parser.parse_args()
    if not args.run_dir.is_dir():
        parser.error('Run directory does not exist')
    try:
        report = build_report(args.run_dir, args.time_proof, trace_files=args.trace_file,
                              summary_files=args.summary_file)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
