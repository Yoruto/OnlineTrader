"""Cross-day close allocation review from original fills and exact native lots.

This pure planner grants no send permission. Pending/UNKNOWN quantities remain
reserved by offset until matching terminal/detail evidence proves otherwise.
"""
from online_trader.contracts.codec import content_hash
from online_trader.domain.values import quantity
from online_trader.errors import TraderError
from online_trader.scheduling.calendar import _day


def replay_lots(contract_id,direction,current_day,fills,*,require_allocations):
    _day(current_day);lots=[];seen=set()
    if direction not in {'LONG','SHORT'}:raise TraderError('E_SCOPE','Expected an explicit owned side')
    if type(fills) is not list:raise TraderError('E_FACT_BARRIER','Explicit original position facts are required')
    separated=contract_id.split('.')[0] in {'SHFE','INE'}
    for fill in sorted(fills,key=lambda row:int(row['event_seq'])):
        if (fill['contract_id']!=contract_id or fill['direction']!=direction or fill['trade_key'] in seen):
            raise TraderError('E_OWNERSHIP','Fill scope or original identity differs')
        seen.add(fill['trade_key']);day=_day(fill['trading_day']);qty=quantity(fill['quantity'],positive=True)
        if day>current_day:raise TraderError('E_FACT_BARRIER','A fill belongs to a future cabinet day')
        owner=fill.get('position_cycle_id')
        if require_allocations and not isinstance(owner,str):
            raise TraderError('E_OWNERSHIP','Every original fill needs its cycle allocation')
        if fill['effect'] in {'OPEN','ADOPTED_TODAY','ADOPTED_HISTORICAL'}:
            lots.append({'day':day,'quantity':qty,'owner':owner,'lot_key':fill['trade_key'],
                'already_historical':fill['effect']=='ADOPTED_HISTORICAL'})
        elif fill['effect']=='EXTERNAL_REDUCE':
            remaining=qty;bucket=fill['bucket']
            if bucket not in {'today','historical'}:raise TraderError('E_FACT_BARRIER','External reduction bucket is invalid')
            for lot in lots:
                is_today=lot['day']==day and not lot['already_historical']
                if (lot['owner']==owner and lot['day']<=day and is_today==(bucket=='today')
                        and ('lot_key' not in fill or fill['lot_key']==lot['lot_key'])):
                    matched=min(remaining,lot['quantity']);remaining-=matched;lot['quantity']-=matched
            if remaining:raise TraderError('E_OWNERSHIP','Reviewed reduction exceeds its original cycle bucket')
        elif fill['effect']=='REDUCE':
            offset=fill.get('offset_kind')
            if separated and offset not in {'CLOSE_TODAY','CLOSE_YESTERDAY','CLOSETODAY','CLOSE'}:
                raise TraderError('E_FACT_BARRIER','Original reduction lacks its native offset')
            if not separated and offset!='CLOSE':
                raise TraderError('E_FACT_BARRIER','A combined-exchange close needs its original CLOSE offset')
            remaining=qty
            # Pinned SDK TargetPosTask._get_order documents today first on
            # combined exchanges. Native buckets and FIFO monetary cost are
            # separate ledgers; cost FIFO cannot prove a native close bucket.
            candidates=lots if separated else sorted(lots,key=lambda lot:lot['day']!=day or lot['already_historical'])
            for lot in candidates:
                eligible=(not require_allocations or lot['owner']==owner) and lot['day']<=day
                is_today=lot['day']==day and not lot['already_historical']
                eligible=eligible and (not separated or is_today==(offset in {'CLOSE_TODAY','CLOSETODAY'}))
                if eligible:
                    matched=min(remaining,lot['quantity']);remaining-=matched;lot['quantity']-=matched
            if remaining:raise TraderError('E_OWNERSHIP','Confirmed closes exceed the proved original lot bucket')
        else:raise TraderError('E_FACT_BARRIER','Unknown fill effect')
    return lots


