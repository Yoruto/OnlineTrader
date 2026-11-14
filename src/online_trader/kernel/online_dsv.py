"""DSV simulation owner: same ledger/OMS, separate finite capability."""
import copy
from datetime import datetime, timedelta, timezone

from online_trader.configuration.rollover import CHINA_TIME
from online_trader.contracts.codec import strict_loads
from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError
from online_trader.kernel.simulation import OnlineSimulation
from online_trader.online_scope import DsvSimulationGrant
from online_trader.protection.reducer import DsvSimulationPlanPermit


class OnlineDsvSimulation(OnlineSimulation):
    strategy_enabled=True

    def __init__(self,kernel,grant):
        self._archived_cycles = set()
        self._archived_order_calendars = {}
        self._approved_grants = {}
        self._prior_calendars = {}
        super().__init__(kernel,grant)
        from online_trader.kernel.day_recovery import DayRecovery
        from online_trader.kernel.historical_orders import HistoricalOrders
        self.days=DayRecovery(self)
        self.historical_orders=HistoricalOrders(self)

    def accepts_cycle(self, cycle):
        if super().accepts_cycle(cycle):
            return True
        plan = cycle['plan']
        if (plan.get('account_key') != self.grant.account_key
                or plan.get('approval_record_id') == self.grant.value['grant_id']
                or cycle['lifecycle'] != 'CLOSED' or cycle['protection']['owned_qty']
                or cycle.get('statistics_review_required') or cycle['protection'].get('reconciliation_required')
                or any(not order['terminal_confirmed'] or order['reported_fill_qty'] != order['filled_qty']
                       or (order['send_state'] == 'DEFINITELY_NOT_SENT' and order['filled_qty'])
                       or order['send_state'] not in {'DEFINITELY_NOT_SENT', 'CONFIRMED'}
                       or order.get('cancel_status') in {'REQUESTED', 'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}
                       for order in cycle['orders'])):
            return False
        original = self.approved_grant(plan['approval_record_id'])
        try:DsvSimulationPlanPermit.issue(plan, original)
        except TraderError:return False
        self._archived_cycles.add(cycle['position_cycle_id'])
        for order in cycle['orders']:
            self._archived_order_calendars[order['child_order_id']] = original.value['calendar_hash']
        return True

    def archived_cycle(self, cycle):
        return cycle['position_cycle_id'] in self._archived_cycles

    def approved_grant(self, grant_id):
        if grant_id not in self._approved_grants:
            rows = self.kernel.ledger.query(
                'SELECT manifest_json,manifest_hash FROM approval_manifests WHERE approval_record_id=?', (grant_id,))
            if not rows:
                raise TraderError('PLAN_APPROVAL_REQUIRED', 'The original strategy scope has not committed')
            record = rows[0]
            grant = DsvSimulationGrant.parse(record['manifest_json'])
            if grant.content_hash != record['manifest_hash'] or grant.account_key != self.kernel.account_key:
                raise TraderError('E_SCOPE', 'Committed strategy scope identity differs')
            self._approved_grants[grant_id] = grant
        return self._approved_grants[grant_id]

    def initialize(self):
        snapshot=super().initialize()
        if not any(c['plan']['trading_day']!=snapshot['trading_day'] and not self.archived_cycle(c)
                   for c in self.kernel._staged.values()):
            self.days.ready_day=snapshot['trading_day'];self.days.ready_epoch=self.port.source_epoch
        return snapshot

    def exit_day_ready(self,cycle,day):
        if self.archived_cycle(cycle):return False
        if self.grant.multi_day:return self.grant.permits_day(day) and self.days.ready(day) and not self.port.fault
        return day==cycle['plan']['trading_day']

    def reconciliation_snapshot(self,snapshot):
        if not self.grant.multi_day:return snapshot
        return self.historical_orders.supplement(self.days.snapshot(snapshot))

    def native_observations(self):
        observations=super().native_observations()
        if not self.historical_orders.records or not self.port.complete():return observations
        current=self.port.snapshot()
        if not current['complete']:return observations
        self.historical_orders.supplement(current)
        return self.historical_orders.observations(observations)

    def historical_terminal(self,observation):
        return observation['order_id'] in self.historical_orders.records

    def authority_policy(self):
        return {'allowed_origins':['MANUAL' if self.grant.manual_workbench else 'STRATEGY'],'execution_scope':'ONLINE_SIMULATION','provider':self.grant.provider,
                'grant_hash':self.grant.content_hash,'automatic_strategy':not self.grant.manual_workbench,
                'max_order_quantity':self.grant.value['max_order_quantity'], **self.grant.opening_limit}

    def permit(self,plan):
        grant = self.approved_grant(plan['approval_record_id'])
        if (grant.content_hash != self.grant.content_hash
                and plan['position_cycle_id'] not in self._archived_cycles):
            raise TraderError('PLAN_APPROVAL_REQUIRED', 'The original plan is outside the current owner')
        return DsvSimulationPlanPermit.issue(plan, grant)

    def consumed(self):
        if self.grant.daily_targets_per_contract is not None:
            # The per-contract quota is checked at admission and dispatch.
            return False
        return self.opening_attempt_count() >= self.grant.value['opening_attempts']

    def can_resume_entries(self, snapshot):
        k = self.kernel
        try:
            self._validate_snapshot(snapshot)
            if (self.grant.manual_workbench or self.port.fault or not self.days.ready(snapshot['trading_day'])
                    or k._has_unknown() or k.recovery_required or not k.control.can_increase()
                    or snapshot['funds'].get('available') is None):
                return False
            return not any(
                OrderState.from_dict(order).potential_remaining and
                (order['effect'] == 'OPEN' or order.get('cancel_status') in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'})
                for cycle in k._staged.values() for order in cycle['orders'])
        except TraderError:
            return False

    def target_quota(self, contract, day, *, existing=None):
        limit = self.grant.daily_targets_per_contract
        if limit is None:
            return self.opening_attempt_count() + (existing is None) <= self.grant.value['opening_attempts']
        counts = self.kernel.statistics.opening_counts(self.kernel._staged.values(), day, reserve=True, exclude_decision=existing)
        return counts.get(contract, 0) < limit

    def opening_attempt_count(self):
        # All prepared attempts consume the finite scope, including unknown,
        # rejected and definitely-unsent children; restart never renews it.
        return sum(o['effect']=='OPEN' for c in self.kernel._staged.values() if not self.archived_cycle(c) for o in c['orders'])

    def arm(self):
        k=self.kernel;scheduler=getattr(k,'_scheduled_runtime',None)
        if not self.port.entry_clock_ready():
            raise TraderError('E_CLOCK','A valid Windows wall clock is required to enable new entries')
        if (scheduler is None or not scheduler._enabled or scheduler._fault or scheduler.clock_rebuild_pending
                or scheduler.calendar.content_hash!=self.grant.value['calendar_hash']
                or scheduler.strategy.epoch!=k.active_epoch or not k.control.can_increase()
                or any(c.get('statistics_review_required') for c in k._staged.values())):
            raise TraderError('E_NOT_READY','Current DSV owner, calendar, clock and control permission are required')
        return super().arm()

    def validate_entry(self,plan,*,manual=False,entry_kind=None):
        if manual!=self.grant.manual_workbench or plan.get('approval_record_id') != self.grant.value['grant_id']:
            raise TraderError('E_ONLINE_SCOPE','This scope does not authorize the requested entry origin')
        self.permit(plan)
        if not self.port.entry_clock_ready():
            raise TraderError('E_CLOCK','A valid Windows wall clock is required for DSV/SimNow entries')
        if not self._configured or self.port.fault or not self.port.complete():
            raise TraderError('E_GATEWAY_SYNC', '当前柜台事实未完整同步。')
        if not self.grant.in_window(self.port.clock.now()):
            raise TraderError('E_ENTRY_WINDOW_EXPIRED', '当前时刻超出已确认的开仓窗口。')
        if not self.target_quota(plan['contract_id'], plan['trading_day']):
            code = 'DAILY_ORDER_LIMIT' if self.grant.daily_targets_per_contract is not None else 'E_ENTRY_ATTEMPT_LIMIT'
            raise TraderError(code, '本次授权允许的开仓额度已用完。')
        runtime=getattr(self.kernel,'_dsv_runtime',None)
        if runtime is None or runtime.profile.content_hash!=self.grant.value['profile_hash']:
            raise TraderError('E_ONLINE_SCOPE','The active strategy profile differs from its original scope')
        code = plan['contract_id']
        if not manual and entry_kind == 'NORMAL':
            facts = runtime._latest.get((code, plan['trading_day']))
            side = 'BUY' if plan['direction']=='LONG' else 'SELL'
            fresh = self.port.fresh_strategy_entry(code, side, facts['event'] if facts is not None else None)
        else:
            fresh = self.port.fresh(code)
        if not fresh or self.port.position(code)!={'long':0,'short':0} or self.port.active_orders(code):
            raise TraderError('E_OWNERSHIP','Strategy entry requires a fresh unoccupied selected contract')

    def manual_entry_facts(self,candidate,quantity,*,exclude_decision=None):
        k=self.kernel;runtime=k._dsv_runtime;code=candidate['contract_id']
        facts=runtime._latest.get((code,candidate['trading_day']))
        if not self.grant.manual_workbench or facts is None or not self.days.ready(candidate['trading_day']) or not self.port.fresh(code):
            raise TraderError('E_NOT_READY','A current manual workbench scope and native quote are required')
        event=facts['event']
        if not k._scheduled_runtime.entry_quote_after_clock_reset(event):
            raise TraderError('E_NOT_READY', '完整核对后需等待下一笔实时行情。')
        if (event['source_epoch']!=self.port.source_epoch or
                k._quote_age_ns(facts['received_ns'])>k.max_quote_age_ms*1_000_000 or
                runtime._blocked(event,exclude_decision=exclude_decision) or
                (facts.get('stop_result') and not facts['stop_result']['allow_new_entries'])):
            raise TraderError('E_NOT_READY','Current session, account and stop gates block manual entry')
        account,instrument=self.manual_native_facts(event,k)
        account['unknown_orders']=any(o['send_state'] in {'SEND_POSSIBLE','UNKNOWN'} for c in k._staged.values()
            if c['decision_key']!=exclude_decision for o in c['orders'])
        facts=facts|{'account':account,'instrument':instrument}
        risk=runtime._risk(candidate|{'entry_kind':'MANUAL'},facts,exclude_decision=exclude_decision)
        if not risk['allowed'] or quantity>min(risk['quantity'],self.grant.value['max_order_quantity']):
            raise TraderError('E_RISK','Manual quantity exceeds current account, contract or scope limits')
        return facts,risk

    def manual_open(self,params,decision,*,context_id,peer_id):
        from online_trader.strategy.plan import build_simulation_plan
        k=self.kernel;runtime=k._dsv_runtime;code=params['contract_id']
        day=self.port.snapshot()['trading_day']
        symbol=next((row for row in runtime.profile.to_record()['settings']['symbols'] if row['code']==code),None)
        if symbol is None or code not in self.grant.value['contracts'] or not self.grant.permits_day(day):
            raise TraderError('E_SCOPE','Manual entry requires a contract and cabinet day in the current scope')
        price=params['limit_price'];qty=params['quantity']
        candidate={'account_key':k.account_key,'contract_id':code,'trading_day':day,'direction':params['direction'],
            'profile_hash':runtime.profile.content_hash,'theoretical_price':price,'limit_price':price,
            'open_offset':symbol['open_offset'],'stop_distance':symbol['stop_offset'],'take_profit_distance':symbol['profit_offset']}
        facts,risk=self.manual_entry_facts(candidate,qty)
        candidate['source_time_utc']=facts['event']['source_time_utc']
        plan=build_simulation_plan(runtime.profile,candidate,grant=self.grant,account_key=k.account_key,trading_day=day,
            config_revision=str(k._config_revision),config_hash=k._configuration['payload_hash'],
            rule_set_id=k._configuration['rule_set_id'],quantity=qty,plan_id='manual-plan-'+decision,
            position_cycle_id='manual-cycle-'+decision)
        metadata={'candidate':candidate,'instrument':facts['instrument'],'margin_per_lot':risk['metrics']['margin_per_lot']}
        child=k.admit_entry(plan,decision,price,context_id=context_id,peer_id=peer_id,_manual=metadata)
        return plan['position_cycle_id'],child

    def mark_fill(self,cycle,order):
        # Advanced protection is source-bound; the old one-minute manual-test
        # deadline must never be silently attached to an automatic DSV cycle.
        pass

    def native_day_proof(self, child, code, stamp, *, fill=False):
        from online_trader.gateway.native_days import native_day
        calendar = self.port.calendar
        digest = self._archived_order_calendars.get(child)
        if digest is not None and digest != calendar.content_hash:
            if digest not in self._prior_calendars:
                from online_trader.scheduling.calendar import TradingCalendar
                rows = self.kernel.ledger.query("""
                    SELECT payload_json FROM config_revisions
                    WHERE json_extract(payload_json,'$.scheduled_development.calendar_hash')=?
                    ORDER BY config_revision DESC LIMIT 1
                    """, (digest,))
                if not rows:
                    raise TraderError('E_CALENDAR_INCOMPLETE', 'The archived order calendar is unavailable')
                record = strict_loads(rows[0]['payload_json'])['scheduled_development']['calendar']
                previous = TradingCalendar.from_record(record)
                if previous.content_hash != digest:
                    raise TraderError('E_CALENDAR_INCOMPLETE', 'The archived order calendar identity differs')
                self._prior_calendars[digest] = previous
            try:
                return native_day(self._prior_calendars[digest], code, stamp)
            except TraderError as error:
                if not fill or error.code != 'E_CALENDAR_INCOMPLETE':
                    raise
        return native_day(calendar, code, stamp)

    def _validate_native_time(self, proof):
        scheduler = getattr(self.kernel, '_scheduled_runtime', None)
        if scheduler is not None and scheduler._fault:
            # Native identity and the original calendar still prove received
            # facts when Windows wall time cannot validate the arrival time.
            return
        now = self.port.clock.now()
        quote = self.port.quotes.get(proof['contract_id'])
        try:
            source = datetime.fromisoformat(quote.datetime)
            if source.tzinfo is None:
                source = source.replace(tzinfo=CHINA_TIME)
            source_utc = source.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            if self.port.calendar.query(proof['contract_id'], source_utc)['is_trading']:
                now = max(now, source)
        except (AttributeError, ValueError, TraderError):
            pass
        upper_ns = int((now + timedelta(seconds=1)).timestamp() * 1_000_000) * 1000
        if int(proof['source_time_ns']) > upper_ns:
            raise TraderError('E_FACT_BARRIER', 'Native timestamp is ahead of current time evidence')

    def fill_day(self,observation,fill):
        proof=self.native_day_proof(observation['child_order_id'],observation['contract_id'],fill['source_time_ns'],fill=True)
        if fill.get('native_day_evidence')!=proof or fill.get('trading_day')!=proof['trading_day']:
            raise TraderError('E_FACT_BARRIER','Native fill day is not proved by the original bound calendar')
        self._validate_native_time(proof)
        origin = observation.get('order_day_evidence')
        if origin is None or proof['trading_day'] < origin['trading_day']:
            raise TraderError('E_FACT_BARRIER', 'Native fill belongs to a day before its original order')
        return proof['trading_day']

    def validate_native_order(self,observation):
        proof=observation.get('order_day_evidence')
        if not proof or proof!=self.native_day_proof(observation['child_order_id'],observation['contract_id'],proof['source_time_ns']):
            raise TraderError('E_FACT_BARRIER','Original native order needs its exact calendar-bound day')
        cycle,_=self.kernel._find(observation['child_order_id'])
        order=next(o for o in cycle['orders'] if o['child_order_id']==observation['child_order_id'])
        if proof['trading_day']!=order.get('native_trading_day',cycle['plan']['trading_day']):
            raise TraderError('E_SCOPE','Native order day differs from the committed original child')
        self._validate_native_time(proof)

    def prepare_order(self,cycle,order):
        exchange=cycle['plan']['contract_id'].split('.')[0]
        order.native_offset='OPEN' if order.effect=='OPEN' else 'CLOSETODAY' if exchange in {'SHFE','INE'} else 'CLOSE'
        order.native_trading_day=cycle['plan']['trading_day']
        if order.effect=='REDUCE':
            from online_trader.execution.close_routes import preview_owned_routes
            state=cycle['protection'];target=state.get('target_remaining_qty')
            if target is None:raise TraderError('E_FACT_BARRIER','An original common exit target is required')
            scheduler = getattr(self.kernel, '_scheduled_runtime', None)
            now = None
            if scheduler is not None and scheduler._fault:
                now = scheduler.clock_protection_time(cycle)
                if now is None:
                    return False
            try:review=preview_owned_routes(self.kernel,cycle['position_cycle_id'],max(0,state['owned_qty']-target),_now=now)
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

    def verify_clock_protection(self, cycle, snapshot, *, exclude_child=None):
        """Check current account facts before acting on a native quote during a wall-clock fault."""
        self._validate_snapshot(snapshot)
        code = cycle['plan']['contract_id']
        self.verify_owned_position(code, snapshot)
        self.verify_foreign_orders(cycle, snapshot)
        managed = [c for c in self.kernel._staged.values() if c['plan']['contract_id'] == code
                   and c['lifecycle'] != 'CLOSED']
        owned = {self.port.order_id(o['child_order_id']) for c in managed for o in c['orders']}
        if any(o['order_id'] not in owned for o in snapshot['orders']
               if o['contract_id'] == code and o['status'] == 'ALIVE'):
            raise TraderError('E_OWNERSHIP', 'An active foreign order prevents proof of owned price protection')
        snapshot = self.reconciliation_snapshot(snapshot)
        for current in managed:
            orders = [o for o in current['orders'] if o['child_order_id'] != exclude_child]
            if any(o['send_state'] in {'SEND_POSSIBLE', 'UNKNOWN'}
                   or o['cancel_status'] == 'UNKNOWN' and not o['terminal_confirmed']
                   or o['reported_fill_qty'] not in (None, o['filled_qty']) for o in orders):
                raise TraderError('E_UNKNOWN_SEND', 'The original order or cancellation still needs native evidence')
            self.verify_cycle_orders(current, orders, snapshot)

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

    def recover_working(self,observations,*,reconcile_cycles=True):
        k=self.kernel
        if not self.port.complete():return
        snapshot=self.port.snapshot();self._validate_snapshot(snapshot);snapshot=self.reconciliation_snapshot(snapshot)
        native={o['order_id']:o for o in snapshot['orders']}
        for observation in observations:
            if observation['status']!='ALIVE':continue
            cycle,_=k._find(observation['child_order_id'])
            if self.archived_cycle(cycle):continue
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
        if not reconcile_cycles:return
        for cid in tuple(k._recovery_pending):
            # Reconciliation validates all native order and detailed-fill
            # identities before lifting only the original exit barrier.
            if not self.port.fresh(k._staged[cid]['plan']['contract_id']):continue
            cycle=k._staged[cid]
            if self.archived_cycle(cycle):continue
            if ('CANCEL_TERMINAL_TIMEOUT' in cycle.get('execution',{}).get('last_reasons',[])
                    and any(o.get('cancel_status') in {'SEND_POSSIBLE','CALL_RETURNED','UNKNOWN'} and not o['terminal_confirmed'] for o in cycle['orders'])):
                continue
            try:self.reconcile(cid,allow_working=True,clear_gateway_fault=False)
            except TraderError as error:
                if error.code not in {'E_FACT_BARRIER','E_OWNERSHIP'}:raise
        if self.port.fault and not self.grant.multi_day:
            self.days.ready_day = snapshot['trading_day']
            self.days.ready_epoch = self.port.source_epoch
            # Account sync and original order reconciliation are distinct.
            # An unresolved cycle stays blocked without freezing unrelated exits.
            self.capture('ONLINE_DSV_GATEWAY_RESYNCED',{'snapshot':snapshot,'entry_authority':'DISARMED',
                'unreconciled_cycles':sorted(k._recovery_pending)})
            self.port.fault=False;self._fault_recorded=False
