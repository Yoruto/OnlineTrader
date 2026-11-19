"""Owner-driven finite DSV session; connections occur only in explicit run()."""
from datetime import datetime,timezone
from pathlib import Path
import json
from queue import Empty, SimpleQueue
from threading import Thread
import time

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import strict_loads,canonical_bytes,content_hash
from online_trader.configuration.files import publish_new_bytes
from online_trader.errors import TraderError, exception_record
from online_trader.execution.close_routes import preview_owned_routes
from online_trader.gateway.dsv_facts import DsvFactFeed,utc
from online_trader.gateway.tqkq_dsv import TqKqDsvPort
from online_trader.gateway.simnow import strategy_port
from online_trader.kernel import Kernel
from online_trader.kernel.scheduler import ExpiredMarketQuote,ScheduledRuntime
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.simulation import SimulationSession
from online_trader.runtime.instance import AccountLock, ClockActivity
from online_trader.runtime.identity import runtime_identity
from online_trader.runtime.market_monitor import DsvMarketMonitor
from online_trader.runtime.quote_trace import QuoteTrace
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.scheduling.holdover import ProtectionClockAnchor, legacy_protection_pending
from online_trader.scheduling.reference import CalibratedClock, LocalWallClock, check_wall_clock
from online_trader.scheduling.clock import quote_age_quality, source_age_ns
from online_trader.runtime.comparison import ComparisonRecorder, verify_dsv_profile
from online_trader.runtime.strategy_log import DsvEntryLog


class NativeReferenceClock:
    def __init__(self,port):self.port=port
    def sample(self):return {'now_utc':utc(self.port.clock.now()),'monotonic_ns':str(time.monotonic_ns())}


