"""Pure target accounting for the explicit DSV order controller.

The module deliberately has no SDK, journal, clock, Qt or legacy Kernel
dependency.  It describes the maximum quantity that non-terminal children can
still add to, or remove from, one Runtime-proved strategy position.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TargetAccounting:
    """Quantity projection for one effect against one owned target."""

    confirmed_qty: int
    target_qty: int
    pending_qty: int
    potential_qty: int
    needed_qty: int
    excess_pending_qty: int
    pending_children: tuple[str, ...]
    uncertain_children: tuple[str, ...]
    cancel_pending_children: tuple[str, ...]

    @property
    def covered(self):
        return self.needed_qty == 0

    @property
    def action_reason(self):
        if self.excess_pending_qty:
            return 'TARGET_REDUCED'
        if self.pending_qty and self.covered:
            return 'WORKING_ORDER_COVERS_TARGET'
        if self.needed_qty:
            return 'TARGET_GAP'
        return 'TARGET_ALREADY_COVERED'


def _remaining(order):
    return max(0, int(order['original_qty']) - int(order['filled_qty']))


def account_target(*, confirmed_qty, target_qty, orders, terminal):
    """Project a target using the maximum remaining non-terminal quantity.

    ``terminal`` is injected by the controller so the accounting function uses
    the same strict native-fact and DEFINITELY_NOT_SENT semantics as dispatch.
    Cancellation requests, SEND_POSSIBLE and UNKNOWN all remain pending until
    that predicate observes an admissible terminal fact.
    """

    confirmed = int(confirmed_qty)
    target = int(target_qty)
    if confirmed < 0 or target < 0:
        raise ValueError('Target quantities must be nonnegative')
    active = tuple(order for order in orders if not terminal(order) and _remaining(order))
    pending = sum(_remaining(order) for order in active)
    potential = confirmed + pending
    uncertain = tuple(order['child_order_id'] for order in active
                      if order['send_state'] in {'SEND_POSSIBLE', 'UNKNOWN'})
    cancelling = tuple(order['child_order_id'] for order in active if order.get('cancel_status'))
    return TargetAccounting(
        confirmed_qty=confirmed,
        target_qty=target,
        pending_qty=pending,
        potential_qty=potential,
        needed_qty=max(0, target - potential),
        excess_pending_qty=max(0, potential - target),
        pending_children=tuple(order['child_order_id'] for order in active),
        uncertain_children=uncertain,
        cancel_pending_children=cancelling,
    )


def effective_target_qty(position, *, orders=None, terminal=None) -> int:
    """Return the effective strategy desired quantity for ``position``.

    Covers normal opening (``quantity``), partial fill with stopped entry
    (``owned``/``entry_filled``), stop with preserved open orders, and active
    reduction/exit (``exit_target``).
    """
    if position is None:
        return 0
    exit_target = position.get('exit_target')
    if exit_target is not None:
        return max(0, min(int(position['owned']), int(exit_target)))
    if position.get('entry_stopped'):
        if position.get('preserve_open_on_stop') and orders is not None and terminal is not None:
            pending_open = sum(
                _remaining(o) for o in orders
                if o.get('effect') == 'OPEN' and not terminal(o)
            )
            return max(0, min(int(position['quantity']), int(position['entry_filled']) + pending_open))
        return max(0, int(position['owned']))
    return max(0, int(position['quantity']))


def effective_position_target(position, *, orders=None, terminal=None) -> tuple[int, int]:
    """Return ``(target_long, target_short)`` using unified effective-target rules."""
    qty = effective_target_qty(position, orders=orders, terminal=terminal)
    if position is None or qty <= 0:
        return 0, 0
    if position.get('direction') == 'LONG':
        return qty, 0
    if position.get('direction') == 'SHORT':
        return 0, qty
    return 0, 0


def contract_pending_summary(orders, contract_id: str, terminal) -> dict[str, int]:
    """Aggregate all non-terminal orders for ``contract_id`` across strategy and convergence.

    Includes ``SEND_POSSIBLE``, ``UNKNOWN``, and ``CancelPending`` orders until
    ``terminal(order)`` observes an admissible terminal fact.
    """
    summary = {
        'open_long': 0,
        'open_short': 0,
        'close_long': 0,
        'close_short': 0,
        'strategy_close_long': 0,
        'strategy_close_short': 0,
        'convergence_close_long': 0,
        'convergence_close_short': 0,
        'close_long_today': 0,
        'close_long_hist': 0,
        'close_short_today': 0,
        'close_short_hist': 0,
        'uncertain_open_long': 0,
        'uncertain_open_short': 0,
        'uncertain_close_long': 0,
        'uncertain_close_short': 0,
        'total_pending': 0,
    }
    for order in orders:
        if order.get('contract_id') != contract_id or terminal(order):
            continue
        rem = _remaining(order)
        if rem <= 0:
            continue
        summary['total_pending'] += rem
        effect = order.get('effect')
        direction = order.get('direction')
        is_uncertain = (
            order.get('send_state') in {'SEND_POSSIBLE', 'UNKNOWN'}
            or order.get('cancel_status') in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}
        )
        if effect == 'OPEN':
            side_key = 'long' if direction == 'BUY' else 'short'
            summary[f'open_{side_key}'] += rem
            if is_uncertain:
                summary[f'uncertain_open_{side_key}'] += rem
        elif effect == 'REDUCE':
            side_key = 'long' if direction == 'SELL' else 'short'
            summary[f'close_{side_key}'] += rem
            if order.get('convergence_order'):
                summary[f'convergence_close_{side_key}'] += rem
            else:
                summary[f'strategy_close_{side_key}'] += rem
            bucket_key = 'today' if order.get('native_offset') == 'CLOSETODAY' else 'hist'
            summary[f'close_{side_key}_{bucket_key}'] += rem
            if is_uncertain:
                summary[f'uncertain_close_{side_key}'] += rem
    return summary
