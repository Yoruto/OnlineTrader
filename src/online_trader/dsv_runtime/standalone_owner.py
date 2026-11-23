"""Construct the explicit DSV owner inside one Standalone Runtime boot.

This factory deliberately does not expose a pipe, start entries or select a
desktop route.  The Standalone lifecycle owns those decisions.  It only binds
an already synchronized SDK connection to the current temporary Journal.
"""

import copy
from concurrent.futures import Future
from importlib.metadata import PackageNotFoundError, version
from threading import Event

from online_trader.contracts.codec import content_hash, payload_hash
from online_trader.contracts.context import ContextRegistry
from online_trader.contracts.validation import validate_message
from online_trader.domain.values import identifier
from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.dsv_runtime.standalone_commands import translate_workbench_command
from online_trader.dsv_runtime.standalone_projection import (
    projection_batch, workbench_positions, workbench_view,
)
from online_trader.errors import TraderError
from online_trader.runtime.startup_metadata import SDK_VERSION


class StandaloneDsvOwner:
    """One-thread facade consumed by the existing Standalone lifecycle."""

    def __init__(self, session, adapter, connection, profile, binding, configuration_revision):
        self.session, self.adapter, self.connection = session, adapter, connection
        self.profile, self.binding = profile, binding
        self.configuration_revision = str(configuration_revision)
        self.closed = False
        self.runtime_protocol = 'DSV_TARGET/1'
        self.kernel_epoch = 1
        self.contexts = ContextRegistry(self.boot_id, self.account_key)
        self.control = self
        self._pending = []
        self._command_receipts = {}
        self._latest_account_snapshot = None
        self._projection_seq = 1
        self._authority_generation = 0
        self._commit_views = [self.snapshot()]
        self.stop_event = Event()
        self._shutdown_after_flatten = False

    @property
    def authority_generation(self):
        return self._authority_generation

    @property
    def revisions(self):
        state = self.snapshot()
        revision = str(max(1, int(state.get('sequence') or 0)))
        return {'state_revision': revision, 'risk_revision': revision,
                'config_revision': self.configuration_revision,
                'authority_revision': revision}

    @property
    def boot_id(self):
        return self.binding.boot_id

    @property
    def account_key(self):
        return self.binding.account_key

    def _check_owner(self):
        self.session._check_owner()

    def snapshot(self):
        state = self.session.snapshot()
        state['projection_seq'] = self._projection_seq
        state['storage_failed'] = bool(getattr(self.session, '_failed', False))
        state['retired_positions'] = copy.deepcopy(self.session._state.get('retired_positions', {}))
        if self._latest_account_snapshot is not None:
            state['account_snapshot'] = copy.deepcopy(self._latest_account_snapshot)
        state['runtime_commands'] = [copy.deepcopy(value)
                                     for value in list(self._command_receipts.values())[-64:]]
        return state

    def projection(self):
        return projection_batch(self.snapshot())

    def workbench(self):
        return workbench_view(self.snapshot(), profile=self.profile, adapter=self.adapter,
                              configuration_revision=self.configuration_revision)

    def prepare_workbench_publication(self):
        snapshot = self.snapshot()
        base = projection_batch(snapshot)
        view = workbench_view(snapshot, profile=self.profile, adapter=self.adapter,
                              configuration_revision=self.configuration_revision)

        def complete():
            return base, view
        return complete

    def positions(self, after=None):
        return workbench_positions(self.snapshot(), after)

    def native_account_snapshot(self):
        """Read one complete broker observation from this owner's SDK connection.

        This value is publication/cache input only.  Target accounting continues
        to consume the ordered facts processed by ``step``.
        """
        self._check_owner()
        if (not self.connection.broker_connected or self.connection.read_only
                or not self.connection.complete()):
            return None
        snapshot = self.connection.snapshot()
        if (snapshot.get('account_key') != self.account_key or not snapshot.get('complete')
                or snapshot.get('trading_day') is None
                or not isinstance(snapshot.get('funds'), dict)
                or snapshot['funds'].get('balance') is None):
            return None
        self._latest_account_snapshot = copy.deepcopy({
            key: snapshot[key] for key in ('trading_day', 'source_epoch', 'funds')
        })
        return snapshot

    def command(self, body):
        command = translate_workbench_command(
            body, self.session.snapshot(), runtime_boot_id=self.boot_id,
            configuration_revision=self.configuration_revision)
        receipt = self.adapter.command(self.session, command)
        if command['action'] in {'START', 'STOP', 'FLATTEN_STOP'}:
            self._authority_generation += 1
        if command['action'] == 'FLATTEN_STOP':
            self._shutdown_after_flatten = True
        self._projection_seq += 1
        self._commit_views.append(self.snapshot())
        self._update_stop_ready()
        return receipt

    def start_confirmed(self):
        """Enable entries after the desktop request and first complete frame."""
        receipt = self.adapter.command(self.session, {
            'id': 'standalone-confirmed-start-' + self.boot_id,
            'action': 'START',
        })
        self._authority_generation += 1
        self._projection_seq += 1
        self._commit_views.append(self.snapshot())
        return receipt

    def receipt(self, command_id):
        return self.session.command_receipt(command_id)

    def step(self, *, trading_day, complete):
        receipt = self.adapter.step(self.session, trading_day=trading_day, complete=complete)
        if receipt is not None:
            self._projection_seq += 1
            self._commit_views.append(self.snapshot())
        self._update_stop_ready()
        return receipt

    def disconnect_for_error(self, reason, *, disarm=False, fault_code=None):
        receipt = self.session.disconnect(reason, disarm=disarm, fault_code=fault_code)
        if disarm:
            self._authority_generation += 1
        self._projection_seq += 1
        self._commit_views.append(self.snapshot())
        self._update_stop_ready()
        return receipt

    def poll(self):
        self._update_stop_ready()
        return 0

    def _update_stop_ready(self):
        if self._shutdown_after_flatten and self.session.prepare_shutdown()['ready']:
            self.stop_event.set()

    def take_committed_views(self):
        views, self._commit_views = self._commit_views, []
        return False, views

    def _make_snapshot(self):
        state = self.snapshot()
        revision = str(max(1, int(state.get('projection_seq') or state.get('sequence') or 0)))
        fault_code = state.get('owner_fault_code')
        return {'account_key': self.account_key, 'kernel_boot_id': self.boot_id,
                'kernel_epoch': str(self.kernel_epoch), 'durable_seq': revision,
                'armed': state['running'],
                'recovery_required': not state['synchronized'] or bool(fault_code),
                'storage_failed': bool(state.get('storage_failed', False)), 'pending_transactions': 0,
                'revisions': self.revisions, 'errors': [fault_code] if fault_code else [], 'cycles': []}

    @staticmethod
    def _ready(value):
        future = Future()
        future.set_result(value)
        return future

    def execute_command(self, message, peer_id, *, operator_actor=None):
        validate_message(message)
        if (message['type'] != 'OperatorCommand' or message['account_key'] != self.account_key
                or message['kernel_boot_id'] != self.boot_id):
            raise TraderError('E_SCOPE', 'Command account or Runtime boot differs from the target owner')
        body = copy.deepcopy(message['body'])
        digest = payload_hash(message)
        if digest != body['payload_hash']:
            raise TraderError('E_SCHEMA', 'Command payload hash differs')
        command_id = identifier(body['command_id'])
        prior = self._command_receipts.get(command_id)
        if prior is None:
            stored = self.session.runtime_command_receipt(command_id)
            prior = None if stored is None else stored['receipt']
        if prior is not None:
            if prior['payload_hash'] != digest:
                raise TraderError('E_DUPLICATE_CONFLICT', 'Command identity changed content')
            return self._ready(copy.deepcopy(prior))
        scope = body['scope']
        ids, kind = scope['scope_ids'], scope['scope_type']
        scope_id = self.account_key if kind in {'ACCOUNT', 'OWNED_ORDERS'} else (
            ids[0] if len(ids) == 1 else content_hash(scope))
        self.contexts.validate(
            body['context_id'], peer_id, 'OPERATOR_COMMAND', body['operation'], scope_id,
            kernel_boot_id=self.boot_id, account_key=self.account_key,
            revisions=body['expected_revisions'], kernel_epoch=body['kernel_epoch'])
        command_body = {key: body[key] for key in ('command_id', 'operation', 'scope', 'parameters')}
        command_body['binding'] = {'account_key': self.account_key, 'kernel_boot_id': self.boot_id,
                                   'config_revision': self.configuration_revision}
        dsv_receipt = self.command(command_body)
        state = self.snapshot()
        seq = str(max(1, int(state.get('projection_seq') or state.get('sequence') or 0)))
        result = {'command_id': command_id, 'payload_hash': digest, 'status': 'SUCCEEDED',
                  'receipt_revision': '1', 'tx_id': 'target-' + content_hash(command_id)[:40],
                  'event_seq': seq, 'durable_seq': seq, 'state_revision': seq,
                  'error_code': 'NONE', 'reason': 'Target execution command committed',
                  'evidence_ids': [dsv_receipt['state_after_digest'][:32]]}
        self.session.record_runtime_command_receipt(command_body, result)
        self._command_receipts[command_id] = copy.deepcopy(result)
        while len(self._command_receipts) > 256:
            self._command_receipts.pop(next(iter(self._command_receipts)))
        return self._ready(result)

    def get_status(self, command_id, digest):
        result = self._command_receipts.get(identifier(command_id))
        if result is None:
            stored = self.session.runtime_command_receipt(command_id)
            result = None if stored is None else stored['receipt']
        if result is None or result['payload_hash'] != digest:
            raise TraderError('E_OWNERSHIP', 'Command record is unavailable')
        return self._ready(copy.deepcopy(result))

    def close(self):
        if self.closed:
            return
        try:
            self.contexts.revoke_all(reason='RUNTIME_OWNER_CLOSED')
            self.session.close()
        finally:
            self.connection.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def create_standalone_owner(database, *, binding, connection, profile, calendar, clock,
                            configuration_revision):
    binding.validate()
    grant = getattr(connection, 'grant', None)
    schema = None if grant is None else grant.value.get('schema_version')
    standalone_scope = schema in {'KQ_DSV_DESKTOP/1', 'SIMNOW_DSV_DESKTOP/1'} or (
        schema == 'SIMNOW_DSV_SIM/1'
        and str(grant.value.get('authorization_reference', '')).startswith('AUTO:'))
    if (grant is None or grant.replacement
            or not standalone_scope
            or (grant.account_key, grant.provider) != (binding.account_key, binding.provider)
            or not connection.broker_connected or connection.read_only or not connection.complete()):
        raise TraderError('E_DSV_SDK_SYNC', 'Standalone DSV owner requires a complete bound broker baseline')
    try:
        installed = version('tqsdk')
    except PackageNotFoundError:
        installed = None
    if installed != SDK_VERSION:
        raise TraderError('E_DEPENDENCY', 'The standalone DSV owner requires the pinned SDK')
    adapter = DsvSdkAdapter(
        connection.api, connection.account, account_key=binding.account_key,
        profile=profile, calendar=calendar, sdk_version=installed,
        _connection=connection, clock=clock, owner_mode='STANDALONE')
    session = DsvSession(
        database, profile=profile, port=adapter, _execution_lock=binding.account_lock,
        _owner_binding=binding, account_key=binding.account_key, provider=binding.provider,
        trading_day=grant.value['trading_day'])
    # A complete broker baseline is still processed through the first native
    # frame.  Construction never grants entry authority.
    session.disconnect('STANDALONE_OWNER_CREATED_DISARMED')
    return StandaloneDsvOwner(session, adapter, connection, profile, binding,
                              configuration_revision)
