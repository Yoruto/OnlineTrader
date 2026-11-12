"""Explicit-calendar DSV scheduler on the existing kernel owner.

The native simulation boundary must supply a separately bound calendar. This
module creates no connection, service, invented quote or matching engine.
UTC locates sessions; monotonic observations advance deadlines.
"""

import copy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
from pathlib import Path
import time

from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.domain.orders import OrderState
from online_trader.domain.values import quantity
from online_trader.errors import TraderError
from online_trader.execution.policy import select_price
from online_trader.gateway.ports import UnavailablePort
from online_trader.kernel.dsv_strategy import DsvStrategyRuntime
from online_trader.protection import ProtectionState, reduce_event
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.scheduling.clock import ClockSample, ClockGuard, SystemClock


def _ms(delta):
    return (delta.days*86400000000+delta.seconds*1000000+delta.microseconds)//1000


class ExpiredMarketQuote(TraderError):
    def __init__(self):
        super().__init__('E_FACT_BARRIER','Quote expired before the scheduler could accept it')


class ScheduledRuntime:
    def __init__(self,kernel,calendar,*,clock=None,profile=None,interval_ms=500,max_wall_drift_ms=1000):
        kernel._writable()
        if (not kernel.development and not getattr(kernel.simulation,'strategy_enabled',False)) or isinstance(kernel.port,UnavailablePort):
            raise TraderError('E_RULE_UNAPPROVED','Scheduled execution requires an isolated development port')
        if hasattr(kernel,'_scheduled_runtime'):raise TraderError('E_OWNER','This owner already has a scheduler')
        if not isinstance(calendar,TradingCalendar):raise TraderError('E_CALENDAR','Expected an immutable explicit calendar')
        quantity(interval_ms,positive=True);quantity(max_wall_drift_ms)
        self.kernel=kernel;self.calendar=calendar;self.clock=clock or SystemClock()
        self.guard=ClockGuard(max_wall_drift_ms=max_wall_drift_ms)
        self.interval_ms=interval_ms;self.max_wall_drift_ms=max_wall_drift_ms
        self._sample=None;self._busy=False;self._enabled=False;self._fault=None;self._fault_written=False
        self._quotes={};self._ages={};self._anchors={};self._driven={};self._gaps={};self._scope_faults=set()
        for row in kernel.ledger.query("SELECT event_type,payload_json FROM event_journal WHERE event_type IN ('SCHEDULER_FACT_BARRIER','CLOCK_RESET_REQUIRES_FRESH_FACTS') ORDER BY event_seq"):
            data=strict_loads(row['payload_json']);data=data.get('data',data)
            if row['event_type']=='CLOCK_RESET_REQUIRES_FRESH_FACTS':self._fault=None;self._fault_written=False
            elif any(reason.startswith(('CLOCK_','E_CLOCK')) for reason in data.get('reasons',[])):
                self._fault=list(data['reasons']);self._fault_written=True
        self.strategy=DsvStrategyRuntime(kernel,profile=profile)
        kernel._scheduled_runtime=self

    def descriptor(self):
        from online_trader.scheduling import clock,calendar
        return {'calendar_id':self.calendar.calendar_id,'calendar_version':self.calendar.calendar_version,
                'calendar_hash':self.calendar.content_hash,'interval_ms':self.interval_ms,
                'max_wall_drift_ms':self.max_wall_drift_ms,'time_policy':'DSV_TIME/1',
                'scheduler_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'calendar':self.calendar.to_record(),
                'source_hashes':{name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                    for name,module in (('clock',clock),('calendar',calendar))}}

    def business_state(self):
        return {'calendar_hash':self.calendar.content_hash,'clock_fault':copy.deepcopy(self._fault) if self._fault_written else None}

    def activate(self):
        result=self.strategy.activate();self._enabled=True;self.preflight();return result

    def preflight(self):
        if not self._enabled or self._busy:return
        try:
            observed=self.guard.observe(self.clock.sample())
            if observed['valid']:self._sample=ClockSample.from_record({'now_utc':observed['now_utc'],'monotonic_ns':observed['monotonic_ns']})
            elif self._fault is None:self._fault=list(observed['reasons'])
        except TraderError as error:
            if self._fault is None:self._fault=[error.code]
        except Exception:
            if self._fault is None:self._fault=['CLOCK_SOURCE_FAILED']
        if self._fault:
            k=self.kernel;k._armed=False;k.contexts.revoke_all()
            if not self._fault_written:
                k._recovery_pending.update(c['position_cycle_id'] for c in k._staged.values() if c['lifecycle']!='CLOSED')
                k.recovery_required=bool(k._recovery_pending)

    def quote_age_ns(self,received):
        real=max(0,time.monotonic_ns()-int(received));anchor=self._ages.get(str(received))
        if anchor is None:return self.kernel.max_quote_age_ms*1000000+1
        if self._sample is None:return real
        return max(real,self._sample.monotonic-anchor,0)

    def _register(self,received,sample,source_age_ms=0):
        self._ages[str(received)]=sample.monotonic-source_age_ms*1000000
        if len(self._ages)>4096:
            # Only boot-local freshness indexes are bounded; original facts and
            # unknown orders stay durable. Evicted quotes become unexecutable.
            keep={str(q['received_ns']) for q in self._quotes.values()}|{str(received)}
            keep.update(str(row['received_ns']) for row in self.strategy._latest.values())
            keep.update(str(c['last_quote']['received_ns']) for c in self.kernel._staged.values()
                        if c['lifecycle']!='CLOSED' and c.get('last_quote'))
            self._ages={key:value for key,value in self._ages.items() if key in keep}

    def scope_current(self,contract,day,session_id=None):
        if self._fault or not self._enabled or self._sample is None:return False
        at=self.calendar.query(contract,self._sample.to_record()['now_utc'])
        return bool(at['complete'] and at['is_trading'] and at['trading_day']==day and
                    (session_id is None or at['session']['session_id']==session_id))

    def cycle_day_current(self,cycle,day):
        simulation=self.kernel.simulation
        return cycle['plan']['trading_day']==day or bool(getattr(simulation,'strategy_enabled',False)
            and simulation.exit_day_ready(cycle,day))

    def _event(self,event):
        event=copy.deepcopy(event);at=self.calendar.query(event['contract_id'],event['source_time_utc'])
        if not at['complete'] or not at['is_trading']:raise TraderError('E_CALENDAR_INCOMPLETE','Quote lacks a complete active calendar window')
        session=at['session']
        if event['trading_day']!=session['trading_day']:raise TraderError('E_SCOPE','Quote cabinet-day label differs from the explicit calendar')
        for key,value in (('session_id',session['session_id']),('session_open_utc',session['open_utc']),('session_close_utc',session['close_utc']),
                          ('is_opening_session',session['is_opening_session'])):
            if key in event and event[key]!=value:raise TraderError('E_SCOPE','Quote session differs from its calendar evidence')
            event[key]=value
        event['calendar_complete']=True
        event['session_policy']={'schema_version':'DSV_TIME/1','calendar_hash':self.calendar.content_hash,
            'force_close_at_end':session['force_close_at_end'],'entry_close_utc':at['next_forced_close_utc']}
        return event

    def on_market(self,event,account,instrument,*,_allow_entry=True):
        self.kernel._writable();self.preflight()
        if not self._enabled or self._fault:raise TraderError('E_CLOCK_FAULT','Activate and resolve the scheduler clock before accepting market facts')
        event=self._event(event);sample=self._sample
        if not self.scope_current(event['contract_id'],event['trading_day'],event['session_id']):
            raise TraderError('E_SCOPE','The quote no longer belongs to the current proved trading session')
        source=datetime.fromisoformat(event['source_time_utc'])
        age=_ms(sample.utc-source)
        if age < -self.max_wall_drift_ms:
            raise TraderError('E_FACT_BARRIER','Quote source time is not fresh against the clock evidence')
        if age>self.kernel.max_quote_age_ms:
            raise ExpiredMarketQuote()
        contract=event['contract_id'];previous=self._quotes.get(contract)
        signature=(event['source_epoch'],event['sequence'],content_hash(event))
        if previous and signature[:2]==previous['signature'][:2] and signature!=previous['signature']:
            raise TraderError('E_DUPLICATE_CONFLICT','Quote source identity changed content')
        fresh=previous is None or signature[:2]!=previous['signature'][:2]
        received=time.monotonic_ns()
        if fresh:
            self._quotes[contract]={'event':event,'instrument':copy.deepcopy(instrument),'signature':signature,
                'received_ns':received,'sample':sample,'source_age_ms':max(0,age)}
            self._register(received,sample,max(0,age))
        self._busy=True
        try:
            future=self.strategy.on_market(event,account,instrument,_allow_entry=_allow_entry)
            latest=self.strategy._latest.get((event['contract_id'],event['trading_day']))
            if fresh and latest:self._register(latest['received_ns'],sample,max(0,age))
            for cycle in self.kernel._staged.values():
                if cycle['plan']['contract_id']==contract and self.cycle_day_current(cycle,event['trading_day']):
                    if fresh:
                        prior=cycle.get('execution',{}).get('elapsed_ms',0)
                        origin=(cycle.get('strategy_metadata') or {}).get('candidate',{}).get('source_time_utc')
                        if origin is not None:prior=max(prior,_ms(sample.utc-datetime.fromisoformat(origin)))
                        self._anchors[cycle['position_cycle_id']]=(sample.monotonic,prior)
                    if fresh and (not getattr(self.kernel.simulation,'strategy_enabled',False) or 'execution' in cycle):
                        self._driven[(cycle['position_cycle_id'],'clock')]=sample.monotonic
            return future
        except BaseException:
            if fresh:
                if previous is None:self._quotes.pop(contract,None)
                else:self._quotes[contract]=previous
            raise
        finally:self._busy=False

    def binding_for(self,plan,*,entry_time_utc):
        at=self.calendar.query(plan['contract_id'],entry_time_utc)
        if not at['complete'] or not at['is_trading'] or at['trading_day']!=plan['trading_day']:
            raise TraderError('E_CALENDAR_INCOMPLETE','Original entry lacks complete calendar coverage')
        return {'calendar_hash':self.calendar.content_hash,'calendar_id':self.calendar.calendar_id,
            'calendar_version':self.calendar.calendar_version,'plan_hash':content_hash(plan),
            'entry_time_utc':entry_time_utc,'forced_close_utc':at['next_forced_close_utc'],
            'calendar_reference':plan['close_session']['calendar_version'],
            'schedule_policy_id':plan['close_session']['schedule_policy_id'],
            'gap_policy_id':plan['data_gap_exit'].get('data_quality_policy_id')}

    def bind_existing_cycle(self,cid):
        k=self.kernel;k._writable()
        if cid in k._recovery_pending or k._has_unknown():raise TraderError('E_FACT_BARRIER','Reconcile original exposure before resolving its calendar reference')
        cycle=copy.deepcopy(k._staged[cid])
        if cycle['plan']['schema_version'] not in ('1.2','1.3'):raise TraderError('E_UNSUPPORTED','This scheduler drives the DSV execution contract')
        candidate=(cycle.get('strategy_metadata') or {}).get('candidate') or {}
        origin=candidate.get('source_time_utc')
        if origin is None:raise TraderError('E_UNSUPPORTED','Original entry-time evidence is required for automatic calendar management')
        binding=self.binding_for(cycle['plan'],entry_time_utc=origin)
        if cycle.get('schedule_binding') is not None and cycle['schedule_binding']!=binding:
            raise TraderError('E_PLAN_BINDING','An existing calendar binding cannot be replaced')
        cycle['schedule_binding']=binding;cycle['revision']+=1
        return k._stage_cycle(cycle,'TX_PROTECT','SCHEDULE_REFERENCE_BOUND')

    def _sequence(self,cycle):
        old=cycle.get('execution') or {}
        return max(self.kernel.kernel_epoch,old.get('source_epoch',0)),old.get('sequence',0)+1

    def execution_event(self,cycle,event,instrument):
        cached=self._quotes[event['contract_id']];sample=self._sample
        old=cycle.get('execution') or {};anchor=self._anchors.get(cycle['position_cycle_id'])
        if anchor is None:
            origin=(cycle.get('strategy_metadata') or {}).get('candidate',{}).get('source_time_utc',event['source_time_utc'])
            elapsed=max(old.get('elapsed_ms',0),_ms(sample.utc-datetime.fromisoformat(origin)))
        else:elapsed=anchor[1]+max(0,(sample.monotonic-anchor[0])//1000000)
        at=self.calendar.query(event['contract_id'],sample.to_record()['now_utc'])
        return self._execution(cycle,cached,at,elapsed)

    def _execution(self,cycle,cached,at,elapsed):
        epoch,seq=self._sequence(cycle);event=cached['event'];sample=self._sample
        current=at['session'];since=at['since_open_ms'] if current else 0
        policy=cycle['plan']['execution_policy'];deadline=cycle['schedule_binding']['forced_close_utc']
        close_due=deadline is not None and _ms(datetime.fromisoformat(deadline)-sample.utc)<=cycle['plan']['close_session']['advance_seconds']*1000
        fresh=self.quote_age_ns(cached['received_ns'])<=self.kernel.max_quote_age_ms*1000000
        # Missing entry history blocks new signals, not fresh-price management
        # of an already proved owned position.
        market={'complete':at['complete'],
            'fresh':fresh,'is_trading':at['is_trading'] is True and event['is_trading'],'quality':event['quality'],
            'last':event['last'],'bid1':event['bid1'],'ask1':event['ask1'],'tick_size':event['tick_size'],
            'upper_limit':cached['instrument'].get('upper_limit'),'lower_limit':cached['instrument'].get('lower_limit'),
            'since_open_ms':since,'near_open':bool(current and current['is_opening_session'] and 0<=since<=policy['chase_window_ms'])}
        result={'event_id':'scheduled-execution-'+content_hash({'cycle':cycle['position_cycle_id'],'clock':sample.to_record(),'sequence':seq}),
            'source_epoch':epoch,'sequence':seq,'elapsed_ms':quantity(elapsed),'trading_day':at['trading_day'] or cycle['plan']['trading_day'],
            'close_due':close_due,'market':market,'calendar_hash':self.calendar.content_hash,
            'observed_at_utc':sample.to_record()['now_utc']}
        if 'native_time_ns' in event:
            result['market_identity']={'contract_id':event['contract_id'],'native_time_ns':event['native_time_ns'],
                'native_tick_id':event['native_tick_id'],'quote_hash':content_hash({key:event[key] for key in
                    ('last','bid1','ask1','day_high','day_low','native_time_ns','native_tick_id')})}
        if close_due:
            due=datetime.fromisoformat(deadline)-timedelta(seconds=cycle['plan']['close_session']['advance_seconds'])
            result['close_start_elapsed_ms']=max(0,elapsed-max(0,_ms(sample.utc-due)))
        return result

    def received_for(self,contract):return self._quotes[contract]['received_ns']

    def _block(self,reason,cid=None):
        k=self.kernel;k._armed=False;k.contexts.revoke_all()
        ids=[cid] if cid else [c['position_cycle_id'] for c in k._staged.values() if c['lifecycle']!='CLOSED']
        k._recovery_pending.update(ids);k.recovery_required=bool(k._recovery_pending)
        k._state_revision+=1;k._authority_revision+=1;authority=k._authority()
        return k._submit('TX_RECONCILE','SCHEDULER_FACT_BARRIER',{'reasons':[reason],'cycles_affected':ids,
            'clock':None if self._sample is None else self._sample.to_record(),'authority':authority},
            lambda conn,seq:k._write_authority(conn,seq,authority))

    def poll(self):
        k=self.kernel;k._check_owner()
        if not self._enabled or self._busy or k._failed:return {'status':'IDLE'}
        self.preflight()
        if self._fault:
            if not k._pending and not self._fault_written:
                self._fault_written=True;self._block(self._fault[0])
            return {'status':'CLOCK_FAULT','reasons':list(self._fault)}
        if k._pending:return {'status':'DEFERRED'}
        sample=self._sample
        self._busy=True
        try:
            for cycle in tuple(k._staged.values()):
                cid=cycle['position_cycle_id'];binding=cycle.get('schedule_binding')
                if cycle['lifecycle']=='CLOSED' or not binding:continue
                cached=self._quotes.get(cycle['plan']['contract_id'])
                if cached is None:continue
                at=self.calendar.query(cycle['plan']['contract_id'],sample.to_record()['now_utc'])
                if (getattr(k.simulation,'strategy_enabled',False) and k.simulation.grant.multi_day
                        and at['is_trading'] and k.simulation.grant.permits_day(at['trading_day'])
                        and not self.cycle_day_current(cycle,at['trading_day'])):
                    # The original owner is reconciling this new cabinet day.
                    # Keep the old binding; do not latch a fabricated calendar
                    # failure while the exact day recovery is still pending.
                    continue
                if (not at['complete'] or binding!=self.binding_for(cycle['plan'],entry_time_utc=binding['entry_time_utc']) or
                        (at['is_trading'] and not self.cycle_day_current(cycle,at['trading_day']))):
                    if cid not in self._scope_faults:
                        self._scope_faults.add(cid);self._block('SCHEDULE_SCOPE_UNKNOWN',cid)
                    return {'status':'CALENDAR_UNKNOWN'}
                if cid in k._recovery_pending:continue
                current=at['session'];policy=cycle['plan']['data_gap_exit']
                if current is not None and policy['enabled'] and self._gaps.get(cid)!=current['session_id']:
                    elapsed=at['since_open_ms']
                    quiet=min(elapsed,max(0,(sample.monotonic-cached['sample'].monotonic)//1000000))
                    if elapsed>=60001 and quiet>=policy['active_trading_gap_ms']:
                        self._gaps[cid]=current['session_id'];self._gap(cycle,cached,quiet,elapsed)
                        return {'status':'PROCESSED','kind':'DATA_GAP'}
                last=self._driven.get((cid,'clock'))
                if last is not None and sample.monotonic-last<self.interval_ms*1000000:continue
                self._driven[(cid,'clock')]=sample.monotonic
                anchor=self._anchors.setdefault(cid,(sample.monotonic,cycle.get('execution',{}).get('elapsed_ms',0)))
                elapsed=anchor[1]+max(0,(sample.monotonic-anchor[0])//1000000)
                tick=self._execution(cycle,cached,at,elapsed)
                k.execution.tick(cid,tick,_observe_quote=False,_market_received_ns=cached['received_ns'])
                return {'status':'PROCESSED','kind':'CLOCK'}
            return {'status':'IDLE'}
        finally:self._busy=False

    def _gap(self,cycle,cached,gap,elapsed):
        k=self.kernel;policy=cycle['plan']['data_gap_exit']
        market=self._execution(cycle,cached,self.calendar.query(cycle['plan']['contract_id'],self._sample.to_record()['now_utc']),
                               cycle.get('execution',{}).get('elapsed_ms',0))['market']
        legal=select_price(k.execution.side(cycle,'REDUCE'),market)['allowed']
        fact={'type':'DATA_GAP','active_trading':True,'gap_ms':gap,'session_elapsed_ms':elapsed,
            'policy_authorized':True,'policy_id':policy['data_quality_policy_id'],'legal_price_available':legal,
            'event_seq':k._sequence+1,'event_id':'scheduled-gap-'+content_hash({'cycle':cycle['position_cycle_id'],'clock':self._sample.to_record()})}
        d=reduce_event(cycle['plan'],ProtectionState.from_dict(cycle['protection']),fact,
                       simulation_permit=k.simulation.permit(cycle['plan']) if k.simulation else None)
        k._apply_decision(cycle,d);cycle['last_quote']=None;cycle['revision']+=1
        cycle['scheduler_state']={'last_gap_session':self.calendar.query(cycle['plan']['contract_id'],self._sample.to_record()['now_utc'])['session']['session_id'],
                                  'clock':self._sample.to_record(),'gap_ms':gap}
        k._stage_cycle(cycle,'TX_PROTECT','SCHEDULED_DATA_GAP',after=lambda:k._progress_exits(cycle['position_cycle_id']))

    def reset_clock(self):
        k=self.kernel;k._writable()
        if k._pending or k.recovery_required or k._has_unknown():raise TraderError('E_FACT_BARRIER','Reconcile original management facts before resetting the clock')
        self.guard.reset(self.clock.sample());self._fault=None;self._fault_written=False
        self._quotes.clear();self._ages.clear();self._anchors.clear();self._driven.clear();self._gaps.clear()
        self.strategy._latest.clear()
        k._armed=False;k.contexts.revoke_all()
        authority=k._authority()
        return k._submit('TX_RECONCILE','CLOCK_RESET_REQUIRES_FRESH_FACTS',{'authority':authority},
                         lambda conn,seq:k._write_authority(conn,seq,authority))

    def status(self):
        return {'enabled':self._enabled,'clock_fault':copy.deepcopy(self._fault),'calendar_hash':self.calendar.content_hash,
                'cached_contracts':sorted(self._quotes),'armed':self.kernel._armed,'broker_connected':False}
