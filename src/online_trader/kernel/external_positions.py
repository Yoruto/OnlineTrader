"""Explicit acquisition of a native position into the original execution owner."""
import copy
from dataclasses import replace
from decimal import Decimal

from online_trader.contracts.codec import content_hash
from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_text, decimal_value
from online_trader.errors import TraderError
from online_trader.execution.close_routes import owned_lot_facts, replay_lots
from online_trader.gateway.tqkq import number
from online_trader.kernel.equal_lot_review import original_lots, reconcile_equal_lots
from online_trader.protection.reducer import ProtectionState, initial_state, reduce_event
from online_trader.strategy.plan import build_simulation_plan


def review_positions(owner, *, snapshot=None):
    port=owner.port;k=owner.kernel
    snapshot=port.snapshot() if snapshot is None else snapshot
    if not snapshot['complete']:return snapshot,[]
    owner._validate_snapshot(snapshot)
    rows=[]
    positions={p['contract_id']:p for p in snapshot['positions']}
    native_positions = None
    bucket_fields = {'long_today', 'long_historical', 'short_today', 'short_historical',
                     'long_reference_price', 'short_reference_price'}
    if any(not bucket_fields <= set(position) for position in snapshot['positions']):
        # Compatibility for offline fixtures and older adapters. Production Tq
        # snapshots carry these bucket facts in the captured account snapshot.
        native_positions = port.api.get_position(account=port.account)
    cycles_by_code={};orders_by_code={};trades_by_code={}
    order_ids=set()
    codes=set(positions)
    for cycle in k._committed.values():
        code=cycle['plan']['contract_id']
        cycles_by_code.setdefault(code,[]).append(cycle)
        if cycle['lifecycle']!='CLOSED':
            codes.add(code)
        order_ids.update(port.order_id(order['child_order_id']) for order in cycle['orders'])
    for rows_by_code, facts in ((orders_by_code,snapshot['orders']),(trades_by_code,snapshot['trades'])):
        for fact in facts:
            rows_by_code.setdefault(fact['contract_id'],[]).append(fact)
    for code in sorted(codes):
        position=positions.get(code,{'contract_id':code,'long':0,'short':0})
        cycles=cycles_by_code.get(code,[])
        active=[c for c in cycles if c['lifecycle']!='CLOSED']
        owned={side:sum(c['protection']['owned_qty'] for c in cycles if c['plan']['direction']==side.upper())
               for side in ('long','short')}
        difference={side:position[side]-owned[side] for side in owned}
        scoped=code in owner.grant.value['contracts']
        native = native_positions.get(code) if native_positions is not None else None
        orders=sorted(orders_by_code.get(code,[]),key=lambda o:o['order_id'])
        trades=sorted(trades_by_code.get(code,[]),key=lambda t:t['trade_id'])
        occupied=any(c.get('management_state') in {'TAKEN_OVER','TAKEOVER_PENDING'} or
            any(OrderState.from_dict(o).potential_remaining or o['send_state'] in {'UNKNOWN','SEND_POSSIBLE'}
                for o in c['orders']) for c in active)
        buckets = {}
        for side in owned:
            if bucket_fields <= set(position):
                buckets[side] = {'today': position[side + '_today'],
                                 'historical': position[side + '_historical'],
                                 'reference_price': position[side + '_reference_price']}
            else:
                buckets[side] = {'today': getattr(native, 'pos_' + side + '_today', 0 if not position[side] else None),
                                 'historical': getattr(native, 'pos_' + side + '_his', 0 if not position[side] else None),
                                 'reference_price': number(getattr(native, 'open_price_' + side, None))}
        all_sides_valid=all(
            type(b['today']) is int and type(b['historical']) is int and b['today']>=0 and b['historical']>=0
            and position[side]==b['today']+b['historical']
            for side,b in buckets.items())
        foreign=[o for o in orders if o['order_id'] not in order_ids]
        foreign_changed=any(_foreign_orders_changed(c,foreign,order_ids,snapshot['trading_day']) for c in active)
        visible={o['order_id'] for o in foreign}
        unresolved_foreign=any(o['contract_id']==code and o['order_id'] not in order_ids|visible and o['status']=='ALIVE'
            for c in active for o in _foreign_baseline(c)['orders'])
        adjustments=[];additions={};review_error=None
        if all_sides_valid:
            try:
                adjustments,additions=_changes(k,code,snapshot['trading_day'],cycles,buckets)
            except TraderError as error:review_error=error.code
        equal_turnover=foreign_changed and not adjustments and not additions and any(
                o['quantity']>o['remaining'] and any(o not in _foreign_baseline(c)['orders'] for c in active)
                for o in foreign)
        if equal_turnover:
            # Equal bucket counts do not prove which lots survived a close and
            # reopen on the same side. Preserve the unresolved ownership gate.
            review_error='E_OWNERSHIP'
        if not any(difference.values()) and not (adjustments or additions or foreign_changed or review_error):continue
        settled=(owner.grant.external_allocation_allowed and scoped and not occupied and not unresolved_foreign and port.fresh(code)
            and not any(o['status']=='ALIVE' for o in orders) and all_sides_valid
            and not k._pending and owner.grant.in_window(port.clock.now()))
        review_available=settled and equal_turnover
        lot_candidates=original_lots(owner,code,snapshot['trading_day'])[0] if review_available else {}
        for side,delta in difference.items():
            if not delta and side not in additions and not any(r['direction']==side.upper() for r in adjustments) and not ((foreign_changed or review_error) and side=='long'):
                continue
            quantity=position[side]
            evidence={'account_key':k.account_key,'kernel_boot_id':k.boot_id,'source_epoch':port.source_epoch,
                'trading_day':snapshot['trading_day'],'contract_id':code,'direction':side.upper(),
                'native_quantity':quantity,'owned_quantity':owned[side],'difference':str(delta),
                'today':None,'historical':None,'reference_price':None,'native_position':position,'native_buckets':buckets,
                'original_cycles':{c['position_cycle_id']:{'owned_quantity':c['protection']['owned_qty'],
                    'plan_hash':content_hash(c['plan']),'management_state':c.get('management_state')} for c in active},
                'reductions':adjustments,'additions':additions,'review_error':review_error,
                'original_lots':lot_candidates,'lot_review_available':review_available,
                'native_orders_hash':content_hash(orders),'native_trades_hash':content_hash(trades)}
            evidence.update(buckets.get(side,{}))
            rows.append(evidence|{'eligible':settled and review_error is None,'evidence_hash':content_hash(evidence)})
    return snapshot,rows


