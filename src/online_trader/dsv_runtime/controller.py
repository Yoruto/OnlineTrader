"""Plan explicit child orders and reconcile only bound native facts."""

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import logging
import sqlite3
import time
from uuid import uuid4
from typing import NamedTuple

from online_trader.domain.orders import OrderState
from online_trader.domain.values import decimal_text, quantity
from online_trader.dsv_core.price_rules import strategy_price
from online_trader.dsv_core.transition import entry_price, price_value
from online_trader.dsv_runtime.market_permissions import infer_offline_market_permission
from online_trader.dsv_runtime.order_diagnostics import OrderDiagnosticTracker
from online_trader.dsv_runtime.target_execution import (
    account_target,
    contract_pending_summary,
    effective_position_target,
)
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
        raise TraderError(
            'E_DSV_NATIVE_FACT',
            f'Native evidence contradicts a locally proved unsent child ({order["contract_id"]} {order["child_order_id"]} -> {native})',
        )
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
        self._diagnostics = OrderDiagnosticTracker(account_key=scope.get('account_key'))

    def _diag(self):
        tracker = getattr(self, '_diagnostics', None)
        if tracker is None:
            scope = getattr(self, 'scope', {}) or {}
            tracker = OrderDiagnosticTracker(account_key=scope.get('account_key'))
            self._diagnostics = tracker
        return tracker

    def confirm_absent(self, state, child_id, *, frame):
        """Release an UNKNOWN child after a complete broker snapshot proves absence."""
        order = state['orders'].get(child_id)
        if order is None:
            raise TraderError('E_DSV_NATIVE_ID', 'Negative reconciliation references an unknown child')
        if (order['send_state'] != 'UNKNOWN' or order.get('native_id') is not None
                or order['filled_qty'] or order['terminal_confirmed']):
            raise TraderError('E_DSV_NATIVE_FACT', 'Negative reconciliation contradicts an active or bound child')
        order['send_state'] = 'DEFINITELY_NOT_SENT'
        order['local_send_rejection'] = {
            'effect_id': child_id + '-send',
            'stage': 'BROKER_NEGATIVE_RECONCILIATION',
            'error_code': 'E_DSV_BROKER_ORDER_ABSENT',
            'rejection_reason': 'BROKER_NEGATIVE_RECONCILED',
            'frame_id': frame['id'],
            'sequence': frame['sequence'],
        }
        if order['effect'] == 'OPEN':
            row = state['symbols'].get(order['contract_id'])
            position = None if row is None else row.get('position')
            if position is not None and position['id'] == order['position_id']:
                position.update(entry_stopped=True, preserve_open_on_stop=False,
                                execution_reason='BROKER_NEGATIVE_RECONCILED')
        self.journal.event('UNKNOWN_NEGATIVE_RECONCILED', {
            'child_order_id': child_id,
            'contract_id': order['contract_id'],
            'effect': order['effect'],
            'frame_id': frame['id'],
            'sequence': frame['sequence'],
        })
        self._diag().emit(
            'UNKNOWN_NEGATIVE_RECONCILED',
            level=logging.INFO,
            contract_id=order['contract_id'],
            position_id=order['position_id'],
            child_order_id=child_id,
            effect=order['effect'],
            reason='BROKER_NEGATIVE_RECONCILED',
            sequence=frame['sequence'],
        )

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
                raise TraderError(
                    'E_DSV_NATIVE_FACT',
                    f'Native contract, side or offset differs from the child ({order["contract_id"]} {child_id})',
                )
            if fact['kind'] == 'LOCAL_ORDER':
                local = _native_id(fact['native_order_id'])
                if (order['send_state'] != 'CALL_RETURNED' or order['native_id'] is not None
                        or order.get('local_order_id', local) != local):
                    raise TraderError(
                        'E_DSV_NATIVE_FACT',
                        f'Local SDK order must match the original returned send ({order["contract_id"]} {child_id}: {local})',
                    )
                order['local_order_id'] = local
                continue
            native_id = _bind_native(order, fact['native_order_id'])
            order.pop('local_order_id', None)
            if fact['kind'] == 'ORDER':
                if stamp := fact.get('source_time_ns'):
                    prior_stamp = order.get('native_order_time_ns')
                    if prior_stamp is not None and prior_stamp != stamp:
                        skew_ns = abs(int(stamp) - int(prior_stamp))
                        # CTP/SimNow OnRtnOrder packets can adjust InsertTime across front/exchange acks.
                        if skew_ns > 60_000_000_000:
                            raise TraderError(
                                'E_DSV_NATIVE_FACT',
                                f'Original native order timestamp changed ({order["contract_id"]} {native_id}: {prior_stamp} -> {stamp})',
                            )
                        order['native_order_adjusted_time_ns'] = stamp
                        self.journal.event('NATIVE_ORDER_CLOCK_SKEW', {
                            'child_order_id': child_id,
                            'native_order_id': native_id,
                            'prior_time_ns': str(prior_stamp),
                            'incoming_time_ns': str(stamp),
                        })
                    else:
                        order['native_order_time_ns'] = stamp
                prior_state = (order.get('broker_state'), order.get('filled_qty'), order.get('terminal_confirmed'))
                self._order_update(order, fact)
                if self.journal.record_order(child_id, native_id, order):
                    self.journal.event('NATIVE_ORDER', fact)
                current_state = (order.get('broker_state'), order.get('filled_qty'), order.get('terminal_confirmed'))
                if current_state != prior_state:
                    self._diag().emit(
                        'ORDER_NATIVE_UPDATE',
                        level=logging.INFO,
                        component='ORDER',
                        priority='HIGH' if order.get('terminal_confirmed') else 'NORMAL',
                        log_to_logger=False,
                        contract_id=order['contract_id'],
                        position_id=order['position_id'],
                        child_order_id=child_id,
                        native_order_id=native_id,
                        effect=order['effect'],
                        broker_state=order['broker_state'],
                        filled_qty=order['filled_qty'],
                        original_qty=order['original_qty'],
                        terminal_confirmed=order['terminal_confirmed'],
                        category=order.get('broker_rejection_category'),
                        reason=order.get('broker_rejection_detail'),
                    )
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
            raise TraderError(
                'E_DSV_NATIVE_FACT',
                f'Invalid native order status or cumulative fill ({order["contract_id"]} {order["child_order_id"]}: {status}/{filled})',
            )
        if order['terminal_confirmed']:
            if status == 'ALIVE' and filled <= order['reported_fill_qty']:
                return
            if order['broker_state'] == 'CANCELLED' and status in {'CANCELLED', 'FILLED'} and filled > order['reported_fill_qty']:
                order.update(broker_state=status, reported_fill_qty=filled, terminal_conflict=True)
                return
            if (order['broker_state'], status) in {('CANCELLED', 'REJECTED'), ('REJECTED', 'CANCELLED')} and filled == order['reported_fill_qty'] == 0:
                if status == 'REJECTED':
                    order['broker_state'] = 'REJECTED'
                    if isinstance(fact.get('native_message'), str) and fact['native_message']:
                        order['native_message'] = fact['native_message'][:200]
                    if fact.get('rejection_category') is not None:
                        order['broker_rejection_category'] = fact['rejection_category']
                    if fact.get('rejection_detail') is not None:
                        order['broker_rejection_detail'] = fact['rejection_detail']
                return
            if (status, filled) != (order['broker_state'], order['reported_fill_qty']):
                raise TraderError(
                    'E_DSV_NATIVE_FACT',
                    f'Conflicting terminal order evidence ({order["contract_id"]} {order.get("native_id") or order["child_order_id"]}: '
                    f'{order["broker_state"]}/{order["reported_fill_qty"]} -> {status}/{filled})',
                )
            return
        if status == 'ALIVE' and filled < max(order['filled_qty'], order['reported_fill_qty'] or 0):
            return
        if status != 'ALIVE' and filled < max(order['filled_qty'], order['reported_fill_qty'] or 0):
            raise TraderError(
                'E_DSV_NATIVE_FACT',
                f'Terminal quantity contradicts accepted fills ({order["contract_id"]} {order.get("native_id") or order["child_order_id"]}: {filled} < {max(order["filled_qty"], order["reported_fill_qty"] or 0)})',
            )
        order.update(broker_state=status, terminal_confirmed=status != 'ALIVE', reported_fill_qty=filled)
        if isinstance(fact.get('native_message'), str) and fact['native_message']:
            order['native_message'] = fact['native_message'][:200]
        if fact.get('rejection_category') is not None:
            order['broker_rejection_category'] = fact['rejection_category']
        if fact.get('rejection_detail') is not None:
            order['broker_rejection_detail'] = fact['rejection_detail']
        if order.get('terminal_conflict') and terminal(order):
            order.pop('terminal_conflict')

    def _emit_trade_fill(self, order, native_id, trade_id, qty, price, owned_after=None):
        self._diag().emit(
            'TRADE_FILL',
            level=logging.INFO,
            component='EXECUTION',
            priority='HIGH',
            log_to_logger=False,
            contract_id=order['contract_id'],
            position_id=order['position_id'],
            child_order_id=order['child_order_id'],
            native_order_id=native_id,
            native_trade_id=trade_id,
            effect=order['effect'],
            direction=order['direction'],
            quantity=qty,
            price=str(price),
            owned_after=owned_after,
        )

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
            anchor_time_ns = int(order['native_order_time_ns'])
            adjusted_time_ns = int(order.get('native_order_adjusted_time_ns', anchor_time_ns))
            # Match sdk_adapter's latest accepted OnRtnOrder timestamp when CTP/SimNow
            # adjusted InsertTime earlier within the 60s NATIVE_ORDER_CLOCK_SKEW bound.
            effective_order_time_ns = min(anchor_time_ns, adjusted_time_ns) if order_day == fill_day else anchor_time_ns
            if int(fact['source_time_ns']) + skew_limit_ns < effective_order_time_ns:
                raise TraderError(
                    'E_DSV_NATIVE_FACT',
                    f'Native trade precedes its original order timestamp ({order["contract_id"]} {native_id}/{trade_id}: '
                    f'{fact["source_time_ns"]} < {effective_order_time_ns})',
                )
            if int(fact['source_time_ns']) < anchor_time_ns:
                self.journal.event('NATIVE_TRADE_CLOCK_SKEW', {
                    'child_order_id': order['child_order_id'],
                    'native_order_id': native_id,
                    'native_trade_id': trade_id,
                    'order_time_ns': str(order['native_order_time_ns']),
                    'effective_order_time_ns': str(effective_order_time_ns),
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
        if order.get('convergence_order'):
            if order['filled_qty'] + qty > order['original_qty']:
                raise TraderError('E_DSV_NATIVE_FACT', 'Trade would exceed original child quantity')
            order['filled_qty'] += qty
            fills = order.setdefault('convergence_fills', [])
            fills.append({
                'event_seq': max(len(fills) + 1, int(state.get('sequence') or 0)),
                'contract_id': order['contract_id'],
                'child_order_id': order['child_order_id'],
                'direction': order['convergence_side'],
                'trade_key': [order_day, native_id, trade_id],
                'native_trade_id': trade_id,
                'source_time_ns': fact.get('source_time_ns'),
                'trading_day': fill_day,
                'quantity': qty,
                'price': fact['price'],
                'effect': 'REDUCE',
                'offset_kind': order['native_offset'],
                'position_cycle_id': order['position_id'],
                'order_source': 'CONVERGENCE',
            })
            if order.get('terminal_conflict') and terminal(order):
                order.pop('terminal_conflict')
            self.journal.event('NATIVE_TRADE', fact)
            self._emit_trade_fill(order, native_id, trade_id, qty, fact['price'])
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
        self._emit_trade_fill(order, native_id, trade_id, qty, fact['price'], owned_after=position['owned'])

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
        record = {
            'event_seq': seq, 'contract_id': order['contract_id'],
            'child_order_id': order['child_order_id'],
            'direction': position['direction'],
            'trade_key': [order['native_trading_day'], fact['native_order_id'], fact['native_trade_id']],
            'trading_day': fill_day, 'quantity': fact['quantity'], 'price': fact['price'],
            'effect': order['effect'],
            'offset_kind': order['native_offset'],
        }
        if fact.get('native_trade_id') is not None:
            record['native_trade_id'] = fact['native_trade_id']
        if 'source_time_ns' in fact:
            record['source_time_ns'] = fact['source_time_ns']
        position['fills'].append(record)

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
        effect_record = self._prepare(order, 'SEND', quantity=qty, limit_price=price, native_offset=offset, direction=side)
        self._diag().emit(
            'ORDER_PREPARED',
            level=logging.INFO,
            component='ORDER',
            priority='HIGH',
            log_to_logger=False,
            contract_id=contract,
            position_id=position['id'],
            child_order_id=child_id,
            effect_id=effect_record['id'],
            effect=effect,
            direction=side,
            quantity=qty,
            limit_price=str(price),
            native_offset=offset,
            sequence=state.get('sequence'),
        )
        return effect_record

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
        order['cancel_seq'] = order.get('cancel_seq', order.get('cancel_attempt', 0)) + 1
        order['cancel_attempt'] = order.get('cancel_attempt', 0) + 1
        order.update(cancel_status='SEND_POSSIBLE', cancel_reason=reason)
        effect_record = self._prepare(order, 'CANCEL', native_order_id=identity, reason=reason)
        self._diag().emit(
            'ORDER_CANCEL_REQUESTED',
            level=logging.INFO,
            component='ORDER',
            priority='HIGH',
            log_to_logger=False,
            contract_id=order['contract_id'],
            position_id=order['position_id'],
            child_order_id=order['child_order_id'],
            effect_id=effect_record['id'],
            native_order_id=identity,
            effect=order['effect'],
            reason=reason,
            cancel_reason=reason,
            attempt=order['cancel_attempt'],
        )
        return effect_record

    def _prepare(self, order, kind, **parameters):
        effect_id = order['child_order_id'] + '-' + kind.lower()
        cancel_seq = order.get('cancel_seq', order.get('cancel_attempt', 1))
        if kind == 'CANCEL' and cancel_seq > 1:
            effect_id += '-' + str(cancel_seq)
        effect = {'id': effect_id, 'kind': kind,
                  'child_order_id': order['child_order_id'], 'contract_id': order['contract_id'],
                  'account_key': self.scope['account_key'], 'provider': self.scope['provider'],
                  'trading_day': order['native_trading_day'], **parameters}
        if kind == 'CANCEL':
            effect['cancel_attempt'] = order['cancel_attempt']
            effect['cancel_seq'] = cancel_seq
            order['cancel_effect_id'] = effect_id
        self.journal.prepare(effect)
        return effect

    @staticmethod
    def _position_proof_reason(state, position, market, *, for_effect=None):
        own = 'long_qty' if position['direction'] == 'LONG' else 'short_qty'
        other = 'short_qty' if own == 'long_qty' else 'long_qty'
        if market.get('standalone_external_model'):
            if for_effect == 'REDUCE':
                if market[own] < position['owned']:
                    return f'OWN_QTY_UNDERCOVERED({position["owned"]}>{market[own]})'
                if not position['owned']:
                    return None
                routes, reason = ExplicitOrderController._reduction_routes_with_reason(
                    state, position, market, position['owned'], include_pending_orders=False)
                return None if routes else (reason or 'REDUCTION_ROUTES_EMPTY')
            if market[own] != position['owned']:
                return f'OWN_QTY_MISMATCH({position["owned"]}!={market[own]})'
            if market[other]:
                return f'OPPOSITE_SIDE_PRESENT({market[other]})'
            if market.get('external_active_orders'):
                return 'EXTERNAL_ACTIVE_ORDERS'
            if not position['owned']:
                return None
            routes, reason = ExplicitOrderController._reduction_routes_with_reason(
                state, position, market, position['owned'], include_pending_orders=False)
            return None if routes else (reason or 'REDUCTION_ROUTES_EMPTY')
        if market[own] != position['owned']:
            return f'OWN_QTY_MISMATCH({position["owned"]}!={market[own]})'
        if market[other]:
            return f'OPPOSITE_SIDE_PRESENT({market[other]})'
        if market.get('external_orders'):
            return 'EXTERNAL_ORDERS'
        if not position['owned']:
            return None
        routes, reason = ExplicitOrderController._reduction_routes_with_reason(
            state, position, market, position['owned'], include_pending_orders=False)
        return None if routes else (reason or 'REDUCTION_ROUTES_EMPTY')

    @staticmethod
    def proves_position(state, position, market, *, for_effect=None):
        return ExplicitOrderController._position_proof_reason(
            state, position, market, for_effect=for_effect) is None

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
    def _reduction_routes_with_reason(state, position, market, needed, *, include_pending_orders=True):
        day = state['trading_day']
        side = position['direction'].lower()
        today = market.get(f'{side}_today')
        historical = market.get(f'{side}_historical')
        if today is None or historical is None:
            if position['open_day'] != day or any(fill['trading_day'] != day for fill in position['fills']):
                return [], 'MISSING_TODAY_HIS_BUCKETS'
            today, historical = position['owned'], 0
        fills = ExplicitOrderController._position_fills(position, market['contract_id'])
        pending_orders = []
        target_quantity = needed
        native_today, native_hist = today, historical
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
            # Account for active convergence close orders on the same contract/side
            # so strategy reductions and convergence closes never double-spend broker buckets.
            close_dir = 'SELL' if position['direction'] == 'LONG' else 'BUY'
            conv_today = conv_hist = 0
            for order in state['orders'].values():
                if (order.get('convergence_order')
                        and order.get('contract_id') == market['contract_id']
                        and order.get('effect') == 'REDUCE'
                        and order.get('direction') == close_dir
                        and not terminal(order)):
                    rem = max(0, int(order['original_qty']) - int(order['filled_qty']))
                    if order.get('native_offset') == 'CLOSETODAY':
                        conv_today += rem
                    else:
                        conv_hist += rem
            if conv_today or conv_hist:
                if market['contract_id'].split('.')[0] in {'SHFE', 'INE'}:
                    native_today = max(0, today - conv_today)
                    native_hist = max(0, historical - conv_hist)
                else:
                    rem_comb = conv_today + conv_hist
                    take_hist = min(native_hist, rem_comb)
                    native_hist -= take_hist
                    rem_comb -= take_hist
                    native_today = max(0, native_today - rem_comb)
        try:
            result = close_routes(contract_id=market['contract_id'], direction=position['direction'], current_day=day,
                                  fills=fills, native={'today': native_today, 'historical': native_hist,
                                                       'total': native_today + native_hist},
                                  orders=pending_orders, target_quantity=target_quantity,
                                  allow_external=bool(market.get('standalone_external_model')),
                                  historical_first_on_combined=True)
        except TraderError as error:
            if error.code in {'E_OWNERSHIP', 'E_FACT_BARRIER'}:
                return [], error.code
            raise
        if not result['routes']:
            return [], 'EMPTY_CLOSE_ROUTES'
        return result['routes'], None

    @staticmethod
    def _reduction_routes(state, position, market, needed, *, include_pending_orders=True):
        routes, _ = ExplicitOrderController._reduction_routes_with_reason(
            state, position, market, needed, include_pending_orders=include_pending_orders)
        return routes

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
                rejected_retry = self._retry_broker_exit_failure(
                    position, last, market, newer_target, wall_time=wall_time)
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

    def _retry_broker_exit_failure(self, position, order, market, newer_target, *, wall_time=None):
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
        category = order.get('broker_rejection_category') or (
            'TRANSIENT' if is_exchange_cancel else 'UNCLASSIFIED')
        detail = order.get('broker_rejection_detail') or (
            'EXCHANGE_CANCELLED' if is_exchange_cancel else 'UNCLASSIFIED_BROKER_REJECT')
        if not order.get('exit_failure_counted'):
            order['exit_failure_counted'] = True
            if wall_time is not None:
                order['exit_failure_at'] = str(wall_time)
            position['consecutive_exit_rejections'] = position.get('consecutive_exit_rejections', 0) + 1
            self._diag().emit(
                'EXIT_FAILURE_CLASSIFIED',
                contract_id=order['contract_id'],
                position_id=position['id'],
                child_order_id=order['child_order_id'],
                effect='REDUCE',
                broker_state=order['broker_state'],
                category=category,
                detail=detail,
                attempt=position['consecutive_exit_rejections'],
            )
        count = position.get('consecutive_exit_rejections', 0)
        if category == 'STRUCTURAL':
            position['exit_manual_takeover'] = True
            position['execution_reason'] = 'EXIT_MANUAL_TAKEOVER_REQUIRED'
            return False
        if category == 'TRANSIENT':
            if count > 20:
                position['exit_manual_takeover'] = True
                position['execution_reason'] = 'EXIT_MANUAL_TAKEOVER_REQUIRED'
                return False
            schedule = (0, 1, 2, 4, 8, 15, 30)
            delay = schedule[min(max(0, count - 1), len(schedule) - 1)]
            failed_at = order.get('exit_failure_at')
            if wall_time is not None and failed_at is not None and float(wall_time) - float(failed_at) < delay:
                position['execution_reason'] = 'EXIT_TRANSIENT_RETRY_BACKOFF'
                return False
            fresh = self._has_fresh_quote_for_order(order, market)
            if not fresh:
                position['execution_reason'] = 'EXIT_WAIT_FRESH_QUOTE'
            return fresh
        if count >= 3:
            position['exit_manual_takeover'] = True
            position['execution_reason'] = 'EXIT_MANUAL_TAKEOVER_REQUIRED'
            return False
        delay = 0 if count <= 1 else 1
        failed_at = order.get('exit_failure_at')
        if wall_time is not None and failed_at is not None and float(wall_time) - float(failed_at) < delay:
            position['execution_reason'] = 'EXIT_TRANSIENT_RETRY_BACKOFF'
            return False
        fresh = self._has_fresh_quote_for_order(order, market)
        if not fresh:
            position['execution_reason'] = 'EXIT_WAIT_FRESH_QUOTE'
        return fresh

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
        elif (opening.needed_qty or reducing.needed_qty) and position.get('execution_reason') in {
                'POSITION_CHANGED', 'NO_LEGAL_PRICE', 'EXIT_MANUAL_TAKEOVER_REQUIRED',
                'E_ENTRY_PRICE_CAP', 'REDUCTION_ROUTE_UNAVAILABLE'}:
            status, reason = 'BLOCKED', position['execution_reason']
        elif opening.pending_qty or reducing.pending_qty:
            status, reason = 'WORKING', 'WORKING_ORDER_COVERS_TARGET'
        elif opening.needed_qty or reducing.needed_qty:
            reason = position.get('execution_reason') or 'TARGET_GAP'
            status = 'READY'
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
        allow_cancel = market.get('allow_cancel', True)
        position = row['position']
        prior = [
            order for order in state['orders'].values()
            if order['contract_id'] == market['contract_id'] and not terminal(order)
            and not order.get('convergence_order')
            and (position is None or order['position_id'] != position['id'])]
        if prior:
            if not allow_cancel:
                return [], False
            current = [] if position is None else self.child_orders(state, position)
            return [request for order in prior + current if order['effect'] == 'OPEN' and not order.get('terminal_conflict')
                    and (request := self.cancel(order, 'TARGET_REDUCED'))], False
        if row.get('detached_positions') or row.get('native_conflicts'):
            if not allow_cancel:
                return [], False
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
            if not allow_cancel:
                return [], False
            return [request for order in children if not order.get('terminal_conflict')
                    and (request := self.cancel(order, 'TARGET_REDUCED'))], False
        effects = []
        for effect in ('OPEN', 'REDUCE'):
            accounting = self.target_accounting(state, position, effect)
            excess = accounting.excess_pending_qty
            if not excess:
                continue
            if not allow_cancel:
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
            contract_id = market['contract_id']
            diag = self._diag()
            block_key = ('controller', contract_id, effect)
            if opening and not market.get('allow_new_entry', market.get('allow_new_order', True)):
                accounting = self.target_accounting(state, position, effect)
                diag.observe_block(
                    block_key, active=accounting.needed_qty > 0, contract_id=contract_id,
                    position_id=position['id'], effect=effect, reason='ENTRY_NOT_ALLOWED',
                    detail='ALLOW_NEW_ENTRY_FALSE', needed_qty=accounting.needed_qty)
                continue
            if not opening and not market.get('allow_protective_exit', market.get('allow_new_order', True)):
                accounting = self.target_accounting(state, position, effect)
                diag.observe_block(
                    block_key, active=accounting.needed_qty > 0, contract_id=contract_id,
                    position_id=position['id'], effect=effect, reason='PROTECTIVE_EXIT_NOT_ALLOWED',
                    detail='ALLOW_PROTECTIVE_EXIT_FALSE', needed_qty=accounting.needed_qty)
                continue
            proven_position = self.proves_position(state, position, market, for_effect=effect)
            proof_reason = (None if proven_position else
                            self._position_proof_reason(state, position, market, for_effect=effect))
            previous = self.child_orders(state, position, effect)
            outstanding = [order for order in previous if not terminal(order)]
            accounting = self.target_accounting(state, position, effect)
            needed = accounting.needed_qty
            desired = self._desired(position, market, effect, wall_time)
            repricing = False
            can_reprice = (
                proven_position
                and desired is not None
                and market.get('allow_cancel', True)
                and (market.get('market_permission') or {}).get('continuous_matching', True)
            )
            if can_reprice:
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
                diag.observe_block(
                    block_key, active=needed > 0, contract_id=contract_id,
                    position_id=position['id'], effect=effect, reason='POSITION_CHANGED',
                    detail=proof_reason, needed_qty=needed)
            elif desired is None:
                position['execution_reason'] = 'NO_LEGAL_PRICE'
                diag.observe_block(
                    block_key, active=needed > 0, contract_id=contract_id,
                    position_id=position['id'], effect=effect, reason='NO_LEGAL_PRICE',
                    detail='TICK_OR_LIMIT_INVALID', needed_qty=needed)
            if (not proven_position or desired is None or accounting.cancel_pending_children
                    or accounting.uncertain_children or repricing):
                if proven_position and desired is not None and needed > 0:
                    if accounting.uncertain_children:
                        diag.observe_block(
                            block_key, active=True, contract_id=contract_id,
                            position_id=position['id'], effect=effect, reason='SEND_RESULT_UNKNOWN',
                            detail=','.join(accounting.uncertain_children), needed_qty=needed)
                    elif accounting.cancel_pending_children or repricing:
                        diag.observe_block(
                            block_key, active=True, contract_id=contract_id,
                            position_id=position['id'], effect=effect,
                            reason='PRICE_REPRICE_REQUIRED' if repricing else 'CANCEL_PENDING',
                            detail=','.join(accounting.cancel_pending_children) or None,
                            needed_qty=needed)
                continue
            if needed <= 0:
                diag.observe_block(block_key, active=False, contract_id=contract_id,
                                   position_id=position['id'], effect=effect)
                continue
            maximum = market.get('max_order_qty', 2_147_483_647)
            minimum = market.get('min_order_qty', 1)
            if opening and not minimum <= needed <= maximum:
                position['entry_stopped'] = True
                position['execution_reason'] = 'QUANTITY_OUT_OF_RANGE'
                diag.observe_block(
                    block_key, active=True, contract_id=contract_id,
                    position_id=position['id'], effect=effect, reason='QUANTITY_OUT_OF_RANGE',
                    detail=f'needed={needed},min={minimum},max={maximum}', needed_qty=needed)
                continue
            # Reconciliation proves completion independently of the result of the cancel call.
            if previous and not outstanding and previous[-1]['filled_qty'] < previous[-1]['original_qty']:
                old = previous[-1]
                allowed_retry = old['cancel_reason'] in {'REPRICE', 'TARGET_REDUCED'}
                rolled_exit = not opening and old['native_trading_day'] != state['trading_day']
                newer_target = not opening and position.get('exit_revision', 0) > old.get('exit_revision', 0)
                local_retry = self._retry_unsent_open(old, market) if opening else self._retry_unsent_exit(old, market, newer_target)
                rejected_retry = not opening and self._retry_broker_exit_failure(
                    position, old, market, newer_target, wall_time=wall_time)
                if not rejected_retry and not local_retry and (old['broker_state'] != 'CANCELLED' or not (
                        allowed_retry or newer_target or rolled_exit and old['cancel_reason'] != 'USER_CANCEL')):
                    block_reason = position.get('execution_reason') or (
                        'OPEN_NOT_RETRYABLE' if opening else 'EXIT_RETRY_BLOCKED')
                    local_reason = (old.get('local_send_rejection') or {}).get('rejection_reason')
                    block_detail = (
                        f"send_state={old['send_state']},broker_state={old['broker_state']},"
                        f"cancel_reason={old['cancel_reason']},local_reason={local_reason},"
                        f"broker_detail={old.get('broker_rejection_detail')}"
                    )
                    diag.observe_block(
                        block_key, active=True, contract_id=contract_id,
                        position_id=position['id'], child_order_id=old['child_order_id'],
                        effect=effect, reason=block_reason, detail=block_detail, needed_qty=needed)
                    continue
                if opening and local_retry and position.get('cap') is not None:
                    cap = Decimal(str(position['cap']))
                    proposed = Decimal(desired)
                    exceeds = proposed > cap if position['direction'] == 'LONG' else proposed < cap
                    if exceeds:
                        position.update(entry_stopped=True, preserve_open_on_stop=False,
                                        execution_reason='E_ENTRY_PRICE_CAP')
                        diag.observe_block(
                            block_key, active=True, contract_id=contract_id,
                            position_id=position['id'], child_order_id=old['child_order_id'],
                            effect=effect, reason='E_ENTRY_PRICE_CAP',
                            detail=f'proposed={desired},cap={position["cap"]}', needed_qty=needed)
                        continue
            if opening:
                diag.observe_block(block_key, active=False, contract_id=contract_id,
                                   position_id=position['id'], effect=effect)
                effects.append(self.new_order(state, position, market, effect, needed, desired, 'OPEN'))
            else:
                routes = self._reduction_routes(state, position, market, needed)
                if routes:
                    diag.observe_block(block_key, active=False, contract_id=contract_id,
                                       position_id=position['id'], effect=effect)
                    route = routes[0]
                    previous_offsets = {order['native_offset'] for order in previous}
                    if previous_offsets and route['native_offset'] not in previous_offsets:
                        position['execution_reason'] = 'OFFSET_ROUTE_CHANGED'
                    effects.append(self.new_order(state, position, market, effect,
                                                  min(route['quantity'], maximum), desired, route['native_offset']))
                else:
                    _, route_reason = self._reduction_routes_with_reason(state, position, market, needed)
                    position['execution_reason'] = 'REDUCTION_ROUTE_UNAVAILABLE'
                    diag.observe_block(
                        block_key, active=True, contract_id=contract_id,
                        position_id=position['id'], effect=effect,
                        reason='REDUCTION_ROUTE_UNAVAILABLE', detail=route_reason or 'EMPTY_CLOSE_ROUTES',
                        needed_qty=needed)
        return effects

    def plan(self, state, row, market, wall_time):
        if row.get('position') is not None:
            row['position'].pop('execution_reason', None)
        maintenance, can_price = self.plan_maintenance(state, row, market)
        contract_id = market['contract_id']
        diag = self._diag()
        pos = row.get('position')
        allow_any = (market.get('allow_new_order', True)
                     or market.get('allow_new_entry', False)
                     or market.get('allow_protective_exit', False))
        if pos is not None:
            pending_needed = {
                eff: self.target_accounting(state, pos, eff).needed_qty
                for eff in ('OPEN', 'REDUCE')
            }
            if can_price and market.get('tick') is None and any(pending_needed.values()):
                pos['execution_reason'] = 'NO_LEGAL_PRICE'
                diag.observe_block(
                    ('plan_gate', contract_id), active=True, contract_id=contract_id,
                    position_id=pos['id'], reason='NO_LEGAL_PRICE', detail='TICK_MISSING',
                    open_needed=pending_needed['OPEN'], reduce_needed=pending_needed['REDUCE'])
            elif not can_price and any(pending_needed.values()):
                diag.observe_block(
                    ('plan_gate', contract_id), active=True, contract_id=contract_id,
                    position_id=pos['id'], reason='MAINTENANCE_GATE_BLOCKED',
                    detail='PRIOR_OR_CONFLICT_OR_DAY_ROLLOVER',
                    open_needed=pending_needed['OPEN'], reduce_needed=pending_needed['REDUCE'])
            elif can_price and not allow_any and any(pending_needed.values()):
                diag.observe_block(
                    ('plan_gate', contract_id), active=True, contract_id=contract_id,
                    position_id=pos['id'], reason='ORDER_POLICY_DISALLOWED',
                    detail='ALLOW_NEW_ORDER_FALSE',
                    open_needed=pending_needed['OPEN'], reduce_needed=pending_needed['REDUCE'])
            else:
                diag.observe_block(('plan_gate', contract_id), active=False,
                                   contract_id=contract_id, position_id=pos['id'])
        else:
            diag.observe_block(('plan_gate', contract_id), active=False, contract_id=contract_id)
            for eff in ('OPEN', 'REDUCE'):
                diag.observe_block(('controller', contract_id, eff), active=False, contract_id=contract_id)
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

    @staticmethod
    def _convergence_limit_locked(tick, close_buy):
        if tick is None:
            return False
        last = tick.get('last')
        if price_value(last) is None:
            return False
        last_dec = Decimal(last)
        if close_buy:
            upper = tick.get('upper_limit')
            if price_value(upper) is not None and last_dec >= Decimal(upper) and price_value(tick.get('ask1')) is None:
                return True
        else:
            lower = tick.get('lower_limit')
            if price_value(lower) is not None and last_dec <= Decimal(lower) and price_value(tick.get('bid1')) is None:
                return True
        return False

    @staticmethod
    def _convergence_close_price(market, pos_side):
        ticks = market.get('ticks') or ()
        tick = market.get('tick') or (ticks[-1] if ticks else None)
        if tick is None:
            return None, 'DEFERRED_QUOTE_UNAVAILABLE'
        buy = pos_side == 'SHORT'
        if ExplicitOrderController._convergence_limit_locked(tick, buy):
            return None, 'DEFERRED_LIMIT_LOCKED'
        quote = tick.get('ask1' if buy else 'bid1')
        if price_value(quote) is None:
            quote = tick.get('last')
        if price_value(quote) is None:
            return None, 'DEFERRED_NO_LEGAL_PRICE'

        def positive(value):
            return Decimal(value) if price_value(value) is not None else None

        mode = market.get('close_price_mode') or 'MARKET'
        if mode not in {'AGGR1', 'MARKET'}:
            mode = 'MARKET'
        raw = strategy_price(
            'BUY' if buy else 'SELL',
            mode,
            opponent=positive(quote),
            tick=positive(tick.get('tick_size')),
            limit=positive(tick.get('upper_limit' if buy else 'lower_limit')),
            reference=None,
        )
        if mode == 'AGGR1':
            raw = _clamp_to_limits(tick, raw)
        price = legal_price(tick, raw)
        if price is None:
            return None, 'DEFERRED_NO_LEGAL_PRICE'
        return price, None

    def new_convergence_order(self, state, contract_id, pos_side, market, qty, price, offset, *, cond_fp=None):
        child_id = 'child-' + uuid4().hex
        side = 'BUY' if pos_side == 'SHORT' else 'SELL'
        chain = [
            o for o in state['orders'].values()
            if o.get('convergence_order') and o['contract_id'] == contract_id and o.get('convergence_side') == pos_side
        ]
        parent = chain[-1]['child_order_id'] if chain else None
        order = OrderState(
            child_id,
            f'conv-{contract_id}-{pos_side.lower()}',
            child_id,
            'REDUCE',
            qty,
            price,
            parent_child_order_id=parent,
            ordinal=len(chain) + 1,
            native_offset=offset,
            native_trading_day=state['trading_day'],
        ).to_dict()
        order.update(
            contract_id=contract_id,
            direction=side,
            position_id=f'conv-{contract_id}-{pos_side.lower()}',
            native_id=None,
            cancel_reason=None,
            convergence_order=True,
            convergence_side=pos_side,
            order_source='CONVERGENCE',
        )
        if cond_fp is not None:
            order['convergence_cond_fp'] = cond_fp
        ticks = market.get('ticks') or ()
        tick = market.get('tick') or (ticks[-1] if ticks else None)
        if tick is not None and tick.get('native_time') is not None:
            order['quote_at'] = tick['native_time']
        state['orders'][child_id] = order
        return self._prepare(
            order, 'SEND', quantity=qty, limit_price=price, native_offset=offset,
            direction=side, order_source='CONVERGENCE',
        )

    def _cancel_non_strategy_order(self, state, ext_order, *, perm=None):
        if perm is not None and not perm.get('can_cancel', False):
            return None
        native_id = str(ext_order['native_order_id'])
        cancels = state.setdefault('convergence_cancels', {})
        rec = cancels.get(native_id)
        if rec is not None:
            if rec.get('cancel_status') in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}:
                return None
            if rec.get('local_cancel_rejection', {}).get('error_code') == 'E_DSV_SDK_CANCEL_RISK':
                return None
            fail_count = rec.get('cancel_failure_count', rec.get('cancel_attempt', 0))
            if fail_count >= 3:
                return None
        seq = (0 if rec is None else rec.get('cancel_seq', rec.get('cancel_attempt', 0))) + 1
        fail_count = 0 if rec is None else rec.get('cancel_failure_count', rec.get('cancel_attempt', 0))
        effect_id = f'conv-cancel-{native_id}-{seq}'
        effect = {
            'id': effect_id,
            'kind': 'CANCEL',
            'child_order_id': f'ext-{native_id}',
            'contract_id': ext_order['contract_id'],
            'account_key': self.scope['account_key'],
            'provider': self.scope['provider'],
            'trading_day': state['trading_day'],
            'native_order_id': native_id,
            'external_order': True,
            'reason': 'CONVERGENCE_CANCEL_NON_STRATEGY',
            'cancel_attempt': fail_count + 1,
            'cancel_seq': seq,
        }
        self.journal.prepare(effect)
        cancels[native_id] = {
            'native_order_id': native_id,
            'contract_id': ext_order['contract_id'],
            'direction': ext_order.get('direction'),
            'native_offset': ext_order.get('native_offset'),
            'volume_left': ext_order.get('volume_left'),
            'cancel_status': 'SEND_POSSIBLE',
            'cancel_attempt': fail_count + 1,
            'cancel_seq': seq,
            'cancel_failure_count': fail_count,
            'cancel_effect_id': effect_id,
        }
        return effect

    @staticmethod
    def _convergence_deferred_status(perm):
        reason = (perm or {}).get('wait_reason')
        if reason in {
            'DEFERRED_AUCTION_ORDERING',
            'DEFERRED_AUCTION_MATCH',
            'DEFERRED_HALTED',
            'DEFERRED_STATUS_UNKNOWN',
        }:
            return reason
        return 'DEFERRED_SESSION_OR_QUOTE'

    @staticmethod
    def _convergence_cond_fp(offset, desired, free_today, free_hist, excess_qty, has_ext, perm, tick):
        return '|'.join(str(part) for part in (
            offset,
            desired,
            free_today,
            free_hist,
            excess_qty,
            int(bool(has_ext)),
            (perm or {}).get('market_phase') or 'UNKNOWN',
            '' if tick is None else tick.get('lower_limit', ''),
            '' if tick is None else tick.get('upper_limit', ''),
            '' if tick is None else tick.get('tick_size', ''),
        ))

    @staticmethod
    def _reconcile_convergence_cancels(state, non_strategy_orders):
        active_ext_ids = {str(item['native_order_id']) for item in (non_strategy_orders or ())}
        cancels = state.setdefault('convergence_cancels', {})
        for nid in [k for k in cancels if k not in active_ext_ids]:
            cancels.pop(nid, None)

    def reconcile_stopped_convergence(
        self, state, configured_markets, unconfigured_markets, non_strategy_orders, *, wall_iso=None,
    ):
        """Reconcile convergence responsibilities while DISARMED (no new opens/closes/reprices)."""
        self._reconcile_convergence_cancels(state, non_strategy_orders)
        effects = []
        perm_by_cid = {}
        for cid, market in (configured_markets or {}).items():
            perm_by_cid[cid] = market.get('market_permission') or infer_offline_market_permission(
                market, wall_iso, clock_unverified=bool(state.get('clock_recovery_required')))
        for market in (unconfigured_markets or ()):
            cid = market['contract_id']
            perm_by_cid[cid] = market.get('market_permission') or infer_offline_market_permission(
                market, wall_iso, clock_unverified=bool(state.get('clock_recovery_required')))
        for order in state['orders'].values():
            if not order.get('convergence_order') or terminal(order):
                continue
            perm = perm_by_cid.get(order['contract_id'])
            if perm is None or not perm.get('can_cancel', False):
                continue
            if req := self.cancel(order, 'USER_STOPPED'):
                effects.append(req)
        return effects

    def plan_convergence(self, state, configured_markets, unconfigured_markets, non_strategy_orders, wall_time,
                         *, wall_iso=None):
        """Plan non-blocking per-contract convergence orders and external cancellations (ARMED only)."""
        effects = []
        statuses = {}
        events = []
        if wall_iso is None and wall_time is not None:
            wall_iso = datetime.fromtimestamp(float(wall_time), tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        self._reconcile_convergence_cancels(state, non_strategy_orders)

        all_targets = []
        market_by_cid = {}
        for cid in sorted(configured_markets):
            m = configured_markets[cid]
            all_targets.append((cid, m, True))
            market_by_cid[cid] = m
        unconf_map = {m['contract_id']: m for m in (unconfigured_markets or ())}
        for cid in sorted(unconf_map):
            m = unconf_map[cid]
            all_targets.append((cid, m, False))
            market_by_cid[cid] = m

        perm_by_cid = {}
        for cid, market in market_by_cid.items():
            perm_by_cid[cid] = market.get('market_permission') or infer_offline_market_permission(
                market, wall_iso, clock_unverified=bool(state.get('clock_recovery_required')))

        ext_by_contract = {}
        for item in (non_strategy_orders or ()):
            cid = item['contract_id']
            ext_by_contract.setdefault(cid, []).append(item)

        # Cancel non-strategy active orders only when market permission permits cancelling.
        for cid in sorted(ext_by_contract):
            perm = perm_by_cid.get(cid)
            if perm is None or not perm.get('can_cancel', False):
                continue
            for ext_order in ext_by_contract[cid]:
                req = self._cancel_non_strategy_order(state, ext_order, perm=perm)
                if req is not None:
                    effects.append(req)
                    events.append({
                        'event': 'CONVERGENCE_ACTION',
                        'contract_id': cid,
                        'action': 'CANCEL_NON_STRATEGY_ORDER',
                        'native_order_id': req['native_order_id'],
                        'direction': ext_order.get('direction'),
                        'volume_left': ext_order.get('volume_left'),
                    })

        separated_exchanges = {'SHFE', 'INE'}
        all_orders = list(state['orders'].values())
        for cid, market, is_configured in all_targets:
            perm = perm_by_cid[cid]
            pos = state['symbols'][cid]['position'] if (is_configured and cid in state['symbols']) else None
            pos_orders = self.child_orders(state, pos) if pos is not None else ()
            target_long, target_short = effective_position_target(
                pos, orders=pos_orders, terminal=terminal)
            owned_long = pos['owned'] if (pos is not None and pos['direction'] == 'LONG') else 0
            owned_short = pos['owned'] if (pos is not None and pos['direction'] == 'SHORT') else 0
            actual_long = int(market.get('long_qty') or 0)
            actual_short = int(market.get('short_qty') or 0)
            # Desired-based excess/deficit for unified Account vs Desired reporting.
            excess_long = max(0, actual_long - target_long)
            excess_short = max(0, actual_short - target_short)
            deficit_long = max(0, target_long - actual_long)
            deficit_short = max(0, target_short - actual_short)
            # Convergence only closes holdings above strategy-owned quantity and
            # above active strategy target (e.g. when broker position snapshot
            # leads fill facts during an in-flight open).
            conv_excess_long = min(excess_long, max(0, actual_long - owned_long))
            conv_excess_short = min(excess_short, max(0, actual_short - owned_short))
            conv_orders = [
                o for o in all_orders
                if o.get('convergence_order') and o['contract_id'] == cid and not terminal(o)
            ]
            pending_summary = contract_pending_summary(all_orders, cid, terminal)
            has_ext = cid in ext_by_contract
            perm_meta = {
                'market_phase': perm.get('market_phase'),
                'evidence_state': perm.get('evidence_state'),
                'status_source': perm.get('status_source'),
                'continuous_matching': bool(perm.get('continuous_matching')),
                'can_open': bool(perm.get('can_open')),
                'can_close': bool(perm.get('can_close')),
                'can_cancel': bool(perm.get('can_cancel')),
            }
            qty_meta = {
                'actual_long': actual_long,
                'actual_short': actual_short,
                'target_long': target_long,
                'target_short': target_short,
                'excess_long': excess_long,
                'excess_short': excess_short,
                'deficit_long': deficit_long,
                'deficit_short': deficit_short,
                'pending_open_long': pending_summary['open_long'],
                'pending_open_short': pending_summary['open_short'],
                'pending_close_long': pending_summary['close_long'],
                'pending_close_short': pending_summary['close_short'],
            }
            if market.get('position_known') is False:
                statuses[cid] = {
                    'configured': is_configured,
                    'status': 'DEFERRED_FACTS_INCOMPLETE',
                    'wait_reason': 'POSITION_FACTS_UNAVAILABLE',
                    'failure_count': 0,
                    **qty_meta,
                    **perm_meta,
                }
                events.append({
                    'event': 'CONVERGENCE_DEFERRED',
                    'contract_id': cid,
                    'reason': 'POSITION_FACTS_UNAVAILABLE',
                })
                continue

            # Always check active convergence orders FIRST — even when conv_excess == 0 —
            # so redundant working close orders are cancelled promptly before alignment.
            excess_cancel_status = None
            excess_cancel_wait = None
            for pos_side, conv_excess in (('LONG', conv_excess_long), ('SHORT', conv_excess_short)):
                side_orders = [o for o in conv_orders if o.get('convergence_side') == pos_side]
                if not side_orders:
                    continue
                side_pending = sum(max(0, o['original_qty'] - o['filled_qty']) for o in side_orders)
                if side_pending <= conv_excess:
                    continue
                uncertain_side = [
                    o['child_order_id'] for o in side_orders
                    if o['send_state'] in {'SEND_POSSIBLE', 'UNKNOWN'}
                    or o.get('cancel_status') in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}
                ]
                if uncertain_side:
                    excess_cancel_status = (
                        'SEND_RESULT_UNKNOWN'
                        if any(o['send_state'] in {'SEND_POSSIBLE', 'UNKNOWN'} for o in side_orders)
                        else 'CANCELLING_EXCESS_CLOSE'
                    )
                    continue
                if perm.get('can_cancel', False):
                    over = side_pending - conv_excess
                    for order in reversed(side_orders):
                        if req := self.cancel(order, 'TARGET_REDUCED'):
                            effects.append(req)
                            events.append({
                                'event': 'CONVERGENCE_ACTION',
                                'contract_id': cid,
                                'action': 'CANCEL_EXCESS_CONVERGENCE_CLOSE',
                                'side': pos_side,
                                'child_order_id': order['child_order_id'],
                                'native_order_id': order.get('native_id'),
                            })
                        over -= max(0, order['original_qty'] - order['filled_qty'])
                        if over <= 0:
                            break
                    excess_cancel_status = 'CANCELLING_EXCESS_CLOSE'
                else:
                    excess_cancel_status = self._convergence_deferred_status(perm)
                    excess_cancel_wait = perm.get('wait_reason')

            if conv_excess_long == 0 and conv_excess_short == 0 and not has_ext:
                if excess_cancel_status is not None:
                    st_name = excess_cancel_status
                    st_wait = excess_cancel_wait
                elif conv_orders or pending_summary['close_long'] or pending_summary['close_short']:
                    st_name = 'CONVERGING_FILL_PENDING'
                    st_wait = None
                elif deficit_long or deficit_short or pending_summary['open_long'] or pending_summary['open_short']:
                    st_name = 'OPENING'
                    st_wait = None
                elif excess_long or excess_short:
                    st_name = 'CONVERGING_FILL_PENDING'
                    st_wait = None
                else:
                    st_name = 'ALIGNED'
                    st_wait = None
                statuses[cid] = {
                    'configured': is_configured,
                    'status': st_name,
                    'wait_reason': st_wait,
                    'failure_count': 0,
                    **qty_meta,
                    **perm_meta,
                }
                continue
            if has_ext:
                if not perm.get('can_cancel', False):
                    defer_status = self._convergence_deferred_status(perm)
                    defer_reason = perm.get('wait_reason') or 'DEFERRED_CANCEL_DISALLOWED'
                    statuses[cid] = {
                        'configured': is_configured,
                        'status': defer_status,
                        'wait_reason': defer_reason,
                        'failure_count': 0,
                        **qty_meta,
                        **perm_meta,
                    }
                    events.append({
                        'event': 'CONVERGENCE_DEFERRED',
                        'contract_id': cid,
                        'reason': defer_reason,
                        'market_phase': perm.get('market_phase'),
                        'pending_orders': len(ext_by_contract[cid]),
                    })
                else:
                    statuses[cid] = {
                        'configured': is_configured,
                        'status': 'CANCELLING_NON_STRATEGY_ORDERS',
                        'wait_reason': 'WAITING_NON_STRATEGY_CANCEL',
                        'failure_count': 0,
                        **qty_meta,
                        **perm_meta,
                    }
                    events.append({
                        'event': 'CONVERGENCE_DEFERRED',
                        'contract_id': cid,
                        'reason': 'WAITING_NON_STRATEGY_CANCEL',
                        'pending_orders': len(ext_by_contract[cid]),
                    })
                continue

            ticks = market.get('ticks') or ()
            if not perm.get('can_close', False) or not ticks:
                defer_status = excess_cancel_status or self._convergence_deferred_status(perm)
                defer_reason = excess_cancel_wait or perm.get('wait_reason') or 'OUTSIDE_SESSION_OR_NO_QUOTE'
                statuses[cid] = {
                    'configured': is_configured,
                    'status': defer_status,
                    'wait_reason': defer_reason,
                    'failure_count': 0,
                    **qty_meta,
                    **perm_meta,
                }
                events.append({
                    'event': 'CONVERGENCE_DEFERRED',
                    'contract_id': cid,
                    'reason': defer_reason,
                    'market_phase': perm.get('market_phase'),
                    'excess_long': excess_long,
                    'excess_short': excess_short,
                })
                continue

            owned_today_bucket = owned_hist_bucket = 0
            if pos is not None and pos['owned'] > 0:
                try:
                    pb = self.position_buckets(state, pos, cid)
                    owned_today_bucket = int(pb.get('today', 0))
                    owned_hist_bucket = int(pb.get('historical', 0))
                except TraderError:
                    owned_today_bucket = pos['owned']
                    owned_hist_bucket = 0

            contract_status = excess_cancel_status or 'CONVERGING'
            contract_wait_reason = excess_cancel_wait
            contract_failures = 0
            tick = market.get('tick') or ticks[-1]
            for pos_side, excess_qty in (('LONG', conv_excess_long), ('SHORT', conv_excess_short)):
                if excess_qty <= 0:
                    continue
                side_orders = [o for o in conv_orders if o.get('convergence_side') == pos_side]
                close_dir = 'SELL' if pos_side == 'LONG' else 'BUY'
                # Unified non-terminal close orders for this contract/side (strategy + convergence + UNKNOWN/CancelPending).
                all_side_close = [
                    o for o in state['orders'].values()
                    if o.get('contract_id') == cid and o.get('effect') == 'REDUCE'
                    and o.get('direction') == close_dir and not terminal(o)
                ]
                strat_side_close = [o for o in all_side_close if not o.get('convergence_order')]
                desired, price_reason = self._convergence_close_price(market, pos_side)
                if desired is None:
                    contract_status = price_reason
                    contract_wait_reason = price_reason
                    events.append({
                        'event': 'CONVERGENCE_DEFERRED',
                        'contract_id': cid,
                        'side': pos_side,
                        'reason': price_reason,
                        'excess_qty': excess_qty,
                    })
                    continue

                uncertain = [
                    o['child_order_id'] for o in side_orders
                    if o['send_state'] in {'SEND_POSSIBLE', 'UNKNOWN'}
                    or o.get('cancel_status') in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}
                ]
                pending_qty = sum(max(0, o['original_qty'] - o['filled_qty']) for o in side_orders)
                if pending_qty > excess_qty:
                    if uncertain:
                        contract_status = (
                            'SEND_RESULT_UNKNOWN'
                            if any(o['send_state'] in {'SEND_POSSIBLE', 'UNKNOWN'} for o in side_orders)
                            else 'CANCELLING_EXCESS_CLOSE'
                        )
                    else:
                        contract_status = excess_cancel_status or 'CANCELLING_EXCESS_CLOSE'
                    continue

                repricing = False
                if not uncertain and perm.get('can_cancel', False) and perm.get('continuous_matching', False):
                    for order in side_orders:
                        if order.get('native_id') is None or order['broker_state'] != 'ALIVE':
                            continue
                        cur_px = Decimal(order['limit_price'])
                        prop_px = Decimal(desired)
                        worse = prop_px > cur_px if order['direction'] == 'BUY' else prop_px < cur_px
                        if worse and (req := self.cancel(order, 'REPRICE')):
                            effects.append(req)
                            repricing = True
                if uncertain or repricing:
                    contract_status = 'SEND_RESULT_UNKNOWN' if uncertain else 'REPRICING'
                    continue

                needed = max(0, excess_qty - pending_qty)
                if needed <= 0:
                    contract_status = 'CONVERGING_FILL_PENDING'
                    continue

                side_key = pos_side.lower()
                actual_side_qty = actual_long if pos_side == 'LONG' else actual_short
                owned_side_qty = owned_long if pos_side == 'LONG' else owned_short
                m_today = int(market.get(f'{side_key}_today', actual_side_qty))
                m_hist = int(market.get(f'{side_key}_historical', 0))
                res_today = owned_today_bucket if (pos is not None and pos['direction'] == pos_side) else 0
                res_hist = owned_hist_bucket if (pos is not None and pos['direction'] == pos_side) else 0
                strat_today = sum(
                    max(0, o['original_qty'] - o['filled_qty'])
                    for o in strat_side_close if o.get('native_offset') == 'CLOSETODAY'
                )
                strat_hist = sum(
                    max(0, o['original_qty'] - o['filled_qty'])
                    for o in strat_side_close if o.get('native_offset') != 'CLOSETODAY'
                )
                all_today = sum(
                    max(0, o['original_qty'] - o['filled_qty'])
                    for o in all_side_close if o.get('native_offset') == 'CLOSETODAY'
                )
                all_hist = sum(
                    max(0, o['original_qty'] - o['filled_qty'])
                    for o in all_side_close if o.get('native_offset') != 'CLOSETODAY'
                )
                keep_today = max(0, res_today - strat_today)
                keep_hist = max(0, res_hist - strat_hist)
                free_today = max(0, m_today - all_today - keep_today)
                free_hist = max(0, m_hist - all_hist - keep_hist)
                if free_today + free_hist < needed:
                    keep_total = max(0, owned_side_qty - (strat_today + strat_hist))
                    rem_total = max(0, m_today + m_hist - (all_today + all_hist) - keep_total)
                    needed = min(needed, rem_total) if rem_total > 0 else 0
                if needed <= 0:
                    continue

                min_qty = int(market.get('min_order_qty', 1))
                max_qty = int(market.get('max_order_qty', 2_147_483_647))
                is_sep = cid.split('.')[0] in separated_exchanges
                if is_sep:
                    if free_hist > 0:
                        offset = 'CLOSE'
                        send_qty = min(needed, free_hist, max_qty)
                    elif free_today > 0:
                        offset = 'CLOSETODAY'
                        send_qty = min(needed, free_today, max_qty)
                    else:
                        contract_status = 'DEFERRED_FACTS_INCOMPLETE'
                        contract_wait_reason = 'CLOSE_BUCKET_UNPROVED'
                        events.append({
                            'event': 'CONVERGENCE_DEFERRED',
                            'contract_id': cid,
                            'side': pos_side,
                            'reason': 'CLOSE_BUCKET_UNPROVED',
                            'needed': needed,
                        })
                        continue
                else:
                    offset = 'CLOSE'
                    send_qty = min(needed, max_qty)
                if send_qty < min_qty:
                    contract_status = 'DEFERRED_BELOW_MIN_QTY'
                    contract_wait_reason = 'BELOW_MIN_ORDER_QTY'
                    events.append({
                        'event': 'CONVERGENCE_DEFERRED',
                        'contract_id': cid,
                        'side': pos_side,
                        'reason': 'BELOW_MIN_ORDER_QTY',
                        'needed': send_qty,
                        'min_order_qty': min_qty,
                    })
                    continue

                cond_fp = self._convergence_cond_fp(
                    offset, desired, free_today, free_hist, excess_qty, has_ext, perm, tick,
                )

                # Classify prior unfilled convergence failures on this side (since last fill, current day).
                side_history = [
                    o for o in state['orders'].values()
                    if o.get('convergence_order') and o['contract_id'] == cid
                    and o.get('convergence_side') == pos_side
                    and o.get('native_trading_day') == state['trading_day']
                    and terminal(o)
                ]
                recent_failures = []
                for prev in side_history:
                    if prev['filled_qty'] > 0:
                        recent_failures.clear()
                        continue
                    local_rej = prev.get('local_send_rejection') or {}
                    local_code = local_rej.get('error_code')
                    local_reason = local_rej.get('rejection_reason')
                    # Market phase pre-send guards and quote-race unsent proofs never count as failures.
                    if prev['send_state'] == 'DEFINITELY_NOT_SENT' and (
                        local_code == 'E_DSV_MARKET_PHASE'
                        or local_reason in {'QUOTE_REVISION_CHANGED', 'NATIVE_QUOTE_NOT_CURRENT'}
                    ):
                        continue
                    if prev.get('convergence_failure_at') is None:
                        prev['convergence_failure_at'] = str(wall_time)
                    recent_failures.append(prev)

                if recent_failures:
                    last_fail = recent_failures[-1]
                    last_local = last_fail.get('local_send_rejection') or {}
                    last_cat = last_fail.get('broker_rejection_category')
                    last_detail = last_fail.get('broker_rejection_detail') or last_local.get('rejection_reason')
                    last_code = last_local.get('error_code')
                    if last_detail in {'NO_TRADING_PERMISSION', 'SDK_PERMISSION_REFUSED'}:
                        contract_failures += 1
                        contract_status = 'DEFERRED_NO_TRADING_PERMISSION'
                        contract_wait_reason = 'NO_TRADING_PERMISSION'
                        events.append({
                            'event': 'CONVERGENCE_DEFERRED',
                            'contract_id': cid,
                            'side': pos_side,
                            'reason': 'NO_TRADING_PERMISSION',
                            'excess_qty': needed,
                        })
                        continue

                    is_phase_temp = last_cat == 'TRANSIENT' and last_detail == 'BROKER_TEMP_STATE'
                    is_transient = (
                        last_cat == 'TRANSIENT'
                        or (last_fail['broker_state'] == 'CANCELLED' and last_fail.get('cancel_reason') is None)
                    )
                    if is_transient:
                        # Count consecutive transient failures; BROKER_TEMP_STATE (market phase) does not penalize failure count.
                        trans_streak = 0
                        for item in reversed(recent_failures):
                            icat = item.get('broker_rejection_category')
                            if icat == 'TRANSIENT' or (item['broker_state'] == 'CANCELLED' and item.get('cancel_reason') is None):
                                trans_streak += 1
                            else:
                                break
                        if not is_phase_temp:
                            contract_failures += trans_streak
                        schedule = (0, 1, 2, 4, 8, 15, 30)
                        delay = schedule[min(max(0, trans_streak - 1), len(schedule) - 1)]
                        fail_at = float(last_fail.get('convergence_failure_at') or wall_time)
                        if float(wall_time) - fail_at < delay:
                            contract_status = 'DEFERRED_TRANSIENT_RETRY_BACKOFF'
                            contract_wait_reason = last_detail or 'TRANSIENT_RETRY_BACKOFF'
                            events.append({
                                'event': 'CONVERGENCE_DEFERRED',
                                'contract_id': cid,
                                'side': pos_side,
                                'reason': contract_wait_reason,
                                'excess_qty': needed,
                            })
                            continue
                    else:
                        is_structural = (
                            last_cat == 'STRUCTURAL'
                            or last_code in {'E_DSV_SDK_SEND_VALIDATION', 'E_DSV_SDK_SEND_RISK'}
                        )
                        matching = 0
                        for item in reversed(recent_failures):
                            icat = item.get('broker_rejection_category')
                            icode = (item.get('local_send_rejection') or {}).get('error_code')
                            item_fp = item.get('convergence_cond_fp')
                            if icat == 'TRANSIENT':
                                break
                            if item_fp is not None and item_fp != cond_fp:
                                break
                            if icat == 'STRUCTURAL' or icode in {'E_DSV_SDK_SEND_VALIDATION', 'E_DSV_SDK_SEND_RISK'} or not is_structural:
                                matching += 1
                        contract_failures += matching
                        cap = 2 if is_structural else 3
                        if matching >= cap:
                            contract_status = 'DEFERRED_STRUCTURAL_REJECTION'
                            contract_wait_reason = last_detail or 'STRUCTURAL_REJECTION_CAP'
                            events.append({
                                'event': 'CONVERGENCE_DEFERRED',
                                'contract_id': cid,
                                'side': pos_side,
                                'reason': contract_wait_reason,
                                'excess_qty': needed,
                            })
                            continue
                        fail_at = float(last_fail.get('convergence_failure_at') or wall_time)
                        if matching >= 1 and float(wall_time) - fail_at < 1:
                            contract_status = 'DEFERRED_TRANSIENT_RETRY_BACKOFF'
                            contract_wait_reason = last_detail or 'RETRY_BACKOFF'
                            continue

                req = self.new_convergence_order(
                    state, cid, pos_side, market, send_qty, desired, offset, cond_fp=cond_fp)
                effects.append(req)
                events.append({
                    'event': 'CONVERGENCE_ACTION',
                    'contract_id': cid,
                    'action': 'CLOSE_EXCESS_OR_UNOWNED',
                    'side': pos_side,
                    'direction': req['direction'],
                    'native_offset': offset,
                    'quantity': send_qty,
                    'limit_price': desired,
                    'configured': is_configured,
                })

            post_pending = contract_pending_summary(state['orders'].values(), cid, terminal)
            statuses[cid] = {
                'configured': is_configured,
                'status': contract_status,
                'wait_reason': contract_wait_reason,
                'failure_count': contract_failures,
                **qty_meta,
                'pending_open_long': post_pending['open_long'],
                'pending_open_short': post_pending['open_short'],
                'pending_close_long': post_pending['close_long'],
                'pending_close_short': post_pending['close_short'],
                **perm_meta,
            }
        return effects, statuses, events

    def dispatch(self, state, effects):
        for effect in effects:
            is_ext_cancel = bool(effect.get('external_order'))
            if is_ext_cancel:
                order = state.setdefault('convergence_cancels', {})[effect['native_order_id']]
            else:
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
                    sym_row = state['symbols'].get(order['contract_id'])
                    position = None if sym_row is None else sym_row['position']
                    retryable_open_race = (
                        order['effect'] == 'OPEN'
                        and details.get('rejection_reason') in {'QUOTE_REVISION_CHANGED', 'NATIVE_QUOTE_NOT_CURRENT'})
                    stopped_open = False
                    if (position is not None and position['id'] == order['position_id']
                            and order['effect'] == 'OPEN' and not retryable_open_race):
                        position.update(entry_stopped=True, preserve_open_on_stop=False)
                        stopped_open = True
                    self._diag().emit(
                        'EFFECT_DISPATCH_DEFINITELY_NOT_SENT',
                        contract_id=order['contract_id'],
                        position_id=order['position_id'],
                        child_order_id=order['child_order_id'],
                        effect=order['effect'],
                        stage=details.get('stage'),
                        error_code=details.get('error_code'),
                        reason=details.get('rejection_reason') or details.get('error_code'),
                        entry_stopped=stopped_open,
                    )
                elif result == 'UNKNOWN':
                    self._diag().emit(
                        'EFFECT_DISPATCH_UNKNOWN',
                        contract_id=order['contract_id'],
                        position_id=order['position_id'],
                        child_order_id=order['child_order_id'],
                        effect=order['effect'],
                        stage=details.get('stage'),
                        error_code=details.get('error_code'),
                        reason='SEND_RESULT_UNKNOWN',
                    )
            else:
                if result == 'DEFINITELY_NOT_SENT':
                    order['cancel_status'] = None
                    order['local_cancel_rejection'] = {'effect_id': effect['id'], **details}
                    if details.get('error_code') == 'E_DSV_MARKET_PHASE':
                        if not is_ext_cancel:
                            order['cancel_attempt'] = max(0, int(order.get('cancel_attempt') or 1) - 1)
                    elif is_ext_cancel:
                        order['cancel_failure_count'] = int(order.get('cancel_failure_count') or 0) + 1
                else:
                    order['cancel_status'] = result
                if is_ext_cancel and effect['kind'] == 'CANCEL':
                    order['native_order_id'] = effect['native_order_id']
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
