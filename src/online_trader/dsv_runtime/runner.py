"""Replay native batches through one account owner and one fact store."""

import copy
from datetime import date, datetime, timedelta
from decimal import Decimal
import sqlite3
from threading import get_ident
from uuid import uuid4

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import content_hash
from online_trader.domain.values import decimal_text, decimal_value, identifier, quantity
from online_trader.dsv_core.business_statistics import reduce_stops
from online_trader.dsv_core.month_stop import observe_month_stop
from online_trader.dsv_core.transition import continuation_entry, ordinary_entry, price_value, protect
from online_trader.dsv_core.price_rules import scheduled_close_mode
from online_trader.dsv_runtime.controller import ExplicitOrderController, terminal, tighten_target
from online_trader.dsv_storage.journal import Journal
from online_trader.dsv_storage.workbench_evidence import WorkbenchEvidence
from online_trader.errors import TraderError
from online_trader.gateway.ports import OnlineSimulationPort
from online_trader.runtime.instance import AccountLock
from online_trader.strategy.entry import is_night_session


_CLOCK_READS = {'session', 'opening', 'entry_close', 'quote_age', 'cooldown', 'chase', 'submit',
                'close', 'pending', 'protection', 'pending_start', 'price_context', 'stale', 'opening_observation',
                'position_sync'}
_PRICE_FIELDS = ('last', 'day_high', 'day_low', 'bid1', 'ask1', 'tick_size', 'lower_limit', 'upper_limit')
_FUNDS_FIELDS = ('available', 'margin_per_lot', 'multiplier', 'equity', 'risk_ratio', 'margin',
                 'frozen_margin', 'pending_margin', 'symbol_margin')
_BUCKETS = ('long_today', 'long_historical', 'short_today', 'short_historical')


def _day(value):
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except (TypeError, ValueError):
        pass
    raise TraderError('E_DSV_SCOPE', 'Expected a canonical cabinet trading day')


def _utc(value):
    if type(value) is not str or not value.endswith('Z'):
        raise TraderError('E_DSV_ROUND', 'Expected an explicit UTC timestamp ending in Z')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise TraderError('E_DSV_ROUND', 'Invalid UTC timestamp') from error
    if parsed.utcoffset() != timedelta(0):
        raise TraderError('E_DSV_ROUND', 'Expected a UTC timestamp')
    return parsed


def _initial_symbol():
    return {'position': None, 'last_signal_bar': None, 'last_order_ms': None, 'target_count': 0,
            'net_loss': '0', 'day_stop': False, 'last_stop': None, 'tick': None, 'tick_session': None,
            'night_reference': None, 'continuation_attempted': False, 'continuation_stopped': False,
            'stop_streak': 0, 'month_stop': None, 'stop_days': []}


def initial_state(trading_day, contracts):
    return {'running': False, 'sequence': 0, 'monotonic_ms': None, 'session_id': None,
            'trading_day': trading_day, 'synchronized': True,
            'symbols': {key: _initial_symbol() for key in contracts}, 'orders': {}, 'retired_positions': {}}


