"""Finite DSV TqKq adapter; inherits native binding/callbacks, never a matcher."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import copy

from online_trader.errors import TraderError
from online_trader.gateway.tqkq import TqKqPort
from online_trader.online_scope import DsvSimulationGrant


class TqKqDsvPort(TqKqPort):
    def entry_clock_ready(self):
        if not self.broker_connected:return True  # injected QA facts have their own clock
        from online_trader.scheduling.reference import clock_observation
        return clock_observation(self.clock)['system_aligned']

    def __init__(self,api,account,grant,clock):
        if type(grant) is not DsvSimulationGrant:
            raise TraderError('E_ONLINE_SCOPE','The manual one-shot grant cannot enable DSV execution')
        super().__init__(api,account,grant,clock)
        self.calendar=None
        self.started_at=clock.now()
        self.subscription_ready_at={}
        # Pinned SDK 3.10.4 api.get_tick_serial explicitly caps native history
        # at 10000. Use its supported full window; never synthesize older ticks.
        self.tick_serials={code:api.get_tick_serial(code,data_length=10000) for code in grant.value['contracts']}

    def configure_calendar(self,calendar):
        if calendar.content_hash!=self.grant.value['calendar_hash']:
            raise TraderError('E_ONLINE_SCOPE','The native adapter calendar differs from the strategy grant')
        self.calendar=calendar

    def snapshot(self):
        value=super().snapshot()
        if self.calendar is None:raise TraderError('E_CALENDAR','Explicit strategy calendar is required')
        now=self.clock.now().isoformat().replace('+00:00','Z')
        windows=[self.calendar.query(code,now) for code in self.grant.value['contracts']]
        days={item.get('trading_day') or (item.get('next_session') or {}).get('trading_day') for item in windows}
        # Day labels come from explicit current calendar evidence, never from
        # blindly copying the requested grant into a native-current claim.
        value['trading_day']=next(iter(days)) if len(days)==1 else None
        value['complete']=bool(value['complete'] and all(item['complete'] for item in windows)
                               and len(days)==1 and self.grant.permits_day(value['trading_day']))
        value['trading_day_source']='BOUND_EXPLICIT_CALENDAR_AT_VERIFIED_REFERENCE'
        return value

    def poll_updates(self,seconds=0.1):
        epoch=self.source_epoch
        super().poll_updates(seconds)
        if self.source_epoch!=epoch:self.started_at=self.clock.now();self.subscription_ready_at.clear()

    def observations(self):
        from online_trader.gateway.native_days import native_day
        observations=super().observations()
        orders=self.api.get_order(account=self.account)
        for observation in observations:
            item=orders[observation['order_id']]
            stamp=getattr(item,'insert_date_time',None)
            try:
                if type(stamp) is not int:raise TraderError('E_FACT_BARRIER','Missing native order time')
                observation['order_day_evidence']=native_day(self.calendar,observation['contract_id'],str(stamp))
            except TraderError as error:observation['order_day_error']=error.code
            for fill in observation['fills']:
                try:
                    proof=native_day(self.calendar,observation['contract_id'],fill['source_time_ns'])
                    fill.update(trading_day=proof['trading_day'],native_day_evidence=proof)
                except TraderError as error:fill['native_day_error']=error.code
        return observations

    def validate_send(self,request):
        value=self.grant.value;kernel=getattr(self,'kernel',None)
        if (self.read_only or self.fault or not self.complete() or kernel is None
                or request.get('account_key')!=self.grant.account_key or request.get('contract_id') not in self.quotes
                or not self.grant.permits_day(request.get('trading_day')) or request.get('direction') not in {'BUY','SELL'}
                or request.get('effect') not in {'OPEN','REDUCE'} or type(request.get('original_qty')) is not int
                or not 1<=request['original_qty']<=value['max_order_quantity'] or not self.fresh(request['contract_id'])):
            raise TraderError('E_GATEWAY_SCOPE','A fresh, complete and exactly owned DSV simulated order is required')
        snapshot=self.snapshot()
        if not snapshot['complete'] or snapshot['trading_day']!=request['trading_day']:
            raise TraderError('E_FACT_BARRIER','Current trading day is outside the explicit native scope')
        cycle,_=kernel._find(request['child_order_id'])
        order=next(o for o in cycle['orders'] if o['child_order_id']==request['child_order_id'])
        if (cycle['position_cycle_id']!=request.get('position_cycle_id') or cycle.get('management_state')=='TAKEN_OVER'
                or any(order[key]!=request[key] for key in ('effect','original_qty','limit_price','native_offset'))):
            raise TraderError('E_OWNERSHIP','Native order is not the exact committed original child')
        expected='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL'
        if request['direction']!=expected:raise TraderError('E_OWNERSHIP','Native order direction differs from the original cycle')
        position=self.position(request['contract_id'])
        owned={side:sum(c['protection']['owned_qty'] for c in kernel._staged.values()
                       if c['plan']['contract_id']==request['contract_id'] and c['plan']['direction']==side.upper()) for side in ('long','short')}
        if position!=owned:raise TraderError('E_OWNERSHIP','External position changes block this native send')
        owned_ids={self.order_id(o['child_order_id']) for c in kernel._staged.values() for o in c['orders']}
        if any(o['order_id'] not in owned_ids for o in snapshot['orders']
               if o['contract_id']==request['contract_id'] and o['status']=='ALIVE'):
            raise TraderError('E_OWNERSHIP','An active foreign order blocks native execution on the selected contract')
        if request['effect']=='OPEN':
            kernel.simulation.comparison_gate()
            if not self.entry_clock_ready():
                raise TraderError('E_CLOCK','Windows clock alignment is required immediately before native opening send')
            facts=kernel._dsv_runtime._latest.get((request['contract_id'],request['trading_day']))
            current=datetime.fromisoformat(self.quotes[request['contract_id']].datetime).replace(tzinfo=timezone(timedelta(hours=8)))
            if facts is None or current!=datetime.fromisoformat(facts['event']['source_time_utc']):
                raise TraderError('E_GATEWAY_SCOPE','The entry proposal does not reflect the latest native quote')
            if (not self.grant.in_window(self.clock.now()) or kernel.recovery_required
                    or kernel.simulation.opening_attempt_count()>value['opening_attempts']
                    or any(o['order_id']!=self.order_id(request['child_order_id']) for o in self.active_orders(request['contract_id']))):
                raise TraderError('E_GATEWAY_SCOPE','New risk exceeds its original window, quota or account gate')
        elif request['original_qty']>owned['long' if expected=='SELL' else 'short']:
            raise TraderError('E_OWNERSHIP','Native reduction exceeds the proved owned side')
        if request['effect']=='REDUCE':
            if not kernel.simulation.exit_day_ready(cycle,request['trading_day']):
                raise TraderError('E_FACT_BARRIER','Original exits require current-day native reconciliation')
            from online_trader.execution.close_routes import preview_owned_routes
            state=cycle['protection'];target=state.get('target_remaining_qty')
            if target is None:raise TraderError('E_FACT_BARRIER','Native reduction lacks its common remaining target')
            review=preview_owned_routes(kernel,cycle['position_cycle_id'],max(0,state['owned_qty']-target),
                _exclude_child=request['child_order_id'])
            route=next((r for r in review['routes'] if r['native_offset']==request['native_offset']),None)
            saved=order.get('close_route')
            if (route is None or request['original_qty']>route['quantity'] or order.get('native_trading_day')!=review['current_day']
                    or saved is None or saved.get('position_cycle_id')!=cycle['position_cycle_id']
                    or saved.get('trading_day')!=review['current_day'] or saved.get('native_offset')!=request['native_offset']
                    or saved.get('quantity')!=request['original_qty']):
                raise TraderError('E_OWNERSHIP','Native close no longer matches its committed cycle and unreserved bucket')
        q=self.quotes[request['contract_id']];price=Decimal(request['limit_price']);tick=Decimal(str(q.price_tick))
        if price%tick or not Decimal(str(q.lower_limit))<=price<=Decimal(str(q.upper_limit)):
            raise TraderError('E_PRICE_ALIGNMENT','DSV native order price is not a legal exchange tick')

    def send(self,request):
        self.validate_send(request);child=request['child_order_id']
        if child in self._issued:raise TraderError('E_DUPLICATE_CONFLICT','A DSV native send cannot be replayed')
        self._issued.add(child)
        self._bindings[child]={key:request[key] for key in ('contract_id','direction','effect','original_qty','limit_price','native_offset')}
        return self.api.insert_order(symbol=request['contract_id'],direction=request['direction'],
            offset=request['native_offset'],volume=request['original_qty'],
            limit_price=float(Decimal(request['limit_price'])),order_id=self.order_id(child),account=self.account)

    def tick_records(self,code):
        serial=self.tick_serials[code]
        if not self.api.is_serial_ready(serial):return []
        self.subscription_ready_at.setdefault(code,self.clock.now())
        return serial.to_dict('records')
