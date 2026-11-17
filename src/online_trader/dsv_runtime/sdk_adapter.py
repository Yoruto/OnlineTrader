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
from online_trader.dsv_runtime.controller import SendNotAttempted, legal_price, terminal
from online_trader.scheduling.reference import CalibratedClock
from online_trader.errors import TraderError, exception_record
from online_trader.gateway.dsv_facts import native_integer, utc
from online_trader.gateway.native_days import native_day
from online_trader.gateway.tqkq import number
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.calendar import TradingCalendar
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
                 _legacy_native_orders=None):
        if sdk_version != SDK_VERSION or not isinstance(profile, DsvProfile) or not isinstance(calendar, TradingCalendar):
            raise TraderError('E_DSV_SDK_SCOPE', 'Expected the pinned SDK, normalized profile and explicit calendar')
        identifier(account_key)
        grant = None if _connection is None else getattr(_connection, 'grant', None)
        if account is None or (_connection is None and not account_key.startswith('DEV:')) or (
                _connection is not None and (type(grant) is not DsvSimulationGrant or not grant.replacement
                or grant.account_key != account_key or _connection.api is not api or _connection.account is not account
                or not _connection.broker_connected or _connection.read_only)):
            raise TraderError('E_DSV_SDK_SCOPE', 'Expected an explicitly bound SDK account and connection')
        self.api, self.account, self.account_key = api, account, account_key
        self.grant, self.connection = grant, _connection
        self.provider = 'OFFLINE' if grant is None else grant.provider
        self.profile, self.calendar = profile, calendar
        self.contracts = tuple(row['code'] for row in profile.to_record()['settings']['symbols'])
        calendar_record = calendar.to_record()
        self.periods = {row['contract_id']: row['sessions'] for row in calendar_record['contracts']}
        if not calendar_record['complete'] or any(not self.periods.get(code) for code in self.contracts):
            raise TraderError('E_DSV_SDK_SCOPE', 'The calendar must cover every selected contract')
        day_sets = [{period['trading_day'] for period in self.periods[code]} for code in self.contracts]
        self.trading_days = sorted(set.intersection(*day_sets)) if day_sets else []
        self.clock = clock if clock is not None else lambda: (datetime.now(timezone.utc), time.monotonic_ns() // 1_000_000)
        self.owner = get_ident()
        self.session = None
        self.epoch = uuid4().hex
        self.enabled = False
        self.synchronized = False
        self.flat_confirmed = False
        self.dispatch_market = {}
        self.quote_times = {}
        self._seen_effects = set()
        self._local_returns = set()
        self._foreign_trades = None
        self._legacy_native_orders = _legacy_native_orders or {}
        self.quotes = {code: api.get_quote(code) for code in self.contracts}
        self.minutes = {code: api.get_kline_serial(code, 60, data_length=500) for code in self.contracts}

    def matches_online_scope(self, account, provider):
        return self.grant is not None and (account, provider) == (self.account_key, self.provider)

    def check_start(self, state):
        now, _ = self.clock()
        if not self.entry_clock_ready():
            raise TraderError('E_CLOCK', '无法校准应用时间，已暂停新开仓。')
        if not self.synchronized or not state['synchronized'] or not self.grant.in_window(now):
            raise TraderError('E_DSV_SDK_SYNC', 'Start needs a current synchronized account within the reviewed window')
        if not self.grant.permits_day(state['trading_day']):
            raise TraderError('E_DSV_SDK_SCOPE', 'Start needs the authorized cabinet day')

    def entry_clock_ready(self):
        clock_ready = getattr(self.connection, 'entry_clock_ready', None)
        return clock_ready is None or clock_ready()

    def opening_window(self, day, now):
        return self.entry_clock_ready() and self.grant.permits_day(day) and self.grant.in_window(now)

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
        self.quote_times = {}
        self._local_returns.clear()
        self.epoch = uuid4().hex
        session.disconnect('SDK_DISCONNECTED')

    def command(self, session, command):
        """Run a user command through the same owner and committed effect path."""
        state = self._bind(session)
        self.enabled = self.synchronized and state['synchronized']
        try:
            return session.command(command)
        finally:
            self.enabled = False

    def step(self, session, *, trading_day, complete):
        state = self._bind(session)
        self.enabled = False
        try:
            if self.connection is None:
                self.api.wait_update(deadline=time.time() + 0.5)
            else:
                clock = getattr(self.connection, 'clock', None)
                if isinstance(clock, CalibratedClock):
                    clock.poll()
                epoch = self.connection.source_epoch
                self.connection.poll_updates(0.5)
                if self.connection.source_epoch != epoch:
                    self.disconnect(session)
                    state = session.snapshot()
            synchronized = complete() if callable(complete) else complete
            if type(synchronized) is not bool:
                raise TraderError('E_DSV_SDK_SYNC', 'SDK synchronization must be supplied as a complete fact')
            now, mono = self.clock()
            if not isinstance(now, datetime) or now.utcoffset() is None or type(mono) is not int or mono < 0:
                raise TraderError('E_DSV_SDK_CLOCK', 'Expected an explicit wall and monotonic clock sample')
            if not synchronized:
                self.disconnect(session)
                return {'type': 'DISCONNECTED', 'synchronized': False}
            quotes = {}
            for code, quote in self.quotes.items():
                moment = self._quote_time(quote)
                if moment is None:
                    continue
                window = self.calendar.query(code, utc(moment))
                if not window['complete'] or window['is_trading'] and window['trading_day'] < state['trading_day']:
                    continue
                if moment >= self.quote_times.get(code, moment):
                    self.quote_times[code] = moment
                    quotes[code] = moment
            market_time = max(self.quote_times.values(), default=None)
            if callable(trading_day):
                trading_day = (state['trading_day'] if market_time is None else
                               trading_day(state['trading_day'], market_time))
            frame, market = self._frame(state, market_time, mono, trading_day, quotes, now)
            self.enabled = True
            self.dispatch_market = market
            receipt = session.apply_round(frame)
            self.synchronized = session.snapshot()['synchronized']
            self.flat_confirmed = self.synchronized and all(
                row['long_qty'] == row['short_qty'] == 0 and not row['external_orders'] for row in market.values())
            return receipt
        except BaseException:
            self.synchronized = False
            self.flat_confirmed = False
            self.dispatch_market = {}
            self.quote_times = {}
            self._local_returns.clear()
            self.epoch = uuid4().hex
            if not session._failed:
                session.disconnect('SDK_ROUND_FAILED')
            raise
        finally:
            self.enabled = False

    def _frame(self, state, now, mono, day, quotes, authorization_time):
        if day not in self.trading_days or day < state['trading_day']:
            raise TraderError('E_DSV_SDK_SCOPE', 'Cabinet day is outside of the selected calendar')
        active_days = {window['trading_day'] for code in self.contracts
                       if now is not None and (window := self.calendar.query(code, utc(now)))['is_trading']}
        if active_days and active_days != {day} or day != state['trading_day'] and not active_days:
            raise TraderError('E_DSV_SDK_SCOPE', 'A cabinet-day change requires a matching active calendar session')
        bindings = {self.order_id(child): order for child, order in state['orders'].items()}
        orders = self.api.get_order(account=self.account)
        trades = self.api.get_trade(account=self.account)
        self._check_legacy_orders(orders, trades)
        native = self._order_facts(orders, bindings)
        foreign_orders = self._foreign_orders(orders, bindings)
        accepted = {self.order_id(order['child_order_id']): order.get('native_order_time_ns') for order in state['orders'].values()}
        accepted.update({fact['native_order_id']: fact['source_time_ns'] for fact in native if 'source_time_ns' in fact})
        details, foreign_trades = self._trade_facts(trades, bindings, day, accepted, needs_recovery=bool(state['orders']))
        funds = self.api.get_account(account=self.account)
        markets = [self._market(code, day, now, funds, state['symbols'][code],
                                code in foreign_orders or code in foreign_trades, state['orders'], quote_time=quotes.get(code))
                   for code in self.contracts]
        if isinstance(getattr(self.connection, 'clock', None), CalibratedClock):
            observation_time = utc(authorization_time)
            for market in markets:
                market['wall_reads'] = {name: observation_time for name in ('close', 'pending', 'position_sync')}
        if any(row.get('native_coverage') is False for row in markets):
            raise TraderError('E_DSV_SDK_SCOPE', 'Market time is outside the complete calendar coverage')
        sequence = state['sequence'] + 1
        # A quote-less startup can reconcile account facts without advancing strategy time.
        stamp = utc(now) if now is not None else min(row['session_open'] for row in markets)
        frame = {'id': f'sdk-{self.epoch}-{sequence}', 'sequence': sequence, 'session_id': self.epoch,
                 'wall_time': stamp, 'authorization_time': utc(authorization_time.astimezone(timezone.utc)),
                 'market_clock': True, 'monotonic_ms': str(mono), 'trading_day': day,
                 'trading_days': self.trading_days, 'calendar_hash': self.calendar.content_hash,
                 'synchronized': True, 'market': markets,
                 'sync_orders': [fact['child_order_id'] for fact in native if fact['kind'] == 'ORDER'],
                 'native': native + details}
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
                if (order.status == 'ALIVE' and filled == 0 and (stamp is None or type(stamp) is int and stamp == 0)
                        and getattr(order, '_this_session', False) is True
                        and not getattr(order, 'exchange_order_id', None)
                        and binding['send_state'] == 'CALL_RETURNED'
                        and binding['child_order_id'] in self._local_returns):
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

    def _trade_facts(self, trades, bindings, day, accepted, *, needs_recovery):
        facts, foreign = [], {}
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
            if ordered is not None and stamp < int(ordered):
                raise TraderError('E_DSV_SDK_FACT', 'Native trade precedes its original order timestamp')
            if ordered is None:
                # The account trade map can change before the original order
                # image arrives. Its next snapshot will supply the same trade.
                continue
            proof = native_day(self.calendar, binding['contract_id'], str(stamp))
            if proof['trading_day'] > day:
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
            code for native_trade, (code, _) in foreign.items() if native_trade not in self._foreign_trades}

    def _foreign_orders(self, orders, bindings):
        result = set()
        for native_id, order in orders.items():
            if native_id not in bindings and order.status == 'ALIVE' and order.volume_left:
                result.add(self._contract(order))
        return result

    def _check_legacy_orders(self, orders, trades):
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

    def _market(self, code, day, now, funds, previous, external, orders, *, quote_time=None):
        window = (self.calendar.query(code, utc(now)) if now is not None else
                  {'is_trading': False, 'complete': True})
        periods = [period for period in self.periods[code] if period['trading_day'] == day]
        session = window['session'] if window['is_trading'] else next(
            (period for period in periods if now is None or datetime.fromisoformat(period['open_utc']) > now), periods[-1])
        position = self.api.get_position(code, account=self.account)
        quote = self.quotes[code]
        tick = self._tick(code, quote, session, quote_time) if window['is_trading'] else None
        result = {'contract_id': code, 'session_open': session['open_utc'], 'session_close': session['close_utc'],
                  'opening_session': session['is_opening_session'], 'ticks': [] if tick is None else [tick],
                  'external_orders': external, 'native_coverage': window['complete'],
                  'market_session_active': window['is_trading']}
        for field, native in _POSITION.items():
            result[field] = native_integer(getattr(position, native))
        if window['is_trading']:
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
            if value is not None and window['is_trading']:
                actual = native_integer(value, minimum=1)
                result[field] = min(actual, result.get(field, actual))
        return result

    @staticmethod
    def _quote_time(quote):
        stamp = getattr(quote, 'datetime', None)
        if not stamp or getattr(quote, 'expired', False):
            return None
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError as error:
            raise TraderError('E_DSV_SDK_FACT', 'Invalid SDK quote time') from error
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_BEIJING)
        return moment.astimezone(timezone.utc)

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

    def _authorized(self, effect, kind):
        if get_ident() != self.owner or not self.enabled or self.session is None or self.session._closed:
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
        if not self.opening_window(state['trading_day'], now):
            return 'SCOPE_OPENING_WINDOW_CLOSED'
        if state.get('sdk_scope_targets', 0) >= value['opening_attempts']:
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
            market = self.dispatch_market[effect['contract_id']]
            tick = market['ticks'][-1] if market['ticks'] else None
            if (tick is None or market['external_orders'] or
                    legal_price(tick, Decimal(effect['limit_price'])) != effect['limit_price']):
                rejection_reason = 'QUOTE_OR_PRICE_SCOPE'
                raise TraderError('E_DSV_SDK_SCOPE', 'A native dispatch requires a legal price and current scoped quote')
            if self.grant is not None:
                now, _ = self.clock()
                if (effect['contract_id'] not in self.grant.value['contracts'] or
                        not self.grant.permits_day(effect['trading_day']) or
                        effect['native_offset'] == 'OPEN' and not self.opening_window(effect['trading_day'], now)):
                    rejection_reason = 'ACCOUNT_SCOPE_OR_WINDOW'
                    raise TraderError('E_DSV_SDK_SCOPE', 'The SDK request exceeded its explicit account scope')
                if effect['native_offset'] == 'OPEN' and isinstance(
                        getattr(self.connection, 'clock', None), CalibratedClock):
                    source_time = self._quote_time(self.quotes[effect['contract_id']])
                    max_age = float(self.profile.to_record()['settings']['trade']['max_quote_age_seconds'])
                    age = (now - source_time).total_seconds() if source_time is not None else float('inf')
                    if source_time is None or age < -1 or (max_age > 0 and age > max_age):
                        rejection_reason = 'QUOTE_OR_PRICE_SCOPE'
                        raise TraderError('E_DSV_SDK_SCOPE', 'Opening requires a current native quote')
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
        native = self._authorized(effect, 'CANCEL')
        if native != effect['native_order_id']:
            raise TraderError('E_DSV_SDK_SCOPE', 'SDK cancellation requires the original native order identity')
        self._seen_effects.add(effect['id'])
        return self.api.cancel_order(native, account=self.account)
