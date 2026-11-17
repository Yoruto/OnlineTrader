"""Bounded ONLINE_SIM owner operations using the original kernel and writer."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal, localcontext
import copy,time,uuid

from online_trader.contracts.codec import canonical_bytes,content_hash,strict_loads
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.protection import ProtectionState,reduce_event


class OnlineSimulation:
    strategy_enabled=False
    def __init__(self,kernel,grant):
        self.kernel,self.grant,self.port=kernel,grant,kernel.port
        if self.port.grant.content_hash!=grant.content_hash:raise TraderError('E_SCOPE','Adapter and owner grant differ')
        prior=kernel.ledger.query('SELECT manifest_hash FROM approval_manifests WHERE approval_record_id=?',(grant.value['grant_id'],))
        if prior and prior[0]['manifest_hash']!=grant.content_hash:
            raise TraderError('E_SCOPE','The original immutable scope identity has different contents')
        if any(not self.accepts_cycle(cycle) for cycle in kernel._staged.values()):
            raise TraderError('E_SCOPE','A simulation restart requires the original grant and owned cycle identities')
        self.port.bind(kernel);self._configured=False;self._last_observations=None;self._quotes={};self._fault_recorded=False
        self.baseline=None;self._terminal=set();self._entry_reconciled=set();self._closed=set()
        self._account_revision=0
        self._observed_orders={}
        for row in kernel.ledger.query("SELECT payload_json FROM event_journal WHERE event_type='ONLINE_ORDER_OBSERVATIONS' ORDER BY event_seq"):
            data=strict_loads(row['payload_json'])['data']
            # Older boots could commit observation evidence before rejecting its
            # fill key. Normalize that evidence before unioning it with live facts.
            for observation in data['orders']:
                for fill in observation['fills']:
                    fill.update(self.port.trade_identity(self.port.native_trade_id(fill)))
            self._observed_orders=self._merge_observations(data['orders'])

    def accepts_cycle(self, cycle):
        plan = cycle['plan']
        return (plan.get('approval_record_id') == self.grant.value['grant_id']
                and plan.get('account_key') == self.grant.account_key
                and self.day_permitted(plan.get('trading_day'))
                and plan.get('contract_id') in self.grant.value['contracts'])

    def archived_cycle(self, cycle):
        return False

    def _merge_observations(self,observations):
        # Retain immutable native trade IDs/timestamps across reconnect/restart.
        # Missing callbacks never erase previously committed execution evidence.
        result=copy.deepcopy(self._observed_orders)
        for observation in observations:
            child=observation['child_order_id']
            original_day=self._observed_orders.get(child,{}).get('order_day_evidence')
            incoming_day=observation.get('order_day_evidence')
            if original_day is not None and incoming_day is not None and original_day!=incoming_day:
                raise TraderError('E_DUPLICATE_CONFLICT','Original native order timestamp changed')
            previous={f['trade_key']:f for f in self._observed_orders.get(child,{}).get('fills',[])}
            incoming={f['trade_key']:f for f in observation['fills']}
            if len(incoming)!=len(observation['fills']) or any(key in previous and
                    any(fill.get(name)!=value for name,value in previous[key].items()
                        if name not in {'native_day_error'}) for key,fill in incoming.items()):
                raise TraderError('E_DUPLICATE_CONFLICT','Native fill identity changed or is duplicated')
            value=copy.deepcopy(observation);value['fills']=list((previous|incoming).values())
            result[child]=value
        return result

    def authority_policy(self):
        return {'allowed_origins':['MANUAL'],'execution_scope':'ONLINE_SIMULATION','provider':'TQKQ',
                'grant_hash':self.grant.content_hash,'quantity':1,'opening_attempts':1,'automatic_strategy':False}

    def permit(self,plan):
        from online_trader.protection.reducer import SimulationPlanPermit
        rows=self.kernel.ledger.query('SELECT manifest_hash FROM approval_manifests WHERE approval_record_id=?',(self.grant.value['grant_id'],))
        if not rows or rows[0]['manifest_hash']!=self.grant.content_hash:raise TraderError('PLAN_APPROVAL_REQUIRED','Simulation grant has not committed')
        return SimulationPlanPermit.issue(plan,self.grant)

    def initialize(self):
        k=self.kernel;k._writable();k.wait_idle();snapshot=self.port.snapshot();self._validate_snapshot(snapshot)
        self.baseline=snapshot
        value=self.grant.value;rule=('kq-dsv-' if self.strategy_enabled else 'kq-manual-')+self.grant.content_hash[:32]
        descriptor={'kind':'KQ_DSV_ONLINE_SIMULATION' if self.strategy_enabled else 'KQ_MANUAL_ONLINE_SIMULATION',
                    'grant':value,'grant_hash':self.grant.content_hash,'execution_scope':'ONLINE_SIMULATION'}
        if getattr(self.grant,'provider','TQKQ')=='TQ_SIMNOW':
            rule='simnow-dsv-'+self.grant.content_hash[:32]
            descriptor['kind']='SIMNOW_DSV_ONLINE_SIMULATION'
        previous=k.ledger.query('SELECT payload_json FROM config_revisions WHERE config_revision=?',(k._config_revision,))
        if previous and strict_loads(previous[0]['payload_json'])==descriptor:
            self._configured=True
        else:
            revision=k.ledger.query('SELECT MAX(config_revision) AS n FROM config_revisions')[0]['n']+1
            digest=content_hash(descriptor);k._state_revision+=1;k._authority_revision+=1
            authority=k._authority()|{'config_revision':revision,'armed':False}
            def mutate(conn,seq):
                raw=canonical_bytes(descriptor).decode()
                conn.execute('INSERT OR IGNORE INTO rule_sets VALUES(?,?,?,?)',(rule,raw,digest,seq))
                conn.execute('INSERT INTO config_revisions VALUES(?,?,?,?,?,?)',(revision,rule,raw,digest,'NEXT_CYCLE',seq))
                conn.execute('INSERT OR IGNORE INTO approval_manifests VALUES(?,?,?,?,?)',(value['grant_id'],rule,canonical_bytes(value).decode(),self.grant.content_hash,seq))
                policies=[('MARKET_QUALITY','quality'),('EXECUTION','execution')]
                if self.strategy_enabled:policies.append(('AUTHORITY','authority'))
                for kind,name in policies:
                    payload={'kind':'KQ_MANUAL_SIMULATION_'+kind,'grant_hash':self.grant.content_hash,'execution_scope':'ONLINE_SIMULATION'}
                    if self.strategy_enabled:payload.update(kind='KQ_DSV_SIMULATION_'+kind,
                        allowed_origins=self.authority_policy()['allowed_origins'])
                    if getattr(self.grant,'provider','TQKQ')=='TQ_SIMNOW':payload['kind']='SIMNOW_DSV_SIMULATION_'+kind
                    conn.execute('INSERT OR IGNORE INTO policy_revisions VALUES(?,?,?,?,?,?)',('sim-'+name+'-'+self.grant.content_hash[:24],kind,rule,canonical_bytes(payload).decode(),content_hash(payload),seq))
                k._write_authority(conn,seq,authority)
            def accepted():
                k._config_revision=revision;k._configuration={'payload_hash':digest,'rule_set_id':rule};self._configured=True
            k._submit('TX_CONFIG','ONLINE_SIMULATION_SCOPE_COMMITTED',{'authority':authority,'online_scope':descriptor},mutate,after=accepted)
            k.wait_idle()
        self.capture('ONLINE_SIMULATION_BASELINE',{'snapshot':snapshot});return snapshot

    def capture(self,event,data):
        k=self.kernel;k._state_revision+=1
        if data.get('snapshot') is not None:data=data|{'money_account':self.money_fact(data['snapshot'])}
        future=k._submit('TX_RECONCILE',event,data,lambda conn,seq:None);k.wait_idle();return future.result()

    def money_fact(self,snapshot):
        from online_trader.accounting.book import ACCOUNT_FIELDS,utc_now,validate_account
        book = self.kernel.money.book
        fact = {'account_key': self.kernel.account_key, 'trading_day': snapshot['trading_day'],
                'source_epoch': snapshot['source_epoch'], 'evidence_id': snapshot['snapshot_id'],
                'currency': 'CNY', 'complete': snapshot['complete']}
        fact.update({key: snapshot['funds'].get(key) for key in ACCOUNT_FIELDS})
        recorded = book.account_evidence.get(fact['evidence_id'])
        if recorded is not None:
            # Several original cycles can reference the same cabinet snapshot.
            fact.update(revision=recorded['revision'],
                        observed_at_utc=snapshot.get('observed_at_utc', recorded['observed_at_utc']))
            if fact != recorded:
                raise TraderError('E_DUPLICATE_CONFLICT', 'Account snapshot identity changed content')
            return fact
        previous=book.account_facts[-1] if book.account_facts else None
        if previous and previous['source_epoch']==snapshot['source_epoch']:
            self._account_revision=max(self._account_revision,previous['revision'])
        self._account_revision+=1
        fact.update(revision=self._account_revision, observed_at_utc=snapshot.get('observed_at_utc') or utc_now())
        return validate_account(fact,self.kernel.account_key)

    def day_permitted(self,day):
        return self.grant.permits_day(day) if self.strategy_enabled else day==self.grant.value['trading_day']

    def reconciliation_snapshot(self,snapshot):return snapshot

    def _validate_snapshot(self,snapshot):
        if (snapshot.get('account_key')!=self.kernel.account_key or snapshot.get('gateway_identity')!=self.port.gateway_identity
                or snapshot.get('source_epoch')!=self.port.source_epoch or not self.day_permitted(snapshot.get('trading_day')) or not snapshot.get('complete')):
            raise TraderError('E_FACT_BARRIER','Current full simulated account snapshot is required')
        for table,key in (('orders','order_id'),('trades','trade_id'),('positions','contract_id')):
            rows=snapshot.get(table)
            if (not isinstance(rows,list) or any(not isinstance(row,dict) or not isinstance(row.get(key),str) or not row[key] for row in rows)
                    or len({row[key] for row in rows})!=len(rows)):
                raise TraderError('E_FACT_BARRIER','Full snapshot contains missing or duplicate native identities')

    def consumed(self):return any(c['plan'].get('approval_record_id')==self.grant.value['grant_id'] for c in self.kernel._staged.values())

    def arm(self):
        k=self.kernel;k._writable();k.wait_idle()
        ready = getattr(self.port.clock, 'entry_ready', None)
        if ready is not None and not ready():
            raise TraderError('E_CLOCK', 'Application UTC calibration is unavailable for opening')
        if (not self._configured or self.consumed() or k.recovery_required or k._has_unknown() or self.port.fault
                or not self.port.complete() or not self.grant.in_window(self.port.clock.now())):
            raise TraderError('E_NOT_READY','Simulation scope, time, quota or recovery gate blocks opening')
        k._state_revision+=1;k._authority_revision+=1
        authority=k._authority()|{'armed':True};generation=k._authority_revision
        result=k._submit('TX_AUTH','ONLINE_SIMULATION_ARMED',{'authority':authority,'grant_hash':self.grant.content_hash},
            lambda conn,seq:k._write_authority(conn,seq,authority),after=lambda:setattr(k,'_armed',generation==k._authority_revision))
        return result

    def validate_entry(self,plan):
        value=self.grant.value
        ready = getattr(self.port.clock, 'entry_ready', None)
        if ready is not None and not ready():
            raise TraderError('E_CLOCK', 'Application UTC calibration is unavailable for opening')
        if (not self._configured or plan.get('execution_scope')!='ONLINE_SIMULATION' or plan.get('schema_version')!='1.0'
                or plan.get('approval_record_id')!=value['grant_id'] or plan.get('contract_id') not in value['contracts']
                or plan.get('trading_day')!=value['trading_day'] or plan.get('initial_open_quantity')!=1 or plan.get('direction')!='LONG'
                or self.consumed() or not self.grant.in_window(self.port.clock.now()) or self.port.fault):
            raise TraderError('E_ONLINE_SCOPE','Manual one-lot simulation plan is outside its exact grant')
        if any(plan[field].get('enabled') for field in ('profit_protection','trailing_stop','partial_reduction','close_session','slippage_exit','data_gap_exit','emergency_exit')):
            raise TraderError('E_UNSUPPORTED','This online stage permits G0 fixed protection and the durable timed exit only')
        if not self.port.fresh(plan['contract_id']) or self.port.position(plan['contract_id'])!={'long':0,'short':0} or self.port.active_orders(plan['contract_id']):
            raise TraderError('E_NOT_READY','Selected online contract is stale or occupied')

    def mark_fill(self,cycle,order):
        if order['effect']=='OPEN' and 'online_deadline' not in cycle:
            now=time.monotonic_ns();reference=self.port.clock.now()
            stamps=[int(f['source_time_ns']) for o in self.port.observations() if o['child_order_id']==order['child_order_id'] for f in o['fills']]
            source=min(stamps) if stamps else None
            if source is not None:
                first=datetime.fromtimestamp(source//1_000_000_000,timezone.utc)+timedelta(microseconds=(source%1_000_000_000+999)//1000)
                if first>reference+timedelta(seconds=1):raise TraderError('E_CLOCK','Native fill is ahead of the verified reference clock')
            else:first=reference
            due=first+timedelta(seconds=60);remaining=max(0,int((due-reference).total_seconds()*1_000_000_000))
            cycle['online_deadline']={'first_fill_monotonic_ns':str(now),'first_fill_source_time_ns':str(source) if source is not None else None,'due_monotonic_ns':str(now+remaining),
                'due_reference_utc':due.isoformat().replace('+00:00','Z'),'boot_id':self.kernel.boot_id,
                'grant_id':self.grant.value['grant_id'],'hold_seconds':60}

    def fill_day(self,observation,fill):
        return self.grant.value['trading_day']

    def validate_native_order(self,observation):pass

    def native_observations(self):return self.port.observations()

    def historical_terminal(self,observation):return False

    def observe_native_orders(self):
        """Commit received facts without authorizing sends or lifting recovery.

        Day-transition recovery uses this same consumer while its persistent
        barrier remains set. The legacy manual owner keeps its original day.
        """
        k=self.kernel;k._writable()
        try:observations=self.native_observations()
        except TraderError as error:
            k._record_conflict('ONLINE_NATIVE_ORDER_CONFLICT',{'error_code':error.code});k.wait_idle();raise
        digest=content_hash(observations)
        if digest!=self._last_observations:
            # Validate immutable identities before committing or consuming facts.
            try:candidate=self._merge_observations(observations)
            except TraderError as error:
                k._record_conflict('ONLINE_NATIVE_FILL_IDENTITY_CONFLICT',{'error_code':error.code,'orders':observations});k.wait_idle();raise
            self.capture('ONLINE_ORDER_OBSERVATIONS',{'gateway_identity':self.port.gateway_identity,'source_epoch':self.port.source_epoch,'orders':observations})
            self._observed_orders=candidate
            for incoming in observations:
                observation=candidate[incoming['child_order_id']]
                child=observation['child_order_id']
                try:self.validate_native_order(observation)
                except TraderError as error:
                    k._record_conflict('ONLINE_NATIVE_ORDER_DAY_UNPROVED',{'child_order_id':child,'error_code':error.code})
                    k.wait_idle();continue
                proved_fills = []
                for fill in observation['fills']:
                    try:day=self.fill_day(observation,fill)
                    except TraderError as error:
                        k._record_conflict('ONLINE_NATIVE_DAY_UNPROVED',{'child_order_id':child,
                            'trade_key':fill['trade_key'],'source_time_ns':fill['source_time_ns'],'error_code':error.code})
                        k.wait_idle();continue
                    proved_fills.append((fill, day))
                proved = len(proved_fills) == len(observation['fills'])
                total = sum(fill['quantity'] for fill in observation['fills'])
                rejected = observation.get('rejected', False)
                if rejected and (observation['status'] != 'FINISHED' or observation['reported_fill_qty'] or total):
                    k._record_conflict('ONLINE_NATIVE_ORDER_REJECTION_CONFLICT', {'child_order_id':child})
                    k.wait_idle()
                    continue
                cycle, _ = k._find(child)
                order = next(order for order in cycle['orders'] if order['child_order_id'] == child)
                archived = self.archived_cycle(cycle)
                if archived and (observation['status'] != 'FINISHED' or observation['reported_fill_qty'] != order['filled_qty']):
                    self.flag_archived_order(cycle, order, observation)
                first_confirmation = None
                defer_entry_slippage = False
                opening = order['effect'] == 'OPEN' and cycle['plan']['schema_version'] == '1.3'
                initial_fill = not cycle['protection']['entry_filled_qty']
                needs_slippage = (cycle['plan']['slippage_exit']['enabled']
                                  and not cycle.get('entry_slippage_confirmation_seen', not initial_fill))
                notice = cycle.get('initial_open_confirmation')
                needs_notice = (cycle.get('entry_origin') == 'STRATEGY' and
                                (notice == {'status':'PENDING'} or (initial_fill and notice is None)))
                if opening and (needs_slippage or needs_notice):
                    defer_entry_slippage = not proved or total != observation['reported_fill_qty']
                    if not defer_entry_slippage and total > 0:
                        with localcontext() as context:
                            context.prec = 80
                            value = sum(Decimal(fill['price']) * fill['quantity'] for fill, _ in proved_fills)
                        first_confirmation = {'quantity': total, 'value': decimal_text(value)}
                for fill, day in proved_fills:
                    changed=k.record_fill(child,fill['trade_key'],fill['quantity'],fill['price'],trading_day=day,
                        native_day_evidence=fill.get('native_day_evidence'),native_trade_id=fill.get('native_trade_id'),
                        _initial_entry_confirmation=first_confirmation,
                        _defer_entry_slippage=defer_entry_slippage,_adapter_token=self.port.callback_token)
                    k.wait_idle()
                    if changed:
                        cycle,_=k._find(child);cid=cycle['position_cycle_id']
                        self._closed.discard(cid);self._entry_reconciled.discard(cid)
                if proved and observation['status']=='FINISHED' and total==observation['reported_fill_qty']:
                    terminal_key=(child,total)
                    if ((terminal_key not in self._terminal and (not archived or not order['terminal_confirmed']
                                                                 or order['reported_fill_qty'] != total))
                            or (rejected and order['broker_state'] != 'REJECTED')):
                        cycle,_=k._find(child);order=next(o for o in cycle['orders'] if o['child_order_id']==child)
                        status='FILLED' if total==order['original_qty'] else 'REJECTED' if rejected else 'CANCELLED'
                        k.confirm_terminal(child,status,total,_adapter_token=self.port.callback_token,
                            _historical_recovery=self.historical_terminal(observation))
                        k.wait_idle();self._terminal.add(terminal_key)
            self._last_observations=digest
        return observations

    def flag_archived_order(self, cycle, order, observation):
        k = self.kernel
        issue = {'child_order_id':order['child_order_id'],'original_grant_id':cycle['plan']['approval_record_id'],
                 'status':observation['status'],'reported_fill_qty':observation['reported_fill_qty']}
        if cycle.get('archived_order_review') == issue:
            return
        k._record_conflict('ONLINE_ARCHIVED_ORDER_CHANGED', issue)
        k.wait_idle()
        cycle['archived_order_review'] = issue
        cycle['protection'].update(reconciliation_required=True, block_new=True)
        if cycle['lifecycle'] == 'CLOSED':
            cycle['lifecycle'] = 'FLAT_PENDING_CONFIRM'
        k._recovery_pending.add(cycle['position_cycle_id'])
        cycle['revision'] += 1
        k._stage_cycle(cycle, 'TX_RECONCILE', 'ONLINE_ARCHIVED_ORDER_NEEDS_ORIGINAL_SCOPE')
        k.wait_idle()

    def tick(self):
        k=self.kernel;k._writable()
        if self.port.fault and not self._fault_recorded:
            details={'source_epoch':self.port.source_epoch}
            if self.strategy_enabled and any(cycle['lifecycle']!='CLOSED' for cycle in k._staged.values()):
                k._record_conflict('ONLINE_GATEWAY_DISCONTINUITY',details);k.wait_idle()
            else:
                k.pause();k.wait_idle();self.capture('ONLINE_GATEWAY_DISCONTINUITY',details)
            self._fault_recorded=True
        observations=self.observe_native_orders()
        self.recover_working(observations)
        if self.strategy_enabled and self.port.fault:return
        for cid,cycle in list(k._staged.items()):
            if self.archived_cycle(cycle):continue
            if self.strategy_enabled and cid in k._recovery_pending:continue
            if cid in self._closed and cycle['lifecycle']=='CLOSED':continue
            code=cycle['plan']['contract_id']
            if self.port.fault and self.port.complete() and self.port.fresh(code) and all(o['terminal_confirmed'] for o in cycle['orders'] if o['effect']=='OPEN'):
                self.reconcile(cid);self._fault_recorded=False
            if not self.strategy_enabled and not self.port.fault and self.port.fresh(code):
                market=self.port.market(code)
                if self._quotes.get(code)!=market['source_time']:
                    k.observe(cid,market['price'],bid1=market['bid1'],ask1=market['ask1'],_adapter_token=self.port.callback_token);k.wait_idle()
                    self._quotes[code]=market['source_time']
            cycle=k._staged[cid]
            settled=lambda order:(order['child_order_id'],order['filled_qty']) in self._terminal or order['send_state']=='DEFINITELY_NOT_SENT'
            if cid not in self._closed and not cycle['protection']['owned_qty'] and self.can_close(cycle) and all(settled(order) for order in cycle['orders']):
                self.reconcile(cid,close=True);self._closed.add(cid);self._entry_reconciled.add(cid);continue
            opening=[o for o in cycle['orders'] if o['effect']=='OPEN']
            if cid not in self._entry_reconciled and all((o['child_order_id'],o['filled_qty']) in self._terminal or o['send_state']=='DEFINITELY_NOT_SENT' for o in opening):
                self.reconcile(cid);self._entry_reconciled.add(cid)
            deadline=cycle.get('online_deadline')
            if deadline and cycle['protection']['owned_qty'] and cycle['protection'].get('target_remaining_qty') is None:
                due=(time.monotonic_ns()>=int(deadline['due_monotonic_ns']) if deadline['boot_id']==k.boot_id else
                     self.port.clock.now()>=datetime.fromisoformat(deadline['due_reference_utc'].replace('Z','+00:00')))
                if due:k.manual_close(cid,cycle['protection']['owned_qty']);k.wait_idle()
            cycle=k._staged[cid]
            if cid not in self._closed and not cycle['protection']['owned_qty'] and self.can_close(cycle) and all((o['child_order_id'],o['filled_qty']) in self._terminal or o['send_state']=='DEFINITELY_NOT_SENT' for o in cycle['orders']):
                self.reconcile(cid,close=True);self._closed.add(cid)

    def recover_working(self,observations):pass

    def can_close(self,cycle):return True

    def reconcile(self,cid,*,close=False,allow_working=False,clear_gateway_fault=True,_snapshot=None,_statistics_at_utc=None):
        k=self.kernel;k._writable()
        snapshot=self.port.snapshot() if _snapshot is None else _snapshot
        self._validate_snapshot(snapshot)
        snapshot=self.reconciliation_snapshot(snapshot)
        cycle=copy.deepcopy(k._staged[cid]);code=cycle['plan']['contract_id']
        if close and cycle.get('statistics_review_required'):
            raise TraderError('E_FACT_BARRIER','Late fills require an explicit statistics correction review')
        if allow_working and not self.strategy_enabled:raise TraderError('E_UNSUPPORTED','Working-order recovery is scoped to the DSV owner')
        orders=cycle['orders'] if close or self.strategy_enabled else [o for o in cycle['orders'] if o['effect']=='OPEN']
        if any((not o['terminal_confirmed'] and (close or (not allow_working and (not self.strategy_enabled or o['effect']=='OPEN'))))
               or o['reported_fill_qty'] not in (None,o['filled_qty']) or o['send_state']=='UNKNOWN' for o in orders):
            raise TraderError('E_FACT_BARRIER','Native terminal orders and all owned fills must agree')
        self.verify_foreign_orders(cycle,snapshot)
        self.verify_cycle_orders(cycle,orders,snapshot)
        self.verify_owned_position(code,snapshot)
        rid='sim-reconcile-'+uuid.uuid4().hex;cycle['entry_reconciliation_id']=rid
        cycle['online_reconciliation']=snapshot
        if close:
            if cycle['protection']['owned_qty']:raise TraderError('E_FACT_BARRIER','Owned cycle is not flat')
            cycle['lifecycle']='CLOSED'
        elif all(o['terminal_confirmed'] for o in cycle['orders'] if o['effect']=='OPEN'):
            decision=reduce_event(cycle['plan'],ProtectionState.from_dict(cycle['protection']),{'type':'ENTRY_TERMINAL_RECONCILED','owned_quantity':cycle['protection']['owned_qty'],
                'reconciliation_id':rid,'event_seq':k._sequence+1,'event_id':'sim-evidence-'+uuid.uuid4().hex},simulation_permit=self.permit(cycle['plan']))
            k._apply_decision(cycle,decision)
        else:
            cycle['protection'].update(reconciliation_required=False,block_new=True)
        cycle['revision']+=1
        statistics=k.statistics.prepare_close(cycle,at_utc=_statistics_at_utc) if close else None
        def accepted():
            k._recovery_pending.discard(cid);k.recovery_required=bool(k._recovery_pending) or k._has_unknown()
            if clear_gateway_fault and self.port.fault and self.port.fresh(code):self.port.fault=False
            k._progress_exits(cid)
        result=k._stage_cycle(cycle,'TX_RECONCILE','ONLINE_SIMULATION_RECONCILED',after=accepted,money_account=self.money_fact(snapshot),nominal_statistics=statistics);k.wait_idle();return result.result()

    def verify_owned_position(self,code,snapshot):
        position=next((p for p in snapshot['positions'] if p['contract_id']==code),{'long':0,'short':0})
        expected={direction:sum(c['protection']['owned_qty'] for c in self.kernel._staged.values()
                   if c['plan']['contract_id']==code and c['plan']['direction']==direction.upper()) for direction in ('long','short')}
        if position['long']!=expected['long'] or position['short']!=expected['short']:
            raise TraderError('E_OWNERSHIP','Native position differs from the exact owned quantity')

    def verify_foreign_orders(self,cycle,snapshot):
        code=cycle['plan']['contract_id'];k=self.kernel
        native={o['order_id']:o for o in snapshot['orders']}
        baseline=cycle.get('reviewed_foreign_baseline',cycle.get('online_entry_baseline',self.baseline or {}))
        original={o['order_id'] for o in baseline.get('orders',[])}
        owned={self.port.order_id(order['child_order_id']) for c in k._staged.values() for order in c['orders']}
        if any(o['contract_id']==code and o['order_id'] not in original|owned for o in snapshot['orders']):
            raise TraderError('E_OWNERSHIP','Concurrent foreign activity exists on the selected contract')
        if any(o['contract_id']==code and o['order_id'] not in owned and native.get(o['order_id'])!=o
               and not (getattr(self.grant,'multi_day',False) and baseline.get('trading_day','')<snapshot['trading_day']
                        and o.get('status')=='FINISHED' and o['order_id'] not in native)
               for o in baseline.get('orders',[])):
            raise TraderError('E_OWNERSHIP','A preexisting foreign order changed on the selected contract')

    def verify_cycle_orders(self,cycle,orders,snapshot):
        k=self.kernel;code=cycle['plan']['contract_id']
        native={o['order_id']:o for o in snapshot['orders']}
        for order in orders:
            if order['send_state']=='DEFINITELY_NOT_SENT' and not order['filled_qty']:continue
            item=native.get(self.port.order_id(order['child_order_id']))
            direction='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL'
            offset=order.get('native_offset','OPEN' if order['effect']=='OPEN' else 'CLOSE')
            status='FINISHED' if order['terminal_confirmed'] else 'ALIVE'
            if (item is None or item['status']!=status or item.get('contract_id')!=code
                    or item.get('direction')!=direction or item.get('offset')!=offset
                    or type(item.get('quantity')) is not int or item['quantity']!=order['original_qty']
                    or type(item.get('remaining')) is not int or not 0<=item['remaining']<=item['quantity']
                    or item.get('limit_price')!=order['limit_price'] or item['quantity']-item['remaining']!=order['filled_qty']):
                raise TraderError('E_FACT_BARRIER','Full simulated account snapshot does not corroborate owned order')
            trades=[t for t in snapshot['trades'] if t.get('order_id')==item['order_id']]
            facts=k.ledger.query('SELECT trade_key,quantity,price FROM fill_facts WHERE broker_order_key=?',(order['child_order_id'],))
            evidence={f['trade_key']:f for f in self._observed_orders.get(order['child_order_id'],{}).get('fills',[])}
            expected={f['trade_key']:{'quantity':f['quantity'],'price':f['price'],
                'source_time_ns':evidence.get(f['trade_key'],{}).get('source_time_ns')} for f in facts}
            actual={self.port.trade_key(t['trade_id']):{key:t.get(key) for key in ('quantity','price','source_time_ns')} for t in trades}
            if (actual!=expected or sum(f['quantity'] for f in facts)!=order['filled_qty']
                    or any(t.get('contract_id')!=code or t.get('direction')!=direction or t.get('offset')!=offset for t in trades)
                    or any(f['source_time_ns'] is None for f in expected.values())):
                raise TraderError('E_FACT_BARRIER','Full-day native trade identities and economics differ from committed owned fills')
    def plan(self,code,price,*,stop_points,take_profit_points):
        k=self.kernel;identity=self.grant.content_hash[:24]
        return {'schema_version':'1.0','plan_id':'sim-plan-'+uuid.uuid4().hex,'plan_revision':'1','account_key':k.account_key,
            'position_cycle_id':'sim-cycle-'+uuid.uuid4().hex,'contract_id':code,'trading_day':self.grant.value['trading_day'],'direction':'LONG',
            'theoretical_entry_price':price,'initial_open_quantity':1,'rule_set_id':k._configuration['rule_set_id'],
            'config_revision':str(k._config_revision),'config_hash':k._configuration['payload_hash'],'execution_scope':'ONLINE_SIMULATION',
            'approval_record_id':self.grant.value['grant_id'],'market_quality_policy_id':'sim-quality-'+identity,'price_field':'LAST',
            'fixed_stop_loss':{'enabled':True,'distance_points':stop_points},'fixed_take_profit':{'enabled':True,'distance_points':take_profit_points},
            **{key:{'enabled':False} for key in ('profit_protection','trailing_stop','partial_reduction','close_session','slippage_exit','data_gap_exit','emergency_exit')},
            'execution_policy':{'policy_id':'sim-execution-'+identity,'first_price_mode':'OPPONENT_LIMIT','cancel_check_interval_ms':5000,
                'max_child_order_attempts':3,'price_steps':[],'close_offset_policy_id':'sim-dce-close-'+identity}}
