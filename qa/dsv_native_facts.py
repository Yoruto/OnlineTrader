"""External native SDK-shaped facts. Records sends; fills require explicit calls."""
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace as NS
import copy
import time


class FactClock:
    def __init__(self,value='2026-11-10T01:06:00Z'):
        self.value=datetime.fromisoformat(value);self.monotonic_ns=time.monotonic_ns();self.monotonic_anchor=time.monotonic()
    def now(self):return self.value
    def record(self):return {'mode':'EXPLICIT_OFFLINE_FACT_CLOCK','os_clock_changed':False}
    def sample(self):return {'now_utc':self.value.isoformat().replace('+00:00','Z'),'monotonic_ns':str(self.monotonic_ns)}
    def advance(self,milliseconds):self.value+=timedelta(milliseconds=milliseconds);self.monotonic_ns+=milliseconds*1_000_000


class _NativeColumn(list):
    def __ge__(self, threshold):
        return [value >= threshold for value in self]


class _NativeRowSelection:
    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, mask):
        return NativeRows([row for row, selected in zip(self.rows, mask, strict=True) if selected])


class NativeRows:
    def __init__(self, rows=None):self.rows=[] if rows is None else rows

    def __getitem__(self, column):
        return _NativeColumn(row[column] for row in self.rows)

    @property
    def loc(self):
        return _NativeRowSelection(self.rows)

    @property
    def iloc(self):
        return self.rows

    def to_dict(self,mode):assert mode=='records';return copy.deepcopy(self.rows)
    def tail(self, count):
        return NativeRows(self.rows[-count:])


class NativeFactApi:
    def __init__(self,clock,contracts=('DCE.p2701',)):
        self.clock=clock;self._account=NS(_get_trade_more_data=lambda data:False)
        self._data={'notify':{}};self.orders={};self.trades={};self.inserted=[];self.cancelled=[];self.closed=0
        self.positions={code:NS(pos_long=0,pos_short=0,pos_long_today=0,pos_long_his=0,pos_short_today=0,pos_short_his=0,margin=0.) for code in contracts}
        self.quotes={code:NS(datetime='2026-11-10 09:06:00.000000',expired=False,bid_price1=1054.,ask_price1=1056.,
            last_price=1055.,highest=1090.,lowest=1000.,price_tick=1.,lower_limit=800.,upper_limit=1200.,
            bid_volume1=10,ask_volume1=10,volume_multiple=10,min_limit_order_volume=1,max_limit_order_volume=100,
            expire_datetime=datetime(2027,1,20,tzinfo=timezone.utc).timestamp()) for code in contracts}
        self.serials={code:NativeRows() for code in contracts}
        self.funds=NS(balance=1000000.,pre_balance=1000000.,available=1000000.,margin=0.,frozen_margin=0.,
                      close_profit=0.,position_profit=0.,float_profit=0.,commission=0.,risk_ratio=0.)
    def get_quote(self,code):return self.quotes[code]
    def get_tick_serial(self,code,**kwargs):return self.serials[code]
    def is_serial_ready(self,serial):return bool(serial.rows)
    def get_account(self,**kwargs):return self.funds
    def get_position(self,code=None,**kwargs):return self.positions if code is None else self.positions[code]
    def get_order(self,**kwargs):return self.orders
    def get_trade(self,**kwargs):return self.trades
    def wait_update(self,**kwargs):return False
    def insert_order(self,**values):
        assert values['offset'] in {'OPEN','CLOSE','CLOSETODAY'},'The injected native API follows TqSdk offset spellings'
        self.inserted.append(copy.deepcopy(values));oid=values['order_id'];exchange,code=values['symbol'].split('.')
        self.orders[oid]=NS(exchange_id=exchange,instrument_id=code,direction=values['direction'],offset=values['offset'],
            status='ALIVE',volume_orign=values['volume'],volume_left=values['volume'],limit_price=values['limit_price'],trade_records={},
            insert_date_time=int(self.clock.now().timestamp()*1_000_000)*1000)
        return self.orders[oid]
    def cancel_order(self,oid,**kwargs):self.cancelled.append(oid)
    def terminal(self,oid):self.orders[oid].status='FINISHED'
    def fill(self,oid,quantity,price,*,trade_id=None,finish=None):
        order=self.orders[oid];assert 0<quantity<=order.volume_left
        tid=trade_id or 'fact-trade-'+str(len(self.trades)+1)
        fill=NS(order_id=oid,exchange_id=order.exchange_id,instrument_id=order.instrument_id,
            direction=order.direction,offset=order.offset,volume=quantity,price=float(price),
            trade_date_time=int(self.clock.now().timestamp()*1_000_000)*1000)
        self.trades[tid]=fill;order.trade_records[tid]=fill;order.volume_left-=quantity
        if finish is True or finish is None and order.volume_left==0:order.status='FINISHED'
        position=self.positions[order.exchange_id+'.'+order.instrument_id]
        side='pos_long' if (order.direction=='BUY')==(order.offset=='OPEN') else 'pos_short'
        setattr(position,side,getattr(position,side)+(quantity if order.offset=='OPEN' else -quantity))
        if order.offset=='OPEN':setattr(position,side+'_today',getattr(position,side+'_today')+quantity)
        elif order.exchange_id in {'SHFE','INE'}:
            bucket=side+('_his' if order.offset=='CLOSE' else '_today')
            setattr(position,bucket,getattr(position,bucket)-quantity)
        else:
            today=min(quantity,getattr(position,side+'_today'))
            setattr(position,side+'_today',getattr(position,side+'_today')-today)
            setattr(position,side+'_his',getattr(position,side+'_his')-(quantity-today))
        position.margin=(position.pos_long+position.pos_short)*1500.
    def tick(self,code='DCE.p2701',*,at=None,last=1055,high=1090,low=1000,bid=None,ask=None,index=None):
        at=self.clock.now() if at is None else datetime.fromisoformat(at)
        rows=self.serials[code].rows
        row={'id':(rows[-1]['id']+1 if rows else 0) if index is None else index,'datetime':int(at.timestamp()*1_000_000)*1000,
             'last_price':float(last),'highest':float(high),'lowest':float(low),
             'bid_price1':float(last-1 if bid is None else bid),'ask_price1':float(last+1 if ask is None else ask)}
        rows.append(row);quote=self.quotes[code]
        for source,target in (('last_price','last_price'),('highest','highest'),('lowest','lowest'),('bid_price1','bid_price1'),('ask_price1','ask_price1')):
            setattr(quote,target,row[source])
        quote.datetime=at.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S.%f')
        return row
    def close(self):self.closed+=1