def close_routes(*,contract_id,direction,current_day,fills,native,orders,target_quantity,position_cycle_id=None):
    """Allocate the remaining exit across proved lots and existing reservations."""
    quantity(target_quantity)
    separated=contract_id.split('.')[0] in {'SHFE','INE'}
    if type(orders) is not list:raise TraderError('E_FACT_BARRIER','Explicit original order facts are required')
    lots=replay_lots(contract_id,direction,current_day,fills,require_allocations=position_cycle_id is not None)
    today=sum(l['quantity'] for l in lots if l['day']==current_day and not l['already_historical'])
    historical=sum(l['quantity'] for l in lots)-today
    if (type(native) is not dict or set(native)!={'today','historical','total'} or
            any(type(v) is not int or v<0 for v in native.values()) or native!={'today':today,'historical':historical,'total':today+historical}):
        raise TraderError('E_OWNERSHIP','Native today/historical quantities do not exactly match original owned fills')
    selected=[l for l in lots if position_cycle_id is None or l['owner']==position_cycle_id]
    selected_today=sum(l['quantity'] for l in selected if l['day']==current_day and not l['already_historical'])
    selected_lots={'today':selected_today,'historical':sum(l['quantity'] for l in selected)-selected_today}
    if target_quantity>sum(selected_lots.values()):raise TraderError('E_RISK','Requested reduction exceeds the exact owned cycle quantity')
    reserved={'today':0,'historical':0,'combined':0}
    selected_reserved={'today':0,'historical':0,'combined':0}
    for order in orders:
        if order['contract_id']!=contract_id or order['direction']!=direction:raise TraderError('E_OWNERSHIP','Pending close belongs to another side')
        original=quantity(order['original_qty'],positive=True);filled=quantity(order['filled_qty'])
        if filled>original:raise TraderError('E_FACT_BARRIER','Order fill exceeds original quantity')
        terminal=order.get('terminal_confirmed') is True and order.get('reported_fill_qty')==filled and order.get('send_state') not in {'UNKNOWN','SEND_POSSIBLE'}
        remaining=0 if terminal or order.get('send_state')=='DEFINITELY_NOT_SENT' and filled==0 else original-filled
        if not remaining:continue
        if order.get('trading_day')!=current_day:
            raise TraderError('E_FACT_BARRIER','A pending close from another or unknown cabinet day needs native terminal reconciliation')
        offset=order.get('native_offset')
        bucket='combined' if not separated else 'today' if offset=='CLOSETODAY' else 'historical' if offset=='CLOSE' else None
        if bucket is None:raise TraderError('E_FACT_BARRIER','Pending close offset is unproved')
        reserved[bucket]+=remaining
        if position_cycle_id is not None and not isinstance(order.get('position_cycle_id'),str):
            raise TraderError('E_OWNERSHIP','Pending closes need their original cycle allocation')
        if position_cycle_id is None or order['position_cycle_id']==position_cycle_id:
            selected_reserved[bucket]+=remaining
    available={'today':today-reserved['today'],'historical':historical-reserved['historical']}
    if min(available.values())<0 or reserved['combined']>today+historical:raise TraderError('E_FACT_BARRIER','Close reservations exceed the proved native bucket')
    selected_available={key:selected_lots[key]-selected_reserved[key] for key in selected_lots}
    if min(selected_available.values())<0 or selected_reserved['combined']>sum(selected_lots.values()):
        raise TraderError('E_FACT_BARRIER','Close reservations exceed their own cycle bucket')
    uncovered=max(0,target_quantity-sum(selected_reserved.values()))
    desired=min(uncovered,
                max(0,today+historical-sum(reserved.values())))
    reasons=[]
    if target_quantity and not uncovered:reasons.append('TARGET_COVERED_BY_PENDING')
    if not separated and selected_lots['today']<today:
        # CLOSE cannot select historical lots while another cycle still owns
        # today's lots. Pending orders are not proof those lots have closed.
        safe=max(0,selected_lots['today']-selected_reserved['combined'])
        if desired>safe:reasons.append('COMBINED_CLOSE_WOULD_SPEND_OTHER_CYCLE_TODAY')
        desired=min(desired,safe)
    routes=[]
    if separated:
        # DSV does not override the pinned SDK's "今昨,开" offset priority.
        # Native execution order is separate from FIFO monetary cost.
        for bucket,offset in (('today','CLOSETODAY'),('historical','CLOSE')):
            qty=min(desired,available[bucket],selected_available[bucket]);desired-=qty
            if qty:routes.append({'native_offset':offset,'quantity':qty})
    elif desired:routes=[{'native_offset':'CLOSE','quantity':desired}]
    result={'contract_id':contract_id,'direction':direction,'current_day':current_day,'owned_today':today,'owned_historical':historical,
        'reserved':reserved,'routes':routes,'requested_reduction_quantity':target_quantity,'uncovered_target_quantity':uncovered,
        'routing_reasons':reasons,'execution_eligible':False,'source':'ORIGINAL_FILL_BUCKET_REVIEW'}
    if position_cycle_id is not None:
        result.update(position_cycle_id=position_cycle_id,cycle_owned=selected_lots,cycle_reserved=selected_reserved)
    return result|{'evidence_hash':content_hash(result)}