class DsvSession:
    """Public synchronous offline runtime. Pass complete scoped snapshots and ordered native changes."""

    def __init__(self, database, *, account_key, provider, trading_day, profile, port, _execution_lock=None):
        identifier(account_key)
        offline = account_key.startswith('DEV:') and provider == 'OFFLINE' and not isinstance(port, OnlineSimulationPort)
        if not offline:
            from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
            if (type(port) is not DsvSdkAdapter or not port.matches_online_scope(account_key, provider)
                    or type(_execution_lock) is not AccountLock or not _execution_lock.matches(account_key)):
                raise TraderError('E_DSV_SCOPE', 'The SDK session requires its explicitly bound account, adapter and owner lock')
        if not isinstance(profile, DsvProfile):
            raise TraderError('E_DSV_SCOPE', 'Expected a normalized DSV business profile')
        _day(trading_day)
        self._owner = get_ident()
        self._closed = False
        self._failed = False
        self._busy = False
        self._database = database
        self._workbench_reader = None
        self._lock = _execution_lock if _execution_lock is not None else AccountLock(account_key)
        self._owns_lock = _execution_lock is None
        self._sdk_port = port if not offline else None
        self._scope = {'account_key': account_key, 'provider': provider, 'trading_day': trading_day,
                       'configuration_id': profile.content_hash}
        settings = profile.to_record()['settings']
        self._trade = settings['trade']
        self._symbols = {item['code']: item for item in settings['symbols']}
        initial = initial_state(trading_day, self._symbols)
        try:
            self._journal = Journal(database, self._scope, initial)
            self._state = self._journal.load()
            if not offline:
                self._bind_sdk_grant()
            self._state.setdefault('trading_day', trading_day)
            self._state.setdefault('synchronized', True)
            self._state.setdefault('retired_positions', {})
            for row in self._state['symbols'].values():
                position = row['position']
                if position is not None and 'fills' not in position:
                    position.update(open_day=trading_day, legacy_owned=position['owned'], fills=[])
            self._controller = ExplicitOrderController(self._scope, self._journal, port)
        except BaseException:
            if hasattr(self, '_journal'):
                self._journal.close()
            if self._owns_lock:
                self._lock.close()
            raise

    def _bind_sdk_grant(self):
        grant = self._sdk_port.grant
        binding = {'grant_id': grant.value['grant_id'], 'grant_hash': grant.content_hash}
        with self._journal.transaction():
            existing = self._journal.sdk_scope_grant()
            if existing is None:
                self._journal.event('SDK_SCOPE_BOUND', binding)
            elif existing != binding:
                raise TraderError('E_DSV_SDK_SCOPE', 'Replacement facts already bind a different finite grant')

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()

    def _check_owner(self):
        if self._closed or self._owner != get_ident():
            raise TraderError('E_DSV_OWNER', 'The same session owner must drive the fact writer and effects')
        if self._failed:
            raise TraderError('E_DSV_STORE', 'Reopen and reconcile prepared effects after a storage failure')

    def close(self):
        if self._closed:
            return
        if self._owner != get_ident() or self._busy:
            raise TraderError('E_DSV_OWNER', 'The session must close on its owning thread')
        try:
            if self._workbench_reader is not None:
                self._workbench_reader.close()
            self._journal.close()
        finally:
            if self._owns_lock:
                self._lock.close()
            self._closed = True

    def snapshot(self):
        self._check_owner()
        state = copy.deepcopy(self._state)
        state['scope'] = dict(self._scope)
        for contract, row in state['symbols'].items():
            position = row['position']
            if position is not None:
                position.update(self._controller.position_buckets(state, position, contract))
            if detached := row.get('detached_positions'):
                row['detached_owned'] = sum(state['retired_positions'][key]['owned'] for key in detached)
                row['detached_cycles'] = []
                for key in detached:
                    cycle = state['retired_positions'][key]
                    row['detached_cycles'].append({
                        'position_id': key, 'direction': cycle['direction'], 'owned': cycle['owned'],
                        'reviewed_exit_target': cycle.get('reviewed_exit_target'),
                        **self._controller.position_buckets(state, cycle, contract)})
        state.pop('retired_positions', None)
        return state

    def workbench_evidence(self):
        self._check_owner()
        if self._workbench_reader is None:
            self._workbench_reader = WorkbenchEvidence(self._database)
        result = self._workbench_reader.request()
        activity = [dict(row) for row in result['activity']]
        fills = [dict(row) for row in result['fills']]
        for row in activity + fills:
            if order := self._state['orders'].get(row.get('child_order_id')):
                row['contract_id'] = order['contract_id']
                row['direction'] = order['direction']
                row['native_offset'] = order['native_offset']
            if row.get('reason') == 'EXPIRY_ENTRY_BLOCK':
                row['min_days_to_expiry'] = self._trade['min_days_to_expiry']
        return {'history_status': result['history_status'], 'activity': activity, 'fills': fills}

    def command_receipt(self, command_id):
        self._check_owner()
        return self._journal.command_receipt(identifier(command_id))

    def verify_scope(self, expected):
        self._check_owner()
        if expected is not None and expected != self._scope:
            raise TraderError('E_DSV_SCOPE', 'The offline owner account or profile changed')

    def verify_day(self, expected):
        self._check_owner()
        if expected is not None and expected != self._state['trading_day']:
            raise TraderError('E_DSV_SCOPE', 'The observed cabinet trading day changed')

    def disconnect(self, reason='FEED_DISCONNECTED'):
        """Keep the last committed owner state; require a fresh complete round before effects."""
        self._check_owner()
        if self._busy:
            raise TraderError('E_DSV_OWNER', 'Do not reenter the account owner from the order port')
        state = copy.deepcopy(self._state)
        self._mark_unsynchronized(state)
        try:
            with self._journal.transaction():
                self._journal.event('DISCONNECTED', {'reason': reason})
                self._journal.save(state)
        except sqlite3.Error as error:
            self._failed = True
            raise TraderError('E_DSV_STORE', 'Connection loss could not be recorded; owner requires restart') from error
        self._state = state
        return {'synchronized': False, 'running': state['running']}

    def disable_entries_on_restart(self):
        self._check_owner()
        self.command({'id': 'sdk-startup-stop-' + uuid4().hex, 'action': 'STOP'})

    @staticmethod
    def _mark_unsynchronized(state):
        state['synchronized'] = False
        for order in state['orders'].values():
            order.pop('local_order_id', None)
        for row in state['symbols'].values():
            row.update(tick=None, tick_session=None)
            row.pop('quote_clock', None)

    def prepare_shutdown(self):
        """Report unresolved child identities and owned lots without issuing commands."""
        self._check_owner()
        orders = [order['child_order_id'] for order in self._state['orders'].values() if not terminal(order)]
        conflicts = [code for code, row in self._state['symbols'].items() if row.get('native_conflicts')]
        positions = {key: row['position']['owned'] for key, row in self._state['symbols'].items()
                     if row['position'] is not None and row['position']['owned']}
        for contract, row in self._state['symbols'].items():
            if detached := row.get('detached_positions'):
                positions[contract] = positions.get(contract, 0) + sum(
                    self._state['retired_positions'][key]['owned'] for key in detached)
        result = {'ready': not orders and not positions and not conflicts, 'unresolved_children': orders,
                  'owned_lots': positions}
        if conflicts:
            result['allocation_conflicts'] = conflicts
        return result

    def apply_round(self, frame):
        self._check_owner()
        try:
            self._validate_frame(frame)
            return self._run('ROUND', frame, self._evaluate)
        except (KeyError, TypeError, AttributeError) as error:
            raise TraderError('E_DSV_ROUND', 'Malformed offline round or native fact') from error

    def command(self, command):
        self._check_owner()
        try:
            return self._run('COMMAND', command, self._operate)
        except (KeyError, TypeError) as error:
            raise TraderError('E_DSV_COMMAND', 'Malformed offline command') from error

    def _run(self, kind, source, apply):
        if self._busy:
            raise TraderError('E_DSV_OWNER', 'Do not reenter the account owner from the order port')
        self._busy = True
        try:
            return self._commit(kind, source, apply)
        finally:
            self._busy = False

    def _commit(self, kind, source, apply):
        identity = identifier(source['id'])
        digest = content_hash(source)
        state = copy.deepcopy(self._state)
        try:
            with self._journal.transaction():
                prior = self._journal.receipt(kind, identity, digest)
                if prior is not None:
                    return prior
                before = content_hash(state)
                decisions, effects = apply(state, source)
                receipt = {'type': kind, 'input_id': identity, 'configuration_id': self._scope['configuration_id'],
                           'state_before_digest': before, 'state_after_digest': content_hash(state),
                           'decisions': decisions, 'effects': effects}
                if kind == 'ROUND':
                    receipt['evaluated_at'] = source['wall_time']
                    if 'trading_day' in source:
                        receipt['trading_day'] = state['trading_day']
                self._journal.event(kind, {'input': source, 'receipt': receipt})
                self._journal.save(state)
                self._journal.record_receipt(kind, identity, digest, receipt)
        except sqlite3.Error as error:
            self._failed = True
            raise TraderError('E_DSV_STORE', 'Required round or command transaction failed before dispatch') from error
        self._state = state
        try:
            self._controller.dispatch(state, effects)
        except BaseException:
            self._failed = True
            raise
        return receipt

    def _validate_frame(self, frame):
        identifier(frame['session_id'])
        quantity(frame['sequence'], positive=True)
        _utc(frame['wall_time'])
        if 'trading_day' in frame:
            _day(frame['trading_day'])
        if 'trading_days' in frame:
            days = frame['trading_days']
            if (type(days) is not list or len(days) > 512 or
                    any(_day(day) != day for day in days) or sorted(set(days)) != days):
                raise TraderError('E_DSV_SCOPE', 'Expected an ordered complete trading-day calendar')
        if 'synchronized' in frame and type(frame['synchronized']) is not bool:
            raise TraderError('E_DSV_ROUND', 'Synchronization status must be explicit')
        if 'sync_orders' in frame and (type(frame['sync_orders']) is not list or
                                       len(set(frame['sync_orders'])) != len(frame['sync_orders'])):
            raise TraderError('E_DSV_ROUND', 'Expected an ordered unique list of synchronized child IDs')
        mono = frame['monotonic_ms']
        if type(mono) not in (str, int) or not str(mono).isascii() or not str(mono).isdecimal() or len(str(mono)) > 19:
            raise TraderError('E_DSV_ROUND', 'A round needs explicit nonnegative monotonic milliseconds')
        if type(frame['market']) is not list or type(frame['native']) is not list:
            raise TraderError('E_DSV_ROUND', 'Expected ordered market and native lists')
        if len({row['contract_id'] for row in frame['market']}) != len(frame['market']):
            raise TraderError('E_DSV_ROUND', 'A round supplies one snapshot per configured contract')
        if {row['contract_id'] for row in frame['market']} != set(self._symbols):
            raise TraderError('E_DSV_SCOPE', 'A round must supply the configured contracts in this account')
        for market in frame['market']:
            for key in ('long_qty', 'short_qty'):
                quantity(market[key])
            if any(key in market for key in _BUCKETS):
                for key in _BUCKETS:
                    quantity(market[key])
                if any(market[f'{side}_today'] + market[f'{side}_historical'] != market[f'{side}_qty']
                       for side in ('long', 'short')):
                    raise TraderError('E_DSV_OWNERSHIP', 'Native position totals disagree with today and historical buckets')
            if quantity(market.get('min_order_qty', 1), positive=True) > quantity(
                    market.get('max_order_qty', 2_147_483_647), positive=True):
                raise TraderError('E_DSV_ROUND', 'Instrument order quantity bounds are inverted')
            if _utc(market['session_open']) >= _utc(market['session_close']):
                raise TraderError('E_DSV_ROUND', 'Invalid start and end of session')
            for key in ('entry_close', 'liquidation_at'):
                if market.get(key) is not None:
                    _utc(market[key])
            if type(market['opening_session']) is not bool or type(market['ticks']) is not list:
                raise TraderError('E_DSV_ROUND', 'Describe the opening session and ordered quote rows explicitly')
            if market.get('night_boundary') is not None:
                _utc(market['night_boundary'])
            for name, reading in market.get('wall_reads', {}).items():
                if name not in _CLOCK_READS:
                    raise TraderError('E_DSV_ROUND', 'Unrecognized rule-specific wall-clock reading')
                _utc(reading)
            for key in _FUNDS_FIELDS:
                if market.get(key) is not None:
                    decimal_value(market[key])
            if market.get('days_to_expiry') is not None:
                try:
                    expiry = int(market['days_to_expiry'])
                    if str(expiry) != str(market['days_to_expiry']):
                        raise ValueError()
                except (TypeError, ValueError) as error:
                    raise TraderError('E_DSV_ROUND', 'Expected signed natural days to expiry') from error
            if type(market.get('external_orders', False)) is not bool:
                raise TraderError('E_DSV_ROUND', 'External order presence must be explicit when supplied')
            for tick in market['ticks']:
                _utc(tick['native_time'])
                if tick['candle_id'] is not None and type(tick['candle_id']) is not str:
                    raise TraderError('E_DSV_ROUND', 'Expected the opaque minute-bar identity')
                if 'native_quote_stamp' in tick and (type(tick['native_quote_stamp']) is not str or not tick['native_quote_stamp']):
                    raise TraderError('E_DSV_ROUND', 'Expected the original SDK quote timestamp')
                for key in _PRICE_FIELDS:
                    if tick.get(key) not in (None, 'NaN'):
                        decimal_value(tick[key])
        for fact in frame['native']:
            if fact['kind'] not in {'ORDER', 'TRADE', 'LOCAL_ORDER'}:
                raise TraderError('E_DSV_ROUND', 'Unsupported native fact kind')
            if fact['kind'] == 'LOCAL_ORDER':
                from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
                port = self._controller.port
                if type(port) is not DsvSdkAdapter or not port.enabled:
                    raise TraderError('E_DSV_NATIVE_FACT', 'Local SDK orders require the active SDK owner')
            if fact['kind'] == 'TRADE':
                decimal_value(fact['price'], positive=True)
            if 'source_time_ns' in fact:
                stamp = fact['source_time_ns']
                if (type(stamp) is not str or not stamp.isascii() or not stamp.isdecimal() or len(stamp) > 19
                        or stamp.startswith('0') or int(stamp) > 9_223_372_036_854_775_807):
                    raise TraderError('E_DSV_NATIVE_FACT', 'Expected a positive canonical nanosecond timestamp')
            for key in ('trading_day', 'fill_day'):
                if key in fact:
                    _day(fact[key])
            if any(key in fact and fact[key] != self._scope[key] for key in ('account_key', 'provider')):
                raise TraderError('E_DSV_SCOPE', 'Native facts must match the account, provider and cabinet day')

    def _evaluate(self, state, frame):
        mono = str(frame['monotonic_ms'])
        day = frame.get('trading_day', state['trading_day'])
        if 'trading_days' in frame and day not in frame['trading_days']:
            raise TraderError('E_DSV_SCOPE', 'Trading-day calendar must include the observed cabinet day')
        if frame['sequence'] != state['sequence'] + 1:
            raise TraderError('E_DSV_ROUND_SEQUENCE', 'Expected the next wait_update round')
        if (day == state['trading_day'] and state['session_id'] == frame['session_id'] and state['monotonic_ms'] is not None
                and int(mono) < int(state['monotonic_ms'])):
            raise TraderError('E_DSV_ROUND_SEQUENCE', 'Monotonic clock moved backward inside one session')
        old_day = state['trading_day']
        if day < old_day:
            raise TraderError('E_DSV_SCOPE', 'Observed cabinet day cannot move backward')
        if day != old_day and frame.get('synchronized') is not True:
            raise TraderError('E_DSV_SCOPE', 'Day rollover requires an explicit cabinet-day snapshot')
        # Source monthly observation precedes per-symbol trading-day reset.
        # Evaluate saved loss/day first, so the last round's loss is not lost.
        for contract, spec in self._symbols.items():
            row = state['symbols'][contract]
            stops = observe_month_stop(
                old_day, loss=row['net_loss'], stop_offset=spec['stop_offset'], trade=self._trade,
                history=row.get('stop_days', []), month_stop=row.get('month_stop'),
                day_stopped=row['day_stop'], day_stopped_reason=row.get('day_stop_reason'),
                calendar=frame.get('trading_days', []))
            row.update(stop_days=stops['stop_days'], month_stop=stops['month_stop'],
                       day_stop=stops['day_stopped'], day_stop_reason=stops['day_stopped_reason'])
        if day != old_day:
            self._roll_day(state, day)
            if self._sdk_port is not None:
                state['running'] = False
        needs_sync = not state['synchronized']
        pending = {child for child, order in state['orders'].items() if not terminal(order)} if needs_sync else set()
        self._controller.accept_native(state, frame['native'])
        if frame.get('synchronized') is False:
            self._mark_unsynchronized(state)
        if needs_sync and frame.get('synchronized') is True:
            self._synchronize(state, frame, pending)
        state.update(sequence=frame['sequence'], monotonic_ms=mono, session_id=frame['session_id'])
        if self._sdk_port is not None and not self._sdk_port.opening_window(day, _utc(frame['wall_time'])):
            state['running'] = False
            for row in state['symbols'].values():
                if position := row['position']:
                    position.update(entry_stopped=True, preserve_open_on_stop=False)
        market_rows = {market['contract_id']: market for market in frame['market']}
        decisions, effects = [], []
        for contract, spec in self._symbols.items():
            row = state['symbols'][contract]
            market = market_rows[contract]
            if not state['synchronized']:
                decisions.append({'contract_id': contract, 'decision_id': f"{frame['id']}:{contract}:0",
                                  'reason': 'AWAITING_NATIVE_SYNC', 'rule_ids': ['I01', 'I02']})
                continue
            self._observe_market(row, market, frame)
            tick = row['tick'] if row['tick_session'] == frame['session_id'] else None
            if (tick is not None and market.get('native_coverage') is True and
                    not _utc(market['session_open']) <= _utc(tick['native_time']) < _utc(market['session_close'])):
                tick = None
            observed = market | {'tick': tick}
            values = self._evaluate_symbol(state, row, observed, frame, spec)
            self._update_stops(row, contract, day, spec, frame.get('trading_days', []))
            decisions.extend({'contract_id': contract, 'decision_id': f"{frame['id']}:{contract}:{i}",
                              **value} for i, value in enumerate(values))
            if self._in_session(frame, observed):
                now = _utc(observed.get('wall_reads', {}).get('pending', frame['wall_time'])).timestamp()
                if tick is None:
                    self._controller.settle(state, row)
                else:
                    gap = row['position'] and row['position'].get('gap_exit')
                    if gap and not self._fresh_after_gap(observed, gap):
                        observed = observed | {'allow_new_order': False}
                    effects.extend(self._controller.plan(state, row, observed, now))
            else:
                if self._sdk_port is not None:
                    position = row['position']
                    if position is not None and position['entry_stopped'] and not position.get('preserve_open_on_stop'):
                        for order in self._controller.child_orders(state, position, 'OPEN'):
                            if request := self._controller.cancel(order, 'TARGET_REDUCED'):
                                effects.append(request)
                self._controller.settle(state, row)
        return decisions, effects

    def _update_stops(self, row, contract, day, spec, calendar):
        result = reduce_stops(
            contract, day, loss=Decimal(row['net_loss']), stop_streak=row.get('stop_streak', 0),
            continuation_stopped=row.get('continuation_stopped', False), day_stopped=row['day_stop'],
            day_stopped_reason=row.get('day_stop_reason'), history=row.get('stop_days', []),
            month_stop=row.get('month_stop'), calendar=calendar, stop_offset=spec['stop_offset'],
            trade=self._trade | {'month_stop_days': 0})
        # Monthly tracking already ran at the source's earlier phase. The
        # legacy reducer remains unchanged for the not-yet-retired Kernel.
        # Do not overwrite a stop cause that was established before this phase.
        if row['day_stop']:
            result['state']['day_stopped_reason'] = row.get('day_stop_reason')
        stops = result['state']
        row.update(day_stop=stops['day_stopped'], day_stop_reason=stops['day_stopped_reason'],
                   stop_days=stops['stop_days'], month_stop=stops['month_stop'])

    @staticmethod
    def _fresh_after_gap(market, gap):
        native = _utc(market['tick']['native_time'])
        previous = gap['last_native_time']
        return (_utc(market['session_open']) <= native < _utc(market['session_close'])
                and (previous is None or native > _utc(previous)))

    def _roll_day(self, state, day):
        self._mark_unsynchronized(state)
        state['trading_day'] = day
        for row in state['symbols'].values():
            position, last_stop = row['position'], row['last_stop']
            history = row.get('stop_days', [])
            month = row.get('month_stop')
            reviews = {key: row[key] for key in ('detached_positions', 'native_conflicts') if row.get(key)}
            if 'expiry_days_checked' in row:
                reviews['expiry_days_checked'] = row['expiry_days_checked']
            row.clear()
            row.update(_initial_symbol(), position=position, last_stop=last_stop, month_stop=month, stop_days=history, **reviews)
            if position is not None:
                position.update(entry_stopped=True, preserve_open_on_stop=False)
                for key in ('close_started', 'close_refresh', 'close_mode'):
                    position.pop(key, None)

    def _observe_market(self, row, market, frame):
        session, opening = frame['session_id'], market['session_open']
        wall = _utc(market.get('wall_reads', {}).get('stale', frame['wall_time']))
        sample = {'wall': wall.isoformat(), 'mono': str(frame['monotonic_ms'])}
        clock = row.get('quote_clock')
        if clock is None or (clock['session'], clock['opening']) != (session, opening):
            clock = {'session': session, 'opening': opening, 'native_time': None, 'anchor': sample}
            if market.get('native_coverage') is True:
                row.update(tick=None, tick_session=None)
        previous = clock.get('previous')
        if previous is not None:
            wall_gap = (wall - datetime.fromisoformat(previous['wall'])).total_seconds()
            mono_gap = (int(sample['mono']) - int(previous['mono'])) / 1000
            if wall_gap < 0 or wall_gap > 300 and 0 < mono_gap < wall_gap * 0.5:
                clock['anchor'] = sample
        clock['previous'] = sample
        row['quote_clock'] = clock
        for tick in market['ticks']:
            identity = tick.get('native_quote_stamp', tick['native_time'])
            if identity != clock.get('quote_identity', clock['native_time']):
                clock.update(native_time=tick['native_time'], quote_identity=identity, anchor=sample)
            if is_night_session(_utc(opening)):
                self._observe_night(row, market, tick)
        if market['ticks']:
            row.update(tick=copy.deepcopy(market['ticks'][-1]), tick_session=session)
        if boundary := market.get('night_boundary'):
            night = row.get('night_reference')
            if (night is None or night['native_time'] != boundary or not _utc(boundary) < _utc(opening)
                    or is_night_session(_utc(opening))):
                raise TraderError('E_DSV_NIGHT_BOUNDARY', 'Night boundary must match the observed same-day tail')
            night['complete'] = True

    @staticmethod
    def _observe_night(row, market, tick):
        time = tick['native_time']
        if not _utc(market['session_open']) <= _utc(time) < _utc(market['session_close']):
            return
        last, high, low = (tick.get(key) for key in ('last', 'day_high', 'day_low'))
        if any(price_value(value) is None for value in (last, high, low)):
            return
        if not Decimal(low) <= Decimal(last) <= Decimal(high):
            return
        night = row.get('night_reference')
        if night is None:
            row['night_reference'] = {'native_time': time, 'high': high, 'low': low, 'last': last, 'complete': False}
        elif not night['complete']:
            if _utc(time) >= _utc(night['native_time']):
                night.update(native_time=time, last=last)
            night['high'] = decimal_text(max(Decimal(high), Decimal(night['high'])))
            night['low'] = decimal_text(min(Decimal(low), Decimal(night['low'])))

    def _synchronize(self, state, frame, pending):
        seen = {fact['child_order_id'] for fact in frame['native'] if fact['kind'] == 'ORDER'}
        claimed = set(frame.get('sync_orders', []))
        if not claimed <= seen:
            raise TraderError('E_DSV_SYNC', 'Synchronization lists an order without an original snapshot in this round')
        if any(child not in claimed and not terminal(state['orders'][child]) for child in pending):
            return
        for market in frame['market']:
            row = state['symbols'][market['contract_id']]
            position = row['position']
            if row.get('detached_positions') or row.get('native_conflicts'):
                continue
            if position is not None and not self._controller.proves_position(state, position, market):
                return
        state['synchronized'] = True

    @staticmethod
    def _in_session(frame, market):
        now = _utc(market.get('wall_reads', {}).get('session', frame['wall_time']))
        return _utc(market['session_open']) <= now < _utc(market['session_close'])

    def _evaluate_symbol(self, state, row, market, frame, spec):
        position, tick = row['position'], market['tick']
        if row.get('detached_positions') or row.get('native_conflicts'):
            return [{'reason': 'LATE_FILL_OWNERSHIP_REVIEW', 'rule_ids': ['I01', 'I02']}]
        if position is not None:
            if 'close_request_at' in position:
                wait = self._synchronize_protection_exit(state, position, market, frame, spec)
                if wait is not None:
                    return [wait]
            return self._maintain_position(row, position, market, frame, spec)
        if any(order['contract_id'] == market['contract_id'] and not terminal(order) for order in state['orders'].values()):
            return [{'reason': 'UNRESOLVED_PRIOR_CHILD', 'rule_ids': ['I02']}]
        if not state['running']:
            return [{'reason': 'USER_STOPPED', 'rule_ids': ['D14']}]
        if tick is None:
            return [{'reason': 'QUOTE_UNAVAILABLE', 'rule_ids': ['D01']}]
        if market['long_qty'] or market['short_qty'] or market.get('external_orders'):
            return [{'reason': 'UNOWNED_POSITION', 'rule_ids': ['I01', 'I02']}]
        if self._sdk_port is not None:
            reason = self._sdk_port.opening_limit(state, market, spec, _utc(frame['wall_time']))
            if reason:
                return [{'reason': reason, 'rule_ids': ['I02']}]
        rows = tuple(state['symbols'].values())
        active = sum(not candidate['day_stop'] for candidate in rows)
        # Source counts established strategy directions, independent of requested quantity.
        opened = sum(bool(candidate['position'] and candidate['position']['owned']) for candidate in rows)
        sizes = {'active_symbols': max(1, active), 'open_symbols': opened}
        decision = continuation_entry(row, frame, market, spec, self._trade, **sizes)
        if decision is not None:
            row['continuation_attempted'] = True
            rules = ['D05', 'D07']
        else:
            if tick['candle_id'] is None:
                return [{'reason': 'MINUTE_BAR_UNAVAILABLE', 'rule_ids': ['D03']}]
            decision = ordinary_entry(row, frame, market, spec, self._trade, **sizes)
            rules = ['D01', 'D02', 'D04', 'D05', 'D06']
        if 'expiry_days_checked' in decision:
            row['expiry_days_checked'] = decision['expiry_days_checked']
        if 'consume' in decision:
            row['last_signal_bar'] = decision['consume']
        if decision.get('day_stop'):
            row['day_stop'] = True
            if decision.get('day_stop_reason'):
                row['day_stop_reason'] = decision['day_stop_reason']
                rules.append('D03')
        elif decision['reason'] == 'HYSTERESIS_NOT_READY':
            rules.append('D03')
        if 'target' in decision:
            goal = decision['target']
            row.update(last_order_ms=goal['order_ms'], last_order_direction=goal['direction'])
            row['target_count'] += 1
            if self._sdk_port is not None:
                state['sdk_scope_targets'] = state.get('sdk_scope_targets', 0) + 1
            row['position'] = goal | {'id': 'position-' + uuid4().hex, 'owned': 0, 'entry_filled': 0,
                                      'entry_notional': '0', 'children': [], 'entry_stopped': False,
                                      'peak': '0', 'protected': False, 'half_stage': 0, 'exit_target': None,
                                      'open_day': state['trading_day'], 'fills': [],
                                      'entry_kind': decision['reason'], 'contract_id': market['contract_id'],
                                      'multiplier': market.get('multiplier')}
            if self._sdk_port is not None:
                row['position']['sdk_multiplier'] = market['multiplier']
        decisions = []
        if decision.pop('geometry_block', False):
            decisions.append({'reason': 'GEOMETRY_BLOCK', 'rule_ids': ['D02']})
        decisions.append(decision | {'rule_ids': rules})
        return decisions

    def _synchronize_protection_exit(self, state, position, market, frame, spec):
        """Keep the strategy exit pending while preserving the native flat facts."""
        reads = market.get('wall_reads', {})
        sync_time = _utc(reads.get('position_sync', frame['wall_time']))
        elapsed = (sync_time - _utc(position['close_request_at'])).total_seconds()
        native_flat = not market['long_qty'] and not market['short_qty'] and not market.get('external_orders')
        children = self._controller.child_orders(state, position)
        # _run line 6236 guards the account read for five seconds. The
        # position mismatch (booked side versus native flat) takes the
        # reconciliation branch regardless of POSITION_ACT_GRACE.
        if (elapsed >= 5 and native_flat
                and not position['owned'] and all(terminal(order) for order in children)):
            position.pop('close_request_at')
            return {'reason': 'PROTECTION_EXIT_SYNCHRONIZED', 'rule_ids': ['D08', 'I02']}
        if market['tick'] is not None and self._in_session(frame, market):
            # The original may renew the same zero target before its account
            # sync is due. Recheck the trigger for timing without booking or
            # sending another target.
            original_position = position | {'owned': position['entry_filled'], 'exit_target': None}
            _, events = protect(original_position, market['tick'], self._trade, spec)
            if any(event.get('remaining') == 0 for event in events):
                position['close_request_at'] = reads.get('protection', frame['wall_time'])
        if not position['owned']:
            return {'reason': 'PROTECTION_EXIT_AWAITING_SYNC', 'rule_ids': ['D08', 'I02']}
        return None

    def _maintain_position(self, row, position, market, frame, spec):
        if market['tick'] is not None and position['owned'] and self._in_session(frame, market):
            close = self._scheduled_close(row, position, market, frame)
            if close is not None:
                return [close]
        if position['entry_filled'] and not position.get('entry_slip_checked'):
            position['entry_slip_checked'] = True
            if self._trade['entry_slip_abort'] and position['exit_target'] != 0:
                actual = float(position['entry_notional']) / position['entry_filled']
                theory = float(position['theory'])
                slip = actual - theory if position['direction'] == 'LONG' else theory - actual
                cap = max(2.0, float(position['stop']) * float(self._trade['entry_slip_abort_ratio']))
                if slip > cap:
                    tighten_target(position, 0)
                    position['rollback_exit'] = {'reason': 'ADVERSE_SLIPPAGE', 'at': frame['wall_time'],
                                                 'price': None, 'slip': str(slip)}
                    row['net_loss'] = decimal_text(Decimal(row['net_loss']) + Decimal(str(slip)))
                    if self._trade['entry_slip_abort_stop_day']:
                        row.update(day_stop=True, day_stop_reason='ADVERSE_SLIPPAGE')
                    return [{'reason': 'ADVERSE_SLIPPAGE', 'remaining': 0, 'slip': str(slip), 'rule_ids': ['D04', 'D08']}]
        if position.get('gap_exit'):
            return [{'reason': 'DATA_GAP_EXIT_PENDING', 'remaining': 0, 'rule_ids': ['D10', 'I01']}]
        if self._stale_position(row, position, market, frame):
            clock = row['quote_clock']
            position['gap_exit'] = {'last_native_time': clock['native_time'], 'day_stop': self._trade['stale_close_day_stop']}
            position['rollback_exit'] = {'reason': 'DATA_GAP_EXIT', 'at': frame['wall_time'], 'price': None, 'slip': None}
            tighten_target(position, 0)
            return [{'reason': 'DATA_GAP_EXIT', 'remaining': 0, 'rule_ids': ['D10', 'I01']}]
        now = _utc(market.get('wall_reads', {}).get('pending', frame['wall_time'])).timestamp()
        elapsed = now - float(position['submitted_at'])
        if not position['entry_stopped'] and not position['entry_filled'] and elapsed > self._trade['entry_confirm_timeout']:
            position['entry_stopped'] = True
            return [{'reason': 'ENTRY_CONFIRM_TIMEOUT', 'rule_ids': ['D12']}]
        if position['mode'] == 'PASSIVE' and not position['entry_filled'] and elapsed >= float(self._trade.get('passive_wait_seconds', '3')):
            position.update(mode='CAP', context_at=str(now))
        if market['tick'] is None or not self._in_session(frame, market):
            return [{'reason': 'POSITION_WAITING_FOR_MARKET', 'rule_ids': ['D06']}]
        previous_target = position['exit_target']
        position, results = protect(position, market['tick'], self._trade, spec)
        row['position'] = position
        if position['exit_target'] != previous_target:
            position['exit_revision'] = position.get('exit_revision', 0) + 1
        for result in results:
            reason = result['reason']
            if 'remaining' in result:
                position['entry_stopped'] = True
            if reason == 'PROTECTION_ACTIVATED' and self._trade['protect_stop_day']:
                row['day_stop'] = True
            if reason == 'TRAILING_STOP' and self._trade['stop_day_on_trailing']:
                row['day_stop'] = True
            if reason == 'STOP_LOSS':
                row['stop_streak'] = row.get('stop_streak', 0) + 1
                if position.get('entry_kind') == 'CONTINUATION_ENTRY' and self._trade['cont_stop_day_stop']:
                    row.update(continuation_stopped=True, day_stop=True)
                cooldown = float(self._trade['stop_cooldown_minutes']) * 60
                stop_at = market.get('wall_reads', {}).get('protection', frame['wall_time'])
                protection_now = _utc(stop_at).timestamp()
                row['last_stop'] = {'direction': position['direction'], 'theory': position['theory'],
                                    'until': str(protection_now + cooldown)
                                    if self._trade['stop_cooldown_enabled'] and cooldown > 0 else None}
            if result.get('remaining') == 0:
                reference, nominal_move = market['tick']['last'], result['move']
                if reason == 'PROTECTED_FLOOR':
                    # DSV _run 7733-7735 books this exit at the protection line,
                    # not the quote that may have jumped through it. Keep source
                    # float operation order; native fills and prices are untouched.
                    theory = float(position['theory'])
                    distance = float(position['profit']) * (float(self._trade['protect_keep_ratio']) or 0.5)
                    level = theory + distance if position['direction'] == 'LONG' else theory - distance
                    reference = decimal_text(Decimal(str(level)))
                    nominal_move = str(level - theory if position['direction'] == 'LONG' else theory - level)
                result.update(nominal_reference_price=reference, nominal_move=nominal_move)
                position['rollback_exit'] = {'reason': reason,
                                             'at': market.get('wall_reads', {}).get('protection', frame['wall_time']),
                                             'price': reference, 'slip': None}
                position['close_request_at'] = position['rollback_exit']['at']
                if reason in {'TAKE_PROFIT', 'TRAILING_STOP', 'PROTECTED_FLOOR'}:
                    row['stop_streak'] = 0
                loss = (Decimal(position['stop']) if reason == 'STOP_LOSS' else
                        -Decimal(position['profit']) if reason == 'TAKE_PROFIT' else -Decimal(nominal_move))
                row['net_loss'] = decimal_text(Decimal(row['net_loss']) + loss)
                # Frozen _run 7825-7830: ordinary take-profit, not trailing
                # or the 80% floor; do not overwrite an existing day-stop cause.
                if (reason == 'TAKE_PROFIT' and self._trade.get('stop_day_on_profit', True)
                        and not row['day_stop']):
                    row.update(day_stop=True, day_stop_reason='止盈触发')
        return [result | {'rule_ids': ['D08', 'D09', 'D10']} for result in results] or [
            {'reason': 'POSITION_OR_ENTRY_PENDING', 'rule_ids': ['D08', 'D12']}]

    def _stale_position(self, row, position, market, frame):
        limit = Decimal(self._trade['stale_force_close_seconds'])
        if not position['owned'] or not limit or not self._in_session(frame, market) or position['exit_target'] == 0:
            return False
        clock = row['quote_clock']
        anchor = clock['anchor']
        wall = datetime.fromisoformat(clock['previous']['wall'])
        start = _utc(market['session_open'])
        quiet = (wall - max(start, datetime.fromisoformat(anchor['wall']))).total_seconds()
        elapsed = Decimal(int(frame['monotonic_ms']) - int(anchor['mono'])) / 1000
        return (wall - start).total_seconds() > 60 and quiet > limit and elapsed > limit

    def _scheduled_close(self, row, position, market, frame):
        end = market.get('liquidation_at')
        if end is None or (position['exit_target'] == 0 and 'close_started' not in position):
            return None
        end = _utc(end)
        wall = _utc(market.get('wall_reads', {}).get('close', frame['wall_time']))
        native = _utc(market['tick']['native_time'])
        window = self._trade['close_before_seconds']
        if not any(0 < (end - moment).total_seconds() <= window for moment in (wall, native)):
            return None
        now = wall.timestamp()
        first = 'close_started' not in position
        if first:
            position['close_started'] = str(now)
            position['rollback_exit'] = {'reason': 'SESSION_CLOSE',
                                         'at': market.get('wall_reads', {}).get('close', frame['wall_time']),
                                         'price': market['tick']['last'], 'slip': None}
            if is_night_session(_utc(market['session_open'])) and row.get('night_reference'):
                row['night_reference']['fallback'] = position['direction']
        if first or now - float(position['close_refresh']) >= 3:
            remaining_ms = window * 1000 - int((now - float(position['close_started'])) * 1000)
            position.update(close_mode=scheduled_close_mode(remaining_ms), close_refresh=str(now))
        tighten_target(position, 0)
        if first:
            last = price_value(market['tick'].get('last'))
            if last is not None:
                movement = Decimal(str(last)) - Decimal(position['theory'])
                if position['direction'] == 'SHORT':
                    movement = -movement
                row['net_loss'] = decimal_text(Decimal(row['net_loss']) - movement)
        return {'reason': 'SCHEDULED_CLOSE' if first else 'SCHEDULED_CLOSE_PENDING',
                'remaining': 0, 'price_mode': position['close_mode'], 'rule_ids': ['D06', 'D11']}

    def _operate(self, state, command):
        action = command['action']
        if action in {'START', 'STOP'}:
            if action == 'START' and self._sdk_port is not None:
                self._sdk_port.check_start(state)
            state['running'] = action == 'START'
            effects = []
            if action == 'STOP':
                for row in state['symbols'].values():
                    if row['position'] is not None:
                        row['position']['entry_stopped'] = True
                        row['position']['preserve_open_on_stop'] = not self._trade['cancel_on_stop']
                        if state['synchronized'] and not row['position']['preserve_open_on_stop']:
                            for order in self._controller.child_orders(state, row['position'], 'OPEN'):
                                request = self._controller.cancel(order, 'USER_STOP')
                                if request:
                                    effects.append(request)
            return [{'reason': action, 'rule_ids': ['D14']}], effects
        if action == 'CANCEL':
            if not state['synchronized']:
                raise TraderError('E_DSV_SYNC', 'Wait for a synchronized order and position snapshot before cancelling')
            order = state['orders'].get(command['child_order_id'])
            if order is None:
                raise TraderError('E_DSV_NATIVE_ID', 'Manual cancellation requires an owned child')
            request = self._controller.cancel(order, 'USER_CANCEL')
            return [{'reason': 'USER_CANCEL' if request else 'CANCEL_UNAVAILABLE', 'rule_ids': ['I01', 'I02']}], [request] if request else []
        if action == 'REDUCE':
            row = state['symbols'].get(command['contract_id'])
            position = row['position'] if row else None
            qty = quantity(command['quantity'], positive=True)
            if position is None or qty > position['owned']:
                raise TraderError('E_DSV_OWNERSHIP', 'Manual reduction exceeds the owned position')
            if row.get('detached_positions') or row.get('native_conflicts'):
                raise TraderError('E_DSV_OWNERSHIP', 'Late fill from another cycle requires ownership review')
            target = position['owned'] - qty
            if position['exit_target'] is not None:
                target = min(target, position['exit_target'])
            tighten_target(position, target, new_request=True)
            if target == 0 and not position.get('rollback_exit'):
                position['rollback_exit'] = {'reason': 'MANUAL_EXIT', 'at': None, 'price': None, 'slip': None}
            return [{'reason': 'USER_REDUCE', 'remaining': target, 'rule_ids': ['I01', 'I02']}], []
        if action == 'REDUCE_DETACHED':
            row = state['symbols'].get(command['contract_id'])
            identity = identifier(command['position_id'])
            qty = quantity(command['quantity'], positive=True)
            if (not state['synchronized'] or row is None or row.get('native_conflicts') or
                    identity not in row.get('detached_positions', [])):
                raise TraderError('E_DSV_OWNERSHIP', 'A reduction review requires a synchronized, identified detached cycle')
            position = state['retired_positions'][identity]
            if qty > position['owned']:
                raise TraderError('E_DSV_OWNERSHIP', 'Reviewed reduction exceeds the original owned cycle')
            target = min(position.get('reviewed_exit_target', position['owned']), position['owned'] - qty)
            position['reviewed_exit_target'] = target
            tighten_target(position, target, new_request=True)
            return [{'reason': 'USER_REDUCE_DETACHED', 'position_id': identity, 'remaining': target,
                     'rule_ids': ['I01', 'I02']}], []
        raise TraderError('E_DSV_COMMAND', 'Unsupported offline command')
