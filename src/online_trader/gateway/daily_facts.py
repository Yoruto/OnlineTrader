"""Daily bars from the already connected port; no SDK import or connection."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from io import StringIO
import csv
import time

from online_trader.configuration.offsets import DailyDataset,formula_source
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.gateway.dsv_facts import native_integer,utc
from online_trader.gateway.tqkq import number


def native_daily_rows(api, serials, cutoff, last):
    """Validate exact initialized bars and the explicitly completed source day."""
    rows=[];native=[]
    for identity,serial in serials.items():
        if not api.is_serial_ready(serial):raise TraderError('E_NOT_READY','Daily history has not synchronized')
        bars=[];indices=[]
        for raw in serial.to_dict('records'):
            try:index=native_integer(raw.get('id'))
            except TraderError:continue  # SDK uninitialized padding
            stamp=native_integer(raw.get('datetime'),minimum=1)
            seconds,nanos=divmod(stamp,1_000_000_000)
            # Same conversion as DSV time_to_datetime(...).strftime(date).
            at=(datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=seconds,microseconds=nanos//1000)).astimezone(timezone(timedelta(hours=8)))
            day=at.date().isoformat();values={k:number(raw.get(k)) for k in ('high','low','close')}
            if any(v is None for v in values.values()):raise TraderError('E_NOT_READY','Daily history contains incomplete price fields')
            if not Decimal(0)<Decimal(values['low'])<=Decimal(values['close'])<=Decimal(values['high']):
                raise TraderError('E_FACT_BARRIER','Native daily OHLC values are inconsistent')
            bars.append({'series_id':identity,'date':day,**values})
            indices.append(index)
            native.append({'series_id':identity,'native_id':str(index),'datetime_ns':str(stamp),'daily_date_label':day,**values})
        if not bars:raise TraderError('E_NOT_READY','No initialized daily bars are available')
        if len(set(indices))!=len(indices) or any(b!=a+1 for a,b in zip(indices,indices[1:])):
            raise TraderError('E_FACT_BARRIER','Native daily history has a duplicate or missing initialized index')
        if len({b['date'] for b in bars})!=len(bars):raise TraderError('E_FACT_BARRIER','Daily history contains duplicate date labels')
        if any(b['date']<a['date'] for a,b in zip(bars,bars[1:])):raise TraderError('E_FACT_BARRIER','Daily history date labels regress')
        # An active current-day label proves the SDK has rolled its daily
        # series. Never mistake a stale/in-progress prior label for a close.
        if bars[-1]['date']!=cutoff:raise TraderError('E_NOT_READY','The daily series has not rolled to the explicit cabinet-day label')
        completed=[b for b in bars if b['date']<cutoff]
        if not completed or completed[-1]['date']!=last:raise TraderError('E_NOT_READY','The exact last completed daily label is missing')
        rows.extend(bars)
    return rows, native


class NativeDailyFeed:
    def __init__(self,port,profile):
        if (not port.grant.automatic_offsets or profile.content_hash!=port.grant.value['profile_hash']
                or getattr(port,'kernel',None) is not None or port._issued):
            raise TraderError('E_AUTO_OFFSETS','Daily startup calculation must precede the original execution owner and sends')
        self.port=port;self.profile=profile
        settings=profile.to_record()['settings'];formula=settings['auto_offset']['formula']
        self.series={row['code']:formula_source(formula,row['name'],row['code'])[1]
                     for row in settings['symbols'] if row['code'] in port.grant.value['contracts']}
        self.serials={identity:port.api.get_kline_serial(identity,86400,data_length=120)
                      for identity in sorted(set(self.series.values()))}

    def snapshot(self):
        p=self.port;policy=p.grant.value['auto_offsets'];cutoff=policy['as_of_date'];last=policy['last_completed_day']
        if not p.grant.in_window(p.clock.now()):raise TraderError('E_AUTO_OFFSETS','Daily calculation is outside its finite source window')
        rows,native=native_daily_rows(p.api,self.serials,cutoff,last)
        ranges={};quotes=[]
        for code in self.series:
            window=p.calendar.query(code,utc(p.clock.now()))
            if (not window['complete'] or not window['is_trading'] or window['trading_day']!=cutoff or not p.fresh(code)):
                raise TraderError('E_NOT_READY','Current range needs a fresh quote in the explicit calculation day')
            q=p.quotes[code];high=number(getattr(q,'highest',None));low=number(getattr(q,'lowest',None))
            if high is None or low is None or not Decimal(0)<Decimal(low)<=Decimal(high):
                raise TraderError('E_NOT_READY','The native current-day range is incomplete')
            ranges[code]=decimal_text(Decimal(high)-Decimal(low))
            quotes.append({'contract_id':code,'source_time':q.datetime,'highest':high,'lowest':low})
        stream=StringIO();writer=csv.DictWriter(stream,('series_id','date','high','low','close'));writer.writeheader();writer.writerows(rows)
        dataset=DailyDataset.from_csv(stream.getvalue())
        evidence={'kind':'SAME_CONNECTED_PORT_NATIVE_DAILY','duration_seconds':86400,'requested_bars':120,
            'observed_at_reference_utc':utc(p.clock.now()),'source_epoch':p.source_epoch,'gateway_identity':p.gateway_identity,
            'daily_datetime_policy':'DSV_TIME_TO_DATETIME_BEIJING_DATE_LABEL','native_bars':native,'range_quotes':quotes,
            'live_verified_by_this_call':bool(p.broker_connected)}
        return dataset,ranges,evidence

    def wait(self,*,timeout_seconds=30):
        deadline=time.monotonic()+timeout_seconds
        now=self.port.clock.now()
        for code in self.series:
            at=self.port.calendar.query(code,utc(now));upcoming=at.get('next_session')
            if not at['is_trading'] and upcoming and upcoming['trading_day']==self.port.grant.value['trading_day']:
                lead=(datetime.fromisoformat(upcoming['open_utc'])-now).total_seconds()
                if 0<lead<=900:deadline=max(deadline,time.monotonic()+lead+timeout_seconds)
        while True:
            try:return self.snapshot()
            except TraderError as error:
                if error.code!='E_NOT_READY' or time.monotonic()>=deadline:raise
                self.port.poll_updates(.1)