def owned_lot_facts(kernel,contract_id,direction,cycles):
    mapping={c['position_cycle_id']:c for c in cycles if c['plan']['contract_id']==contract_id and c['plan']['direction']==direction}
    facts=kernel.ledger.query('SELECT f.trade_key,f.contract_id,f.trading_day,f.offset_kind,f.recorded_event_seq AS event_seq,a.effect,a.quantity,a.position_cycle_id '
        'FROM fill_facts f JOIN fill_allocations a ON a.fill_id=f.fill_id WHERE f.contract_id=? AND f.recorded_event_seq<=? ORDER BY f.recorded_event_seq',
        (contract_id,kernel._durable_seq))
    fills=[f|{'direction':direction} for f in facts if f['position_cycle_id'] in mapping]
    for cid,owned_cycle in mapping.items():
        adoption=owned_cycle.get('external_adoption')
        if adoption:
            lots=adoption.get('lots',[{'lot_key':cid+'-'+bucket,'bucket':bucket,'quantity':adoption[bucket]}
                for bucket in ('today','historical') if adoption[bucket]])
            for lot in lots:
                fills.append({'contract_id':contract_id,'direction':direction,'trade_key':lot['lot_key'],
                    'position_cycle_id':cid,'trading_day':adoption['trading_day'],'event_seq':adoption['allocation_event_seq'],
                    'effect':'ADOPTED_'+lot['bucket'].upper(),'quantity':lot['quantity']})
        for adjustment in owned_cycle.get('external_reductions',[]):
            fills.append(adjustment|{'contract_id':contract_id,'direction':direction,'position_cycle_id':cid,'effect':'EXTERNAL_REDUCE'})
    return fills,mapping


def preview_owned_routes(kernel,cycle_id,target_quantity,*,_exclude_child=None,_recovery_review=False):
    """Review accepted original ledger lots against the current native buckets."""
    kernel._check_owner();cycle=kernel._committed[cycle_id];plan=cycle['plan'];port=kernel.port
    calendar=getattr(port,'calendar',None)
    if calendar is None:raise TraderError('E_CALENDAR_INCOMPLETE','A current bound calendar is required')
    window=calendar.query(plan['contract_id'],port.clock.now().isoformat().replace('+00:00','Z'))
    if not window['complete'] or not window['is_trading']:raise TraderError('E_CALENDAR_INCOMPLETE','Current native close buckets need a proved active trading day')
    if not port.complete() or (port.fault and not _recovery_review):raise TraderError('E_FACT_BARRIER','Current native position sync is incomplete')
    fills,mapping=owned_lot_facts(kernel,plan['contract_id'],plan['direction'],kernel._committed.values())
    side='long' if plan['direction']=='LONG' else 'short';native=port.api.get_position(plan['contract_id'],account=port.account)
    native={'today':getattr(native,'pos_'+side+'_today',None),'historical':getattr(native,'pos_'+side+'_his',None),'total':getattr(native,'pos_'+side,None)}
    # Reservations include prepared owner work as well as accepted work. Never
    # reuse a bucket while a sibling intent is waiting for its CommitAck.
    orders=[o|{'contract_id':plan['contract_id'],'direction':plan['direction'],
               'trading_day':o.get('native_trading_day',c['plan']['trading_day']),
               'position_cycle_id':c['position_cycle_id']}
        for c in kernel._staged.values() if c['position_cycle_id'] in mapping
        for o in c['orders'] if o['effect']=='REDUCE' and o['child_order_id']!=_exclude_child]
    return close_routes(contract_id=plan['contract_id'],direction=plan['direction'],current_day=window['trading_day'],
        fills=fills,native=native,orders=orders,target_quantity=target_quantity,position_cycle_id=cycle_id)
