"""Plan explicit child orders and reconcile only bound native facts."""

from decimal import Decimal, localcontext
import sqlite3
from uuid import uuid4

from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_text, quantity
from online_trader.dsv_core.price_rules import strategy_price
from online_trader.dsv_core.transition import entry_price, price_value
from online_trader.errors import TraderError


def terminal(order):
    return (order['terminal_confirmed'] and order['reported_fill_qty'] == order['filled_qty']
            and order['send_state'] not in {'UNKNOWN', 'SEND_POSSIBLE'})


def _native_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or not value.isprintable():
        raise TraderError('E_DSV_NATIVE_ID', 'Expected a nonempty original native identity')
    return value


def _bind_native(order, value):
    native = _native_id(value)
    if order['native_id'] is not None and order['native_id'] != native:
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
            if any(key in fact and fact[key] != order[key] for key in ('contract_id', 'direction', 'native_offset')):
                raise TraderError('E_DSV_NATIVE_FACT', 'Native contract, side or offset differs from the child')
            native_id = _bind_native(order, fact['native_order_id'])
            if fact['kind'] == 'ORDER':
                self._order_update(order, fact)
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
            if (status, filled) != (order['broker_state'], order['reported_fill_qty']):
                raise TraderError('E_DSV_NATIVE_FACT', 'Conflicting terminal order evidence')
            return
        if status == 'ALIVE' and filled < (order['reported_fill_qty'] or 0):
            return
        if status != 'ALIVE' and filled < max(order['filled_qty'], order['reported_fill_qty'] or 0):
            raise TraderError('E_DSV_NATIVE_FACT', 'Terminal quantity contradicts accepted fills')
        order.update(broker_state=status, terminal_confirmed=status != 'ALIVE', reported_fill_qty=filled)

    def _trade_update(self, state, order, native_id, fact):
        trade_id = _native_id(fact['native_trade_id'])
        qty = quantity(fact['quantity'], positive=True)
        price = price_value(fact['price'])
        if price is None:
            raise TraderError('E_DSV_NATIVE_FACT', 'A fill needs a positive native price')
        normalized = {key: fact[key] for key in ('kind', 'child_order_id', 'native_order_id', 'native_trade_id', 'quantity', 'price')}
        if not self.journal.record_trade(native_id, trade_id, normalized):
            return
        row = state['symbols'][order['contract_id']]
        position = row['position']
        limit = order['reported_fill_qty'] if order['terminal_confirmed'] else order['original_qty']
        if position is None or position['id'] != order['position_id'] or order['filled_qty'] + qty > limit:
            raise TraderError('E_DSV_NATIVE_FACT', 'Trade would exceed its bound order or position')
        order['filled_qty'] += qty
        if order['effect'] == 'REDUCE':
            if qty > position['owned']:
                raise TraderError('E_DSV_NATIVE_FACT', 'Closing trade exceeds the owned quantity')
            position['owned'] -= qty
        else:
            position['entry_filled'] += qty
            position['owned'] += qty
            position['entry_notional'] = decimal_text(Decimal(position['entry_notional']) + Decimal(fact['price']) * qty)
        self.journal.event('NATIVE_TRADE', fact)

    def new_order(self, state, position, market, effect, qty, price):
        child_id = 'child-' + uuid4().hex
        opening = effect == 'OPEN'
        side = 'BUY' if (position['direction'] == 'LONG') == opening else 'SELL'
        contract = market['contract_id']
        offset = 'OPEN' if opening else 'CLOSETODAY' if contract.split('.')[0] in {'SHFE', 'INE'} else 'CLOSE'
        chain = self.child_orders(state, position, effect)
        parent = chain[-1]['child_order_id'] if chain else None
        order = OrderState(child_id, position['id'] + '-' + effect.lower(), child_id, effect, qty, price,
                           parent_child_order_id=parent, ordinal=len(chain) + 1,
                           native_offset=offset, native_trading_day=self.scope['trading_day']).to_dict()
        order.update(contract_id=contract, direction=side, position_id=position['id'], native_id=None, cancel_reason=None)
        state['orders'][child_id] = order
        position['children'].append(child_id)
        return self._prepare(order, 'SEND', quantity=qty, limit_price=price, native_offset=offset, direction=side)

    def cancel(self, order, reason):
        if order['terminal_confirmed'] or order['cancel_status'] or order['send_state'] != 'NATIVE_SEEN':
            return None
        order.update(cancel_status='SEND_POSSIBLE', cancel_reason=reason)
        return self._prepare(order, 'CANCEL', native_order_id=order['native_id'], reason=reason)

    def _prepare(self, order, kind, **parameters):
        effect = {'id': order['child_order_id'] + '-' + kind.lower(), 'kind': kind,
                  'child_order_id': order['child_order_id'], 'contract_id': order['contract_id'],
                  'account_key': self.scope['account_key'], 'provider': self.scope['provider'],
                  'trading_day': self.scope['trading_day'], **parameters}
        self.journal.prepare(effect)
        return effect

    @staticmethod
    def _proven_position(position, market):
        own = 'long_qty' if position['direction'] == 'LONG' else 'short_qty'
        other = 'short_qty' if own == 'long_qty' else 'long_qty'
        return market[own] == position['owned'] and not market[other] and not market.get('external_orders')

    @staticmethod
    def _desired(position, market, effect, wall_time):
        tick = market['tick']
        direction = position['direction']
        if effect == 'OPEN':
            mode = position['mode']
            if mode != 'ACTIVE' and wall_time - float(position['context_at']) > 90:
                mode = 'ACTIVE'
            reference = position['theory'] if mode == 'PASSIVE' else position['cap']
        else:
            direction = 'SHORT' if direction == 'LONG' else 'LONG'
            mode = position.get('close_mode')
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
                return legal_price(tick, raw)
            mode, reference = 'ACTIVE', None
        return legal_price(tick, entry_price(tick, direction, mode, None if reference is None else float(reference)))

    def plan(self, state, row, market, wall_time):
        position = row['position']
        tick = market.get('tick')
        if position is None or tick is None:
            return []
        proven_position = self._proven_position(position, market)
        effects = []
        for effect in ('OPEN', 'REDUCE'):
            previous = self.child_orders(state, position, effect)
            outstanding = [order for order in previous if not terminal(order)]
            opening = effect == 'OPEN'
            if opening:
                needed = 0 if position['entry_stopped'] else position['quantity'] - position['entry_filled']
                if position.get('preserve_open_on_stop') and position['exit_target'] is None:
                    continue
            else:
                needed = 0 if position['exit_target'] is None else position['owned'] - position['exit_target']
            desired = self._desired(position, market, effect, wall_time) if needed > 0 else None
            for order in outstanding:
                pending = order['original_qty'] - order['filled_qty']
                reason = 'TARGET_REDUCED' if pending > max(0, needed) else None
                if reason is None and proven_position and pending and desired is not None:
                    current = Decimal(order['limit_price'])
                    proposed = Decimal(desired)
                    worse = proposed > current if order['direction'] == 'BUY' else proposed < current
                    if worse:
                        reason = 'REPRICE'
                if reason and (request := self.cancel(order, reason)):
                    effects.append(request)
            if not proven_position or outstanding or desired is None or needed <= 0:
                continue
            maximum = market.get('max_order_qty', 2_147_483_647)
            if opening and not market.get('min_order_qty', 1) <= needed <= maximum:
                position['entry_stopped'] = True
                continue
            # Reconciliation proves completion independently of the result of the cancel call.
            if previous and previous[-1]['filled_qty'] < previous[-1]['original_qty']:
                old = previous[-1]
                allowed_retry = old['cancel_reason'] in {'REPRICE', 'TARGET_REDUCED'}
                if old['broker_state'] != 'CANCELLED' or not allowed_retry:
                    continue
            effects.append(self.new_order(state, position, market, effect, min(needed, maximum), desired))
        self.settle(state, row)
        return effects

    def settle(self, state, row):
        position = row['position']
        if position is None:
            return
        if position['owned'] == 0 and position['entry_stopped'] and not position['children']:
            row['position'] = None
            return
        if (position['owned'] == 0 and position['children']
                and all(terminal(order) for order in self.child_orders(state, position))):
            opens = self.child_orders(state, position, 'OPEN')
            if (position['entry_filled'] or position['entry_stopped'] or
                    opens[-1]['cancel_reason'] != 'REPRICE' or opens[-1]['broker_state'] != 'CANCELLED'):
                row['position'] = None

    def dispatch(self, state, effects):
        for effect in effects:
            order = state['orders'][effect['child_order_id']]
            sender = self.port.send if effect['kind'] == 'SEND' else self.port.cancel
            try:
                sender(dict(effect))
            except Exception:
                result = 'UNKNOWN'
            else:
                result = 'CALL_RETURNED'
            if effect['kind'] == 'SEND':
                order['send_state'] = result
            else:
                order['cancel_status'] = result
            try:
                with self.journal.transaction():
                    self.journal.resolve(effect, result)
                    self.journal.event('EFFECT_RESULT', {'id': effect['id'], 'result': result})
                    self.journal.save(state)
            except sqlite3.Error as error:
                raise TraderError('E_DSV_STORE', 'Dispatch outcome could not be committed; prepared effect requires reconciliation') from error
