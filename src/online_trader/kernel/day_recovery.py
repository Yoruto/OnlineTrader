"""Original-owner cabinet-day recovery and certified native terminal history.

Current SDK collections are never treated as complete historical queries.
Only quantity-consistent terminal orders from accepted reconciliations can be
carried forward when the current collection no longer contains them.
"""
import copy

from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError


class DayRecovery:
    def __init__(self,owner):
        self.owner=owner;self.kernel=owner.kernel;self.history={};self._history_seq=0
        self.ready_day=None;self.ready_epoch=None

    def invalidate(self):
        self.ready_day=None;self.ready_epoch=None

    def ready(self,day):
        return self.ready_day==day and self.ready_epoch==self.owner.port.source_epoch

    def _history(self):
        k=self.kernel
        for row in k.ledger.query("SELECT event_seq,payload_json FROM event_journal WHERE event_type='ONLINE_SIMULATION_RECONCILED' AND event_seq>? AND event_seq<=? ORDER BY event_seq",
                                  (self._history_seq,k._durable_seq)):
            data=strict_loads(row['payload_json']);data=data.get('data',data)
            cycle=data['cycle'];snapshot=cycle.get('online_reconciliation',{})
            if (not snapshot.get('complete') or snapshot.get('account_key')!=k.account_key
                    or snapshot.get('gateway_identity')!=self.owner.port.gateway_identity):continue
            native={item['order_id']:item for item in snapshot['orders']}
            inherited={item['order_id']:item for item in snapshot.get('history_sources',[])}
            for order in cycle['orders']:
                child=order['child_order_id'];oid=self.owner.port.order_id(child);item=native.get(oid)
                if (not order['terminal_confirmed'] or order['send_state']!='CONFIRMED'
                        or order.get('reported_fill_qty')!=order['filled_qty'] or item is None
                        or item['status']!='FINISHED' or item['quantity']-item['remaining']!=order['filled_qty']):continue
                trades=[t for t in snapshot['trades'] if t['order_id']==oid]
                if sum(t['quantity'] for t in trades)!=order['filled_qty']:continue
                source=inherited.get(oid) or {'order_id':oid,'event_seq':str(row['event_seq']),
                    'snapshot_hash':content_hash(snapshot),'trading_day':snapshot['trading_day']}
                self.history[child]={'order':copy.deepcopy(item),'trades':copy.deepcopy(trades),
                    'original_day':order.get('native_trading_day',cycle['plan']['trading_day']),
                    'source':source}
            self._history_seq=row['event_seq']

    def snapshot(self,current):
        self._history();result=copy.deepcopy(current)
        native={item['order_id']:item for item in result['orders']}
        trades={item['trade_id']:item for item in result['trades']};sources=[]
        for cycle in self.kernel._staged.values():
            if self.owner.archived_cycle(cycle):continue
            for order in cycle['orders']:
                child=order['child_order_id'];oid=self.owner.port.order_id(child)
                proof=self.history.get(child)
                original_day=order.get('native_trading_day',cycle['plan']['trading_day'])
                item=native.get(oid);observation=self.owner._observed_orders.get(child,{})
                if (original_day<current['trading_day'] and item is not None and item['status']=='FINISHED'
                        and order['terminal_confirmed'] and order['send_state']=='CONFIRMED'
                        and item['quantity']==order['original_qty'] and item['quantity']-item['remaining']==order['filled_qty']):
                    fills=observation.get('fills',[])
                    persisted=self.kernel.ledger.query('SELECT trade_key,quantity,price FROM fill_facts WHERE broker_order_key=?',(child,))
                    expected={f['trade_key']:(f['quantity'],f['price']) for f in persisted}
                    actual={f['trade_key']:(f['quantity'],f['price']) for f in fills}
                    direction='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL'
                    if (expected==actual and sum(f['quantity'] for f in fills)==order['filled_qty']
                            and item['contract_id']==cycle['plan']['contract_id'] and item['direction']==direction
                            and item['offset']==order['native_offset'] and item['limit_price']==order['limit_price']):
                        from online_trader.gateway.native_days import native_day
                        retained=[]
                        for f in fills:
                            day=native_day(self.owner.port.calendar,cycle['plan']['contract_id'],f['source_time_ns'])['trading_day']
                            trade={'trade_id':f['trade_key'][3:],'order_id':oid,'contract_id':cycle['plan']['contract_id'],
                                'direction':direction,'offset':item['offset'],'quantity':f['quantity'],
                                'price':f['price'],'source_time_ns':f['source_time_ns']}
                            # Missing current-day details are still a missing
                            # current native sync, not historical backfill.
                            if day>=current['trading_day'] and trades.get(trade['trade_id'])!=trade:
                                raise TraderError('E_FACT_BARRIER','Current-day native detailed fills are incomplete')
                            retained.append(trade)
                        proof={'order':copy.deepcopy(item),'trades':retained,'original_day':original_day,
                            'source':{'order_id':oid,'trading_day':current['trading_day'],
                                'event_seq':str(self.kernel._durable_seq),'snapshot_hash':content_hash(current),
                                'accepted_observation_hash':content_hash(observation),
                                'kind':'NATIVE_TERMINAL_AND_PERSISTED_ORIGINAL_FILL_RECEIPTS'}}
                if proof is None or proof['original_day']>=current['trading_day']:continue
                if (order['send_state']!='CONFIRMED' or not order['terminal_confirmed']
                        or order.get('reported_fill_qty')!=order['filled_qty']
                        or proof['order']['quantity']-proof['order']['remaining']!=order['filled_qty']):continue
                if oid in native and native[oid]!=proof['order']:
                    raise TraderError('E_DUPLICATE_CONFLICT','Current native order conflicts with its certified terminal history')
                native.setdefault(oid,copy.deepcopy(proof['order']))
                for trade in proof['trades']:
                    if trade['trade_id'] in trades and trades[trade['trade_id']]!=trade:
                        raise TraderError('E_DUPLICATE_CONFLICT','Current native trade conflicts with certified terminal history')
                    trades.setdefault(trade['trade_id'],copy.deepcopy(trade))
                sources.append(copy.deepcopy(proof['source']))
                self.owner._terminal.add((child,order['filled_qty']))
        result.update(orders=list(native.values()),trades=list(trades.values()))
        if sources:
            result.update(history_sources=sources,current_native_snapshot_hash=content_hash(current),
                          reconciliation_source='CURRENT_NATIVE_AND_OWNED_CERTIFIED_TERMINAL_HISTORY')
        return result

    def reconcile(self,day):
        k=self.kernel;o=self.owner;port=o.port
        if not o.grant.multi_day or not o.grant.permits_day(day):return False
        raw=port.snapshot();o._validate_snapshot(raw)
        if raw['trading_day']!=day:return False
        observations=o.observe_native_orders();o.recover_working(observations,reconcile_cycles=False)
        by_id={item['order_id']:item for item in raw['orders']}
        waiting=False
        for original in tuple(k._staged.values()):
            if o.archived_cycle(original):continue
            cycle=copy.deepcopy(k._staged[original['position_cycle_id']]);changed=False
            if cycle['plan']['trading_day']!=day and not cycle.get('cancel_entries'):
                if 'execution' in cycle:cycle['execution']['entry_stopped']=True
                cycle['cancel_entries']=True;changed=True
            cancel=[]
            for order in cycle['orders']:
                if not OrderState.from_dict(order).potential_remaining:continue
                if order.get('native_trading_day',cycle['plan']['trading_day'])==day:continue
                waiting=True;item=by_id.get(port.order_id(order['child_order_id']))
                # Missing original IDs remain occupied, including UNKNOWN.
                if item is None or item['status']!='ALIVE':continue
                if order.get('cancel_status'):continue
                if 'execution' in cycle:cycle['execution'].setdefault('cancel_reasons',{})[order['child_order_id']]='CABINET_DAY_ROLLOVER'
                changed=True;cancel.append(order['child_order_id'])
            if changed:
                cycle['revision']+=1
                k._stage_cycle(cycle,'TX_CANCEL','ONLINE_DSV_PRIOR_DAY_REMAINDERS',
                    after=lambda children=tuple(cancel):[k.request_cancel(child) for child in children])
                k.wait_idle()
        if waiting or k._has_unknown():return False
        positions={p['contract_id']:p for p in raw['positions']}
        # Foreign inventory on a different contract has no claim on the
        # original cycles. It stays visible and blocks openings on that code.
        managed={c['plan']['contract_id'] for c in k._staged.values() if c['lifecycle']!='CLOSED'}
        for code in managed:
            expected={side:sum(c['protection']['owned_qty'] for c in k._staged.values()
                if c['plan']['contract_id']==code and c['plan']['direction']==side.upper()) for side in ('long','short')}
            native=positions.get(code,{'long':0,'short':0})
            if any(native[side]!=expected[side] for side in expected):
                raise TraderError('E_OWNERSHIP','New-day selected positions do not exactly match original owned cycles')
        from online_trader.execution.close_routes import preview_owned_routes
        for cid in tuple(k._staged):
            cycle=k._staged[cid]
            if o.archived_cycle(cycle):continue
            if cycle['lifecycle']=='CLOSED' and cid not in k._recovery_pending:continue
            if cycle.get('management_state')=='TAKEN_OVER':
                raise TraderError('E_OWNERSHIP','A transferred cycle cannot be implicitly recovered on a new day')
            o.reconcile(cid,allow_working=True,clear_gateway_fault=False)
            if cycle['protection']['owned_qty']:
                preview_owned_routes(k,cid,cycle['protection']['owned_qty'],_recovery_review=True)
        if k._recovery_pending or k._has_unknown() or getattr(k._scheduled_runtime,'_fault',None):return False
        k.recovery_required=False
        o.capture('ONLINE_DSV_DAY_RECONCILED',{'trading_day':day,'source_epoch':port.source_epoch,
            'calendar_hash':port.calendar.content_hash,'grant_hash':o.grant.content_hash,
            'cycle_revisions':{cid:c['revision'] for cid,c in k._staged.items()},
            'current_native_snapshot_hash':content_hash(raw),'entry_authority':'DISARMED'})
        port.fault=False;o._fault_recorded=False
        self.ready_day=day;self.ready_epoch=port.source_epoch
        return True
