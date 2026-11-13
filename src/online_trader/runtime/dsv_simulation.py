"""Owner-driven finite DSV session; connections occur only in explicit run()."""
from datetime import datetime,timezone
from pathlib import Path
import json
import time

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import strict_loads,canonical_bytes,content_hash
from online_trader.configuration.files import publish_new_bytes
from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import DsvFactFeed,utc
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.gateway.simnow import strategy_port
from online_trader.kernel import Kernel
from online_trader.kernel.scheduler import ExpiredMarketQuote,ScheduledRuntime
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.simulation import SimulationSession
from online_trader.runtime.instance import AccountLock
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.scheduling.reference import measure_reference
from online_trader.scheduling.reference import clock_observation
from online_trader.runtime.comparison import ComparisonRecorder,verify_dsv_profile


class NativeReferenceClock:
    def __init__(self,port):self.port=port
    def sample(self):return {'now_utc':utc(self.port.clock.now()),'monotonic_ns':str(time.monotonic_ns())}


class DsvSimulationSession(SimulationSession):
    def status(self,phase,**values):
        try:return super().status(phase,**values)
        except OSError:
            self.kernel._comparison_fault='E_SESSION_STATUS_WRITE'
            if self.kernel._armed and not self.kernel._failed:
                self.kernel.pause();self.kernel.wait_idle()
            try:self.emit({'phase':phase,'error_code':'E_SESSION_STATUS_WRITE','owned_protection_continues':True})
            except Exception:pass

    def __init__(self,kernel,output,*,profile,calendar,clock=None,emit=None,dsv_config=None,dsv_log_dir=None):
        if not getattr(kernel.simulation,'strategy_enabled',False):
            raise TraderError('E_ONLINE_SCOPE','A manual simulation owner cannot activate the DSV runtime')
        if profile.content_hash!=kernel.simulation.grant.value['profile_hash'] or calendar.content_hash!=kernel.simulation.grant.value['calendar_hash']:
            raise TraderError('E_ONLINE_SCOPE','Session settings and calendar differ from the original strategy scope')
        super().__init__(kernel,output,emit=emit,profile=profile)
        self.driver=ScheduledRuntime(kernel,calendar,profile=profile,clock=clock or NativeReferenceClock(self.port))
        self.driver.activate();kernel.wait_idle()
        self.feed=DsvFactFeed(self.port,profile,calendar);self.last_decisions={};self._primed=False
        self.owner.manual_native_facts=self.feed.facts
        self._last_funds_capture=0
        self._source_epoch=self.port.source_epoch
        self._deferred_session_paths={}
        self._arm_request_revision=None
        self._calendar_anchor=utc(self.port.clock.now())
        self._day_barriers=set()
        self._last_day_observations=None
        original_days=[c['plan']['trading_day'] for c in kernel._staged.values() if c['lifecycle']!='CLOSED']
        self._managed_day=min(original_days) if original_days else self.port.snapshot()['trading_day']
        self._rollover_key=None;self._day_resume_revision=None
        self._incomplete_day_barrier=False
        for row in kernel.ledger.query("SELECT payload_json FROM event_journal WHERE event_type='ONLINE_DSV_DAY_CHANGE_REQUIRES_RECONCILIATION'"):
            record=strict_loads(row['payload_json']);data=record.get('data',record)
            self._day_barriers.add(data['conflict']['transition_hash'])
        self.comparison=None;self.comparison_error=None
        kernel._comparison_profile_source=dsv_config
        try:self.comparison=ComparisonRecorder(kernel,output,profile,calendar,dsv_config=dsv_config,dsv_log_dir=dsv_log_dir)
        except Exception as error:self._comparison_failed(error)

    def _comparison_failed(self,error):
        self.comparison_error=getattr(error,'code','E_COMPARISON_WRITE')
        self.kernel._comparison_fault=self.comparison_error
        if self.kernel._armed and not self.kernel._failed:
            self.kernel.pause();self.kernel.wait_idle()
        try:self.status('COMPARISON_REQUIRES_ATTENTION',error_code=self.comparison_error)
        except Exception:pass  # A full output disk must not interrupt owned exits.

    def _comparison(self,method,*args):
        if self.comparison is None:return
        try:getattr(self.comparison,method)(*args)
        except Exception as error:
            # Capture is auxiliary. Stop new risk but always finish this owner
            # turn, so a disk/log problem cannot remove owned protection.
            self._comparison_failed(error)

    def close(self):
        super().close()
        self._comparison('poll')
        if self.comparison is not None:self.comparison.close()

    def check_day_transition(self):
        """Persist a day-change barrier, retaining every original exit/UNKNOWN.

        A civil midnight never rolls counters. A new explicitly proved cabinet
        day requires original native reconciliation and fresh entry authority.
        """
        if self.grant.multi_day:return self._check_multi_day()
        now=utc(self.port.clock.now());changes=[]
        for code in self.grant.value['contracts']:
            transition=self.driver.calendar.transition(code,self._calendar_anchor,now)
            current=self.driver.calendar.query(code,now)
            if transition['changed'] or current['is_trading'] and current['trading_day']!=self.grant.value['trading_day']:
                changes.append({'contract_id':code,'old_day':self.grant.value['trading_day'],
                    'current_day':current['trading_day'],'days':transition['days'],'calendar_hash':self.driver.calendar.content_hash})
        if changes:
            observations=self.port.observations()
            digest=content_hash([{k:v for k,v in row.items() if k!='days'} for row in changes])
            if digest not in self._day_barriers:
                self.kernel._record_conflict('ONLINE_DSV_DAY_CHANGE_REQUIRES_RECONCILIATION',{'transition_hash':digest,'changes':changes,
                    'native_observations':observations})
                self.kernel.wait_idle();self._day_barriers.add(digest)
            observation_hash=content_hash({'orders':observations,'source_epoch':self.port.source_epoch})
            if observation_hash!=self._last_day_observations:
                # Retain late/changed native evidence without falsely labelling
                # it with the old grant day or replaying any uncertain send.
                self.owner.capture('ONLINE_DSV_DAY_CHANGE_OBSERVATIONS',{'orders':observations,'source_epoch':self.port.source_epoch,
                    'observation_hash':observation_hash,'requires_explicit_reconciliation':True})
                self._last_day_observations=observation_hash
            self._arm_request_revision=None
        self._calendar_anchor=now
        return bool(changes)

    def _check_multi_day(self):
        k=self.kernel;snapshot=self.port.snapshot();day=snapshot['trading_day']
        if not snapshot['complete']:
            self.owner.days.invalidate();self._day_resume_revision=None
            self._rollover_key=None
            if not self._incomplete_day_barrier:
                k._record_conflict('ONLINE_DSV_DAY_SCOPE_INCOMPLETE',{'trading_day':day,'source_epoch':self.port.source_epoch})
                k.wait_idle();self._incomplete_day_barrier=True
            return True
        self._incomplete_day_barrier=False
        if (day==self._managed_day and self.port.source_epoch==self._source_epoch and not self.port.fault
                and self.owner.days.ready(day)):return False
        key=(day,self.port.source_epoch)
        if self._rollover_key!=key:
            resume=(day!=self._managed_day and self.port.source_epoch==self._source_epoch and not self.port.fault
                and k._armed and self.grant.value['resume_entries_on_day_change'])
            self.owner.days.invalidate()
            # Old cached prices cannot describe the new session. Preserve
            # durable protection, but wait for this day's real fresh quote.
            self.driver._quotes.clear();self.driver._anchors.clear()
            k._record_conflict('ONLINE_DSV_DAY_CHANGE_REQUIRES_RECONCILIATION',{
                'transition_hash':content_hash({'old_day':self._managed_day,'current_day':day,'source_epoch':self.port.source_epoch}),
                'changes':[{'old_day':self._managed_day,'current_day':day}],
                'native_observations':self.port.observations()})
            k.wait_idle();self._rollover_key=key
            self._day_resume_revision=k._authority_revision if resume else None
            self._arm_request_revision=None
        if self._day_resume_revision!=k._authority_revision:self._day_resume_revision=None
        try:ready=self.owner.days.reconcile(day)
        except TraderError as error:
            if error.code not in {'E_FACT_BARRIER','E_OWNERSHIP','E_CALENDAR_INCOMPLETE'}:raise
            return True
        if not ready:return True
        resume=self._day_resume_revision==k._authority_revision
        self._managed_day=day;self._calendar_anchor=utc(self.port.clock.now())
        self.prime(manage_owned=True);self._source_epoch=self.port.source_epoch
        if resume:self._arm_request_revision=k._authority_revision
        self._day_resume_revision=None
        return False

    def request_initial_arm(self):
        if self.kernel._staged:raise TraderError('E_NOT_READY','A restored ledger manages original exits only')
        self._arm_request_revision=self.kernel._authority_revision

    def _market_batches(self):
        if self.kernel._staged or not self.driver.strategy._states:
            return self.feed.batches()
        return self.feed.batches(resume_states=self.driver.strategy._states,calendar_event=self.driver._event)

    def prime(self,*,manage_owned=False):
        """Rebuild available history with zero signal execution and no arming."""
        self.kernel.pause();self.kernel.wait_idle()
        self._deferred_session_paths.clear()
        now=utc(self.port.clock.now())
        for code in self.grant.value['contracts']:
            at=self.driver.calendar.query(code,now)
            upcoming=at['next_session']
            if not at['complete'] or at['is_trading'] or upcoming is None:continue
            day,session=upcoming['trading_day'],upcoming['session_id']
            state=self.driver.strategy._states.get((code,day))
            if state and state['source_epoch']<self.port.source_epoch and state['session_id']!=session:
                self._deferred_session_paths[code]=(day,session)
        rebuilt={}
        for code,events in self._market_batches():
            if events and all(e['path_complete'] for e in events):
                first=events[0]
                state=self.driver.strategy._states.get((code,first['trading_day']))
                extends_checkpoint=(state is not None and not state['coverage_lost']
                    and (state['source_epoch']==first['source_epoch'] or
                         (code,first['source_epoch']) in self.feed.resume_anchors)
                    and state['session_id']==first['session_id']
                    and state['last_sequence']+1==first['sequence'])
                rebuilt[code]=self._prime_path(code,events,extend=True) if extends_checkpoint else self._prime_path(code,events)
                self._deferred_session_paths.pop(code,None)
        # Replaying a long session can outlive the freshness budget. Catch up
        # the native path first, then register a fresh quote for execution.
        catchup_deadline=time.monotonic()+8
        while rebuilt and time.monotonic()<catchup_deadline:
            self.port.poll_updates(.2)
            pending=dict(self.feed.batches())
            for code,event in tuple(rebuilt.items()):
                suffix=pending.get(code,[])
                if (suffix and all(row['path_complete'] for row in suffix)
                        and suffix[0]['source_epoch']==event['source_epoch']
                        and suffix[0]['session_id']==event['session_id']):
                    event=self._prime_path(code,suffix,extend=True)
                    rebuilt[code]=event
                now=self.port.clock.now()
                age=(now-datetime.fromisoformat(event['source_time_utc'])).total_seconds()
                at=self.driver.calendar.query(code,utc(now))
                if (event['source_epoch']!=self.port.source_epoch or not 0<=age<=self.kernel.max_quote_age_ms/1000
                        or not at['is_trading'] or at['session']['session_id']!=event['session_id']):continue
                account,instrument=self.feed.facts(event,self.kernel)
                try:future=self.driver.on_market(event,account,instrument,_allow_entry=False)
                except ExpiredMarketQuote:continue
                self.kernel.wait_idle();future.result()
                del rebuilt[code]
                if manage_owned:
                    for cycle in tuple(self.kernel._staged.values()):
                        if (cycle['plan']['contract_id']!=code or cycle['lifecycle']=='CLOSED'
                                or not self.owner.exit_day_ready(cycle,event['trading_day'])):continue
                        tick=self.driver.execution_event(cycle,event,instrument)
                        future=self.kernel.execution.tick(cycle['position_cycle_id'],tick,
                            _market_received_ns=self.driver.received_for(code))
                        self.kernel.wait_idle();future.result()
        self._primed=True
        self._comparison('poll')
        self.status('DSV_PRIMED_DISARMED',strategy=self.driver.strategy.status())

    def _prime_path(self,code,events,*,extend=False,preserve_authority=False):
        enriched=[self.driver._event(event) for event in events]
        anchor_key=(code,events[0]['source_epoch'])
        evidence='native-path-'+self.kernel.boot_id+'-'+str(events[0]['source_epoch'])+'-'+code+'-'+content_hash(enriched)[:16]
        future=self.driver.strategy.rebuild_observation(enriched,evidence_id=evidence,extend=extend,
            _preserve_authority=preserve_authority,_resume_anchor=self.feed.resume_anchors.get(anchor_key))
        self.kernel.wait_idle();future.result()
        self.feed.resume_anchors.pop(anchor_key,None)
        self.feed.accept(events[-1])
        return events[-1]

    def _extend_late_path(self,code,events):
        first=events[0];state=self.driver.strategy._states.get((code,first['trading_day']))
        if (state is not None and not state['coverage_lost'] and state['session_id']==first['session_id']
                and state['source_epoch']==first['source_epoch'] and state['last_sequence']+1==first['sequence']
                and all(event['path_complete'] for event in events)):
            self._prime_path(code,events,extend=True,preserve_authority=True)

    def _rebuild_deferred_paths(self,batches):
        rebuilt=set()
        for code,events in batches:
            first=events[0]
            expected=self._deferred_session_paths.get(code)
            if expected==(first['trading_day'],first['session_id']) and all(e['path_complete'] for e in events):
                # Priming during a break could not prove the next session yet.
                # Rebuild its first complete batch with the usual disarm.
                self._prime_path(code,events)
                del self._deferred_session_paths[code]
                rebuilt.add(code)
        return rebuilt

    def _quote_expired(self,event):
        age=(self.port.clock.now()-datetime.fromisoformat(event['source_time_utc'])).total_seconds()
        return age>self.kernel.max_quote_age_ms/1000

    def step(self):
        k=self.kernel
        try:
            self._refresh_reference();self.port.poll_updates(.05)
            if not self.port.entry_clock_ready() and k._armed:
                k.pause();k.wait_idle()
                self.owner.capture('ONLINE_WINDOWS_CLOCK_ALIGNMENT_LOST',{'clock':clock_observation(self.port.clock)})
            if self.check_day_transition():
                # Receive proved original fills even while cross-day execution
                # and entry remain behind the durable reconciliation barrier.
                self.owner.observe_native_orders();k.wait_idle()
                self.status('DSV_DAY_CHANGE_ORIGINAL_EXITS_NEED_RECONCILIATION',armed=False)
                return
            if self.port.fault and not self.port.complete():
                self.owner.tick();k.wait_idle()
                self.status('DSV_WAITING_GATEWAY_SYNC',armed=False)
                return
            if self.port.source_epoch!=self._source_epoch:
                self.driver._quotes.clear();self.driver._ages.clear()
                self.prime();self._source_epoch=self.port.source_epoch
            self.owner.tick();k.wait_idle()
            batches=self._market_batches()
            rebuilt=self._rebuild_deferred_paths(batches)
            if self._arm_request_revision is not None:
                if k._authority_revision!=self._arm_request_revision:
                    self._arm_request_revision=None
                elif self.grant.in_window(self.port.clock.now()):
                    try:self.owner.arm();k.wait_idle();self._arm_request_revision=None
                    except TraderError as error:
                        if error.code!='E_NOT_READY':raise
            for code,events in batches:
                replayed=code in rebuilt
                if replayed:
                    events=events[-1:]
                elif (code,events[0]['source_epoch']) in self.feed.resume_anchors and all(e['path_complete'] for e in events):
                    self._prime_path(code,events,extend=True)
                    continue
                late=[]
                for index,event in enumerate(events):
                    if self._quote_expired(event):
                        late.append(event)
                        continue
                    if late:
                        # Rebuild a proved suffix for observation only. Entry
                        # and existing exits still require fresh market facts.
                        self._extend_late_path(code,late);late=[]
                        if self._quote_expired(event):
                            late.append(event)
                            continue
                    account,instrument=self.feed.facts(event,k)
                    try:future=self.driver.on_market(event,account,instrument,_allow_entry=not replayed and index==len(events)-1)
                    except ExpiredMarketQuote:
                        late.append(event)
                        continue
                    k.wait_idle();self.last_decisions[code]=future.result();self.feed.accept(event)
                    self._deferred_session_paths.pop(code,None)
                    self._comparison('market',event,account,instrument,self.last_decisions[code])
                if late:self._extend_late_path(code,late)
            self.driver.poll();k.wait_idle();self.owner.tick();k.wait_idle()
            if time.monotonic()-self._last_funds_capture>=10:
                self.owner.capture('ONLINE_DSV_ACCOUNT_REFRESH',{'snapshot':self.port.snapshot()})
                self._last_funds_capture=time.monotonic()
            expired=self.port.clock.now()>datetime.fromisoformat(self.grant.value['not_after_utc'])
            if expired and k._armed:k.pause();k.wait_idle()
            if expired and all(c['lifecycle']=='CLOSED' for c in k._staged.values()):
                self.result={'status':'DSV_SCOPE_FINISHED','execution_scope':'ONLINE_SIMULATION',
                    'snapshot':k.snapshot(),'money_report':k.money.report(),'grant_hash':self.grant.content_hash}
                self.write('result.json',self.result);self.stop.set()
            elif getattr(k,'_auto_parameter_wait',False):
                self.status('AUTO_OFFSETS_WAITING_MATCHING_DSV_PARAMETERS',armed=k._armed,
                    effective_profile_hash=self.driver.strategy.profile.content_hash)
            else:self.status('DSV_MANAGING_OWNED' if k._staged else 'DSV_OBSERVING',armed=k._armed)
        except Exception as error:
            if not k._failed:
                if getattr(error,'code',None) in {'E_DUPLICATE_CONFLICT','E_SCOPE','E_OWNERSHIP','E_FACT_BARRIER'}:
                    k._record_conflict('ONLINE_DSV_FACT_BARRIER',{'error_code':error.code,'detail':str(error)});k.wait_idle()
                k.pause();k.wait_idle()
            self.status('DSV_BLOCKED_'+getattr(error,'code','E_GATEWAY_RUNTIME'),error_code=getattr(error,'code','E_GATEWAY_RUNTIME'))
        finally:self._comparison('poll')


