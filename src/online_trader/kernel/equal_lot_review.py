"""Compare reviewed surviving opening lots with the owner and current native trades."""
from decimal import Decimal, localcontext

from online_trader.contracts.codec import content_hash
from online_trader.domain.values import decimal_text, decimal_value, identifier, quantity
from online_trader.errors import TraderError
from online_trader.execution.close_routes import owned_lot_facts, replay_lots
from online_trader.gateway.native_days import native_day


def original_lots(owner, code, day):
    result={};used=set();k=owner.kernel
    for side in ('LONG','SHORT'):
        facts,_=owned_lot_facts(k,code,side,k._committed.values())
        used.update(f['trade_key'] for f in facts)
        for lot in replay_lots(code,side,day,facts,require_allocations=True):
            if lot['quantity']:
                bucket='today' if lot['day']==day and not lot['already_historical'] else 'historical'
                result[lot['lot_key']]={'position_cycle_id':lot['owner'],'direction':side,'bucket':bucket,
                    'quantity':lot['quantity'],'opening_day':lot['day']}
    return result,used


def reconcile_equal_lots(owner, snapshot, row, packet):
    k=owner.kernel;port=owner.port;code=row['contract_id'];day=row['trading_day']
    expected={'schema_version':'POSITION_LOT_REVIEW/1','account_key':k.account_key,'gateway_identity':port.gateway_identity,
              'calendar_hash':port.calendar.content_hash,'trading_day':day,'contract_id':code}
    if (type(packet) is not dict or packet.keys()!=expected.keys()|{'statement_reference','original_lots','replacement_lots'}
            or any(packet[key]!=value for key,value in expected.items())):
        raise TraderError('E_SCOPE','Lot evidence must bind the original account, calendar and cabinet day')
    identifier(packet['statement_reference'])
    originals,used=original_lots(owner,code,day)
    originals_input=_rows(packet['original_lots'],'lot_key')
    replacements=_rows(packet['replacement_lots'],'trade_id')
    if originals_input.keys()!=originals.keys() or not replacements:
        raise TraderError('E_FACT_BARRIER','Evidence must identify every original lot and each surviving replacement')
    reductions=[];removed={}
    for key,remain in originals_input.items():
        original=originals[key];qty=original['quantity']-remain
        if qty<0:raise TraderError('E_FACT_BARRIER','An original lot cannot grow through a position review')
        if qty:
            if not key.startswith(port.trade_prefix):
                raise TraderError('E_OWNERSHIP','The removed original batch needs its own native opening trade identity')
            bucket=(original['direction'],original['bucket']);removed[bucket]=removed.get(bucket,0)+qty
            reductions.append({name:original[name] for name in ('position_cycle_id','direction','bucket')}|
                              {'lot_key':key,'quantity':qty})

    owned={port.order_id(o['child_order_id']) for c in k._committed.values() for o in c['orders']}
    previously_reviewed={content_hash(o) for c in k._committed.values() if c['position_cycle_id'] in row['original_cycles']
        for o in (c.get('reviewed_foreign_baseline') or c['online_entry_baseline'])['orders'] if o['contract_id']==code}
    orders={o['order_id']:o for o in snapshot['orders'] if o['contract_id']==code and o['order_id'] not in owned
        and o['status']=='FINISHED' and o['quantity']>o['remaining'] and content_hash(o) not in previously_reviewed}
    foreign={};quantities={}
    for trade in snapshot['trades']:
        order=orders.get(trade['order_id'])
        if order is None:continue
        if any(trade[key]!=order[key] for key in ('direction','offset','contract_id')):
            raise TraderError('E_FACT_BARRIER','Foreign order and detailed trade disagree')
        if native_day(port.calendar,code,trade['source_time_ns'])['trading_day']!=day:continue
        if int(trade['source_time_ns'])>int(port.clock.now().timestamp()*1_000_000_000)+1_000_000_000:
            raise TraderError('E_FACT_BARRIER','Native replacement trade is ahead of the reference clock')
        foreign[trade['trade_id']]=trade
        oid=order['order_id'];quantities[oid]=quantities.get(oid,0)+quantity(trade['quantity'],positive=True)
    for oid,order in orders.items():
        if quantities.get(oid,0)!=order['quantity']-order['remaining']:
            raise TraderError('E_FACT_BARRIER','Replacement evidence requires complete terminal foreign trade details')
    additions={};added={}
    for tid,qty in replacements.items():
        trade=foreign.get(tid);key=identifier(port.trade_prefix+tid)
        if trade is None or key in used or trade['offset']!='OPEN' or not 0<qty<=trade['quantity']:
            raise TraderError('E_OWNERSHIP','Replacement must be a new surviving native opening batch')
        side='LONG' if trade['direction']=='BUY' else 'SHORT'
        bucket=(side,'today');added[bucket]=added.get(bucket,0)+qty
        additions.setdefault(side.lower(),[]).append({'lot_key':key,'bucket':'today','quantity':qty,'price':trade['price']})
    if not removed or added!=removed:
        raise TraderError('E_FACT_BARRIER','Removed and replacement lots must exactly balance by direction and cabinet bucket')
    close_capacity={}
    for trade in foreign.values():
        if trade['offset']=='OPEN':continue
        side='LONG' if trade['direction']=='SELL' else 'SHORT'
        # A combined CLOSE cannot prove the exact bucket while both buckets exist.
        closes_today=(trade['offset']=='CLOSETODAY' or code.split('.')[0] not in {'SHFE','INE'}
                      and trade['offset']=='CLOSE' and not row['native_buckets'][side.lower()]['historical'])
        if closes_today:close_capacity[side]=close_capacity.get(side,0)+trade['quantity']
    if any(qty>close_capacity.get(side,0) for (side,_),qty in removed.items()):
        raise TraderError('E_FACT_BARRIER','Native detailed closes do not cover the proposed original lots')
    result={}
    with localcontext() as context:
        context.prec=80
        for side,lots in additions.items():
            qty=sum(lot['quantity'] for lot in lots)
            if qty>owner.grant.value['max_order_quantity']:raise TraderError('E_RISK','Replacement exceeds scope quantity')
            basis=sum(decimal_value(lot['price'],positive=True)*lot['quantity'] for lot in lots)/qty
            result[side]={'lots':lots,'native_quantity':qty,'today':qty,'historical':0,'reference_price':decimal_text(basis)}
    return reductions,result


def _rows(rows,key):
    if type(rows) is not list or not 1<=len(rows)<=512:
        raise TraderError('E_SCHEMA','Expected bounded explicit lot rows')
    result={}
    for row in rows:
        if type(row) is not dict or row.keys()!={key,'remaining_quantity'}:
            raise TraderError('E_SCHEMA','Expected a batch identity and its reviewed surviving quantity')
        identity=identifier(row[key])
        if identity in result:raise TraderError('E_DUPLICATE_CONFLICT','Lot identity is repeated')
        result[identity]=quantity(row['remaining_quantity'])
    return result