class DsvSimulationSession(SimulationSession):
    def status(self, phase, **values):
        diagnostic = {key: val for key, val in values.items() if key not in {'exception', 'repeat_count'}}
        if values.get('exception'):
            diagnostic['fingerprint'] = values['exception']['fingerprint']
        if (phase, diagnostic) == self._status_signature:
            return
        self._status_signature = (phase, diagnostic)
        self.phase = None  # The base status deduplicates by phase only.
        try:
            super().status(phase, **values)
            self.kernel._dsv_diagnostics.pop('status', None)
        except Exception:
            self.kernel._dsv_diagnostics['status'] = 'E_SESSION_STATUS_WRITE'
            self._status_signature = None

    def __init__(self,kernel,output,*,profile,calendar,clock=None,emit=None,dsv_config=None,dsv_log_dir=None,entry_point=None):
        if not getattr(kernel.simulation,'strategy_enabled',False):
            raise TraderError('E_ONLINE_SCOPE','A manual simulation owner cannot activate the DSV runtime')
        if profile.content_hash!=kernel.simulation.grant.value['profile_hash'] or calendar.content_hash!=kernel.simulation.grant.value['calendar_hash']:
            raise TraderError('E_ONLINE_SCOPE','Session settings and calendar differ from the original strategy scope')
        super().__init__(kernel,output,emit=emit,profile=profile)
        kernel._dsv_run_directory = str(self.output.resolve())
        kernel._dsv_diagnostics = {}
        identity = {'schema_version': 'DSV_RUN_IDENTITY/1', 'account_key': kernel.account_key,
                    'kernel_boot_id': kernel.boot_id, 'scope_hash': self.grant.content_hash,
                    'run_directory': kernel._dsv_run_directory}
        try:
            publish_new_bytes(canonical_bytes(identity), self.output / 'run-identity.json')
        except (OSError, TraderError):
            kernel._dsv_diagnostics['run_identity'] = 'E_RUN_IDENTITY_WRITE'
        source_clock = self.port.clock
        self.driver = ScheduledRuntime(kernel, calendar, profile=profile,
                                       clock=clock or (source_clock if isinstance(source_clock, LocalWallClock)
                                                       else NativeReferenceClock(self.port)))
        if entry_point is None:
            entry_point = ('simnow' if self.grant.provider == 'TQ_SIMNOW' else 'dsv') + ' run'
        kernel._dsv_runtime_identity = runtime_identity(
            entry_point, 'DSV_LEGACY_KERNEL', kernel.boot_id, self.port.source_epoch, kernel.boot_id, calendar=calendar,
            profile=profile, grant=self.grant)
        try:
            publish_new_bytes(canonical_bytes(kernel._dsv_runtime_identity), self.output / 'runtime-identity.json')
        except (OSError, TraderError):
            kernel._dsv_diagnostics['run_identity'] = 'E_RUN_IDENTITY_WRITE'
        self.driver.activate();kernel.wait_idle()
        self.feed=DsvFactFeed(self.port,profile,calendar);self.last_decisions={};self._primed=False
        self.comparison = None
        self.driver.strategy.on_parameters_saved = self._update_feed_parameters
        profile = self.driver.strategy.profile
        self._update_feed_parameters(profile)
        scoped = set(self.grant.value['contracts'])
        monitor_settings = profile.to_record()['settings']
        monitor_contracts = [row['code'] for row in monitor_settings['symbols'] if row['code'] in scoped]
        self.monitor = DsvMarketMonitor(monitor_contracts, calendar, diagnostic=monitor_settings['trade']['tick_diag'])
        self._quote_trace = None
        if self.monitor.diagnostic:
            self._quote_trace = QuoteTrace(
                self.output / 'market-quote-trace.jsonl', kernel.boot_id, calendar, contracts=monitor_contracts)
            self.feed.quote_trace = self._quote_trace
            self.port._quote_trace = self._quote_trace
            kernel._dsv_quote_trace = self._quote_trace
            self._quote_trace.record('SESSION_START', contracts=monitor_contracts, profile_hash=profile.content_hash,
                                     calendar_hash=calendar.content_hash, clock=type(source_clock).__name__,
                                     cache_age_limit_ms=kernel.max_quote_age_ms,
                                     opening_age_limit_seconds=monitor_settings['trade']['max_quote_age_seconds'])
            self._quote_trace.flush(phase='SESSION_START', force=True)
        kernel._dsv_market_monitor = self.monitor
        kernel._dsv_monitor_rows = []
        self._monitor_scope = None
        self._monitor_seq = None
        self._monitor_at_ns = 0
        self._latency_at_ns = 0
        self._latency_written_ns = 0
        self.owner.manual_native_facts=self.feed.facts
        self._last_funds_capture=0
        self._source_epoch=self.port.source_epoch
        self._arm_request_revision=None
        self._entry_resume = None
        self._utc_resume = None
        self._calendar_anchor=utc(self.port.clock.now())
        self._clock_reset_seen = None
        self._day_barriers=set()
        self._last_day_observations=None
        original_days=[c['plan']['trading_day'] for c in kernel._staged.values() if c['lifecycle']!='CLOSED']
        self._managed_day=min(original_days) if original_days else self.port.snapshot()['trading_day']
        self._rollover_key=None;self._day_resume_revision=None
        self._incomplete_day_barrier=False
        for row in kernel.ledger.query("SELECT payload_json FROM event_journal WHERE event_type='ONLINE_DSV_DAY_CHANGE_REQUIRES_RECONCILIATION'"):
            record=strict_loads(row['payload_json']);data=record.get('data',record)
            self._day_barriers.add(data['conflict']['transition_hash'])
        self._status_signature = None
        self._path_rebuild_ns = 0
        self._runtime_error = None
        self._ntp_results = SimpleQueue()
        self._next_clock_check = 0
        self._clock_worker = None
        self.entry_log = DsvEntryLog(kernel, output, profile)
        self._register_operation_log()
        self.comparison_error = None
        try:
            self.entry_log.start()
        except Exception as error:
            self.entry_log.failed('start', error, self.emit)
        try:
            self.entry_log.find_prior_failures()
        except Exception as error:
            self.entry_log.failed('discover', error, self.emit)
        try:
            self.comparison = ComparisonRecorder(
                kernel, output, profile, calendar, dsv_config=dsv_config, dsv_log_dir=dsv_log_dir,
                desktop_archive=entry_point == 'desktop-start' and dsv_config is None and dsv_log_dir is None)
        except Exception as error:
            self._comparison_failed(error)

    def _update_feed_parameters(self, profile):
        self.feed.profile = profile
        self.feed._continuation_seconds = self.driver.strategy._continuation_seconds
        if self.comparison is not None:
            self.comparison.update_profile(profile)

    def _register_operation_log(self):
        # This binding uses the original writer to locate the diagnostics
        # even when their separate file index cannot be written.
        self.owner.capture('DSV_OPERATION_LOG_STARTED', self.entry_log.registration())

    def _comparison_failed(self, error):
        self.comparison_error = getattr(error, 'code', 'E_COMPARISON_WRITE')
        self.kernel._dsv_diagnostics['comparison'] = self.comparison_error

    def _comparison(self, method, *args):
        if self.comparison is None:
            return
        try:
            getattr(self.comparison, method)(*args)
        except Exception as error:
            self._comparison_failed(error)
        else:
            if method == 'poll' and self.comparison_error in {
                    None, 'E_COMPARISON_SETTINGS', 'E_COMPARISON_SETTINGS_UNAVAILABLE',
                    'E_COMPARISON_LOG_UNAVAILABLE', 'E_COMPARISON_BACKPRESSURE'}:
                self.comparison_error = (self.comparison.archive_warning or self.comparison.profile_warning
                                         or self.comparison.log_warning)
                if self.comparison_error:
                    self.kernel._dsv_diagnostics['comparison'] = self.comparison_error
                else:
                    self.kernel._dsv_diagnostics.pop('comparison', None)

    def _operation_log(self, method, *args):
        try:
            getattr(self.entry_log, method)(*args)
        except Exception as error:
            self.entry_log.failed(method, error, self.emit)

    def _utc_resume_binding(self):
        k = self.kernel
        return (k.control.entry_stop_generation, self._managed_day, k._authority_revision,
                k.active_epoch, self.port.source_epoch)

    def _poll_clock_diagnostic(self):
        clock = self.port.clock
        if isinstance(clock, CalibratedClock):
            clock.poll()
            ready = clock.entry_ready()
            k = self.kernel
            k._dsv_diagnostics['ntp'] = 'NTP_ALIGNED' if ready else 'NTP_UNAVAILABLE'
            k._dsv_diagnostics['application_utc'] = clock.record()
            if self._utc_resume is not None and (
                    self._utc_resume != self._utc_resume_binding()
                    or k.control.strategy_stop_requested() or not self.owner.entry_window(clock.now())):
                self._utc_resume = None
            if not ready:
                self._entry_resume = None
                self._arm_request_revision = None
                if k._armed:
                    eligible = (not self.grant.manual_workbench and not k.recovery_required
                                and not k._has_unknown() and not self.port.fault
                                and not self.driver._fault and not self.driver.clock_rebuild_pending
                                and not k.control.strategy_stop_requested() and self.owner.entry_window(clock.now()))
                    k.pause()
                    k.wait_idle()
                    self._utc_resume = self._utc_resume_binding() if eligible else None
                    if self._utc_resume is not None:
                        self.owner.capture('ONLINE_DSV_ENTRY_RECOVERY_PENDING', {
                            'trading_day': self._managed_day, 'source_epoch': self.port.source_epoch,
                            'reason': 'APPLICATION_UTC_UNAVAILABLE'})
            if self._utc_resume is not None:
                k._dsv_diagnostics['utc_resume_pending'] = True
            else:
                k._dsv_diagnostics.pop('utc_resume_pending', None)
            return
        if not isinstance(self.port.clock, LocalWallClock):
            return
        try:
            result = self._ntp_results.get_nowait()
        except Empty:
            pass
        else:
            self.kernel._dsv_diagnostics['ntp'] = result['code']
            self._operation_log('auxiliary', [{
                'observed_at_utc': utc(self.port.clock.now()), 'kind': result['code'],
                'tag': '时钟', 'message': result['message']}])
        now = time.monotonic()
        if now >= self._next_clock_check and (self._clock_worker is None or not self._clock_worker.is_alive()):
            self._next_clock_check = now + 3600

            def sample():
                try:
                    result = check_wall_clock()
                except Exception:
                    result = {'code': 'NTP_UNAVAILABLE', 'message': 'NTP 样本不可用，策略使用 Windows 时间。'}
                self._ntp_results.put(result)

            self._clock_worker = Thread(target=sample, name='OnlineTrader-clock-check', daemon=True)
            try:
                self._clock_worker.start()
            except RuntimeError:
                self.kernel._dsv_diagnostics['ntp'] = 'NTP_UNAVAILABLE'

    def close(self):
        try:
            super().close()
            self._operation_log('poll')
            self._operation_log('close')
            self._comparison('poll')
            self._comparison('close')
        finally:
            if self._quote_trace is not None:
                self._quote_trace.close()

    def _trace_tick(self, stage, event, **details):
        if self._quote_trace is not None:
            identity = {key: event.get(key) for key in ('contract_id', 'trading_day', 'session_id', 'source_epoch',
                                                       'native_tick_id', 'native_time_ns', 'source_time_utc', 'path_complete')}
            self._quote_trace.record(stage, **identity, **details)

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
            self._entry_resume = None
        self._calendar_anchor=now
        return 'DSV_DAY_CHANGE_ORIGINAL_EXITS_NEED_RECONCILIATION' if changes else None

    def _check_multi_day(self):
        k=self.kernel;snapshot=self.port.snapshot();day=snapshot['trading_day']
        if not snapshot['complete'] or day is None:
            self.owner.days.invalidate();self._day_resume_revision=None
            self._rollover_key=None
            if not self._incomplete_day_barrier:
                k._record_conflict('ONLINE_DSV_DAY_SCOPE_INCOMPLETE',{'trading_day':day,'source_epoch':self.port.source_epoch})
                k.wait_idle();self._incomplete_day_barrier=True
            return 'DSV_WAITING_CABINET_DAY_SYNC'
        self._incomplete_day_barrier=False
        if self._entry_resume is not None and day != self._entry_resume[1]:
            self._entry_resume = None
        if (day==self._managed_day and self.port.source_epoch==self._source_epoch and not self.port.fault
                and self.owner.days.ready(day)):return None
        changed_day = day != self._managed_day
        phase = ('DSV_DAY_CHANGE_ORIGINAL_EXITS_NEED_RECONCILIATION' if changed_day
                 else 'DSV_RECONNECT_ORIGINAL_EXITS_NEED_RECONCILIATION')
        key=(day,self.port.source_epoch)
        if self._rollover_key!=key:
            resume=(changed_day and self.port.source_epoch==self._source_epoch and not self.port.fault
                and k._armed and self.grant.value['resume_entries_on_day_change'])
            self.owner.days.invalidate()
            # Old cached prices cannot describe the new session. Preserve
            # durable protection, but wait for this day's real fresh quote.
            self.driver._quotes.clear();self.driver._anchors.clear()
            observations = self.port.observations()
            if changed_day:
                k._record_conflict('ONLINE_DSV_DAY_CHANGE_REQUIRES_RECONCILIATION',{
                    'transition_hash':content_hash({'old_day':self._managed_day,'current_day':day,'source_epoch':self.port.source_epoch}),
                    'changes':[{'old_day':self._managed_day,'current_day':day}],
                    'native_observations':observations})
            else:
                k._record_conflict('ONLINE_DSV_RECONNECT_REQUIRES_RECONCILIATION',{
                    'trading_day':day,'source_epoch':self.port.source_epoch,'native_observations':observations})
            k.wait_idle();self._rollover_key=key
            self._day_resume_revision=k._authority_revision if resume else None
            self._arm_request_revision=None
        if self._day_resume_revision!=k._authority_revision:self._day_resume_revision=None
        try:ready=self.owner.days.reconcile(day)
        except TraderError as error:
            if error.code not in {'E_FACT_BARRIER','E_OWNERSHIP','E_CALENDAR_INCOMPLETE'}:raise
            return phase
        if not ready:return phase
        resume=self._day_resume_revision==k._authority_revision
        stop_generation=k.control.entry_stop_generation
        if not self.prime(manage_owned=True):
            self._primed = False
            return 'DSV_OBSERVATION_REBUILD_DEFERRED'
        if self.port.fault or self.port.source_epoch != key[1] or self.driver._fault:
            self._primed = False
            return phase
        self._managed_day=day;self._calendar_anchor=utc(self.port.clock.now())
        self._source_epoch=key[1]
        if resume and stop_generation==k.control.entry_stop_generation:
            self._arm_request_revision=k._authority_revision
        self._day_resume_revision=None
        return None

    def request_initial_arm(self):
        if self.driver._fault:
            return  # A latched boot requires complete reconciliation and explicit F5.
        if any(not self.owner.archived_cycle(cycle) for cycle in self.kernel._staged.values()):
            raise TraderError('E_NOT_READY','A restored scope manages original exits only')
        self._arm_request_revision=self.kernel._authority_revision

    def _market_batches(self):
        if any(not self.owner.archived_cycle(cycle) for cycle in self.kernel._staged.values()):
            return self.feed.batches(allow_startup_observation=False, reconnect_ready=self._reconnect_observation_ready)
        return self.feed.batches(resume_states=self.driver.strategy._states,calendar_event=self.driver._event,
                                 reconnect_ready=self._reconnect_observation_ready)

    def _reconnect_observation_ready(self):
        return (not self.port.fault and self.port.complete()
                and self.owner.can_resume_entries(self.port.snapshot()))

    def prime(self,*,manage_owned=False):
        """Rebuild available history with zero signal execution and no arming."""
        trace = self._quote_trace
        live_poll = trace.live_poll if trace is not None else False
        if trace is not None:
            if live_poll:
                for code in trace.contracts:
                    trace.market.incomplete(code)
            trace.live_poll = False
        try:
            self._prime(manage_owned=manage_owned, trace=trace)
            return True
        except TraderError as error:
            if error.code != 'E_DSV_REBUILD_INTERRUPTED' or self.kernel._failed:
                raise
            self._rebuild_deferred(error)
            return False
        finally:
            if trace is not None:
                trace.live_poll = live_poll

    def _rebuild_deferred(self, error):
        trace = self._quote_trace
        if trace is not None:
            trace.record('OBSERVATION_REBUILD_DEFERRED', source_epoch=self.port.source_epoch, reason=str(error))
        self.status('DSV_OBSERVATION_REBUILD_DEFERRED', armed=self.kernel._armed, reason=error.code)

    def _prime(self, *, manage_owned, trace):
        if trace is not None:
            trace.record('PRIME_START', manage_owned=manage_owned)
            trace.flush(phase='PRIME_START', force=True)
        self.kernel.pause();self.kernel.wait_idle()
        if self.driver._fault:
            self.driver.clock_rebuild_pending = True
            self._primed = True
            if trace is not None:
                trace.record('PRIME_WAIT_CLOCK', reasons=list(self.driver._fault))
            return  # Startup still exposes the original callbacks and reconciliation entry.
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
        # Keep advancing every rebuilt contract while any contract is waiting.
        # Removing a ready contract here would leave its early quote aging
        # until the rest of startup finishes.
        waiting = set(rebuilt)
        resting = set()
        catchup_deadline = time.monotonic() + 8
        while waiting:
            remaining = catchup_deadline - time.monotonic()
            if remaining <= 0:
                break
            self.port.poll_updates(min(.2, remaining))
            if isinstance(self.port.clock, LocalWallClock):
                self.port.clock.begin_turn()
            self.driver.preflight()
            if self.driver._fault:
                self.driver.clock_rebuild_pending = True
                self._primed = True
                if trace is not None:
                    trace.record('PRIME_WAIT_CLOCK', reasons=list(self.driver._fault))
                return
            pending = dict(self._market_batches())
            for code, event in rebuilt.items():
                suffix = pending.get(code)
                if suffix:
                    first = suffix[0]
                    continues_path = (first['source_epoch'] == event['source_epoch']
                                      and first['trading_day'] == event['trading_day']
                                      and first['session_id'] == event['session_id']
                                      and first['sequence'] == event['sequence'] + 1
                                      and int(first['native_tick_id']) > int(event['native_tick_id'])
                                      and all(row['path_complete'] for row in suffix))
                    if continues_path:
                        event = rebuilt[code] = self._prime_path(code, suffix, extend=True)
                        waiting.add(code)
                    else:
                        self._trace_tick('PRIME_SUFFIX_DEFERRED', first, reason='OBSERVATION_SUFFIX_NOT_CONTINUOUS',
                                         accepted_tick_id=event['native_tick_id'])
                if code in waiting and self._register_prime_quote(event, manage_owned=manage_owned):
                    waiting.remove(code)
            # Registration and protection for the other contracts take time.
            # A quote may need another distinct Tick before leaving catch-up.
            reference_utc = utc(self.port.clock.now())
            closed = {code for code in rebuilt
                      if self.driver.calendar.query(code, reference_utc)['is_trading'] is False}
            if trace is not None:
                for code in sorted(closed - resting):
                    self._trace_tick('PRIME_CONTRACT_RESTING', rebuilt[code], reference_utc=reference_utc,
                                     reason='QUOTE_OUTSIDE_CURRENT_SESSION')
            resting = closed
            waiting.difference_update(resting)
            for code, event in rebuilt.items():
                if code in waiting or code in resting:
                    continue
                if not self._prime_quote_current(event):
                    waiting.add(code)
                    continue
                cached = self.driver._quotes.get(code)
                age_ns = self.driver.quote_age_ns(cached['received_ns']) if cached is not None else None
                if age_ns is None or age_ns > self.kernel.max_quote_age_ms * 1_000_000:
                    self._trace_tick('PRIME_QUOTE_REJECTED', event, reason='REGISTERED_QUOTE_NOT_FRESH',
                                     cache_age_ns=age_ns, age_limit_ms=self.kernel.max_quote_age_ms)
                    waiting.add(code)
        self._primed=True
        self.driver.clock_rebuild_pending = False
        self._comparison('poll')
        self.status('DSV_PRIMED_DISARMED',strategy=self.driver.strategy.status())
        if trace is not None:
            trace.record('PRIME_FINISHED',
                         ready_contracts=[code for code in rebuilt if code not in waiting and code not in resting],
                         waiting_contracts=[code for code in rebuilt if code in waiting],
                         resting_contracts=[code for code in rebuilt if code in resting],
                         unreconstructed_contracts=[code for code in self.grant.value['contracts'] if code not in rebuilt])
            trace.flush(phase=self.phase, force=True)

    def _prime_quote_current(self, event):
        clock = self.port.clock
        now = clock.utc_now() if hasattr(clock, 'utc_now') else clock.now()
        age = (now - datetime.fromisoformat(event['source_time_utc'])).total_seconds()
        age_limit_ms = self.kernel.max_quote_age_ms
        at = self.driver.calendar.query(event['contract_id'], utc(now))
        current = (event['source_epoch'] == self.port.source_epoch and 0 <= age <= age_limit_ms / 1000
                   and at['is_trading'] and at['session']['session_id'] == event['session_id'])
        if not current:
            self._trace_tick('PRIME_QUOTE_REJECTED', event, reason='CATCH_UP_QUOTE_NOT_CURRENT',
                             source_age_seconds=str(age), age_limit_ms=age_limit_ms,
                             reference_utc=utc(now), current_epoch=self.port.source_epoch,
                             active=at['is_trading'], current_session=(at['session'] or {}).get('session_id'))
        return current

    def _register_prime_quote(self, event, *, manage_owned):
        if not self._prime_quote_current(event):
            return False
        account, instrument = self.feed.facts(event, self.kernel)
        try:
            future = self.driver.on_market(event, account, instrument, _allow_entry=False)
        except ExpiredMarketQuote:
            self._trace_scheduler_expiry(event)
            return False
        self.kernel.wait_idle()
        future.result()
        self._trace_tick('ACCEPTED_TICK', event, origin='PRIME')
        if manage_owned:
            code = event['contract_id']
            for cycle in tuple(self.kernel._staged.values()):
                if (cycle['plan']['contract_id'] != code or cycle['lifecycle'] == 'CLOSED'
                        or not self.owner.exit_day_ready(cycle, event['trading_day'])):
                    continue
                tick = self.driver.execution_event(cycle, event, instrument)
                future = self.kernel.execution.tick(cycle['position_cycle_id'], tick,
                                                    _market_received_ns=self.driver.received_for(code))
                self.kernel.wait_idle()
                future.result()
        return True

    def _prime_path(self,code,events,*,extend=False,preserve_authority=False):
        started = time.monotonic_ns()
        first = events[0]
        epoch = first['source_epoch']
        completed = False

        def progress():
            self._pump_rebuild_responsibilities()
            at = self.driver.calendar.query(code, utc(self.port.clock.now()))
            session = at['session']
            if (self.port.fault or self.port.source_epoch != epoch or self.driver._fault
                    or not at['complete'] or not at['is_trading'] or session is None
                    or at['trading_day'] != first['trading_day'] or session['session_id'] != first['session_id']):
                raise TraderError('E_DSV_REBUILD_INTERRUPTED',
                                  'The native source, clock or active session changed during observation rebuild')

        try:
            anchor_key=(code,epoch)
            prefix='native-path-'+self.kernel.boot_id+'-'+str(epoch)+'-'+code+'-'
            future=self.driver.strategy.rebuild_observation(events,extend=extend,
                _preserve_authority=preserve_authority,_resume_anchor=self.feed.resume_anchors.get(anchor_key),
                _progress=progress,_enrich=self.driver._event,_evidence_prefix=prefix)
            self.kernel.wait_idle();future.result()
            self.feed.resume_anchors.pop(anchor_key,None)
            self.feed.accept(events[-1])
            completed = True
            return events[-1]
        finally:
            elapsed_ns = time.monotonic_ns() - started
            self._path_rebuild_ns += elapsed_ns
            self._trace_tick('REPLAY_PATH', events[-1], elapsed_ns=elapsed_ns, tick_count=len(events),
                             first_tick_id=events[0].get('native_tick_id'), extend=extend, completed=completed)

    def _pump_rebuild_responsibilities(self):
        """Let the original owner process native facts while rebuilding observations."""
        clock = self.port.clock
        if isinstance(clock, LocalWallClock):
            clock.end_turn()
        self.port.poll_updates(0)
        if isinstance(clock, CalibratedClock):
            self._poll_clock_diagnostic()
        if isinstance(clock, LocalWallClock):
            clock.begin_turn()
        self.owner.observe_native_orders()
        self.kernel.wait_idle()
        self.driver.poll()
        self.kernel.wait_idle()

    def _extend_late_path(self,code,events):
        first=events[0];state=self.driver.strategy._states.get((code,first['trading_day']))
        if (state is not None and not state['coverage_lost'] and state['session_id']==first['session_id']
                and state['source_epoch']==first['source_epoch'] and state['last_sequence']+1==first['sequence']
                and all(event['path_complete'] for event in events)):
            self._prime_path(code,events,extend=True,preserve_authority=True)

    def _rebuild_session_paths(self,batches):
        rebuilt=set()
        for code,events in batches:
            first=events[0]
            state=self.driver.strategy._states.get((code,first['trading_day']))
            epoch,session=first['source_epoch'],first['session_id']
            needs_rebuild=(code,epoch,session) in self.feed.rebuild_paths
            if state is not None and state['session_id']!=session:
                needs_rebuild |= state['source_epoch']!=epoch or state['coverage_lost']
            if needs_rebuild and all(e['path_complete'] for e in events):
                # The feed proved the active session independently of the
                # previous session's checkpoint. Replay it without signals.
                self._suspend_for_recovery()
                self._prime_path(code,events)
                rebuilt.add(code)
        return rebuilt

    def _suspend_for_recovery(self):
        k = self.kernel
        if (self._entry_resume is None and k._armed and not self.grant.manual_workbench
                and not k.recovery_required and not k._has_unknown()):
            self._entry_resume = (k.control.entry_stop_generation, self._managed_day)
            self._arm_request_revision = None
            k.pause()
            k.wait_idle()
            self.owner.capture('ONLINE_DSV_ENTRY_RECOVERY_PENDING', {
                'trading_day': self._managed_day, 'source_epoch': self.port.source_epoch,
                'reason': 'WAITING_FOR_NATIVE_RECONCILIATION_AND_LIVE_QUOTE'})

    def _try_resume_entries(self, event, account):
        clock_recovery = self._utc_resume is not None
        pending = self._utc_resume if clock_recovery else self._entry_resume
        if pending is None:
            return
        k = self.kernel
        generation, original_day = pending[:2]
        now = self.port.clock.utc_now() if hasattr(self.port.clock, 'utc_now') else self.port.clock.now()
        if (generation != k.control.entry_stop_generation or event['trading_day'] != original_day
                or not self.owner.entry_window(now) or k.control.strategy_stop_requested()
                or clock_recovery and pending != self._utc_resume_binding()):
            self._entry_resume = None
            self._utc_resume = None
            return
        if clock_recovery:
            if not self.port.clock.entry_stable():
                return
            sample = self.port.clock.capture_live()
            stamp = self.port.quotes[event['contract_id']].datetime
            source = datetime.fromisoformat(event['source_time_utc'])
            age = source_age_ns(now, source, stamp)
            if quote_age_quality(age, self.port.clock.health(sample))[0] != 'VALID':
                return {'reason_code': 'ENTRY_RESUME_NATIVE_QUOTE'}
        key = event['contract_id'], event['trading_day']
        strategy = self.driver.strategy
        state = strategy._states.get(key)
        if (state is None or state['coverage_lost'] or state['session_id'] != event['session_id']
                or state['source_epoch'] != event['source_epoch'] or event['source_epoch'] != self.port.source_epoch
                or state['last_sequence'] + 1 != event['sequence'] or event['quality'] != 'CONTIGUOUS'
                or not event['path_complete'] or not event['day_range_complete']):
            return {'reason_code': 'ENTRY_RESUME_MARKET_PATH'}
        if not self.driver.scope_current(key[0], key[1], event['session_id']) or not self.port.current_strategy_quote(event):
            return {'reason_code': 'ENTRY_RESUME_NATIVE_QUOTE'}
        snapshot = self.port.snapshot()
        if (clock_recovery and snapshot['trading_day'] != original_day
                or not self.owner.can_resume_entries(snapshot, clock_recovery=clock_recovery)):
            return {'reason_code': 'ENTRY_RESUME_ACCOUNT_SYNC'}
        # Preview the same continuation the armed strategy would evaluate.
        # Its native freshness gate is independent of the ordinary age limit.
        if strategy.ordinary_quote_stale(event, now=now) and (
                clock_recovery or self._quote_expired(event) or not strategy.continuation_ready_on_resume(
                    state, self.driver._event(event), account, now=now)):
            return {'reason_code': 'ENTRY_RESUME_QUOTE_AGE_POLICY',
                    'age_seconds': str(strategy.ordinary_quote_age(event, now=now)),
                    'limit_seconds': str(strategy.profile.to_record()['settings']['trade']['max_quote_age_seconds'])}
        try:
            self.owner.arm()
        except TraderError as error:
            if error.code != 'E_NOT_READY':
                raise
            return {'reason_code': 'ENTRY_RESUME_ACCOUNT_SYNC'}
        k.wait_idle()
        if k._armed:
            self._entry_resume = None
            self._utc_resume = None
            self.owner.capture('ONLINE_DSV_ENTRIES_RESUMED', {
                'trading_day': event['trading_day'], 'source_epoch': event['source_epoch'],
                'quote_id': event.get('native_tick_id', event['sequence']),
                **({'reason': 'APPLICATION_UTC_RECOVERED'} if clock_recovery else {})})
            return
        return {'reason_code': 'ENTRY_RESUME_ACCOUNT_SYNC'}

    def _quote_expired(self,event):
        now = self.port.clock.now()
        age = (now - datetime.fromisoformat(event['source_time_utc'])).total_seconds()
        expired = age > self.kernel.max_quote_age_ms / 1000
        if expired:
            self._trace_tick('TICK_REJECTED', event, reason='BACKLOG_AGE_LIMIT',
                             source_age_seconds=str(age), age_limit_ms=self.kernel.max_quote_age_ms,
                             reference_utc=utc(now))
        return expired

    def _trace_scheduler_expiry(self, event):
        if self._quote_trace is not None:
            sample = self.driver._sample
            age = (sample.utc - datetime.fromisoformat(event['source_time_utc'])).total_seconds()
            self._trace_tick('TICK_REJECTED', event, reason='SCHEDULER_AGE_LIMIT',
                             source_age_seconds=str(age), age_limit_ms=self.kernel.max_quote_age_ms,
                             reference_utc=sample.now_utc)

    def _update_monitor(self):
        from online_trader.kernel.workbench import monitor_symbols
        now = time.monotonic_ns()
        scope = (self.monitor.source_epoch, self.monitor.trading_day, self.monitor.latest['quote_status'] == '连接中')
        if scope != self._monitor_scope:
            self._monitor_at_ns = 0
        self._monitor_scope = scope
        if self._monitor_seq != self.kernel._durable_seq or now - self._monitor_at_ns >= 250_000_000:
            self.kernel._dsv_monitor_rows = monitor_symbols(self.kernel, self.last_decisions)
            self._monitor_at_ns = now
            self._monitor_seq = self.kernel._durable_seq
        if self._quote_trace is not None and now - self._latency_at_ns >= 1_000_000_000:
            diagnostic = self.monitor.diagnostics(now)
            ticks = [tick for tick in diagnostic['ticks'] if int(tick['strategy_end_monotonic_ns']) > self._latency_written_ns]
            queued = self._quote_trace.enqueue(self.output/'market-latency.jsonl', diagnostic | {
                'ticks': ticks, 'account_key': self.kernel.account_key, 'kernel_boot_id': self.kernel.boot_id,
                **self.monitor.latest,
            })
            self._latency_at_ns = now
            if queued and ticks:
                self._latency_written_ns = int(ticks[-1]['strategy_end_monotonic_ns'])

    def _finish_strategy_stop(self, *, _snapshot=None):
        k = self.kernel
        snapshot = self.port.snapshot() if _snapshot is None else _snapshot
        if self.port.fault or not snapshot['complete']:
            reason = '等待柜台恢复同步'
        elif snapshot['positions']:
            reason = '账户仍有持仓，继续管理自有持仓'
        elif any(order['status'] == 'ALIVE' or (order['quantity'] > 0 and order['status'] != 'FINISHED')
                 for order in snapshot['orders']):
            reason = '等待柜台活动委托结束'
        elif (k._pending or k.recovery_required or k._has_unknown() or k.control.stop_cancel_pending()
              or any(c['lifecycle'] != 'CLOSED' or c.get('statistics_review_required')
                     for c in k._staged.values())):
            reason = '等待委托、成交与账本核对'
        else:
            try:
                self.owner._validate_snapshot(snapshot)
                proof = verify_closed_snapshot(k, snapshot)
            except TraderError:
                reason = '等待柜台终态与逐笔成交核对'
            else:
                stop_evidence = {
                    'grant_hash':self.grant.content_hash, 'native_snapshot_id':snapshot['snapshot_id'],
                    'trading_day':snapshot['trading_day'], 'source_epoch':snapshot['source_epoch'],
                    'positions':0, 'active_orders':0}
                if not k._legacy_stream:
                    stop_evidence['final_account_observation'] = snapshot
                self.owner.capture('ONLINE_DSV_STOP_VERIFIED', stop_evidence)
                result = {'status':'DSV_STOPPED', 'execution_scope':'ONLINE_SIMULATION',
                          'account_key':k.account_key, 'kernel_boot_id':k.boot_id,
                          'checkpoint_event_seq':str(k._durable_seq), 'grant_hash':self.grant.content_hash,
                          'native_snapshot_id':snapshot['snapshot_id'],
                          'verification':proof | {'whole_account_flat_claimed':True}}
                try:self.write('result.json', result)
                except (OSError, TraderError):self._comparison_failed(TraderError('E_SESSION_STATUS_WRITE','Stop result file could not be written'))
                k._dsv_stop_wait = None
                self.result = result
                self.status('DSV_STOPPED', armed=False, kernel_boot_id=k.boot_id)
                self.stop.set()
                return
        k._dsv_stop_wait = reason
        self.status('DSV_STOPPING', armed=False, reason=reason)

    def _clock_protection(self, sample, received_ns):
        k = self.kernel
        blocked = {}
        active = []
        for cycle in tuple(k._staged.values()):
            cid, code = cycle['position_cycle_id'], cycle['plan']['contract_id']
            if (cycle['lifecycle'] == 'CLOSED' or self.owner.archived_cycle(cycle)
                    or cycle.get('management_state') == 'TAKEN_OVER'):
                continue
            owned = cycle['protection']['owned_qty']
            try:
                if sample is None:
                    raise TraderError('CLOCK_PROTECTION_MONOTONIC_UNPROVED', 'No same-process monotonic clock proof')
                if self.port.fault or not self.port.complete() or self.port.source_epoch != self._source_epoch:
                    raise TraderError('CLOCK_PROTECTION_CONNECTION_UNPROVED', 'The original synchronized native source changed')
                at = self.driver.clock_protection_session(code, sample.utc)
                if at is None or not self.owner.exit_day_ready(cycle, at['trading_day']):
                    raise TraderError('CLOCK_PROTECTION_SESSION_UNPROVED', 'The last verified active trading session is no longer current')
                if cid in k._recovery_pending or cycle['protection']['reconciliation_required']:
                    raise TraderError('RECONCILIATION_REQUIRED', 'The original cycle still needs full reconciliation')
                snapshot = self.port.snapshot(now=sample.utc)
                if snapshot['trading_day'] != at['trading_day']:
                    raise TraderError('CLOCK_PROTECTION_SESSION_UNPROVED', 'The account and the quote do not prove the same cabinet day')
                self.owner.verify_clock_protection(cycle, snapshot)
                orders_terminal = all(o['terminal_confirmed'] for o in cycle['orders'])
                if not owned:
                    if orders_terminal and self.owner.can_close(cycle):
                        self.owner.reconcile(cid, close=True, _snapshot=snapshot, _statistics_at_utc=sample.now_utc)
                        self.owner._closed.add(cid)
                    continue
                binding = cycle.get('schedule_binding')
                if (not binding or binding != self.driver.binding_for(cycle['plan'], entry_time_utc=binding['entry_time_utc'])):
                    raise TraderError('CLOCK_PROTECTION_SESSION_UNPROVED', 'The original cycle calendar binding changed')
                reduction = preview_owned_routes(k, cid, owned, _now=sample.utc)
                if sum(reduction['cycle_owned'].values()) != owned:
                    raise TraderError('E_OWNERSHIP', 'Native close buckets differ from the owned cycle')
                if (not cycle['protection']['entry_terminal_reconciled']
                        and all(o['terminal_confirmed'] for o in cycle['orders'] if o['effect'] == 'OPEN')):
                    self.owner.reconcile(cid, _snapshot=snapshot)
                    self.owner._entry_reconciled.add(cid)
                    cycle = k._staged[cid]
                event = self.feed.current_protection_event(code, at['session'])
                if event is None or not self.port.current_protection_quote(event, sample.utc):
                    raise TraderError('E_NATIVE_QUOTE_NOT_CURRENT', 'A fresh matching native book and tick are required')
                prior = cycle.get('last_protection_market_identity')
                if prior and (int(event['native_time_ns']) < int(prior['native_time_ns'])
                              or int(event['native_tick_id']) < int(prior['native_tick_id'])
                              or (event['native_tick_id'] == prior['native_tick_id']
                                  and event['native_time_ns'] != prior['native_time_ns'])):
                    raise TraderError('E_NATIVE_QUOTE_NOT_CURRENT', 'The live native source moved backwards')
                instrument = self.port.market(code)
                if not self.driver.on_clock_protection(cycle, event, instrument, sample, at, received_ns):
                    raise TraderError('E_NATIVE_QUOTE_NOT_CURRENT', 'The original session or native quote changed before evaluation')
                current = k._staged[cid]
                if any(o['send_state'] == 'UNKNOWN' or (o['cancel_status'] == 'UNKNOWN' and not o['terminal_confirmed'])
                       for o in current['orders']):
                    raise TraderError('E_UNKNOWN_SEND', 'The original order or cancellation still needs native evidence')
                if cid in k._recovery_pending:
                    raise TraderError('RECONCILIATION_REQUIRED', 'The original cycle still needs full reconciliation')
                if current.get('last_dispatch_abort') != cycle.get('last_dispatch_abort'):
                    reasons = current['last_dispatch_abort']['reasons']
                    blocked[cid] = next(iter(reasons), 'E_FACT_BARRIER')
                    continue
                active.append(cid)
            except (TraderError, ValueError, OverflowError) as error:
                if k._failed:
                    raise
                if owned:
                    blocked[cid] = getattr(error, 'code', 'E_NATIVE_QUOTE_NOT_CURRENT')
        result = {'active_cycles': active, 'blocked_cycles': blocked}
        k._dsv_diagnostics['clock_protection'] = result
        return result

    def step(self):
        k=self.kernel
        clock = self.port.clock
        loop_start = time.monotonic_ns()
        commit_wait_start = k.commit_wait_ns
        path_rebuild_start = self._path_rebuild_ns
        timings = {}
        code, event = None, None
        if self._quote_trace is not None:
            self._quote_trace.live_poll = self._primed
        try:
            if isinstance(clock, CalibratedClock):
                self._poll_clock_diagnostic()
            update_start = time.monotonic_ns()
            received = self.port.poll_updates(.05)
            if isinstance(clock, LocalWallClock):
                clock.begin_turn()
            clock_received_ns = time.monotonic_ns()
            wall, sdk_return = received or (datetime.now(timezone.utc), time.monotonic_ns())
            timings['SDK 更新'] = sdk_return - update_start
            try:
                self.monitor.observe(self.port, returned_ns=sdk_return, wall=wall)
            except (TraderError, ValueError, AttributeError, TypeError):
                self.monitor.invalidate(wall)
            if self._entry_resume and self._entry_resume[0] != k.control.entry_stop_generation:
                self._entry_resume = None
            if self.port.fault or self.port.source_epoch != self._source_epoch:
                self._utc_resume = None
                self._suspend_for_recovery()
            self.driver.preflight()
            if self.driver._fault:
                self._utc_resume = None
                protection_sample = self.driver.clock_protection_sample()
                self.owner.observe_native_orders()
                k.wait_idle()
                self._entry_resume = None
                self._arm_request_revision = None
                self._day_resume_revision = None
                self.driver.poll()
                k.wait_idle()
                protection = self._clock_protection(protection_sample, clock_received_ns)
                if k.control.strategy_stop_requested():
                    stop_snapshot = self.driver.clock_protection_snapshot(protection_sample)
                    if stop_snapshot is not None and stop_snapshot['trading_day'] == self._managed_day:
                        self._finish_strategy_stop(_snapshot=stop_snapshot)
                        if self.result is not None:
                            return
                    else:
                        k._dsv_stop_wait = '等待原盘次及柜台终态核对'
                self.status('DSV_CLOCK_PROTECTION_BLOCKED' if protection['blocked_cycles'] else 'DSV_CLOCK_FAULT',
                            armed=False, reasons=list(self.driver._fault), clock_protection=protection)
                return
            k._dsv_diagnostics.pop('clock_protection', None)
            expired = self.owner.observe_scope_expiration(clock.now())
            reset = self.driver.last_clock_reset
            if reset is not self._clock_reset_seen:
                self._calendar_anchor = reset.now_utc
                self._clock_reset_seen = reset
            recovery_phase = self.check_day_transition()
            if recovery_phase:
                self._utc_resume = None
                self.owner.observe_native_orders()
                k.wait_idle()
                if self._entry_resume and self.port.complete():
                    day = self.port.snapshot()['trading_day']
                    if day is not None and day != self._entry_resume[1]:
                        self._entry_resume = None
                self.status(recovery_phase,armed=False)
                if k.control.strategy_stop_requested():
                    k._dsv_stop_wait = ('等待重新建立行情观察'
                                        if recovery_phase == 'DSV_OBSERVATION_REBUILD_DEFERRED'
                                        else '等待柜台同步及原委托、持仓核对')
                return
            if self.port.fault and not self.port.complete():
                self.owner.tick();k.wait_idle()
                self.status('DSV_WAITING_GATEWAY_SYNC',armed=False)
                if k.control.strategy_stop_requested():k._dsv_stop_wait = '等待柜台恢复同步'
                return
            if self.port.source_epoch!=self._source_epoch:
                self.owner.tick()
                k.wait_idle()
                self.driver._quotes.clear()
                self.driver._ages.clear()
                epoch = self.port.source_epoch
                if not self.prime():
                    return
                self._source_epoch = epoch
            self.owner.tick();k.wait_idle()
            if not self._primed:
                self.prime(manage_owned=True)
                return
            if self.driver.clock_rebuild_pending:
                self._utc_resume = None
                self.prime(manage_owned=True)
                return
            path_start = time.monotonic_ns()
            try:
                batches=self._market_batches()
            finally:
                timings['Tick 批次'] = time.monotonic_ns() - path_start
            rebuilt=self._rebuild_session_paths(batches)
            if self._arm_request_revision is not None:
                if k._authority_revision!=self._arm_request_revision:
                    self._arm_request_revision=None
                elif self.owner.entry_window(self.port.clock.now()):
                    try:self.owner.arm();k.wait_idle();self._arm_request_revision=None
                    except TraderError as error:
                        if error.code!='E_NOT_READY':raise
            for code,events in batches:
                replayed=code in rebuilt
                if replayed:
                    events=events[-1:]
                elif (code,events[0]['source_epoch']) in self.feed.resume_anchors and all(e['path_complete'] for e in events):
                    self._prime_path(code,events,extend=True,preserve_authority=True)
                    continue
                late=[]
                for index,event in enumerate(events):
                    live = not replayed and index == len(events) - 1
                    if (not live or not event['path_complete']) and self._quote_expired(event):
                        late.append(event)
                        continue
                    if late:
                        # Rebuild a proved suffix for observation only. Entry
                        # and existing exits still require fresh market facts.
                        self._extend_late_path(code,late);late=[]
                    strategy_start = time.monotonic_ns()
                    account,instrument=self.feed.facts(event,k)
                    entry_recovery = self._try_resume_entries(event, account) if live else None
                    try:future=self.driver.on_market(event,account,instrument,_allow_entry=live,_entry_recovery=entry_recovery)
                    except ExpiredMarketQuote:
                        self._trace_scheduler_expiry(event)
                        late.append(event)
                        continue
                    k.wait_idle();self.last_decisions[code]=future.result();self.feed.accept(event)
                    self._trace_tick('ACCEPTED_TICK', event, origin='LIVE' if live else 'BACKLOG')
                    strategy_end = time.monotonic_ns()
                    timings['策略处理'] = timings.get('策略处理', 0) + strategy_end - strategy_start
                    if self.monitor.diagnostic:
                        try:self.monitor.processed(event, strategy_start, strategy_end, self.port.clock)
                        except (TraderError, ValueError, TypeError, KeyError):k._dsv_monitor_error = 'E_MARKET_DIAGNOSTIC'
                    self._comparison('market',event,account,instrument,self.last_decisions[code])
                if late:self._extend_late_path(code,late)
            self.driver.poll();k.wait_idle();self.owner.tick();k.wait_idle()
            if time.monotonic()-self._last_funds_capture>=10:
                self.owner.capture('ONLINE_DSV_ACCOUNT_REFRESH',{'snapshot':self.port.snapshot()})
                self._last_funds_capture=time.monotonic()
            if expired:
                self._entry_resume = None
                self._utc_resume = None
                if k._armed:
                    k.pause()
                    k.wait_idle()
            if k.control.strategy_stop_requested():
                self._finish_strategy_stop()
            elif expired and all(c['lifecycle']=='CLOSED' for c in k._staged.values()):
                self.result={'status':'DSV_SCOPE_FINISHED','execution_scope':'ONLINE_SIMULATION',
                    'snapshot':k.snapshot(),'money_report':k.money.report(),'grant_hash':self.grant.content_hash}
                try:
                    self.write('result.json', self.result)
                except (OSError, TraderError) as error:
                    self.kernel._dsv_diagnostics['status'] = getattr(error, 'code', 'E_SESSION_STATUS_WRITE')
                self.stop.set()
            elif (isinstance(clock, CalibratedClock) and not clock.protection_ready()
                  and any(c['lifecycle'] != 'CLOSED' for c in k._staged.values())):
                self.status('DSV_UTC_PROTECTION_WAIT', armed=False,
                            reason='原委托和持仓继续核对，等待可靠时间后执行退出')
            elif self._entry_resume is not None or self._utc_resume is not None:
                self.status('DSV_ENTRY_RECOVERY_PENDING', armed=False)
            else:self.status('DSV_MANAGING_OWNED' if any(c['lifecycle']!='CLOSED' for c in k._staged.values())
                             else 'DSV_OBSERVING',armed=k._armed)
        except Exception as error:
            if isinstance(error, TraderError) and error.code == 'E_DSV_REBUILD_INTERRUPTED' and not k._failed:
                self._rebuild_deferred(error)
                return
            self.monitor.invalidate(datetime.now(timezone.utc))
            self._monitor_at_ns = 0
            self._entry_resume = None
            self._utc_resume = None
            self._arm_request_revision = None
            error_code = getattr(error, 'code', 'E_GATEWAY_RUNTIME')
            record = exception_record(error)
            decision = self.last_decisions.get(code, {})
            opportunity = decision.get('opportunity') or {}
            matches = event is not None and opportunity.get('market', {}).get('sequence') == event['sequence']
            decision_id = decision.get('decision_id') if matches else None
            fingerprint = record['fingerprint']
            previous = self._runtime_error
            new = previous is None or previous['fingerprint'] != fingerprint
            self._runtime_error = (record | {'count': 1} if new else previous | {'count': previous['count'] + 1})
            repeat = self._runtime_error['count']
            if not k._failed:
                if new and error_code in {'E_DUPLICATE_CONFLICT', 'E_SCOPE', 'E_OWNERSHIP', 'E_FACT_BARRIER'}:
                    k._record_conflict('ONLINE_DSV_FACT_BARRIER', {'error_code': error_code, 'exception': record})
                    k.wait_idle()
                if k._armed or new:
                    k.pause()
                    k.wait_idle()
            if new or repeat in {10, 100, 1000} or self.phase != 'DSV_BLOCKED_' + error_code:
                self._status_signature = None
                self.status('DSV_BLOCKED_' + error_code, error_code=error_code, contract_id=code, decision_id=decision_id,
                            exception={key: val for key, val in self._runtime_error.items() if key != 'count'},
                            repeat_count=repeat)
                self._operation_log('auxiliary', [{
                    'observed_at_utc': utc(datetime.now(timezone.utc)), 'kind': 'DSV_RUNTIME_ERROR', 'tag': '警告',
                    'message': f"{error_code} · {self._runtime_error['exception_id']} · 累计 {repeat} 次",
                    'contract_id': code, 'decision_id': decision_id,
                    'exception_id': self._runtime_error['exception_id'],
                    'exception': {key: val for key, val in self._runtime_error.items() if key != 'count'},
                    'repeat_count': repeat}])
        finally:
            try:
                self._update_monitor()
            except (OSError, TraderError, ValueError, KeyError, TypeError, ArithmeticError):
                k._dsv_monitor_error = 'E_MARKET_DIAGNOSTIC'
                self._latency_at_ns = time.monotonic_ns()
            try:
                self._poll_clock_diagnostic()
                timings['会话路径重建'] = self._path_rebuild_ns - path_rebuild_start
                timings['提交等待（含各阶段内等待）'] = k.commit_wait_ns - commit_wait_start
                self._operation_log('monitor', self.monitor, loop_start, timings)
                self._operation_log('poll')
                archive_start = time.monotonic_ns()
                self._comparison('poll')
                timings['对照归档提交'] = time.monotonic_ns() - archive_start
            finally:
                if self._quote_trace is not None:
                    self._quote_trace.live_poll = False
                    elapsed_ns = time.monotonic_ns() - loop_start
                    if elapsed_ns >= 100_000_000:
                        self._quote_trace.record('OWNER_LOOP_SLOW', elapsed_ns=elapsed_ns,
                                                 phase=self.phase, timings_ns=timings)
                    self._quote_trace.flush(phase=self.phase, timings=timings)
                    if self._quote_trace.write_failed:
                        k._dsv_diagnostics['quote_trace'] = 'E_QUOTE_TRACE_WRITE'
                    else:
                        k._dsv_diagnostics.pop('quote_trace', None)
                if isinstance(clock, LocalWallClock):
                    clock.end_turn()


