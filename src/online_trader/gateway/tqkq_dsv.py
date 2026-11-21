"""Finite DSV TqKq adapter; inherits native binding/callbacks, never a matcher."""
import asyncio
import copy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import math
import time

from online_trader.configuration.rollover import CHINA_TIME, expiry_date, future_product, rollover_hint
from online_trader.domain.values import order_price
from online_trader.errors import DefinitelyNotSentError, TraderError
from online_trader.execution.policy import committed_limit_is_safe, select_price
from online_trader.gateway.tq_wait import is_pinned_tq_api
from online_trader.gateway.tqkq import TqKqPort, number
from online_trader.online_scope import DsvSimulationGrant
from online_trader.scheduling.clock import NS_PER_SECOND, quote_age_quality, quote_timestamp, source_age_ns, utc_text


def _missing_book_price(value):
    if value is None:
        return True
    try:
        price = Decimal(str(value))
    except (ValueError, TypeError):
        return False
    return price.is_nan() or price == 0


class TqKqDsvPort(TqKqPort):
    def entry_clock_ready(self):
        try:
            self.clock.now()
        except TraderError as error:
            if error.code != 'E_CLOCK':
                raise
            return False
        return super().entry_clock_ready()

    def strategy_wall_time(self):
        return self.clock.now()

    def _matches_strategy_quote(self, quote, event):
        stamp = quote_timestamp(quote.datetime, CHINA_TIME)
        return stamp == datetime.fromisoformat(event['source_time_utc']) and number(quote.last_price) == event['last']

    def _quote_age_acceptable(self, code, now, source, age, check_age):
        if hasattr(self.clock, 'health') and hasattr(self.clock, 'capture'):
            sample = self.clock.capture_live() if hasattr(self.clock, 'capture_live') else self.clock.capture()
            health = self.clock.health(sample)
            quality, reason = quote_age_quality(age, health)
            if quality == 'SOURCE_ANOMALY':
                reference = health.get('reference') or {}
                return self._quote_rejected(code, 'NATIVE_AGE', reason, source_age_ns=str(age),
                                            ntp_offset_ns=reference.get('offset_ns'),
                                            ntp_uncertainty_ns=reference.get('estimated_error_ns'))
        if self.calendar is None or self.grant.manual_workbench:
            return super()._quote_age_acceptable(code, now, source, age, check_age)
        current = self.calendar.query(code, utc_text(now))
        quoted = self.calendar.query(code, utc_text(source))
        if not current['is_trading'] or current['session'] != quoted['session']:
            return self._quote_rejected(code, 'NATIVE_AGE', 'QUOTE_SESSION_MISMATCH',
                                        active=current['is_trading'], calendar_complete=current.get('complete'),
                                        current_session=(current['session'] or {}).get('session_id'),
                                        quote_session=(quoted['session'] or {}).get('session_id'),
                                        reference_utc=utc_text(now), source_age_ns=str(age))
        if not self.grant.permits_day(current['trading_day']):
            return self._quote_rejected(code, 'NATIVE_AGE', 'QUOTE_DAY_OUTSIDE_SCOPE',
                                        trading_day=current['trading_day'])
        if check_age and age > 5 * NS_PER_SECOND:
            return self._quote_rejected(code, 'NATIVE_AGE', 'QUOTE_AGE_LIMIT',
                                        reference_utc=utc_text(now), source_age_ns=str(age),
                                        maximum_age_ns=str(5 * NS_PER_SECOND))
        return True

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
            age_ns = source_age_ns(now, source, q.datetime)
            limit_ns = self.kernel.max_quote_age_ms * 1_000_000
            if age_ns > limit_ns:
                return self._quote_rejected(code, 'PROTECTION', 'TICK_AGE_LIMIT',
                                            native_tick_id=event.get('native_tick_id'),
                                            accepted_time_utc=event['source_time_utc'],
                                            source_age_ns=str(age_ns), maximum_age_ns=str(limit_ns))
            if not self.fresh(code, now=now):
                return False
            if not self._matches_strategy_quote(q, event):
                return self._quote_rejected(code, 'PROTECTION', 'QUOTE_TICK_IDENTITY_MISMATCH',
                                            native_tick_id=event.get('native_tick_id'),
                                            accepted_time_utc=event['source_time_utc'], accepted_last=event['last'],
                                            raw_last=number(q.last_price))
            if (number(q.bid_price1), number(q.ask_price1), number(q.price_tick)) != (
                    event['bid1'], event['ask1'], event['tick_size']):
                return self._quote_rejected(code, 'PROTECTION', 'QUOTE_TICK_BOOK_MISMATCH',
                                            native_tick_id=event.get('native_tick_id'),
                                            accepted_bid=event['bid1'], accepted_ask=event['ask1'],
                                            accepted_tick_size=event['tick_size'])
            if not all(math.isfinite(size) for size in (q.bid_volume1, q.ask_volume1)):
                return self._quote_rejected(code, 'PROTECTION', 'INVALID_BOOK_SIZE')
            if not q.lower_limit <= q.last_price <= q.upper_limit:
                return self._quote_rejected(code, 'PROTECTION', 'LAST_OUT_OF_LIMIT')
            for price in ('last', 'bid1', 'ask1'):
                order_price(event[price], event['tick_size'])
            return True
        except (TraderError, KeyError, AttributeError, TypeError, ValueError) as error:
            return self._quote_rejected(code, 'PROTECTION', 'UNUSABLE_PROTECTION_QUOTE',
                                        native_tick_id=event.get('native_tick_id'),
                                        error_code=getattr(error, 'code', type(error).__name__))

    def fresh_strategy_entry(self, code, side, event, *, profile=None, signal=None):
        """Use the strategy age rule and verify the exact legal native quote."""
        if event is None:
            return self._quote_rejected(code, 'OPEN', 'NO_ACCEPTED_TICK', side=side)
        runtime = getattr(getattr(self, 'kernel', None), '_dsv_runtime', None)
        try:
            quote = self.quotes[code]
            now = self.clock.utc_now() if hasattr(self.clock, 'utc_now') else self.clock.now()
            if not self._matches_strategy_quote(quote, event):
                return self._quote_rejected(code, 'OPEN', 'QUOTE_TICK_IDENTITY_MISMATCH', side=side,
                                            native_tick_id=event.get('native_tick_id'),
                                            accepted_time_utc=event['source_time_utc'], accepted_last=event['last'],
                                            raw_last=number(quote.last_price))
            if (runtime is not None and (signal or {}).get('entry_kind', 'NORMAL') == 'NORMAL'
                    and runtime.ordinary_quote_stale(event, now=now, profile=profile)):
                if getattr(self, '_quote_trace', None) is None:
                    return False
                limit = (runtime._quote_age_limit if profile is None else
                         profile.to_record()['settings']['trade']['max_quote_age_seconds'])
                return self._quote_rejected(code, 'OPEN', 'STRATEGY_QUOTE_AGE_LIMIT', side=side,
                                            native_tick_id=event.get('native_tick_id'),
                                            source_age_seconds=str(runtime.ordinary_quote_age(event, now=now)),
                                            maximum_age_seconds=str(limit))
            field = 'ask1' if side == 'BUY' else 'bid1'
            opponent, other, volume = ((getattr(quote,'ask_price1',None), getattr(quote,'bid_price1',None),
                                        getattr(quote,'bid_volume1',None)) if side == 'BUY' else
                                       (getattr(quote,'bid_price1',None), getattr(quote,'ask_price1',None),
                                        getattr(quote,'ask_volume1',None)))
            if not _missing_book_price(event[field]):
                if number(quote.ask_price1) != event['ask1'] or number(quote.bid_price1) != event['bid1']:
                    return self._quote_rejected(code, 'OPEN', 'QUOTE_TICK_BOOK_MISMATCH', side=side,
                                                native_tick_id=event.get('native_tick_id'),
                                                accepted_bid=event['bid1'], accepted_ask=event['ask1'])
                return self.fresh(code, check_age=runtime is None)
            if not self._fresh_last(code, check_age=runtime is None):
                return False
            if not _missing_book_price(opponent):
                return self._quote_rejected(code, 'OPEN', 'OPPONENT_PRICE_CHANGED', side=side,
                                            opponent_price=number(opponent))
            if not _missing_book_price(other) and not (type(other) in (int, float) and math.isfinite(other)
                    and quote.lower_limit <= other <= quote.upper_limit and type(volume) in (int, float)
                    and math.isfinite(volume) and volume > 0):
                return self._quote_rejected(code, 'OPEN', 'INVALID_REMAINING_BOOK_SIDE', side=side,
                                            other_price=number(other), other_size=number(volume))
            order_price(event['last'], number(quote.price_tick))
            return True
        except (TraderError, KeyError, AttributeError, TypeError, ValueError) as error:
            return self._quote_rejected(code, 'OPEN', 'UNUSABLE_ENTRY_QUOTE', side=side,
                                        native_tick_id=event.get('native_tick_id'),
                                        error_code=getattr(error, 'code', type(error).__name__))

    def current_strategy_entry(self, code, side, *, profile=None, signal=None):
        """Validate the latest native book without requiring an older tick identity.

        The strategy signal is immutable, but the native book is expected to
        advance while the owner commits an intent.  Ordinary breakout signals
        additionally retain their trigger-side predicate; a quote that crossed
        back through the trigger is not made executable merely because it is
        newer or cheaper.
        """
        try:
            quote = self.quotes[code]
            stamp = quote_timestamp(quote.datetime, CHINA_TIME)
            event = {
                'contract_id': code,
                'source_time_utc': stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                'last': number(quote.last_price),
                'bid1': number(quote.bid_price1),
                'ask1': number(quote.ask_price1),
                'tick_size': number(quote.price_tick),
                'native_tick_id': None,
            }
            if not self.fresh_strategy_entry(code, side, event, profile=profile, signal=signal):
                return False
            signal = signal or {}
            if signal.get('entry_kind') == 'NORMAL':
                trigger = Decimal(signal['trigger_price'])
                last = Decimal(event['last'])
                valid = last >= trigger if signal['direction'] == 'LONG' else last <= trigger
                if not valid:
                    return self._quote_rejected(
                        code, 'OPEN', 'ENTRY_SIGNAL_CROSSED_BACK', side=side,
                        trigger_price=str(trigger), current_last=event['last'],
                        signal_time_utc=signal.get('signal_time_utc'))
            return True
        except (TraderError, KeyError, AttributeError, TypeError, ValueError) as error:
            return self._quote_rejected(code, 'OPEN', 'UNUSABLE_CURRENT_ENTRY_QUOTE', side=side,
                                        error_code=getattr(error, 'code', type(error).__name__))

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
        self._local_order_returns = set()
        self.waiting_reduction_quotes = set()
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
            self._local_order_returns.clear()
            self.waiting_reduction_quotes.clear()
            self.minute_chart_sample_at = None
            self.minute_chart_cache = {}
            if self._rollover_task is not None:
                self._rollover_task.cancel()
                self._rollover_task = None
            self._rollover_poll_at = None
            self._rollover_main.clear()
            self._rollover_catalog.clear()
        trace = getattr(self, '_quote_trace', None)
        if trace is not None and (trace.live_poll or self.last_poll_updated):
            pinned = is_pinned_tq_api(self.api)
            for code, serial in self.tick_serials.items():
                reason = None
                supported = pinned and type(serial).__module__ == 'tqsdk.objs_not_entity'
                try:
                    ready = supported and self.api.is_serial_ready(serial)
                except (AttributeError, KeyError, TypeError, ValueError, TraderError):
                    ready = False
                if self.source_epoch != epoch:
                    reason = 'SDK_SOURCE_EPOCH_CHANGED'
                elif self.fault:
                    reason = 'SDK_CONNECTION_OR_SYNC_UNPROVED'
                elif not supported:
                    reason = 'SDK_SERIAL_CONTRACT_UNVERIFIED'
                elif not ready:
                    reason = 'SDK_SERIAL_NOT_READY'
                trace.sdk_ticks(code, self.source_epoch, serial if ready else None, received[0],
                                updated=self.last_poll_updated, unavailable=reason)
        return received

    def observations(self):
        from online_trader.gateway.native_days import native_day
        observations=super().observations()
        orders=self.api.get_order(account=self.account)
        owner = getattr(getattr(self, 'kernel', None), 'simulation', None)
        proved_observations = []
        for observation in observations:
            item=orders[observation['order_id']]
            stamp=getattr(item,'insert_date_time',None)
            child, code = observation['child_order_id'], observation['contract_id']
            if getattr(item, 'is_error', None) is True:
                observation['rejected'] = True
            if (child in self._local_order_returns and not self.fault
                    and observation['status'] == 'ALIVE' and observation['reported_fill_qty'] == 0
                    and (stamp is None or type(stamp) is int and stamp == 0)
                    and getattr(item, '_this_session', False) is True
                    and not getattr(item, 'exchange_order_id', None)
                    and not observation.get('rejected')):
                # This is the SDK's local insert result. Keep the durable
                # reservation and wait for the original broker order facts.
                continue
            self._local_order_returns.discard(child)
            proved_observations.append(observation)
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
        return proved_observations

    def automatic_reduction_quote_ready(self, code):
        """Reuse the live native-book requirement before preparing an automatic reduction."""
        ready = self.fresh(code)
        if ready:
            self.waiting_reduction_quotes.discard(code)
        else:
            self.waiting_reduction_quotes.add(code)
        return ready

    def validate_send(self,request):
        self._last_quote_rejection = None
        self.check_dispatch_clock(request['effect'])
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
        elif request['effect']=='OPEN' and cycle['entry_origin']=='STRATEGY':
            intent = cycle.get('entry_intent') or {}
            signal = intent.get('signal') or {
                'entry_kind': candidate.get('entry_kind'), 'direction': candidate.get('direction'),
                'trigger_price': candidate.get('theoretical_price'),
                'signal_time_utc': candidate.get('source_time_utc')}
            fresh = self.current_strategy_entry(code, request['direction'],
                profile=runtime.profile_for_plan(cycle['plan']) if runtime else None, signal=signal)
            if fresh and signal.get('entry_kind') != 'NORMAL':
                current = quote_timestamp(self.quotes[code].datetime, CHINA_TIME)
                accepted = None if facts is None else datetime.fromisoformat(facts['event']['source_time_utc'])
                if accepted is None or current != accepted:
                    fresh = self._quote_rejected(
                        code, 'SEND', 'QUOTE_REVISION_CHANGED', effect=request['effect'],
                        child_order_id=request['child_order_id'],
                        accepted_time_utc=None if accepted is None else accepted.isoformat(),
                        current_time_utc=current.isoformat())
        else:
            fresh = self.fresh(code) or (direct and self.fresh_manual_reduction(code))
        if not fresh:
            evidence = self._last_quote_rejection
            predicate = (evidence or {}).get('reason')
            error_code = ({'ENTRY_SIGNAL_CROSSED_BACK': 'E_ENTRY_SIGNAL_INVALIDATED',
                           'QUOTE_REVISION_CHANGED': 'E_QUOTE_REVISION_CHANGED'}.get(
                               predicate, 'E_NATIVE_QUOTE_NOT_CURRENT'))
            if evidence is None:
                self._quote_rejected(code, 'SEND', error_code, effect=request['effect'],
                                     child_order_id=request['child_order_id'], trading_day=request['trading_day'])
                evidence = self._last_quote_rejection
            error = TraderError(error_code, '报单前原生行情未满足当前信号与执行合同。')
            error.quote_evidence = evidence or self._last_quote_rejection
            raise error
        expected='BUY' if (cycle['plan']['direction']=='LONG')==(order['effect']=='OPEN') else 'SELL'
        if request['direction']!=expected:raise TraderError('E_OWNERSHIP','Native order direction differs from the original cycle')
        position=self.position(request['contract_id'])
        owned={side:sum(c['protection']['owned_qty'] for c in kernel._staged.values()
                       if c['plan']['contract_id']==request['contract_id'] and c['plan']['direction']==side.upper()) for side in ('long','short')}
        if position!=owned:raise TraderError('E_POSITION_MISMATCH','External position changes block this native send')
        owned_ids={self.order_id(o['child_order_id']) for c in kernel._staged.values() for o in c['orders']}
        if any(o['order_id'] not in owned_ids for o in snapshot['orders']
               if o['contract_id']==request['contract_id'] and o['status']=='ALIVE'):
            raise TraderError('E_FOREIGN_ACTIVE_ORDER','An active foreign order blocks native execution on the selected contract')
        if request['effect']=='OPEN':
            if (cycle['entry_origin']=='MANUAL')!=self.grant.manual_workbench:
                raise TraderError('E_GATEWAY_SCOPE','Native opening origin differs from the finite scope')
            if not self.entry_clock_ready():
                raise TraderError('E_CLOCK','A valid Windows wall clock is required immediately before native opening send')
            if cycle.get('manual_limit_order'):
                kernel.simulation.manual_entry_facts(cycle['strategy_metadata']['candidate'],request['original_qty'],
                    exclude_decision=cycle['decision_key'])
            send_now = self.clock.utc_now() if hasattr(self.clock, 'utc_now') else self.clock.now()
            if not kernel.simulation.entry_window(send_now):
                raise TraderError('E_ENTRY_WINDOW_EXPIRED', '当前时刻超出已确认的开仓窗口。')
            window = self.calendar.query(code, utc_text(send_now))
            if not window['is_trading'] or window['session']['session_id'] != facts['event']['session_id']:
                self._quote_rejected(code, 'SEND', 'QUOTE_SESSION_CHANGED_BEFORE_SEND', effect=request['effect'],
                                     child_order_id=request['child_order_id'], reference_utc=utc_text(send_now),
                                     accepted_session=facts['event']['session_id'],
                                     current_session=(window['session'] or {}).get('session_id'))
                error = TraderError('E_NATIVE_QUOTE_NOT_CURRENT', '报单前已离开原报价盘段。')
                error.quote_evidence = self._last_quote_rejection
                raise error
            q = self.quotes[code]
            market = {
                'complete': True, 'fresh': True, 'is_trading': True, 'quality': 'CONTIGUOUS',
                'last': number(q.last_price), 'bid1': number(q.bid_price1), 'ask1': number(q.ask_price1),
                'tick_size': number(q.price_tick), 'lower_limit': number(q.lower_limit),
                'upper_limit': number(q.upper_limit),
            }
            if cycle.get('manual_limit_order'):
                priced = {'allowed': True, 'price': request['limit_price'], 'mode': 'MANUAL', 'reasons': []}
            elif kernel.execution.active(cycle):
                priced = kernel.execution.price(cycle, 'OPEN', market_override=market)
            else:
                plan_policy = cycle['plan']['execution_policy']
                signal = (cycle.get('entry_intent') or {}).get('signal') or {}
                side = request['direction']
                if candidate.get('entry_near_open'):
                    contexts = {}
                elif plan_policy.get('passive_entry_first'):
                    contexts = {'PASSIVE': {'side': side, 'age_ms': 0,
                                            'level': cycle['plan']['theoretical_entry_price']}}
                else:
                    contexts = {'CAP': {'side': side, 'age_ms': 0,
                                        'cap': signal.get('price_cap', request['limit_price'])}}
                priced = select_price(side, market, contexts=contexts)
            if not priced['allowed']:
                self._quote_rejected(code, 'SEND', 'CURRENT_EXECUTION_PRICE_UNAVAILABLE',
                                     effect='OPEN', child_order_id=request['child_order_id'],
                                     reason_codes=priced['reasons'])
                error = TraderError('E_NATIVE_QUOTE_NOT_CURRENT', '当前原生盘口不能形成合法执行价。')
                error.quote_evidence = self._last_quote_rejection
                raise error
            if not committed_limit_is_safe(request['direction'], request['limit_price'], priced['price']):
                self._quote_rejected(code, 'SEND', 'QUOTE_REVISION_CHANGED', effect='OPEN',
                                     child_order_id=request['child_order_id'],
                                     committed_limit_price=request['limit_price'],
                                     current_policy_price=priced['price'], price_mode=priced['mode'])
                error = TraderError('E_QUOTE_REVISION_CHANGED', '当前执行政策需要新的持久化限价。')
                error.quote_evidence = self._last_quote_rejection
                raise error
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
                raise TraderError('E_EXIT_ROUTE_CHANGED','Native close no longer matches its committed cycle and unreserved bucket')
        q=self.quotes[request['contract_id']];price=Decimal(request['limit_price']);tick=Decimal(str(q.price_tick))
        if price%tick or not Decimal(str(q.lower_limit))<=price<=Decimal(str(q.upper_limit)):
            raise TraderError('E_PRICE_ALIGNMENT','DSV native order price is not a legal exchange tick')

    def send(self,request):
        try:
            self.validate_send(request)
        except TraderError as error:
            evidence = {'predicate_code': error.code,
                        'quote_evidence': getattr(error, 'quote_evidence', None) or self._last_quote_rejection}
            refused = DefinitelyNotSentError(error.code, str(error), evidence=evidence)
            refused.quote_evidence = evidence['quote_evidence']
            raise refused from error
        child=request['child_order_id']
        if child in self._issued:raise TraderError('E_DUPLICATE_CONFLICT','A DSV native send cannot be replayed')
        self._issued.add(child)
        self._bindings[child]={key:request[key] for key in ('contract_id','direction','effect','original_qty','limit_price','native_offset')}
        price = float(Decimal(request['limit_price']))
        native_id = self.order_id(child)
        trace = getattr(self, '_quote_trace', None)
        if trace is not None:
            trace.record('SDK_INSERT_ORDER', contract_id=request['contract_id'], child_order_id=child,
                         effect=request['effect'], trading_day=request['trading_day'])
        order = self.api.insert_order(symbol=request['contract_id'],direction=request['direction'],
            offset=request['native_offset'],volume=request['original_qty'],
            limit_price=price,order_id=native_id,account=self.account)
        self._local_order_returns.add(child)
        if request['effect'] == 'REDUCE':
            self.waiting_reduction_quotes.discard(request['contract_id'])
        return order

    def tick_records(self,code,*,after_id=None):
        serial=self.tick_serials[code]
        backfill=self._tick_backfill_serials.get(code)
        if (backfill is not None and self.api.is_serial_ready(backfill)
                and backfill.iloc[-1]['id']>=serial.iloc[-1]['id']):
            serial=self.tick_serials[code]=self._tick_backfill_serials.pop(code)
            after_id = None
        if not self.api.is_serial_ready(serial):
            trace = getattr(self, '_quote_trace', None)
            if trace is not None:
                trace.record('SDK_SERIAL_NOT_READY', contract_id=code, state=(self.source_epoch, after_id),
                             source_epoch=self.source_epoch, after_tick_id=after_id)
            return []
        self.subscription_ready_at.setdefault(code,self.clock.now())
        if after_id is not None:
            if is_pinned_tq_api(self.api) and type(serial).__module__ == 'tqsdk.objs_not_entity':
                # The pinned SDK puts each native ID into its corresponding
                # ascending slot, even when the slot's Tick is still missing.
                # Keep the boundary row so DsvFactFeed can check corrections.
                serial = serial.iloc[serial['id'].searchsorted(after_id):]
            else:
                serial = serial.loc[serial['id'] >= after_id]
        return serial.to_dict('records')

    def incremental_tick_records(self, code, after_id):
        """Include the accepted boundary row to detect corrections and gaps."""
        return self.tick_records(code, after_id=after_id)

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
