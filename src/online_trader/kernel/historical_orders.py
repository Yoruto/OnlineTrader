"""Reviewed terminal broker evidence for original children missing from native sync."""
from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.domain.values import decimal_value, identifier, quantity
from online_trader.errors import TraderError
from online_trader.gateway.native_days import native_day


class HistoricalOrders:
    def __init__(self, owner):
        self.owner=owner
        self.records={}
        for row in owner.kernel.ledger.query(
                "SELECT payload_json FROM event_journal WHERE event_type='ONLINE_REVIEWED_TERMINAL_HISTORY' ORDER BY event_seq"):
            data=strict_loads(row['payload_json'])['data']
            self.records.update({r['order']['order_id']:r for r in data['historical_orders']})

    def query_archive(self):
        owner=self.owner;k=owner.kernel;port=owner.port
        current=port.snapshot();owner._validate_snapshot(current)
        visible={row['order_id'] for row in current['orders']}|set(self.records)
        pending={port.order_id(o['child_order_id']):(c,o) for c in k._committed.values() for o in c['orders']
            if not o['terminal_confirmed'] and o['send_state']!='DEFINITELY_NOT_SENT'}
        records=[];seen=set();trade_ids=set()
        if not pending.keys()-visible:return records
        for row in k.ledger.query("SELECT event_seq,payload_json FROM event_journal "
                "WHERE event_type='ONLINE_ORDER_OBSERVATIONS' AND event_seq<=? ORDER BY event_seq DESC",(k._durable_seq,)):
            data=strict_loads(row['payload_json'])['data']
            if data['gateway_identity']!=port.gateway_identity:continue
            for item in data['orders']:
                oid=item['order_id'];proof=item.get('order_day_evidence')
                if oid not in pending or oid in visible|seen:continue
                seen.add(oid)
                if (item['status']!='FINISHED' or not proof
                        or any('native_day_error' in f for f in item['fills'])
                        or sum(f['quantity'] for f in item['fills'])!=item['reported_fill_qty']):
                    continue
                cycle,order=pending[oid]
                direction='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL'
                packet={'schema_version':'NATIVE_TERMINAL_HISTORY/1','account_key':k.account_key,
                    'gateway_identity':port.gateway_identity,'calendar_hash':port.calendar.content_hash,
                    'statement_reference':f"journal-observation-{row['event_seq']}",'orders':[{
                        'order_id':oid,'contract_id':item['contract_id'],'direction':direction,'offset':order['native_offset'],
                        'quantity':order['original_qty'],'remaining':item['remaining'],'status':'FINISHED',
                        'limit_price':order['limit_price'],'source_time_ns':proof['source_time_ns'],
                        'trades':[{key:f[key] for key in ('quantity','price','source_time_ns')}|
                            {'trade_id':f['trade_key'].removeprefix(port.trade_prefix)} for f in item['fills']]}]}
                verified=self.prepare(packet,source_kind='DURABLE_NATIVE_ORDER_OBSERVATION')
                for record in verified:
                    ids={t['trade_id'] for t in record['trades']}
                    if trade_ids&ids:raise TraderError('E_DUPLICATE_CONFLICT','An archived trade appears on multiple original orders')
                    trade_ids.update(ids)
                records.extend(verified)
                if len(records)>=64 or pending.keys()<=visible|seen:return records
        return records

    def prepare(self, packet, *, source_kind='OPERATOR_REVIEWED_TERMINAL_HISTORY'):
        owner=self.owner;k=owner.kernel;port=owner.port
        fields={'schema_version','account_key','gateway_identity','calendar_hash','statement_reference','orders'}
        if (type(packet) is not dict or packet.keys()!=fields or packet['schema_version']!='NATIVE_TERMINAL_HISTORY/1'
                or packet['account_key']!=k.account_key or packet['gateway_identity']!=port.gateway_identity
                or packet['calendar_hash']!=port.calendar.content_hash or type(packet['orders']) is not list
                or not 1<=len(packet['orders'])<=64):
            raise TraderError('E_SCOPE','Historical evidence must bind the original account, gateway and calendar')
        identifier(packet['statement_reference'])
        current=port.snapshot();owner._validate_snapshot(current)
        current_orders={r['order_id']:r for r in current['orders']}
        current_trades={r['trade_id']:r for r in current['trades']}
        committed={port.order_id(o['child_order_id']):(c,o) for c in k._committed.values() for o in c['orders']}
        seen_orders=set();seen_trades=set()
        historical_trades={t['trade_id']:t for record in self.records.values() for t in record['trades']}
        result=[]
        for row in packet['orders']:
            if type(row) is not dict or row.keys()!={'order_id','contract_id','direction','offset','quantity','remaining',
                    'status','limit_price','source_time_ns','trades'}:
                raise TraderError('E_SCHEMA','Expected one explicit historical order and its complete trade details')
            oid=identifier(row['order_id'])
            if oid in seen_orders or oid not in committed:
                raise TraderError('E_OWNERSHIP','History may identify each original committed order once')
            seen_orders.add(oid)
            cycle,child=committed[oid];plan=cycle['plan'];child_id=child['child_order_id']
            direction='BUY' if (plan['direction']=='LONG')==(child['effect']=='OPEN') else 'SELL'
            if (row['status']!='FINISHED' or row['contract_id']!=plan['contract_id'] or row['direction']!=direction
                    or row['offset']!=child['native_offset'] or row['limit_price']!=child['limit_price']
                    or quantity(row['quantity'],positive=True)!=child['original_qty'] or quantity(row['remaining'])>row['quantity']
                    or type(row['trades']) is not list or len(row['trades'])>512):
                raise TraderError('E_FACT_BARRIER','History differs from the original order or lacks terminal proof')
            order={key:value for key,value in row.items() if key not in {'source_time_ns','trades'}}
            known=self.records.get(oid)
            if current_orders.get(oid) is not None and current_orders[oid]!=order:
                raise TraderError('E_DUPLICATE_CONFLICT','Historical order conflicts with current native sync')
            if not known and (oid in current_orders or child['send_state']=='DEFINITELY_NOT_SENT'
                    or child['terminal_confirmed'] and child['send_state']=='CONFIRMED'):
                raise TraderError('E_FACT_BARRIER','History import is for missing unresolved original orders')
            observation={'child_order_id':child_id,'order_id':oid,'contract_id':row['contract_id'],'status':'FINISHED',
                'reported_fill_qty':row['quantity']-row['remaining'],'remaining':row['remaining'],'fills':[],
                'order_day_evidence':native_day(port.calendar,row['contract_id'],row['source_time_ns'])}
            owner.validate_native_order(observation)
            trades=[]
            for item in row['trades']:
                if type(item) is not dict or item.keys()!={'trade_id','quantity','price','source_time_ns'}:
                    raise TraderError('E_SCHEMA','Historical trade details require their original identity and timestamp')
                tid=identifier(item['trade_id']);trade_key=identifier(port.trade_prefix+tid)
                quantity(item['quantity'],positive=True);decimal_value(item['price'],positive=True)
                proof=native_day(port.calendar,row['contract_id'],item['source_time_ns'])
                fill={key:item[key] for key in ('quantity','price','source_time_ns')}|{'trade_key':trade_key,
                    'trading_day':proof['trading_day'],'native_day_evidence':proof}
                owner.fill_day(observation,fill)
                trade=item|{key:row[key] for key in ('order_id','contract_id','direction','offset')}
                if (tid in seen_trades or any(t is not None and t!=trade for t in
                        (historical_trades.get(tid),current_trades.get(tid)))
                        or int(item['source_time_ns'])<int(row['source_time_ns'])):
                    raise TraderError('E_DUPLICATE_CONFLICT','Historical trade identity, order or chronology conflicts')
                seen_trades.add(tid);trades.append(trade);observation['fills'].append(fill)
            observation['fills'].sort(key=lambda f:(int(f['source_time_ns']),f['trade_key']))
            trades.sort(key=lambda t:t['trade_id'])
            durable=k.ledger.query('SELECT trade_key,broker_order_key,quantity,price FROM fill_facts WHERE broker_order_key=?',(child_id,))
            expected={f['trade_key']:(f['quantity'],f['price']) for f in durable}
            actual={f['trade_key']:(f['quantity'],f['price']) for f in observation['fills']}
            if (any(actual.get(tid)!=value for tid,value in expected.items())
                    or sum(f['quantity'] for f in observation['fills'])!=observation['reported_fill_qty']):
                raise TraderError('E_FACT_BARRIER','Terminal volume must reconcile with every detailed trade and original fill')
            for f in observation['fills']:
                key=f['trade_key']
                if key in k._facts and key not in expected:
                    raise TraderError('E_DUPLICATE_CONFLICT','Trade is already allocated to another original order')
            owner._merge_observations([observation])
            material={'order':order,'trades':trades,'observation':observation}
            if known and any(known[key]!=value for key,value in material.items()):
                raise TraderError('E_DUPLICATE_CONFLICT','Accepted historical terminal evidence is immutable')
            if not known:result.append(material|{'statement_reference':packet['statement_reference'],
                'packet_hash':content_hash(packet),'observed_snapshot_hash':content_hash(current),'source_kind':source_kind})
        return result

    def observations(self, live):
        present={row['order_id'] for row in live}
        return live+[r['observation'] for oid,r in self.records.items() if oid not in present]

    def supplement(self, snapshot):
        native={row['order_id']:row for row in snapshot['orders']}
        trades={row['trade_id']:row for row in snapshot['trades']}
        sources={row['order_id']:row for row in snapshot.get('history_sources',[])}
        for oid,record in self.records.items():
            if oid in native and native[oid]!=record['order']:
                raise TraderError('E_DUPLICATE_CONFLICT','Live order conflicts with accepted historical evidence')
            native.setdefault(oid,record['order'])
            for trade in record['trades']:
                tid=trade['trade_id']
                if tid in trades and trades[tid]!=trade:
                    raise TraderError('E_DUPLICATE_CONFLICT','Live trade conflicts with accepted historical evidence')
                trades.setdefault(tid,trade)
            sources[oid]=({key:record[key] for key in ('statement_reference','packet_hash','observed_snapshot_hash')}|
                          {'order_id':oid,'kind':record.get('source_kind','OPERATOR_REVIEWED_TERMINAL_HISTORY')})
        return snapshot|{'orders':list(native.values()),'trades':list(trades.values()),'history_sources':list(sources.values())}