def run(scope,credentials,profile_path,calendar_path,output,*,pipe=None,arm=False,dsv_config=None,dsv_log_dir=None,
        _execution_lock=None,_reference=None,_existing_ledger=False,_trusted_control_pid=None):
    """Future explicitly scoped connection path. Offline tests never call it."""
    with ClockActivity():
        return _run(scope, credentials, profile_path, calendar_path, output, pipe=pipe, arm=arm,
                    dsv_config=dsv_config, dsv_log_dir=dsv_log_dir, _execution_lock=_execution_lock,
                    _reference=_reference, _existing_ledger=_existing_ledger, _trusted_control_pid=_trusted_control_pid)


def _run(scope, credentials, profile_path, calendar_path, output, *, pipe, arm, dsv_config, dsv_log_dir,
         _execution_lock, _reference, _existing_ledger=False, _trusted_control_pid=None):
    grant,profile,calendar=load_inputs(scope,profile_path,calendar_path)
    if grant.replacement:
        raise TraderError('E_ONLINE_SCOPE','A replacement grant requires the replacement SDK startup')
    if grant.manual_workbench and (arm or not pipe):
        raise TraderError('E_ONLINE_SCOPE','Manual workbench runs need a control pipe and explicit arming after attachment')
    source_grant,source_profile=grant,profile;automatic=grant.automatic_offsets;preparation=None
    # Claim the original account before even reading credentials or connecting.
    # The same lease is adopted by Kernel; a second GUI/CLI cannot briefly log
    # in and only then discover that an execution owner already exists.
    if _execution_lock is not None and (not isinstance(_execution_lock,AccountLock)
            or not _execution_lock.matches(grant.account_key)):
        raise TraderError('E_INSTANCE_LOCK','The desktop startup lease must still own this exact account on this thread')
    lease=_execution_lock if _execution_lock is not None else AccountLock(grant.account_key)
    port=None;kernel=None;session=None
    try:
        output = Path(output).resolve()
        _claim_run_output(output, grant)
        if automatic:
            from online_trader.runtime.auto_offsets import load_preparation,prepare_connected
            restored=load_preparation(source_grant,source_profile)
            if restored is not None:preparation,grant,profile=restored
        anchor = ProtectionClockAnchor(grant.value['ledger_path'], grant.account_key)
        reference = _reference
        if reference is None:
            reference = CalibratedClock(recovery_anchor=anchor, recovering=lambda: legacy_protection_pending(grant))
        else:
            reference.bind_recovery_anchor(anchor)
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
                      risk_budget=grant.value['risk_budget'],max_quote_age_ms=5000,_execution_lock=lease,
                      _existing_ledger=_existing_ledger, _legacy_stream=_trusted_control_pid is None)
        kernel.simulation.initialize()
        if automatic:
            kernel._auto_source_profile=source_profile
            kernel.simulation.capture('ONLINE_AUTO_OFFSETS_APPLIED',{
                'preparation_hash':preparation['preparation_hash'],'source_scope_hash':source_grant.content_hash,
                'source_profile_hash':source_profile.content_hash,'effective_profile_hash':profile.content_hash,
                'calculation':preparation['calculation'],'offset_changes':preparation['offset_changes'],
                'preparation':preparation,
                'restored_original_calculation':restored is not None,'existing_plans_changed':False})
        restoring=any(not kernel.simulation.archived_cycle(cycle) for cycle in kernel._staged.values())
        session=DsvSimulationSession(kernel,output,profile=profile,calendar=calendar,dsv_config=dsv_config,
                                     dsv_log_dir=dsv_log_dir,entry_point='desktop-start' if _execution_lock is not None else None)
        session._poll_clock_diagnostic()
        session.prime()
        reference.end_turn()
        # Restart is original exits only, regardless of a repeated --arm flag.
        if arm and not restoring and reference.entry_ready():session.request_initial_arm()
        if pipe:
            from online_trader.kernel.server import ControlServer
            ControlServer(kernel, pipe, trusted_client_pid=_trusted_control_pid).serve(
                session.stop, owner_tick=session.step, publish_workbench=True)
        else:
            while not session.stop.is_set():session.step();time.sleep(.01)
        return 0 if session.result and (session.result['status']=='DSV_STOPPED' or final_relogin(kernel,session,credentials)) else 2
    except (KeyboardInterrupt,Exception) as error:
        if isinstance(error, TraderError) and error.code == 'E_RUN_OUTPUT':
            raise
        if kernel is None:
            publish_new_bytes(canonical_bytes({'status':'NOT_CONNECTED_OR_NOT_INITIALIZED','provider':grant.provider,
                'error_code':getattr(error,'code','E_GATEWAY_RUNTIME'),'exception':exception_record(error),
                'entry_authority':'DISARMED'}),Path(output)/'startup-failure.json')
        if kernel is not None:
            kernel.pause();kernel.wait_idle()
            if session is None or not session._primed:
                try:
                    publish_new_bytes(canonical_bytes({'status':'CONNECTED_BUT_NOT_PRIMED','provider':grant.provider,
                        'error_code':getattr(error,'code','E_GATEWAY_RUNTIME'),'error_type':type(error).__name__,
                        'exception':exception_record(error),'entry_authority':'DISARMED'}),
                        Path(output)/'startup-failure.json')
                except OSError:pass
            # Closing a controller cannot terminate existing execution protection.
            if session is not None and any(c['lifecycle']!='CLOSED' for c in kernel._staged.values()):
                session.status('DSV_CONTROLLER_INTERRUPTED_ORIGINAL_PROTECTION_CONTINUES',error_code=getattr(error,'code','E_CONTROL_INTERRUPTED'))
                while not session.stop.is_set():
                    try:session.step();time.sleep(.01)
                    except KeyboardInterrupt:continue
        return 0 if (session is not None and session.stop.is_set() and session.result
                     and (session.result['status']=='DSV_STOPPED' or final_relogin(kernel,session,credentials))) else 2
    finally:
        if session is not None:session.close()
        try:
            if kernel is not None:kernel.close()
        finally:
            if port is not None:port.close()
            lease.close()


