"""Run only extracted pure DSV calculator functions on saved real inputs.

Requires an independent Python 3.14/pandas QA environment. Does not import the
DSV application or SDK, evaluate its module, access credentials, or connect.
"""
import argparse
import builtins
import dis
import hashlib
import json
import marshal
from pathlib import Path
import struct
import sys
import types
import zlib

import numpy as np
import pandas as pd


def extract_module(executable, name):
    data = executable.read_bytes()
    cookie = data.rfind(b'MEI\014\013\012\013\016')
    magic, size, offset, length, version, library = struct.unpack('!8sIIII64s', data[cookie:cookie+88])
    assert magic == b'MEI\014\013\012\013\016' and version == 314
    start = cookie + 88 - size
    toc = data[start+offset:start+offset+length]
    pos = 0
    while pos < len(toc):
        size, offset, compressed, raw_size, flag, kind = struct.unpack('!IIIIBc', toc[pos:pos+18])
        entry = toc[pos+18:pos+size].split(b'\0')[0].decode('utf-8')
        if entry == 'PYZ.pyz':
            pyz = data[start+offset:start+offset+compressed]
            if flag:
                pyz = zlib.decompress(pyz)
            assert len(pyz) == raw_size
            break
        pos += size
    else:
        raise ValueError('PYZ absent')
    assert pyz[:4] == b'PYZ\0'
    index = struct.unpack('!i', pyz[8:12])[0]
    entries = dict(marshal.loads(pyz[index:]))
    kind, offset, length = entries[name]
    raw = zlib.decompress(pyz[offset:offset+length])
    return marshal.loads(raw), raw, hashlib.sha256(data).hexdigest(), pyz, entries


