"""Explicitly authorized local application; never connects or starts trading."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.runtime.auto_offsets import load_preparation, compile_preparation, dataset_from_rows, cache_path
from online_trader.runtime.comparison import read_dsv_profile, verify_dsv_profile
from online_trader.runtime.dsv_simulation import load_inputs, prepare
from online_trader.online_scope import DsvSimulationGrant
from online_trader.scheduling.reference import measure_reference, clock_observation


def scalar_spans(text):
    """Find leaf token spans, retaining unrelated source bytes verbatim."""
    decoder = json.JSONDecoder(); spans = {}
    def space(pos):
        while pos < len(text) and text[pos].isspace(): pos += 1
        return pos
    def visit(pos, path):
        pos = space(pos)
        if text[pos] == '{':
            pos = space(pos+1)
            if text[pos] == '}': return pos+1
            while True:
                key, pos = decoder.raw_decode(text, pos); pos = space(pos)
                assert text[pos] == ':'
                pos = space(visit(pos+1, path+(key,)))
                if text[pos] == '}': return pos+1
                assert text[pos] == ','; pos = space(pos+1)
        if text[pos] == '[':
            pos = space(pos+1); index = 0
            if text[pos] == ']': return pos+1
            while True:
                pos = space(visit(pos, path+(index,))); index += 1
                if text[pos] == ']': return pos+1
                assert text[pos] == ','; pos = space(pos+1)
        value, end = decoder.raw_decode(text, pos)
        spans[path] = (pos, end)
        return end
    assert space(visit(0, ())) == len(text)
    return spans


def main():
    if sys.argv[1:] != ['--apply-user-authorized-no-orders']:
        raise SystemExit('Explicit application argument required')
    base = ROOT/'output/simnow/20261110-night-ready'
    original_packet = base/'prepared-auto-dev22-final'
    source, old_profile, calendar = load_inputs(original_packet/'scope.json', original_packet/'dsv-profile.json', original_packet/'calendar.json')
    saved, _, calculated = load_preparation(source, old_profile)
    replay = json.loads((ROOT/'output/validation/dsv-real-input-replay-20261110-final/result.json').read_bytes())
    assert replay['status'] == 'ALL_CURRENT_ROWS_MATCH' and replay['preparation_hash'] == saved['preparation_hash']
    target = Path(r'C:\Users\xyc\Desktop\trading\config.json')
    raw = target.read_bytes(); original = json.loads(raw.decode('utf-8-sig'))
    assert read_dsv_profile(target).content_hash == old_profile.content_hash, 'DSV changed since review; do not overwrite'
    desired = deepcopy(original)
    offsets = {row['code']: row for row in calculated.to_record()['settings']['symbols']}
    edits = {}
    for index, row in enumerate(desired['symbols']):
        for field in ('open_offset', 'stop_offset', 'profit_offset'):
            value = int(offsets[row['code']][field])
            if row[field] != value:
                row[field] = value; edits[('symbols', index, field)] = value
    for path in (('trade', 'auto_start_on_launch'), ('schedule', 'enabled')):
        if desired[path[0]][path[1]]:
            desired[path[0]][path[1]] = False; edits[path] = False
    text = raw.decode('utf-8-sig'); spans = scalar_spans(text)
    patched = text
    for path, value in sorted(edits.items(), key=lambda item: spans[item[0]][0], reverse=True):
        start, end = spans[path]
        patched = patched[:start] + json.dumps(value) + patched[end:]
    assert json.loads(patched) == desired
    output_bytes = (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + patched.encode('utf-8')
    applied = DsvProfile.from_dsv_json(output_bytes)
    expected = calculated.to_record()
    expected['settings']['trade']['auto_start_on_launch'] = False
    expected['settings']['schedule']['enabled'] = False
    assert applied.content_hash == DsvProfile.from_record(expected).content_hash
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    packet = base/('applied-comparison-'+stamp)
    packet.mkdir()
    def write(path, value):
        with path.open('xb') as stream: stream.write(canonical_bytes(value))
    value = source.value
    value.update(grant_id='simnow-matched-'+uuid.uuid4().hex,
                 profile_hash=applied.content_hash, ledger_path=str(packet/'ledger.db'),
                 authorization_reference='USER_APPLY_MATCHED_PARAMETERS_PREPARE_ONLY_20261110_NO_ORDERS')
    new_source = DsvSimulationGrant.parse(canonical_bytes(value))
    receipt, fixed_scope, effective = compile_preparation(new_source, applied,
        dataset_from_rows(saved['daily_rows']), saved['realized_ranges'], saved['source_evidence'])
    assert effective.content_hash == applied.content_hash
    assert receipt['calculation']['data_hash'] == saved['calculation']['data_hash']
    assert receipt['realized_ranges'] == saved['realized_ranges']
    write(packet/'scope.json', new_source.value)
    write(packet/'dsv-profile.json', applied.to_record())
    write(packet/'effective-profile.json', effective.to_record())
    write(packet/'calendar.json', calendar.to_record())
    write(packet/'calculation-preparation.json', receipt)
    cache = cache_path(new_source); cache.parent.mkdir(parents=True)
    write(cache, receipt)
    (packet/'business-settings.json').write_text(applied.to_json(), encoding='utf-8')
    from online_trader.configuration.symbol_csv import export_symbols
    export_symbols(applied, packet/'matched-symbols.csv')
    # Both sides are stopped. Backup private bytes only alongside their original,
    # never into product evidence. Preserve all unrelated tokens verbatim.
    backup = target.with_name('config.json.before-OnlineTrader-'+stamp+'.bak')
    with backup.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    assert target.read_bytes() == raw, 'DSV changed during application'
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.matched-offsets-', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name); stream.write(output_bytes); stream.flush(); os.fsync(stream.fileno())
    try: os.replace(temporary, target)
    finally:
        if temporary.exists(): temporary.unlink()
    assert target.read_bytes() == output_bytes
    assert read_dsv_profile(target).content_hash == applied.content_hash
    assert verify_dsv_profile(target, effective, source_profile=applied).content_hash == effective.content_hash
    current = json.loads(target.read_bytes().decode('utf-8-sig'))
    assert all(current[k] == original[k] for k in original if k not in {'trade', 'schedule', 'symbols'})
    assert all(current['trade'][k] == original['trade'][k] for k in original['trade'] if k != 'auto_start_on_launch')
    assert all(current['schedule'][k] == original['schedule'][k] for k in original['schedule'] if k != 'enabled')
    start = (original_packet/'Start-Night.ps1').read_text(encoding='utf-8-sig')
    (packet/'Start-Night.ps1').write_text(start, encoding='utf-8')
    readiness = prepare(packet/'scope.json', packet/'dsv-profile.json', packet/'calendar.json', packet/'prepare-check',
                        dsv_config=target, dsv_log_dir=target.parent/'logs')
    observation = clock_observation(measure_reference())
    write(packet/'clock-check.json', observation)
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    sessions = {code: calendar.query(code, now) for code in source.value['contracts']}
    write(packet/'session-state-at-application.json', {'at_utc': now, 'contracts': sessions})
    result = {'status': 'APPLIED_TO_BOTH_STARTUP_CONFIGURATIONS_DISARMED', 'packet': str(packet),
              'applied_profile_hash': applied.content_hash, 'original_effective_profile_hash': calculated.content_hash,
              'original_preparation_hash': saved['preparation_hash'], 'new_preparation_hash': receipt['preparation_hash'],
              'dataset_hash': receipt['calculation']['data_hash'], 'realized_ranges_unchanged': True,
              'dsv_config': str(target), 'private_backup': str(backup),
              'dsv_before_sha256': hashlib.sha256(raw).hexdigest(), 'dsv_after_sha256': hashlib.sha256(output_bytes).hexdigest(),
              'private_and_unrelated_settings_preserved': True,
              'changed_fields': [{'path': list(path), 'after': val} for path, val in edits.items()],
              'automatic_start_enabled': False, 'scheduled_start_enabled': False,
              'entry_authority': 'DISARMED', 'execution_owner_created': False,
              'account_connection_performed': False, 'orders': 0, 'cancellations': 0,
              'preparation': readiness, 'clock_aligned': observation['system_aligned'],
              'dsv_ui_loaded_verified': False, 'product_ui_loaded_verified': False,
              'runtime_strategy_parameters_verified': False}
    write(packet/'application-result.json', result)
    # A separate pointer selects the prepared package without rewriting history.
    (base/'current-comparison-packet.json').write_bytes(canonical_bytes({'packet': str(packet),
         'profile_hash': applied.content_hash, 'entry_authority': 'DISARMED', 'last_application': stamp}))
    print(json.dumps({'status': result['status'], 'packet': str(packet), 'profile_hash': applied.content_hash,
                      'private_and_unrelated_settings_preserved': True, 'clock_aligned': observation['system_aligned'],
                      'entry_authority': 'DISARMED'}, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
