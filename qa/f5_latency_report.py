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
_WINDOW_NS = 15 * 60 * 1_000_000_000


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


def _counts_consistent(row, total):
    timely, late, unknown = (int(row[key]) for key in ('m2_timely', 'm2_late', 'm2_unknown'))
    measured = [int(value) for value in row['m2_histogram_counts']]
    return (min(total, timely, late, unknown, *measured) >= 0
            and timely + late + unknown == total
            and sum(measured) <= timely + late)


def _bucket_coverage(windows):
    groups = defaultdict(list)
    for key, row in windows.items():
        groups[key[:-1]].append(row)
    output = []
    for key, rows in groups.items():
        buckets = {int(row['first_seen_window']): row for row in rows if row['first_seen_window'] is not None}
        session = next((row for row in rows if row['first_seen_window'] is None), None)
        reference = session or rows[0]
        opens_utc = reference.get('session_open_utc')
        closes_utc = reference.get('session_close_utc')
        missing = None
        if opens_utc and closes_utc:
            opens_ns = delta_ns(datetime.fromisoformat(opens_utc), _EPOCH)
            closes_ns = delta_ns(datetime.fromisoformat(closes_utc), _EPOCH)
            if closes_ns <= opens_ns:
                raise ValueError('Session coverage requires an ordered calendar interval')
            first, last = opens_ns // _WINDOW_NS, (closes_ns - 1) // _WINDOW_NS
            present = sorted(bucket for bucket in buckets if first <= bucket <= last)
            missing = []
            cursor = first
            for bucket in present:
                if bucket > cursor:
                    missing.append([str(cursor), str(bucket - 1)])
                cursor = bucket + 1
            if cursor <= last:
                missing.append([str(cursor), str(last)])
        output.append({
            'kernel_boot_id': key[0], 'contract_id': key[1], 'source_epoch': key[2],
            'trading_day': key[3], 'session_id': key[4],
            'session_open_utc': opens_utc, 'session_close_utc': closes_utc,
            'calendar_buckets_without_summary': missing,
            'summary_buckets_with_polls_and_no_ticks': [
                str(bucket) for bucket, row in sorted(buckets.items())
                if int(row.get('sdk_poll_count', 0)) and int(row['observed_candidates']) == 0],
            'session_summary_present': session is not None,
        })
    return output


def _score_window(boot, row, intervals, *, coverage, source_coverage):
    total = int(row['observed_candidates'])
    counts_match = _counts_consistent(row, total)
    timely, late = 0, 0
    lows, highs = defaultdict(int), defaultdict(int)
    for lower, upper in intervals:
        timely += upper <= _M1_LIMIT_NS
        late += lower > _M1_LIMIT_NS
        lows[max(0, lower)] += 1
        highs[upper] += 1
    counts_match = counts_match and len(intervals) <= total
    lows[0] += max(0, total - len(intervals))
    coverage = coverage and counts_match and not int(row['coverage_losses'])
    source_result = p99_budget_result(total, timely, late, coverage_proved=coverage and source_coverage)
    local_result = p99_budget_result(total, int(row['m2_timely']), int(row['m2_late']), coverage_proved=coverage)
    return dict(row, kernel_boot_id=boot, m2_p99_result=local_result, m1_budget_ms=1000,
                m1_timely=str(timely), m1_late=str(late), m1_unknown=str(max(0, total - timely - late)),
                m1_result=source_result,
                m1_reason=None if source_result != 'NOT_PROVED' else 'INSUFFICIENT_PROOF_OR_COVERAGE',
                m1_quantile_bounds_ns=_percentile_bounds(total, lows, highs) if counts_match else {},
                m2_quantile_bounds_ns=_local_bounds(row, total) if counts_match else {})


def _small_groups(windows, inconsistent):
    by_session = defaultdict(list)
    for key in windows:
        if key[-1] is not None:
            by_session[key[:-1]].append(key)
    for keys in by_session.values():
        group, size = [], 0
        for key in sorted(keys, key=lambda key: int(key[-1])):
            row = windows[key]
            count = int(row['observed_candidates'])
            eligible = key not in inconsistent and 0 < count < 100 and not int(row['coverage_losses'])
            adjacent = not group or int(key[-1]) == int(group[-1][-1]) + 1
            if not eligible or not adjacent:
                if len(group) > 1:
                    yield group
                group, size = [], 0
            if eligible:
                group.append(key)
                size += count
                if size >= 100:
                    yield group
                    group, size = [], 0
        if len(group) > 1:
            yield group