def literal_tables(code):
    """Decode initial literal MINRANGE assignments; execute no module code."""
    values = {}
    stack = []
    active = False
    for instruction in dis.get_instructions(code):
        op, value = instruction.opname, instruction.argval
        if not active:
            if op == 'STORE_NAME' and value == 'math':
                active = True
            continue
        if op in {'LOAD_CONST', 'LOAD_SMALL_INT'}:
            stack.append(value)
        elif op == 'LOAD_NAME':
            stack.append(values[value])
        elif op == 'SWAP':
            index = instruction.arg
            stack[-1], stack[-index] = stack[-index], stack[-1]
        elif op == 'STORE_NAME':
            values[value] = stack.pop()
            if value == 'MINRANGE':
                return values
        elif op == 'BUILD_MAP':
            count = instruction.arg * 2
            cells = stack[-count:] if count else []
            if count:
                del stack[-count:]
            stack.append(dict(zip(cells[::2], cells[1::2])))
        else:
            raise ValueError('Unexpected literal opcode: ' + op)
    raise ValueError('MINRANGE absent')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--facts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root, raw, exe_hash, pyz, entries = extract_module(args.executable, 'offset_calc')
    facts = json.loads(args.facts.read_text(encoding='utf-8'))
    assert exe_hash == facts['source_executable_sha256']
    assert hashlib.sha256(raw).hexdigest() == facts['module_marshaled_sha256']
    version_constants = {}
    for name in ('pandas._version_meson', 'numpy.version'):
        kind, offset, length = entries[name]
        module = marshal.loads(zlib.decompress(pyz[offset:offset+length]))
        version_constants[name] = [v for v in module.co_consts if isinstance(v, str) and len(v) < 80]
    assert pd.__version__ == version_constants['pandas._version_meson'][0], 'Use the bundled pandas version in the isolated QA environment'
    assert np.__version__ in version_constants['numpy.version'], 'Use the bundled numpy version in the isolated QA environment'
    record = json.loads(args.receipt.read_text(encoding='utf-8'))
    calculation = record['calculation']
    assert calculation['formula'] == 'minrange', 'Replay scope is the current minrange setup'
    literals = literal_tables(root)
    # Only current MINRANGE rows are needed by get_coef. They come directly from
    # the executable's literal bytecode, not the new product's implementation.
    globals_ = {**literals,
                'COEF': {'minrange': {name: {'coef': row['coef']} for name, row in literals['MINRANGE'].items()}},
                'STOP_DEFAULT': facts['fixed_stops']}
    def allowed_import(name, *a, **kw):
        if name != 'pandas':
            raise RuntimeError('Calculator import blocked: ' + name)
        return pd
    globals_['__builtins__'] = {name: getattr(builtins, name) for name in
                               ('len', 'str', 'float', 'int', 'round', 'max', 'min', 'dict')}
    globals_['__builtins__']['__import__'] = allowed_import
    wanted = {'source_of', 'windows_of', 'get_coef', 'main_cont_code', '_tr', 'base_series', 'completed_only', 'calc_one'}
    function_provenance = {}
    for code in root.co_consts:
        if isinstance(code, types.CodeType) and code.co_name in wanted:
            globals_[code.co_name] = types.FunctionType(code, globals_)
            function_provenance[code.co_name] = {'first_line': code.co_firstlineno,
                'bytecode_sha256': hashlib.sha256(code.co_code).hexdigest()}
    assert wanted <= globals_.keys()
    for name in ('source_of', 'base_series', 'completed_only'):
        globals_[name].__defaults__ = (None,)
    globals_['calc_one'].__defaults__ = ('minrange', None, None, None, .5, .4, .9, None)
    # Block network/process activity even if an unexpected dependency path runs.
    def audit(event, arguments):
        if event.startswith(('socket.', 'subprocess.')) or event in {'os.system', 'os.spawn', 'os.exec'}:
            raise RuntimeError('Read-only replay blocked: ' + event)
    sys.addaudithook(audit)
    settings = record['source_profile']['settings']
    auto = settings['auto_offset']
    rows = []
    symbols = {row['code']: row for row in settings['symbols']}
    for product in calculation['rows']:
        code = product['contract_id']
        symbol = symbols[code]
        name = symbol['name']
        source = globals_['source_of'](auto['formula'], name)
        identity = code if source == 'contract' else globals_['main_cont_code'](code)
        assert identity == product['series_id']
        bars = [row for row in record['daily_rows'] if row['series_id'] == identity]
        frame = pd.DataFrame(bars)
        for field in ('high', 'low', 'close'):
            frame[field] = frame[field].astype('float64')
        override = auto['coef'].get(name)
        coefficient = float(override) if override is not None and float(override) > 0 else globals_['get_coef'](auto['formula'], name)
        kwargs = {'formula': auto['formula'], 'coef': coefficient,
                  'stop': globals_['STOP_DEFAULT'].get(name),
                  'realized_range': float(record['realized_ranges'][code]),
                  'ceiling_ratio': float(auto['ceiling']), 'floor_ratio': float(auto['floor']),
                  'exclude_date': calculation['as_of_date']}
        original = globals_['calc_one'](frame, name, **kwargs)
        assert original is not None
        completed = globals_['completed_only'](frame, kwargs['exclude_date'])
        # Original base_series 176-177 uses `s_n or 3`, `l_n or 30`.
        # calc_one returns raw table windows, which are (None, None) for silver;
        # the product records the actual calculation windows instead.
        actual_windows = [original['windows'][0] or 3, original['windows'][1] or 30]
        checks = {field: str(original[field]) == product[field] for field in ('open_cfg', 'open', 'stop', 'profit', 'source')}
        checks.update(actual_windows=actual_windows == product['windows'],
                      coefficient=original['coef'] == float(product['coefficient']),
                      base=abs(original['base'] - float(product['base'])) <= 1e-10,
                      completed_bars=len(completed) == product['completed_bars'],
                      last_completed_date=str(completed['date'].iloc[-1]) == product['last_completed_date'])
        rows.append({'contract_id': code, 'name': name, 'series_id': identity,
                     'input_bars': len(frame), 'completed_bars': len(completed),
                     'last_completed_date': str(completed['date'].iloc[-1]),
                     'original_actual_windows': actual_windows,
                     'window_metadata_note': 'Original raw None windows fall back to 3/30 in base_series; product records actual windows' if list(original['windows']) != actual_windows else None,
                     'original_dsv': original, 'online_trader': product,
                     'checks': checks, 'all_equal': all(checks.values())})
    result = {'status': 'ALL_CURRENT_ROWS_MATCH' if all(row['all_equal'] for row in rows) else 'MISMATCH',
              'method': 'Original extracted pure function bytecode, original literal MINRANGE tables, real pandas; no application module evaluation',
              'source_executable_sha256': exe_hash, 'offset_module_marshaled_sha256': hashlib.sha256(raw).hexdigest(),
              'receipt_sha256': hashlib.sha256(args.receipt.read_bytes()).hexdigest(),
              'preparation_hash': record['preparation_hash'], 'dataset_hash': calculation['data_hash'],
              'python': sys.version, 'pandas': pd.__version__, 'numpy': np.__version__,
              'dependency_versions_match_original_bundle': True,
              'bundled_version_constants': version_constants, 'function_provenance': function_provenance,
              'formula': auto['formula'], 'ceiling': auto['ceiling'], 'floor': auto['floor'],
              'exclude_date': calculation['as_of_date'], 'rows': rows,
              'account_connection': False, 'orders': 0, 'cancellations': 0,
              'dsv_application_launched': False, 'dsv_module_imported': False,
              'sdk_imported': False, 'product_source_changed': False}
    (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'pandas': pd.__version__,
          'bundled_version_constants': version_constants,
          'rows': [{'name': r['name'], 'original': [r['original_dsv'][k] for k in ('open', 'stop', 'profit')],
                    'product': [r['online_trader'][k] for k in ('open', 'stop', 'profit')], 'all_equal': r['all_equal']}
                   for r in rows]}, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'ALL_CURRENT_ROWS_MATCH' else 2


if __name__ == '__main__':
    raise SystemExit(main())