def _foreign_orders_changed(cycle,foreign,owned,day):
    baseline=_foreign_baseline(cycle)
    code=cycle['plan']['contract_id']
    before={o['order_id']:o for o in baseline['orders'] if o['contract_id']==code and o['order_id'] not in owned}
    after={o['order_id']:o for o in foreign}
    return any(before.get(oid)!=order for oid,order in after.items()) or any(
        oid not in after and not (baseline['trading_day']<day and order['status']=='FINISHED') for oid,order in before.items())


def _foreign_baseline(cycle):
    return cycle.get('reviewed_foreign_baseline') or cycle['online_entry_baseline']


def _changes(k,code,day,cycles,buckets):
    additions={};reductions=[]
    for side,native in buckets.items():
        direction=side.upper()
        fills,mapping=owned_lot_facts(k,code,direction,cycles)
        lots=replay_lots(code,direction,day,fills,require_allocations=True)
        by_bucket={'today':[],'historical':[]};total_by_cycle={}
        for lot in lots:
            if not lot['quantity']:continue
            bucket='today' if lot['day']==day and not lot['already_historical'] else 'historical'
            by_bucket[bucket].append(lot)
            cid=lot['owner'];total_by_cycle[cid]=total_by_cycle.get(cid,0)+lot['quantity']
        if any(total_by_cycle.get(cid,0)!=c['protection']['owned_qty'] for cid,c in mapping.items()):
            raise TraderError('E_FACT_BARRIER','Original ledger quantities differ from its allocated lots')
        surplus={'today':0,'historical':0};debits={}
        for bucket,items in by_bucket.items():
            balance=native[bucket]-sum(lot['quantity'] for lot in items)
            surplus[bucket]=max(0,balance)
            remaining=max(0,-balance)
            for lot in items:
                qty=min(remaining,lot['quantity'])
                if qty:
                    key=(lot['owner'],bucket);debits[key]=debits.get(key,0)+qty;remaining-=qty
        reductions.extend({'position_cycle_id':cid,'direction':direction,'bucket':bucket,'quantity':qty}
                          for (cid,bucket),qty in debits.items())
        qty=sum(surplus.values());basis=native['reference_price']
        if qty:
            if qty>k.simulation.grant.value['max_order_quantity'] or basis is None or Decimal(basis)<=0:
                raise TraderError('E_FACT_BARRIER','Surplus needs a valid broker reference and explicit size allowance')
            additions[side]=surplus|{'reference_price':basis,'native_quantity':qty}
    return reductions,additions


