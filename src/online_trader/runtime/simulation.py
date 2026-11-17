"""Explicit optional simulation session using Kernel, OMS, protection and reports."""
from datetime import datetime
from pathlib import Path
from decimal import Decimal
import hashlib,json,threading,time,uuid

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes,content_hash
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import TqKqPort
from online_trader.kernel import Kernel
from online_trader.online_scope import SimulationGrant
from online_trader.runtime.instance import AccountLock
from online_trader.scheduling.holdover import ProtectionClockAnchor, legacy_protection_pending
from online_trader.scheduling.reference import CalibratedClock


class SimulationSession:
    def __init__(self,kernel,output,*,emit=None,profile=None):
        self.kernel=kernel;self.owner=kernel.simulation;self.port=kernel.port;self.grant=self.owner.grant
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True)
        self.emit=emit or (lambda value:print(json.dumps(value,ensure_ascii=False),flush=True))
        self.stop=threading.Event();self.phase=None;self._phase_sequence=0;self._quotes={};self._submitted_at={};self._cancelled=set();self._entered=False
        self._last_money=0;self._last_error=None;self.result=None
        self._start_sequence=kernel._durable_seq
        self._start_authority_revision=kernel._authority_revision
        if profile is None:profile,_=load_confirmed_baseline()
        self.symbols={row['code']:row for row in profile.to_record()['settings']['symbols']}
        if any(code not in self.symbols for code in self.grant.value['contracts']):raise TraderError('E_CONFIG','Simulation test contracts must have explicit saved DSV distance parameters')

    def write(self,name,value):return publish_new_bytes(canonical_bytes(value),self.output/name)

    def status(self,phase,**values):
        if phase==self.phase:return
        self.phase=phase;self._phase_sequence+=1
        value={'phase':phase,'observed_at_utc':self.port.clock.now().isoformat().replace('+00:00','Z'),'account_key':self.kernel.account_key,**values}
        self.write(f'phase-{self._phase_sequence:04}.json',value);self.emit(value)

    def _capture_reference(self, reference):
        self.owner.capture('ONLINE_CLOCK_REFERENCE_REFRESH', {'clock': reference.record()})

    def step(self):
        k=self.kernel;p=self.port
        try:
            if isinstance(p.clock, CalibratedClock):
                p.clock.poll(before_install=self._capture_reference)
            p.poll_updates(0.1);self.owner.tick()
            now=p.clock.now();value=self.grant.value
            if time.monotonic()-self._last_money>=10:
                self.owner.capture('ONLINE_ACCOUNT_REFRESH',{'snapshot':p.snapshot()});self._last_money=time.monotonic()
            cycles=[cycle for cycle in k._staged.values() if cycle['plan'].get('approval_record_id')==value['grant_id']]
            if cycles:
                self._entered=True
                for cycle in cycles:
                    for order in cycle['orders']:
                        if order['terminal_confirmed']:continue
                        first=self._submitted_at.setdefault(order['child_order_id'],time.monotonic())
                        if time.monotonic()-first>=15 and order['child_order_id'] not in self._cancelled:
                            k.request_cancel(order['child_order_id']);k.wait_idle();self._cancelled.add(order['child_order_id'])
                    if cycle['protection']['owned_qty']:
                        self.status('HOLDING_WITH_KERNEL_PROTECTION',contract_id=cycle['plan']['contract_id'],owned_quantity=cycle['protection']['owned_qty'],deadline=cycle.get('online_deadline'))
                if all(c['lifecycle']=='CLOSED' for c in cycles):
                    k.pause();k.wait_idle()
                    final=p.snapshot();self.owner.capture('ONLINE_FINAL_SNAPSHOT',{'snapshot':final})
                    self.result={'status':'CLOSED' if any(c['protection']['entry_filled_qty'] for c in cycles) else 'NO_ENTRY_FILL',
                        'execution_scope':'ONLINE_SIMULATION','grant_id':value['grant_id'],'snapshot':k.snapshot(),'account_final':final,
                        'money_report':k.money.report(),'cycles':cycles,'native_orders_sent':len(p._issued) if hasattr(p,'_issued') else len(p.sent),
                        'source':'PRODUCT_KERNEL_ORIGINAL_LEDGER','phase':self.phase}
                    self.write('result.json',self.result);self.status(self.result['status']);self.stop.set();return
                return
            pauses=k.ledger.query("SELECT event_seq FROM event_journal WHERE event_type='PAUSE_NEW' AND event_seq>?",(self._start_sequence,))
            if pauses or k._authority_revision!=self._start_authority_revision:
                self.result={'status':'PAUSED_BEFORE_OPEN','source':'PRODUCT_KERNEL_ORIGINAL_LEDGER'};self.write('result.json',self.result)
                self.status('PAUSED_BEFORE_OPEN');self.stop.set();return
            if (self.output/'stop-before-open').exists():
                self.status('STOPPED_BEFORE_OPEN');self.result={'status':'STOPPED_BEFORE_OPEN'};self.write('result.json',self.result);self.stop.set();return
            if now<datetime.fromisoformat(value['not_before_utc'].replace('Z','+00:00')):
                self.status('WAITING_AUTHORIZED_OPEN_WINDOW',not_before_utc=value['not_before_utc']);return
            if now>datetime.fromisoformat(value['not_after_utc'].replace('Z','+00:00')):
                self.result={'status':'NO_ENTRY_WITHIN_WINDOW','source':'PRODUCT_KERNEL_ORIGINAL_LEDGER'};self.write('result.json',self.result)
                self.status('NO_ENTRY_WITHIN_WINDOW');self.stop.set();return
            if self._entered:return
            if isinstance(p.clock, CalibratedClock) and not p.clock.entry_ready():
                self.status('CLOCK_UNAVAILABLE_NEW_ENTRIES_PAUSED');return
            for code in value['contracts']:
                if not p.fresh(code) or p.position(code)!={'long':0,'short':0} or p.active_orders(code):continue
                market=p.market(code);seen=self._quotes.setdefault(code,set());seen.add(market['source_time'])
                if len(seen)<2:continue
                source=self.symbols[code]
                if any(source[key]=='0' for key in ('stop_offset','profit_offset')):continue
                funds=p.snapshot()['funds']
                if funds.get('available') is None or Decimal(funds['available'])<10000:continue
                self.owner.arm();k.wait_idle()
                plan=self.owner.plan(code,market['ask1'],stop_points=source['stop_offset'],take_profit_points=source['profit_offset'])
                self.write('selected-plan.json',plan)
                context=k.issue_entry_context('simulation-session',code)
                child=k.admit_entry(plan,'sim-opportunity-'+value['grant_id'],market['ask1'],context_id=context['context_id'],peer_id='simulation-session')
                self._entered=True;k.wait_idle();self._submitted_at[child]=time.monotonic()
                self.status('ENTRY_COMMITTED',contract_id=code,child_order_id=child,quantity=1);break
            else:self.status('WAITING_FRESH_UNOCCUPIED_CONTRACT')
        except Exception as error:
            code=getattr(error,'code','E_GATEWAY_RUNTIME')
            self._last_error=code
            if not k._failed:
                try:k.pause();k.wait_idle()
                except TraderError:pass
            self.status('BLOCKED_'+code,error_code=code)
            time.sleep(0.1)
            # No fresh opening follows a partially created intent. Original owned
            # protection, callbacks and reconciliation remain managed by this owner.