def run(scope,credentials,profile_path,calendar_path,output,*,pipe=None,arm=False,dsv_config=None,dsv_log_dir=None,
        _execution_lock=None):
    """Future explicitly scoped connection path. Offline tests never call it."""
    grant,profile,calendar=load_inputs(scope,profile_path,calendar_path)
    if grant.manual_workbench and (arm or not pipe):
        raise TraderError('E_ONLINE_SCOPE','Manual workbench runs need a control pipe and explicit arming after attachment')
    source_grant,source_profile=grant,profile;automatic=grant.automatic_offsets;preparation=None
    if dsv_log_dir is not None and not Path(dsv_log_dir).is_dir():
        raise TraderError('E_COMPARISON_LOG','Explicit DSV log directory does not exist')
    # Claim the original account before even reading credentials or connecting.
    # The same lease is adopted by Kernel; a second GUI/CLI cannot briefly log
    # in and only then discover that an execution owner already exists.
    if _execution_lock is not None and (not isinstance(_execution_lock,AccountLock)
            or not _execution_lock.matches(grant.account_key)):
        raise TraderError('E_INSTANCE_LOCK','The desktop startup lease must still own this exact account on this thread')
    lease=_execution_lock if _execution_lock is not None else AccountLock(grant.account_key)
    port=None;kernel=None;session=None
    try:
        if automatic:
            from online_trader.runtime.auto_offsets import load_preparation,prepare_connected
            restored=load_preparation(source_grant,source_profile)
            if restored is not None:preparation,grant,profile=restored
        if dsv_config is not None:
            try:verify_dsv_profile(dsv_config,profile,source_profile=source_profile if automatic else None)
            except TraderError:
                if not (automatic and restored is not None and Path(grant.value['ledger_path']).exists()):raise
                # Rebuild original exits with the original calculation. The
                # comparison observer will retain the mismatch and block entry.
        reference=measure_reference()
        if not clock_observation(reference)['system_aligned']:
            raise TraderError('E_CLOCK','Synchronize Windows with the reference clock before connecting for DSV comparison')
        port=strategy_port(grant).connect(credentials,grant,reference)
        port.configure_calendar(calendar)
        if automatic and preparation is None:
            publish_new_bytes(canonical_bytes({'status':'WAITING_NATIVE_DAILY_HISTORY_AND_RANGE',
                'source_scope_hash':source_grant.content_hash,'policy':source_grant.value['auto_offsets'],
                'entry_authority':'DISARMED'}),Path(output)/'auto-offsets-starting.json')
            preparation,grant,profile=prepare_connected(source_grant,source_profile,port)
            publish_new_bytes(canonical_bytes({'status':'CALCULATED_BEFORE_OWNER','preparation_hash':preparation['preparation_hash'],
                'effective_profile_hash':profile.content_hash,'offset_changes':preparation['offset_changes'],
                'entry_authority':'DISARMED'}),Path(output)/'auto-offsets-completed.json')
        kernel=Kernel(grant.value['ledger_path'],grant.account_key,port=port,simulation_grant=grant,
                      risk_budget=grant.value['risk_budget'],max_quote_age_ms=5000,_execution_lock=lease)
        kernel.simulation.initialize()
        if automatic:
            kernel._auto_source_profile=source_profile
            kernel.simulation.capture('ONLINE_AUTO_OFFSETS_APPLIED',{
                'preparation_hash':preparation['preparation_hash'],'source_scope_hash':source_grant.content_hash,
                'source_profile_hash':source_profile.content_hash,'effective_profile_hash':profile.content_hash,
                'calculation':preparation['calculation'],'offset_changes':preparation['offset_changes'],
                'preparation':preparation,
                'restored_original_calculation':restored is not None,'existing_plans_changed':False})
        restoring=bool(kernel._staged)
        session=DsvSimulationSession(kernel,output,profile=profile,calendar=calendar,dsv_config=dsv_config,dsv_log_dir=dsv_log_dir)
        session.prime()
        # Restart is original exits only, regardless of a repeated --arm flag.
        if arm and not restoring:session.request_initial_arm()
        if pipe:
            from online_trader.kernel.server import ControlServer
            ControlServer(kernel,pipe).serve(session.stop,owner_tick=session.step)
        else:
            while not session.stop.is_set():session.step();time.sleep(.01)
        return 0 if session.result and final_relogin(kernel,session,credentials) else 2
    except (KeyboardInterrupt,Exception) as error:
        if kernel is None:
            publish_new_bytes(canonical_bytes({'status':'NOT_CONNECTED_OR_NOT_INITIALIZED','provider':grant.provider,
                'error_code':getattr(error,'code','E_GATEWAY_RUNTIME'),'entry_authority':'DISARMED'}),Path(output)/'startup-failure.json')
        if kernel is not None:
            kernel.pause();kernel.wait_idle()
            if session is None or not session._primed:
                try:
                    publish_new_bytes(canonical_bytes({'status':'CONNECTED_BUT_NOT_PRIMED','provider':grant.provider,
                        'error_code':getattr(error,'code','E_GATEWAY_RUNTIME'),'error_type':type(error).__name__,
                        'detail':str(error) if isinstance(error,TraderError) else None,'entry_authority':'DISARMED'}),
                        Path(output)/'startup-failure.json')
                except OSError:pass
            # Closing a controller cannot terminate existing execution protection.
            if session is not None and any(c['lifecycle']!='CLOSED' for c in kernel._staged.values()):
                session.status('DSV_CONTROLLER_INTERRUPTED_ORIGINAL_PROTECTION_CONTINUES',error_code=getattr(error,'code','E_CONTROL_INTERRUPTED'))
                while not session.stop.is_set():
                    try:session.step();time.sleep(.01)
                    except KeyboardInterrupt:continue
        return 0 if session is not None and session.result and final_relogin(kernel,session,credentials) else 2
    finally:
        if session is not None:session.close()
        try:
            if kernel is not None:kernel.close()
        finally:
            if port is not None:port.close()
            lease.close()


