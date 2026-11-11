"""Owner-side DSV execution driven only by explicit development facts."""

from concurrent.futures import Future
import copy
from decimal import Decimal, ROUND_HALF_EVEN
import time
from uuid import uuid4

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_text, identifier, quantity
from online_trader.errors import TraderError
from online_trader.execution.policy import select_price, close_price_context, decide_order_action
from online_trader.gateway.ports import UnavailablePort
from online_trader.protection import ProtectionState, reduce_event


def _id(kind):return kind+'-'+uuid4().hex


class ExecutionCoordinator:
    def __init__(self,kernel):self.kernel=kernel

    @staticmethod
    def active(cycle):return cycle['plan']['schema_version'] in ('1.2','1.3') and 'execution' in cycle

    @staticmethod
    def side(cycle,effect):
        return 'BUY' if (cycle['plan']['direction']=='LONG')==(effect=='OPEN') else 'SELL'

    def tick(self,cid,event,*,_observe_quote=True,_market_received_ns=None):
        k=self.kernel;k._writable()
        if (not k.development and not getattr(k.simulation,'strategy_enabled',False)) or isinstance(k.port,UnavailablePort):
            raise TraderError('E_UNSUPPORTED','Execution facts require an isolated development port')
        if k._pending:raise TraderError('E_FACT_BARRIER','Accept preceding owner transactions before advancing execution')
        if type(event) is not dict:raise TraderError('E_EXECUTION_INPUT','Expected an explicit execution fact')
        canonical_bytes(event);identifier(event.get('event_id'))
        for name in ('source_epoch','sequence'):quantity(event.get(name),positive=True)
        quantity(event.get('elapsed_ms'))
        cycle=copy.deepcopy(k._staged[cid])
        identity=event.get('market_identity')
        if identity is not None:
            previous=cycle.get('last_protection_market_identity')
            same=previous and all(previous.get(key)==identity.get(key) for key in ('contract_id','native_time_ns','native_tick_id'))
            if same:
                if previous.get('quote_hash')!=identity.get('quote_hash'):
                    raise TraderError('E_DUPLICATE_CONFLICT','An original native protection quote changed content')
                _observe_quote=False
            elif previous and int(identity['native_time_ns'])<int(previous['native_time_ns']):
                raise TraderError('E_FACT_BARRIER','Protection cannot consume an older native source quote')
            if _observe_quote:cycle['last_protection_market_identity']=copy.deepcopy(identity)
        if cycle['plan']['schema_version'] not in ('1.2','1.3'):raise TraderError('E_UNSUPPORTED','Original plans retain their original execution policy')
        if (event.get('trading_day')!=cycle['plan']['trading_day'] and not
                (getattr(k.simulation,'strategy_enabled',False) and k.simulation.exit_day_ready(cycle,event.get('trading_day')))):
            raise TraderError('E_SCOPE','Execution fact belongs to another trading day')
        for name,value in (('account_key',k.account_key),('position_cycle_id',cid),('contract_id',cycle['plan']['contract_id'])):
            if name in event and event[name]!=value:raise TraderError('E_SCOPE','Execution fact belongs to another owner scope')
        if type(event.get('market')) is not dict:raise TraderError('E_EXECUTION_INPUT','Execution requires explicit market facts')
        frozen_tick=(cycle.get('instrument') or {}).get('tick_size',k.tick_size)
        if event['market'].get('tick_size')!=frozen_tick:
            raise TraderError('E_SCOPE','Execution tick size differs from the original instrument')
        for name in ('close_due','force_close'):
            if name in event and type(event[name]) is not bool:raise TraderError('E_EXECUTION_INPUT','Expected an explicit execution switch')
        k.statistics.mark_observation(cycle,event.get('observed_at_utc'))
        planned_close=event.get('close_start_elapsed_ms')
        if planned_close is not None:
            quantity(planned_close)
            if planned_close>event['elapsed_ms']:raise TraderError('E_FACT_BARRIER','A close start cannot lie after its clock evidence')
        old=cycle.get('execution');digest=content_hash(event)
        if old:
            if event['source_epoch']<old['source_epoch'] or (event['source_epoch']==old['source_epoch'] and event['sequence']<old['sequence']):
                raise TraderError('E_REVISION_CONFLICT','Execution source facts cannot move backwards')
            if event['source_epoch']==old['source_epoch'] and event['sequence']==old['sequence']:
                if digest!=old['event_hash']:raise TraderError('E_DUPLICATE_CONFLICT','Execution source identity changed content')
                f=Future();f.set_result({'status':'DUPLICATE_EXECUTION_EVENT'});return f
            if event['event_id']==old['event_id']:raise TraderError('E_DUPLICATE_CONFLICT','Execution fact identity was reused')
            if event['elapsed_ms']<old['elapsed_ms']:raise TraderError('E_REVISION_CONFLICT','Execution clock cannot move backwards')
        ex=old or {'contexts':{},'context_ms':{},'cancel_reasons':{},'entry_stopped':False,
                   'close_start_ms':None,'force_start_ms':None,'last_action':'WAIT'}
        ex.update(event_id=event['event_id'],source_epoch=event['source_epoch'],sequence=event['sequence'],
                  event_hash=digest,elapsed_ms=event['elapsed_ms'],market=copy.deepcopy(event['market']))
        ex['context_id']=event.get('context_id');ex['peer_id']=event.get('peer_id','qa')
        cycle['execution']=ex
        # Validate quote grammar before freezing a clock-driven exit reference.
        priced=select_price(self.side(cycle,'REDUCE'),event['market'])
        market=event['market']
        if event.get('close_due'):
            started=event['elapsed_ms'] if planned_close is None else planned_close
            if ex['close_start_ms'] is None:ex['close_start_ms']=started
            else:ex['close_start_ms']=min(ex['close_start_ms'],started)
            state=cycle['protection'];state['target_remaining_qty']=0;state['block_new']=True
            state['state_revision']=str(int(state['state_revision'])+1)
            cycle['cancel_entries']=True;cycle['reasons']=sorted(set(cycle.get('reasons',[]))|{'SESSION_CLOSE'})
            k.statistics.freeze_exit(cycle,'SESSION_CLOSE',reference_price=market.get('last'))
        if event.get('force_close'):
            if ex['force_start_ms'] is None:ex['force_start_ms']=event['elapsed_ms']
            state=cycle['protection'];state['target_remaining_qty']=0;state['block_new']=True
            cycle['cancel_entries']=True
            k.statistics.freeze_exit(cycle,'MANUAL_EXIT',reference_price=market.get('last'))
        if priced['allowed']:
            if _observe_quote and cycle['protection']['owned_qty']:
                d=reduce_event(cycle['plan'],ProtectionState.from_dict(cycle['protection']),
                    {'type':'QUOTE','price':market.get('last'),'bid1':market.get('bid1'),'ask1':market.get('ask1'),
                     'event_seq':k._sequence+1,'event_id':_id('execution-quote')},
                    simulation_permit=k.simulation.permit(cycle['plan']) if k.simulation else None)
                k._apply_decision(cycle,d)
            received=time.monotonic_ns() if _market_received_ns is None else _market_received_ns
            if type(received) is not int or not 0<=received<=time.monotonic_ns():
                raise TraderError('E_FACT_BARRIER','The original quote arrival is not a valid monotonic fact')
            cycle['last_quote']={'price':market.get('last'),'bid1':market.get('bid1'),'ask1':market.get('ask1'),
                                 'kernel_boot_id':k.boot_id,'received_ns':str(received)}
            from online_trader.accounting.book import utc_now
            if _observe_quote:
                cycle['money_quote']={'price':market.get('last'),
                    'observed_at_utc':event.get('observed_at_utc') or cycle.get('statistics_observed_at_utc') or utc_now(),
                    'trading_day':event['trading_day'],'received_ns':str(received)}
        else:cycle['last_quote']=None
        self._contexts(cycle)
        cycle['revision']+=1
        return k._stage_cycle(cycle,'TX_PROTECT','EXECUTION_OBSERVATION',after=lambda:self.progress(cid,allow_open=True,allow_retry=True))

    def _contexts(self,cycle):
        ex=cycle['execution'];p=cycle['plan']['execution_policy'];elapsed=ex['elapsed_ms']
        for effect in ('OPEN','REDUCE'):
            side=self.side(cycle,effect);mode=None;row={}
            if effect=='OPEN':
                market=ex['market'];since=market.get('since_open_ms')
                if type(since) is not int:continue
                saved=ex['contexts'].get(effect,{})
                near=(cycle.get('strategy_metadata') or {}).get('candidate',{}).get('entry_near_open',market.get('near_open') is True)
                near=near or saved.get('mode')=='ACTIVE'
                confirmed_passive=saved.get('mode')=='PASSIVE' and cycle['protection']['entry_filled_qty']>0
                passive=p['passive_entry_first'] and not near and (elapsed<p['passive_wait_ms'] or confirmed_passive)
                mode='ACTIVE' if near else ('PASSIVE' if passive else 'CAP')
                if ex['contexts'].get(effect,{}).get('mode')==mode:continue
                row={'side':side,'age_ms':0}
                if passive:row['level']=cycle['plan']['theoretical_entry_price']
                else:
                    ratio=p['chase_slip_ratio'] if 0<=since<=p['chase_window_ms'] else p['entry_slip_ratio']
                    cap=max(Decimal(1),(Decimal(p['open_offset'])*Decimal(ratio)).to_integral_value(rounding=ROUND_HALF_EVEN))
                    anchor=Decimal(cycle['plan']['theoretical_entry_price'])
                    row['cap']=decimal_text(anchor+(cap if side=='BUY' else -cap))
            elif ex['force_start_ms'] is not None:
                previous=ex['context_ms'].get(effect)
                if previous is not None and elapsed-previous<p['force_retry_ms']:continue
                mode='MARKET';row={'side':side,'age_ms':0}
            elif ex['close_start_ms'] is not None:
                previous=ex['context_ms'].get(effect)
                step=close_price_context(side,elapsed_close_ms=elapsed-ex['close_start_ms'],
                    advance_seconds=cycle['plan']['close_session']['advance_seconds'],
                    last_context_age_ms=None if previous is None else elapsed-previous)
                if not step['refresh']:continue
                mode=step['mode'];row=step['contexts'].get(mode,{'side':side,'age_ms':0})
            else:mode='ACTIVE';row={'side':side,'age_ms':0}
            born_ms=elapsed
            if effect=='OPEN' and (mode!='CAP' or not p['passive_entry_first']):born_ms=0
            ex['contexts'][effect]={'mode':mode,'row':row};ex['context_ms'][effect]=born_ms

    def price(self,cycle,effect):
        ex=cycle['execution'];market=copy.deepcopy(ex['market']);quote=cycle.get('last_quote');k=self.kernel
        if not quote or quote['kernel_boot_id']!=k.boot_id or k._quote_age_ns(quote['received_ns'])>k.max_quote_age_ms*1_000_000:
            market['fresh']=False
        else:market.update(last=quote['price'],bid1=quote.get('bid1'),ask1=quote.get('ask1'))
        contexts={};saved=ex['contexts'].get(effect)
        if saved and saved['mode']!='ACTIVE':
            row=copy.deepcopy(saved['row']);row['age_ms']=ex['elapsed_ms']-ex['context_ms'][effect]
            contexts[saved['mode']]=row
        result=select_price(self.side(cycle,effect),market,contexts=contexts)
        if effect=='OPEN' and ('OPEN' not in ex['contexts']):
            result.update(allowed=False,price=None,reasons=['OPEN_SESSION_CLOCK_UNKNOWN'])
        return result

    def progress(self,cid,*,allow_open=False,allow_retry=False):
        k=self.kernel
        if k._failed:return
        cycle=copy.deepcopy(k._staged[cid])
        if not self.active(cycle) or cid in k._recovery_pending or cycle.get('management_state')=='TAKEN_OVER':return
        ex=cycle['execution'];state=cycle['protection'];target=state.get('target_remaining_qty')
        if getattr(k.simulation,'strategy_enabled',False):
            snapshot=k.port.snapshot()
            if not snapshot['complete'] or not k.simulation.exit_day_ready(cycle,snapshot['trading_day']):return
        if set(state.get('exit_reasons',[]))&{'ADVERSE_SLIPPAGE','DATA_GAP_EXIT'} and ex['force_start_ms'] is None:
            ex['force_start_ms']=ex['elapsed_ms'];self._contexts(cycle)
        for effect in ('OPEN','REDUCE'):
            orders=[o for o in cycle['orders'] if o['effect']==effect]
            if not orders:continue
            desired=max(0,cycle['plan']['initial_open_quantity']-state['entry_filled_qty']) if effect=='OPEN' else (max(0,state['owned_qty']-target) if target is not None else 0)
            stop_open=bool(effect=='OPEN' and (ex['entry_stopped'] or cycle.get('cancel_entries') or target is not None))
            timeout=bool(effect=='OPEN' and state['entry_filled_qty']==0 and ex['elapsed_ms']>cycle['plan']['execution_policy']['entry_confirm_timeout_ms'])
            can_increase=not stop_open and not timeout and k._armed and not k.recovery_required and k.control.can_increase(cycle['plan']['contract_id'])
            priced=self.price(cycle,effect)
            working=[o for o in orders if OrderState.from_dict(o).potential_remaining]
            candidates=working or [orders[-1]]
            for order in candidates:
                pending=sum(OrderState.from_dict(o).potential_remaining for o in orders if o['child_order_id']!=order['child_order_id'])
                action=decide_order_action(order|{'side':self.side(cycle,effect)},priced['price'] if priced['allowed'] else None,
                    can_increase=can_increase if effect=='OPEN' else True,remaining_quantity=0 if stop_open else desired,
                    other_pending_quantity=pending,timeout_due=timeout,
                    reprice_cancelled=ex['cancel_reasons'].get(order['child_order_id']) in {'NEW_QUOTE_PRICE_WORSENED','TARGET_QUANTITY_REDUCED','CABINET_DAY_ROLLOVER'})
                if action['action']=='CANCEL':
                    reason=action['reasons'][0]
                    if timeout:ex['entry_stopped']=True
                    ex['cancel_reasons'][order['child_order_id']]=reason
                    ex.setdefault('cancel_times',{})[order['child_order_id']]=ex['elapsed_ms']
                    cycle['revision']+=1;ex['last_action']='CANCEL';ex['last_reasons']=action['reasons']
                    k._stage_cycle(cycle,'TX_CANCEL','EXECUTION_CANCEL_REASON',after=lambda child=order['child_order_id']:k.request_cancel(child))
                    return
                if (action['action']=='WAIT' and 'CANCELLATION_PENDING' in action['reasons'] and
                        order['child_order_id'] in ex.get('cancel_times',{}) and
                        ex['elapsed_ms']-ex['cancel_times'][order['child_order_id']]>=30000):
                    cycle['protection'].update(reconciliation_required=True,block_new=True)
                    cycle['revision']+=1;ex['last_action']='WAIT_RECONCILIATION';ex['last_reasons']=['CANCEL_TERMINAL_TIMEOUT']
                    k._armed=False;k.contexts.revoke_all();k._recovery_pending.add(cid);k.recovery_required=True
                    k._stage_cycle(cycle,'TX_RECONCILE','EXECUTION_CANCEL_TIMEOUT');return
                if action['action']=='REPLACE' and (effect!='OPEN' or allow_open):
                    if order['send_state']=='DEFINITELY_NOT_SENT' and not allow_retry:continue
                    self._replace(cycle,order,action);return
                if (action['action']=='STOP' and effect=='OPEN' and not ex['entry_stopped'] and
                        (timeout or 'UNEXPECTED_ORDER_TERMINAL' in action['reasons'])):
                    ex['entry_stopped']=True;ex['last_action']='STOP';ex['last_reasons']=action['reasons']
                    cycle['revision']+=1;k._stage_cycle(cycle,'TX_ORDER','ENTRY_EXECUTION_STOPPED');return
        # A new reduction target is distinct from a replacement of a rejected SDK chain.
        if target is not None:
            pending=sum(OrderState.from_dict(o).potential_remaining for o in cycle['orders'] if o['effect']=='REDUCE')
            qty=max(0,state['owned_qty']-target-pending)
            reduces=[o for o in cycle['orders'] if o['effect']=='REDUCE']
            blocked=any(o['broker_state']=='REJECTED' or
                (o['terminal_confirmed'] and o['filled_qty']<o['original_qty'] and
                 o['send_state']!='DEFINITELY_NOT_SENT' and o['child_order_id'] not in ex['cancel_reasons']) for o in reduces)
            blocked=blocked or bool(reduces and reduces[-1]['send_state']=='DEFINITELY_NOT_SENT' and not allow_retry)
            if qty and not blocked:
                priced=self.price(cycle,'REDUCE')
                if priced['allowed']:self._new_reduction(cycle,qty,priced['price'])

    def _replace(self,cycle,old,action):
        k=self.kernel;qty=action['quantity'];price=action['price'];effect=old['effect']
        if qty<=0:return
        if effect=='OPEN' and getattr(k.simulation,'strategy_enabled',False) and k.simulation.consumed():return
        ordinal=max(o.get('ordinal',1) for o in cycle['orders'] if o['intent_id']==old['intent_id'])+1
        order=OrderState(_id('child'),old['intent_id'],_id('attempt'),effect,qty,price,
                        kernel_boot_id=k.boot_id,parent_child_order_id=old['child_order_id'],ordinal=ordinal)
        if getattr(k.simulation,'strategy_enabled',False) and k.simulation.prepare_order(cycle,order) is False:return
        if effect=='OPEN':
            if cycle.get('entry_origin')=='STRATEGY':
                runtime=getattr(k,'_dsv_runtime',None)
                if runtime is None:return
                prepared=runtime.prepare_replacement(cycle,order)
                if prepared is None:return
                cycle['entry_authorization']=prepared['authorization'];k._entry_gates[order.attempt_id]=prepared['gate']
            else:
                ex=cycle['execution'];context=ex.get('context_id')
                if not context:return
                try:k.contexts.validate(context,ex['peer_id'],'OPERATOR_COMMAND','MANUAL_OPEN',cycle['plan']['contract_id'],revisions=k.revisions,kernel_epoch=str(k.kernel_epoch))
                except TraderError:return
                cycle['entry_authorization']={'context_id':context,'peer_id':ex['peer_id'],'authority_revision':k._authority_revision}
            if k._exposure()+Decimal(cycle['risk_per_lot'])*qty>k.budget:return
            if getattr(k.simulation,'strategy_enabled',False):
                k.simulation.entry_attempt_prepared(cycle['position_cycle_id'])
                cycle['protection'].update(entry_terminal_reconciled=False,reconciled_entry_qty=None)
                cycle['lifecycle']='HOLDING' if cycle['protection']['owned_qty'] else 'ENTRY_PENDING'
        cycle['orders'].append(order.to_dict());cycle['revision']+=1
        cycle['execution']['last_action']='REPLACE'
        k._stage_cycle(cycle,'TX_INTENT','REPLACEMENT_PREPARED',after=lambda:k._dispatch(cycle['position_cycle_id'],order.child_order_id))

    def _new_reduction(self,cycle,qty,price):
        k=self.kernel
        order=OrderState(_id('child'),_id('exit-intent'),_id('attempt'),'REDUCE',qty,price,kernel_boot_id=k.boot_id)
        if getattr(k.simulation,'strategy_enabled',False) and k.simulation.prepare_order(cycle,order) is False:return
        cycle['orders'].append(order.to_dict());cycle['revision']+=1
        cycle['execution']['last_action']='REDUCE'
        k._stage_cycle(cycle,'TX_INTENT','EXIT_PREPARED',after=lambda:k._dispatch(cycle['position_cycle_id'],order.child_order_id))
