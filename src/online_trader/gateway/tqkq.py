"""Optional TqKq ONLINE_SIM adapter. SDK imports occur only on explicit connect."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import hashlib,math,re,time,uuid
from pathlib import Path

from online_trader.contracts.codec import content_hash
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.gateway.tq_wait import wait_update_slice
from online_trader.scheduling.clock import NS_PER_SECOND, quote_timestamp, source_age_ns
from online_trader.gateway.ports import OnlineSimulationPort


def number(value):
    if type(value) not in {int,float} or not math.isfinite(value):return None
    return decimal_text(Decimal(str(value)))


def quote_book(quote):
    return {key: number(getattr(quote, key, None)) for key in (
        'last_price', 'bid_price1', 'ask_price1', 'bid_volume1', 'ask_volume1',
        'price_tick', 'lower_limit', 'upper_limit', 'volume')}


def account_number(value):
    text = number(value)
    if text is None:
        return None
    with localcontext() as context:
        context.prec = 40
        return decimal_text(Decimal(text).quantize(Decimal('0.000000000001'), rounding=ROUND_HALF_EVEN))


def broker_account_facts(api, account):
    """Copy the native sections from one SDK update without inferring ownership."""
    funds = api.get_account(account=account)
    positions = [
        {'contract_id': code, 'long': position.pos_long, 'short': position.pos_short,
         'long_today': getattr(position, 'pos_long_today', None),
         'long_historical': getattr(position, 'pos_long_his', None),
         'short_today': getattr(position, 'pos_short_today', None),
         'short_historical': getattr(position, 'pos_short_his', None),
         'long_reference_price': number(getattr(position, 'open_price_long', None)),
         'short_reference_price': number(getattr(position, 'open_price_short', None))}
        for code, position in api.get_position(account=account).items()
        if position.pos_long or position.pos_short
    ]
    orders = [
        {'order_id': order_id, 'contract_id': order.exchange_id + '.' + order.instrument_id,
         'direction': order.direction, 'offset': order.offset, 'quantity': order.volume_orign,
         'remaining': order.volume_left, 'status': order.status, 'limit_price': number(order.limit_price)}
        for order_id, order in api.get_order(account=account).items()
    ]
    trades = [
        {'trade_id': trade_id, 'order_id': trade.order_id, 'contract_id': trade.exchange_id + '.' + trade.instrument_id,
         'direction': trade.direction, 'offset': trade.offset, 'quantity': trade.volume,
         'price': number(trade.price), 'source_time_ns': str(trade.trade_date_time)}
        for trade_id, trade in api.get_trade(account=account).items()
    ]
    amounts = ('balance', 'pre_balance', 'available', 'margin', 'frozen_margin', 'close_profit',
               'position_profit', 'float_profit', 'commission')
    account_funds = {key: account_number(getattr(funds, key, None)) for key in amounts}
    # Preserve the broker value until the strategy applies its risk-ratio precision.
    account_funds['risk_ratio'] = number(getattr(funds, 'risk_ratio', None))
    return {'funds': account_funds, 'positions': positions, 'orders': orders, 'trades': trades}


def credentials(path):
    from online_trader.accounts import StoredCredential,StoredSimNowCredential
    if type(path) is StoredCredential:return path.resolve()
    if type(path) is StoredSimNowCredential:raise TraderError('E_CREDENTIALS','A SimNow credential reference cannot connect to KQ')
    try:
        raw=Path(path).read_bytes()
        if len(raw)>16384:raise ValueError()
        for encoding in ('utf-8-sig','gb18030'):
            try:lines=raw.decode(encoding).splitlines();break
            except UnicodeError:pass
        else:raise ValueError()
        values={};inside=False
        for line in lines:
            if '快期' in line and '账号' in line:inside=True;continue
            if not inside:continue
            parts=re.split('[:：=]',line,maxsplit=1)
            if len(parts)!=2:continue
            key={'用户名':'username','密码':'password'}.get(parts[0].strip())
            if key:
                if key in values:raise ValueError()
                values[key]=parts[1].strip()
        if set(values)!={'username','password'} or not all(values.values()):raise ValueError()
        return values
    except (OSError,ValueError):raise TraderError('E_CREDENTIALS','Cannot read the explicit KQ credential fields') from None


class TqKqPort(OnlineSimulationPort):
    provider='TQKQ'
    @classmethod
    def connect(cls,path,grant,clock,*,read_only=False):
        if getattr(grant,'provider','TQKQ')!='TQKQ':
            raise TraderError('E_ONLINE_SCOPE','A SimNow scope cannot connect to a KQ trading account')
        auth=credentials(path)
        if hashlib.sha256(auth['username'].encode()).hexdigest()[:16]!=grant.value['account_fingerprint']:
            raise TraderError('E_SCOPE','KQ account differs from the explicit grant')
        try:
            from tqsdk import TqApi,TqKq,TqAuth
        except ImportError:raise TraderError('E_DEPENDENCY','The optional simulation SDK is not installed') from None
        api=None
        try:
            account=TqKq()
            if type(account) is not TqKq:raise TraderError('E_SCOPE','Only TqKq online simulation is supported')
            api=TqApi(account=account,auth=TqAuth(auth['username'],auth['password']),debug=False,web_gui=False,disable_print=True)
            result=cls(api,account,grant,clock);result.read_only=read_only
            deadline=time.monotonic()+25
            while not result.complete() or number(getattr(api.get_account(account=account),'balance',None)) is None or any(number(getattr(q,'price_tick',None)) is None for q in result.quotes.values()):
                if time.monotonic()>=deadline:raise TraderError('E_FACT_BARRIER','Simulated initial full sync did not finish')
                result.poll_updates(0.2)
            result.broker_connected=True
            return result
        except Exception:
            if api is not None:api.close()
            raise TraderError('E_GATEWAY_CONNECT','KQ simulated login failed; credential values are not echoed') from None

    def __init__(self,api,account,grant,clock):
        if getattr(grant,'provider','TQKQ')!=self.provider:
            raise TraderError('E_ONLINE_SCOPE','Scope provider differs from its native adapter')
        self.api,self.account,self.grant,self.clock=api,account,grant,clock
        self._bindings={};self._issued=set();self._known_notifications=set(api._data.get('notify',{}))
        self._transport_up=True;self._md_transport_up=True;self._td_transport_up=True;self.fault=False;self.source_epoch=1
        self.read_only=False
        self.broker_connected=False
        self._closed=False
        self._opening_consumed=False
        self.quotes={code:api.get_quote(code) for code in grant.value['contracts']}
        self.gateway_identity=self.provider+':'+grant.value['account_fingerprint']

    @staticmethod
    def order_id(child_id):return 'otsim'+hashlib.sha256(child_id.encode()).hexdigest()[:27]

    def bind(self,kernel):
        super().bind(kernel)
        for cycle in kernel._staged.values():
            if cycle['plan'].get('approval_record_id')==self.grant.value['grant_id']:self._opening_consumed=True
            for order in cycle['orders']:
                self._issued.add(order['child_order_id'])
                self._bindings[order['child_order_id']]={'contract_id':cycle['plan']['contract_id'],
                    'direction':'BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL',
                    'effect':order['effect'],'original_qty':order['original_qty'],'limit_price':order['limit_price']}
                if order.get('native_offset'):self._bindings[order['child_order_id']]['native_offset']=order['native_offset']

    def poll_updates(self,seconds=0.1):
        trace = getattr(self, '_quote_trace', None)
        started_ns = time.monotonic_ns() if trace is not None else None
        updated = wait_mode = None
        try:
            updated, wait_mode = wait_update_slice(self.api, seconds)
            self.last_poll_updated = updated
        finally:
            if trace is not None:
                trace.sdk_wait(started_ns, time.monotonic_ns(), updated, wait_mode=wait_mode)
        capture = getattr(self.clock, 'capture_live', None) or getattr(self.clock, 'capture', None)
        sample = capture() if capture is not None else None
        received = ((sample.utc, sample.monotonic) if sample is not None
                    else (datetime.now(timezone.utc), time.monotonic_ns()))
        for key,item in self.api._data.get('notify',{}).items():
            if key in self._known_notifications:continue
            self._known_notifications.add(key);code=item.get('code')
            text=' '.join(str(item.get(field,'')) for field in ('content','url','conn_id')).lower()
            is_md=('行情' in text or 'md' in text) and not ('交易' in text or 'td' in text)
            is_td=('交易' in text or 'td' in text) and not ('行情' in text or 'md' in text)
            if code in {2019112910,2019112911}:
                self.fault=True
                if is_md:self._md_transport_up=False
                elif is_td:self._td_transport_up=False
                else:self._md_transport_up=self._td_transport_up=False
                self._transport_up=self._md_transport_up and self._td_transport_up
            elif code==2019112902:
                self.fault=True
                if is_md:self._md_transport_up=True
                elif is_td:self._td_transport_up=True
                else:self._md_transport_up=self._td_transport_up=True
                self._transport_up=self._md_transport_up and self._td_transport_up
                self.source_epoch+=1
        if trace is not None:
            trace.sdk_quotes(self.quotes, received[0], self.source_epoch, self.fault)
            trace.sdk_clock(sample, self.clock)
        return received

    def complete(self):return self._transport_up and not self.api._account._get_trade_more_data(self.api._data)

    def entry_clock_ready(self):
        ready = getattr(self.clock, 'entry_ready', None)
        return ready is None or ready()

    def check_dispatch_clock(self, effect):
        if not self.protection_clock_ready():
            raise TraderError('E_CLOCK','Application UTC protection window expired')
        if effect == 'OPEN' and not self.entry_clock_ready():
            raise TraderError('E_CLOCK','Application UTC calibration is unavailable for opening')

    def protection_clock_ready(self):
        ready = getattr(self.clock, 'protection_ready', None)
        return ready is None or ready()

    def _quote_rejected(self, code, check, reason, **facts):
        quote = self.quotes.get(code)
        stamp = getattr(quote, 'datetime', None)
        self._last_quote_rejection = {
            'check': check, 'reason': reason, 'source_epoch': self.source_epoch,
            'source_time_raw': stamp[:64] if isinstance(stamp, str) else None, 'book': quote_book(quote), **facts}
        trace = getattr(self, '_quote_trace', None)
        if trace is not None:
            trace.record('NATIVE_QUOTE_REJECTED', contract_id=code,
                         raw_time=self._last_quote_rejection['source_time_raw'], **self._last_quote_rejection)
        return False

    def fresh(self, code, *, check_age=True, now=None):
        if not self.protection_clock_ready():
            return self._quote_rejected(code, 'NATIVE_BOOK', 'PROTECTION_CLOCK_UNAVAILABLE')
        try:
            q=self.quotes[code];at=quote_timestamp(q.datetime, timezone(timedelta(hours=8)))
            if now is None:
                now = self.clock.utc_now() if hasattr(self.clock, 'utc_now') else self.clock.now()
            age = source_age_ns(now, at, q.datetime)
            values=(q.bid_price1,q.ask_price1,q.last_price,q.price_tick,q.lower_limit,q.upper_limit)
            if q.expired:
                return self._quote_rejected(code, 'NATIVE_BOOK', 'INSTRUMENT_EXPIRED')
            if not self._quote_age_acceptable(code, now, at, age, check_age):
                return False
            if not all(math.isfinite(value) and value > 0 for value in values):
                return self._quote_rejected(code, 'NATIVE_BOOK', 'INVALID_PRICE_OR_LIMIT')
            if not q.lower_limit <= q.bid_price1 <= q.ask_price1 <= q.upper_limit:
                return self._quote_rejected(code, 'NATIVE_BOOK', 'CROSSED_OR_OUT_OF_LIMIT_BOOK')
            if not (q.bid_volume1 > 0 and q.ask_volume1 > 0):
                return self._quote_rejected(code, 'NATIVE_BOOK', 'EMPTY_BOOK_SIDE',
                                            bid_size=number(q.bid_volume1), ask_size=number(q.ask_volume1))
            return True
        except (KeyError, AttributeError, TypeError, ValueError, TraderError) as error:
            return self._quote_rejected(code, 'NATIVE_BOOK', 'UNREADABLE_QUOTE',
                                        error_code=getattr(error, 'code', type(error).__name__))

    def _quote_age_acceptable(self, code, now, source, age, check_age):
        if age < -NS_PER_SECOND:
            return self._quote_rejected(code, 'NATIVE_AGE', 'QUOTE_LEADS_CLOCK',
                                        source_age_ns=str(age), minimum_age_ns=str(-NS_PER_SECOND))
        if check_age and age > 5 * NS_PER_SECOND:
            return self._quote_rejected(code, 'NATIVE_AGE', 'QUOTE_AGE_LIMIT',
                                        source_age_ns=str(age), maximum_age_ns=str(5 * NS_PER_SECOND))
        return True

    def fresh_manual_reduction(self,code):
        """Validate a current native quote for a frozen manual reduction limit."""
        return self._fresh_last(code)

    def _fresh_last(self, code, *, check_age=True):
        try:
            q = self.quotes[code]
            stamp = quote_timestamp(q.datetime, timezone(timedelta(hours=8)))
            now = self.clock.utc_now() if hasattr(self.clock, 'utc_now') else self.clock.now()
            age = source_age_ns(now, stamp, q.datetime)
            values = (q.last_price,q.price_tick,q.lower_limit,q.upper_limit)
            if q.expired:
                return self._quote_rejected(code, 'NATIVE_LAST', 'INSTRUMENT_EXPIRED')
            if not self._quote_age_acceptable(code, now, stamp, age, check_age):
                return False
            if not all(math.isfinite(value) and value > 0 for value in values):
                return self._quote_rejected(code, 'NATIVE_LAST', 'INVALID_PRICE_OR_LIMIT')
            if not q.lower_limit <= q.last_price <= q.upper_limit:
                return self._quote_rejected(code, 'NATIVE_LAST', 'LAST_OUT_OF_LIMIT')
            return True
        except (KeyError, AttributeError, TypeError, ValueError, TraderError) as error:
            return self._quote_rejected(code, 'NATIVE_LAST', 'UNREADABLE_QUOTE',
                                        error_code=getattr(error, 'code', type(error).__name__))

    def position(self,code):
        p=self.api.get_position(code,account=self.account)
        return {'long':p.pos_long,'short':p.pos_short}

    def active_orders(self,code):
        return [o for o in self.api.get_order(account=self.account).values() if o.exchange_id+'.'+o.instrument_id==code and o.status=='ALIVE']

    def instrument(self,code):
        q=self.quotes[code]
        return {'tick_size':number(q.price_tick),'multiplier':number(q.volume_multiple),'currency':'CNY'}

    def market(self,code):
        q=self.quotes[code]
        return {'price':number(q.last_price),'bid1':number(q.bid_price1),'ask1':number(q.ask_price1),'source_time':q.datetime,
                'lower_limit':number(q.lower_limit),'upper_limit':number(q.upper_limit),**self.instrument(code)}

    def snapshot(self):
        facts = broker_account_facts(self.api, self.account)
        return {'snapshot_id':'kq-snapshot-'+uuid.uuid4().hex,'gateway_identity':self.gateway_identity,'source_epoch':self.source_epoch,
            'account_key':self.grant.account_key,'trading_day':self.grant.value['trading_day'],'complete':self.complete(),
            **facts}

    def validate_send(self,request):
        self.check_dispatch_clock(request['effect'])
        if (self.read_only or self.fault or not self.complete() or request['account_key']!=self.grant.account_key or request['contract_id'] not in self.quotes
                or request['effect'] not in {'OPEN','REDUCE'} or request['trading_day']!=self.grant.value['trading_day'] or type(request['original_qty']) is not int or request['original_qty']!=1 or not self.fresh(request['contract_id'])):
            raise TraderError('E_GATEWAY_SCOPE','Fresh, complete matching simulated scope is required')
        if request['effect']=='OPEN':
            if self._opening_consumed or request['direction']!='BUY' or not self.grant.in_window(self.clock.now()) or self.position(request['contract_id'])!={'long':0,'short':0} or self.active_orders(request['contract_id']):
                raise TraderError('E_GATEWAY_SCOPE','Opening window or selected contract is occupied')
        else:
            p=self.position(request['contract_id'])
            if request['direction']!='SELL' or p!={'long':1,'short':0}:raise TraderError('E_OWNERSHIP','Only the single owned long lot may be reduced')
        q=self.quotes[request['contract_id']];price=Decimal(request['limit_price']);tick=Decimal(str(q.price_tick))
        if price%tick or not Decimal(str(q.lower_limit))<=price<=Decimal(str(q.upper_limit)):
            raise TraderError('E_PRICE_ALIGNMENT','Online price is not a legal tick within exchange limits')

    def send(self,request):
        self.validate_send(request);child=request['child_order_id']
        if child in self._issued:raise TraderError('E_DUPLICATE_CONFLICT','An online send may never be replayed')
        # Bind before the native call: an exception can still leave an accepted order.
        self._issued.add(child);self._bindings[child]={key:request[key] for key in ('contract_id','direction','effect','original_qty','limit_price')}
        if request['effect']=='OPEN':self._opening_consumed=True
        return self.api.insert_order(symbol=request['contract_id'],direction=request['direction'],offset='OPEN' if request['effect']=='OPEN' else 'CLOSE',
            volume=1,limit_price=float(Decimal(request['limit_price'])),order_id=self.order_id(child),account=self.account)

    def cancel(self,request):
        child=request['child_order_id']
        if self.read_only or request['account_key']!=self.grant.account_key or child not in self._bindings:raise TraderError('E_OWNERSHIP','Cannot cancel an unowned online order')
        return self.api.cancel_order(self.order_id(child),account=self.account)

    def observations(self):
        result=[]
        all_orders=self.api.get_order(account=self.account)
        for child,binding in self._bindings.items():
            oid=self.order_id(child);o=all_orders.get(oid)
            if o is None or o.volume_orign==0:continue
            if (o.exchange_id+'.'+o.instrument_id!=binding['contract_id'] or o.direction!=binding['direction']
                    or o.offset!=binding.get('native_offset',('OPEN' if binding['effect']=='OPEN' else 'CLOSE')) or type(o.volume_orign) is not int
                    or o.volume_orign!=binding['original_qty'] or number(o.limit_price)!=binding['limit_price']):
                raise TraderError('E_SCOPE','Online callback does not match the committed owned order')
            if type(o.volume_left) is not int or not 0<=o.volume_left<=o.volume_orign or o.status not in {'ALIVE','FINISHED'}:
                raise TraderError('E_FACT_BARRIER','Native order quantity or status is invalid')
            fills=[]
            for tid,t in o.trade_records.items():
                if (t.order_id!=oid or t.exchange_id+'.'+t.instrument_id!=binding['contract_id'] or t.direction!=o.direction or t.offset!=o.offset):
                    raise TraderError('E_SCOPE','Fill is outside the bound native order')
                if (not isinstance(tid,str) or not tid or type(t.volume) is not int or t.volume<=0
                        or number(t.price) is None or Decimal(number(t.price))<=0
                        or type(t.trade_date_time) is not int or t.trade_date_time<=0):
                    raise TraderError('E_FACT_BARRIER','Native fill identity, quantity, price or time is invalid')
                fills.append(self.trade_identity(tid)|{'quantity':t.volume,'price':number(t.price),'source_time_ns':str(t.trade_date_time)})
            if sum(f['quantity'] for f in fills)>o.volume_orign-o.volume_left:
                raise TraderError('E_FACT_BARRIER','Native detailed fills exceed the reported execution quantity')
            result.append({'child_order_id':child,'order_id':oid,'contract_id':binding['contract_id'],'status':o.status,
                'reported_fill_qty':o.volume_orign-o.volume_left,'remaining':o.volume_left,'fills':fills})
        return result

    def close(self):
        if not self._closed:self.api.close();self._closed=True;self.broker_connected=False
