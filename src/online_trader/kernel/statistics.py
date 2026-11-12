"""Commit-bound nominal DSV statistics for locally enrolled owned cycles.

The existing journal is the durable full-history receipt index. There is no second
writer, currency accounting claim, external position import or database migration.
"""

import copy
from datetime import datetime, timezone
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
import time

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.errors import TraderError
from online_trader.scheduling.clock import ClockSample
from online_trader.strategy.statistics import initial_state, apply_event, rollover_day, cooldown_gate
from online_trader.strategy.stops import evaluate_stops


_CLOSE_REASONS={'STOP_LOSS','BREAK_EVEN','TAKE_PROFIT','TRAILING_STOP','PROTECTED_FLOOR',
                'SESSION_CLOSE','ADVERSE_SLIPPAGE','DATA_GAP_EXIT','MANUAL_EXIT'}
_UNSET=object()


def _point(number):
    with localcontext() as context:
        context.prec=80
        value=number.quantize(Decimal('0.000000000001'),rounding=ROUND_HALF_EVEN)
    result=format(value,'f').rstrip('0').rstrip('.')
    return '0' if not value else result


class StatisticsOwner:
    def __init__(self,kernel):
        self.kernel=kernel;self._committed={};self._staged={};self._receipts={};self._profiles={};self._calendars={}
        self._restoring=True
        for row in kernel.ledger.query('SELECT payload_json FROM event_journal ORDER BY event_seq'):
            data=strict_loads(row['payload_json']);data=data.get('data',data)
            update=data.get('nominal_statistics')
            if update:self.accept(update)
        self._restoring=False

    def enroll(self,cycle):
        if cycle.get('entry_origin')!='STRATEGY':return
        metadata=cycle.get('strategy_metadata') or {};candidate=metadata.get('candidate') or {}
        rows=self.kernel.ledger.query('SELECT payload_json FROM config_revisions WHERE config_revision=?',
                                      (int(cycle['plan']['config_revision']),))
        if not rows:return
        payload=strict_loads(rows[0]['payload_json'])
        if payload.get('kind') not in ('DSV_DEVELOPMENT_EXECUTION','DSV_ONLINE_SIMULATION_EXECUTION') or payload.get('profile') is None:return
        profile=DsvProfile.from_record(payload['profile'])
        if profile.content_hash!=candidate.get('profile_hash'):raise TraderError('E_CONFIG','Statistics profile differs from the original strategy candidate')
        cycle['statistics_profile']=profile.to_record();self._profiles[profile.content_hash]=profile

    def _profile(self,cycle):
        record=cycle.get('statistics_profile')
        if record is None:return None
        profile=DsvProfile.from_record(record);self._profiles[profile.content_hash]=profile;return profile

    def observe_calendar(self,contract,days):
        if days is None:return
        if type(days) is not list or len(days)>512 or any(type(day) is not str for day in days):
            raise TraderError('E_STATISTICS_INPUT','Expected a bounded explicit trading-day calendar')
        for day in days:
            try:
                if datetime.fromisoformat(day).date().isoformat()!=day:raise ValueError
            except ValueError as error:raise TraderError('E_STATISTICS_INPUT','Invalid explicit trading day') from error
        if len(days)!=len(set(days)):raise TraderError('E_STATISTICS_INPUT','Repeated explicit trading day')
        self._calendars[contract]=sorted(days)

    def _days(self,contract):
        scheduler=getattr(self.kernel,'_scheduled_runtime',None)
        if scheduler is not None:
            record=scheduler.calendar.to_record()
            if record['complete']:
                item=next((row for row in record['contracts'] if row['contract_id']==contract),None)
                if item is not None:return sorted({s['trading_day'] for s in item['sessions']})
        return copy.deepcopy(self._calendars.get(contract,[]))

    def _base(self,contract,day,*,committed=False):
        rows=self._committed if committed else self._staged;key=(contract,day)
        if key in rows:return copy.deepcopy(rows[key]['state'])
        previous=[(d,row) for (c,d),row in rows.items() if c==contract and d<day]
        if not previous:return initial_state(self.kernel.account_key,contract,day)
        return rollover_day(max(previous,key=lambda pair:pair[0])[1]['state'],day)

    def _stop(self,profile,state,*,committed=False):
        rows=self._committed if committed else self._staged
        contract=state['contract_id'];day=state['trading_day'];history=set();month=None;latched=False;reason=None
        for (c,d),row in rows.items():
            if c!=contract or d>day:continue
            saved=row['stop_result']['state'];history.update(saved['stop_days'])
            if saved['month_stop']==day[:7]:month=saved['month_stop']
            if d==day and saved['day_stopped']:latched=True;reason=saved['day_stopped_reason']
        # A later NEXT_CYCLE profile may change thresholds, but is not a
        # release receipt for a month stop already latched by owned cycles.
        stopped=bool(state.get('day_stopped') or latched or month==day[:7])
        if month==day[:7]:reason='本月连续停机停开'
        return evaluate_stops(profile,contract,day,net_loss_points=state['net_loss_points'],stop_streak=state['stop_streak'],
            continuation_stopped=state['continuation_stopped'],day_stopped=stopped,
            day_stopped_reason=reason or state.get('day_stopped_reason'),stop_days=sorted(history)[-12:],
            month_stop=month,trading_days=self._days(contract))

    def _sample(self,cycle=None):
        scheduler=getattr(self.kernel,'_scheduled_runtime',None)
        if scheduler is not None and scheduler._sample is not None:
            sample=scheduler._sample
            return {'now_utc':sample.now_utc,'monotonic_ns':sample.monotonic_ns,'kernel_boot_id':self.kernel.boot_id,
                    'clock_basis':'SCHEDULED_MONOTONIC'}
        observed=(cycle or {}).get('statistics_observed_at_utc')
        return {'now_utc':observed or datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z'),
                'monotonic_ns':str(time.monotonic_ns()),'kernel_boot_id':self.kernel.boot_id,'clock_basis':'EXPLICIT_SOURCE_OR_LOCAL'}

    def mark_observation(self,cycle,at_utc=None):
        if cycle.get('statistics_profile') is None:return
        sample=self._sample()
        observed=sample['now_utc'] if at_utc is None else at_utc
        ClockSample(observed,sample['monotonic_ns'])
        cycle['statistics_observed_at_utc']=observed

    def freeze_exit(self,cycle,reason=None,*,reference_price=_UNSET):
        if cycle.get('statistics_profile') is None or cycle.get('nominal_exit') is not None:return
        if cycle['protection'].get('target_remaining_qty')!=0:return
        state=cycle['protection'];plan=cycle['plan']
        if reason not in _CLOSE_REASONS:
            reason=next((r for r in state.get('exit_reasons',[]) if r in _CLOSE_REASONS),None)
        if reason is None:
            reasons=cycle.get('reasons',[])
            reason='SESSION_CLOSE' if 'SESSION_CLOSE' in reasons else 'MANUAL_EXIT' if set(reasons)&{'MANUAL_EXIT','FLATTEN_THEN_STOP'} else None
        if reason is None:return
        price=(state.get('last_price') or (cycle.get('last_quote') or {}).get('price')) if reference_price is _UNSET else reference_price
        with localcontext() as context:
            context.prec=80
            if reason=='PROTECTED_FLOOR':
                sign=Decimal(1) if plan['direction']=='LONG' else Decimal(-1)
                price=_point(Decimal(plan['theoretical_entry_price'])+sign*Decimal(plan['fixed_take_profit']['distance_points'])*Decimal(plan['profit_protection']['floor_ratio']))
            slip=None
            if reason=='ADVERSE_SLIPPAGE' and state['entry_filled_qty']:
                sign=Decimal(1) if plan['direction']=='LONG' else Decimal(-1)
                slip=_point(max(Decimal(0),sign*(Decimal(state['entry_value'])/state['entry_filled_qty']-Decimal(plan['theoretical_entry_price']))))
        cycle['nominal_exit']={'exit_reason':reason,'nominal_exit_price':price,'adverse_slippage_points':slip,
            'trigger_clock':self._sample(cycle),'plan_hash':content_hash(plan)}

    def _event(self,cycle,profile,kind,evidence):
        plan=cycle['plan'];candidate=(cycle.get('strategy_metadata') or {}).get('candidate') or {}
        return {'type':kind,'event_id':'statistics-'+content_hash({'cycle':cycle['position_cycle_id'],'type':kind}),
            'evidence_id':evidence,'account_key':self.kernel.account_key,'contract_id':plan['contract_id'],
            'trading_day':plan['trading_day'],'position_cycle_id':cycle['position_cycle_id'],'plan_id':plan['plan_id'],
            'plan_hash':content_hash(plan),'profile_hash':profile.content_hash,'direction':plan['direction'],
            'entry_kind':candidate.get('entry_kind','NORMAL'),'theoretical_entry_price':plan['theoretical_entry_price'],
            'stop_distance':plan['fixed_stop_loss']['distance_points'],'take_profit_distance':plan['fixed_take_profit']['distance_points']}

    def _prepare(self,cycle,profile,event):
        key=(event['contract_id'],event['trading_day']);receipt_key=(event['position_cycle_id'],event['type'])
        if receipt_key in self._receipts:
            material={name:value for name,value in event.items() if name not in {'event_id','evidence_id'}}
            if content_hash(material)!=self._receipts[receipt_key]['event_hash']:
                raise TraderError('E_DUPLICATE_CONFLICT','Committed cycle statistics identity changed economic content')
            return None
        state=self._base(*key);result=apply_event(profile,state,event)
        stop=self._stop(profile,result['state'])
        update={'state':result['state'],'receipt':result['receipt'],'stop_result':stop,'profile':profile.to_record(),
                'prepared_sequence':self.kernel._sequence+1,
                'trading_days':self._days(key[0]),'nominal_exit':copy.deepcopy(cycle.get('nominal_exit')),
                'scope':'LOCAL_ENROLLED_OWNED_DSV_CYCLES','source_notes':result['source_notes']}
        self._staged[key]=copy.deepcopy(update)
        return update

    def prepare_entry(self,cycle,fill):
        profile=self._profile(cycle)
        if profile is None or cycle.get('statistics_entry_id') is not None:return None
        event=self._event(cycle,profile,'ENTRY_CONFIRMED',fill['trade_key'])
        event.update(requested_quantity=cycle['plan']['initial_open_quantity'],confirmed_quantity=fill['quantity'])
        update=self._prepare(cycle,profile,event)
        if update is not None:cycle['statistics_entry_id']=event['event_id']
        return update

    def prepare_close(self,cycle,at_utc=None):
        if cycle.get('external_reductions'):return None
        profile=self._profile(cycle)
        if profile is None or cycle.get('statistics_close_id') is not None:return None
        event=self._event(cycle,profile,'CYCLE_CLOSED',cycle['entry_reconciliation_id'])
        source=cycle.get('nominal_exit') or {}
        if not cycle['protection']['entry_filled_qty']:
            reason='NO_FILL'
        else:
            reason=source.get('exit_reason')
            if reason is None:raise TraderError('E_FACT_BARRIER','The original full-exit statistics cause is unproven')
        price=source.get('nominal_exit_price')
        if reason=='MANUAL_EXIT':
            with localcontext() as context:
                context.prec=80
                rows=self.kernel.ledger.query("SELECT f.price,a.quantity FROM fill_facts f JOIN fill_allocations a ON a.fill_id=f.fill_id WHERE a.position_cycle_id=? AND a.effect='REDUCE'",(cycle['position_cycle_id'],))
                total=sum(row['quantity'] for row in rows)
                if total!=cycle['protection']['entry_filled_qty']:raise TraderError('E_FACT_BARRIER','Confirmed owned manual exit details are incomplete')
                price=_point(sum(Decimal(row['price'])*row['quantity'] for row in rows)/total)
        sample=self._sample()
        event.update(flat_confirmed=True,orders_terminal=True,exit_reason=reason,closed_at_utc=at_utc or sample['now_utc'],
            exit_triggered_at_utc=(source.get('trigger_clock') or {}).get('now_utc'),nominal_exit_price=price,
            adverse_slippage_points=source.get('adverse_slippage_points'))
        update=self._prepare(cycle,profile,event)
        if update is not None:cycle['statistics_close_id']=event['event_id']
        return update

    def accept(self,update):
        state=update['state'];key=(state['contract_id'],state['trading_day'])
        self._committed[key]=copy.deepcopy(update)
        if self._staged.get(key,{}).get('prepared_sequence',0)<=update.get('prepared_sequence',0):
            self._staged[key]=copy.deepcopy(update)
        receipt=update['receipt'];self._receipts[(receipt['position_cycle_id'],receipt['type'])]=copy.deepcopy(receipt)
        profile=DsvProfile.from_record(update['profile']);self._profiles[profile.content_hash]=profile
        if self._restoring or state['contract_id'] not in self._calendars:
            self._calendars[state['contract_id']]=copy.deepcopy(update.get('trading_days',[]))

    def entry_reasons(self,contract,day,*,profile=None,direction=None,entry_kind='NORMAL',now_utc=None):
        barriers=[]
        for cycle in self.kernel._staged.values():
            if cycle['plan']['contract_id']!=contract:continue
            if cycle.get('statistics_review_required'):barriers.append('STATISTICS_LATE_FILL_REVIEW_REQUIRED')
            if cycle.get('statistics_entry_id') and not cycle.get('statistics_close_id') and cycle['protection']['owned_qty']==0:
                barriers.append('STATISTICS_CLOSE_RECONCILIATION_REQUIRED')
        rows=[row for (c,d),row in sorted(self._staged.items()) if c==contract and d<=day]
        if not rows:return list(dict.fromkeys(barriers))
        profile=profile or DsvProfile.from_record(rows[-1]['profile']);state=self._base(contract,day)
        stop=self._stop(profile,state);reasons=barriers+(list(stop['reasons']) if not stop['allow_new_entries'] else [])
        if entry_kind=='CONTINUATION' and not stop['allow_continuation']:reasons+=stop['reasons']
        trade=profile.to_record()['settings']['trade']
        if direction is not None and trade['stop_cooldown_enabled'] and state['cooldown_until'][direction] is not None:
            elapsed=None;origin=None
            for row in reversed(rows):
                candidate=row.get('nominal_exit') or {};clock=candidate.get('trigger_clock') or {}
                if clock.get('now_utc')==state['cooldown_started_at_utc']:origin=clock;break
            sample=self._sample()
            if origin and origin.get('kernel_boot_id')==self.kernel.boot_id and origin.get('clock_basis')==sample['clock_basis']=='SCHEDULED_MONOTONIC':
                delta=(int(sample['monotonic_ns'])-int(origin['monotonic_ns']))//1000000
                if 0<=delta<=2147483647:elapsed=delta
            gate=cooldown_gate(state,direction=direction,now_utc=now_utc or sample['now_utc'],elapsed_ms=elapsed)
            reasons+=gate['reasons']
        return list(dict.fromkeys(reasons))

    def snapshot(self,contract,day):
        self.kernel._check_owner();row=self._committed.get((contract,day))
        if row is None:return None
        result=copy.deepcopy(row);result.pop('profile',None)
        cycles=[c for c in self.kernel._committed.values() if c['plan']['contract_id']==contract and c['plan']['trading_day']==day and c.get('statistics_profile')]
        result['actual_confirmed_entry_quantity']=sum(c['protection']['entry_filled_qty'] for c in cycles)
        result['actual_confirmed_exit_quantity']=sum(c['protection']['exit_filled_qty'] for c in cycles)
        result['receipts']=[copy.deepcopy(value) for (cid,kind),value in self._receipts.items() if value['contract_id']==contract and value['trading_day']==day]
        result['currency_pnl']=None
        return result

    def business_state(self):
        return [copy.deepcopy(row) for _,row in sorted(self._committed.items())]