def adopt_position(owner,binding):
    k=owner.kernel;port=owner.port
    if not owner.grant.external_allocation_allowed or not owner.grant.in_window(port.clock.now()) or k._pending:
        raise TraderError('E_NOT_READY','Adjustment needs a current permitted scope and a settled owner')
    snapshot,rows=review_positions(owner)
    params=binding['body']['parameters'];command_id=binding['body']['command_id']
    row=next((r for r in rows if r['evidence_hash']==params['evidence_hash'] and
        (r['contract_id'],r['direction'])==(params['contract_id'],params['direction'])),None)
    lot_review=params.get('lot_review')
    if not row or not (row['lot_review_available'] if lot_review is not None else row['eligible']):
        raise TraderError('E_FACT_BARRIER','Native positions, orders or trades changed, or adoption is ineligible')
    code=row['contract_id'];day=row['trading_day']
    runtime=k._dsv_runtime;profile=runtime.profile;scheduler=k._scheduled_runtime
    moment=port.clock.now().isoformat().replace('+00:00','Z')
    window=port.calendar.query(code,moment)
    if not window['complete'] or not window['is_trading'] or window['trading_day']!=day or scheduler._fault:
        raise TraderError('E_FACT_BARRIER','Adjustment requires a proved active cabinet session')
    effective=owner.reconciliation_snapshot(snapshot)
    if lot_review is not None:
        reductions,additions=reconcile_equal_lots(owner,snapshot,row,lot_review)
        row=row|{'reductions':reductions,'additions':additions}
    cycles=[]
    for original in k._committed.values():
        cid=original['position_cycle_id']
        if cid not in row['original_cycles']:continue
        owner.verify_cycle_orders(original,original['orders'],effective)
        cycle=copy.deepcopy(original)
        cycle['reviewed_foreign_baseline']={'trading_day':day,'orders':[o for o in snapshot['orders'] if o['contract_id']==code]}
        cycle['external_review']={'evidence_hash':row['evidence_hash'],'command_id':command_id,
            'snapshot_id':snapshot['snapshot_id'],'source_epoch':snapshot['source_epoch'],'native_trades_hash':row['native_trades_hash']}
        for entry in row['reductions']:
            if entry['position_cycle_id']==cid:
                apply_external_reduction(owner, cycle, entry, command_id, day)
        cycles.append(cycle)
    for side,surplus in row['additions'].items():
        cycles.append(_build_cycle(owner,row|surplus|{'direction':side.upper()},snapshot,command_id,profile,scheduler))
    binding.update(initial_status='SUCCEEDED',reason='Reviewed native position allocations and original protection saved',
        evidence_ids=[c['entry_reconciliation_id'] for c in cycles])
    binding['evidence_ids']=[value for value in binding['evidence_ids'] if value]
    binding['metadata']['cycle_ids']=[c['position_cycle_id'] for c in cycles]
    k._armed=False;k.contexts.revoke_all();k._authority_revision+=1
    if row['original_cycles']:
        owner.days.invalidate()
        k._recovery_pending.update(c['position_cycle_id'] for c in cycles)
        k.recovery_required=True
    proof={'review':row,'snapshot_id':snapshot['snapshot_id'],'gateway_identity':snapshot['gateway_identity'],
        'orders':[o for o in snapshot['orders'] if o['contract_id']==code],
        'trades':[t for t in snapshot['trades'] if t['contract_id']==code]}
    if lot_review is not None:proof['lot_review']=lot_review
    k.stage_control_cycles(cycles,'TX_TAKEOVER','ONLINE_EXTERNAL_POSITION_ADOPTED',allocation_review=proof)