def verify_closed_snapshot(kernel,snapshot):
    """Independent native proof of this run only, not whole-account flatness."""
    grant=kernel.simulation.grant
    if kernel.recovery_required or kernel._has_unknown():
        raise TraderError('E_FACT_BARRIER','Final verification requires all original recovery barriers resolved')
    if (not snapshot.get('complete') or snapshot.get('account_key')!=kernel.account_key
            or snapshot.get('gateway_identity')!=kernel.port.gateway_identity or not grant.permits_day(snapshot.get('trading_day'))):
        raise TraderError('E_FACT_BARRIER','Final independent login does not prove the original full account scope')
    snapshot=kernel.simulation.reconciliation_snapshot(snapshot)
    for table,key in (('orders','order_id'),('trades','trade_id'),('positions','contract_id')):
        rows=snapshot.get(table,[])
        if len({row[key] for row in rows})!=len(rows):raise TraderError('E_FACT_BARRIER','Final snapshot has duplicate native identities')
    by_id={o['order_id']:o for o in snapshot['orders']}
    codes=set(grant.value['contracts'])
    if any(p['contract_id'] in codes and (p['long'] or p['short']) for p in snapshot['positions']):
        raise TraderError('E_OWNERSHIP','Final selected contracts are not flat; external activity is not adopted')
    for cycle in kernel._staged.values():
        if cycle['lifecycle']!='CLOSED' or cycle['protection']['owned_qty']:
            raise TraderError('E_FACT_BARRIER','Every original owned cycle must be quantity-consistent closed')
        for order in cycle['orders']:
            if order['send_state']=='DEFINITELY_NOT_SENT' and not order['filled_qty']:continue
            item=by_id.get(kernel.port.order_id(order['child_order_id']))
            direction='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL'
            if (not order['terminal_confirmed'] or order['send_state']=='UNKNOWN' or item is None or item['status']!='FINISHED'
                    or item['contract_id']!=cycle['plan']['contract_id'] or item['direction']!=direction
                    or item['offset']!=order['native_offset'] or item['quantity']!=order['original_qty']
                    or item['quantity']-item['remaining']!=order['filled_qty'] or item['limit_price']!=order['limit_price']):
                raise TraderError('E_FACT_BARRIER','Final native order identity or terminal quantities differ')
            evidence={f['trade_key']:f for f in kernel.simulation._observed_orders.get(order['child_order_id'],{}).get('fills',[])}
            facts=kernel.ledger.query('SELECT trade_key,quantity,price FROM fill_facts WHERE broker_order_key=?',(order['child_order_id'],))
            expected={f['trade_key'][3:]:{'quantity':f['quantity'],'price':f['price'],
                'source_time_ns':evidence.get(f['trade_key'],{}).get('source_time_ns')} for f in facts}
            trades=[t for t in snapshot['trades'] if t['order_id']==item['order_id']]
            actual={t['trade_id']:{key:t[key] for key in ('quantity','price','source_time_ns')} for t in trades}
            if (expected!=actual or sum(t['quantity'] for t in trades)!=order['filled_qty']
                    or any(t['contract_id']!=item['contract_id'] or t['direction']!=direction or t['offset']!=item['offset'] for t in trades)):
                raise TraderError('E_FACT_BARRIER','Final native trade identities and economics differ from original owned fills')
    return {'test_owned_flat':True,'active_owned_orders':[],
            'foreign_positions':[p for p in snapshot['positions'] if p['contract_id'] not in codes],
            'whole_account_flat_claimed':False}