def run_session(scope,credential_file,output,*,pipe=None,prepare=False):
    grant=SimulationGrant.load(scope);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    lease = AccountLock(grant.account_key)
    port = kernel = session = None
    try:
        anchor = ProtectionClockAnchor(grant.value['ledger_path'], grant.account_key)
        clock = (CalibratedClock() if prepare else
                 CalibratedClock(recovery_anchor=anchor, recovering=lambda: legacy_protection_pending(grant)))
        port = TqKqPort.connect(credential_file, grant, clock, read_only=prepare)
        kernel=Kernel(grant.value['ledger_path'],grant.account_key,port=port,simulation_grant=grant,risk_budget='1000',
                      max_quote_age_ms=5000,_execution_lock=lease)
        baseline=kernel.simulation.initialize()
        start={'scope_hash':grant.content_hash,'execution_scope':'ONLINE_SIMULATION','kernel_boot_id':kernel.boot_id,'ledger_path':grant.value['ledger_path'],
            'clock':clock.record(),'baseline':baseline,'prepare_only':prepare}
        publish_new_bytes(canonical_bytes(start),output/'session-start.json')
        if prepare:
            print(json.dumps({'status':'PREPARED_DISARMED','kernel_boot_id':kernel.boot_id,'ledger_path':grant.value['ledger_path'],
                'contracts':grant.value['contracts'],'window':grant.value['not_before_utc'],'complete_snapshot':baseline['complete']},ensure_ascii=False));return 0
        session=SimulationSession(kernel,output)
        try:
            if pipe:
                from online_trader.kernel.server import ControlServer
                ControlServer(kernel,pipe).serve(session.stop,owner_tick=session.step)
            else:
                while not session.stop.is_set():session.step()
        except (KeyboardInterrupt,Exception) as error:
            kernel.pause();kernel.wait_idle()
            if any(c['lifecycle']!='CLOSED' for c in kernel._staged.values()):
                kernel.flatten([cid for cid,c in kernel._staged.items() if c['lifecycle']!='CLOSED']);kernel.wait_idle()
                session.status('CONTROL_INTERRUPTED_EXITING_OWNED_ONLY',error_code=getattr(error,'code','E_CONTROL_INTERRUPTED'))
                # IPC or terminal interruption must not stop the execution owner.
                while not session.stop.is_set():
                    try:session.step()
                    except KeyboardInterrupt:continue
            else:session.status('INTERRUPTED_BEFORE_OPEN')
        if session.result and session.result.get('status')=='CLOSED':
            # Close the sender before creating a separate read-only full login.
            port.close()
            verification=None
            for attempt in range(3):
                reader=None
                try:
                    reader=TqKqPort.connect(credential_file,grant,port.clock,read_only=True)
                    reader.source_epoch=port.source_epoch+1
                    fresh=reader.snapshot()
                    owned={port.order_id(o['child_order_id']) for c in session.result['cycles'] for o in c['orders'] if o['send_state']!='DEFINITELY_NOT_SENT'}
                    codes={c['plan']['contract_id'] for c in session.result['cycles']}
                    by_id={o['order_id']:o for o in fresh['orders']}
                    if not fresh['complete'] or not owned<=by_id.keys() or any(by_id[oid]['status']!='FINISHED' for oid in owned):
                        raise TraderError('E_FACT_BARRIER','Final native order snapshot is incomplete')
                    if any(p['contract_id'] in codes and (p['long'] or p['short']) for p in fresh['positions']):
                        raise TraderError('E_OWNERSHIP','Final selected contract is not flat')
                    for cycle in session.result['cycles']:
                        for order in cycle['orders']:
                            oid=port.order_id(order['child_order_id'])
                            if oid not in by_id and order['send_state']=='DEFINITELY_NOT_SENT':continue
                            if by_id[oid]['quantity']-by_id[oid]['remaining']!=order['filled_qty'] or sum(t['quantity'] for t in fresh['trades'] if t['order_id']==oid)!=order['filled_qty']:
                                raise TraderError('E_FACT_BARRIER','Final relogin order and detailed fills differ')
                    kernel.simulation.capture('ONLINE_FINAL_RELOGIN_VERIFIED',{'snapshot':fresh})
                    verification={'status':'PASS','mode':'FRESH_READ_ONLY_LOGIN','complete':True,'test_owned_flat':True,'active_owned_orders':[],
                        'native_snapshot':fresh,'durable_seq':str(kernel._durable_seq)}
                    session.write('final-relogin.json',verification);session.write('final-money-report.json',kernel.money.report());break
                except TraderError as error:
                    session.write(f'verification-failure-{attempt+1}.json',{'code':error.code});time.sleep(2)
                finally:
                    if reader is not None:reader.close()
            return 0 if verification is not None else 2
        return 2
    finally:
        try:
            if kernel is not None:kernel.close()
        finally:
            try:
                if port is not None:
                    port.close()
            finally:
                lease.close()