def _claim_run_output(output, grant):
    """Claim the output once, including when different accounts start concurrently."""
    records = ('comparison-*', 'run-identity.json', 'strategy-log.jsonl', 'phase-[0-9][0-9][0-9][0-9]*.json',
               'result.json', 'startup-failure.json', 'auto-offsets-starting.json',
               'auto-offsets-completed.json', 'market-latency*.jsonl*', 'market-quote-trace*.jsonl',
               'workbench-latency-*', 'final-relogin.json', 'final-money-report.json')
    try:
        recovery = output.with_name(output.name + '.operation-log-recovery.json')
        if recovery.exists() or any(next(output.glob(pattern), None) is not None for pattern in records):
            raise TraderError('E_RUN_OUTPUT', 'Choose a new DSV output directory for each run')
        publish_new_bytes(canonical_bytes({'account_key': grant.account_key, 'scope_hash': grant.content_hash}),
                          output / 'dsv-run-claim.json')
    except TraderError as error:
        if error.code == 'E_RUN_OUTPUT':
            raise
        raise TraderError('E_RUN_OUTPUT', 'Cannot claim this DSV output directory. Choose a new writable path.') from error


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
    trades_by_order = {}
    for trade in snapshot['trades']:
        trades_by_order.setdefault(trade['order_id'], []).append(trade)
    for cycle in kernel._staged.values():
        if cycle['lifecycle']!='CLOSED' or cycle['protection']['owned_qty']:
            raise TraderError('E_FACT_BARRIER','Every original owned cycle must be quantity-consistent closed')
        if kernel.simulation.archived_cycle(cycle):
            continue
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
            expected={f['trade_key']:{'quantity':f['quantity'],'price':f['price'],
                'source_time_ns':evidence.get(f['trade_key'],{}).get('source_time_ns')} for f in facts}
            trades=trades_by_order.get(item['order_id'], ())
            actual={kernel.port.trade_key(t['trade_id']):{key:t[key] for key in ('quantity','price','source_time_ns')} for t in trades}
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
            'dsv_log_directory_attached':dsv_log_dir is not None,'clock_policy':'SYSTEM_WALL_CLOCK_NTP_DIAGNOSTIC_ONLY',
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