def final_relogin(kernel,session,credentials):
    """Close sender first; a separate read-only login cannot create any order."""
    kernel.pause();kernel.wait_idle();session.port.close();reader=None
    try:
        reader=strategy_port(session.grant).connect(credentials,session.grant,session.port.clock,read_only=True)
        reader.configure_calendar(session.driver.calendar);reader.source_epoch=session.port.source_epoch+1
        snapshot=reader.snapshot();proof=verify_closed_snapshot(kernel,snapshot)
        kernel.simulation.capture('ONLINE_DSV_FINAL_RELOGIN_VERIFIED',{'snapshot':snapshot})
        result={'status':'PASS','mode':'FRESH_READ_ONLY_LOGIN','scope_hash':session.grant.content_hash,
                **proof,'complete':True,'native_snapshot':snapshot,'durable_seq':str(kernel._durable_seq),
                'broker_connected':reader.broker_connected}
        session.write('final-relogin.json',result);session.write('final-money-report.json',kernel.money.report())
        return True
    except TraderError as error:
        session.write('final-verification-failure.json',{'status':'NEEDS_REVIEW','error_code':error.code})
        return False
    finally:
        if reader is not None:reader.close()


def load_inputs(scope,profile_path,calendar_path):
    grant=DsvSimulationGrant.load(scope)
    profile=DsvProfile.from_record(strict_loads(Path(profile_path).read_bytes()))
    calendar=TradingCalendar.from_record(strict_loads(Path(calendar_path).read_bytes()))
    return validate_inputs(grant,profile,calendar)


