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
from online_trader.dsv_core.transition import ordinary_entry, protect
from online_trader.dsv_core.price_rules import scheduled_close_mode
from online_trader.dsv_runtime.controller import ExplicitOrderController, terminal, tighten_target
from online_trader.dsv_storage.journal import Journal
from online_trader.errors import TraderError
from online_trader.gateway.ports import OnlineSimulationPort
from online_trader.runtime.instance import AccountLock


_CLOCK_READS = {'session', 'opening', 'entry_close', 'quote_age', 'cooldown', 'chase', 'submit',
                'close', 'pending', 'protection', 'pending_start', 'price_context'}
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
            'net_loss': '0', 'day_stop': False, 'last_stop': None, 'tick': None, 'tick_session': None}


class DsvSession:
    """Public synchronous offline runtime. Pass complete scoped snapshots and ordered native changes."""

    def __init__(self, database, *, account_key, provider, trading_day, profile, port):
        if (not identifier(account_key).startswith('DEV:') or provider != 'OFFLINE' or
                isinstance(port, OnlineSimulationPort)):
            raise TraderError('E_DSV_SCOPE', 'The replacement runner only accepts an offline development account and port')
        if not isinstance(profile, DsvProfile):
            raise TraderError('E_DSV_SCOPE', 'Expected a normalized DSV business profile')
        _day(trading_day)
        self._owner = get_ident()
        self._closed = False
        self._failed = False
        self._busy = False
        self._lock = AccountLock(account_key)
        self._scope = {'account_key': account_key, 'provider': provider, 'trading_day': trading_day,
                       'configuration_id': profile.content_hash}
        settings = profile.to_record()['settings']
        self._trade = settings['trade']
        self._symbols = {item['code']: item for item in settings['symbols']}
        initial = {'running': False, 'sequence': 0, 'monotonic_ms': None, 'session_id': None,
                   'trading_day': trading_day, 'synchronized': True, 'symbols': {key: _initial_symbol() for key in self._symbols},
                   'orders': {}, 'retired_positions': {}}
        try:
            self._journal = Journal(database, self._scope, initial)
            self._state = self._journal.load()
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
            self._lock.close()
            raise

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
            self._journal.close()
        finally:
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
        state.pop('retired_positions', None)
        return state

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

    @staticmethod
    def _mark_unsynchronized(state):
        state['synchronized'] = False
        for row in state['symbols'].values():
            row.update(tick=None, tick_session=None)

    def prepare_shutdown(self):
        """Report unresolved child identities and owned lots without issuing commands."""
        self._check_owner()
        orders = [order['child_order_id'] for order in self._state['orders'].values() if not terminal(order)]
        positions = {key: row['position']['owned'] for key, row in self._state['symbols'].items()
                     if row['position'] is not None and row['position']['owned']}
        for contract, row in self._state['symbols'].items():
            if detached := row.get('detached_positions'):
                positions[contract] = positions.get(contract, 0) + sum(
                    self._state['retired_positions'][key]['owned'] for key in detached)
        return {'ready': not orders and not positions, 'unresolved_children': orders, 'owned_lots': positions}

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
                if type(tick['candle_id']) is not str:
                    raise TraderError('E_DSV_ROUND', 'Expected the opaque minute-bar identity')
                for key in _PRICE_FIELDS:
                    if tick.get(key) not in (None, 'NaN'):
                        decimal_value(tick[key])
        for fact in frame['native']:
            if fact['kind'] not in {'ORDER', 'TRADE'}:
                raise TraderError('E_DSV_ROUND', 'Unsupported native fact kind')
            if fact['kind'] == 'TRADE':
                decimal_value(fact['price'], positive=True)
            for key in ('trading_day', 'fill_day'):
                if key in fact:
                    _day(fact[key])
            if any(key in fact and fact[key] != self._scope[key] for key in ('account_key', 'provider')):
                raise TraderError('E_DSV_SCOPE', 'Native facts must match the account, provider and cabinet day')

    def _evaluate(self, state, frame):
        mono = str(frame['monotonic_ms'])
        day = frame.get('trading_day', state['trading_day'])
        if frame['sequence'] != state['sequence'] + 1:
            raise TraderError('E_DSV_ROUND_SEQUENCE', 'Expected the next wait_update round')
        if (day == state['trading_day'] and state['session_id'] == frame['session_id'] and state['monotonic_ms'] is not None
                and int(mono) < int(state['monotonic_ms'])):
            raise TraderError('E_DSV_ROUND_SEQUENCE', 'Monotonic clock moved backward inside one session')
        old_day = state['trading_day']
        if day < old_day:
            raise TraderError('E_DSV_SCOPE', 'Observed cabinet day cannot move backward')
        if day != old_day:
            if frame.get('synchronized') is not True:
                raise TraderError('E_DSV_SCOPE', 'Day rollover requires an explicit cabinet-day snapshot')
            self._roll_day(state, day)
        needs_sync = not state['synchronized']
        pending = {child for child, order in state['orders'].items() if not terminal(order)} if needs_sync else set()
        self._controller.accept_native(state, frame['native'])
        if frame.get('synchronized') is False:
            self._mark_unsynchronized(state)
        if needs_sync and frame.get('synchronized') is True:
            self._synchronize(state, frame, pending)
        state.update(sequence=frame['sequence'], monotonic_ms=mono, session_id=frame['session_id'])
        market_rows = {market['contract_id']: market for market in frame['market']}
        decisions, effects = [], []
        for contract, spec in self._symbols.items():
            row = state['symbols'][contract]
            market = market_rows[contract]
            if market['ticks'] and state['synchronized']:
                row.update(tick=copy.deepcopy(market['ticks'][-1]), tick_session=frame['session_id'])
            if not state['synchronized']:
                decisions.append({'contract_id': contract, 'decision_id': f"{frame['id']}:{contract}:0",
                                  'reason': 'AWAITING_NATIVE_SYNC', 'rule_ids': ['I01', 'I02']})
                continue
            tick = row['tick'] if row['tick_session'] == frame['session_id'] else None
            observed = market | {'tick': tick}
            values = self._evaluate_symbol(state, row, observed, frame, spec)
            decisions.extend({'contract_id': contract, 'decision_id': f"{frame['id']}:{contract}:{i}",
                              **value} for i, value in enumerate(values))
            if self._in_session(frame, observed):
                now = _utc(observed.get('wall_reads', {}).get('pending', frame['wall_time'])).timestamp()
                if tick is None:
                    self._controller.settle(state, row)
                else:
                    effects.extend(self._controller.plan(state, row, observed, now))
            else:
                self._controller.settle(state, row)
        return decisions, effects

    def _roll_day(self, state, day):
        self._mark_unsynchronized(state)
        state['trading_day'] = day
        for row in state['symbols'].values():
            position, last_stop = row['position'], row['last_stop']
            row.update(_initial_symbol(), position=position, last_stop=last_stop)
            if position is not None:
                position.update(entry_stopped=True, preserve_open_on_stop=False)
                for key in ('close_started', 'close_refresh', 'close_mode'):
                    position.pop(key, None)

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
            if row.get('detached_positions'):
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
        if row.get('detached_positions'):
            return [{'reason': 'LATE_FILL_OWNERSHIP_REVIEW', 'rule_ids': ['I01', 'I02']}]
        if position is not None:
            return self._maintain_position(row, position, market, frame, spec)
        if any(order['contract_id'] == market['contract_id'] and not terminal(order) for order in state['orders'].values()):
            return [{'reason': 'UNRESOLVED_PRIOR_CHILD', 'rule_ids': ['I02']}]
        if not state['running']:
            return [{'reason': 'USER_STOPPED', 'rule_ids': ['D14']}]
        if tick is None:
            return [{'reason': 'QUOTE_UNAVAILABLE', 'rule_ids': ['D01']}]
        if market['long_qty'] or market['short_qty'] or market.get('external_orders'):
            return [{'reason': 'UNOWNED_POSITION', 'rule_ids': ['I01', 'I02']}]
        rows = tuple(state['symbols'].values())
        active = sum(not candidate['day_stop'] for candidate in rows)
        # Source counts established strategy directions, independent of requested quantity.
        opened = sum(bool(candidate['position'] and candidate['position']['owned']) for candidate in rows)
        decision = ordinary_entry(row, frame, market, spec, self._trade, active_symbols=max(1, active), open_symbols=opened)
        if 'consume' in decision:
            row['last_signal_bar'] = decision['consume']
        if decision.get('day_stop'):
            row['day_stop'] = True
        if 'target' in decision:
            goal = decision['target']
            row.update(last_order_ms=goal['order_ms'], last_order_direction=goal['direction'])
            row['target_count'] += 1
            row['position'] = goal | {'id': 'position-' + uuid4().hex, 'owned': 0, 'entry_filled': 0,
                                      'entry_notional': '0', 'children': [], 'entry_stopped': False,
                                      'peak': '0', 'protected': False, 'half_stage': 0, 'exit_target': None,
                                      'open_day': state['trading_day'], 'fills': []}
        return [decision | {'rule_ids': ['D01', 'D02', 'D04', 'D05', 'D06']}]

    def _maintain_position(self, row, position, market, frame, spec):
        if market['tick'] is not None and position['owned'] and self._in_session(frame, market):
            close = self._scheduled_close(row, position, market, frame)
            if close is not None:
                return [close]
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
                cooldown = float(self._trade['stop_cooldown_minutes']) * 60
                protection_now = _utc(market.get('wall_reads', {}).get('protection', frame['wall_time'])).timestamp()
                row['last_stop'] = {'direction': position['direction'], 'theory': position['theory'],
                                    'until': str(protection_now + cooldown) if cooldown > 0 else None}
            if result.get('remaining') == 0:
                loss = (Decimal(position['stop']) if reason == 'STOP_LOSS' else
                        -Decimal(position['profit']) if reason == 'TAKE_PROFIT' else -Decimal(result['move']))
                row['net_loss'] = decimal_text(Decimal(row['net_loss']) + loss)
        return [result | {'rule_ids': ['D08', 'D09', 'D10']} for result in results] or [
            {'reason': 'POSITION_OR_ENTRY_PENDING', 'rule_ids': ['D08', 'D12']}]

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
        if first or now - float(position['close_refresh']) >= 3:
            remaining_ms = window * 1000 - int((now - float(position['close_started'])) * 1000)
            position.update(close_mode=scheduled_close_mode(remaining_ms), close_refresh=str(now))
        tighten_target(position, 0)
        return {'reason': 'SCHEDULED_CLOSE' if first else 'SCHEDULED_CLOSE_PENDING',
                'remaining': 0, 'price_mode': position['close_mode'], 'rule_ids': ['D06', 'D11']}

    def _operate(self, state, command):
        action = command['action']
        if action in {'START', 'STOP'}:
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
            if row.get('detached_positions'):
                raise TraderError('E_DSV_OWNERSHIP', 'Late fill from another cycle requires ownership review')
            target = position['owned'] - qty
            if position['exit_target'] is not None:
                target = min(target, position['exit_target'])
            tighten_target(position, target, new_request=True)
            return [{'reason': 'USER_REDUCE', 'remaining': target, 'rule_ids': ['I01', 'I02']}], []
        raise TraderError('E_DSV_COMMAND', 'Unsupported offline command')
