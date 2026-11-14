"""Finite DSV TqKq adapter; inherits native binding/callbacks, never a matcher."""
import asyncio
import copy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import math
import time

from online_trader.configuration.rollover import CHINA_TIME, expiry_date, future_product, rollover_hint
from online_trader.domain.values import order_price
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import TqKqPort, number
from online_trader.online_scope import DsvSimulationGrant


def _missing_book_price(value):
    return value is None or type(value) in (int, float) and (value == 0 or math.isnan(value))


class TqKqDsvPort(TqKqPort):
    def entry_clock_ready(self):
        try:
            self.clock.now()
        except TraderError as error:
            if error.code != 'E_CLOCK':
                raise
            return False
        return True

    def strategy_wall_time(self):
        return self.clock.now()

    def _matches_strategy_quote(self, quote, event):
        stamp = datetime.fromisoformat(quote.datetime).replace(tzinfo=CHINA_TIME)
        return stamp == datetime.fromisoformat(event['source_time_utc']) and number(quote.last_price) == event['last']

    def current_strategy_quote(self, event):
        """Prove the current native last price before restoring entry authority."""
        try:
            code = event['contract_id']
            quote = self.quotes[code]
            if not self._matches_strategy_quote(quote, event) or not self._fresh_last(code, check_age=False):
                return False
            order_price(event['last'], number(quote.price_tick))
            return True
        except (TraderError, KeyError, AttributeError, TypeError, ValueError):
            return False

    def current_protection_quote(self, event, now):
        """Match an executable native book to the price observation used for an exit."""
        code = event['contract_id']
        try:
            q = self.quotes[code]
            source = datetime.fromisoformat(event['source_time_utc'])
            if (not 0 <= (now - source).total_seconds() * 1000 <= self.kernel.max_quote_age_ms
                    or not self.fresh(code, now=now) or not self._matches_strategy_quote(q, event)
                    or (number(q.bid_price1), number(q.ask_price1), number(q.price_tick)) != (
                        event['bid1'], event['ask1'], event['tick_size'])
                    or not all(math.isfinite(size) for size in (q.bid_volume1, q.ask_volume1))
                    or not q.lower_limit <= q.last_price <= q.upper_limit):
                return False
            for price in ('last', 'bid1', 'ask1'):
                order_price(event[price], event['tick_size'])
            return True
        except (TraderError, KeyError, AttributeError, TypeError, ValueError):
            return False

    def fresh_strategy_entry(self, code, side, event):
        """Use the strategy age rule and verify the exact legal native quote."""
        if event is None:
            return False
        runtime = getattr(getattr(self, 'kernel', None), '_dsv_runtime', None)
        try:
            quote = self.quotes[code]
            if (not self._matches_strategy_quote(quote, event) or
                    (runtime is not None and runtime.ordinary_quote_stale(event))):
                return False
            field = 'ask1' if side == 'BUY' else 'bid1'
            opponent, other, volume = ((getattr(quote,'ask_price1',None), getattr(quote,'bid_price1',None),
                                        getattr(quote,'bid_volume1',None)) if side == 'BUY' else
                                       (getattr(quote,'bid_price1',None), getattr(quote,'ask_price1',None),
                                        getattr(quote,'ask_volume1',None)))
            if event[field] is not None:
                return (number(quote.ask_price1) == event['ask1'] and number(quote.bid_price1) == event['bid1']
                        and self.fresh(code, check_age=runtime is None))
            if not self._fresh_last(code, check_age=runtime is None) or not _missing_book_price(opponent):
                return False
            if not _missing_book_price(other) and not (type(other) in (int, float) and math.isfinite(other)
                    and quote.lower_limit <= other <= quote.upper_limit and type(volume) in (int, float)
                    and math.isfinite(volume) and volume > 0):
                return False
            order_price(event['last'], number(quote.price_tick))
            return True
        except (TraderError, KeyError, AttributeError, TypeError, ValueError):
            return False

    def __init__(self,api,account,grant,clock):
        if type(grant) is not DsvSimulationGrant:
            raise TraderError('E_ONLINE_SCOPE','The manual one-shot grant cannot enable DSV execution')
        super().__init__(api,account,grant,clock)
        self.calendar=None
        self.started_at=clock.now()
        self.subscription_ready_at={}
        # Pinned SDK 3.10.4 api.get_tick_serial explicitly caps native history
        # at 10000. Use its supported full window; never synthesize older ticks.
        self.tick_serials={code:api.get_tick_serial(code,data_length=10000) for code in grant.value['contracts']}
        self._tick_backfill_serials={}
        self._tick_backfill_attempts={}
        self._tick_backfill_after={}
        self._tick_backfill_pending=set()
        self.minute_chart_serials={}
        self.minute_chart_requested=False
        self.minute_chart_sample_at = None
        self.minute_chart_cache = {}
        self._rollover_main = {}
        self._rollover_catalog = {}
        self._rollover_poll_at = None
        self._rollover_task = None

    async def _subscribe_rollover_quotes(self, epoch):
        products = dict.fromkeys(filter(None, map(future_product, self.quotes)))
        for exchange, product in products:
            if epoch != self.source_epoch:
                return
            key = exchange, product
            try:
                self._rollover_main[key] = self.api.get_quote(f"KQ.m@{exchange}.{product}")
            except Exception:
                pass
        for exchange, product in products:
            if epoch != self.source_epoch:
                return
            key = exchange, product
            try:
                listed = await asyncio.wait_for(self.api.query_quotes(
                    ins_class="FUTURE", exchange_id=exchange, product_id=product, expired=False), timeout=8)
                if epoch != self.source_epoch:
                    return
                codes = list(dict.fromkeys(code for code in listed if future_product(code) == key))
                if len(codes) <= 64:
                    self._rollover_catalog[key] = dict(zip(codes, self.api.get_quote_list(codes))) if codes else {}
            except Exception:
                continue

    def _refresh_rollover_quotes(self):
        if (not self.broker_connected or self.read_only or self.fault or self.calendar is None or
                not all(callable(getattr(self.api, name, None)) for name in
                        ("create_task", "query_quotes", "get_quote_list"))):
            return
        now = time.monotonic()
        if self._rollover_poll_at is not None and now - self._rollover_poll_at < 300:
            return
        if self._rollover_task is not None and not self._rollover_task.done():
            return
        self._rollover_poll_at = now
        task = self._subscribe_rollover_quotes(self.source_epoch)
        try:
            self._rollover_task = self.api.create_task(task)
        except Exception:
            task.close()

    def _rollover_quote_matches_day(self, code, quote, day):
        if day is None or self.calendar is None or self.fault:
            return False
        try:
            stamp = datetime.fromisoformat(quote.datetime)
            stamp = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=CHINA_TIME)
            now = self.clock.now()
            if stamp > now:
                return False
            at = self.calendar.query(code, stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
            return at["complete"] and at["is_trading"] and at["trading_day"] == day
        except (AttributeError, TypeError, ValueError, TraderError):
            return False

    def rollover_market_facts(self, code, day):
        """Read already received contract metadata separately from order and tick maps."""
        product = future_product(code)
        current = self.quotes.get(code)
        if product is None or current is None:
            return {}
        main_quote = self._rollover_main.get(product)
        main = (getattr(main_quote, "underlying_symbol", None)
                if self._rollover_quote_matches_day(code, main_quote, day) else None)
        catalog = self._rollover_catalog.get(product, {})
        activity = {}
        if code in catalog and self._rollover_quote_matches_day(code, current, day):
            for candidate, quote in (catalog | {code: current}).items():
                if not getattr(quote, "expired", False) and self._rollover_quote_matches_day(code, quote, day):
                    fields = (number(getattr(quote, key, None)) for key in ("open_interest", "volume"))
                    activity[candidate] = tuple(Decimal(value) if value is not None else None for value in fields)
        facts = rollover_hint(code, main, activity)
        current_expiry = expiry_date(getattr(current, "expire_datetime", None))
        main_expiry = expiry_date(getattr(current if main == code else catalog.get(main), "expire_datetime", None))
        try:
            today = self.clock.now().astimezone(CHINA_TIME).date().isoformat()
        except TraderError:
            today = None
        return facts | {"as_of_date": today,
                        "expiry_date": current_expiry.isoformat() if current_expiry else None,
                        "main_expiry_date": main_expiry.isoformat() if main_expiry else None}

    async def _subscribe_minute_charts(self):
        # TqSdk's public call waits synchronously for initialization outside its
        # own loop. Inside the SDK loop it returns the chart frame immediately.
        for code in self.tick_serials:
            try:self.minute_chart_serials[code]=self.api.get_kline_serial(code,60,data_length=500)
            except Exception:continue

    def minute_chart_bars(self):
        from online_trader.gateway.dsv_facts import native_integer
        from online_trader.gateway.tqkq import number
        if (not self.minute_chart_requested and self.broker_connected and
                callable(getattr(self.api,'create_task',None))):
            self.minute_chart_requested=True
            task=self._subscribe_minute_charts()
            try:self.api.create_task(task)
            except Exception:task.close()
        if self.fault:
            self.minute_chart_sample_at = None
            self.minute_chart_cache = {}
            return {}
        now = time.monotonic()
        if self.minute_chart_sample_at is not None and now - self.minute_chart_sample_at < 6:
            return self.minute_chart_cache
        result={}
        for code,serial in self.minute_chart_serials.items():
            try:
                if not self.api.is_serial_ready(serial):continue
                bars={}
                for row in serial.tail(60).to_dict('records'):
                    try:stamp=native_integer(row.get('datetime'),minimum=60_000_000_000)
                    except TraderError:continue
                    opening,high,low,closing=(number(row.get(key)) for key in ('open','high','low','close'))
                    if any(value is None or len(value)>32 for value in (opening,high,low,closing)):continue
                    o,h,l,c=map(Decimal,(opening,high,low,closing))
                    if 0<l<=min(o,c)<=max(o,c)<=h:
                        bars[stamp//60_000_000_000]=[opening,high,low,closing]
                if bars:result[code]=[[minute,*bars[minute]] for minute in sorted(bars)]
            except (AttributeError,TypeError,ValueError,ArithmeticError):
                continue
        self.minute_chart_sample_at = now
        self.minute_chart_cache = result
        return result

    def configure_calendar(self,calendar):
        if calendar.content_hash!=self.grant.value['calendar_hash']:
            raise TraderError('E_ONLINE_SCOPE','The native adapter calendar differs from the strategy grant')
        self.calendar=calendar

    def snapshot(self, *, now=None):
        value=super().snapshot()
        if self.calendar is None:raise TraderError('E_CALENDAR','Explicit strategy calendar is required')
        now=(self.clock.now() if now is None else now).isoformat().replace('+00:00','Z')
        windows=[self.calendar.query(code,now) for code in self.grant.value['contracts']]
        days=set()
        for item in windows:
            day=item['trading_day'] or (item['next_session'] or {}).get('trading_day')
            if day is None and item['complete'] and item['is_trading'] is False:
                # The covered interval after the last session still needs its
                # cabinet day to reconcile terminal orders and verify flatness.
                transition=self.calendar.transition(item['contract_id'],item['coverage_from_utc'],now)
                if transition['complete'] and transition['days']:
                    day=transition['days'][-1]
            days.add(day)
        # Day labels come from explicit current calendar evidence, never from
        # blindly copying the requested grant into a native-current claim.
        value['trading_day']=next(iter(days)) if len(days)==1 else None
        value['complete']=bool(value['complete'] and all(item['complete'] for item in windows)
                               and len(days)==1 and self.grant.permits_day(value['trading_day']))
        value['trading_day_source']='BOUND_EXPLICIT_CALENDAR_AT_VERIFIED_REFERENCE'
        return value

    def poll_updates(self,seconds=0.1):
        epoch=self.source_epoch
        self._refresh_rollover_quotes()
        received = super().poll_updates(seconds)
        if self.source_epoch!=epoch:
            self.started_at=self.clock.now();self.subscription_ready_at.clear()
            self.minute_chart_sample_at = None
            self.minute_chart_cache = {}
            if self._rollover_task is not None:
                self._rollover_task.cancel()
                self._rollover_task = None
            self._rollover_poll_at = None
            self._rollover_main.clear()
            self._rollover_catalog.clear()
        return received

    def observations(self):
        from online_trader.gateway.native_days import native_day
        observations=super().observations()
        orders=self.api.get_order(account=self.account)
        owner = getattr(getattr(self, 'kernel', None), 'simulation', None)
        for observation in observations:
            item=orders[observation['order_id']]
            stamp=getattr(item,'insert_date_time',None)
            child, code = observation['child_order_id'], observation['contract_id']
            if getattr(item, 'is_error', None) is True:
                observation['rejected'] = True
            try:
                if type(stamp) is not int:raise TraderError('E_FACT_BARRIER','Missing native order time')
                observation['order_day_evidence'] = (owner.native_day_proof(child, code, str(stamp)) if owner
                                                     else native_day(self.calendar, code, str(stamp)))
            except TraderError as error:observation['order_day_error']=error.code
            for fill in observation['fills']:
                try:
                    stamp = fill['source_time_ns']
                    proof = (owner.native_day_proof(child, code, stamp, fill=True) if owner
                             else native_day(self.calendar, code, stamp))
                    fill.update(trading_day=proof['trading_day'],native_day_evidence=proof)
                except TraderError as error:fill['native_day_error']=error.code
        return observations

    def validate_send(self,request):
        value=self.grant.value;kernel=getattr(self,'kernel',None)
        if (self.read_only or self.fault or not self.complete() or kernel is None
                or request.get('account_key')!=self.grant.account_key or request.get('contract_id') not in self.quotes
                or not self.grant.permits_day(request.get('trading_day')) or request.get('direction') not in {'BUY','SELL'}
                or request.get('effect') not in {'OPEN','REDUCE'} or type(request.get('original_qty')) is not int
                or not 1<=request['original_qty']<=value['max_order_quantity']):
            raise TraderError('E_GATEWAY_SCOPE','A fresh, complete and exactly owned DSV simulated order is required')
        cycle,_=kernel._find(request['child_order_id'])
        order=next(o for o in cycle['orders'] if o['child_order_id']==request['child_order_id'])
        if (cycle['position_cycle_id']!=request.get('position_cycle_id') or cycle.get('management_state')=='TAKEN_OVER'
                or kernel.simulation.archived_cycle(cycle)
                or any(order[key]!=request[key] for key in ('effect','original_qty','limit_price','native_offset'))):
            raise TraderError('E_OWNERSHIP','Native order is not the exact committed original child')
        scheduler = getattr(kernel, '_scheduled_runtime', None)
        protection_now = None
        if scheduler is not None and scheduler._fault:
            if order['effect'] == 'REDUCE':
                protection_now = scheduler.clock_protection_time(cycle)
            if protection_now is None:
                raise TraderError('E_CLOCK_FAULT', '本地自动保护需要重新确认原盘次与实时行情。')
        snapshot=self.snapshot(now=protection_now)
        if not snapshot['complete'] or snapshot['trading_day']!=request['trading_day']:
            raise TraderError('E_FACT_BARRIER','Current trading day is outside the explicit native scope')
        manual = kernel.execution.manual_reduction(cycle) if order['effect']=='REDUCE' else None
        direct = (manual and manual.get('price')==request['limit_price']
                  and cycle['execution'].get('manual_reduction_children',{}).get(order['child_order_id'])==manual['request_id'])
        code = request['contract_id']
        metadata = cycle.get('strategy_metadata') or {}
        candidate = metadata.get('candidate') or {}
        runtime = getattr(kernel, '_dsv_runtime', None)
        facts = runtime._latest.get((code, request['trading_day'])) if runtime is not None else None
        if protection_now is not None:
            # The scheduler matched the native book immediately above. Check
            # original orders again, excluding just this unsent durable child.
            kernel.simulation.verify_clock_protection(cycle, snapshot, exclude_child=request['child_order_id'])
            fresh = True
        elif request['effect']=='OPEN' and candidate.get('entry_kind')=='NORMAL' and cycle['entry_origin']=='STRATEGY':
            fresh = self.fresh_strategy_entry(code, request['direction'], facts['event'] if facts is not None else None)
        else:
            fresh = self.fresh(code) or (direct and self.fresh_manual_reduction(code))
        if not fresh:
            raise TraderError('E_NATIVE_QUOTE_NOT_CURRENT', '报单需要当前有效的原生报价。')
        expected='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL'
        if request['direction']!=expected:raise TraderError('E_OWNERSHIP','Native order direction differs from the original cycle')
        position=self.position(request['contract_id'])
        owned={side:sum(c['protection']['owned_qty'] for c in kernel._staged.values()
                       if c['plan']['contract_id']==request['contract_id'] and c['plan']['direction']==side.upper()) for side in ('long','short')}
        if position!=owned:raise TraderError('E_OWNERSHIP','External position changes block this native send')
        owned_ids={self.order_id(o['child_order_id']) for c in kernel._staged.values() for o in c['orders']}
        if any(o['order_id'] not in owned_ids for o in snapshot['orders']
               if o['contract_id']==request['contract_id'] and o['status']=='ALIVE'):
            raise TraderError('E_OWNERSHIP','An active foreign order blocks native execution on the selected contract')
        if request['effect']=='OPEN':
            if (cycle['entry_origin']=='MANUAL')!=self.grant.manual_workbench:
                raise TraderError('E_GATEWAY_SCOPE','Native opening origin differs from the finite scope')
            if not self.entry_clock_ready():
                raise TraderError('E_CLOCK','A valid Windows wall clock is required immediately before native opening send')
            current=datetime.fromisoformat(self.quotes[request['contract_id']].datetime).replace(tzinfo=timezone(timedelta(hours=8)))
            if facts is None or current!=datetime.fromisoformat(facts['event']['source_time_utc']):
                raise TraderError('E_GATEWAY_SCOPE','The entry proposal does not reflect the latest native quote')
            if cycle.get('manual_limit_order'):
                kernel.simulation.manual_entry_facts(cycle['strategy_metadata']['candidate'],request['original_qty'],
                    exclude_decision=cycle['decision_key'])
            if not self.grant.in_window(self.clock.now()):
                raise TraderError('E_ENTRY_WINDOW_EXPIRED', '当前时刻超出已确认的开仓窗口。')
            if kernel.recovery_required:
                raise TraderError('RECONCILIATION_REQUIRED', '原委托和持仓需要核对。')
            if not kernel.simulation.target_quota(code, request['trading_day'], existing=cycle['decision_key']):
                reason = 'DAILY_ORDER_LIMIT' if self.grant.daily_targets_per_contract is not None else 'E_ENTRY_ATTEMPT_LIMIT'
                raise TraderError(reason, '本次授权允许的开仓额度已用完。')
            if any(o['order_id'] != self.order_id(request['child_order_id']) for o in self.active_orders(code)):
                raise TraderError('OWNED_EXPOSURE_EXISTS', '已有活动委托。')
        elif request['original_qty']>owned['long' if expected=='SELL' else 'short']:
            raise TraderError('E_OWNERSHIP','Native reduction exceeds the proved owned side')
        if request['effect']=='REDUCE':
            if not kernel.simulation.exit_day_ready(cycle,request['trading_day']):
                raise TraderError('E_FACT_BARRIER','Original exits require current-day native reconciliation')
            kernel.simulation.verify_foreign_orders(cycle,snapshot)
            from online_trader.execution.close_routes import preview_owned_routes
            state=cycle['protection'];target=state.get('target_remaining_qty')
            if target is None:raise TraderError('E_FACT_BARRIER','Native reduction lacks its common remaining target')
            review=preview_owned_routes(kernel,cycle['position_cycle_id'],max(0,state['owned_qty']-target),
                _exclude_child=request['child_order_id'], _now=protection_now)
            route=next((r for r in review['routes'] if r['native_offset']==request['native_offset']),None)
            saved=order.get('close_route')
            if (route is None or request['original_qty']>route['quantity'] or order.get('native_trading_day')!=review['current_day']
                    or saved is None or saved.get('position_cycle_id')!=cycle['position_cycle_id']
                    or saved.get('trading_day')!=review['current_day'] or saved.get('native_offset')!=request['native_offset']
                    or saved.get('quantity')!=request['original_qty']):
                raise TraderError('E_OWNERSHIP','Native close no longer matches its committed cycle and unreserved bucket')
        q=self.quotes[request['contract_id']];price=Decimal(request['limit_price']);tick=Decimal(str(q.price_tick))
        if price%tick or not Decimal(str(q.lower_limit))<=price<=Decimal(str(q.upper_limit)):
            raise TraderError('E_PRICE_ALIGNMENT','DSV native order price is not a legal exchange tick')

    def send(self,request):
        self.validate_send(request);child=request['child_order_id']
        if child in self._issued:raise TraderError('E_DUPLICATE_CONFLICT','A DSV native send cannot be replayed')
        self._issued.add(child)
        self._bindings[child]={key:request[key] for key in ('contract_id','direction','effect','original_qty','limit_price','native_offset')}
        return self.api.insert_order(symbol=request['contract_id'],direction=request['direction'],
            offset=request['native_offset'],volume=request['original_qty'],
            limit_price=float(Decimal(request['limit_price'])),order_id=self.order_id(child),account=self.account)

    def tick_records(self,code):
        serial=self.tick_serials[code]
        backfill=self._tick_backfill_serials.get(code)
        if (backfill is not None and self.api.is_serial_ready(backfill)
                and backfill.iloc[-1]['id']>=serial.iloc[-1]['id']):
            serial=self.tick_serials[code]=self._tick_backfill_serials.pop(code)
        if not self.api.is_serial_ready(serial):return []
        self.subscription_ready_at.setdefault(code,self.clock.now())
        return serial.to_dict('records')

    def latest_tick_record(self, code):
        serial = self.tick_serials[code]
        if self.api.is_serial_ready(serial):
            return serial.tail(1).to_dict('records')[0]

    def request_tick_backfill(self,code):
        """Refresh a gapped native serial through a new, bounded SDK request."""
        if not self.broker_connected or code in self._tick_backfill_pending:
            return
        now=time.monotonic()
        attempts=self._tick_backfill_attempts.get(code,0)
        if attempts>=4 or now<self._tick_backfill_after.get(code,0):
            return
        self._tick_backfill_attempts[code]=attempts+1
        self._tick_backfill_after[code]=now+10
        self._tick_backfill_pending.add(code)
        task=self._subscribe_tick_backfill(code,10000-attempts-1)
        try:self.api.create_task(task)
        except Exception:
            task.close()
            self._tick_backfill_pending.discard(code)

    async def _subscribe_tick_backfill(self,code,width):
        try:
            # The pinned SDK caches tick subscriptions by contract and width.
            # A distinct width requests a fresh native window on the same feed.
            self._tick_backfill_serials[code]=self.api.get_tick_serial(code,data_length=width)
        except Exception:
            pass
        finally:
            self._tick_backfill_pending.discard(code)

    def cancel_external(self,order,day):
        if (self.read_only or not self.grant.manual_workbench or self.fault or not self.grant.in_window(self.clock.now())
                or order['contract_id'] not in self.quotes or order['order_id'] in {self.order_id(child) for child in self._bindings}):
            raise TraderError('E_OWNERSHIP','External cancellation is outside the reviewed account scope')
        snapshot=self.snapshot()
        if not snapshot['complete'] or snapshot['trading_day']!=day or order not in snapshot['orders'] or order['status']!='ALIVE':
            raise TraderError('E_FACT_BARRIER','The original active order changed before cancellation')
        return self.api.cancel_order(order['order_id'],account=self.account)