def validate_inputs(grant,profile,calendar):
    record=calendar.to_record();value=grant.value
    if grant.automatic_offsets:
        from online_trader.configuration.offsets import FORMULAS
        if profile.to_record()['settings']['auto_offset']['formula'] not in FORMULAS:
            raise TraderError('E_AUTO_OFFSETS','The source scope selects an unsupported DSV calculation formula')
    if profile.content_hash!=value['profile_hash'] or calendar.content_hash!=value['calendar_hash']:
        raise TraderError('E_ONLINE_SCOPE','Profile and calendar must match before any connection is attempted')
    configured={row['code'] for row in profile.to_record()['settings']['symbols']}
    if not set(value['contracts'])<=configured or not record['complete']:
        raise TraderError('E_ONLINE_SCOPE','Every scoped contract requires an explicit profile and complete calendar')
    start,end=(datetime.fromisoformat(value[key]) for key in ('not_before_utc','not_after_utc'))
    if not datetime.fromisoformat(record['coverage_from_utc'])<=start<end<=datetime.fromisoformat(record['coverage_to_utc']):
        raise TraderError('E_CALENDAR_INCOMPLETE','Finite strategy window is outside calendar coverage')
    for code in value['contracts']:
        item=next((row for row in record['contracts'] if row['contract_id']==code),None)
        if item is None or any(not any(s['trading_day']==day and s['force_close_at_end'] for s in item['sessions']) for day in grant.trading_days):
            raise TraderError('E_CALENDAR_INCOMPLETE','Each strategy contract needs its exact cabinet-day close reference')
    return grant,profile,calendar


