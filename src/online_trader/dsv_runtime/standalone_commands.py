"""Translate the existing Runtime Workbench contract into DSV owner commands.

This is an in-process adapter.  It does not create a second pipe or expose the
Replacement/offline protocol to the product UI.
"""

from online_trader.domain.values import identifier, sequence
from online_trader.dsv_runtime.controller import terminal
from online_trader.errors import TraderError


def _account_scope(scope):
    if type(scope) is not dict or scope.get('scope_type') != 'ACCOUNT':
        raise TraderError('E_SCOPE', 'This operation requires the current Runtime account scope')


def _position(snapshot, position_id):
    for row in snapshot['symbols'].values():
        position = row.get('position')
        if position is not None and position['id'] == position_id:
            return position
    raise TraderError('E_DSV_OWNERSHIP', 'The selected strategy position is no longer current')


def _price_mode(parameters):
    mode = parameters.get('price_mode')
    if mode is not None and mode not in {'对手价', '排队价', '最新价'}:
        raise TraderError('E_DSV_COMMAND', 'Manual close uses a supported Workbench price mode')
    return mode


def translate_workbench_command(body, snapshot, *, runtime_boot_id, configuration_revision):
    """Return one idempotent DsvSession command bound to current projections."""
    if type(body) is not dict:
        raise TraderError('E_SCHEMA', 'Expected a Runtime Workbench command body')
    command_id = identifier(body.get('command_id'))
    operation = body.get('operation')
    scope, parameters = body.get('scope'), body.get('parameters')
    if type(parameters) is not dict:
        raise TraderError('E_SCHEMA', 'Workbench command parameters must be an object')
    binding = body.get('binding')
    expected = {'account_key': snapshot['scope']['account_key'],
                'kernel_boot_id': runtime_boot_id,
                'config_revision': str(sequence(configuration_revision, positive=True))}
    if type(binding) is not dict:
        raise TraderError('E_SCHEMA', 'Workbench command binding must be an object')
    if (binding.get('kernel_boot_id') != runtime_boot_id
            or snapshot['scope'].get('runtime_boot_id') != runtime_boot_id):
        raise TraderError('E_BOOT_MISMATCH', 'The Runtime boot changed before command execution')
    if binding.get('account_key') != expected['account_key']:
        raise TraderError('E_SCOPE', 'The command account differs from the Runtime owner')
    if binding.get('config_revision') != expected['config_revision'] or binding != expected:
        raise TraderError('E_REVISION_CONFLICT', 'The Runtime configuration changed before command execution')
    common = {'id': command_id, 'workbench_operation': operation,
              'workbench_scope': scope, 'runtime_boot_id': runtime_boot_id,
              'configuration_revision': expected['config_revision']}
    if operation == 'ARM_ENTRIES':
        _account_scope(scope)
        return common | {'action': 'START'}
    if operation in {'PAUSE_NEW_ENTRIES', 'STOP_DSV_STRATEGY'}:
        _account_scope(scope)
        return common | {'action': 'STOP'}
    if operation in {'CANCEL_SELECTED', 'CANCEL_OWNED'}:
        if type(scope) is not dict:
            raise TraderError('E_SCOPE', 'Cancellation requires an explicit scope')
        children = parameters.get('child_order_ids')
        if operation == 'CANCEL_OWNED':
            if scope.get('scope_type') not in {'OWNED_ORDERS', 'ACCOUNT'}:
                raise TraderError('E_SCOPE', 'Owned-order cancellation requires OWNED_ORDERS or ACCOUNT scope')
            if children is None:
                if parameters.get('include_exits') not in {None, False, True}:
                    raise TraderError('E_DSV_COMMAND', 'Owned-order cancellation needs an explicit exit switch')
                children = [child for child, order in snapshot['orders'].items()
                            if not terminal(order) and (
                                parameters.get('include_exits') is True or order['effect'] == 'OPEN')]
        if operation == 'CANCEL_SELECTED' and (
                scope.get('scope_type') != 'SELECTED_ORDERS'
                or scope.get('scope_ids') != children):
            raise TraderError('E_SCOPE', 'Selected-order scope differs from its child identities')
        return common | {'action': 'CANCEL_MANY', 'child_order_ids': children}
    if operation == 'MANUAL_CLOSE':
        position_id = identifier(parameters.get('position_cycle_id'))
        if type(scope) is not dict or scope.get('scope_type') != 'CYCLE' or scope.get('scope_ids') != [position_id]:
            raise TraderError('E_SCOPE', 'Manual close scope differs from its strategy position')
        position = _position(snapshot, position_id)
        current_revision = int(position.get('target_revision', 1))
        raw_expected = parameters.get('expected_target_revision')
        if raw_expected is None:
            raw_expected = parameters.get('external_takeover_id')
        if raw_expected is None:
            raise TraderError('E_SCHEMA', 'Manual close requires expected_target_revision')
        if isinstance(raw_expected, str) and raw_expected.startswith('target-rev-'):
            raw_expected = raw_expected.removeprefix('target-rev-')
        try:
            expected_revision = sequence(raw_expected, positive=True)
        except TraderError:
            raise TraderError('E_REVISION_CONFLICT', 'The strategy position target revision is invalid') from None
        if expected_revision != current_revision:
            raise TraderError('E_REVISION_CONFLICT', 'The strategy position target changed before manual close')
        return common | {'action': 'REDUCE', 'contract_id': position['contract_id'],
                         'quantity': parameters.get('quantity'),
                         'price_mode': _price_mode(parameters),
                         'expected_target_id': position.get('target_id', position['id']),
                         'expected_target_revision': expected_revision}
    if operation in {'MANUAL_CLOSE_ALL', 'FLATTEN_THEN_STOP'}:
        _account_scope(scope)
        current_positions = {row['position']['id']: row['position'] for row in snapshot['symbols'].values()
                             if row.get('position') is not None
                             and (operation == 'FLATTEN_THEN_STOP' or row['position']['owned'])}
        current = {pid: pos['owned'] for pid, pos in current_positions.items()}
        current_revisions = {pid: int(pos.get('target_revision', 1)) for pid, pos in current_positions.items()}
        if operation == 'MANUAL_CLOSE_ALL':
            submitted = parameters.get('positions')
            if type(submitted) is not dict or not submitted:
                raise TraderError('E_SCHEMA', 'Bulk close requires a positions map')
            submitted_qty = {}
            submitted_rev = {}
            raw_rev_map = parameters.get('expected_target_revisions')
            if raw_rev_map is not None and type(raw_rev_map) is not dict:
                raise TraderError('E_SCHEMA', 'Bulk close expected_target_revisions must be an object')
            for raw_key, qty in submitted.items():
                if type(raw_key) is not str:
                    raise TraderError('E_SCHEMA', 'Bulk close position keys must be strings')
                if ':rev:' in raw_key:
                    pid_part, rev_part = raw_key.rsplit(':rev:', 1)
                    pid = identifier(pid_part)
                    if pid in submitted_qty:
                        raise TraderError('E_SCHEMA', 'Duplicate position_cycle_id in bulk close')
                    try:
                        submitted_rev[pid] = sequence(rev_part, positive=True)
                    except TraderError:
                        raise TraderError('E_REVISION_CONFLICT', 'Bulk close target revision is invalid') from None
                else:
                    pid = identifier(raw_key)
                    if pid in submitted_qty:
                        raise TraderError('E_SCHEMA', 'Duplicate position_cycle_id in bulk close')
                submitted_qty[pid] = qty
            if raw_rev_map is not None:
                for raw_pid, raw_rev in raw_rev_map.items():
                    pid = identifier(raw_pid)
                    try:
                        rev = sequence(raw_rev, positive=True)
                    except TraderError:
                        raise TraderError('E_REVISION_CONFLICT', 'Bulk close target revision is invalid') from None
                    if pid in submitted_rev and submitted_rev[pid] != rev:
                        raise TraderError('E_REVISION_CONFLICT', 'Conflicting bulk close target revisions')
                    submitted_rev[pid] = rev
            if not submitted_rev or set(submitted_rev) != set(submitted_qty):
                raise TraderError('E_SCHEMA', 'Bulk close requires expected target revision for every position')
            if submitted_qty != current or submitted_rev != current_revisions:
                raise TraderError('E_REVISION_CONFLICT', 'Bulk close quantities or target revisions changed before submission')
        return common | {'action': 'FLATTEN_STOP' if operation == 'FLATTEN_THEN_STOP' else 'REDUCE_ALL',
                         'position_ids': list(current), 'price_mode': _price_mode(parameters),
                         **(({'expected_target_revisions': current_revisions}) if operation == 'MANUAL_CLOSE_ALL' else {})}
    if operation == 'REQUEST_RECONCILIATION':
        _account_scope(scope)
        if set(parameters) - {'requested_scope', 'position_cycle_id', 'clock_recovery_id'}:
            raise TraderError('E_AUTH', 'Legacy evidence import is not a target-owner reconciliation command')
        if snapshot.get('clock_recovery_required'):
            return common | {
                'action': 'CLOCK_RECOVER',
                'clock_recovery_id': parameters.get('clock_recovery_id') or snapshot.get('clock_recovery_id'),
            }
        return common | {'action': 'RECONCILE'}
    raise TraderError('E_AUTH', 'The target-execution owner does not support this Workbench operation')


def execute_workbench_command(session, adapter, body, *, runtime_boot_id, configuration_revision):
    snapshot = session.snapshot()
    command = translate_workbench_command(
        body, snapshot, runtime_boot_id=runtime_boot_id,
        configuration_revision=configuration_revision)
    return adapter.command(session, command)