def apply_external_reduction(owner, cycle, entry, command_id, day):
    seq = owner.kernel._sequence + 1
    key = 'external-reduction-' + content_hash({'command_id': command_id, 'cycle': cycle['position_cycle_id'],
        'bucket': entry['bucket'], 'lot_key': entry.get('lot_key')})
    decision = reduce_event(cycle['plan'], ProtectionState.from_dict(cycle['protection']),
        {'type': 'EXTERNAL_ALLOCATION_REDUCTION', 'quantity': entry['quantity'], 'event_seq': seq, 'event_id': key},
        simulation_permit=owner.permit(cycle['plan']))
    cycle['protection'] = decision.state.to_dict()
    reduction = {'trade_key': key, 'trading_day': day, 'event_seq': str(seq),
                 'bucket': entry['bucket'], 'quantity': entry['quantity']}
    if 'lot_key' in entry:
        reduction['lot_key'] = entry['lot_key']
    cycle.setdefault('external_reductions', []).append(reduction)
    if not decision.state.owned_qty:
        cycle['lifecycle'] = 'FLAT_PENDING_CONFIRM'


def _build_cycle(owner,row,snapshot,command_id,profile,scheduler):
    k=owner.kernel;port=owner.port
    code=row['contract_id'];day=row['trading_day'];qty=row['native_quantity'];basis=row['reference_price']
    symbol=next(s for s in profile.to_record()['settings']['symbols'] if s['code']==code)
    candidate={'account_key':k.account_key,'contract_id':code,'trading_day':day,'direction':row['direction'],
        'profile_hash':profile.content_hash,'theoretical_price':basis,'limit_price':basis,
        'open_offset':symbol['open_offset'],'stop_distance':symbol['stop_offset'],'take_profit_distance':symbol['profit_offset']}
    identity=content_hash({'command_id':command_id,'direction':row['direction']})[:40];cid='adopted-cycle-'+identity
    plan=build_simulation_plan(profile,candidate,grant=owner.grant,account_key=k.account_key,trading_day=day,
        config_revision=str(k._config_revision),config_hash=k._configuration['payload_hash'],
        rule_set_id=k._configuration['rule_set_id'],quantity=qty,plan_id='adopted-plan-'+identity,position_cycle_id=cid)
    permit=owner.permit(plan);state=initial_state(plan,simulation_permit=permit)
    # Initialize the interpreter's quantity and price anchor from the broker
    # inventory. The persisted fact is an allocation, with no order or fill ID.
    state=replace(state,owned_qty=qty,entry_filled_qty=qty,entry_value=decimal_value(basis)*qty)
    rid='adopted-reconcile-'+identity
    state=reduce_event(plan,state,{'type':'ENTRY_TERMINAL_RECONCILED','owned_quantity':qty,
        'reconciliation_id':rid,'event_seq':k._sequence+1,'event_id':'adopted-evidence-'+identity},
        simulation_permit=permit).state
    instrument=port.instrument(code);moment=port.clock.now().isoformat().replace('+00:00','Z')
    allocation={key:row[key] for key in ('account_key','contract_id','direction','trading_day','source_epoch',
        'today','historical','native_quantity','reference_price','evidence_hash')}
    if 'lots' in row:allocation['lots']=row['lots']
    return {'position_cycle_id':cid,'plan':plan,'protection':state.to_dict(),'orders':[],
        'decision_key':'adopted-'+identity,'entry_origin':'EXTERNAL_ADOPTION','strategy_epoch':None,
        'risk_per_lot':decimal_text(decimal_value(symbol['stop_offset'])*decimal_value(instrument['multiplier'])),
        'lifecycle':'HOLDING','revision':1,'entry_reconciliation_id':rid,'actual_average_entry_price':basis,
        'last_quote':None,'online_entry_baseline':snapshot,'online_reconciliation':snapshot,'instrument':instrument,
        'financial_instrument':{'multiplier':instrument['multiplier'],'currency':'CNY','basis':'ORIGINAL_INSTRUMENT',
            'max_quote_age_ms':k.max_quote_age_ms},
        'schedule_binding':scheduler.binding_for(plan,entry_time_utc=moment),
        'external_adoption':allocation|{'observed_at_utc':moment,'allocation_event_seq':str(k._sequence+1)}}
