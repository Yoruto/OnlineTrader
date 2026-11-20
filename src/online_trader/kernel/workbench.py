"""Bounded read-only workbench view, assembled by the existing owner.

No SDK methods that wait for network data and no second execution state exist.
Business rows come exclusively from accepted commits at the stated watermark.
"""
import copy
from datetime import datetime, timezone
from decimal import Decimal
from heapq import nsmallest
import time

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_text
from online_trader.kernel.projection import projection_batch
from online_trader.kernel.workbench_contracts import capability_rows, config_snapshot
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import number
from online_trader.scheduling.clock import delta_ns, quote_timestamp, source_age_ns, utc_text
from online_trader.strategy.entry import CHINA_TIME


def workbench_positions(kernel, after):
    """Read a page of owned, reducible cycles at the owner's committed boundary."""
    page_size = 64
    candidates = (
        cycle_id for cycle_id, cycle in kernel._committed.items()
        if (after is None or cycle_id > after) and cycle['protection']['owned_qty']
        and cycle.get('management_state') not in {'TAKEOVER_PENDING', 'TAKEN_OVER'}
    )
    ids = nsmallest(page_size + 1, candidates)
    rows = []
    for cycle_id in ids[:page_size]:
        cycle = kernel._committed[cycle_id]
        rows.append({'position_cycle_id': cycle_id, 'contract_id': cycle['plan']['contract_id'],
                     'direction': cycle['plan']['direction'], 'remaining_quantity': cycle['protection']['owned_qty']})
    return {'account_key': kernel.account_key, 'kernel_boot_id': kernel.boot_id,
            'checkpoint_event_seq': str(kernel._durable_seq), 'positions': rows, 'has_more': len(ids) > page_size}


def monitor_symbols(kernel, decisions):
    """Small operator projection from cached quotes and committed execution state."""
    monitor = kernel._dsv_market_monitor
    runtime = kernel._dsv_runtime
    day = monitor.trading_day
    settings = runtime.profile.to_record()['settings']
    names = {row['code']: row['name'] for row in settings['symbols']}
    cycles = kernel._committed.values()
    counts = kernel.statistics.opening_counts(cycles, day)
    trade = settings['trade']
    maximum = trade['max_daily_orders']
    by_code = {}
    for cycle in cycles:
        if cycle['lifecycle'] != 'CLOSED' or cycle['plan']['trading_day'] == day:
            by_code.setdefault(cycle['plan']['contract_id'], []).append(cycle)
    rows = []
    for code in monitor.contracts:
        key = (code, day)
        stop = (kernel.statistics._committed.get(key) or {}).get('stop_result', {}).get('state', {})
        runtime_stop = runtime._stop_states.get(key, {}).get('state', {})
        facts = runtime._latest.get(key, {})
        event = facts.get('event', {})
        reasons = (decisions.get(code, {}).get('reasons', ()) if
                   event.get('source_epoch') == monitor.source_epoch and not kernel.port.fault else ())
        session = monitor.sessions.get(code, {})
        row = {'contract_id': code, 'name': names.get(code, code), 'state': 'ACTIVE',
               'status': None, 'real_margin_rate': monitor.margin_rates.get(code),
               'position_quantity': 0, 'net_loss_points': (stop or runtime_stop).get('net_loss_points', '0' if day else None),
               'opening_orders': counts.get(code, 0), 'main_contract': code}
        arrival = monitor.arrivals.get(code)
        if arrival is not None:
            row['quote_observed_at_utc'] = arrival['wall'].isoformat().replace('+00:00', 'Z')
        for cycle in by_code.get(code, ()):
            protection = cycle['protection']
            row['position_quantity'] += protection['owned_qty']
            exits = set(protection.get('exit_reasons', ()))
            force = exits & {'ADVERSE_SLIPPAGE', 'DATA_GAP_EXIT'}
            if force and cycle['lifecycle'] != 'CLOSED':
                row['state'] = 'PENDING'
                row['status'] = ('开仓滑点超限强平' if 'ADVERSE_SLIPPAGE' in force else '行情断流强平') + '（待确认成交）'
            if (row['status'] is None and cycle['plan']['trading_day'] == day
                    and protection.get('block_new_for_trading_day')):
                profit = cycle['plan']['profit_protection']
                if protection.get('protected_event_seq') and profit.get('block_new_for_trading_day'):
                    ratio = Decimal(profit['activation_ratio']) * 100
                    row['status'] = f'浮盈曾达止盈{decimal_text(ratio)}%'
                else:
                    row['status'] = '持仓保护已锁定当日停开'
        if row['state'] != 'PENDING':
            stop_reason = next((state.get('day_stopped_reason') or '当日已停止开仓'
                                for state in (stop, runtime_stop) if state.get('day_stopped')), None)
            if not stop_reason:
                if any(c == code and d <= day and saved['state'].get('month_stop') == day[:7]
                       for (c,d),saved in runtime._stop_states.items() if day is not None):
                    stop_reason = '本月连续停机停开'
                elif runtime._states.get(key, {}).get('first_minute_blocks'):
                    stop_reason = '开盘首分钟跳空'
                elif 'EXPIRY_ENTRY_BLOCK' in reasons:
                    stop_reason = '合约临近到期'
                elif row['opening_orders'] >= maximum:
                    stop_reason = f'报单达上限 {maximum}'
                elif monitor.closing_protection(code, trade['close_before_seconds']):
                    stop_reason = '本时段已收盘保护'
            if stop_reason or row['status']:
                row.update(state='STOPPED', status=stop_reason or row['status'])
            elif not kernel.control.symbol_entry_enabled(code, committed=True):
                row['status'] = '品种已手动停用'
            elif 'GEOMETRY_BLOCK' in reasons:
                row['status'] = '日内振幅超出开仓上限'
            elif 'OPENING_WAIT' in reasons:
                row['status'] = '开盘等待'
            elif 'CLOSE_SESSION_NO_OPEN' in reasons and session.get('is_trading'):
                row['status'] = '收盘前禁开'
            elif arrival is None:
                row['status'] = '等待行情'
            elif row['position_quantity']:
                row['status'] = '持仓中'
            else:
                row['status'] = '可开仓'
        rows.append(row)
    return rows


