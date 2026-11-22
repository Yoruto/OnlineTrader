"""Bridge the explicit DSV owner to an already constructed TqSdk-shaped API."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from hashlib import sha256
from threading import get_ident
import time
from uuid import uuid4

from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.rollover import expiry_date
from online_trader.domain.values import decimal_text, decimal_value, identifier, quantity
from online_trader.dsv_runtime.controller import CancelNotAttempted, SendNotAttempted, legal_price, terminal
from online_trader.errors import TraderError, exception_record
from online_trader.gateway.dsv_facts import native_integer, utc
from online_trader.gateway.native_days import native_day
from online_trader.gateway.tqkq import number
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.scheduling.clock import (
    ClockContinuity, ClockSample, NS_PER_SECOND, SystemClock, TIME_SEMANTICS, quote_age_quality, quote_timestamp, sdk_deadline,
    delta_ns, source_age_ns, source_fraction_tail, utc_text,
)
from online_trader.scheduling.timers import poll_slice
from online_trader.strategy.entry import is_night_session


_BEIJING = timezone(timedelta(hours=8))
_PRICE_FIELDS = {'last': 'last_price', 'day_high': 'highest', 'day_low': 'lowest', 'bid1': 'bid_price1',
                 'ask1': 'ask_price1', 'tick_size': 'price_tick', 'lower_limit': 'lower_limit', 'upper_limit': 'upper_limit'}
_POSITION = {'long_qty': 'pos_long', 'short_qty': 'pos_short',
             'long_today': 'pos_long_today', 'long_historical': 'pos_long_his',
             'short_today': 'pos_short_today', 'short_historical': 'pos_short_his'}


def _amount(value, *, rounding=None):
    text = number(value)
    if text is None:
        return None
    try:
        return decimal_text(decimal_value(text))
    except TraderError:
        amount = Decimal(text)
        if rounding is None or abs(amount) >= 10**18:
            return None
        with localcontext() as ctx:
            ctx.prec = 64
            return decimal_text(amount.quantize(Decimal('0.000000000001'), rounding=rounding))


class DsvSdkAdapter:
    """Read SDK final objects once per wait_update and send only committed child effects.

    Account authentication and synchronization proof belong to the caller.
    The replacement session currently only binds an OFFLINE account.
    """

    def __init__(self, api, account, *, account_key, profile, calendar, sdk_version, clock=None, _connection=None,
                 _legacy_native_orders=None, continuity=None, reference=None, owner_mode='REPLACEMENT'):
        if sdk_version != SDK_VERSION or not isinstance(profile, DsvProfile) or not isinstance(calendar, TradingCalendar):
            raise TraderError('E_DSV_SDK_SCOPE', 'Expected the pinned SDK, normalized profile and explicit calendar')
        identifier(account_key)
        grant = None if _connection is None else getattr(_connection, 'grant', None)
        standalone = owner_mode == 'STANDALONE'
        standalone_schema = None if grant is None else grant.value.get('schema_version')
        standalone_scope = standalone_schema in {'KQ_DSV_DESKTOP/1', 'SIMNOW_DSV_DESKTOP/1'} or (
            standalone_schema == 'SIMNOW_DSV_SIM/1'
            and str(grant.value.get('authorization_reference', '')).startswith('AUTO:'))
        if owner_mode not in {'REPLACEMENT', 'STANDALONE'}:
            raise TraderError('E_DSV_SDK_SCOPE', 'Expected an explicit DSV owner mode')
        if account is None or (_connection is None and not account_key.startswith('DEV:')) or (
                _connection is not None and (type(grant) is not DsvSimulationGrant
                or (grant.replacement == standalone)
                or standalone and not standalone_scope
                or grant.account_key != account_key or _connection.api is not api or _connection.account is not account
                or not _connection.broker_connected or _connection.read_only)):
            raise TraderError('E_DSV_SDK_SCOPE', 'Expected an explicitly bound SDK account and connection')
        self.api, self.account, self.account_key = api, account, account_key
        self.grant, self.connection = grant, _connection
        self.owner_mode = owner_mode
        self.provider = 'OFFLINE' if grant is None else grant.provider
        self.profile, self.calendar = profile, calendar
        self.contracts = tuple(row['code'] for row in profile.to_record()['settings']['symbols'])
        calendar_record = calendar.to_record()
        self.periods = {row['contract_id']: row['sessions'] for row in calendar_record['contracts']}
        if not calendar_record['complete'] or any(not self.periods.get(code) for code in self.contracts):
            raise TraderError('E_DSV_SDK_SCOPE', 'The calendar must cover every selected contract')
        day_sets = [{period['trading_day'] for period in self.periods[code]} for code in self.contracts]
        self.trading_days = sorted(set.intersection(*day_sets)) if day_sets else []
        self.clock = clock if clock is not None else SystemClock()
        self._clock_epoch = uuid4().hex
        self._continuity = continuity if continuity is not None else ClockContinuity()
        self._reference = reference
        self._health = None
        self._sdk_activity = 0
        self._last_pump_ns = None
        self.metrics = {}
        self.owner = get_ident()
        self.session = None
        self.epoch = uuid4().hex
        self.enabled = False
        self.synchronized = False
        self.flat_confirmed = False
        self.dispatch_market = {}
        self.quote_observations = {}
        self._clock_resume_day = None
        self._seen_effects = set()
        self._local_returns = set()
        self._foreign_trades = None
        self._legacy_native_orders = _legacy_native_orders or {}
        self._native_fact_cache = {}
        self.quotes = {code: api.get_quote(code) for code in self.contracts}
        self.minutes = {code: api.get_kline_serial(code, 60, data_length=500) for code in self.contracts}

    def matches_online_scope(self, account, provider, *, owner_mode='REPLACEMENT'):
        return (self.grant is not None and self.owner_mode == owner_mode
                and (account, provider) == (self.account_key, self.provider))

    def check_start(self, state):
        sample = self._clock_sample()
        if not self._entry_clock_ready():
            raise TraderError('E_DSV_SDK_CLOCK', 'Application UTC calibration is unavailable for opening')
        if (not self.synchronized or not state['synchronized'] or state.get('scope_expired_at')
                or not self.grant.in_window(sample.utc)):
            raise TraderError('E_DSV_SDK_SYNC', 'Start needs a current synchronized account within the reviewed window')
        if not self.grant.permits_day(state['trading_day']):
            raise TraderError('E_DSV_SDK_SCOPE', 'Start needs the authorized cabinet day')
        if state.get('clock_recovery_required') or self._continuity.compare(sample)['reason']:
            raise TraderError('E_DSV_SDK_CLOCK', 'Review and recover the clock before enabling entries')

    def check_clock_recovery(self, state):
        sample = self._clock_sample()
        windows = [self.calendar.query(code, sample.now_utc) for code in self.contracts]
        if (not self.synchronized or not self.grant.permits_day(state['trading_day'])
                or self._continuity.compare(sample)['reason']
                or any(not window['complete'] or window['is_trading'] and window['trading_day'] != state['trading_day']
                       for window in windows)):
            raise TraderError('E_DSV_SDK_CLOCK', 'Clock recovery needs a consistent bound account, day and calendar')

    def _clock_sample(self):
        if hasattr(self.clock, 'capture'):
            return self.clock.capture()
        # Keep the existing offline harness callable (aware UTC, milliseconds).
        now, mono = self.clock()
        if type(mono) is not int:
            raise TraderError('E_DSV_SDK_CLOCK', 'Expected integer monotonic milliseconds')
        return ClockSample(utc_text(now), str(mono * 1_000_000), getattr(self, '_clock_epoch', None))

    def opening_window(self, day, now):
        return self._entry_clock_ready() and self.in_grant_window(day, now)

    def in_grant_window(self, day, now):
        return self.grant.permits_day(day) and self.grant.in_window(now)

    def _entry_clock_ready(self):
        ready = getattr(getattr(self, 'clock', None), 'entry_ready', None)
        return ready is None or ready()

    def _protection_clock_ready(self):
        ready = getattr(getattr(self, 'clock', None), 'protection_ready', None)
        return ready is None or ready()

    @property
    def clock_resume_pending(self):
        return self._clock_resume_day is not None

    def expired(self, now):
        return self._protection_clock_ready() and now > datetime.fromisoformat(self.grant.value['not_after_utc'])

    @staticmethod
    def order_id(child):
        return 'otdsv' + sha256(identifier(child).encode('ascii')).hexdigest()[:27]

    def _bind(self, session):
        if get_ident() != self.owner or session._controller.port is not self:
            raise TraderError('E_DSV_SDK_OWNER', 'SDK calls must use the bound DSV owner thread and port')
        state = session.snapshot()
        scope = state['scope']
        if (scope['account_key'] != self.account_key or scope['provider'] != self.provider or
                scope['configuration_id'] != self.profile.content_hash or self.session not in (None, session)):
            raise TraderError('E_DSV_SDK_SCOPE', 'The session does not match the adapter scope')
        if self.session is None:
            session.disconnect('SDK_ATTACHED')
            self.session = session
            return session.snapshot()
        return state

    def disconnect(self, session):
        self._bind(session)
        self.enabled = False
        self.synchronized = False
        self.flat_confirmed = False
        self.dispatch_market = {}
        self.quote_observations = {}
        self._local_returns.clear()
        self.epoch = uuid4().hex
        session.disconnect('SDK_DISCONNECTED')

    def command(self, session, command):
        """Run a user command through the same owner and committed effect path."""
        state = self._bind(session)
        if command.get('action') in {'START', 'STOP', 'CLOCK_RECOVER'}:
            self._clock_resume_day = None
        self.enabled = self.synchronized and state['synchronized']
        try:
            return session.command(command)
        finally:
            self.enabled = False

    def step(self, session, *, trading_day, complete):
        state = self._bind(session)
        self.enabled = False
        try:
            poll_clock = getattr(self.clock, 'poll', None)
            if poll_clock is not None:
                poll_clock()
                if state['running'] and not self._entry_clock_ready():
                    session.disable_entries_for_clock()
                    self._clock_resume_day = (state['trading_day'] if self.grant is not None and state['synchronized']
                                              and not state.get('clock_recovery_required')
                                              and not state.get('scope_expired_at')
                                              and self.grant.in_window(self._clock_sample().utc) else None)
                    state = session.snapshot()
            timers = session.active_timers()
            wait = poll_slice(timers, self._clock_sample()) if timers else 0.5
            started = time.monotonic_ns()
            if self.connection is None:
                updated = self.api.wait_update(deadline=sdk_deadline(wait))
            else:
                epoch = self.connection.source_epoch
                self.connection.poll_updates(wait)
                updated = getattr(self.connection, 'last_poll_updated', None)
                if self.connection.source_epoch != epoch:
                    self._clock_resume_day = None
                    self.disconnect(session)
                    state = session.snapshot()
            after_pump = time.monotonic_ns()
            self.metrics = {'sdk_pump_ns': str(after_pump - started), 'sdk_pump_gap_ns': (
                None if self._last_pump_ns is None else str(started - self._last_pump_ns))}
            self._last_pump_ns = started
            if updated is True:
                self._sdk_activity += 1
            sample = self._clock_sample()
            prior_sample = self._continuity.previous
            self._health = self._continuity.observe(sample)
            if self._health['reason']:
                self._clock_resume_day = None
            if self._health['reason'] and prior_sample is not None:
                self._health['previous_sample'] = prior_sample.paired_record()
            clock_health = getattr(self.clock, 'health', None)
            if clock_health is not None:
                self._health.update(clock_health(sample))
            elif self._reference is not None:
                self._health.update(self._reference.poll(sample, invalidate=bool(self._health['reason'])))
            else:
                self._health['reference_status'] = 'UNOBSERVED'
            synchronized = complete() if callable(complete) else complete
            if type(synchronized) is not bool:
                raise TraderError('E_DSV_SDK_SYNC', 'SDK synchronization must be supplied as a complete fact')
            if not synchronized:
                self.disconnect(session)
                return {'type': 'DISCONNECTED', 'synchronized': False}
            day_held = False
            if callable(trading_day):
                current_day = state['trading_day']
                day_held = not self._protection_clock_ready() or bool(self._health.get('reason'))
                trading_day = current_day if day_held else trading_day(current_day, sample.utc)
            facts_started = time.monotonic_ns()
            frame, market = self._frame(state, sample, trading_day, day_held=day_held)
            self.metrics['native_facts_ns'] = str(time.monotonic_ns() - facts_started)
            self.enabled = True
            self.dispatch_market = market
            receipt = session.apply_round(frame)
            for fact in frame['native']:
                key = (fact['kind'], fact['child_order_id'], fact.get('native_trade_id'))
                self._native_fact_cache[key] = fact
            self.metrics.update(session.round_timing)
            state = session.snapshot()
            self.synchronized = state['synchronized']
            self.flat_confirmed = self.synchronized and not self.metrics['native_fill_waiting_day_contracts'] and all(
                row['long_qty'] == row['short_qty'] == 0 and not row['external_orders'] for row in market.values())
            self._try_resume_clock_entries(session, state, market)
            return receipt
        except BaseException:
            self._clock_resume_day = None
            self.synchronized = False
            self.flat_confirmed = False
            self.dispatch_market = {}
            self.quote_observations = {}
            self._local_returns.clear()
            self.epoch = uuid4().hex
            if not session._failed:
                session.disconnect('SDK_ROUND_FAILED')
            raise
        finally:
            self.enabled = False

    def _try_resume_clock_entries(self, session, state, market):
        day = self._clock_resume_day
        if day is None:
            return
        sample = self._clock_sample()
        if (state['trading_day'] != day or state['running'] or state.get('scope_expired_at')
                or state.get('clock_recovery_required') or not self.grant.in_window(sample.utc)):
            self._clock_resume_day = None
            return
        if not self.synchronized or not self.clock.entry_stable() or not session.clock_resume_native_safe(state, market):
            return
        limit = Decimal(self.profile.to_record()['settings']['trade']['max_quote_age_seconds'])
        for code, row in market.items():
            observed = row['quote_observation']
            if (not row['ticks'] or observed['age_quality'] != 'VALID'
                    or not row['native_coverage'] or not row['market_session_active']):
                continue
            window = self.calendar.query(code, sample.now_utc)
            if (not window['is_trading'] or window['trading_day'] != day
                    or window['session']['session_id'] != row['calendar_session_id']):
                continue
            stamp = observed['source_time_raw']
            source = quote_timestamp(stamp, _BEIJING)
            age = source_age_ns(sample.utc, source, stamp)
            health = self.clock.health(sample) if hasattr(self.clock, 'health') else self._health
            elapsed = delta_ns(sample.utc, source.replace(microsecond=0))
            if quote_age_quality(age, health)[0] != 'VALID' or (limit > 0 and elapsed > limit * NS_PER_SECOND):
                continue
            session.resume_entries_for_clock()
            self._clock_resume_day = None
            return

    def _frame(self, state, sample, day, *, day_held=False):
        if day not in self.trading_days or day < state['trading_day']:
            raise TraderError('E_DSV_SDK_SCOPE', 'Cabinet day is outside of the selected calendar')
        windows = {code: self.calendar.query(code, sample.now_utc) for code in self.contracts}
        active = [window for window in windows.values() if window['is_trading']]
        if day != state['trading_day'] and (not active or any(window['trading_day'] != day for window in active)):
            raise TraderError('E_DSV_SDK_SCOPE', 'A cabinet-day change requires matching active calendar sessions')
        bindings = {self.order_id(child): order for child, order in state['orders'].items()}
        orders = self.api.get_order(account=self.account)
        trades = self.api.get_trade(account=self.account)
        self._check_legacy_orders(orders, trades)
        native = self._order_facts(orders, bindings)
        foreign_orders = self._foreign_orders(orders, bindings)
        accepted = {self.order_id(order['child_order_id']): order.get('native_order_time_ns') for order in state['orders'].values()}
        accepted.update({fact['native_order_id']: fact['source_time_ns'] for fact in native if 'source_time_ns' in fact})
        details, foreign_trades, deferred = self._trade_facts(
            trades, bindings, day, accepted, needs_recovery=bool(state['orders']), day_held=day_held)
        self.metrics['native_fill_waiting_day_contracts'] = len(deferred)
        reconcile = (not state['synchronized'] or day != state['trading_day']
                     or state.get('clock_recovery_required') or self._health.get('reason'))
        coverage = [fact['child_order_id'] for fact in native if fact['kind'] == 'ORDER']
        if not reconcile:
            coverage = []
        changes = native + details
        self.metrics['native_facts_observed'] = len(changes)
        if not reconcile:
            changes = [fact for fact in changes if fact['kind'] == 'LOCAL_ORDER' or
                       self._native_fact_cache.get((fact['kind'], fact['child_order_id'],
                                                    fact.get('native_trade_id'))) != fact]
        self.metrics['native_facts_changed'] = len(changes)
        funds = self.api.get_account(account=self.account)
        markets = []
        for code in self.contracts:
            quote_time, observation = self._observe_quote(code, sample, windows[code], day)
            market = self._market(code, day, sample.utc, funds, state['symbols'][code],
                                  code in foreign_orders or code in foreign_trades, state['orders'],
                                  quote_time=quote_time, window=windows[code], observation=observation,
                                  external_active=code in foreign_orders)
            if code in deferred:
                market.update(ticks=[], native_coverage=False, calendar_reason='CABINET_DAY_UNPROVED')
            markets.append(market)
        sequence = state['sequence'] + 1
        frame = {'id': f'sdk-{self.epoch}-{sequence}', 'sequence': sequence, 'session_id': self.epoch,
                 'time_semantics_version': TIME_SEMANTICS, 'time_sample': sample.paired_record(),
                 'wall_time': sample.now_utc, 'decision_utc': sample.now_utc, 'authorization_time': sample.now_utc,
                 'clock_health': self._health, 'sdk_activity': self._sdk_activity,
                 'monotonic_ms': str(sample.monotonic // 1_000_000), 'trading_day': day,
                 'trading_days': self.trading_days, 'calendar_hash': self.calendar.content_hash,
                 'synchronized': True, 'market': markets,
                 'sync_orders': coverage,
                 'native': changes}
        return frame, {row['contract_id']: row for row in markets}

    def _order_facts(self, orders, bindings):
        result = []
        for native_id, binding in bindings.items():
            order = orders.get(native_id)
            if order is None or not order.volume_orign or not order.status:
                continue
            if (order.order_id != native_id or self._contract(order) != binding['contract_id'] or
                    order.direction != binding['direction'] or order.offset != binding['native_offset'] or
                    native_integer(order.volume_orign, minimum=1) != binding['original_qty'] or
                    number(order.limit_price) != binding['limit_price']):
                raise TraderError('E_DSV_SDK_FACT', 'SDK order differs from the durable original child')
            filled = binding['original_qty'] - native_integer(order.volume_left)
            rejected = getattr(order, 'is_error', None) is True
            stamp = getattr(order, 'insert_date_time', None)
            if type(stamp) is int and stamp > 0:
                actual = native_day(self.calendar, binding['contract_id'], str(stamp))['trading_day']
                if actual != binding['native_trading_day']:
                    raise TraderError('E_DSV_SDK_FACT', 'SDK order belongs to a different cabinet day')
            elif not rejected:
                is_local_pending = (
                    order.status == 'ALIVE' and filled == 0 and (stamp is None or type(stamp) is int and stamp == 0)
                    and getattr(order, '_this_session', False) is True
                    and not getattr(order, 'exchange_order_id', None)
                    and binding['send_state'] == 'CALL_RETURNED')
                if is_local_pending and (
                        binding['child_order_id'] in self._local_returns
                        or getattr(self, 'owner_mode', None) == 'STANDALONE'):
                    result.append(self._fact('LOCAL_ORDER', binding, native_id))
                    continue
                raise TraderError('E_DSV_SDK_FACT', 'SDK order is missing its native acceptance time')
            if not 0 <= filled <= binding['original_qty'] or order.status not in {'ALIVE', 'FINISHED'} or (
                    rejected and (filled or order.status != 'FINISHED')):
                raise TraderError('E_DSV_SDK_FACT', 'SDK order cumulative status is contradictory')
            status = ('REJECTED' if rejected else 'FILLED' if filled == binding['original_qty'] else 'CANCELLED'
                      ) if order.status == 'FINISHED' else 'ALIVE'
            self._local_returns.discard(binding['child_order_id'])
            temporal = {'source_time_ns': str(stamp)} if type(stamp) is int and stamp > 0 else {}
            result.append(self._fact('ORDER', binding, native_id) | {'status': status, 'filled_qty': filled} | temporal)
        return result

    def _trade_facts(self, trades, bindings, day, accepted, *, needs_recovery, day_held=False):
        facts, foreign = [], {}
        deferred = set()
        for native_trade, trade in trades.items():
            binding = bindings.get(trade.order_id)
            if binding is None:
                if trade.order_id in self._legacy_native_orders:
                    continue
                code = self._contract(trade)
                if code in self.contracts:
                    foreign[native_trade] = code, getattr(trade, 'trade_date_time', None)
                continue
            if (trade.trade_id != native_trade or self._contract(trade) != binding['contract_id'] or
                    trade.direction != binding['direction'] or trade.offset != binding['native_offset']):
                raise TraderError('E_DSV_SDK_FACT', 'SDK trade does not match the durable original child')
            stamp = trade.trade_date_time
            if type(stamp) is not int:
                raise TraderError('E_DSV_SDK_FACT', 'SDK trade needs an exact nanosecond timestamp')
            ordered = accepted.get(trade.order_id)
            if ordered is not None and stamp + 5_000_000_000 < int(ordered):
                raise TraderError('E_DSV_SDK_FACT', 'Native trade precedes its original order timestamp')
            if ordered is None:
                # The account trade map can change before the original order
                # image arrives. Its next snapshot will supply the same trade.
                continue
            proof = native_day(self.calendar, binding['contract_id'], str(stamp))
            if proof['trading_day'] > day:
                if day_held:
                    deferred.add(binding['contract_id'])
                    continue
                raise TraderError('E_DSV_SDK_FACT', 'SDK trade belongs to an unobserved future cabinet day')
            price = number(trade.price)
            decimal_value(price, positive=True)
            facts.append((stamp, self._fact('TRADE', binding, trade.order_id) | {
                'native_trade_id': native_trade, 'quantity': native_integer(trade.volume, minimum=1), 'price': price,
                'fill_day': proof['trading_day'], 'source_time_ns': str(stamp)}))
        if self._foreign_trades is None:
            self._foreign_trades = set()
            for identity, (code, stamp) in foreign.items():
                if not needs_recovery and self.grant is None:
                    self._foreign_trades.add(identity)
                elif type(stamp) is int and stamp > 0:
                    try:
                        dated = native_day(self.calendar, code, str(stamp))['trading_day']
                    except TraderError:
                        continue
                    if dated < day and not any(order['contract_id'] == code and order['native_trading_day'] <= dated
                                               for order in bindings.values()):
                        self._foreign_trades.add(identity)
        return [fact for _, fact in sorted(facts, key=lambda item: item[0])], {
            code for native_trade, (code, _) in foreign.items() if native_trade not in self._foreign_trades}, deferred

    def _foreign_orders(self, orders, bindings):
        result = set()
        for native_id, order in orders.items():
            if native_id not in bindings and order.status == 'ALIVE' and order.volume_left:
                result.add(self._contract(order))
        return result

    def _check_legacy_orders(self, orders, trades):
        if not self._legacy_native_orders:
            return
        by_order = {}
        for trade_id, trade in trades.items():
            if trade.order_id in self._legacy_native_orders:
                by_order.setdefault(trade.order_id, {})[trade_id] = trade
        for native_id, expected in self._legacy_native_orders.items():
            native = orders.get(native_id)
            if native is not None:
                if (native.status != 'FINISHED' or self._contract(native) != expected['contract_id']
                        or native.direction != expected['direction'] or native.offset != expected['offset']
                        or native.volume_orign != expected['quantity'] or number(native.limit_price) != expected['limit_price']
                        or native.volume_orign - native.volume_left != expected['filled']):
                    raise TraderError('E_DSV_SDK_FACT', 'Original archived SDK order changed after migration')
            current = 0
            for identity, trade in by_order.get(native_id, {}).items():
                current += native_integer(trade.volume, minimum=1)
                if (expected['fills'].get(identity) != {'quantity': trade.volume, 'price': number(trade.price)}
                        or self._contract(trade) != expected['contract_id'] or trade.direction != expected['direction']
                        or trade.offset != expected['offset']):
                    raise TraderError('E_DSV_SDK_FACT', 'Original archived SDK trade changed after migration')
            if current > expected['filled']:
                raise TraderError('E_DSV_SDK_FACT', 'Original archived SDK fills increased after migration')

    def _fact(self, kind, order, native_id):
        return {'kind': kind, 'account_key': self.account_key, 'provider': self.provider,
                'contract_id': order['contract_id'], 'direction': order['direction'], 'native_offset': order['native_offset'],
                'child_order_id': order['child_order_id'], 'native_order_id': native_id, 'trading_day': order['native_trading_day']}

    @staticmethod
    def _contract(item):
        return item.exchange_id + '.' + item.instrument_id

    def _observe_quote(self, code, sample, current_window, day):
        quote = self.quotes[code]
        previous = self.quote_observations.get(code)
        last_change = None if previous is None else previous[2]
        raw = getattr(quote, 'datetime', None)
        malformed = False
        try:
            source = self._quote_time(quote)
        except (TypeError, ValueError, OverflowError):
            source, malformed = None, True
        session = None
        if source is not None:
            session = self.calendar.query(code, utc(source))['session']
        raw_age = None if source is None else source_age_ns(sample.utc, source, raw)
        quality, reason = 'UNKNOWN', 'QUOTE_UNAVAILABLE'
        if not self._protection_clock_ready():
            quality, reason = 'CLOCK_UNVERIFIED', 'CLOCK_PROTECTION_WINDOW_EXPIRED'
            source = None
        elif malformed:
            quality, reason = 'SOURCE_ANOMALY', 'QUOTE_TIMESTAMP_INVALID'
        elif source is not None:
            precise_time = source, source_fraction_tail(raw)
            active = current_window['session']
            if session is None:
                quality, reason = 'SOURCE_ANOMALY', 'QUOTE_SESSION_UNKNOWN'
            elif active is None:
                reason = 'QUOTE_OUTSIDE_CURRENT_SESSION'
            elif (session['session_id'], session['trading_day']) != (active['session_id'], day):
                quality, reason = 'SOURCE_ANOMALY', 'QUOTE_SESSION_MISMATCH'
            elif previous is not None and precise_time < previous[0]:
                quality, reason = 'SOURCE_ANOMALY', 'QUOTE_TIME_REGRESSION'
            else:
                quality, reason = quote_age_quality(raw_age, self._health)
                if quality != 'SOURCE_ANOMALY' and (previous is None or raw != previous[1]):
                    last_change = sample.monotonic
                    self.quote_observations[code] = precise_time, raw, last_change
        observation = {'source_time_raw': raw if source is not None else None,
                       'source_time_utc': None if source is None else utc(source),
                       'observed_at_utc': sample.now_utc, 'observed_mono_ns': sample.monotonic_ns,
                       'source_epoch': self.epoch, 'quote_identity': raw if source is not None else None,
                       'last_distinct_quote_mono_ns': None if last_change is None else str(last_change),
                       'source_age_raw_ns': None if raw_age is None else str(raw_age),
                       'silence_ns': None if last_change is None else str(max(0, sample.monotonic - last_change)),
                       'age_quality': quality, 'reason': reason,
                       'quote_session': None if session is None else session['session_id']}
        return (source if quality in {'VALID', 'CLOCK_UNVERIFIED'} else None), observation

    def _market(self, code, day, now, funds, previous, external, orders, *, quote_time=None, window=None,
                observation=None, external_active=None):
        if window is None:
            window = self.calendar.query(code, utc(now))
        active = bool(window['is_trading'] and window['trading_day'] == day)
        periods = [period for period in self.periods[code] if period['trading_day'] == day]
        session = window['session'] if active else next(
            (period for period in periods if datetime.fromisoformat(period['open_utc']) > now), periods[-1])
        position = self.api.get_position(code, account=self.account)
        quote = self.quotes[code]
        tick = self._tick(code, quote, session, quote_time) if active else None
        standalone_model = getattr(self, 'owner_mode', None) == 'STANDALONE'
        active_ext = bool(external if external_active is None else external_active)
        result = {'contract_id': code, 'session_open': session['open_utc'], 'session_close': session['close_utc'],
                  'opening_session': session['is_opening_session'], 'ticks': [] if tick is None else [tick],
                  'external_orders': active_ext if standalone_model else bool(external),
                  ' native_coverage': window['complete']}
        result.pop(' native_coverage', None)
        result['native_coverage'] = window['complete']
        result['market_session_active'] = active
        if standalone_model:
            result['standalone_external_model'] = True
            result['external_active_orders'] = active_ext
        prof = getattr(self, 'profile', None)
        if prof is not None:
            result['limit_fallback_ratio'] = str(
                prof.to_record()['settings']['trade'].get('limit_fallback_ratio', '0.01'))
        if observation is not None:
            prior_liquidation = next((period for period in reversed(periods) if period['force_close_at_end']
                                      and datetime.fromisoformat(period['close_utc']) <= now), None)
            result.update(calendar_session_id=session['session_id'] if active else None,
                          calendar_reason=('CALENDAR_COVERAGE_UNKNOWN' if not window['complete'] else
                                           'CABINET_DAY_UNPROVED' if window['is_trading'] and not active else None),
                          prior_liquidation=prior_liquidation,
                          quote_observation=observation | {
                              'native_candle_id': None if tick is None else tick['candle_id'],
                              'native_candle_ns': None if tick is None else tick['native_candle_ns']})
        for field, native in _POSITION.items():
            result[field] = native_integer(getattr(position, native))
        if active:
            if self.grant is not None and previous.get('position'):
                result['max_order_qty'] = self.grant.value['max_order_quantity']
            end_hour = datetime.fromisoformat(session['close_utc']).astimezone(_BEIJING).hour
            if end_hour == 15 or end_hour >= 21 or end_hour < 3:
                result['entry_close'] = session['close_utc']
            if session['force_close_at_end']:
                result['liquidation_at'] = session['close_utc']
        if (tick and (night := previous.get('night_reference')) and not night['complete']
                and not is_night_session(datetime.fromisoformat(session['open_utc']))
                and self.calendar.query(code, night['native_time'])['trading_day'] == day):
            result['night_boundary'] = night['native_time']
        result.update(available=_amount(funds.available, rounding=ROUND_FLOOR), equity=_amount(funds.balance, rounding=ROUND_FLOOR),
                      margin=_amount(funds.margin, rounding=ROUND_CEILING), frozen_margin=_amount(funds.frozen_margin, rounding=ROUND_CEILING),
                      margin_per_lot=_amount(getattr(quote, 'margin', None), rounding=ROUND_CEILING),
                      multiplier=_amount(getattr(quote, 'volume_multiple', None)),
                      symbol_margin=_amount(getattr(position, 'margin', None), rounding=ROUND_CEILING))
        if result['symbol_margin'] is None and not (result['long_qty'] or result['short_qty']):
            result['symbol_margin'] = '0'
        risk = number(funds.risk_ratio)
        if risk is not None and -1 < Decimal(risk) < 10**16:
            result['risk_ratio'] = decimal_text(Decimal(risk).quantize(Decimal('0.000000000001'), rounding=ROUND_FLOOR))
        # SDK frozen margin covers native-acknowledged children; calls still
        # invisible to the SDK reserve local margin until they are reconciled.
        pending = '0'
        for order in orders.values():
            if order['effect'] == 'OPEN' and not terminal(order) and order['send_state'] != 'NATIVE_SEEN':
                pending = None
                break
        result['pending_margin'] = pending
        expiry = expiry_date(getattr(quote, 'expire_datetime', None))
        if expiry is not None and now is not None:
            # DSV subtracts the full Beijing timestamp from expiry-day midnight,
            # then floors through timedelta.days; a date-only subtraction is one
            # day too large after midnight. Keep the decision independent of host TZ.
            midnight = datetime.combine(expiry, datetime.min.time(), tzinfo=_BEIJING)
            result['days_to_expiry'] = str((midnight - (quote_time or now).astimezone(_BEIJING)).days)
        for field, native in (('min_order_qty', 'min_limit_order_volume'), ('max_order_qty', 'max_limit_order_volume')):
            value = getattr(quote, native, None)
            if value is not None and active and not (type(value) is int and value == 0):
                actual = native_integer(value, minimum=1)
                result[field] = min(actual, result.get(field, actual))
        return result

    @staticmethod
    def _quote_time(quote):
        stamp = getattr(quote, 'datetime', None)
        if not stamp or getattr(quote, 'expired', False):
            return None
        return quote_timestamp(stamp, _BEIJING)

    def _tick(self, code, quote, session, moment):
        start, end = map(datetime.fromisoformat, (session['open_utc'], session['close_utc']))
        if moment is None or not start <= moment < end:
            return None
        native_time = utc(moment)
        stamp = quote.datetime
        serial = self.minutes[code]
        try:
            tail = serial.iloc[-1]['datetime']
            minute = int(tail)
            candle = str(minute) if minute > 0 and minute == tail else None
        except (IndexError, KeyError, TypeError, ValueError, OverflowError):
            candle = None
        if candle is not None and self.grant is not None:
            # The legacy owner derives its opaque ID from the UTC minute.
            kline_stamp = datetime.fromtimestamp((minute // 1_000_000_000), timezone.utc)
            candle = code + ':' + kline_stamp.strftime('%Y%m%dT%H%M')
        return {'native_time': native_time, 'candle_id': candle,
                'native_candle_ns': str(minute) if candle is not None else None, 'native_quote_stamp': stamp,
                **{name: _amount(getattr(quote, field, None)) for name, field in _PRICE_FIELDS.items()}}

    def _authorized(self, effect, kind, *, require_enabled=True):
        if (get_ident() != self.owner or (require_enabled and not self.enabled)
                or self.session is None or self.session._closed):
            raise TraderError('E_DSV_SDK_OWNER', 'SDK dispatch requires a synchronized, bound owner')
        if (effect['kind'] != kind or effect['account_key'] != self.account_key or effect['provider'] != self.provider
                or effect['id'] in self._seen_effects or
                not self.session._journal.effect_is_prepared(effect)):
            raise TraderError('E_DSV_SDK_SCOPE', 'SDK dispatch requires an unreplayed committed effect')
        return self.order_id(effect['child_order_id'])

    def opening_limit(self, state, market, spec, now):
        if self.grant is None:
            return None
        value = self.grant.value
        if state.get('scope_expired_at') or not self.opening_window(state['trading_day'], now):
            return 'SCOPE_OPENING_WINDOW_CLOSED'
        if self.grant.daily_targets_per_contract is not None:
            used = state['symbols'][market['contract_id']]['target_count']
            if used >= self.grant.daily_targets_per_contract:
                return 'SCOPE_TARGET_LIMIT'
        elif state.get('sdk_scope_targets', 0) >= value['opening_attempts']:
            return 'SCOPE_TARGET_LIMIT'
        price = market['tick']['last']
        multiplier = market.get('multiplier')
        if price in (None, 'NaN') or multiplier in (None, '0') or Decimal(price) <= 0:
            return 'SCOPE_RISK_UNPROVED'
        per_lot = Decimal(spec['stop_offset']) * Decimal(multiplier)
        held_risk = sum((Decimal(row['position']['stop']) * Decimal(row['position']['sdk_multiplier']) *
                         (row['position']['owned'] if row['position']['entry_stopped']
                          and all(terminal(state['orders'][child]) for child in row['position']['children'])
                          else row['position']['quantity'])
                         for row in state['symbols'].values() if row['position']), Decimal(0))
        available = max(Decimal(0), Decimal(value['risk_budget']) - held_risk)
        limit = 0 if per_lot <= 0 else int(available / per_lot)
        market['max_order_qty'] = min(market.get('max_order_qty', 2_147_483_647), value['max_order_quantity'], limit)
        return None if market['max_order_qty'] >= market.get('min_order_qty', 1) else 'SCOPE_QUANTITY_LIMIT'

    def send(self, effect):
        native = self._authorized(effect, 'SEND')
        rejection_reason = 'PRE_SEND_VALIDATION'
        # _authorized remains outside the proof boundary: a reused/ambiguous
        # effect must never be downgraded to definitely unsent.
        try:
            if not self._protection_clock_ready():
                rejection_reason = 'CLOCK_PROTECTION_WINDOW_EXPIRED'
                raise TraderError('E_DSV_SDK_CLOCK', 'Application UTC protection window expired')
            market = self.dispatch_market[effect['contract_id']]
            tick = market['ticks'][-1] if market['ticks'] else None
            standalone_model = bool(market.get('standalone_external_model'))
            ext_blocked = (
                market.get('external_active_orders', False) if (standalone_model and effect['native_offset'] == 'OPEN')
                else False if standalone_model
                else market['external_orders'])
            if (tick is None or ext_blocked or
                    legal_price(tick, Decimal(effect['limit_price'])) != effect['limit_price']):
                rejection_reason = 'FOREIGN_ACTIVE_ORDER' if ext_blocked and standalone_model else 'QUOTE_OR_PRICE_SCOPE'
                raise TraderError('E_DSV_SDK_SCOPE', 'A native dispatch requires a legal price and current scoped quote')
            clock = self._clock_sample()
            if 'calendar_session_id' in market:
                window = self.calendar.query(effect['contract_id'], clock.now_utc)
                if (self._continuity.compare(clock)['reason'] or not window['is_trading']
                        or window['trading_day'] != effect['trading_day']
                        or window['session']['session_id'] != market['calendar_session_id']):
                    rejection_reason = 'QUOTE_OR_PRICE_SCOPE'
                    raise TraderError('E_DSV_SDK_SCOPE', 'The selected quote or trading session changed before dispatch')
                live_quote = self.quotes[effect['contract_id']]
                if live_quote.datetime != tick['native_quote_stamp']:
                    live_source = None
                    try:
                        live_source = self._quote_time(live_quote)
                    except (TypeError, ValueError, OverflowError):
                        live_source = None
                    live_tick = (self._tick(effect['contract_id'], live_quote, window['session'], live_source)
                                 if live_source is not None else None)
                    limit_dec = Decimal(effect['limit_price'])
                    buy = effect['direction'] == 'BUY'
                    opp = None if live_tick is None else live_tick.get('ask1' if buy else 'bid1')
                    ref_price = opp if price_value(opp) is not None else (
                        None if live_tick is None else live_tick.get('last'))
                    price_still_safe = (
                        live_tick is not None
                        and legal_price(live_tick, limit_dec) == effect['limit_price']
                        and price_value(ref_price) is not None
                        and (Decimal(ref_price) <= limit_dec if buy else Decimal(ref_price) >= limit_dec))
                    if not price_still_safe:
                        rejection_reason = 'QUOTE_REVISION_CHANGED'
                        raise TraderError('E_QUOTE_REVISION_CHANGED',
                                          'Quote revision changed before dispatch; reprice required')
                    tick = live_tick
                stamp = tick['native_quote_stamp']
                source = quote_timestamp(stamp, _BEIJING)
                age = source_age_ns(clock.utc, source, stamp)
                self.metrics.update(pre_dispatch_monotonic_ns=clock.monotonic_ns, pre_dispatch_quote_age_ns=str(age))
                health = self.clock.health(clock) if hasattr(self.clock, 'health') else self._health
                limit = Decimal(self.profile.to_record()['settings']['trade']['max_quote_age_seconds'])
                # Ordinary DSV entries compare against the source's whole second.
                elapsed = delta_ns(clock.utc, source.replace(microsecond=0))
                if (quote_age_quality(age, health)[0] == 'SOURCE_ANOMALY'
                        or (effect['native_offset'] == 'OPEN' and limit > 0 and elapsed > limit * NS_PER_SECOND)):
                    rejection_reason = 'QUOTE_AGE'
                    raise TraderError('E_DSV_SDK_SCOPE', 'The selected quote aged beyond its execution policy')
            if self.grant is not None:
                if (effect['contract_id'] not in self.grant.value['contracts'] or
                        not self.grant.permits_day(effect['trading_day']) or
                        effect['native_offset'] == 'OPEN' and (
                            getattr(self.session, '_state', {}).get('scope_expired_at')
                            or not self.opening_window(effect['trading_day'], clock.utc))):
                    rejection_reason = 'ACCOUNT_SCOPE_OR_WINDOW'
                    raise TraderError('E_DSV_SDK_SCOPE', 'The SDK request exceeded its explicit account scope')
            request = dict(symbol=effect['contract_id'], direction=effect['direction'],
                           offset=effect['native_offset'], volume=quantity(effect['quantity'], positive=True),
                           limit_price=float(Decimal(effect['limit_price'])), order_id=native, account=self.account)
        except Exception as error:
            return SendNotAttempted(effect['id'], getattr(error, 'code', 'E_DSV_PRE_SEND'),
                                    exception_record(error), rejection_reason)
        self._seen_effects.add(effect['id'])
        # Never turn an exception from inside insert_order into unsent proof,
        # even if it has the same error code as a local validation failure.
        self.api.insert_order(**request)
        self._local_returns.add(effect['child_order_id'])
        return None

    def cancel(self, effect):
        native = self._authorized(effect, 'CANCEL', require_enabled=False)
        try:
            if not self.enabled:
                raise TraderError('E_DSV_SDK_OWNER', 'SDK cancellation is waiting for the owner dispatch phase')
            if native != effect['native_order_id']:
                raise TraderError('E_DSV_SDK_SCOPE', 'SDK cancellation requires the original native order identity')
        except Exception as error:
            return CancelNotAttempted(effect['id'], effect['native_order_id'],
                                      getattr(error, 'code', 'E_DSV_PRE_CANCEL'), exception_record(error))
        self._seen_effects.add(effect['id'])
        try:
            return self.api.cancel_order(native, account=self.account)
        except Exception as error:
            if self._sdk_cancel_refused_before_queue(error):
                return CancelNotAttempted(effect['id'], native, 'E_DSV_SDK_CANCEL_RISK', exception_record(error))
            raise

    def _sdk_cancel_refused_before_queue(self, error):
        """Recognize the pinned SDK's rejection before its _send_pack call."""
        try:
            from tqsdk.api import TqApi
            from tqsdk.exceptions import TqRiskRuleError
            from tqsdk.risk_manager import TqRiskManager
        except ImportError:
            return False
        if (type(error) is not TqRiskRuleError
                or getattr(self.api.cancel_order, '__func__', None) is not TqApi.cancel_order):
            return False
        traceback = error.__traceback__
        while traceback is not None:
            if traceback.tb_frame.f_code is TqApi.cancel_order.__code__:
                refusal = traceback.tb_next
                return (refusal is not None and refusal.tb_next is None
                        and refusal.tb_frame.f_code is TqRiskManager._could_cancel_order.__code__)
            traceback = traceback.tb_next
        return False