def _merged_small_windows(keys, windows, observed, *, source_coverage):
    rows = [windows[key] for key in keys]
    first, last = rows[0], rows[-1]
    histogram = first['m2_histogram_upper_ms']
    if any(row['m2_histogram_upper_ms'] != histogram or row['m2_budget_ms'] != first['m2_budget_ms']
           for row in rows):
        return None
    group = {name: first[name] for name in (
        'contract_id', 'source_epoch', 'trading_day', 'session_id', 'm2_histogram_upper_ms', 'm2_budget_ms')}
    group.update({
        'member_windows': [key[-1] for key in keys],
        'first_observed_poll_ns': first['first_observed_poll_ns'],
        'last_observed_poll_ns': last['last_observed_poll_ns'],
        'm2_max_measured_ns': str(max(int(row['m2_max_measured_ns'] or 0) for row in rows)),
        'm2_histogram_counts': [str(sum(map(int, values))) for values in zip(
            *(row['m2_histogram_counts'] for row in rows), strict=True)],
    })
    for name in ('observed_candidates', 'coverage_losses', 'm2_timely', 'm2_late', 'm2_unknown'):
        group[name] = str(sum(int(row[name]) for row in rows))
    intervals = {}
    expected = 0
    for key in keys:
        evidence = observed.get(key, {})
        expected += len(evidence)
        intervals.update(evidence)
    return _score_window(keys[0][0], group, intervals.values(), coverage=True,
                         source_coverage=source_coverage and len(intervals) == expected)


def build_report(directory, proof_path=None, *, trace_files=(), summary_files=()):
    problems = []
    windows = {}
    inconsistent = set()
    summary_losses, trace_losses = set(), set()
    summary_coverage, trace_coverage = {}, {}
    declared_contracts = defaultdict(set)
    ended_collections, ended_traces = set(), set()
    evidence_boot, clocks, sources = _proof_index(proof_path)
    summary_records = _records(directory, 'market-latency-summary.jsonl', summary_files, problems)
    for record in _distinct_batches(summary_records, 'DSV_MARKET_LATENCY/1', summary_coverage):
        boot = record['kernel_boot_id']
        declared_contracts[boot].update(record.get('contracts', ()))
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
        if len(observed[key]) > total or not _counts_consistent(row, total):
            inconsistent.add(key)
        results.append(_score_window(key[0], row, observed[key].values(), coverage=key not in inconsistent,
                                     source_coverage=key[0] not in summary_losses and not problems))
    merged = []
    for keys in _small_groups(windows, inconsistent):
        group = _merged_small_windows(keys, windows, observed,
                                      source_coverage=keys[0][0] not in summary_losses and not problems)
        if group is not None:
            merged.append(group)

    missing_summary_batches = {boot: max(numbers) - len(numbers) for boot, numbers in summary_coverage.items()}
    missing_trace_batches = {boot: max(numbers) - len(numbers) for boot, numbers in trace_coverage.items()}
    coverage = _bucket_coverage(windows)
    missing_contracts = {}
    for boot, expected in declared_contracts.items():
        seen = {item['contract_id'] for item in coverage if item['kernel_boot_id'] == boot}
        missing_contracts[boot] = sorted(expected - seen)
    open_summary_collections = sorted(set(summary_coverage) - ended_collections)
    unconfirmed_trace_collections = sorted(set(summary_coverage) - ended_traces)
    gaps = (not windows or summary_losses or trace_losses or inconsistent or problems
            or open_summary_collections or unconfirmed_trace_collections or any(missing_contracts.values())
            or any(missing_summary_batches.values()) or any(missing_trace_batches.values())
            or any(int(row['coverage_losses']) for row in windows.values())
            or any(item['calendar_buckets_without_summary'] != [] or not item['session_summary_present']
                   for item in coverage))
    return {'schema_version': 'F5_LATENCY_REPORT/1',
            'recorded_evidence_gaps': bool(gaps), 'malformed_records': problems,
            'time_proof_sha256': None if proof_path is None else sha256(proof_path.read_bytes()).hexdigest(),
            'ended_diagnostic_collections': sorted(ended_collections),
            'open_or_unconfirmed_diagnostic_collections': open_summary_collections,
            'contracts_without_observed_session': missing_contracts,
            'ended_trace_collections': sorted(ended_traces),
            'trace_collections_without_end_marker': unconfirmed_trace_collections,
            'missing_summary_batches_through_last_observed': missing_summary_batches,
            'missing_trace_batches_through_last_observed': missing_trace_batches,
            'session_bucket_coverage': coverage, 'windows': results, 'merged_low_sample_windows': merged}


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
