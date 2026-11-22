"""Plan explicit child orders and reconcile only bound native facts."""

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import sqlite3
import time
from uuid import uuid4
from typing import NamedTuple

from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_text, quantity
from online_trader.dsv_core.price_rules import strategy_price
from online_trader.dsv_core.transition import entry_price, price_value
from online_trader.dsv_runtime.target_execution import account_target
from online_trader.errors import TraderError, exception_record
from online_trader.execution.close_routes import close_routes, replay_lots


class SendNotAttempted(NamedTuple):
    """Adapter proof, returned only before entering the native insert call.

    A generic exception (including a TraderError from inside the SDK) is not
    this proof. Cancellation outcomes never use this send-only result.
    """
    effect_id: str
    error_code: str
    exception: dict
    rejection_reason: str | None = None


class CancelNotAttempted(NamedTuple):
    """Proof bound to a prepared cancellation before its native request is queued."""
    effect_id: str
    native_order_id: str
    error_code: str
    exception: dict


def terminal(order):
    if order['send_state'] == 'DEFINITELY_NOT_SENT':
        proof = order.get('local_send_rejection', {})
        return (proof.get('effect_id') == order['child_order_id'] + '-send'
                and order.get('native_id') is None and order['filled_qty'] == 0)
    return (order['terminal_confirmed'] and order['reported_fill_qty'] == order['filled_qty']
            and order['send_state'] not in {'UNKNOWN', 'SEND_POSSIBLE'})


def tighten_target(position, remaining, *, new_request=False):
    # Strategy/schedule target changes supersede a prior Workbench-only price
    # preference. Manual commands set a fresh preference after tightening.
    position.pop('manual_close_mode', None)
    position.pop('manual_close_started_at', None)
    position.pop('exit_manual_takeover', None)
    previous = position['exit_target']
    target = remaining if previous is None else min(previous, remaining)
    if target != previous or new_request:
        position['exit_revision'] = position.get('exit_revision', 0) + 1
        position['target_revision'] = position.get('target_revision', 1) + 1
        position['consecutive_exit_rejections'] = 0
    position.update(exit_target=target, entry_stopped=True)


def _native_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or not value.isprintable():
        raise TraderError('E_DSV_NATIVE_ID', 'Expected a nonempty original native identity')
    return value


def _bind_native(order, value):
    native = _native_id(value)
    if order['send_state'] == 'DEFINITELY_NOT_SENT':
        raise TraderError('E_DSV_NATIVE_FACT', 'Native evidence contradicts a locally proved unsent child')
    known = order['native_id'] or order.get('local_order_id')
    if known is not None and known != native:
        raise TraderError('E_DSV_NATIVE_ID', 'A child cannot change its native order identity')
    order['native_id'] = native
    order['send_state'] = 'NATIVE_SEEN'
    return native


def legal_price(tick, raw):
    if raw is None or any(price_value(tick.get(key)) is None for key in ('tick_size', 'lower_limit', 'upper_limit')):
        return None
    value, step, lo, hi = map(lambda v: Decimal(str(v)), (raw, tick['tick_size'], tick['lower_limit'], tick['upper_limit']))
    with localcontext() as ctx:
        ctx.prec = 80
        return decimal_text(value) if lo <= value <= hi and value % step == 0 else None


def _align_reference_to_tick(tick, reference, direction):
    """Align a strategy reference price to tick_size without exceeding its cap."""
    if reference is None or price_value(tick.get('tick_size')) is None:
        return reference
    value = Decimal(str(reference))
    step = Decimal(str(tick['tick_size']))
    if step <= 0:
        return reference
    with localcontext() as ctx:
        ctx.prec = 80
        if value % step == 0:
            return float(value)
        quotient = value // step
        aligned = quotient * step if direction == 'LONG' else (quotient + (1 if value % step != 0 else 0)) * step
        return float(aligned)


def _clamp_to_limits(tick, raw):
    if raw is None:
        return None
    value = Decimal(str(raw))
    lo = Decimal(str(tick['lower_limit'])) if price_value(tick.get('lower_limit')) is not None else None
    hi = Decimal(str(tick['upper_limit'])) if price_value(tick.get('upper_limit')) is not None else None
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


