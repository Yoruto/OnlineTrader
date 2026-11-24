"""Pure responsibility and mode derivation for Standalone Trading Runtime."""

from __future__ import annotations

from online_trader.dsv_runtime.controller import terminal


_CONVERGENCE_IN_FLIGHT = frozenset({'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'})


def derive_runtime_mode(strategy_intent: str, responsibility_state: str, *, has_start_pending: bool = False) -> str:
    """Derive MONITORING / TRADING / DRAINING from intent and responsibility."""
    if strategy_intent == 'RUNNING' or has_start_pending:
        return 'TRADING'
    if responsibility_state in ('ACTIVE', 'DRAINING', 'UNKNOWN'):
        return 'DRAINING'
    return 'MONITORING'


def evaluate_responsibility(
    snapshot: dict,
    *,
    strategy_intent: str = 'STOPPED',
    has_start_pending: bool = False,
    maintenance: dict | None = None,
    pending_commands: bool = False,
) -> dict:
    """Compute a serializable ResponsibilitySummary and maintenance blockers.

    This is strictly read-only and never touches SDK or disk.  Preview-only
    positions created by startup Plan B (``awaiting_start_arm=True`` with no
    children and not activated in this boot) are excluded from activated
    protection responsibility so an idle MONITORING account can still enter
    maintenance for account switching or updates.
    """
    orders = snapshot.get('orders') or {}
    symbols = snapshot.get('symbols') or {}
    retired = snapshot.get('retired_positions') or {}
    convergence_cancels = snapshot.get('convergence_cancels') or {}

    unresolved_children = sorted(
        str(child_id) for child_id, order in orders.items() if not terminal(order)
    )
    uncertain_children = sorted(
        str(child_id)
        for child_id, order in orders.items()
        if not terminal(order)
        and (
            order.get('send_state') in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}
            or order.get('cancel_status') in {'SEND_POSSIBLE', 'CALL_RETURNED', 'UNKNOWN'}
            or bool(order.get('terminal_conflict'))
        )
    )
    unresolved_convergence_cancels = sorted(
        str(native_id)
        for native_id, rec in convergence_cancels.items()
        if isinstance(rec, dict) and rec.get('cancel_status') in _CONVERGENCE_IN_FLIGHT
    )

    protected_lots: dict[str, int] = {}
    preview_only_contracts: list[str] = []
    conflict_contracts: list[str] = []

    for contract, row in sorted(symbols.items()):
        if not isinstance(row, dict):
            continue
        has_conflict = bool(
            row.get('native_conflicts')
            or row.get('detached_positions')
            or row.get('ownership_conflict')
        )
        if row.get('detached_positions'):
            detached_qty = sum(
                int((retired.get(key) or {}).get('owned') or 0)
                for key in row.get('detached_positions', ())
            )
            if detached_qty > 0:
                protected_lots[contract] = protected_lots.get(contract, 0) + detached_qty
        if has_conflict:
            conflict_contracts.append(contract)
        position = row.get('position')
        if not isinstance(position, dict):
            continue
        owned = int(position.get('owned') or 0)
        is_preview_only = bool(
            position.get('awaiting_start_arm')
            and not position.get('activated_in_boot')
            and not position.get('children')
        )
        if is_preview_only:
            preview_only_contracts.append(contract)
        elif owned > 0 or bool(position.get('children')):
            if owned > 0:
                protected_lots[contract] = protected_lots.get(contract, 0) + owned

    synchronized = bool(snapshot.get('synchronized', False))
    storage_failed = bool(snapshot.get('storage_failed', False))
    clock_fault = snapshot.get('clock_recovery_required')
    owner_fault = snapshot.get('owner_fault_code')

    blockers: list[str] = []
    if strategy_intent == 'RUNNING' or has_start_pending or bool(snapshot.get('running')):
        blockers.append('STRATEGY_RUNNING')
    if not synchronized:
        blockers.append('BROKER_FACTS_INCOMPLETE')
    if storage_failed:
        blockers.append('STORAGE_FAILED')
    if clock_fault:
        blockers.append('CLOCK_FAULT')
    if owner_fault:
        blockers.append('OWNER_FAULT')
    if unresolved_children:
        blockers.append('UNRESOLVED_CHILDREN')
    if unresolved_convergence_cancels:
        blockers.append('UNRESOLVED_CONVERGENCE_CANCELS')
    if protected_lots:
        blockers.append('ACTIVE_PROTECTED_POSITIONS')
    if conflict_contracts:
        blockers.append('ALLOCATION_OR_OWNERSHIP_CONFLICT')
    if pending_commands:
        blockers.append('PENDING_COMMANDS')
    if maintenance is not None:
        blockers.append('MAINTENANCE_IN_FLIGHT')

    if not synchronized or storage_failed or uncertain_children or conflict_contracts:
        state = 'UNKNOWN'
    elif unresolved_children or unresolved_convergence_cancels or protected_lots:
        state = 'DRAINING' if strategy_intent != 'RUNNING' else 'ACTIVE'
    else:
        state = 'ACTIVE' if strategy_intent == 'RUNNING' else 'NONE'

    scope = snapshot.get('scope') or {}
    fact_boundary = {
        'boot_id': scope.get('runtime_boot_id'),
        'account_key': scope.get('account_key'),
        'trading_day': snapshot.get('trading_day'),
        'sequence': str(snapshot.get('sequence') or '0'),
    }
    mode = derive_runtime_mode(
        strategy_intent, state, has_start_pending=has_start_pending,
    )
    return {
        'state': state,
        'mode': mode,
        'strategy_intent': strategy_intent,
        'protected_lots': protected_lots,
        'protected_lots_total': sum(protected_lots.values()),
        'preview_only_contracts': preview_only_contracts,
        'unresolved_child_ids': unresolved_children,
        'unresolved_convergence_cancel_ids': unresolved_convergence_cancels,
        'conflict_contracts': conflict_contracts,
        'synchronized': synchronized,
        'storage_failed': storage_failed,
        'fact_boundary': fact_boundary,
        'blockers': blockers,
        'maintenance_allowed': len(blockers) == 0,
    }