def workbench_monitor_view(kernel):
    kernel._check_owner()
    monitor = kernel._dsv_market_monitor
    return {'account_key': kernel.account_key, 'kernel_boot_id': kernel.boot_id,
            'checkpoint_event_seq': str(kernel._durable_seq), **monitor.latest,
            'symbols': kernel._dsv_monitor_rows, 'diagnostics': monitor.diagnostics(time.monotonic_ns())}


def workbench_view(kernel, *, snapshot=None):
    return _complete_workbench_view(*_capture_workbench_view(kernel, snapshot))


def _capture_workbench_view(kernel, snapshot, *, native_snapshot=None, native_fallback=True):
    kernel._check_owner()
    if snapshot is None:
        snapshot = kernel._make_snapshot()
    port=kernel.port
    quote_trace = getattr(kernel, '_dsv_quote_trace', None)
    cache_trace = {} if quote_trace is not None else None
    records=projection_batch(snapshot, include_orders=False)['records'];scheduler=getattr(kernel,'_scheduled_runtime',None)
    runtime=getattr(kernel,'_dsv_runtime',None)
    markets=[];decisions={}
    scoped=bool(getattr(kernel.simulation,'strategy_enabled',False))
    native=None;external_positions=[];native_cancel_review=None;native_error=None
    if scoped:
        from online_trader.kernel.external_positions import review_positions
        from online_trader.kernel.native_control import cancel_review
        try:
            if native_snapshot is None and not native_fallback:
                raise TraderError('E_ACCOUNT_SYNC', 'A recent native account snapshot is not available')
            native,external_positions=review_positions(kernel.simulation, snapshot=native_snapshot)
            if kernel.simulation.grant.manual_workbench:
                _,_,native_cancel_review=cancel_review(kernel.simulation, snapshot=native)
        except TraderError as error:
            native_error=error.code
    day=native['trading_day'] if native and native['complete'] else None
    symbols={row['code']:row for row in runtime.profile.to_record()['settings']['symbols']} if runtime else {}
    decision_states = {}
    for decision_key, value in (runtime._decision_views.items() if runtime else ()):
        decisions[decision_key] = value['report']
        decision_states[decision_key] = value['state']
    latest={}
    for (code,trading_day),row in sorted((runtime._latest if runtime else {}).items()):
        if day is None or trading_day==day:
            latest[code]=(row,False)
    if scheduler is not None:
        # Rebuilding an observation invalidates strategy send facts. The
        # scheduler still has the last live quote and its original age anchor.
        for code,row in scheduler._quotes.items():
            if day is None or row['event']['trading_day']==day:
                latest.setdefault(code,(row,True))
    for code,(row,from_scheduler) in sorted(latest.items()):
        event=row['event'];name=event['contract_id']
        symbol=symbols.get(name,{})
        received = row.get('received_ns')
        cache_age_ns = None if received is None else kernel._quote_age_ns(received)
        fresh = cache_age_ns is not None and cache_age_ns <= kernel.max_quote_age_ms * 1_000_000
        if from_scheduler:
            fresh=fresh and scheduler.scope_current(code,event['trading_day'],event['session_id'])
            if scoped:
                fresh=fresh and day is not None and not port.fault and event['source_epoch']==port.source_epoch
        if cache_trace is not None:
            cache_trace[code] = {'native_tick_id': event.get('native_tick_id'), 'source_epoch': event['source_epoch'],
                                 'cache_age_ns': str(cache_age_ns) if cache_age_ns is not None else None,
                                 'cache_origin': 'SCHEDULER' if from_scheduler else 'STRATEGY'}
        quote_details={}
        quote=getattr(port,'quotes',{}).get(name)
        if fresh and quote is not None:
            try:
                stamp=datetime.fromisoformat(event['source_time_utc'])
                quote_stamp=datetime.fromisoformat(quote.datetime)
                if quote_stamp.tzinfo is None:
                    quote_stamp=quote_stamp.replace(tzinfo=CHINA_TIME)
                if quote_stamp==stamp:
                    quote_details={key:number(getattr(quote,key,None)) for key in (
                        'open','pre_settlement','settlement','volume','open_interest',
                        'upper_limit','lower_limit','margin','commission','price_tick','bid_volume1','ask_volume1','volume_multiple')}
            except (TypeError,ValueError,AttributeError):
                pass
        decision_key = name, event['trading_day']
        decision_state = decision_states.get(decision_key, {})
        decision_report = decisions.get(decision_key, {})
        decision_details = {}
        if all(decision_state.get(state_key) == event[event_key] for state_key, event_key in (
                ('session_id', 'session_id'), ('source_epoch', 'source_epoch'), ('last_sequence', 'sequence'))):
            levels = {side: price for side, price in decision_state.get('last_trigger_prices', {}).items()
                      if side in {'LONG', 'SHORT'} and price is not None}
            if levels:
                decision_details['trigger_prices'] = levels
            decision_time = (decision_report.get('market_log') or {}).get('decision_time_utc')
            if decision_time:
                decision_details['decision_time_utc'] = datetime.fromisoformat(decision_time).astimezone(
                    timezone.utc).isoformat().replace('+00:00', 'Z')
        markets.append({key:event.get(key) for key in ('contract_id','trading_day','session_id','last','bid1','ask1','day_high','day_low','source_time_utc','path_complete')}
            |{'name':symbol.get('name',name),'fresh':fresh,'enabled':kernel.control.symbol_entry_enabled(name,committed=True),
              'entry_switch_revision':kernel.control.symbol_entries.get(name,{}).get('revision',0),
              'entry_reasons':copy.deepcopy(decisions.get((name,event['trading_day']),{}).get('reasons',[]))}
            |quote_details|decision_details)
    if scoped:
        displayed={row['contract_id'] for row in markets}
        for code in kernel.simulation.grant.value['contracts']:
            if code in displayed:continue
            markets.append(dict.fromkeys(('session_id','last','bid1','ask1','day_high','day_low','source_time_utc'))
                |{'contract_id':code,'name':symbols.get(code,{}).get('name',code),'path_complete':False,'fresh':False,
                  'trading_day':day,
                  'enabled':kernel.control.symbol_entry_enabled(code,committed=True),
                  'entry_switch_revision':kernel.control.symbol_entries.get(code,{}).get('revision',0),
                  'entry_reasons':['WAITING_FRESH_QUOTE']})
    if scoped and scheduler and scheduler._sample is not None:
        for row in markets:
            code = row['contract_id']
            at = scheduler.calendar.query(code, scheduler._sample.now_utc)
            stamp = row['source_time_utc']
            source = datetime.fromisoformat(stamp) if stamp else None
            raw = getattr(getattr(port, 'quotes', {}).get(code), 'datetime', None)
            invalid_stamp = False
            if raw:
                try:
                    source = quote_timestamp(raw, CHINA_TIME)
                except (ValueError, OverflowError):
                    invalid_stamp = True
            age = None
            source_session = None
            if source is not None and not invalid_stamp:
                age = (source_age_ns(scheduler._sample.utc, source, raw) if raw
                       else delta_ns(scheduler._sample.utc, source))
                source_session = scheduler.calendar.query(code, utc_text(source))['session']
            row['market_session_active'] = at['is_trading']
            if at['complete'] and not at['is_trading']:
                row['fresh'] = False
            row['source_time_raw'] = raw if isinstance(raw, str) and raw else stamp
            row['source_age_raw_ns'] = None if age is None else str(age)
            row['quote_time_reason'] = (
                'CALENDAR_COVERAGE_UNKNOWN' if not at['complete'] else
                'QUOTE_OUTSIDE_CURRENT_SESSION' if not at['is_trading'] else
                'QUOTE_TIMESTAMP_INVALID' if invalid_stamp else 'QUOTE_UNAVAILABLE' if age is None else
                'QUOTE_SESSION_MISMATCH' if source_session != at['session'] else
                'QUOTE_LEADS_LOCAL_UTC' if age < 0 else
                None if row['fresh'] else 'QUOTE_STALE')
    if quote_trace is not None:
        displayed_ns = time.monotonic_ns()
        for row in markets:
            code = row['contract_id']
            local_wait = quote_trace.market.current_wait_ns(code, displayed_ns)
            if local_wait is not None:
                row['local_market_wait_ns'] = str(local_wait)
            projection = {key: row.get(key) for key in ('fresh', 'session_id', 'market_session_active',
                          'source_time_utc', 'source_time_raw', 'source_age_raw_ns', 'quote_time_reason')}
            cache = cache_trace.get(code, {})
            quote_trace.record('WORKBENCH_QUOTE', contract_id=code,
                               state=(day, cache.get('source_epoch'), cache.get('native_tick_id'),
                                      projection['source_time_raw'], projection['fresh'],
                                      projection['market_session_active'], projection['quote_time_reason']),
                               trading_day=row['trading_day'], cache_limit_ms=kernel.max_quote_age_ms,
                               **projection, **cache)
    day_stops={}
    for cycle in snapshot['cycles']:
        plan=cycle['plan'];protection=cycle['protection']
        if not protection.get('block_new_for_trading_day'):
            continue
        reasons=protection.get('exit_reasons',[])
        if protection.get('protected_event_seq') and plan['profit_protection'].get('block_new_for_trading_day'):
            ratio=Decimal(plan['profit_protection']['activation_ratio'])*100
            reason=f'浮盈曾达止盈{decimal_text(ratio)}%'
        elif 'TRAILING_STOP' in reasons:
            reason='移动止盈后停开'
        elif 'DATA_GAP_EXIT' in reasons:
            reason='断流强平后停开'
        elif 'MANUAL_EXIT' in reasons:
            reason='手动平仓后停开'
        elif 'ADVERSE_SLIPPAGE' in reasons:
            reason='开仓滑点超限'
        else:
            reason='持仓保护已锁定当日停开'
        day_stops[(plan['contract_id'],plan['trading_day'])]=reason
    for market in markets:
        code=market['contract_id'];trading_day=market['trading_day']
        if runtime is None or trading_day is None:
            continue
        key=(code,trading_day)
        stats=kernel.statistics.snapshot(code,trading_day) or {}
        stop=stats.get('stop_result',{}).get('state',{})
        strategy_stop=runtime._stop_states.get(key,{}).get('state',{})
        stop_reason=next((state.get('day_stopped_reason') or '当日已停止开仓'
                          for state in (stop,strategy_stop) if state.get('day_stopped')),None)
        if stop_reason is None and any(c==code and d<=trading_day and
                result['state'].get('month_stop')==trading_day[:7]
                for (c,d),result in runtime._stop_states.items()):
            stop_reason='本月连续停机停开'
        if stop_reason is None and runtime._states.get(key,{}).get('first_minute_blocks'):
            stop_reason='开盘首分钟跳空'
        market['day_stop_reason']=stop_reason or day_stops.get(key)
        market['net_loss_points']=(stop or strategy_stop).get('net_loss_points')
    rollover_reader=getattr(port,'rollover_market_facts',None)
    if scoped and callable(rollover_reader):
        for market in markets:
            facts=rollover_reader(market['contract_id'],day)
            if facts:
                market['rollover']=facts
    market_by_code={item['contract_id']:item for item in markets}
    broker_orders=port.api.get_order(account=port.account) if day is not None else {}
    broker_positions=port.api.get_position(account=port.account) if day is not None else {}
    broker_trades={item['trade_id']:item for item in native['trades']} if day is not None else {}
    position_owners={}
    for cycle in snapshot['cycles']:
        plan=cycle['plan']
        if cycle['protection']['owned_qty']:
            key=(plan['contract_id'],plan['direction'])
            position_owners[key]=position_owners.get(key,0)+1
    trades=[];position_fees={}
    for fill in kernel.money.book.fills.values():
        fee=kernel.money.book.fees.get(fill['trade_key']) or fill.get('original_fee') or {}
        original = kernel._money_fact_meta.get(fill['trade_key'], fill)
        native_trade_id = original.get('native_trade_id')
        raw_id = port.native_trade_id(original) if day is not None and port.is_native_trade_key(fill['trade_key']) else None
        broker_trade = broker_trades.get(raw_id, {})
        confirmed_fee=fee.get('amount') if fee.get('quality')=='CONFIRMED' else None
        trades.append(copy.deepcopy(fill)|{'commission':confirmed_fee,'native_trade_id':broker_trade.get('trade_id', native_trade_id),
            'source_time_ns':broker_trade.get('source_time_ns')})
        position_fees.setdefault(fill['position_cycle_id'],[]).append(confirmed_fee)
    cycles=[];orders=[];exit_blocks=[]
    for cycle in snapshot['cycles']:
        p=cycle['plan'];s=cycle['protection'];cid=cycle['position_cycle_id']
        route_review={'status':'NO_OWNED_QUANTITY'}
        if s['owned_qty']:
            from online_trader.execution.close_routes import preview_owned_routes
            try:route_review=preview_owned_routes(kernel,cid,s['owned_qty'])
            except TraderError as error:route_review={'status':'BLOCKED','error_code':error.code,'execution_eligible':False}
        target=s.get('target_remaining_qty')
        exit_pending=(target is not None and target<s['owned_qty']) or any(
            order['effect']=='REDUCE' and (OrderState.from_dict(order).potential_remaining or
                order['send_state'] in {'UNKNOWN','SEND_POSSIBLE'}) for order in cycle['orders'])
        if target is not None and target<s['owned_qty'] and cycle.get('management_state')!='TAKEN_OVER':
            reductions=[order for order in cycle['orders'] if order['effect']=='REDUCE']
            reserved=sum(OrderState.from_dict(order).potential_remaining for order in reductions)
            execution=cycle.get('execution') or {}
            cancellations=execution.get('cancel_reasons',{})
            manual_children=execution.get('manual_reduction_children',{})
            manual=kernel.execution.manual_reduction(cycle)
            failed=any(order['terminal_confirmed'] and order['filled_qty']<order['original_qty'] and
                (order['broker_state']=='REJECTED' or (order['broker_state']=='CANCELLED' and not order['cancel_status']
                 and order['child_order_id'] not in cancellations)) for order in reductions
                if order['child_order_id'] not in manual_children or
                (manual and manual_children[order['child_order_id']]==manual['request_id']))
            if failed and s['owned_qty']-target>reserved:
                exit_blocks.append({'reason_code':'EXIT_REDUCTION_TERMINAL_UNFILLED','active':True,
                                    'contract_id':p['contract_id'],'position_cycle_id':cid})
        market=market_by_code.get(p['contract_id'],{})
        price=market.get('last') if market.get('fresh') else None
        average=cycle.get('actual_average_entry_price')
        sign=Decimal(1 if p['direction']=='LONG' else -1)
        base=Decimal(p['theoretical_entry_price'])
        take=Decimal(p['fixed_take_profit']['distance_points'])
        stop_move=-Decimal(p['fixed_stop_loss']['distance_points'])
        if s['mode']=='PROTECTED':
            stop_move=take*Decimal(p['profit_protection']['floor_ratio'])
        elif s.get('break_even_status')=='ACTIVE':
            stop_move=Decimal(0)
        if s['mode']=='NORMAL' and s.get('trailing_floor_move') is not None:
            stop_move=max(stop_move,Decimal(s['trailing_floor_move']))
        broker_position=broker_positions.get(p['contract_id'])
        margin=floating=None
        side=p['direction'].lower()
        if (s['owned_qty'] and broker_position is not None and
                position_owners[(p['contract_id'],p['direction'])]==1 and
                getattr(broker_position,'pos_'+side,None)==s['owned_qty']):
            margin=number(getattr(broker_position,'margin_'+side,None))
            other_side='short' if side=='long' else 'long'
            if margin is None and getattr(broker_position,'pos_'+other_side,None)==0:
                margin=number(getattr(broker_position,'margin',None))
            floating=number(getattr(broker_position,'float_profit_'+side,None))
        fees=position_fees.get(cid,[])
        cycles.append({'position_cycle_id':cid,'contract_id':p['contract_id'],'direction':p['direction'],
            'trading_day':p['trading_day'],'lifecycle':cycle['lifecycle'],'remaining_quantity':s['owned_qty'],
            'target_remaining_quantity':target,'mode':s['mode'],'quality':s['quality'],'exit_pending':bool(exit_pending),
            'entry_price':average,'stop_price':decimal_text(base+sign*stop_move),
            'take_profit_price':decimal_text(base+sign*take),'last_price':price,
            'price_difference':decimal_text(sign*(Decimal(price)-Decimal(average))) if price is not None and average is not None else None,
            'margin':margin,'floating_profit':floating,
            'commission':decimal_text(sum(Decimal(fee) for fee in fees)) if fees and all(fee is not None for fee in fees) else None,
            'phase1_status':s.get('phase1_status'),'phase2_status':s.get('phase2_status'),
            'phase1_target':s.get('phase1_target'),'phase2_target':s.get('phase2_target'),
            'initial_open_confirmation':copy.deepcopy(cycle.get('initial_open_confirmation')),
            'entry_tag':(cycle.get('strategy_metadata') or {}).get('candidate',{}).get('entry_kind'),
            'exit_reasons':copy.deepcopy(s.get('exit_reasons',[])),
            'exit_reason':(cycle.get('nominal_exit') or {}).get('exit_reason'),
            'exit_reference_price':(cycle.get('nominal_exit') or {}).get('nominal_exit_price'),
            'exit_wait_reason': (None if not exit_pending else 'EXIT_WAITING_FOR_SESSION'
                                 if market.get('market_session_active') is False else 'EXIT_WAITING_FOR_PRICE'
                                 if not market.get('fresh') or (
                                     p['contract_id'] in getattr(port, 'waiting_reduction_quotes', ())
                                     and not kernel.execution.manual_reduction(cycle)) else None),
            'break_even_status':s.get('break_even_status'),'max_favorable_move':s.get('max_favorable_move'),
            'management_state':cycle.get('management_state'),'plan_id':p['plan_id'],'plan_revision':p['plan_revision'],'close_route_review':route_review})
        for order in cycle['orders']:
            native_id=port.order_id(order['child_order_id']) if kernel.simulation else None
            broker_order=broker_orders.get(native_id)
            native_time=getattr(broker_order,'insert_date_time',None)
            orders.append({key:order.get(key) for key in ('child_order_id','intent_id','attempt_id','effect','original_qty','filled_qty',
                'limit_price','native_offset','native_trading_day','send_state','broker_state','cancel_status','terminal_confirmed','reported_fill_qty')}
                |{'native_order_id':native_id,'direction':p['direction'],
                  'source_time_ns':str(native_time) if type(native_time) is int and native_time>0 else None,
                  'native_message':str(getattr(broker_order,'last_msg',''))[:200] if broker_order is not None else None,
                  'order_source': ('MANUAL' if (order['effect'] == 'OPEN' and cycle.get('manual_limit_order')) or
                                   order['child_order_id'] in (cycle.get('execution') or {}).get('manual_reduction_children', {})
                                   else 'STRATEGY' if (cycle.get('strategy_metadata') or {}).get('candidate') else None)}
                |{'position_cycle_id':cid,'contract_id':p['contract_id'],'potential_remaining':OrderState.from_dict(order).potential_remaining})
    commands=[copy.deepcopy(s['receipt']) for s in kernel.control.states.values() if s.get('receipt')]
    policies=[]
    for row in kernel.ledger.query('SELECT policy_id,policy_kind,policy_json FROM policy_revisions'):
        try:kernel.control._policy(row['policy_id'],row['policy_kind'])
        except Exception:continue
        policies.append({'policy_id':row['policy_id'],'policy_kind':row['policy_kind']})
    result={'view_schema':'workbench/1','account_key':kernel.account_key,'kernel_boot_id':kernel.boot_id,
        'checkpoint_event_seq':snapshot['durable_seq'],'entry_authority':'ARMED' if snapshot['armed'] else 'DISARMED',
        'execution_scope':snapshot['execution_scope'],'configuration':copy.deepcopy(kernel._configuration)|{'config_revision':str(kernel._config_revision)},
        'connection':{'broker_connected':bool(getattr(port,'broker_connected',False)),
            'synchronized':bool(getattr(port,'complete',lambda:False)()),'fault':bool(getattr(port,'fault',False)),
            'recovery_required':snapshot['recovery_required'],'storage_failed':snapshot['storage_failed'],
            'clock_fault':copy.deepcopy(scheduler._fault) if scheduler else None,'pending_transactions':len(kernel._pending)},
        'markets':markets,'cycles':cycles,'orders':orders,'trades':copy.deepcopy(trades),
        'blocks':[r for r in records if r['record_type']=='BLOCK']+[{'reason_code':e,'active':True} for e in snapshot['errors'][-64:]]+exit_blocks,
        'commands':commands,'policies':policies,'truncated':False}
    result['symbol_entry_switches']=copy.deepcopy(kernel.control.symbol_entries)
    if kernel.control.entry_policies:
        result['entry_policies'] = copy.deepcopy(kernel.control.entry_policies)
        origin = 'MANUAL' if scoped and kernel.simulation.grant.manual_workbench else 'STRATEGY'
        for market in markets:
            keys = (origin + ':ACCOUNT', origin + ':' + market['contract_id'])
            if any(not result['entry_policies'].get(key, {}).get('enabled', True) for key in keys):
                market['enabled'] = False
    diagnostics = getattr(kernel, '_dsv_diagnostics', None)
    if diagnostics:
        result['configuration']['strategy_diagnostics'] = diagnostics.copy()
    if identity := getattr(kernel, '_dsv_runtime_identity', None):
        result['configuration']['runtime_identity'] = identity | {'feed_epoch': port.source_epoch}
        result['configuration']['clock_health'] = copy.deepcopy(scheduler.clock_health) if scheduler else None
    run_directory = getattr(kernel, '_dsv_run_directory', None)
    if run_directory is not None:
        result['configuration']['run_directory'] = run_directory
    if getattr(kernel, '_dsv_market_monitor', None) is not None:
        monitor = kernel._dsv_market_monitor
        result['configuration']['strategy_monitor'] = monitor.latest | {'symbols': kernel._dsv_monitor_rows}
        if getattr(kernel, '_dsv_monitor_error', None):
            result['configuration']['strategy_monitor_error'] = kernel._dsv_monitor_error
    equity=kernel.money.book.daily_equity
    if equity:
        days = sorted(equity)
        result['equity_history'] = [equity[day].copy() for day in days[-512:]]
        result['configuration']['equity_base'] = equity[days[0]].copy()
    if scoped:
        if runtime is not None:
            trade=runtime.profile.to_record()['settings']['trade']
            result['configuration']['strategy_profile_hash']=runtime.profile.content_hash
            result['configuration']['cancel_on_stop']=trade['cancel_on_stop']
            result['configuration']['manual_close_price_mode']=trade['close_price_mode']
            result['configuration']['stop_cancel_pending']=kernel.control.stop_cancel_pending()
            result['configuration']['strategy_stop_requested']=kernel.control.strategy_stop_requested()
            result['configuration']['strategy_stop_wait']=getattr(kernel, '_dsv_stop_wait', None)
            # The strategy worker initializes MARGIN_ALERT_THRESHOLD to 0.0.
            # The contract rules page still compares its separate reference rate.
            result['configuration']['margin_rate_alert_threshold']='0'
            if getattr(port,'started_at',None) is not None:
                try:result['configuration']['margin_alert_grace']=(port.clock.now()-port.started_at).total_seconds()<60
                except TraderError:pass
        result['configuration']['manual_workbench']=kernel.simulation.grant.manual_workbench
        contracts = kernel.simulation.grant.value['contracts']
        result['configuration']['scoped_contracts'] = contracts
        if runtime is not None:
            result['configuration']['live_parameter_save'] = not kernel.simulation.grant.manual_workbench
            fields = ('name', 'code', 'open_offset', 'stop_offset', 'profit_offset',
                      'order_volume', 'half_close_ratio', 'hysteresis_points')
            result['configuration']['strategy_symbols'] = [
                {key: symbol[key] for key in fields} for code, symbol in symbols.items() if code in contracts
            ]
        result['configuration']['gateway_identity']=port.gateway_identity
        result['configuration']['calendar_hash']=kernel.simulation.grant.value['calendar_hash']
        result['external_positions']=external_positions
        if native_cancel_review is not None:
            result['configuration']['native_cancel_review']=native_cancel_review
        if native_error is not None:
            result['blocks'].append({'reason_code':native_error,'active':True})
        if callable(getattr(port,'minute_chart_bars',None)):
            bars=port.minute_chart_bars()
            if bars:
                result['minute_bars']={code: rows[-48:] for code, rows in bars.items()}
                extension={code:rows for code, rows in bars.items() if len(rows)>48}
                if extension:result['configuration']['native_minute_bars']=extension
        if day is not None and not result['connection']['fault']:
            if scheduler is not None:
                schedules={code:scheduler.calendar.session_times(code,day) for code in result['configuration']['scoped_contracts']}
                result['configuration']['schedule_display']={'trading_day':day,
                    'intervals':{code:rows[:16] for code,rows in schedules.items()},
                    'truncated':any(len(rows)>16 for rows in schedules.values())}
            funds=native['funds']
            opening_orders=sum(kernel.statistics.opening_counts(snapshot['cycles'], day).values())
            result['account_metrics']={key:funds.get(key) for key in
                ('balance','available','margin','risk_ratio','pre_balance','close_profit','position_profit','float_profit','commission')}
            result['account_metrics'].update(trading_day=day,opening_orders=opening_orders)
    snapshot_contract = config_snapshot(kernel, runtime, scheduler, port, scoped)
    result['config_snapshot'] = snapshot_contract
    result['configuration']['config_snapshot_hash'] = content_hash(snapshot_contract)
    config_verified = snapshot_contract['integrity'] == 'VERIFIED'
    if not config_verified:
        result['blocks'].append({'reason_code': 'CONFIG_SNAPSHOT_INVALID', 'active': True})
    # Runtime-side presentation capabilities are explanatory facts, never
    # authorization tickets.  The command handler still performs the final
    # account, boot, revision, authority, market and risk checks for every
    # submission.
    projected_capabilities = capability_rows(result, external_positions, config_verified)
    result['capabilities'] = {
        'revision': result['checkpoint_event_seq'],
        'observed_at_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'operations': [
            {'operation': operation, 'scope_type': 'ACCOUNT', 'allowed': allowed, 'reason': reason}
            for operation, (allowed, reason) in projected_capabilities.items()
        ],
    }

    code_map = {
        'E_UNKNOWN_SEND': 'OT-TRD-2101',
        'RECONCILIATION_REQUIRED': 'OT-TRD-2104',
        'E_ACCOUNT_SYNC': 'OT-CON-1003',
        'E_STORAGE': 'OT-DB-5001',
        'LEDGER_WRITE_FAILED': 'OT-DB-5001',
        'CONFIG_SNAPSHOT_INVALID': 'OT-CFG-4001',
    }
    diagnostic_rows = []
    for block in result['blocks'][-64:]:
        reason = block.get('reason_code', 'UNKNOWN_RUNTIME_BLOCK')
        scope_identity = block.get('contract_id') or 'ACCOUNT'
        cycle_identity = block.get('position_cycle_id') or 'NONE'
        diagnostic_rows.append({
            'diagnostic_version': 1,
            'code': code_map.get(reason, 'OT-RSK-3001'),
            'reason': reason,
            'kind': 'BLOCK',
            'category': 'RUNTIME_BLOCK',
            'severity': 'WARNING',
            'source': 'RUNTIME',
            'operation': None,
            'command_id': None,
            # A continuing block needs a stable identity across publications;
            # checkpoint sequence is observation time, not event identity.
            'trace_id': f'block:{reason}:{scope_identity}:{cycle_identity}',
            'impact_scope': 'ACCOUNT',
            'recovery_state': 'ACTIVE',
            'retry_policy': 'RUNTIME_RECOVERY_ONLY',
            'occurred_at': None,
            'context': {key: value for key, value in block.items()
                        if key in {'contract_id', 'position_cycle_id', 'active'}},
        })
    for receipt in result['commands'][-64:]:
        if receipt.get('status') not in {'FAILED', 'REJECTED', 'NEEDS_RECONCILIATION'}:
            continue
        reason = receipt.get('error_code') or receipt.get('reason') or 'COMMAND_NOT_COMPLETED'
        diagnostic_rows.append({
            'diagnostic_version': 1,
            'code': code_map.get(reason, 'OT-TRD-2199'),
            'reason': reason,
            'kind': 'FAULT' if receipt.get('status') == 'FAILED' else 'BLOCK',
            'category': 'COMMAND_RESULT',
            'severity': 'ERROR' if receipt.get('status') == 'FAILED' else 'WARNING',
            'source': 'RUNTIME',
            'operation': receipt.get('operation'),
            'command_id': receipt.get('command_id'),
            'trace_id': receipt.get('payload_hash'),
            'impact_scope': 'COMMAND',
            'recovery_state': 'VERIFYING' if receipt.get('status') == 'NEEDS_RECONCILIATION' else 'ACTIVE',
            'retry_policy': ('QUERY_ORIGINAL_COMMAND' if reason == 'E_UNKNOWN_SEND'
                             else 'REQUIRE_USER_ACTION'),
            'occurred_at': receipt.get('updated_at_utc'),
            'context': {},
        })
    result['diagnostics'] = diagnostic_rows[-128:]

    catalog = set(result['configuration'].get('scoped_contracts', ()))
    rollover_catalog = getattr(port, '_rollover_catalog', {})
    for rows in rollover_catalog.values():
        catalog.update(rows)
    result['contract_catalog'] = {
        'revision': result['checkpoint_event_seq'],
        'observed_at_utc': result['capabilities']['observed_at_utc'],
        'complete': False,
        'source': 'RUNTIME_SCOPED_MARKET_CACHE',
        'contracts': sorted(catalog)[:4_096],
    }
    # Historical rows may be omitted. Current execution responsibility stays
    # complete, including active children of an otherwise closed cycle.
    current_cycles = {
        cycle['position_cycle_id'] for cycle in snapshot['cycles']
        if cycle['lifecycle'] != 'CLOSED' or cycle['protection']['owned_qty']
        or cycle.get('statistics_review_required') or cycle['protection'].get('reconciliation_required')
        or cycle.get('management_state') == 'TAKEOVER_PENDING'
        or any(OrderState.from_dict(order).potential_remaining or order.get('terminal_conflict')
               or order['send_state'] in {'UNKNOWN', 'SEND_POSSIBLE'}
               or (order['cancel_status'] in {'UNKNOWN', 'SEND_POSSIBLE', 'CALL_RETURNED'}
                   and (not order['terminal_confirmed'] or order['reported_fill_qty'] != order['filled_qty']))
               for order in cycle['orders'])
    }
    return result, records, snapshot['cycles'], current_cycles


