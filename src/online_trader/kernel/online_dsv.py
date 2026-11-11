"""DSV simulation owner: same ledger/OMS, separate finite capability."""
import copy

from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError
from online_trader.kernel.simulation import OnlineSimulation
from online_trader.protection.reducer import DsvSimulationPlanPermit


class OnlineDsvSimulation(OnlineSimulation):
    strategy_enabled=True

    def __init__(self,kernel,grant):
        super().__init__(kernel,grant)
        from online_trader.kernel.day_recovery import DayRecovery
        self.days=DayRecovery(self)

    def initialize(self):
        snapshot=super().initialize()
        if not any(c['plan']['trading_day']!=snapshot['trading_day'] for c in self.kernel._staged.values()):
            self.days.ready_day=snapshot['trading_day'];self.days.ready_epoch=self.port.source_epoch
        return snapshot

    def exit_day_ready(self,cycle,day):
        return (day==cycle['plan']['trading_day'] or self.grant.multi_day and self.grant.permits_day(day) and self.days.ready(day))

    def reconciliation_snapshot(self,snapshot):
        return self.days.snapshot(snapshot) if self.grant.multi_day else snapshot

    def authority_policy(self):
        return {'allowed_origins':['STRATEGY'],'execution_scope':'ONLINE_SIMULATION','provider':self.grant.provider,
                'grant_hash':self.grant.content_hash,'automatic_strategy':True,
                'max_order_quantity':self.grant.value['max_order_quantity'],'opening_attempts':self.grant.value['opening_attempts']}

    def permit(self,plan):
        rows=self.kernel.ledger.query('SELECT manifest_hash FROM approval_manifests WHERE approval_record_id=?',(self.grant.value['grant_id'],))
        if not rows or rows[0]['manifest_hash']!=self.grant.content_hash:
            raise TraderError('PLAN_APPROVAL_REQUIRED','The original strategy scope has not committed')
        return DsvSimulationPlanPermit.issue(plan,self.grant)

    def consumed(self):
        return self.opening_attempt_count()>=self.grant.value['opening_attempts']

    def opening_attempt_count(self):
        # All prepared attempts consume the finite scope, including unknown,
        # rejected and definitely-unsent children; restart never renews it.
        return sum(o['effect']=='OPEN' for c in self.kernel._staged.values() for o in c['orders'])

    def arm(self):
        k=self.kernel;scheduler=getattr(k,'_scheduled_runtime',None)
        self.comparison_gate()
        if not self.port.entry_clock_ready():
            raise TraderError('E_CLOCK','Windows and the reference clock differ by more than one second')
        if (scheduler is None or not scheduler._enabled or scheduler._fault
                or scheduler.calendar.content_hash!=self.grant.value['calendar_hash']
                or scheduler.strategy.epoch!=k.active_epoch or not k.control.can_increase()
                or any(c.get('statistics_review_required') for c in k._staged.values())):
            raise TraderError('E_NOT_READY','Current DSV owner, calendar, clock and control permission are required')
        return super().arm()

    def validate_entry(self,plan):
        self.permit(plan)
        self.comparison_gate()
        if not self.port.entry_clock_ready():
            raise TraderError('E_CLOCK','Synchronize Windows before opening comparable DSV/SimNow entries')
        if (not self._configured or self.consumed() or not self.grant.in_window(self.port.clock.now())
                or self.port.fault or not self.port.complete()):
            raise TraderError('E_ONLINE_SCOPE','DSV window, finite attempt budget or full-sync gate blocks entry')
        if (not self.port.fresh(plan['contract_id']) or self.port.position(plan['contract_id'])!={'long':0,'short':0}
                or self.port.active_orders(plan['contract_id'])):
            raise TraderError('E_OWNERSHIP','Strategy entry requires a fresh unoccupied selected contract')
        runtime=getattr(self.kernel,'_dsv_runtime',None)
        if runtime is None or runtime.profile.content_hash!=self.grant.value['profile_hash']:
            raise TraderError('E_ONLINE_SCOPE','The active strategy profile differs from its original scope')

    def comparison_gate(self):
        if getattr(self.kernel,'_comparison_fault',None):
            raise TraderError('E_COMPARISON','Comparison capture requires repair before new entries')
        path=getattr(self.kernel,'_comparison_profile_source',None)
        if path is not None:
            from online_trader.runtime.comparison import verify_dsv_profile
            source=getattr(self.kernel,'_auto_source_profile',None);effective=self.kernel._dsv_runtime.profile
            current=verify_dsv_profile(path,effective,source_profile=source)
            if source is not None:
                from online_trader.configuration.offsets import dsv_startup_profile
                self.kernel._auto_parameter_wait=dsv_startup_profile(current).content_hash!=dsv_startup_profile(effective).content_hash
                if self.kernel._auto_parameter_wait:
                    raise TraderError('E_NOT_READY','DSV must use the same calculated effective parameters before comparison entries can start')

    def mark_fill(self,cycle,order):
        # Advanced protection is source-bound; the old one-minute manual-test
        # deadline must never be silently attached to an automatic DSV cycle.
        pass

    def fill_day(self,observation,fill):
        from online_trader.gateway.native_days import native_day
        proof=native_day(self.port.calendar,observation['contract_id'],fill['source_time_ns'])
        if fill.get('native_day_evidence')!=proof or fill.get('trading_day')!=proof['trading_day']:
            raise TraderError('E_FACT_BARRIER','Native fill day is not proved by the original bound calendar')
        # Night sessions can belong to the next civil day. Compare exact time,
        # never the host's local date or the grant's requested day.
        from datetime import datetime,timezone,timedelta
        seconds,nanos=divmod(int(fill['source_time_ns']),1_000_000_000)
        at=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds,microseconds=nanos//1000)
        if at>self.port.clock.now()+timedelta(seconds=1):
            raise TraderError('E_FACT_BARRIER','Native fill time is ahead of the verified reference')
        origin=observation.get('order_day_evidence')
        if origin is None or proof['trading_day']<origin['trading_day']:
            raise TraderError('E_FACT_BARRIER','Native fill cannot belong to a cabinet day before its original order')
        return proof['trading_day']

    def validate_native_order(self,observation):
        from online_trader.gateway.native_days import native_day
        proof=observation.get('order_day_evidence')
        if not proof or proof!=native_day(self.port.calendar,observation['contract_id'],proof['source_time_ns']):
            raise TraderError('E_FACT_BARRIER','Original native order needs its exact calendar-bound day')
        cycle,_=self.kernel._find(observation['child_order_id'])
        order=next(o for o in cycle['orders'] if o['child_order_id']==observation['child_order_id'])
        if proof['trading_day']!=order.get('native_trading_day',cycle['plan']['trading_day']):
            raise TraderError('E_SCOPE','Native order day differs from the committed original child')
        from datetime import datetime,timezone,timedelta
        seconds,nanos=divmod(int(proof['source_time_ns']),1_000_000_000)
        at=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds,microseconds=nanos//1000)
        if at>self.port.clock.now()+timedelta(seconds=1):
            raise TraderError('E_FACT_BARRIER','Native order time is ahead of the verified reference')

    def prepare_order(self,cycle,order):
        exchange=cycle['plan']['contract_id'].split('.')[0]
        order.native_offset='OPEN' if order.effect=='OPEN' else 'CLOSETODAY' if exchange in {'SHFE','INE'} else 'CLOSE'
        order.native_trading_day=cycle['plan']['trading_day']
        if order.effect=='REDUCE':
            from online_trader.execution.close_routes import preview_owned_routes
            state=cycle['protection'];target=state.get('target_remaining_qty')
            if target is None:raise TraderError('E_FACT_BARRIER','An original common exit target is required')
            try:review=preview_owned_routes(self.kernel,cycle['position_cycle_id'],max(0,state['owned_qty']-target))
            except TraderError as error:
                if error.code not in {'E_FACT_BARRIER','E_OWNERSHIP','E_CALENDAR_INCOMPLETE'}:raise
                return False
            # Existing /1 scopes remain same-day. The bucket sender is ready
            # for explicitly reconciled lots, not a way around the day gate.
            if not self.exit_day_ready(cycle,review['current_day']) or not review['routes']:return False
            route=review['routes'][0]
            order.original_qty=min(order.original_qty,route['quantity'])
            order.native_offset=route['native_offset'];order.native_trading_day=review['current_day']
            order.close_route={'review_hash':review['evidence_hash'],'durable_seq':str(self.kernel._durable_seq),
                'position_cycle_id':cycle['position_cycle_id'],'trading_day':review['current_day'],
                'native_offset':order.native_offset,'quantity':order.original_qty}
        return True

    def can_close(self,cycle):
        ex=cycle.get('execution',{});state=cycle['protection'];k=self.kernel
        pending=(not ex.get('entry_stopped') and not cycle.get('cancel_entries') and state.get('target_remaining_qty') is None
                 and k._armed and cycle['strategy_epoch']==k.active_epoch and not self.consumed()
                 and self.grant.in_window(self.port.clock.now())
                 and state['entry_filled_qty']<cycle['plan']['initial_open_quantity']
                 and any(ex.get('cancel_reasons',{}).get(o['child_order_id']) in {'NEW_QUOTE_PRICE_WORSENED','TARGET_QUANTITY_REDUCED'}
                         for o in cycle['orders'] if o['effect']=='OPEN'))
        return not pending

    def entry_attempt_prepared(self,cid):
        self._entry_reconciled.discard(cid);self._closed.discard(cid)

    def recover_working(self,observations):
        k=self.kernel
        if not self.port.complete():return
        snapshot=self.port.snapshot();self._validate_snapshot(snapshot);snapshot=self.reconciliation_snapshot(snapshot)
        native={o['order_id']:o for o in snapshot['orders']}
        for observation in observations:
            if observation['status']!='ALIVE':continue
            cycle,_=k._find(observation['child_order_id'])
            order=next(o for o in cycle['orders'] if o['child_order_id']==observation['child_order_id'])
            item=native.get(observation['order_id'])
            direction='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL'
            if (item is None or item.get('contract_id')!=cycle['plan']['contract_id'] or item.get('direction')!=direction
                    or item.get('offset')!=order.get('native_offset',('OPEN' if order['effect']=='OPEN' else 'CLOSE')) or item.get('status')!='ALIVE'
                    or item.get('quantity')!=order['original_qty'] or item.get('limit_price')!=order['limit_price']
                    or item['quantity']-item['remaining']!=order['filled_qty']):continue
            if order['send_state'] in {'UNKNOWN','SEND_POSSIBLE'}:
                order.update(send_state='CONFIRMED',broker_state='PARTIALLY_FILLED' if order['filled_qty'] else 'WORKING',
                             reported_fill_qty=order['filled_qty'],revision=order['revision']+1)
                cycle['revision']+=1
                k._stage_cycle(cycle,'TX_ORDER','ONLINE_WORKING_ORDER_PROVED');k.wait_idle()
        for cid in tuple(k._recovery_pending):
            # Reconciliation validates all native order and detailed-fill
            # identities before lifting only the original exit barrier.
            if not self.port.fresh(k._staged[cid]['plan']['contract_id']):continue
            cycle=k._staged[cid]
            if ('CANCEL_TERMINAL_TIMEOUT' in cycle.get('execution',{}).get('last_reasons',[])
                    and any(o.get('cancel_status') in {'SEND_POSSIBLE','CALL_RETURNED','UNKNOWN'} and not o['terminal_confirmed'] for o in cycle['orders'])):
                continue
            try:self.reconcile(cid,allow_working=True)
            except TraderError as error:
                if error.code not in {'E_FACT_BARRIER','E_OWNERSHIP'}:raise
