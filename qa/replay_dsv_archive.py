"""Compare archived night-session facts and the bundled DSV price watcher offline."""

import argparse
import ast
import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from online_trader.configuration.dsv import DsvProfile
from online_trader.execution.policy import decide_order_action, select_price
from online_trader.protection import initial_state, reduce_event
from online_trader.strategy.entry import DsvEntryStrategy
from online_trader.strategy.plan import build_development_plan
from online_trader.strategy.risk import evaluate_entry


SDK_SOURCE_SHA256 = 'd88ed96d447dac01542f84acda4f1222f4a514a68863c9939deac3a325078beb'
PALM_TRIGGER_UTC = '2026-11-11T13:05:05.950000Z'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def market_rows(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            record = json.loads(line)
            if record['kind'] == 'PRODUCT_MARKET_DECISION':
                yield record['data']


def load_price_watcher(path):
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != SDK_SOURCE_SHA256:
        raise ValueError('Bundled DSV SDK source differs from the reviewed file')
    sdk_class = next(node for node in ast.parse(raw).body
                     if isinstance(node, ast.ClassDef) and node.name == 'InsertOrderUntilAllTradedTask')
    watcher = next(node for node in sdk_class.body
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == '_check_price')
    namespace = {'__builtins__': {}}
    exec(compile(ast.Module(body=[watcher], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[watcher.name]


async def compare_price_watcher(watch, prices):
    checked = 0
    for side, quotes in prices.items():
        for old, desired in zip(quotes, quotes[1:]):
            cancels = []
            api = SimpleNamespace(register_update_notify=lambda **_: nullcontext(),
                                  cancel_order=lambda order_id, **_: cancels.append(order_id))
            task = SimpleNamespace(_api=api, _direction=side, _account='offline', _get_price=lambda _: Decimal(desired))

            async def update():
                yield None

            await watch(task, update(), Decimal(old), 'offline-child')
            order = {'side':side,'effect':'OPEN','original_qty':5,'filled_qty':2,'limit_price':old,
                     'terminal_confirmed':False,'send_state':'CALL_RETURNED'}
            product = decide_order_action(order, desired, can_increase=True, remaining_quantity=3, other_pending_quantity=0)
            if product['action'] != ('CANCEL' if cancels else 'WAIT'):
                raise AssertionError((side, old, desired, cancels, product))
            checked += 1
    return checked


def replay_silver(profile, archive, events, log):
    code = 'SHFE.ag2702'
    trades = read_json(archive/'evidence/night-comparison.json')['kq-after']['by_contract'][code]['trades']
    opening = max((row for row in trades if row['offset']=='OPEN'),key=lambda row:row['trade_date_time'])
    closing = max(trades,key=lambda row:row['trade_date_time'])
    opened = datetime.fromisoformat(opening['time_beijing'])
    closed = datetime.fromisoformat(closing['time_beijing'])
    if closing['offset'] != 'CLOSETODAY' or closing['direction'] != 'BUY' or closing['volume'] != opening['volume']:
        raise AssertionError(closing)
    entry_line = next(line for line in log.splitlines()
                      if '[22:30:29]' in line and '[开short下单]' in line and code in line)
    theory = entry_line.split('理论开仓:',1)[1].split(' ',1)[0]
    settings = profile.to_record()['settings']
    symbol = next(row for row in settings['symbols'] if row['code']==code)
    candidate = {'account_key':'DEV:dsv-archive','contract_id':code,'direction':'SHORT',
                 'trading_day':'2026-11-12','profile_hash':profile.content_hash,
                 'theoretical_price':str(Decimal(theory).normalize()),'limit_price':str(opening['price']),
                 'open_offset':symbol['open_offset'],'stop_distance':symbol['stop_offset'],
                 'take_profit_distance':symbol['profit_offset']}
    plan = build_development_plan(profile,candidate,account_key=candidate['account_key'],
        trading_day=candidate['trading_day'],config_revision='1',config_hash=profile.content_hash,
        rule_set_id='dsv-archive',quantity=opening['volume'],plan_id='dsv-silver-plan',position_cycle_id='dsv-silver-cycle')
    state = reduce_event(plan,initial_state(plan),{'type':'FILL','quantity':opening['volume'],'price':str(opening['price'])}).state
    state = reduce_event(plan,state,{'type':'ENTRY_TERMINAL_RECONCILED','owned_quantity':opening['volume'],
                                    'reconciliation_id':'archived-terminal'}).state
    trade = settings['trade']
    entry, profit, stop = map(Decimal,(theory,symbol['profit_offset'],symbol['stop_offset']))
    activation = profit*Decimal(trade['protect_80_ratio'])
    floor = entry-profit*Decimal(trade['protect_keep_ratio'])
    protected_at = None
    compared = 0
    for data in market_rows(events):
        event = data['event']
        if event['contract_id'] != code:
            continue
        moment = datetime.fromisoformat(event['source_time_utc'])
        if not opened <= moment <= closed:
            continue
        last = Decimal(event['last'])
        if protected_at is None and entry-last >= activation:
            protected_at = moment
        expected_exit = ('TAKE_PROFIT' if entry-last >= profit else 'PROTECTED_FLOOR' if protected_at and last >= floor
                         else 'STOP_LOSS' if protected_at is None and last-entry >= stop else None)
        state = reduce_event(plan,state,{'type':'QUOTE','price':event['last'],'event_seq':compared+3}).state
        compared += 1
        if ((state.mode=='PROTECTED') != bool(protected_at) or state.block_new_for_trading_day != bool(protected_at)
                or state.target_remaining_qty != (0 if expected_exit else None) or state.owned_qty != opening['volume']):
            raise AssertionError((moment,expected_exit,state))
        if expected_exit:
            market = {'last':event['last'],'bid1':event['bid1'],'ask1':event['ask1'],'tick_size':event['tick_size'],
                      'lower_limit':data['instrument']['lower_limit'],'upper_limit':data['instrument']['upper_limit'],
                      'complete':True,'fresh':True,'is_trading':True,'quality':'CONTIGUOUS'}
            price = select_price('BUY',market)['price']
            if (expected_exit != 'PROTECTED_FLOOR' or expected_exit not in state.exit_reasons
                    or not 0 <= (closed-moment).total_seconds() < 1 or price != str(closing['price'])):
                raise AssertionError((moment,expected_exit,price,closing))
            state = reduce_event(plan,state,{'type':'EXIT_FILL','quantity':closing['volume']}).state
            if state.owned_qty or state.exit_filled_qty != closing['volume'] or not state.block_new_for_trading_day:
                raise AssertionError(state)
            return {'ticks':compared,'activation_utc':protected_at.isoformat(),'exit_signal_utc':moment.isoformat(),
                    'protection_line':str(floor),'close_limit':price,'confirmed_flat_quantity':state.owned_qty}
    raise AssertionError('No archived silver protection exit before the recorded fill')


def replay(archive, events, recovery_events, sdk):
    profile = DsvProfile.from_record(read_json(archive/'profile.json'))
    settings = profile.to_record()['settings']
    strategy = DsvEntryStrategy(profile)
    opening = datetime.fromisoformat('2026-11-11T13:00:00Z')
    cutoff = opening + timedelta(minutes=5)
    tested = 0
    prices = {'BUY':[], 'SELL':[]}
    palm = None
    for data in market_rows(events):
        event = data['event']
        moment = datetime.fromisoformat(event['source_time_utc'])
        if not opening <= moment <= cutoff + timedelta(seconds=6):
            continue
        # The recorder omits some observation-only rows. Compare each
        # saved tick's time gate independently of unrecorded path state.
        if not event['path_complete'] or event['quality'] != 'CONTIGUOUS':
            continue
        state = strategy.initial_state(event['account_key'],event['contract_id'],event['trading_day'])
        result = strategy.evaluate(state,event,decision_time=moment)
        if ('OPENING_WAIT' in result['reasons']) != (moment <= cutoff):
            raise AssertionError((event['contract_id'],event['source_time_utc'],result['reasons']))
        tested += 1
        if event['contract_id'] == 'DCE.p2701':
            prices['BUY'].append(event['ask1'])
            prices['SELL'].append(event['bid1'])
            if event['source_time_utc'] == PALM_TRIGGER_UTC:
                palm = (data, result)

    if palm is None:
        raise AssertionError('The archive lacks the recorded palm-oil entry')
    data, result = palm
    candidate, = result['candidates']
    log = (archive/'evidence/dsv-night.log').read_text(encoding='utf-8')
    expected_log = next(line for line in log.splitlines()
                        if '[21:05:05]' in line and '[开short下单]' in line and '(DCE.p2701)' in line)
    if candidate['direction'] != 'SHORT' or f"理论开仓:{candidate['theoretical_price']}.0" not in expected_log:
        raise AssertionError((candidate, expected_log))

    account = data['account']
    before = read_json(archive/'evidence/simnow-before.json')['last_snapshot']
    if (not before['synchronized'] or account['orders_today'] != 0
            or any((row['exchange_id'],row['instrument_id'])==('DCE','p2701') for row in before['nonzero_position_rows'])):
        raise AssertionError('The selected archived contract was not proved flat')
    args = dict(direction=candidate['direction'],contract_id=candidate['contract_id'],
                stop_distance=candidate['stop_distance'],limit_price=candidate['limit_price'],
                market_price=data['event']['last'],requested_qty=0,instrument=data['instrument'])
    raw = evaluate_entry(profile,account=account,**args)
    if raw['reasons'] != ['ACCOUNT_VALUES_UNKNOWN'] or account['symbol_margin'] is not None:
        raise AssertionError(raw)
    normalized = evaluate_entry(profile,account=account | {'symbol_margin':'0'},**args)
    # For this automatic, uncapped-instrument, non-aggressive sample, the
    # archived inputs leave the configured per-order cap as the binding limit.
    trade = settings['trade']
    equity = Decimal(account['equity'])
    cap = min(Decimal(trade['max_risk_ratio']),Decimal(trade['total_margin_cap']))
    used = sum(Decimal(account[key]) for key in ('margin','frozen_margin','pending_margin'))
    funds = min(Decimal(account['available']),equity*cap-used,equity*cap/len(settings['symbols']))
    margin_qty = int(funds/Decimal(data['instrument']['margin_per_lot']))
    risk_qty = int(equity*Decimal(trade['risk_per_trade_ratio']) /
                   (Decimal(candidate['stop_distance'])*Decimal(data['instrument']['multiplier'])))
    expected_qty = min(margin_qty,risk_qty,trade['max_volume'])
    if not normalized['allowed'] or normalized['quantity'] != expected_qty:
        raise AssertionError(normalized)
    return {'archive_opening_gate_ticks':tested,
            'bundled_sdk_price_updates':asyncio.run(compare_price_watcher(load_price_watcher(sdk),prices)),
            'palm_entry':{'time_utc':PALM_TRIGGER_UTC,'direction':'SHORT','theoretical_price':candidate['theoretical_price'],
                          'raw_risk_reasons':raw['reasons'],'same_account_quantity':expected_qty},
            'silver_protection':replay_silver(profile,archive,recovery_events,log),
            'archive_complete_native_path':False,'broker_connections':0,'native_orders':0,'native_cancellations':0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--events',type=Path,required=True)
    parser.add_argument('--recovery-events',type=Path,required=True)
    parser.add_argument('--dsv-sdk-source',type=Path,required=True)
    args = parser.parse_args()
    print(json.dumps(replay(args.archive,args.events,args.recovery_events,args.dsv_sdk_source),indent=2))


if __name__ == '__main__':
    main()