def _complete_workbench_view(result, records, cycles, current_cycles):
    historical = {
        'cycles': [row for row in result['cycles'] if row['position_cycle_id'] not in current_cycles],
        'orders': [row for row in result['orders'] if row['position_cycle_id'] not in current_cycles],
        'trades': list(result['trades']),
        'commands': [row for row in result['commands'] if row['status'] in {'SUCCEEDED', 'REJECTED', 'FAILED', 'SUPERSEDED'}],
    }
    result['history_omitted'] = {key: 0 for key in historical}
    result['history_truncated'] = False

    def omit_history(key, count):
        omitted = historical[key][:count]
        historical[key] = historical[key][count:]
        identities = {id(row) for row in omitted}
        result[key] = [row for row in result[key] if id(row) not in identities]
        result['history_omitted'][key] += len(omitted)
        result['history_truncated'] = True

    for key, rows in historical.items():
        if len(rows) > 512:
            omit_history(key, len(rows) - 512)
    account_record = records[0]
    cycle_records = [row for row in records if row['record_type'] == 'CYCLE']
    while True:
        visible_cycles = {row['position_cycle_id'] for row in result['cycles']}
        base = {'records': [account_record, *(row for row in cycle_records if row['position_cycle_id'] in visible_cycles)]}
        result['status_projection'] = base
        # Include the duplicated base and leave room for both IPC envelopes.
        if len(canonical_bytes({'base': base, 'view': result})) <= 950_000:
            break
        if 'native_minute_bars' in result['configuration']:
            bars = result['configuration'].pop('native_minute_bars')
            result['history_omitted']['minute_bars'] = sum(len(rows) for rows in bars.values())
            result['history_truncated'] = True
            continue
        key = max(historical, key=lambda name: len(canonical_bytes(historical[name])))
        if not historical[key]:
            if result['truncated']:
                raise TraderError('E_WORKBENCH_CAPACITY', 'Owner control summary exceeds workbench capacity')
            result['truncated'] = True
            result['detail_unavailable'] = 'CURRENT_STATE_OVERSIZE'
            result['current_responsibility'] = {
                'cycles': len(current_cycles),
                'owned_quantity': sum(cycle['protection']['owned_qty'] for cycle in cycles),
                'active_orders': sum(bool(OrderState.from_dict(order).potential_remaining)
                                     for cycle in cycles for order in cycle['orders']),
            }
            result['detail_omitted'] = {name: len(result.get(name, [])) for name in (
                'cycles', 'orders', 'trades', 'markets', 'blocks', 'commands', 'external_positions')}
            for name in result['detail_omitted']:
                result[name] = []
            result['blocks'] = [{'reason_code': 'WORKBENCH_DETAIL_CAPACITY', 'active': True}]
            result['configuration'] = {name: value for name, value in result['configuration'].items() if name in {
                'rule_set_id', 'config_revision', 'payload_hash', 'runtime_identity', 'run_directory',
                'manual_workbench', 'scoped_contracts', 'cancel_on_stop', 'stop_cancel_pending',
                'strategy_stop_requested', 'strategy_stop_wait', 'manual_close_price_mode',
            }}
            result.pop('minute_bars', None)
            result.pop('equity_history', None)
            continue
        omit_history(key, max(1, len(historical[key]) // 2))
    return result


def workbench_publication(kernel):
    snapshot = kernel._make_snapshot()
    view = workbench_view(kernel, snapshot=snapshot)
    return view['status_projection'], view


def prepare_workbench_publication(kernel, *, native_snapshot=None):
    captured = _capture_workbench_view(kernel, kernel._make_snapshot(), native_snapshot=native_snapshot,
                                       native_fallback=False)

    def complete():
        view = _complete_workbench_view(*captured)
        return view['status_projection'], view

    return complete
