"""Compare a replay JSONL trace with an independent ordered DSV expectation."""

import argparse
from hashlib import sha256
import json
from pathlib import Path

from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError


def _records(path):
    with path.open('rb') as stream:
        return [strict_loads(line) for line in stream if line.strip()]


def _validate(expected):
    if (not expected or expected[0].get('type') != 'DSV_TRACE_EXPECTATION/1' or
            not expected[0].get('reference_release') or not expected[0].get('input_sha256')):
        raise TraderError('E_DSV_TRACE', 'Expected a frozen reference and replay input fingerprint')
    records = expected[1:]
    if not records:
        raise TraderError('E_DSV_TRACE', 'Missing expected inputs')
    ids = set()
    for record in records:
        identity = record.get('input_id')
        if (not identity or identity in ids or record.get('type') not in {'ROUND', 'COMMAND', 'SNAPSHOT'}
                or not record.get('evidence')):
            raise TraderError('E_DSV_TRACE', 'Every expected input needs a unique ID, type and independent evidence')
        ids.add(identity)
        if 'pending' in record:
            if type(record['pending']) is not str or not record['pending']:
                raise TraderError('E_DSV_TRACE', 'Pending expectations need an explicit evidence gap')
        elif record['type'] == 'SNAPSHOT':
            if not record.get('state'):
                raise TraderError('E_DSV_TRACE', 'Snapshot expectations require a business-state projection')
        elif any(type(record.get(key)) is not list for key in ('decisions', 'effects')):
            raise TraderError('E_DSV_TRACE', 'Both ordered decision and effect expectations are required')
    return records


def _check(expected, actual, path, bindings, differences):
    if isinstance(expected, dict) and set(expected) == {'$id'}:
        alias = expected['$id']
        if type(alias) is not str or not alias:
            raise TraderError('E_DSV_TRACE', 'Invalid native identity alias')
        if isinstance(actual, str) and actual:
            if alias not in bindings and actual not in bindings.values():
                bindings[alias] = actual
            if bindings.get(alias) == actual:
                return
    elif type(expected) is dict and type(actual) is dict:
        for key, value in expected.items():
            at = [*path, key]
            if key in actual:
                _check(value, actual[key], at, bindings, differences)
            else:
                differences.append({'path': at, 'expected': value, 'actual_missing': True})
        return
    elif type(expected) is list and type(actual) is list:
        if len(expected) != len(actual):
            differences.append({'path': path, 'expected_count': len(expected), 'actual_count': len(actual)})
        for index, (left, right) in enumerate(zip(expected, actual)):
            _check(left, right, [*path, index], bindings, differences)
        return
    elif type(expected) is type(actual) and expected == actual:
        return
    differences.append({'path': path, 'expected': expected, 'actual': actual})


def compare(expected, candidate, *, input_sha256, candidate_release):
    records = _validate(expected)
    if expected[0]['input_sha256'] != input_sha256:
        raise TraderError('E_DSV_TRACE', 'Replay input differs from the frozen expectation')
    rows, bindings = [], {}
    for index in range(max(len(records), len(candidate))):
        left = records[index] if index < len(records) else None
        right = candidate[index] if index < len(candidate) else None
        identity = right.get('input_id', 'snapshot') if right is not None else None
        if left is None or right is None or (left['input_id'], left['type']) != (identity, right['type']):
            rows.append({'status': 'FAIL', 'input_id': left['input_id'] if left else identity,
                         'difference': 'INPUT_ORDER', 'actual_input_id': identity})
            break
        row = {'input_id': identity, 'evidence': left['evidence'], 'status': 'PASS'}
        if 'pending' in left:
            row.update(status='EXPECTATION_PENDING', reason=left['pending'])
        else:
            fields = ('state',) if left['type'] == 'SNAPSHOT' else ('decisions', 'effects')
            differences = []
            _check({key: left[key] for key in fields}, right, [], bindings, differences)
            if differences:
                row.update(status='FAIL', differences=differences)
        rows.append(row)
    first = next((row for row in rows if row['status'] == 'FAIL'), None)
    return {'type': 'DSV_TRACE_COMPARISON/1', 'reference_release': expected[0]['reference_release'],
            'input_sha256': input_sha256, 'candidate_release': candidate_release,
            'status': 'FAIL' if first else 'EXPECTATION_PENDING' if any(
                row['status'] == 'EXPECTATION_PENDING' for row in rows) else 'PASS',
            'first_divergence': first, 'rows': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('expected', 'candidate', 'input'):
        parser.add_argument('--' + field, required=True, type=Path)
    parser.add_argument('--candidate-release', required=True)
    args = parser.parse_args(argv)
    try:
        with args.input.open('rb') as stream:
            digest = sha256()
            for block in iter(lambda: stream.read(64 * 1024), b''):
                digest.update(block)
        result = compare(_records(args.expected), _records(args.candidate), input_sha256=digest.hexdigest(),
                         candidate_release=args.candidate_release)
    except (OSError, TraderError, KeyError, TypeError, AttributeError) as error:
        parser.exit(2, f'{getattr(error, "code", "E_DSV_TRACE")} {error}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'PASS': 0, 'FAIL': 1, 'EXPECTATION_PENDING': 3}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
