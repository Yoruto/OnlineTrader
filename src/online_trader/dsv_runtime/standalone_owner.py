"""Construct the explicit DSV owner inside one Standalone Runtime boot.

This factory deliberately does not expose a pipe, start entries or select a
desktop route.  The Standalone lifecycle owns those decisions.  It only binds
an already synchronized SDK connection to the current temporary Journal.
"""

import copy
from concurrent.futures import Future
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import inspect
from threading import Event

from online_trader.configuration.dsv import DsvProfile
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
from online_trader.runtime.maintenance_policy import evaluate_responsibility
from online_trader.runtime.startup_metadata import SDK_VERSION


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parameters_saved(receipt):
    return any(item.get('reason') == 'DSV_PARAMETERS_SAVED' for item in receipt.get('decisions', ()))


class StandaloneDsvOwner:
    """One-thread facade consumed by the existing Standalone lifecycle."""

    def __init__(self, session, adapter, connection, profile, binding, configuration_revision):
        self.session, self.adapter, self.connection = session, adapter, connection
        self.profile, self.binding = profile, binding
        self.configuration_revision = str(configuration_revision)
        # Persist hook: receives the validated candidate before it is applied and
        # returns a token that ``on_parameters_restore`` uses to undo the write.
        self.on_parameters_saved = None
        self.on_parameters_restore = None
        self.on_parameters_marker_write = None
        self.on_parameters_marker_clear = None
        self.closed = False
        self.runtime_protocol = 'DSV_TARGET/1'
        self.kernel_epoch = 1
        self.contexts = ContextRegistry(self.boot_id, self.account_key)
        self.control = self
        self._pending = []
        self._command_receipts = {}
        self._latest_account_snapshot = None
        self._latest_decisions = {}
        self.run_directory = None
        self._projection_seq = 1
        self._authority_generation = 0
        self._track_commit_views = True
        self.publication_status = None
        self.stop_event = Event()
        self._shutdown_after_flatten = False
        self.strategy_intent = 'STOPPED'
        self.has_start_pending = False
        self.maintenance = None
        self.maintenance_completion = None
        self._maintenance_verified_step = False
        self.config_apply_pending = False
        self.config_recovery_notice = None
        step_fn = getattr(adapter, 'step', None)
        self._adapter_accepts_force_full_round = (
            callable(step_fn) and 'force_full_round' in inspect.signature(step_fn).parameters
        )
        self._commit_views = [self.snapshot()]

    def set_stream_tracking(self, enabled):
        self._track_commit_views = bool(enabled)
        if not self._track_commit_views:
            self._commit_views.clear()

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

    def set_initial_start_pending(self, pending):
        self.has_start_pending = bool(pending)

    def responsibility_summary(self, *, snapshot=None, ignore_maintenance=False):
        state = self.snapshot() if snapshot is None else snapshot
        return evaluate_responsibility(
            state,
            strategy_intent=self.strategy_intent,
            has_start_pending=self.has_start_pending,
            maintenance=None if ignore_maintenance else self.maintenance,
            pending_commands=bool(self._pending),
        )

    def snapshot(self):
        state = self.session.snapshot()
        state['projection_seq'] = self._projection_seq
        state['storage_failed'] = bool(getattr(self.session, '_failed', False))
        state['retired_positions'] = copy.deepcopy(self.session._state.get('retired_positions', {}))
        if self._latest_account_snapshot is not None:
            state['account_snapshot'] = copy.deepcopy(self._latest_account_snapshot)
        state['latest_decisions'] = copy.deepcopy(self._latest_decisions)
        if self.run_directory is not None:
            state['run_directory'] = self.run_directory
        state['runtime_commands'] = [copy.deepcopy(value)
                                     for value in list(self._command_receipts.values())[-64:]]
        state['strategy_intent'] = self.strategy_intent
        state['has_start_pending'] = self.has_start_pending
        state['maintenance'] = copy.deepcopy(self.maintenance)
        if self.maintenance_completion is not None:
            state['maintenance_completion'] = copy.deepcopy(self.maintenance_completion)
        if self.config_recovery_notice is not None:
            state['config_recovery_notice'] = copy.deepcopy(self.config_recovery_notice)
        state['responsibility_summary'] = evaluate_responsibility(
            state,
            strategy_intent=self.strategy_intent,
            has_start_pending=self.has_start_pending,
            maintenance=self.maintenance,
            pending_commands=bool(self._pending),
        )
        return state

    def projection(self):
        return projection_batch(self.snapshot())

    def workbench(self):
        return workbench_view(self.snapshot(), profile=self.profile, adapter=self.adapter,
                              configuration_revision=self.configuration_revision)

    def _capture_projection_inputs(self, snapshot, profile_record):
        adapter = self.adapter
        capture = getattr(adapter, 'projection_market_snapshot', None)
        if callable(capture):
            contracts = [item['code'] for item in profile_record['settings']['symbols']]
            return capture(contracts, snapshot.get('trading_day'), snapshot.get('symbols'))
        from online_trader.dsv_runtime.standalone_projection import _schedule_display
        contracts = [item['code'] for item in profile_record['settings']['symbols']]
        dispatch_copy = {}
        details_copy = {}
        detail_reader = getattr(adapter, 'display_market_details', None)
        for contract in contracts:
            market = getattr(adapter, 'dispatch_market', {}).get(contract) or {}
            mtick = (market.get('ticks') or [None])[-1]
            dispatch_copy[contract] = {
                key: market.get(key)
                for key in ('long_qty', 'short_qty', 'margin_per_lot', 'multiplier', 'market_session_active', 'market_permission')
            }
            if mtick is not None and mtick.get('last') is not None:
                dispatch_copy[contract]['last_price'] = mtick.get('last')
            if callable(detail_reader):
                sym = (snapshot.get('symbols') or {}).get(contract) or {}
                tick = sym.get('tick') or {}
                quote_obs = sym.get('quote_observation') or {}
                fresh = bool(tick and quote_obs.get('age_quality') == 'VALID'
                             and sym.get('market_session_active') is not False)
                details = detail_reader(contract, snapshot.get('trading_day'), fresh=fresh)
                if details:
                    details_copy[contract] = details
        unconfigured_dispatch = {}
        configured_set = set(contracts)
        for contract, market in (getattr(adapter, 'dispatch_market', {}) or {}).items():
            if contract in configured_set:
                continue
            utick = (market.get('ticks') or [None])[-1]
            unconfigured_dispatch[contract] = {
                'long_qty': market.get('long_qty', 0),
                'short_qty': market.get('short_qty', 0),
                'long_today': market.get('long_today', 0),
                'long_historical': market.get('long_historical', 0),
                'short_today': market.get('short_today', 0),
                'short_historical': market.get('short_historical', 0),
                'market_session_active': market.get('market_session_active', False),
                'market_permission': market.get('market_permission'),
                'last_price': None if utick is None else utick.get('last'),
                'calendar_reason': market.get('calendar_reason'),
            }
        bar_reader = getattr(adapter, 'minute_chart_bars', None)
        minute_bars = bar_reader() if callable(bar_reader) else {}
        return {
            'profile_hash': self.profile.content_hash,
            'calendar_hash': adapter.grant.value['calendar_hash'],
            'broker_connected': bool(adapter.connection and adapter.connection.broker_connected),
            'adapter_synchronized': bool(adapter.synchronized),
            'clock_health': copy.deepcopy(getattr(adapter, '_health', None)),
            'market_health': copy.deepcopy(getattr(adapter, 'market_health', None)),
            'schedule_display': _schedule_display(adapter, snapshot.get('trading_day'), contracts),
            'dispatch_market': dispatch_copy,
            'unconfigured_dispatch_market': unconfigured_dispatch,
            'market_details': details_copy,
            'minute_bars': minute_bars,
        }

    def prepare_workbench_publication(self):
        snapshot = self.snapshot()
        profile_record = copy.deepcopy(self.profile.to_record())
        configuration_revision = self.configuration_revision
        projection_inputs = self._capture_projection_inputs(snapshot, profile_record)

        def complete():
            base = projection_batch(snapshot)
            view = workbench_view(
                snapshot,
                profile=profile_record,
                configuration_revision=configuration_revision,
                projection_inputs=projection_inputs,
            )
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
        if self.maintenance is not None and command['action'] in {'START', 'SAVE_PARAMETERS'}:
            raise TraderError('E_NOT_READY', '维护停机进行中，已禁止启动或修改生效配置')
        if command['action'] == 'SAFE_STOP_FOR_MAINTENANCE':
            summary = self.responsibility_summary()
            prep = self.session.prepare_shutdown()
            if not summary['maintenance_allowed'] or not prep['ready']:
                blockers = ','.join(summary['blockers']) or 'RESPONSIBILITY_ACTIVE'
                raise TraderError(
                    'E_NOT_READY',
                    f'当前存在未结交易责任或异常，无法执行无平仓维护停机（{blockers}）',
                )
            self.session.record_maintenance_event('MAINTENANCE_STOP_REQUESTED', {
                'command_id': command['id'],
                'reason': command['reason'],
                'boot_id': self.boot_id,
                'account_key': self.account_key,
                'responsibility': summary,
            })
            self.maintenance = {
                'command_id': command['id'],
                'reason': command['reason'],
                'state': 'VERIFYING',
            }
            self.strategy_intent = 'STOPPED'
            self.has_start_pending = False
            self._maintenance_verified_step = False
            self._authority_generation += 1
            self._projection_seq += 1
            if self._track_commit_views:
                self._commit_views.append(self.snapshot())
            self._update_stop_ready()
            return {
                'type': 'COMMAND',
                'input_id': command['id'],
                'state_after_digest': content_hash(summary),
                'decisions': [{'reason': 'MAINTENANCE_STOP_REQUESTED', 'rule_ids': ['M01']}],
                'effects': [],
            }
        if command['action'] == 'SAVE_PARAMETERS':
            receipt = self._save_parameters(command)
        else:
            receipt = self.adapter.command(self.session, command)
        if command['action'] == 'START':
            self.strategy_intent = 'RUNNING'
            self.has_start_pending = False
            self.config_recovery_notice = None
        elif command['action'] in {'STOP', 'FLATTEN_STOP'}:
            self.strategy_intent = 'STOPPED'
            self.has_start_pending = False
        if command['action'] in {'START', 'STOP', 'FLATTEN_STOP'}:
            self._authority_generation += 1
        if command['action'] == 'FLATTEN_STOP':
            self._shutdown_after_flatten = True
        if command['action'] == 'SAVE_PARAMETERS' and _parameters_saved(receipt):
            self.profile = DsvProfile.from_record(command['profile'])
            self.configuration_revision = str(int(self.configuration_revision) + 1)
        self._projection_seq += 1
        if self._track_commit_views:
            self._commit_views.append(self.snapshot())
        self._update_stop_ready()
        return receipt

    def _run_adapter_save(self, command, prepared=None):
        if prepared is None:
            return self.adapter.command(self.session, command)
        return self.adapter.command(self.session, command, prepared_contracts=prepared)

    def _save_parameters(self, command):
        """Persist the user configuration first, then apply it in this owner turn.

        A validation or file failure leaves the active profile, configuration
        revision, ledger and trading engine unchanged.  If the engine refuses the
        candidate after the file was written, the previous file is restored; an
        unrestorable or partially applied save fails the owner closed.
        """
        candidate = self.session.check_parameter_save(command)
        prepared = None
        if (candidate is not None and command.get('added_contracts')
                and callable(getattr(self.adapter, 'prepare_added_contracts', None))):
            prepared = self.adapter.prepare_added_contracts(
                self.session, command['added_contracts'], candidate,
            )
        if candidate is None or not callable(self.on_parameters_saved):
            return self._run_adapter_save(command, prepared)
        if callable(self.on_parameters_marker_write):
            self.on_parameters_marker_write(
                old_hash=self.profile.content_hash,
                new_hash=candidate.content_hash,
                apply_id=command.get('id'),
            )
        restore_token = self.on_parameters_saved(candidate)
        try:
            receipt = self._run_adapter_save(command, prepared)
        except BaseException as error:
            if self.session._state.get('profile_hash') == candidate.content_hash:
                raise TraderError(
                    'E_DSV_OWNER',
                    '参数已提交执行账本但未能完整应用；Runtime 停止以免参数事实分裂。') from error
            self._restore_parameters(restore_token)
            self._clear_parameters_marker(strict=False)
            raise
        if not _parameters_saved(receipt):
            self._restore_parameters(restore_token)
            self._clear_parameters_marker(strict=False)
            return receipt
        self._clear_parameters_marker(strict=True)
        return receipt

    def _clear_parameters_marker(self, *, strict=True):
        if not callable(self.on_parameters_marker_clear):
            return
        try:
            self.on_parameters_marker_clear()
        except Exception as error:
            if strict:
                self.session._failed = True
                raise TraderError(
                    'E_DSV_OWNER',
                    '参数已生效，但清理待应用标记失败；下次启动需核对配置一致性。',
                ) from error

    def _restore_parameters(self, token):
        if not callable(self.on_parameters_restore):
            return
        try:
            self.on_parameters_restore(token)
        except Exception as error:
            raise TraderError(
                'E_DSV_OWNER',
                '参数未生效，但本地参数文件未能恢复；Runtime 停止以免下次启动使用未生效的参数。') from error

    def start_confirmed(self):
        """Enable entries after the desktop request and first complete frame."""
        if self.maintenance is not None:
            raise TraderError('E_NOT_READY', '维护停机进行中，已禁止自动启动策略')
        if self.config_apply_pending:
            raise TraderError('E_NOT_READY', '上次参数保存曾中断，已禁止自动启动策略，请核对参数后手动启动')
        receipt = self.adapter.command(self.session, {
            'id': 'standalone-confirmed-start-' + self.boot_id,
            'action': 'START',
        })
        self.strategy_intent = 'RUNNING'
        self.has_start_pending = False
        self._authority_generation += 1
        self._projection_seq += 1
        self._commit_views.append(self.snapshot())
        return receipt

    def receipt(self, command_id):
        return self.session.command_receipt(command_id)

    def step(self, *, trading_day, complete):
        verifying = self.maintenance is not None
        if verifying and self._adapter_accepts_force_full_round:
            receipt = self.adapter.step(
                self.session, trading_day=trading_day, complete=complete, force_full_round=True,
            )
        else:
            receipt = self.adapter.step(self.session, trading_day=trading_day, complete=complete)
        if self.strategy_intent == 'RUNNING' and not bool(self.session._state.get('running')):
            self.strategy_intent = 'STOPPED'
        if self.maintenance is not None and receipt is not None and receipt.get('type') != 'DISCONNECTED':
            self._maintenance_verified_step = True
        if receipt is not None:
            configured_contracts = getattr(self.adapter, 'contracts', None)
            if not configured_contracts:
                configured_contracts = {
                    row['code']
                    for row in self.profile.to_record().get('settings', {}).get('symbols', ())
                    if 'code' in row
                }
            grouped = {}
            for decision in receipt.get('decisions', ()):
                contract = decision.get('contract_id')
                if contract in configured_contracts:
                    grouped.setdefault(contract, []).append(copy.deepcopy(decision))
            for contract, decisions in grouped.items():
                self._latest_decisions[contract] = {
                    'decision_time_utc': receipt.get('evaluated_at'),
                    'decisions': decisions,
                }
            self._projection_seq += 1
            if self._track_commit_views:
                self._commit_views.append(self.snapshot())
        self._update_stop_ready()
        return receipt

    def disconnect_for_error(self, reason, *, disarm=False, fault_code=None):
        receipt = self.session.disconnect(reason, disarm=disarm, fault_code=fault_code)
        if disarm:
            self._authority_generation += 1
            self.strategy_intent = 'STOPPED'
            self.has_start_pending = False
        self._projection_seq += 1
        if self._track_commit_views:
            self._commit_views.append(self.snapshot())
        self._update_stop_ready()
        return receipt

    def set_owner_fault_detail(self, detail):
        setter = getattr(self.session, 'set_owner_fault_detail', None)
        if callable(setter):
            setter(detail)
        self._projection_seq += 1

    def pump_fault_hold(self, *, trading_day, complete):
        pumper = getattr(self.adapter, 'poll_fault_hold', None)
        if callable(pumper):
            pumper(self.session, trading_day=trading_day, complete=complete)
        self._projection_seq += 1
        self._update_stop_ready()

    def poll(self):
        self._update_stop_ready()
        return 0

    def _update_stop_ready(self):
        if self._shutdown_after_flatten and self.session.prepare_shutdown()['ready']:
            self.stop_event.set()
            return
        if self.maintenance is not None and self.maintenance.get('state') == 'VERIFYING':
            if not self._maintenance_verified_step:
                return
            summary = self.responsibility_summary(ignore_maintenance=True)
            prep = self.session.prepare_shutdown()
            if summary['maintenance_allowed'] and prep['ready']:
                completed = {
                    'command_id': self.maintenance['command_id'],
                    'reason': self.maintenance['reason'],
                    'state': 'COMPLETED',
                }
                self.maintenance = completed
                self.maintenance_completion = {
                    'schema_version': 'DSV_MAINTENANCE_COMPLETION/1',
                    'runtime_boot_id': self.boot_id,
                    'account_key': self.account_key,
                    'command_id': completed['command_id'],
                    'reason': completed['reason'],
                    'state': 'COMPLETED',
                    'responsibility': summary,
                    'completed_at_utc': _utc_now(),
                }
                try:
                    self.session.record_maintenance_event('MAINTENANCE_STOP_COMPLETED', self.maintenance_completion)
                except Exception:
                    pass
                self.stop_event.set()
            else:
                aborted = {
                    'schema_version': 'DSV_MAINTENANCE_COMPLETION/1',
                    'runtime_boot_id': self.boot_id,
                    'account_key': self.account_key,
                    'command_id': self.maintenance['command_id'],
                    'reason': self.maintenance['reason'],
                    'state': 'ABORTED',
                    'responsibility': summary,
                    'completed_at_utc': _utc_now(),
                }
                self.maintenance = None
                self.maintenance_completion = aborted
                try:
                    self.session.record_maintenance_event('MAINTENANCE_STOP_ABORTED', aborted)
                except Exception:
                    pass
                self._projection_seq += 1
                if self._track_commit_views:
                    self._commit_views.append(self.snapshot())

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
        added_contracts = any(
            item.get('reason') == 'DSV_PARAMETERS_SAVED' and item.get('added_contracts')
            for item in dsv_receipt.get('decisions', ())
        )
        result = {'command_id': command_id, 'payload_hash': digest, 'status': 'SUCCEEDED',
                  'receipt_revision': '1', 'tx_id': 'target-' + content_hash(command_id)[:40],
                  'event_seq': seq, 'durable_seq': seq, 'state_revision': seq,
                  'error_code': 'NONE',
                  'reason': ('新合约参数已保存并生效，已开始同步行情。'
                             if body['operation'] == 'SAVE_DSV_PARAMETERS' and added_contracts
                             else '参数已保存并生效，下一笔新鲜行情使用新参数。'
                             if body['operation'] == 'SAVE_DSV_PARAMETERS'
                             else 'Target execution command committed'),
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
