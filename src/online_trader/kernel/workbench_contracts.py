"""Read-only configuration and capability contracts for Workbench views."""

from online_trader.contracts.codec import content_hash, strict_loads
from online_trader.errors import TraderError


def config_snapshot(kernel, runtime, scheduler, port, scoped):
    """Project the committed Ledger configuration without creating authority."""
    errors = []
    row = None
    payload = {}
    try:
        rows = kernel.ledger.query(
            'SELECT config_revision,rule_set_id,payload_json,payload_hash,effective_boundary,created_event_seq '
            'FROM config_revisions WHERE config_revision=?', (kernel._config_revision,))
        if len(rows) != 1:
            errors.append('CONFIG_REVISION_NOT_UNIQUE')
        else:
            row = rows[0]
            payload = strict_loads(row['payload_json'])
            if content_hash(payload) != row['payload_hash']:
                errors.append('CONFIG_PAYLOAD_HASH_MISMATCH')
            if (str(row['config_revision']) != str(kernel._config_revision)
                    or row['payload_hash'] != kernel._configuration['payload_hash']
                    or row['rule_set_id'] != kernel._configuration['rule_set_id']):
                errors.append('CONFIG_AUTHORITY_MISMATCH')
    except (TraderError, KeyError, TypeError, ValueError):
        errors.append('CONFIG_SNAPSHOT_UNREADABLE')

    profile_hash = payload.get('profile_hash') if isinstance(payload, dict) else None
    if runtime is not None and profile_hash != runtime.profile.content_hash:
        errors.append('CONFIG_PROFILE_HASH_MISMATCH')
    identity = getattr(kernel, '_dsv_runtime_identity', None) or {}
    clock_health_hash = None
    if scheduler is not None and scheduler.clock_health is not None:
        try:
            clock_health_hash = content_hash(scheduler.clock_health)
        except (TraderError, KeyError, TypeError, ValueError):
            errors.append('CONFIG_CLOCK_HEALTH_UNAVAILABLE')
    decision_inputs = {
        'calendar_hash': (kernel.simulation.grant.value.get('calendar_hash') if scoped else None),
        'market_source_epoch': getattr(port, 'source_epoch', None),
        'gateway_identity': getattr(port, 'gateway_identity', None),
        'time_semantics_version': identity.get('time_semantics_version'),
        'clock_epoch': identity.get('clock_epoch'),
        'clock_health_hash': clock_health_hash,
        'instrument_metadata_hash': None,
    }
    if scoped:
        try:
            instruments = {code: port.instrument(code)
                           for code in kernel.simulation.grant.value['contracts']}
            decision_inputs['instrument_metadata_hash'] = content_hash(instruments)
        except (TraderError, AttributeError, KeyError, TypeError, ValueError):
            errors.append('CONFIG_INSTRUMENT_METADATA_UNAVAILABLE')
    return {
        'schema_version': 'ONLINE_TRADER_CONFIG_SNAPSHOT/1',
        'account_key': kernel.account_key,
        'kernel_boot_id': kernel.boot_id,
        'config_revision': str(kernel._config_revision),
        'payload_hash': kernel._configuration['payload_hash'],
        'rule_set_id': kernel._configuration['rule_set_id'],
        'profile_hash': profile_hash,
        'effective_boundary': row['effective_boundary'] if row is not None else None,
        'created_event_seq': str(row['created_event_seq']) if row is not None else None,
        'effective_at_utc': payload.get('parameters_saved_at_utc') if isinstance(payload, dict) else None,
        'decision_inputs': decision_inputs,
        'integrity': 'VERIFIED' if not errors else 'INVALID',
        'errors': errors,
    }


def capability_rows(result, external_positions, config_verified):
    """Return explanatory UI capabilities; the Owner still authorizes commands."""
    connection = result['connection']
    connected = connection['broker_connected'] and connection['synchronized']
    writable = not connection['storage_failed']
    operational = connected and writable
    healthy = operational and not any(connection[key] for key in
                                      ('fault', 'recovery_required', 'clock_fault'))
    manual = bool(result['configuration'].get('manual_workbench'))
    entry_allowed = healthy and config_verified and result['entry_authority'] == 'ARMED'
    external_adoption_ready = connected and any(
        row.get('eligible') or row.get('lot_review_available') for row in external_positions)
    operational_reason = ('LEDGER_WRITE_FAILED' if not writable else
                          'GATEWAY_DISCONNECTED' if not connected else None)
    healthy_reason = ('CONFIG_SNAPSHOT_INVALID' if not config_verified else
                      'LEDGER_WRITE_FAILED' if not writable else
                      'CLOCK_FAULT' if connection['clock_fault'] else
                      'RUNTIME_NOT_READY')
    return {
        'ARM_ENTRIES': (healthy and config_verified,
                        None if healthy and config_verified else healthy_reason),
        'PAUSE_NEW_ENTRIES': (writable, None if writable else 'LEDGER_WRITE_FAILED'),
        'STOP_DSV_STRATEGY': (writable, None if writable else 'LEDGER_WRITE_FAILED'),
        'SET_ENTRY_POLICY': (config_verified and writable,
                             None if config_verified and writable else
                             'CONFIG_SNAPSHOT_INVALID' if not config_verified else 'LEDGER_WRITE_FAILED'),
        'SAVE_DSV_PARAMETERS': (bool(result['configuration'].get('live_parameter_save'))
                                and config_verified and writable,
                                None if result['configuration'].get('live_parameter_save')
                                and config_verified and writable else
                                'LEDGER_WRITE_FAILED' if not writable
                                else 'CONFIG_SNAPSHOT_INVALID' if not config_verified
                                else 'CONFIG_EDIT_OFFLINE_ONLY'),
        'MANUAL_OPEN': (manual and entry_allowed,
                        None if manual and entry_allowed else
                        'MANUAL_MODE_REQUIRED' if not manual else 'ENTRY_NOT_ALLOWED'),
        'MANUAL_CLOSE': (operational, None if operational else operational_reason),
        'MANUAL_CLOSE_ALL': (operational, None if operational else operational_reason),
        'CANCEL_SELECTED': (operational, None if operational else operational_reason),
        'CANCEL_OWNED': (operational, None if operational else operational_reason),
        'FLATTEN_THEN_STOP': (operational, None if operational else operational_reason),
        'CANCEL_NATIVE_ACCOUNT': (
            operational and bool(result['configuration'].get('native_cancel_review', {}).get('eligible')),
            None if operational and result['configuration'].get('native_cancel_review', {}).get('eligible')
            else operational_reason or 'NATIVE_ACCOUNT_REVIEW_REQUIRED'),
        'ADOPT_EXTERNAL_POSITION': (external_adoption_ready and config_verified and writable,
                                    None if external_adoption_ready and config_verified and writable else
                                    'CONFIG_SNAPSHOT_INVALID' if not config_verified else
                                    'LEDGER_WRITE_FAILED' if not writable else
                                    'GATEWAY_DISCONNECTED' if not connected else
                                    'EXTERNAL_POSITION_REVIEW_REQUIRED'),
        'REQUEST_RECONCILIATION': (writable, None if writable else 'LEDGER_WRITE_FAILED'),
    }