class ExplicitOrderController:
    def __init__(self, scope, journal, port):
        self.scope = scope
        self.journal = journal
        self.port = port

    def child_orders(self, state, position, effect=None):
        return [state['orders'][child] for child in position['children']
                if effect is None or state['orders'][child]['effect'] == effect]

    def accept_native(self, state, facts):
        for fact in facts:
            child_id = fact['child_order_id']
            order = state['orders'].get(child_id)
            if order is None:
                raise TraderError('E_DSV_NATIVE_ID', 'Native update lacks a prepared child')
            order_day = order['native_trading_day']
            if fact.get('trading_day', state['trading_day']) != order_day:
                raise TraderError('E_DSV_SCOPE', 'Native update must identify its original order day')
            if any(key in fact and fact[key] != order[key] for key in ('contract_id', 'direction', 'native_offset')):
                raise TraderError('E_DSV_NATIVE_FACT', 'Native contract, side or offset differs from the child')
            if fact['kind'] == 'LOCAL_ORDER':
                local = _native_id(fact['native_order_id'])
                if (order['send_state'] != 'CALL_RETURNED' or order['native_id'] is not None
                        or order.get('local_order_id', local) != local):
                    raise TraderError('E_DSV_NATIVE_FACT', 'Local SDK order must match the original returned send')
                order['local_order_id'] = local
                continue
            native_id = _bind_native(order, fact['native_order_id'])
            order.pop('local_order_id', None)
            if fact['kind'] == 'ORDER':
                if stamp := fact.get('source_time_ns'):
                    if order.get('native_order_time_ns', stamp) != stamp:
                        raise TraderError('E_DSV_NATIVE_FACT', 'Original native order timestamp changed')
                    order['native_order_time_ns'] = stamp
                self._order_update(order, fact)
                if self.journal.record_order(child_id, native_id, order):
                    self.journal.event('NATIVE_ORDER', fact)
            else:
                self._trade_update(state, order, native_id, fact)
                self.journal.record_order(child_id, native_id, order)

    @staticmethod
    def _order_update(order, fact):
        status = fact['status']
        filled = quantity(fact['filled_qty'])
        if (status not in {'ALIVE', 'FILLED', 'CANCELLED', 'REJECTED'}
                or filled > order['original_qty'] or status == 'FILLED' and filled != order['original_qty']
                or status == 'REJECTED' and filled):
            raise TraderError('E_DSV_NATIVE_FACT', 'Invalid native order status or cumulative fill')
        if order['terminal_confirmed']:
            if status == 'ALIVE' and filled <= order['reported_fill_qty']:
                return
            if status == order['broker_state'] == 'CANCELLED' and filled > order['reported_fill_qty']:
                order.update(reported_fill_qty=filled, terminal_conflict=True)
                return
            if (status, filled) != (order['broker_state'], order['reported_fill_qty']):
                raise TraderError('E_DSV_NATIVE_FACT', 'Conflicting terminal order evidence')
            return
        if status == 'ALIVE' and filled < max(order['filled_qty'], order['reported_fill_qty'] or 0):
            return
        if status != 'ALIVE' and filled < max(order['filled_qty'], order['reported_fill_qty'] or 0):
            raise TraderError('E_DSV_NATIVE_FACT', 'Terminal quantity contradicts accepted fills')
        order.update(broker_state=status, terminal_confirmed=status != 'ALIVE', reported_fill_qty=filled)
        if order.get('terminal_conflict') and terminal(order):
            order.pop('terminal_conflict')

    def _trade_update(self, state, order, native_id, fact):
        trade_id = _native_id(fact['native_trade_id'])
        qty = quantity(fact['quantity'], positive=True)
        price = price_value(fact['price'])
        if price is None:
            raise TraderError('E_DSV_NATIVE_FACT', 'A fill needs a positive native price')
        order_day = order['native_trading_day']
        fill_day = fact.get('fill_day', order_day)
        if not order_day <= fill_day <= state['trading_day']:
            raise TraderError('E_DSV_SCOPE', 'Fill day is outside its order and observed cabinet days')
        if order_day != state['trading_day'] and 'fill_day' not in fact:
            raise TraderError('E_DSV_SCOPE', 'Late trade requires the original fill day')
        if 'source_time_ns' in fact and 'native_order_time_ns' in order:
            skew_limit_ns = 5_000_000_000 if order_day == fill_day else 0
            if int(fact['source_time_ns']) + skew_limit_ns < int(order['native_order_time_ns']):
                raise TraderError('E_DSV_NATIVE_FACT', 'Native trade precedes its original order timestamp')
            if int(fact['source_time_ns']) < int(order['native_order_time_ns']):
                self.journal.event('NATIVE_TRADE_CLOCK_SKEW', {
                    'child_order_id': order['child_order_id'],
                    'native_order_id': native_id,
                    'native_trade_id': trade_id,
                    'order_time_ns': str(order['native_order_time_ns']),
                    'trade_time_ns': str(fact['source_time_ns']),
                })
        separated = order['contract_id'].split('.')[0] in {'SHFE', 'INE'}
        if fill_day != order_day and separated and order['effect'] == 'REDUCE':
            raise TraderError('E_DSV_NATIVE_FACT', 'A split-offset child cannot spend lots on a different cabinet day')
        normalized = {key: fact[key] for key in ('kind', 'child_order_id', 'native_order_id', 'native_trade_id', 'quantity', 'price')}
        normalized['fill_day'] = fill_day
        if 'source_time_ns' in fact:
            normalized['source_time_ns'] = fact['source_time_ns']
        if not self.journal.record_trade(order_day, native_id, trade_id, normalized):
            return
        row = state['symbols'][order['contract_id']]
        position = row['position']
        limit = order['reported_fill_qty'] if order['terminal_confirmed'] else order['original_qty']
        if order['filled_qty'] + qty > order['original_qty']:
            raise TraderError('E_DSV_NATIVE_FACT', 'Trade would exceed original child quantity')
        if position is not None and position['id'] != order['position_id'] and order['effect'] == 'OPEN':
            self._record_detached_late_open(state, row, position, order, qty, fact, fill_day)
            self._reconcile_pending_reduces(state, row, state['retired_positions'][order['position_id']])
            return
        matching = position is not None and position['id'] == order['position_id']
        if order['effect'] == 'REDUCE' and (
                not matching or not self._allocate_reduce(state, position, order, fact, fill_day)):
            self._review_late_reduce(state, row, order, qty, fact, fill_day)
            return
        was_within_terminal = order['terminal_confirmed'] and order['filled_qty'] + qty <= limit
        if position is None or position['id'] != order['position_id'] or order['filled_qty'] + qty > limit:
            position = self._recover_late_trade(state, row, order, qty)
            if was_within_terminal:
                order['terminal_confirmed'] = True
        order['filled_qty'] += qty
        if order['effect'] == 'OPEN':
            position['entry_filled'] += qty
            position['owned'] += qty
            position['entry_notional'] = decimal_text(Decimal(position['entry_notional']) + Decimal(fact['price']) * qty)
            self._position_fill(position, order, fact, fill_day, state_seq=state.get('sequence'))
            self.position_buckets(state, position, order['contract_id'])
            self._reconcile_pending_reduces(state, row, position)
        else:
            position['consecutive_exit_rejections'] = 0
            position.pop('exit_manual_takeover', None)
        if order.get('terminal_conflict') and terminal(order):
            order.pop('terminal_conflict')
        self.journal.event('NATIVE_TRADE', fact)

    def _review_late_reduce(self, state, row, order, qty, fact, fill_day):
        original = state['retired_positions'].get(order['position_id'])
        detached = row.get('detached_positions', [])
        if (original is not None and original['id'] in detached and
                self._allocate_reduce(state, original, order, fact, fill_day)):
            self._accept_corrected_total(order, qty)
            if not original['owned']:
                detached.remove(original['id'])
            self.journal.event('DETACHED_NATIVE_REDUCE_APPLIED', fact)
            return
        self._accept_corrected_total(order, qty)
        entry = {'position_id': order['position_id'], 'order_day': order['native_trading_day'],
                 'fill_day': fill_day, 'fact': fact}
        row.setdefault('native_conflicts', []).append(entry)
        if row['position'] is not None:
            row['position'].update(entry_stopped=True, preserve_open_on_stop=False)
        self.journal.event('NATIVE_REDUCE_ALLOCATION_CONFLICT', entry)

    def _allocate_reduce(self, state, position, order, fact, fill_day):
        qty = fact['quantity']
        if qty > position['owned']:
            return False
        self._position_fill(position, order, fact, fill_day, state_seq=state.get('sequence'))
        try:
            buckets = self.position_buckets(state, position, order['contract_id'])
        except TraderError:
            position['fills'].pop()
            return False
        if sum(buckets.values()) != position['owned'] - qty:
            position['fills'].pop()
            return False
        position['owned'] -= qty
        return True

    def _reconcile_pending_reduces(self, state, row, position):
        remaining = []
        for entry in row.get('native_conflicts', []):
            fact = entry['fact']
            order = state['orders'][fact['child_order_id']]
            if entry['position_id'] == position['id'] and self._allocate_reduce(state, position, order, fact, entry['fill_day']):
                self.journal.event('NATIVE_REDUCE_ALLOCATION_RECONCILED', entry)
            else:
                remaining.append(entry)
        if remaining:
            row['native_conflicts'] = remaining
        else:
            row.pop('native_conflicts', None)
        detached = row.get('detached_positions', [])
        if not position['owned'] and position['id'] in detached:
            detached.remove(position['id'])

    @staticmethod
    def _accept_corrected_total(order, qty):
        order['filled_qty'] += qty
        if order['terminal_confirmed'] and order['filled_qty'] > order['reported_fill_qty']:
            order.update(terminal_confirmed=False, terminal_conflict=True)
        if order.get('terminal_conflict') and terminal(order):
            order.pop('terminal_conflict')

    @staticmethod
    def _position_fill(position, order, fact, fill_day, *, state_seq=None):
        seq = max(len(position['fills']) + 1, int(state_seq or 0))
        position['fills'].append({'event_seq': seq, 'contract_id': order['contract_id'],
                                  'child_order_id': order['child_order_id'],
                                  'direction': position['direction'],
                                  'trade_key': [order['native_trading_day'], fact['native_order_id'], fact['native_trade_id']],
                                  'trading_day': fill_day, 'quantity': fact['quantity'], 'price': fact['price'],
                                  'effect': order['effect'],
                                  'offset_kind': order['native_offset']})

    def _record_detached_late_open(self, state, row, current, order, qty, fact, fill_day):
        original = state['retired_positions'].get(order['position_id'])
        if original is None:
            raise TraderError('E_DSV_NATIVE_FACT', 'Terminal correction lacks its original strategy position')
        if order['terminal_confirmed']:
            order.update(terminal_confirmed=False, terminal_conflict=True)
        order['filled_qty'] += qty
        original['entry_filled'] += qty
        original['owned'] += qty
        original['entry_notional'] = decimal_text(Decimal(original['entry_notional']) + Decimal(fact['price']) * qty)
        self._position_fill(original, order, fact, fill_day, state_seq=state.get('sequence'))
        # Keep detached cycles distinct; the current position may be another side.
        detached = row.setdefault('detached_positions', [])
        if original['id'] not in detached:
            detached.append(original['id'])
        current.update(entry_stopped=True, preserve_open_on_stop=False)
        self.journal.event('DETACHED_NATIVE_TRADE_REVIEW', fact)

    def _recover_late_trade(self, state, row, order, qty):
        position = row['position']
        if not order['terminal_confirmed'] or position is not None and position['id'] != order['position_id']:
            raise TraderError('E_DSV_NATIVE_FACT', 'Trade would exceed its bound order or position')
        if position is None:
            if order['effect'] != 'OPEN':
                raise TraderError('E_DSV_NATIVE_FACT', 'Late closing trade has no remaining owned inventory')
            position = state['retired_positions'].pop(order['position_id'], None)
            if position is None:
                raise TraderError('E_DSV_NATIVE_FACT', 'Terminal correction lacks its original strategy position')
            row['position'] = position
            tighten_target(position, 0)
        order.update(terminal_confirmed=False, terminal_conflict=True)
        position.update(entry_stopped=True, preserve_open_on_stop=False)
        self.journal.event('NATIVE_TERMINAL_CONFLICT', {'child_order_id': order['child_order_id'],
                                                     'reported_fill_qty': order['reported_fill_qty'],
                                                     'new_fill_qty': order['filled_qty'] + qty})
        return position

    def new_order(self, state, position, market, effect, qty, price, offset):
        child_id = 'child-' + uuid4().hex
        opening = effect == 'OPEN'
        side = 'BUY' if (position['direction'] == 'LONG') == opening else 'SELL'
        contract = market['contract_id']
        chain = self.child_orders(state, position, effect)
        parent = chain[-1]['child_order_id'] if chain else None
        order = OrderState(child_id, position['id'] + '-' + effect.lower(), child_id, effect, qty, price,
                           parent_child_order_id=parent, ordinal=len(chain) + 1,
                           native_offset=offset, native_trading_day=state['trading_day']).to_dict()
        order.update(contract_id=contract, direction=side, position_id=position['id'], native_id=None, cancel_reason=None)
        if not opening:
            order['exit_revision'] = position.get('exit_revision', 0)
        tick = market.get('tick')
        if tick is not None and tick.get('native_time') is not None:
            order['quote_at'] = tick['native_time']
        state['orders'][child_id] = order
        position['children'].append(child_id)
        return self._prepare(order, 'SEND', quantity=qty, limit_price=price, native_offset=offset, direction=side)

    def cancel(self, order, reason, *, retry_risk_refusal=False):
        identity = None
        if order['send_state'] == 'NATIVE_SEEN':
            identity = order['native_id']
        elif order['send_state'] == 'CALL_RETURNED':
            identity = order.get('local_order_id')
        if order['terminal_confirmed'] or order['cancel_status'] or identity is None:
            return None
        if (not retry_risk_refusal
                and order.get('local_cancel_rejection', {}).get('error_code') == 'E_DSV_SDK_CANCEL_RISK'):
            return None
        order['cancel_attempt'] = order.get('cancel_attempt', 0) + 1
        order.update(cancel_status='SEND_POSSIBLE', cancel_reason=reason)
        return self._prepare(order, 'CANCEL', native_order_id=identity, reason=reason)

    def _prepare(self, order, kind, **parameters):
        effect_id = order['child_order_id'] + '-' + kind.lower()
        if kind == 'CANCEL' and order.get('cancel_attempt', 1) > 1:
            effect_id += '-' + str(order['cancel_attempt'])
        effect = {'id': effect_id, 'kind': kind,
                  'child_order_id': order['child_order_id'], 'contract_id': order['contract_id'],
                  'account_key': self.scope['account_key'], 'provider': self.scope['provider'],
                  'trading_day': order['native_trading_day'], **parameters}
        if kind == 'CANCEL':
            effect['cancel_attempt'] = order['cancel_attempt']
            order['cancel_effect_id'] = effect_id
        self.journal.prepare(effect)
        return effect

    @staticmethod
    def proves_position(state, position, market, *, for_effect=None):
        own = 'long_qty' if position['direction'] == 'LONG' else 'short_qty'
        other = 'short_qty' if own == 'long_qty' else 'long_qty'
        if market.get('standalone_external_model'):
            if for_effect == 'REDUCE':
                if market[own] < position['owned']:
                    return False
                if not position['owned']:
                    return True
                return bool(ExplicitOrderController._reduction_routes(
                    state, position, market, position['owned'], include_pending_orders=False))
            if market[own] != position['owned'] or market[other] or market.get('external_active_orders'):
                return False
            if not position['owned']:
                return True
            return bool(ExplicitOrderController._reduction_routes(
                state, position, market, position['owned'], include_pending_orders=False))
        if market[own] != position['owned'] or market[other] or market.get('external_orders'):
            return False
        if not position['owned']:
            return True
        return bool(ExplicitOrderController._reduction_routes(
            state, position, market, position['owned'], include_pending_orders=False))

    @staticmethod
    def _position_fills(position, contract):
        fills = [fill | {'trade_key': tuple(fill['trade_key'])} for fill in position['fills']]
        if position.get('legacy_owned'):
            fills.insert(0, {'contract_id': contract, 'direction': position['direction'],
                             'trade_key': ('legacy-checkpoint', position['id']), 'trading_day': position['open_day'],
                             'effect': 'OPEN', 'quantity': position['legacy_owned'], 'event_seq': 0})
        # Backfilled prior-day fills keep their original acquisition bucket.
        ordered = sorted(fills, key=lambda fill: (fill['trading_day'], fill['event_seq']))
        return [fill | {'event_seq': index} for index, fill in enumerate(ordered)]

    @staticmethod
    def position_buckets(state, position, contract):
        day = state['trading_day']
        lots = replay_lots(contract, position['direction'], day,
                           ExplicitOrderController._position_fills(position, contract),
                           require_allocations=False, historical_first_on_combined=True)
        today = sum(lot['quantity'] for lot in lots if lot['day'] == day and not lot['already_historical'])
        return {'today': today, 'historical': sum(lot['quantity'] for lot in lots) - today}

    @staticmethod
    def _reduction_routes(state, position, market, needed, *, include_pending_orders=True):
        day = state['trading_day']
        side = position['direction'].lower()
        today = market.get(f'{side}_today')
        historical = market.get(f'{side}_historical')
        if today is None or historical is None:
            if position['open_day'] != day or any(fill['trading_day'] != day for fill in position['fills']):
                return []
            today, historical = position['owned'], 0
        fills = ExplicitOrderController._position_fills(position, market['contract_id'])
        pending_orders = []
        target_quantity = needed
        if include_pending_orders and 'orders' in state:
            pending_orders = [
                order | {'contract_id': market['contract_id'], 'direction': position['direction'],
                         'trading_day': order.get('native_trading_day', day)}
                for child_id in position.get('children', ())
                if (order := state['orders'].get(child_id)) is not None
                and order['effect'] == 'REDUCE' and not terminal(order)
            ]
            if pending_orders and position.get('exit_target') is not None:
                target_quantity = max(needed, position['owned'] - position['exit_target'])
        try:
            result = close_routes(contract_id=market['contract_id'], direction=position['direction'], current_day=day,
                                  fills=fills, native={'today': today, 'historical': historical, 'total': today + historical},
                                  orders=pending_orders, target_quantity=target_quantity,
                                  allow_external=bool(market.get('standalone_external_model')),
                                  historical_first_on_combined=True)
        except TraderError as error:
            if error.code in {'E_OWNERSHIP', 'E_FACT_BARRIER'}:
                return []
            raise
        return result['routes']

    def plan_detached(self, state, row, market, wall_time):
        positions = [state['retired_positions'][identity] for identity in row.get('detached_positions', [])]
        if row['position'] is not None:
            positions.append(row['position'])
        children = [order for position in positions for order in self.child_orders(state, position) if not terminal(order)]
        if children or row.get('native_conflicts') or market.get('external_orders'):
            return []
        contract, day = market['contract_id'], state['trading_day']
        buckets = {key: 0 for key in ('long_today', 'long_historical', 'short_today', 'short_historical')}
        for position in positions:
            for bucket, count in self.position_buckets(state, position, contract).items():
                buckets[position['direction'].lower() + '_' + bucket] += count
        if any(market.get(bucket) != quantity for bucket, quantity in buckets.items()):
            return []
        for position in positions:
            target = position.get('reviewed_exit_target')
            if target is None or target >= position['owned']:
                continue
            old = self.child_orders(state, position, 'REDUCE')
            if old and old[-1]['filled_qty'] < old[-1]['original_qty']:
                last = old[-1]
                newer_target = position.get('exit_revision', 0) > last.get('exit_revision', 0)
                rejected_retry = self._retry_broker_exit_failure(position, last, market, newer_target)
                if not rejected_retry and not self._retry_unsent_exit(last, market, newer_target) and (
                        last['broker_state'] != 'CANCELLED' or not newer_target):
                    continue
            side = position['direction'].lower()
            fills = [fill | {'position_cycle_id': candidate['id'], 'event_seq': index}
                     for index, (candidate, fill) in enumerate(
                         (candidate, fill) for candidate in positions if candidate['direction'] == position['direction']
                         for fill in self._position_fills(candidate, contract))]
            native = {bucket: buckets[f'{side}_{bucket}'] for bucket in ('today', 'historical')}
            native['total'] = native['today'] + native['historical']
            result = close_routes(contract_id=contract, direction=position['direction'], current_day=day, fills=fills,
                                  native=native, orders=[], target_quantity=position['owned'] - target,
                                  position_cycle_id=position['id'], historical_first_on_combined=True)
            desired = self._desired(position, market, 'REDUCE', wall_time)
            if result['routes'] and desired is not None:
                route = result['routes'][0]
                return [self.new_order(state, position, market, 'REDUCE',
                                       min(route['quantity'], market.get('max_order_qty', 2_147_483_647)),
                                       desired, route['native_offset'])]
        return []

    @staticmethod
    def _has_fresh_quote_for_order(order, market):
        tick = market['tick']
        opponent = 'ask1' if order['direction'] == 'BUY' else 'bid1'
        if price_value(tick.get(opponent)) is None and price_value(tick.get('last')) is None:
            return False
        quote_at = tick.get('native_time')
        previous = order.get('quote_at')
        return quote_at is not None and (
            previous is None or datetime.fromisoformat(quote_at) > datetime.fromisoformat(previous))

    @classmethod
    def _retry_unsent_exit(cls, order, market, newer_target):
        if order['effect'] != 'REDUCE' or order['send_state'] != 'DEFINITELY_NOT_SENT' or not terminal(order):
            return False
        if newer_target:
            return True
        return cls._has_fresh_quote_for_order(order, market)

    @classmethod
    def _retry_unsent_open(cls, order, market):
        if order['effect'] != 'OPEN' or order['send_state'] != 'DEFINITELY_NOT_SENT' or not terminal(order):
            return False
        reason = order.get('local_send_rejection', {}).get('rejection_reason')
        if reason not in {'QUOTE_REVISION_CHANGED', 'NATIVE_QUOTE_NOT_CURRENT'}:
            return False
        return cls._has_fresh_quote_for_order(order, market)

    def _retry_broker_exit_failure(self, position, order, market, newer_target):
        if order['effect'] != 'REDUCE' or not terminal(order):
            return False
        is_rejected = order['broker_state'] == 'REJECTED'
        is_exchange_cancel = order['broker_state'] == 'CANCELLED' and order.get('cancel_reason') is None
        if not (is_rejected or is_exchange_cancel):
            return False
        if newer_target:
            position['consecutive_exit_rejections'] = 0
            position.pop('exit_manual_takeover', None)
            return True
        if not order.get('exit_failure_counted'):
            order['exit_failure_counted'] = True
            position['consecutive_exit_rejections'] = position.get('consecutive_exit_rejections', 0) + 1
        if position.get('consecutive_exit_rejections', 0) >= 3:
            position['exit_manual_takeover'] = True
            position['execution_reason'] = 'EXIT_MANUAL_TAKEOVER_REQUIRED'
            return False
        return self._has_fresh_quote_for_order(order, market)

    @staticmethod
    def _desired(position, market, effect, wall_time):
        tick = market['tick']
        direction = position['direction']
        if effect == 'OPEN':
            mode = position['mode']
            if mode != 'ACTIVE':
                expired = market.get('price_context_expired')
                if expired is None:
                    expired = wall_time - float(position['context_at']) > 90
                if expired:
                    mode = 'ACTIVE'
            raw_ref = position['theory'] if mode == 'PASSIVE' else position['cap']
            reference = _align_reference_to_tick(tick, raw_ref, direction) if raw_ref is not None else None
        else:
            direction = 'SHORT' if direction == 'LONG' else 'LONG'
            manual = position.get('manual_close_mode')
            if manual is not None:
                buy = direction == 'LONG'
                field = ('last' if manual == '最新价'
                         else 'bid1' if (manual == '排队价') == buy else 'ask1')
                direct = legal_price(tick, tick.get(field))
                if direct is not None:
                    return direct
                started = position.get('manual_close_started_at')
                if started is not None and wall_time is not None:
                    elapsed_ms = max(0, int((float(wall_time) - float(started)) * 1000))
                    if elapsed_ms >= 4000:
                        from online_trader.execution.policy import select_manual_price
                        fallback = select_manual_price(
                            manual, 'BUY' if buy else 'SELL', tick,
                            elapsed_ms=elapsed_ms,
                            limit_fallback_ratio=str(market.get('limit_fallback_ratio', '0.01')))
                        if fallback.get('price') is not None:
                            return legal_price(tick, fallback['price'])
                return None
            mode = market.get('close_price_mode', position.get('close_mode'))
            if mode in {'AGGR1', 'MARKET'}:
                buy = direction == 'LONG'
                quote = tick.get('ask1' if buy else 'bid1')
                if price_value(quote) is None:
                    quote = tick.get('last')

                def positive(value):
                    return Decimal(value) if price_value(value) is not None else None

                raw = strategy_price('BUY' if buy else 'SELL', mode, opponent=positive(quote),
                                     tick=positive(tick.get('tick_size')),
                                     limit=positive(tick.get('upper_limit' if buy else 'lower_limit')), reference=None)
                if mode == 'AGGR1':
                    raw = _clamp_to_limits(tick, raw)
                return legal_price(tick, raw)
            mode, reference = 'ACTIVE', None
        return legal_price(tick, entry_price(tick, direction, mode, None if reference is None else float(reference)))

    def target_accounting(self, state, position, effect):
        """Return the single quantity projection used by maintenance and pricing."""
        orders = self.child_orders(state, position, effect)
        if effect == 'OPEN':
            if (position['entry_stopped'] and position.get('preserve_open_on_stop')
                    and position.get('exit_target') is None):
                pending_open = sum(max(0, int(order['original_qty']) - int(order['filled_qty']))
                                   for order in orders if not terminal(order))
                target = min(position['quantity'], position['entry_filled'] + pending_open)
            else:
                target = position['entry_filled'] if position['entry_stopped'] else position['quantity']
            confirmed = position['entry_filled']
        else:
            confirmed = 0
            target = 0 if position['exit_target'] is None else position['owned'] - position['exit_target']
        return account_target(confirmed_qty=confirmed, target_qty=target,
                              orders=orders, terminal=terminal)

    def target_projection(self, state, row):
        """Build the read-only execution projection consumed by Runtime views."""
        position = row.get('position')
        if position is None:
            return None
        opening = self.target_accounting(state, position, 'OPEN')
        reducing = self.target_accounting(state, position, 'REDUCE')
        uncertain = opening.uncertain_children + reducing.uncertain_children
        cancelling = opening.cancel_pending_children + reducing.cancel_pending_children
        if row.get('native_conflicts') or row.get('detached_positions'):
            status, reason = 'BLOCKED', 'BROKER_FACTS_INCOMPLETE'
        elif uncertain:
            status, reason = 'RECONCILING', 'SEND_RESULT_UNKNOWN'
        elif cancelling:
            status = 'CANCELLING'
            reason = (position.get('execution_reason')
                      if position.get('execution_reason') == 'PRICE_REPRICE_REQUIRED'
                      else 'CANCEL_PENDING')
        elif opening.excess_pending_qty or reducing.excess_pending_qty:
            status, reason = 'CANCELLING', 'TARGET_REDUCED'
        elif opening.pending_qty or reducing.pending_qty:
            status, reason = 'WORKING', 'WORKING_ORDER_COVERS_TARGET'
        elif opening.needed_qty or reducing.needed_qty:
            reason = position.get('execution_reason') or 'TARGET_GAP'
            status = ('BLOCKED' if reason in {
                'POSITION_CHANGED', 'NO_LEGAL_PRICE', 'EXIT_MANUAL_TAKEOVER_REQUIRED', 'E_ENTRY_PRICE_CAP'
            } else 'READY')
        else:
            status, reason = 'DONE', 'TARGET_ALREADY_COVERED'
        target_owned = position['exit_target']
        if target_owned is None:
            target_owned = position['entry_filled'] if position['entry_stopped'] else position['quantity']
        return {
            'target_id': position.get('target_id', position['id']),
            'target_revision': position.get('target_revision', 1),
            'position_id': position['id'],
            'contract_id': position['contract_id'],
            'direction': position['direction'],
            'target_owned_qty': target_owned,
            'confirmed_owned_qty': position['owned'],
            'open_pending_qty': opening.pending_qty,
            'reduce_pending_qty': reducing.pending_qty,
            'open_needed_qty': opening.needed_qty,
            'reduce_needed_qty': reducing.needed_qty,
            'uncertain_children': list(uncertain),
            'cancel_pending_children': list(cancelling),
            'status': status,
            'reason': reason,
            'decision_reason': position.get('execution_reason'),
            'exit_revision': position.get('exit_revision', 0),
        }

    def plan_maintenance(self, state, row, market):
        """Cancel provably owned residual orders using identity and quantity alone."""
        position = row['position']
        prior = [
            order for order in state['orders'].values()
            if order['contract_id'] == market['contract_id'] and not terminal(order)
            and (position is None or order['position_id'] != position['id'])]
        if prior:
            current = [] if position is None else self.child_orders(state, position)
            return [request for order in prior + current if order['effect'] == 'OPEN' and not order.get('terminal_conflict')
                    and (request := self.cancel(order, 'TARGET_REDUCED'))], False
        if row.get('detached_positions') or row.get('native_conflicts'):
            owners = [state['retired_positions'][identity] for identity in row.get('detached_positions', [])]
            if position is not None:
                owners.append(position)
            opening = [order for owner in owners for order in self.child_orders(state, owner, 'OPEN')
                       if not terminal(order)]
            return [request for order in opening if not order.get('terminal_conflict')
                    and (request := self.cancel(order, 'TARGET_REDUCED'))], not opening and not row.get('native_conflicts')
        if position is None:
            return [], False
        children = self.child_orders(state, position)
        if any(order['native_trading_day'] != state['trading_day'] and not terminal(order) for order in children):
            return [], False
        if any(order.get('terminal_conflict') and not terminal(order) for order in children):
            return [request for order in children if not order.get('terminal_conflict')
                    and (request := self.cancel(order, 'TARGET_REDUCED'))], False
        effects = []
        for effect in ('OPEN', 'REDUCE'):
            accounting = self.target_accounting(state, position, effect)
            excess = accounting.excess_pending_qty
            if not excess:
                continue
            # A cancellation does not release capacity. Cancel newest children
            # first and let later native facts drive a fresh calculation.
            for order in reversed(self.child_orders(state, position, effect)):
                if terminal(order):
                    continue
                if request := self.cancel(order, 'TARGET_REDUCED'):
                    effects.append(request)
                    position['execution_reason'] = 'TARGET_REDUCED'
                excess -= max(0, order['original_qty'] - order['filled_qty'])
                if excess <= 0:
                    break
        return effects, True

    def plan_priced_effects(self, state, row, market, wall_time):
        if row.get('detached_positions'):
            return self.plan_detached(state, row, market, wall_time)
        position = row['position']
        effects = []
        for effect in ('OPEN', 'REDUCE'):
            opening = effect == 'OPEN'
            if opening and not market.get('allow_new_entry', market.get('allow_new_order', True)):
                continue
            if not opening and not market.get('allow_protective_exit', market.get('allow_new_order', True)):
                continue
            proven_position = self.proves_position(state, position, market, for_effect=effect)
            previous = self.child_orders(state, position, effect)
            outstanding = [order for order in previous if not terminal(order)]
            accounting = self.target_accounting(state, position, effect)
            needed = accounting.needed_qty
            desired = self._desired(position, market, effect, wall_time)
            repricing = False
            if proven_position and desired is not None:
                for order in outstanding:
                    pending = order['original_qty'] - order['filled_qty']
                    current = Decimal(order['limit_price'])
                    proposed = Decimal(desired)
                    worse = proposed > current if order['direction'] == 'BUY' else proposed < current
                    # ``needed`` already subtracts all non-terminal children.  A
                    # fully covered target therefore has needed == 0, but its
                    # working child must still be cancelled when the strategy
                    # price moves against that order.  Compare the child with
                    # the target gap before pending quantity is applied.
                    target_gap = max(0, accounting.target_qty - accounting.confirmed_qty)
                    if worse and 0 < pending <= target_gap and (request := self.cancel(order, 'REPRICE')):
                        effects.append(request)
                        repricing = True
                        position['execution_reason'] = 'PRICE_REPRICE_REQUIRED'
            if not proven_position:
                position['execution_reason'] = 'POSITION_CHANGED'
            elif desired is None:
                position['execution_reason'] = 'NO_LEGAL_PRICE'
            if (not proven_position or desired is None or accounting.cancel_pending_children
                    or accounting.uncertain_children or repricing):
                continue
            if needed <= 0:
                continue
            maximum = market.get('max_order_qty', 2_147_483_647)
            if opening and not market.get('min_order_qty', 1) <= needed <= maximum:
                position['entry_stopped'] = True
                continue
            # Reconciliation proves completion independently of the result of the cancel call.
            if previous and not outstanding and previous[-1]['filled_qty'] < previous[-1]['original_qty']:
                old = previous[-1]
                allowed_retry = old['cancel_reason'] in {'REPRICE', 'TARGET_REDUCED'}
                rolled_exit = not opening and old['native_trading_day'] != state['trading_day']
                newer_target = not opening and position.get('exit_revision', 0) > old.get('exit_revision', 0)
                local_retry = self._retry_unsent_open(old, market) if opening else self._retry_unsent_exit(old, market, newer_target)
                rejected_retry = not opening and self._retry_broker_exit_failure(position, old, market, newer_target)
                if not rejected_retry and not local_retry and (old['broker_state'] != 'CANCELLED' or not (
                        allowed_retry or newer_target or rolled_exit and old['cancel_reason'] != 'USER_CANCEL')):
                    continue
                if opening and local_retry and position.get('cap') is not None:
                    cap = Decimal(str(position['cap']))
                    proposed = Decimal(desired)
                    exceeds = proposed > cap if position['direction'] == 'LONG' else proposed < cap
                    if exceeds:
                        position.update(entry_stopped=True, preserve_open_on_stop=False,
                                        execution_reason='E_ENTRY_PRICE_CAP')
                        continue
            if opening:
                effects.append(self.new_order(state, position, market, effect, needed, desired, 'OPEN'))
            else:
                routes = self._reduction_routes(state, position, market, needed)
                if routes:
                    route = routes[0]
                    previous_offsets = {order['native_offset'] for order in previous}
                    if previous_offsets and route['native_offset'] not in previous_offsets:
                        position['execution_reason'] = 'OFFSET_ROUTE_CHANGED'
                    effects.append(self.new_order(state, position, market, effect,
                                                  min(route['quantity'], maximum), desired, route['native_offset']))
        return effects

    def plan(self, state, row, market, wall_time):
        if row.get('position') is not None:
            row['position'].pop('execution_reason', None)
        maintenance, can_price = self.plan_maintenance(state, row, market)
        if (can_price and market.get('tick') is None
                and any(self.target_accounting(state, row['position'], effect).needed_qty
                        for effect in ('OPEN', 'REDUCE'))):
            row['position']['execution_reason'] = 'NO_LEGAL_PRICE'
        allow_any = (market.get('allow_new_order', True)
                     or market.get('allow_new_entry', False)
                     or market.get('allow_protective_exit', False))
        if can_price and market.get('tick') is not None and allow_any:
            maintenance.extend(self.plan_priced_effects(state, row, market, wall_time))
        if can_price and not row.get('detached_positions'):
            self.settle(state, row)
        return maintenance

    def settle(self, state, row):
        position = row['position']
        if position is None or row.get('native_conflicts') or 'close_request_at' in position:
            return
        if position['owned'] == 0 and position['entry_stopped'] and not position['children']:
            row['position'] = None
            return
        if (position['owned'] == 0 and position['children']
                and all(terminal(order) for order in self.child_orders(state, position))):
            opens = self.child_orders(state, position, 'OPEN')
            retryable_open_unsent = (
                opens and not position['entry_filled'] and not position['entry_stopped']
                and opens[-1]['send_state'] == 'DEFINITELY_NOT_SENT'
                and opens[-1].get('local_send_rejection', {}).get('rejection_reason') in {
                    'QUOTE_REVISION_CHANGED', 'NATIVE_QUOTE_NOT_CURRENT'})
            if not retryable_open_unsent and (
                    position['entry_filled'] or position['entry_stopped'] or
                    opens[-1]['cancel_reason'] != 'REPRICE' or opens[-1]['broker_state'] != 'CANCELLED'):
                if position.get('gap_exit', {}).get('day_stop'):
                    row['day_stop'] = True
                state['retired_positions'][position['id']] = position
                row['position'] = None

    def dispatch(self, state, effects):
        for effect in effects:
            order = state['orders'][effect['child_order_id']]
            sender = self.port.send if effect['kind'] == 'SEND' else self.port.cancel
            details = {'id': effect['id'], 'child_order_id': effect['child_order_id'],
                       'contract_id': effect['contract_id'],
                       'kind': effect['kind'], 'stage': 'PORT_CALL',
                       'dispatch_monotonic_ns': str(time.monotonic_ns())}
            try:
                outcome = sender(dict(effect))
                if isinstance(outcome, SendNotAttempted):
                    if (effect['kind'] != 'SEND' or outcome.effect_id != effect['id']
                            or order['send_state'] != 'SEND_POSSIBLE' or order.get('native_id') is not None
                            or order['filled_qty']):
                        raise TraderError('E_DSV_DISPATCH_RESULT', 'Unsent proof does not match a new prepared send')
                    result = 'DEFINITELY_NOT_SENT'
                    details.update(stage='PRE_SEND_VALIDATION', error_code=outcome.error_code,
                                   exception=outcome.exception)
                    if outcome.rejection_reason is not None:
                        details['rejection_reason'] = outcome.rejection_reason
                elif isinstance(outcome, CancelNotAttempted):
                    if (effect['kind'] != 'CANCEL' or outcome.effect_id != effect['id']
                            or outcome.native_order_id != effect['native_order_id']
                            or order['cancel_status'] != 'SEND_POSSIBLE'):
                        raise TraderError('E_DSV_DISPATCH_RESULT', 'Unattempted cancellation proof differs from its effect')
                    result = 'DEFINITELY_NOT_SENT'
                    details.update(stage='PRE_CANCEL_VALIDATION', native_order_id=outcome.native_order_id,
                                   error_code=outcome.error_code, exception=outcome.exception)
                else:
                    result = 'CALL_RETURNED'
            except Exception as error:
                result = 'UNKNOWN'
                details.update(error_code=getattr(error, 'code', 'E_DSV_PORT_CALL'),
                               exception=exception_record(error))
            if effect['kind'] == 'SEND':
                order['send_state'] = result
                if result == 'DEFINITELY_NOT_SENT':
                    # This is local proof, NOT a fabricated broker terminal or
                    # a cancelled order. Do not refund the consumed strategy target.
                    order['local_send_rejection'] = {'effect_id': effect['id'], **details}
                    position = state['symbols'][order['contract_id']]['position']
                    retryable_open_race = (
                        order['effect'] == 'OPEN'
                        and details.get('rejection_reason') in {'QUOTE_REVISION_CHANGED', 'NATIVE_QUOTE_NOT_CURRENT'})
                    if (position is not None and position['id'] == order['position_id']
                            and order['effect'] == 'OPEN' and not retryable_open_race):
                        position.update(entry_stopped=True, preserve_open_on_stop=False)
            else:
                if result == 'DEFINITELY_NOT_SENT':
                    order['cancel_status'] = None
                    order['local_cancel_rejection'] = {'effect_id': effect['id'], **details}
                else:
                    order['cancel_status'] = result
            details['returned_monotonic_ns'] = str(time.monotonic_ns())
            if 'error_code' in details:
                order['last_dispatch_error'] = dict(details)
            try:
                with self.journal.transaction():
                    self.journal.resolve(effect, result)
                    self.journal.event('EFFECT_RESULT', details | {
                        'result': result, 'at': datetime.now(timezone.utc).isoformat()})
                    self.journal.save(state)
            except sqlite3.Error as error:
                raise TraderError('E_DSV_STORE', 'Dispatch outcome could not be committed; prepared effect requires reconciliation') from error
