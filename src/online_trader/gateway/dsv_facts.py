"""Native tick/account facts for DSV; no synthetic path or matching engine."""
from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_FLOOR
import math

from online_trader.configuration.rollover import CHINA_TIME, expiry_date
from online_trader.contracts.codec import content_hash
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import number
from online_trader.strategy.entry import is_night_session
from online_trader.strategy.risk import calculate_daily_loss


def utc(moment):return moment.isoformat(timespec='microseconds').replace('+00:00','Z')


def _tick_prices(raw):
    values = {name: number(raw.get(field)) for name,field in (
        ('last','last_price'),('bid1','bid_price1'),('ask1','ask_price1'),
        ('day_high','highest'),('day_low','lowest'))}
    for side in ('bid1','ask1'):
        if values[side] is not None and Decimal(values[side]) <= 0:
            values[side] = None
    return values


def native_integer(value,*,minimum=0):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value!=int(value) or value<minimum:
        raise TraderError('E_FACT_BARRIER','Native integer fact is missing or invalid')
    return int(value)


class DsvFactFeed:
    def __init__(self,port,profile,calendar):
        if profile.content_hash!=port.grant.value['profile_hash'] or calendar.content_hash!=port.grant.value['calendar_hash']:
            raise TraderError('E_ONLINE_SCOPE','Native fact conversion requires the original profile and calendar')
        self.port,self.profile,self.calendar=port,profile,calendar
        self._accepted={};self._sequences={};self._path={};self._revision=0;self._last_native={}
        self.resume_anchors={}
        self.rebuild_paths=set()
        trade = profile.to_record()['settings']['trade']
        self._continuation_seconds = float(trade['chase_window_minutes']) * 60 if trade['continuation_entry'] else None
        self._night_references = {}

    def batches(self, *, resume_states=None, calendar_event=None):
        result=[];now=self.port.clock.now()
        for code in self.port.grant.value['contracts']:
            at=self.calendar.query(code,utc(now))
            if not at['complete'] or not at['is_trading']:continue
            if not self.port.grant.permits_day(at['trading_day']):
                raise TraderError('E_SCOPE','Current native quotes belong to a different proved trading day')
            rows=[]
            for raw in self.port.tick_records(code):
                try:index=native_integer(raw['id'])
                except (TraderError,KeyError):continue # SDK's uninitialized padding is not a market event.
                native_time=raw.get('datetime')
                if type(native_time) in {int,float} and native_time==0:
                    # TqSdk can fill unprovided tick slots with a nonnegative
                    # index and default timestamp. Remaining IDs still prove
                    # continuity, so a hole in the actual path stays visible.
                    continue
                try:stamp=native_integer(native_time,minimum=1)
                except TraderError:raise TraderError('E_FACT_BARRIER','A native tick identity has no valid source time') from None
                moment=datetime.fromtimestamp(stamp//1_000_000_000,timezone.utc)
                moment=moment.replace(microsecond=(stamp%1_000_000_000)//1000)
                if moment>now:continue
                rows.append((index,moment,raw))
            rows.sort(key=lambda row:row[0])
            if len({row[0] for row in rows})!=len(rows):
                raise TraderError('E_DUPLICATE_CONFLICT','Native tick serial contains duplicate identities')
            if any(b[1]<a[1] for a,b in zip(rows,rows[1:])):
                raise TraderError('E_FACT_BARRIER','Native tick identities move backwards in source time')
            if not rows:continue
            session=at['session'];start=datetime.fromisoformat(session['open_utc'])
            close=datetime.fromisoformat(session['close_utc'])
            key=(code,self.port.source_epoch,session['session_id'])
            contiguous=all(b[0]==a[0]+1 for a,b in zip(rows,rows[1:]))
            first_index=next((index for index,row in enumerate(rows) if row[1]>=start),len(rows))
            session_rows=[row for row in rows[first_index:] if row[1]<close]
            if not session_rows:continue
            boundary_rows=rows[max(0,first_index-1):first_index+len(session_rows)]
            session_contiguous=all(b[0]==a[0]+1 for a,b in zip(boundary_rows,boundary_rows[1:]))
            accepted_key=(code,self.port.source_epoch)
            previous=self._accepted.get(accepted_key)
            if previous is None and contiguous and resume_states is not None:
                state=resume_states.get((code,at['trading_day']))
                if state is not None:
                    previous=self._resume_anchor(code,session,rows,state,calendar_event)
            prior=next((row for row in rows if row[0]==previous),None)
            if prior is not None:
                normalized={'native_time_ns':str(native_integer(prior[2]['datetime'],minimum=1))}
                normalized.update(_tick_prices(prior[2]))
                if normalized!=self._last_native[(code,self.port.source_epoch)]:
                    raise TraderError('E_DUPLICATE_CONFLICT','An accepted native tick identity changed its source facts')
            pending=[row for row in session_rows if previous is None or row[0]>previous]
            start_ns=(int(start.timestamp())*1_000_000+start.microsecond)*1000
            same_session=previous is not None and int(self._last_native[accepted_key]['native_time_ns'])>=start_ns
            ready=self.port.subscription_ready_at.get(code)
            boundary_proved=session_contiguous and (first_index>0 or session_rows[0][1]==start)
            subscribed_at_open=session_contiguous and ready is not None and ready<=start
            if key in self.rebuild_paths and not boundary_proved:
                self.rebuild_paths.remove(key)
                self._path[key]=False
            gap=bool(same_session and pending and pending[0][0]!=previous+1)
            pending_contiguous=all(b[0]==a[0]+1 for a,b in zip(pending,pending[1:]))
            if not session_contiguous or gap:
                self.port.request_tick_backfill(code)
            if gap or not pending_contiguous:self._path[key]=False
            elif key not in self._path:self._path[key]=boundary_proved or subscribed_at_open
            if not pending:continue
            if not self._path[key] and boundary_proved:
                if same_session:
                    # Rebuild the whole proved segment before publishing a complete
                    # quote. An accepted incomplete tick cannot supply the proof.
                    self.rebuild_paths.add(key)
                self._path[key]=True
            if key in self.rebuild_paths:pending=session_rows
            events=[]
            night_reference = None
            if (contiguous and self._path[key] and self._continuation_seconds is not None
                    and start.astimezone(CHINA_TIME).strftime('%H:%M') == '09:00'
                    and (pending[0][1] - start).total_seconds() <= self._continuation_seconds):
                cached = self._night_references.get(code)
                if cached is not None and cached[0] == key:
                    night_reference = cached[1]
                else:
                    night_reference = self._night_reference(code, rows, start, close, at['trading_day'])
                    if night_reference is not None:
                        self._night_references[code] = (key, night_reference)
            for row in pending:
                event = self._event(code,session,row,self._sequences.get(accepted_key,0)+len(events)+1,self._path[key])
                if night_reference is not None:
                    event['night_reference'] = night_reference
                events.append(event)
            if events:result.append((code,events))
        return result

    def _night_reference(self, code, rows, start, close, trading_day):
        first = next((i for i, row in enumerate(rows) if start <= row[1] < close), None)
        if first is None or first == 0:
            return None
        index, moment, final = rows[first - 1]
        before = self.calendar.query(code, utc(moment))
        if not before['complete'] or not before['is_trading'] or before['trading_day'] != trading_day:
            return None
        session = before['session']
        opened = datetime.fromisoformat(session['open_utc'])
        if not is_night_session(opened) or datetime.fromisoformat(session['close_utc']) > start:
            return None
        last = number(final.get('last_price'))
        high = number(final.get('highest'))
        low = number(final.get('lowest'))
        if None in (last, high, low) or not 0 < Decimal(low) <= Decimal(last) <= Decimal(high):
            return None
        high, low = Decimal(high), Decimal(low)
        for _, previous_time, raw in reversed(rows[:first - 1]):
            if previous_time < opened:
                break
            previous_high = number(raw.get('highest'))
            previous_low = number(raw.get('lowest'))
            if previous_high is not None and Decimal(previous_high) > 0:
                high = max(high, Decimal(previous_high))
            if previous_low is not None and Decimal(previous_low) > 0:
                low = min(low, Decimal(previous_low))
        return {'session_id': session['session_id'], 'source_time_utc': utc(moment),
                'source_epoch': self.port.source_epoch, 'sequence': 0, 'native_tick_id': str(index),
                'high': decimal_text(high), 'low': decimal_text(low), 'last': last, 'boundary_proved': True}

    def _event(self,code,session,native,sequence,path_complete):
        index,moment,row=native
        values = _tick_prices(row)
        if any(values[key] is None or Decimal(values[key]) <= 0 for key in ('last','day_high','day_low')):
            raise TraderError('E_FACT_BARRIER','Native tick lacks legal prices or full day high/low facts')
        return {'account_key':self.port.grant.account_key,'contract_id':code,'trading_day':session['trading_day'],
            'session_id':session['session_id'],'source_epoch':self.port.source_epoch,
            'sequence':sequence,'native_tick_id':str(index),'native_time_ns':str(native_integer(row['datetime'],minimum=1)),
            'source_time_utc':utc(moment),'session_open_utc':session['open_utc'],'session_close_utc':session['close_utc'],
            **values,'tick_size':self.port.instrument(code)['tick_size'],'quality':'CONTIGUOUS','day_range_complete':True,
            'path_complete':path_complete,'is_trading':True,'calendar_complete':True,
            'candle_id':code+':'+moment.strftime('%Y%m%dT%H%M'),'is_opening_session':session['is_opening_session']}

    def _resume_anchor(self,code,session,rows,state,calendar_event):
        if (state['coverage_lost'] or state['session_id']!=session['session_id']
                or not 0<state['source_epoch']<=self.port.source_epoch or not state['last_source_time_utc']):
            return None
        from online_trader.strategy.entry import market_fingerprint
        boundary=datetime.fromisoformat(state['last_source_time_utc'])
        matches=[]
        for row in rows:
            if row[1]!=boundary:
                continue
            event=self._event(code,session,row,state['last_sequence'],True)
            enriched=calendar_event(event|{'source_epoch':state['source_epoch']})
            if market_fingerprint(enriched,session['is_opening_session'])==state['last_event_hash']:
                matches.append(event)
        if len(matches)!=1:
            return None
        event=matches[0]
        if state['source_epoch']!=self.port.source_epoch:
            self.resume_anchors[(code,self.port.source_epoch)]=calendar_event(event)
        self._path[(code,self.port.source_epoch,session['session_id'])]=True
        self.accept(event)
        return int(event['native_tick_id'])

    def accept(self,event):
        key=(event['contract_id'],event['source_epoch'])
        self.rebuild_paths.discard((*key,event['session_id']))
        self._accepted[key]=int(event['native_tick_id']);self._sequences[key]=event['sequence']
        self._last_native[key]={field:event[field] for field in ('native_time_ns','last','bid1','ask1','day_high','day_low')}

    def facts(self,event,kernel):
        p=self.port;code=event['contract_id'];snapshot=p.snapshot();kernel.simulation._validate_snapshot(snapshot)
        funds=snapshot['funds']
        positions=p.api.get_position(account=p.account)
        selected=p.api.get_position(code,account=p.account)
        self._revision+=1
        covered=[]
        for cid,cycle in kernel._staged.items():
            if cycle['lifecycle'] == 'CLOSED':
                continue
            native=p.position(cycle['plan']['contract_id'])
            side='long' if cycle['plan']['direction']=='LONG' else 'short'
            if native[side]==cycle['protection']['owned_qty']:covered.append(cid)
        equity=funds.get('balance');margin=funds.get('margin')
        ratio = funds.get('risk_ratio')
        if ratio is not None:
            native_ratio = Decimal(ratio)
            # Every supported cap is aligned to this scale. Flooring keeps the
            # native result of risk_ratio >= cap even just below the boundary.
            ratio = (decimal_text(native_ratio.quantize(Decimal('0.000000000001'), rounding=ROUND_FLOOR))
                     if -1 < native_ratio < 10**16 else None)
        stopped=[]
        for row in self.profile.to_record()['settings']['symbols']:
            stop=kernel.statistics._staged.get((row['code'],event['trading_day']),{}).get('stop_result',{})
            if stop.get('allow_new_entries') is False:
                stopped.append(row['code'])
        orders_today=sum(o['contract_id']==code for o in snapshot['orders'])
        if p.grant.multi_day:
            from online_trader.gateway.native_days import native_day
            native=p.api.get_order(account=p.account);orders_today=0
            for order in snapshot['orders']:
                if order['contract_id']!=code:continue
                stamp=getattr(native.get(order['order_id']),'insert_date_time',None)
                if type(stamp) is int:
                    try:orders_today+=native_day(self.calendar,code,str(stamp))['trading_day']==event['trading_day']
                    except TraderError:pass  # A missing native-day diagnostic does not alter strategy target counts.
        symbol_margin=number(getattr(selected,'margin',None))
        if symbol_margin is None and not any(row['contract_id']==code for row in snapshot['positions']):
            # The full snapshot contains every nonzero native position.
            symbol_margin='0'
        account={'account_key':kernel.account_key,'trading_day':event['trading_day'],'mode':'simulation','revision':self._revision,
            'complete':snapshot['complete'],'equity':equity,'available':funds.get('available'),'margin':margin,
            'frozen_margin':funds.get('frozen_margin'),'pending_margin':'0','symbol_margin':symbol_margin,
            'risk_ratio':ratio,'daily_loss':calculate_daily_loss(balance=equity,pre_balance=funds.get('pre_balance'),
                close_profit=funds.get('close_profit'),float_profit=funds.get('float_profit'),commission=funds.get('commission')),
            'orders_today':orders_today,
            'active_symbols':sorted(key for key,value in positions.items() if value.pos_long or value.pos_short),
            'stopped_symbols':sorted(stopped),'unknown_orders':kernel._has_unknown(),
            'local_reservations_excluded':True,'covered_local_cycle_ids':covered}
        quote = p.quotes[code]
        expiry = expiry_date(getattr(quote, 'expire_datetime', None))
        days_to_expiry = (datetime.combine(expiry,time.min,CHINA_TIME)-p.clock.now()).days if expiry else None
        instrument={'contract_id':code,'trading_day':event['trading_day'],'complete':True,**p.instrument(code),
            'margin_rate':None,'margin_per_lot':number(getattr(quote,'margin',None)),
            'lower_limit':number(quote.lower_limit),'upper_limit':number(quote.upper_limit),
            'min_order_qty':native_integer(getattr(quote,'min_limit_order_volume',None),minimum=1),
            'max_order_qty':native_integer(getattr(quote,'max_limit_order_volume',None),minimum=1),
            'days_to_expiry':days_to_expiry}
        return account,instrument