def prepare(scope,profile_path,calendar_path,output,*,dsv_config=None,dsv_log_dir=None):
    grant,profile,calendar=load_inputs(scope,profile_path,calendar_path)
    saved=None
    if grant.automatic_offsets:
        from online_trader.runtime.auto_offsets import fixed_stops,load_preparation
        saved=load_preparation(grant,profile)
    if dsv_config is not None:verify_dsv_profile(dsv_config,saved[2] if saved is not None else profile,
        source_profile=profile if grant.automatic_offsets else None)
    if dsv_log_dir is not None and not Path(dsv_log_dir).is_dir():
        raise TraderError('E_COMPARISON_LOG','Explicit DSV log directory does not exist')
    result={'status':'OFFLINE_PREPARED_DISARMED','execution_scope':'ONLINE_SIMULATION',
            'scope_hash':grant.content_hash,'profile_hash':profile.content_hash,'calendar_hash':calendar.content_hash,
            'account_key':grant.account_key,'contracts':grant.value['contracts'],
            'provider':grant.provider,'dsv_settings_verified':dsv_config is not None,
            'dsv_log_directory_attached':dsv_log_dir is not None,'maximum_windows_clock_skew_ms':1000,
            'scope_schema':grant.value['schema_version'],'trading_days':grant.trading_days,
            'manual_workbench':grant.manual_workbench,
            'external_allocation_allowed':grant.external_allocation_allowed,
            'resume_entries_on_day_change':grant.value.get('resume_entries_on_day_change',False),
            'broker_connected':False,'account_state_checked':False,'entry_authority':'DISARMED',
            'source':'OFFLINE_CONFIGURATION_REVIEW_ONLY'}
    if grant.automatic_offsets:
        result['automatic_offsets']={'policy':grant.value['auto_offsets'],
            'status':'ORIGINAL_CALCULATION_SAVED' if saved is not None else 'AWAITING_DAILY_HISTORY',
            'fixed_stops':fixed_stops(profile,grant.value['contracts']),
            'source_profile_hash':profile.content_hash,
            'effective_profile_hash':None if saved is None else saved[2].content_hash,
            'calculated_on_future_explicit_run':saved is None,'account_connection_performed':False}
    publish_new_bytes(canonical_bytes(result),Path(output)/'dsv-preparation.json')
    return result
