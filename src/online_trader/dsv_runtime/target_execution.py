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
