"""Native tick/account facts for DSV; no synthetic path or matching engine."""
from datetime import datetime,timezone
from decimal import Decimal
import math

from online_trader.contracts.codec import content_hash
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import number
from online_trader.strategy.risk import calculate_daily_loss


def utc(moment):return moment.isoformat(timespec='microseconds').replace('+00:00','Z')


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

    def batches(self):
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
                try:stamp=native_integer(raw['datetime'],minimum=1)
                except (TraderError,KeyError):raise TraderError('E_FACT_BARRIER','A native tick identity has no valid source time') from None
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
            key=(code,self.port.source_epoch,session['session_id'])
            contiguous=all(b[0]==a[0]+1 for a,b in zip(rows,rows[1:]))
            previous=self._accepted.get((code,self.port.source_epoch))
            prior=next((row for row in rows if row[0]==previous),None)
            if prior is not None:
                normalized={'native_time_ns':str(native_integer(prior[2]['datetime'],minimum=1))}
                normalized.update({key:number(prior[2].get(field)) for key,field in (
                    ('last','last_price'),('bid1','bid_price1'),('ask1','ask_price1'),('day_high','highest'),('day_low','lowest'))})
                if normalized!=self._last_native[(code,self.port.source_epoch)]:
                    raise TraderError('E_DUPLICATE_CONFLICT','An accepted native tick identity changed its source facts')
            pending=[row for row in rows if previous is None or row[0]>previous]
            if not pending:continue
            gap=previous is not None and pending[0][0]!=previous+1
            ready=self.port.subscription_ready_at.get(code)
            proof=contiguous and not gap and (rows[0][1]<=start or ready is not None and ready<=start)
            if gap or not contiguous:self._path[key]=False
            elif key not in self._path:self._path[key]=proof
            elif previous is None and proof and rows[0][1]<=start:
                # A larger native window can now prove the entire session
                # before any event was accepted. Prime will rebuild disarmed.
                self._path[key]=True
            events=[]
            for index,moment,row in pending:
                if moment<start or moment>=datetime.fromisoformat(session['close_utc']):continue
                values={name:number(row.get(native)) for name,native in (
                    ('last','last_price'),('bid1','bid_price1'),('ask1','ask_price1'),('day_high','highest'),('day_low','lowest'))}
                if any(value is None or Decimal(value)<=0 for value in values.values()):
                    raise TraderError('E_FACT_BARRIER','Native tick lacks legal prices or full day high/low facts')
                tick=self.port.instrument(code)['tick_size']
                event={'account_key':self.port.grant.account_key,'contract_id':code,'trading_day':at['trading_day'],
                    'session_id':session['session_id'],'source_epoch':self.port.source_epoch,
                    'sequence':self._sequences.get((code,self.port.source_epoch),0)+len(events)+1,'native_tick_id':str(index),
                    'native_time_ns':str(native_integer(row['datetime'],minimum=1)),
                    'source_time_utc':utc(moment),'session_open_utc':session['open_utc'],'session_close_utc':session['close_utc'],
                    **values,'tick_size':tick,'quality':'CONTIGUOUS','day_range_complete':True,
                    'path_complete':self._path[key],'is_trading':True,'calendar_complete':True,
                    'candle_id':code+':'+moment.strftime('%Y%m%dT%H%M'),'is_opening_session':session['is_opening_session']}
                events.append(event)
            if events:result.append((code,events))
        return result

    def accept(self,event):
        key=(event['contract_id'],event['source_epoch'])
        self._accepted[key]=int(event['native_tick_id']);self._sequences[key]=event['sequence']
        self._last_native[key]={field:event[field] for field in ('native_time_ns','last','bid1','ask1','day_high','day_low')}

    def facts(self,event,kernel):
        p=self.port;code=event['contract_id'];snapshot=p.snapshot();kernel.simulation._validate_snapshot(snapshot)
        funds=snapshot['funds'];native_orders={o['order_id']:o for o in snapshot['orders']}
        positions=p.api.get_position(account=p.account)
        selected=p.api.get_position(code,account=p.account)
        self._revision+=1
        covered=[];attempts=[]
        for cid,cycle in kernel._staged.items():
            native=p.position(cycle['plan']['contract_id'])
            side='long' if cycle['plan']['direction']=='LONG' else 'short'
            if native[side]==cycle['protection']['owned_qty']:covered.append(cid)
            if cycle['plan']['contract_id']==code:
                attempts.extend(o['attempt_id'] for o in cycle['orders'] if p.order_id(o['child_order_id']) in native_orders
                    and o.get('native_trading_day',cycle['plan']['trading_day'])==event['trading_day'])
        equity=funds.get('balance');margin=funds.get('margin')
        ratio=decimal_text(Decimal(margin)/Decimal(equity)) if equity is not None and Decimal(equity)>0 and margin is not None else None
        stopped=[]
        for row in self.profile.to_record()['settings']['symbols']:
            stop=kernel.statistics._staged.get((row['code'],event['trading_day']),{}).get('stop_result',{})
            if stop.get('allow_new_entries') is False or not kernel.control.symbol_entry_enabled(row['code']):stopped.append(row['code'])
        orders_today=sum(o['contract_id']==code for o in snapshot['orders'])
        if p.grant.multi_day:
            from online_trader.gateway.native_days import native_day
            native=p.api.get_order(account=p.account);orders_today=0
            for order in snapshot['orders']:
                if order['contract_id']!=code:continue
                stamp=getattr(native[order['order_id']],'insert_date_time',None)
                if type(stamp) is not int:raise TraderError('E_FACT_BARRIER','Daily order count needs original native order times')
                orders_today+=native_day(self.calendar,code,str(stamp))['trading_day']==event['trading_day']
        account={'account_key':kernel.account_key,'trading_day':event['trading_day'],'mode':'simulation','revision':self._revision,
            'complete':snapshot['complete'],'equity':equity,'available':funds.get('available'),'margin':margin,
            'frozen_margin':funds.get('frozen_margin'),'pending_margin':'0','symbol_margin':number(getattr(selected,'margin',None)),
            'risk_ratio':ratio,'daily_loss':calculate_daily_loss(balance=equity,pre_balance=funds.get('pre_balance'),
                close_profit=funds.get('close_profit'),float_profit=funds.get('float_profit'),commission=funds.get('commission')),
            'orders_today':orders_today,
            'active_symbols':sorted(key for key,value in positions.items() if value.pos_long or value.pos_short),
            'stopped_symbols':sorted(stopped),'unknown_orders':kernel._has_unknown(),
            'local_reservations_excluded':True,'covered_local_cycle_ids':covered,'covered_local_attempt_ids':attempts}
        quote=p.quotes[code];expiry=number(getattr(quote,'expire_datetime',None))
        instrument={'contract_id':code,'trading_day':event['trading_day'],'complete':True,**p.instrument(code),
            'margin_rate':None,'margin_per_lot':number(getattr(quote,'margin',None)),
            'lower_limit':number(quote.lower_limit),'upper_limit':number(quote.upper_limit),
            'min_order_qty':native_integer(getattr(quote,'min_limit_order_volume',None),minimum=1),
            'max_order_qty':native_integer(getattr(quote,'max_limit_order_volume',None),minimum=1),
            'days_to_expiry':None if expiry is None else (datetime.fromtimestamp(float(expiry),timezone.utc)-p.clock.now()).days}
        return account,instrument
