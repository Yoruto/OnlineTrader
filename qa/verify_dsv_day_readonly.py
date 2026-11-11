"""Compare native market inputs and shadow decisions with order APIs blocked."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from online_trader.contracts.codec import canonical_bytes
from online_trader.configuration.commands import parameter_diff
from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import DsvFactFeed
from online_trader.gateway.simnow import SimNowDsvPort, credentials
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.kernel.scheduler import ScheduledRuntime
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.auto_offsets import load_preparation
from online_trader.runtime.comparison import read_dsv_profile
from online_trader.runtime.dsv_simulation import load_inputs
from online_trader.runtime.instance import AccountLock
from online_trader.scheduling.reference import clock_observation, measure_reference
from online_trader.strategy.entry import DsvEntryStrategy


def main():
    mode = sys.argv[1:]
    if mode not in ([], ['--freshness-only'], ['--kq-freshness-only']):
        raise SystemExit('Unknown observation mode')
    freshness_only = bool(mode)
    base = ROOT / 'output/simnow/20261110-night-ready'
    packet = Path(json.loads((base/'current-comparison-packet.json').read_bytes())['packet'])
    out = ROOT/'output/simnow'/datetime.now(timezone.utc).strftime('day-readonly-%Y%m%dT%H%M%SZ')
    out.mkdir()
    def save(name, value):
        (out/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

    result = {'status': 'IN_PROGRESS', 'output': str(out), 'execution_owner_created': False}
    original_config = Path(r'C:\Users\xyc\Desktop\trading\config.json')
    cfg_bytes = original_config.read_bytes()
    source, profile, calendar = load_inputs(packet/'scope.json', packet/'dsv-profile.json', packet/'calendar.json')
    receipt, grant, effective = load_preparation(source, profile)
    differences = parameter_diff(effective, read_dsv_profile(original_config))
    result['saved_parameter_differences'] = differences
    if any(item['path'] != '$.schedule.enabled' for item in differences):
        raise RuntimeError('SAVED_STRATEGY_PARAMETER_MISMATCH')
    cfg = json.loads(cfg_bytes.decode('utf-8-sig'))
    if cfg['account']['mode'] != 'sim':
        raise RuntimeError('DSV_IS_NOT_CONFIGURED_FOR_SIMULATION')
    reference = base/'credential-reference.json'
    auth = credentials(reference)
    kq_user, kq_password = auth['kq_username'], auth['kq_password']
    if cfg['account']['username'] != kq_user:
        raise RuntimeError('DSV_KQ_IDENTITY_DIFFERS_FROM_SAVED_MARKET_IDENTITY')
    del cfg
    secret_values = tuple(auth.values())
    del auth

    class Redactor:
        def __init__(self, stream): self.stream = stream
        def write(self, value):
            for secret in secret_values:
                if secret: value = value.replace(secret, '[REDACTED]')
            return self.stream.write(value)
        def __getattr__(self, name): return getattr(self.stream, name)

    sys.stdout, sys.stderr = Redactor(sys.stdout), Redactor(sys.stderr)
    from tqsdk import TqApi, TqAuth, TqKq
    apis, closed = [], []
    blocked, aids = Counter(), Counter()
    original_init, original_send = TqApi.__init__, TqApi._send_pack
    def init(api, *args, **kwargs):
        apis.append(api)
        return original_init(api, *args, **kwargs)
    def deny(method):
        def reject(*args, **kwargs):
            blocked[method] += 1
            raise RuntimeError('ORDER_OPERATION_BLOCKED')
        return reject
    def send(api, pack):
        aid = pack.get('aid', 'UNKNOWN')
        if 'order' in aid.lower() and aid not in {'qry_order', 'req_qry_order'}:
            return deny(aid)()
        aids[aid] += 1
        return original_send(api, pack)
    TqApi.__init__, TqApi._send_pack = init, send
    TqApi.insert_order, TqApi.cancel_order = deny('insert_order'), deny('cancel_order')

    feeds = {}
    summaries = {}
    stage = 'CLOCK'
    try:
        clock = measure_reference()
        clock_check = clock_observation(clock)
        save('clock.json', clock_check)
        if not clock_check['system_aligned']:
            raise RuntimeError('CLOCK_NOT_ALIGNED')
        codes = grant.value['contracts']
        now_utc = clock.now().isoformat().replace('+00:00', 'Z')
        session = {code: calendar.query(code, now_utc) for code in codes}
        if not all(value['is_trading'] and value['trading_day'] == grant.value['trading_day'] for value in session.values()):
            raise RuntimeError('FIVE_CONTRACTS_NOT_IN_CURRENT_SESSION')
        result.update(profile_hash=effective.content_hash, cabinet_day=grant.value['trading_day'],
                      historical_trading_window_active=grant.in_window(clock.now()),
                      reference_minus_system_ms=clock_check['reference_minus_system_ms'])
        kq_value = grant.value
        for key in ('trading_days', 'resume_entries_on_day_change'): kq_value.pop(key)
        kq_value.update(schema_version='KQ_DSV_SIM/1', grant_id='day-observation-kq',
                        account_fingerprint=hashlib.sha256(kq_user.encode()).hexdigest()[:16],
                        authorization_reference='USER_CONTINUE_DAY_READONLY_NO_ORDERS')
        kq_grant = DsvSimulationGrant.parse(canonical_bytes(kq_value))
        bindings = [('DSV_TqKq', kq_grant)]
        if mode != ['--kq-freshness-only']:
            bindings.insert(0, ('SimNow', grant))
        for side, binding in bindings:
            stage = side
            lease = AccountLock(binding.account_key)
            first_api = len(apis)
            try:
                if side == 'SimNow':
                    port = SimNowDsvPort.connect(reference, binding, clock, read_only=True)
                else:
                    account = TqKq()
                    api = TqApi(account=account, auth=TqAuth(kq_user, kq_password), debug=False, web_gui=False, disable_print=True)
                    port = TqKqDsvPort(api, account, binding, clock)
                    port.read_only = True
                port.configure_calendar(calendar)
                deadline = time.monotonic()+35
                first = None
                while time.monotonic() < deadline:
                    port.poll_updates(.2)
                    native = port.snapshot()
                    if native['complete']:
                        if first is None: first = native
                        if all(port.api.is_serial_ready(serial) for serial in port.tick_serials.values()):
                            break
                else:
                    raise RuntimeError('NATIVE_SYNC_TIMEOUT')
                feed = DsvFactFeed(port, effective, calendar)
                stage = side+'_INITIAL_HISTORY'
                history_started = clock.now()
                batches = dict(feed.batches())
                after = port.snapshot()
                feeds[side] = batches
                now = clock.now()
                summaries[side] = {'account_snapshots_complete': first['complete'] and after['complete'],
                    'positions_orders_trades_unchanged': all(first[key]==after[key] for key in ('positions','orders','trades')),
                    'native_counts': {key: len(after[key]) for key in ('positions','orders','trades')},
                    'quotes': {code: {'sample_count': len(events), 'path_complete': events[-1]['path_complete'],
                        'age_at_history_start_ms': round((history_started-datetime.fromisoformat(events[-1]['source_time_utc'])).total_seconds()*1000),
                        'last_age_ms': round((now-datetime.fromisoformat(events[-1]['source_time_utc'])).total_seconds()*1000),
                        'last': events[-1]} for code, events in batches.items()}}
                if freshness_only:
                    stage = side+'_INCREMENTAL'
                    for events in batches.values():
                        feed.accept(events[-1])
                    samples = {}
                    update_deadline = time.monotonic()+10
                    while time.monotonic() < update_deadline:
                        port.poll_updates(.2)
                        for code, events in feed.batches():
                            event = events[-1]
                            feed.accept(event)
                            age = round((clock.now()-datetime.fromisoformat(event['source_time_utc'])).total_seconds()*1000)
                            samples[code] = {'source_time_utc':event['source_time_utc'], 'age_ms':age,
                                             'path_complete':event['path_complete']}
                        if all(code in samples and 0 <= samples[code]['age_ms'] <= 5000 and samples[code]['path_complete'] for code in codes):
                            break
                    summaries[side]['incremental_samples'] = samples
                save(side+'.json', summaries[side])
            finally:
                for api in reversed(apis[first_api:]):
                    try: api.close(); closed.append(True)
                    except Exception: closed.append(False)
                lease.close()

        stage = 'COMPARE'
        strategy = DsvEntryStrategy(effective)
        rows = []
        market_keys = ('last','bid1','ask1','day_high','day_low','tick_size')
        def replay(events):
            if not events: return {}
            first = events[0]
            state = strategy.initial_state(first['account_key'], first['contract_id'], first['trading_day'])
            results = {}
            for event in events:
                enriched = ScheduledRuntime._event(SimpleNamespace(calendar=calendar), event)
                decision = strategy.evaluate(state, enriched)
                state = decision['state']
                key = event['native_tick_id'], event['native_time_ns']
                results[key] = {'market': [event[k] for k in market_keys], 'reasons': decision['reasons'],
                    'trigger_prices': state['last_trigger_prices'], 'candidate_count': len(decision['candidates'])}
            return results

        for code in (() if freshness_only else codes):
            left, right = (replay(feeds[side].get(code, [])) for side in ('SimNow','DSV_TqKq'))
            overlap = left.keys() & right.keys()
            market_diff = sum(left[key]['market'] != right[key]['market'] for key in overlap)
            decision_diff = sum(left[key] != right[key] for key in overlap)
            fresh = all(0 <= summaries[side]['quotes'].get(code, {}).get('last_age_ms', 999999) <= 5000 for side in feeds)
            complete = all(summaries[side]['quotes'].get(code, {}).get('path_complete', False) for side in feeds)
            latest = max(overlap, key=lambda key: int(key[1])) if overlap else None
            rows.append({'contract_id':code, 'common_ticks':len(overlap), 'market_differences':market_diff,
                         'shadow_differences':decision_diff, 'fresh_on_both':fresh, 'path_complete_on_both':complete,
                         'latest_common_decision':None if latest is None else left[latest],
                         'latest_common_time_ns':None if latest is None else latest[1]})
        result['rows'] = rows
        result['status'] = ('MATCHED' if all(r['common_ticks'] and not r['market_differences'] and not r['shadow_differences']
                                           and r['fresh_on_both'] and r['path_complete_on_both'] for r in rows)
                            else 'PARTIAL')
        if freshness_only:
            result['incremental_samples'] = {side: value['incremental_samples'] for side, value in summaries.items()}
            result['status'] = 'FRESHNESS_CONFIRMED' if all(
                code in side and 0 <= side[code]['age_ms'] <= 5000 and side[code]['path_complete']
                for side in result['incremental_samples'].values() for code in codes) else 'PARTIAL'
    except Exception as error:
        result.update(status='INCOMPLETE', failed_stage=stage, error_code=getattr(error, 'code', type(error).__name__))
        if isinstance(error, TraderError):
            message = str(error)
            for secret in secret_values:
                if secret: message = message.replace(secret, '[REDACTED]')
            result['error_detail'] = message
    result.update(sdk_instances=len(apis), sdk_close_results=closed, blocked_order_attempts=dict(blocked),
                  allowed_api_packet_counts=dict(aids), dsv_configuration_unchanged=original_config.read_bytes()==cfg_bytes,
                  dsv_application_strategy_started=False, entry_authority='DISARMED')
    save('result.json', result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in {'MATCHED', 'FRESHNESS_CONFIRMED'} and all(closed) and not blocked else 2


if __name__ == '__main__':
    raise SystemExit(main())
