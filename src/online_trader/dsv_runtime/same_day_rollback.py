"""Convert settled replacement facts into a disarmed legacy-shaped copy."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import decimal_text, identifier
from online_trader.dsv_runtime.migration import rollback_snapshots, _source_digest
from online_trader.dsv_runtime.rollback import _check_allocations, _check_terminal, _native_flat, _publish
from online_trader.errors import TraderError
from online_trader.ledger.dsv_rollback import EVENT
from online_trader.ledger.store import Ledger
from online_trader.strategy.entry import DsvEntryStrategy
from online_trader.strategy.statistics import apply_event, initial_state
from online_trader.strategy.stops import evaluate_stops


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _wall_seconds(text):
    return _utc(_EPOCH + timedelta(seconds=float(text)))


def _source_rows(legacy, day):
    entries, statistics, stops, latest = {}, {}, {}, {}
    for record in legacy.execute('SELECT event_type,payload_json FROM event_journal ORDER BY event_seq'):
        data = strict_loads(record['payload_json'])['data']
        cycle = data.get('cycle') or {}
        metadata = (cycle.get('strategy_metadata') or {}) if record['event_type'] == 'ENTRY_PREPARED' else {}
        entry = metadata.get('strategy_state') or data.get('dsv_strategy_state')
        stat = data.get('nominal_statistics')
        stop = metadata.get('stop_result') or data.get('dsv_strategy_stops')
        if entry and entry['trading_day'] == day:
            entries[entry['contract_id']] = entry
        if stat and stat['state']['trading_day'] == day:
            statistics[stat['state']['contract_id']] = stat['state']
            stop = stat['stop_result']
        if stop and stop['state']['trading_day'] == day:
            stops[stop['state']['contract_id']] = stop
        if cycle and cycle.get('lifecycle') == 'CLOSED':
            latest[cycle['position_cycle_id']] = cycle
    return entries, statistics, stops, latest


def _trade_rows(db, state, blockers):
    times = {}
    for seq, payload in db.execute("SELECT seq,payload FROM events WHERE kind='ROUND' ORDER BY seq"):
        row = strict_loads(payload)
        frame = row['input']
        for fact in frame['native']:
            if fact['kind'] == 'TRADE':
                key = (fact['child_order_id'], fact['native_order_id'], fact['native_trade_id'])
                times.setdefault(key, (seq, frame['wall_time']))
    grouped = {}
    for day, order_id, trade_id, payload in db.execute(
            'SELECT trading_day,native_order,native_trade,payload FROM native_trades'):
        fact = strict_loads(payload)
        child = fact['child_order_id']
        order = state['orders'].get(child)
        timing = times.get((child, order_id, trade_id))
        if order is None or timing is None:
            blockers.add('REVERSE_TRADE_PROVENANCE_MISSING')
            continue
        if day != state['trading_day'] or fact.get('fill_day', day) != day:
            blockers.add('REVERSE_TRADE_CROSSES_DAY')
        seq, wall = timing
        stamp = fact.get('source_time_ns')
        observed = _utc(_EPOCH + timedelta(microseconds=int(stamp) // 1000)) if stamp else wall
        grouped.setdefault(order['position_id'], []).append({
            'source_event_seq': str(seq), 'contract_id': order['contract_id'], 'child_order_id': child,
            'position_cycle_id': order['position_id'],
            'order_id': order_id, 'trade_id': trade_id, 'trading_day': day, 'effect': order['effect'],
            'direction': order['direction'], 'native_offset': order['native_offset'], 'quantity': fact['quantity'],
            'price': fact['price'], 'observed_at_utc': observed, 'accepted_at_utc': wall, 'source_time_ns': stamp})
    return grouped


def _event(profile, account, day, position, code, kind, receipt_id):
    pos = position['id']
    real_kind = 'CONTINUATION' if position.get('entry_kind') == 'CONTINUATION_ENTRY' else 'NORMAL'
    return {'type': kind, 'event_id': 'reverse-stat-' + content_hash([pos, kind]),
            'evidence_id': receipt_id, 'account_key': account, 'contract_id': code, 'trading_day': day,
            'position_cycle_id': pos, 'plan_id': 'reverse-plan-' + pos, 'plan_hash': content_hash(
                {key: position[key] for key in ('id', 'direction', 'theory', 'stop', 'profit', 'quantity')}),
            'profile_hash': profile.content_hash, 'direction': position['direction'], 'entry_kind': real_kind,
            'theoretical_entry_price': decimal_text(Decimal(position['theory'])),
            'stop_distance': decimal_text(Decimal(position['stop'])),
            'take_profit_distance': decimal_text(Decimal(position['profit']))}


def _merge_stats(profile, account, code, day, base, cycles, grouped, blockers):
    stat = base if base is not None else initial_state(account, code, day)
    chronology = []
    manual_delta = Decimal(0)
    for position in cycles:
        fills = grouped.get(position['id'], [])
        if not fills:
            continue
        opens = sorted((f for f in fills if f['effect'] == 'OPEN'), key=lambda f: int(f['source_event_seq']))
        closes = [f for f in fills if f['effect'] == 'REDUCE']
        total = sum(f['quantity'] for f in opens)
        exit_info = position.get('rollback_exit')
        if not exit_info or not opens or total != sum(f['quantity'] for f in closes):
            blockers.add('REVERSE_EXIT_BASIS_UNPROVED:' + code)
            continue
        first = opens[0]
        last = max(fills, key=lambda f: int(f['source_event_seq']))
        entry = _event(profile, account, day, position, code, 'ENTRY_CONFIRMED',
                       'reverse-entry-' + content_hash(first))
        entry.update(requested_quantity=position['quantity'], confirmed_quantity=first['quantity'])
        exit_event = _event(profile, account, day, position, code, 'CYCLE_CLOSED',
                            'reverse-close-' + content_hash(position['id']))
        price = exit_info['price']
        if exit_info['reason'] == 'MANUAL_EXIT':
            with localcontext() as ctx:
                ctx.prec = 64
                average = sum(Decimal(f['price']) * f['quantity'] for f in closes) / total
                price = decimal_text(average.quantize(Decimal('0.000000000001')))
        if exit_info['reason'] == 'PROTECTED_FLOOR':
            sign = 1 if position['direction'] == 'LONG' else -1
            ratio = Decimal(profile.to_record()['settings']['trade']['protect_keep_ratio']) or Decimal('0.5')
            price = decimal_text(Decimal(position['theory']) + sign * Decimal(position['profit']) * ratio)
        exit_event.update(flat_confirmed=True, orders_terminal=True, exit_reason=exit_info['reason'],
                          closed_at_utc=last['accepted_at_utc'], exit_triggered_at_utc=exit_info['at'],
                          nominal_exit_price=price, adverse_slippage_points=exit_info['slip'])
        chronology.extend([(int(first['source_event_seq']), 0, entry), (int(last['source_event_seq']), 1, exit_event)])
    for _, _, event in sorted(chronology, key=lambda x: x[:2]):
        try:
            update = apply_event(profile, stat, event)
            stat = update['state']
            if event.get('exit_reason') == 'MANUAL_EXIT':
                manual_delta += Decimal(update['receipt']['net_loss_delta_points'])
        except TraderError:
            blockers.add('REVERSE_STATISTICS_UNPROVED:' + code)
            break
    return stat, manual_delta


def _business_rows(state, legacy, profile, account, grouped, blockers):
    day = state['trading_day']
    entries, statistics, oldstops, oldcycles = _source_rows(legacy, day)
    result = {}
    retired = state['retired_positions'].values()
    for code, row in state['symbols'].items():
        cycles = [p for p in retired if p.get('contract_id') == code or any(
            state['orders'][child]['contract_id'] == code for child in p['children'])]
        if any(p.get('open_day') != day for p in cycles):
            blockers.add('REVERSE_CYCLE_CROSSES_DAY:' + code)
        if row.get('need_hyst') or row.get('hyst_armed') or row.get('open_half_reached'):
            blockers.add('REVERSE_EXPLICIT_OPEN_OBSERVATION_UNMAPPED:' + code)
        legacy_count = sum(c['plan']['trading_day'] == day and c['plan']['contract_id'] == code
                           and c.get('entry_origin') == 'STRATEGY' and (
                               c.get('strategy_target_accepted') or any(
                                   o['effect'] == 'OPEN' and o['send_state'] in {'CONFIRMED', 'CALL_RETURNED', 'UNKNOWN'}
                                   for o in c['orders'])) for c in oldcycles.values())
        imported_count = row['target_count'] - legacy_count
        if imported_count < 0 or imported_count < len(cycles):
            blockers.add('REVERSE_TARGET_COUNT_UNPROVED:' + code)
        entry = DsvEntryStrategy(profile).initial_state(account, code, day)
        entry.update(consumption_version=1, last_signal_bar=row['last_signal_bar'])
        if entry['last_signal_bar'] is not None:
            identifier(entry['last_signal_bar'])
        prior_attempt = entries.get(code, {}).get('last_ordinary_attempt')
        ordinary = [p for p in cycles if p.get('entry_kind') != 'CONTINUATION_ENTRY']
        if ordinary:
            latest = max(ordinary, key=lambda p: int(p['order_ms']))
            prior_attempt = {'direction': latest['direction'],
                             'at_utc': _wall_seconds(int(latest['order_ms']) / 1000)}
        entry['last_ordinary_attempt'] = prior_attempt
        night = row.get('night_reference')
        if night:
            entry['night_reference'] = {'session_id': 'rollback-night', 'source_time_utc': night['native_time'],
                                        'source_epoch': 1, 'sequence': 0, 'native_tick_id': None,
                                        'high': night['high'], 'low': night['low'], 'last': night['last'],
                                        'boundary_proved': night['complete']}
        baseline = statistics.get(code)
        prior = oldstops.get(code, {}).get('state')
        if prior and baseline is None:
            baseline = initial_state(account, code, day)
            baseline.update({key: prior[key] for key in ('net_loss_points', 'stop_streak')})
        stat, manual_delta = _merge_stats(profile, account, code, day, baseline, cycles, grouped, blockers)
        last_stop = row.get('last_stop')
        trade = profile.to_record()['settings']['trade']
        if last_stop and last_stop['until'] and trade['stop_cooldown_enabled']:
            direction = last_stop['direction']
            until = _wall_seconds(last_stop['until'])
            duration = Decimal(trade['stop_cooldown_minutes']) * 60000
            stat.update(cooldown_direction=direction, cooldown_until={'LONG': None, 'SHORT': None},
                        cooldown_started_at_utc=_wall_seconds(float(last_stop['until']) - float(duration) / 1000),
                        cooldown_duration_ms=decimal_text(duration))
            stat['cooldown_until'][direction] = until
        if Decimal(stat['net_loss_points']) != Decimal(row['net_loss']) + manual_delta:
            blockers.add('REVERSE_NET_LOSS_DISAGREES:' + code)
        stat.update(day_stopped=row['day_stop'], day_stopped_reason=row.get('day_stop_reason'),
                    continuation_stopped=row.get('continuation_stopped', False), stop_streak=row.get('stop_streak', 0))
        if stat['day_stopped'] and stat['day_stopped_reason'] is None:
            stat['day_stopped_reason'] = '当日策略停开'
        stops = evaluate_stops(profile, code, day, net_loss_points=stat['net_loss_points'],
                               stop_streak=stat['stop_streak'], day_stopped=stat['day_stopped'],
                               day_stopped_reason=stat['day_stopped_reason'], continuation_stopped=stat['continuation_stopped'],
                               stop_days=row.get('stop_days', []), month_stop=row.get('month_stop'), trading_days=[])
        result[code] = {'entry': entry, 'statistics': stat, 'stops': stops, 'added_targets': max(0, imported_count),
                        'target_count': row['target_count'], 'continuation_attempted': row.get('continuation_attempted', False),
                        'legacy_manual_exit_adjustment': decimal_text(manual_delta),
                        'night_close_direction': (night or {}).get('fallback'), 'last_stop': last_stop,
                        'confirmed_entry_quantity': sum(f['quantity'] for p in cycles for f in grouped.get(p['id'], ())
                                                        if f['effect'] == 'OPEN'),
                        'confirmed_exit_quantity': sum(f['quantity'] for p in cycles for f in grouped.get(p['id'], ())
                                                       if f['effect'] == 'REDUCE')}
    return result


def convert_same_day(source, candidate, profile_record, target=None):
    profile = DsvProfile.from_record(profile_record)
    if target is not None and (target.exists() or target.is_symlink()):
        raise TraderError('E_DUPLICATE_CONFLICT', 'Use a new isolated rollback directory')
    with rollback_snapshots(source, candidate) as (legacy, db, receipt, state, blockers):
        account = legacy.execute('SELECT account_key FROM ledger_scope').fetchone()[0]
        if receipt['target_scope']['configuration_id'] != profile.content_hash:
            raise TraderError('E_DSV_ROLLBACK', 'Rollback must use the same effective DSV profile')
        if state['trading_day'] != receipt['target_scope']['trading_day']:
            blockers.add('REVERSE_REQUIRES_SAME_CABINET_DAY')
        if legacy.execute('SELECT 1 FROM event_journal WHERE event_type=? LIMIT 1', (EVENT,)).fetchone():
            blockers.add('LEGACY_REVERSE_HANDOFF_REQUIRES_REVIEW')
        native_orders = _check_terminal(db, state, blockers)
        count = _check_allocations(db, state, native_orders, blockers)
        if not count:
            blockers.add('NO_CANDIDATE_TRADES_TO_REHEARSE')
        latest = db.execute("SELECT seq,payload FROM events WHERE kind='ROUND' ORDER BY seq DESC LIMIT 1").fetchone()
        if latest is None:
            blockers.add('REVERSE_NATIVE_FLAT_UNPROVED')
        else:
            frame = strict_loads(latest['payload'])['input']
            if (frame.get('synchronized') is not True or not _native_flat(frame, set(state['symbols']))):
                blockers.add('REVERSE_NATIVE_FLAT_UNPROVED')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            blockers.add('TARGET_STORAGE_CHECK_FAILED')
        result = {'type': 'DSV_SAME_DAY_ROLLBACK/1', 'mode': 'PREFLIGHT', 'cabinet_day': state['trading_day'],
                  'migration_id': receipt['migration_id'], 'source_digest': receipt['source_digest'],
                  'candidate_digest': _source_digest(db), 'profile_hash': profile.content_hash,
                  'candidate_trades': str(count), 'execution_authority': False}
        if not blockers:
            grouped = _trade_rows(db, state, blockers)
            business = _business_rows(state, legacy, profile, account, grouped, blockers)
        result.update(eligible=not blockers, blockers=sorted(blockers))
        if result['eligible']:
            cycles = {key: {field: position.get(field) for field in ('direction', 'theory', 'multiplier')}
                      for key, position in state['retired_positions'].items() if key in grouped}
            material = {'account_key': account, 'trading_day': state['trading_day'], 'profile': profile.to_record(),
                        'migration_id': receipt['migration_id'], 'source_digest': receipt['source_digest'],
                        'candidate_digest': result['candidate_digest'], 'candidate_seq': str(latest['seq']),
                        'symbols': business, 'trades': grouped, 'closed_cycles': cycles,
                        'orders': sorted(state['orders'].values(), key=lambda item: item['child_order_id'])}
            result['business_digest'] = content_hash(material)
            result['symbols'] = {code: {key: row[key] for key in ('target_count', 'added_targets',
                                                                'confirmed_entry_quantity', 'confirmed_exit_quantity')}
                                 for code, row in business.items()}
            if target is not None:
                result['mode'] = 'ISOLATED_CONVERSION'
                _publish(legacy, db, target, result, convert=lambda copy: _append(copy, material))
        return result


def _append(db, material):
    account = material['account_key']
    db.execute('PRAGMA foreign_keys=ON')
    now = _utc(datetime.now(timezone.utc))
    key = content_hash(material)
    boot = 'reverse-' + key[:48]
    with db:
        auth = db.execute('SELECT state_revision FROM authority WHERE singleton=1').fetchone()
        if auth is None:
            raise TraderError('E_DSV_ROLLBACK', 'Legacy authority record is absent')
        revision = auth[0] + 1
        seq = db.execute('SELECT COALESCE(MAX(event_seq),0)+1 FROM event_journal').fetchone()[0]
        epoch = db.execute('SELECT COALESCE(MAX(kernel_epoch),0)+1 FROM kernel_boots').fetchone()[0]
        db.execute('INSERT INTO kernel_boots VALUES(?,?,?,?,?,?)', (boot, account, epoch, key, now, now))
        data = {'account_key': account, 'dsv_rollback': material}
        raw, digest = Ledger._journal_payload(data, revision)
        Ledger._insert_event(db, seq, boot, 'TX_COUNTER', EVENT, raw, digest, boot, None)
        policy = canonical_bytes({'allowed_origins': [], 'same_day_rollback': key}).decode()
        db.execute("""UPDATE authority SET kernel_boot_id=?,kernel_epoch=?,strategy_epoch=NULL,new_open_status='DISARMED',
                   grant_id=NULL,grant_policy_json=?,state_revision=?,risk_revision=risk_revision+1,
                   authority_revision=authority_revision+1,updated_event_seq=? WHERE singleton=1""",
                   (boot, epoch, policy, revision, seq))
        db.execute("""UPDATE strategy_instances SET status='REVOKED',state_revision=state_revision+1,updated_event_seq=?
                   WHERE status IN ('ACTIVE','DRAINING')""", (seq,))
