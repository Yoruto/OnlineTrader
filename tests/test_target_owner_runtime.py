"""Public-entry acceptance for the isolated Standalone target owner."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import get_ident
from types import MethodType, SimpleNamespace
import copy
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.contracts.validation import validate_message
from online_trader.dsv_runtime.controller import ExplicitOrderController, SendNotAttempted, terminal
from online_trader.dsv_runtime.order_diagnostics import attach_order_blocking_log, classify_broker_rejection
from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
from online_trader.dsv_runtime.runner import DsvSession, initial_state
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.dsv_runtime.standalone_owner import StandaloneDsvOwner
from online_trader.dsv_runtime.standalone_projection import workbench_view
from online_trader.dsv_storage.journal import Journal
from online_trader.errors import TraderError
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.account_cache import BrokerHistoryWriter
from online_trader.runtime.portable_config import PortableAccountStore
from online_trader.runtime.instance import AccountLock, RuntimeLock
from online_trader.runtime.session_storage import RuntimeSessionStorage
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.runtime.target_owner_startup import (
    _resolve_trusted_pid, classify_owner_tick_error, execute_owner_tick,
    resolve_active_trading_day, run_target_owner,
)
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.transport.status_client import StatusClient
from qa.target_owner.offline_runtime import CONTRACT, DAY, OfflineTargetRuntime, frame, order_fact, profile
from qa.target_owner.process_runtime import OfflineTargetOwnerProcess


class TargetOwnerPureLogicTests(unittest.TestCase):
    """Cross-platform unit tests for Target Owner trading-day, error-tier, and auto-arm logic."""

    def test_market_break_retains_current_trading_day_until_next_session_is_active(self):
        class FakeCalendar:
            def __init__(self):
                self.window = {
                    'complete': True,
                    'is_trading': False,
                    'session': None,
                    'next_session': {'trading_day': '2026-11-16', 'open_utc': '2026-11-13T13:00:00Z'},
                }

            def query(self, _code, _now_utc):
                return dict(self.window)

        cal = FakeCalendar()
        self.assertEqual(
            resolve_active_trading_day(cal, (CONTRACT,), DAY, '2026-11-13T07:30:00Z'),
            DAY,
        )
        cal.window = {
            'complete': True,
            'is_trading': True,
            'session': {'trading_day': '2026-11-16', 'open_utc': '2026-11-13T13:00:00Z'},
            'next_session': None,
        }
        self.assertEqual(
            resolve_active_trading_day(cal, (CONTRACT,), DAY, '2026-11-13T13:01:00Z'),
            '2026-11-16',
        )

    def test_mixed_night_and_day_sessions_advance_trading_day_when_active_contracts_agree_and_fail_closed_on_conflict(self):
        class MixedCalendar:
            def __init__(self, windows):
                self.windows = windows

            def query(self, code, _now_utc):
                return dict(self.windows[code])

        # Scenario 1: Night session active (DCE.p2701 -> 2026-11-16) while day-only
        # contract (CFFEX.IF2612) is inactive -> must advance to 2026-11-16.
        night_plus_day = MixedCalendar({
            'DCE.p2701': {
                'complete': True,
                'is_trading': True,
                'session': {'trading_day': '2026-11-16', 'open_utc': '2026-11-13T13:00:00Z'},
                'next_session': None,
            },
            'CFFEX.IF2612': {
                'complete': True,
                'is_trading': False,
                'session': None,
                'next_session': {'trading_day': '2026-11-16', 'open_utc': '2026-11-16T01:30:00Z'},
            },
        })
        self.assertEqual(
            resolve_active_trading_day(
                night_plus_day, ('DCE.p2701', 'CFFEX.IF2612'), DAY, '2026-11-13T13:01:00Z'),
            '2026-11-16',
        )

        # Scenario 2: Two active contracts disagree on trading_day -> fail closed with E_DSV_SCOPE.
        conflicting = MixedCalendar({
            'DCE.p2701': {
                'complete': True,
                'is_trading': True,
                'session': {'trading_day': '2026-11-16'},
            },
            'SHFE.rb2701': {
                'complete': True,
                'is_trading': True,
                'session': {'trading_day': '2026-11-13'},
            },
        })
        with self.assertRaises(TraderError) as conflict_err:
            resolve_active_trading_day(
                conflicting, ('DCE.p2701', 'SHFE.rb2701'), DAY, '2026-11-13T13:01:00Z')
        self.assertEqual(conflict_err.exception.code, 'E_DSV_SCOPE')

    def test_incomplete_calendar_window_holds_current_trading_day_even_when_other_contract_is_active(self):
        class IncompleteCalendar:
            def __init__(self):
                self.windows = {
                    'DCE.p2701': {
                        'complete': True,
                        'is_trading': True,
                        'session': {'trading_day': '2026-11-16'},
                    },
                    'SHFE.rb2701': {
                        'complete': False,
                        'is_trading': False,
                        'session': None,
                    },
                }

            def query(self, code, _now_utc):
                return dict(self.windows[code])

        cal = IncompleteCalendar()
        self.assertEqual(
            resolve_active_trading_day(
                cal, ('DCE.p2701', 'SHFE.rb2701'), DAY, '2026-11-13T13:01:00Z'),
            DAY,
        )

    def test_owner_tick_error_classification_rules(self):
        self.assertEqual(classify_owner_tick_error(OSError('pipe timeout')), 'TRANSIENT_DISCONNECT')
        self.assertEqual(
            classify_owner_tick_error(TraderError('E_DSV_SDK_SYNC', 'transient sync loss')),
            'TRANSIENT_DISCONNECT',
        )
        self.assertEqual(
            classify_owner_tick_error(TraderError('E_DSV_NATIVE_FACT', 'fact mismatch')),
            'PERSISTENT_DISARM',
        )
        self.assertEqual(
            classify_owner_tick_error(TraderError('E_DSV_OWNERSHIP', 'ownership conflict')),
            'PERSISTENT_DISARM',
        )
        self.assertEqual(
            classify_owner_tick_error(TraderError('E_DSV_SDK_SCOPE', 'unexpected day switch')),
            'PERSISTENT_DISARM',
        )
        self.assertEqual(
            classify_owner_tick_error(TraderError('E_DSV_STORE', 'sqlite failure')),
            'FAIL_STOP',
        )
        self.assertEqual(classify_owner_tick_error(RuntimeError('bug')), 'FAIL_STOP')
        self.assertEqual(classify_owner_tick_error(KeyError('missing')), 'FAIL_STOP')

    def test_initial_auto_arm_survives_transient_error_and_confirms_once_while_persistent_disarm_cancels_it(self):
        class StubOwner:
            def __init__(self, outcomes):
                self.outcomes = list(outcomes)
                self.session = SimpleNamespace(_failed=False)
                self.state = {'synchronized': False, 'running': False, 'owner_fault_code': None}
                self.start_confirmed_calls = 0
                self.disconnect_calls = []
                self.authority_generation = 0

            def step(self, **_kwargs):
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                self.state.update(outcome)
                return {'type': 'APPLIED'}

            def snapshot(self):
                return dict(self.state)

            def start_confirmed(self):
                self.start_confirmed_calls += 1
                self.state['running'] = True

            def disconnect_for_error(self, reason, *, disarm=False, fault_code=None):
                self.disconnect_calls.append((reason, disarm, fault_code))
                self.state['synchronized'] = False
                if disarm:
                    self.state['running'] = False
                    self.state['owner_fault_code'] = fault_code

            def native_account_snapshot(self):
                return None

        binding = SimpleNamespace(boot_id='boot-1', account_key='DEV:auto-arm')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1. Transient error before first sync preserves initial_start_pending and arms once recovered.
            owner = StubOwner([
                TraderError('E_DSV_SDK_SYNC', 'transient gateway sync loss'),
                {'synchronized': True},
                {'synchronized': True},
            ])
            loop_state = {
                'last_status': {'phase': 'WAITING_FOR_NATIVE_SYNC', 'entry_authority': 'DISARMED'},
                'last_history_capture': 0.0,
                'initial_start_pending': True,
            }
            for _ in range(3):
                execute_owner_tick(
                    owner, binding=binding, current_trading_day=DAY, is_complete=lambda: True,
                    history_writer=None, status_path=root / 'status.json',
                    diagnostics_path=root / 'diag.json', loop_state=loop_state,
                    sleep_fn=lambda _s: None,
                )
            self.assertEqual(owner.start_confirmed_calls, 1)
            self.assertTrue(owner.state['running'])
            self.assertFalse(loop_state['initial_start_pending'])

            # 2. Persistent disarm cancels initial_start_pending so subsequent sync does not arm.
            owner2 = StubOwner([
                TraderError('E_DSV_NATIVE_FACT', 'fact conflict'),
                {'synchronized': True},
            ])
            loop_state2 = {
                'last_status': {'phase': 'WAITING_FOR_NATIVE_SYNC', 'entry_authority': 'DISARMED'},
                'last_history_capture': 0.0,
                'initial_start_pending': True,
            }
            for _ in range(2):
                execute_owner_tick(
                    owner2, binding=binding, current_trading_day=DAY, is_complete=lambda: True,
                    history_writer=None, status_path=root / 'status2.json',
                    diagnostics_path=root / 'diag2.json', loop_state=loop_state2,
                    sleep_fn=lambda _s: None,
                )
            self.assertEqual(owner2.start_confirmed_calls, 0)
            self.assertFalse(owner2.state['running'])
            self.assertFalse(loop_state2['initial_start_pending'])

            # 3. User STOP/PAUSE during a transient outage before first sync revokes pending auto-arm.
            owner3 = StubOwner([
                TraderError('E_DSV_SDK_SYNC', 'transient gateway sync loss'),
                {'synchronized': True},
            ])
            loop_state3 = {
                'last_status': {'phase': 'WAITING_FOR_NATIVE_SYNC', 'entry_authority': 'DISARMED'},
                'last_history_capture': 0.0,
                'initial_start_pending': True,
                'initial_arm_generation': owner3.authority_generation,
            }
            execute_owner_tick(
                owner3, binding=binding, current_trading_day=DAY, is_complete=lambda: True,
                history_writer=None, status_path=root / 'status3.json',
                diagnostics_path=root / 'diag3.json', loop_state=loop_state3,
                sleep_fn=lambda _s: None,
            )
            self.assertTrue(loop_state3['initial_start_pending'])
            # User explicitly stops/pauses while waiting for sync recovery.
            owner3.authority_generation += 1
            execute_owner_tick(
                owner3, binding=binding, current_trading_day=DAY, is_complete=lambda: True,
                history_writer=None, status_path=root / 'status3.json',
                diagnostics_path=root / 'diag3.json', loop_state=loop_state3,
                sleep_fn=lambda _s: None,
            )
            self.assertEqual(owner3.start_confirmed_calls, 0)
            self.assertFalse(owner3.state['running'])
            self.assertFalse(loop_state3['initial_start_pending'])


@unittest.skipUnless(sys.platform == 'win32', 'Named-pipe Runtime acceptance requires Windows')
class TargetOwnerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = OfflineTargetRuntime(Path(self.temp.name) / 'runtime').start()
        self.addCleanup(self.runtime.close)
        self.client = StatusClient(self.runtime.pipe, timeout=5)

    def wait_view(self, predicate):
        deadline = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline:
            last = self.client.workbench()
            if predicate(last):
                return last
            time.sleep(.02)
        self.fail(f'target owner view did not converge: {last}')

    def synchronize(self):
        self.runtime.submit(frame(1))
        value = self.wait_view(lambda value: value['connection']['synchronized'])
        self.assertEqual(self.runtime.adapter.connection.network_calls, 0)
        return value

    def test_incomplete_baseline_stays_disarmed_then_complete_frame_enables_controls(self):
        initial = self.client.workbench()
        self.assertEqual(initial['entry_authority'], 'DISARMED')
        self.assertFalse(initial['connection']['synchronized'])
        self.assertIn('BROKER_FACTS_INCOMPLETE', [row['reason_code'] for row in initial['blocks']])
        self.runtime.submit(frame(1, complete=False))
        self.wait_view(lambda value: not value['connection']['synchronized'])
        ready = self.synchronize()
        self.assertTrue(next(row for row in ready['capabilities']['operations']
                             if row['operation'] == 'ARM_ENTRIES')['allowed'])

    def test_public_pipe_command_is_bound_idempotent_and_conflicting_content_is_rejected(self):
        self.synchronize()
        before_gen = self.runtime.call_owner(lambda owner: owner.authority_generation)
        scope = {'scope_type': 'ACCOUNT', 'scope_ids': []}
        command_id = 'qa-command-idempotent'
        pause = {'cancel_owned_entry_remainders': False}
        first = self.client.command('PAUSE_NEW_ENTRIES', scope, pause, command_id=command_id,
                                    expected_config_revision='1')
        second = self.client.command('PAUSE_NEW_ENTRIES', scope, pause, command_id=command_id,
                                     expected_config_revision='1')
        self.assertEqual(first, second)
        after_gen = self.runtime.call_owner(lambda owner: owner.authority_generation)
        self.assertEqual(after_gen, before_gen + 1)
        with self.assertRaises(TraderError) as error:
            self.client.command('REQUEST_RECONCILIATION', scope,
                                {'requested_scope': 'FULL_ACCOUNT', 'position_cycle_id': None},
                                command_id=command_id, expected_config_revision='1')
        self.assertEqual(error.exception.code, 'E_DUPLICATE_CONFLICT')

    def test_public_pipe_rejects_wrong_account_boot_and_configuration_bindings(self):
        self.synchronize()
        with self.assertRaises(TraderError) as account:
            StatusClient(self.runtime.pipe, timeout=5, expected_account_key='DEV:wrong-account').workbench()
        self.assertEqual(account.exception.code, 'E_SCOPE')
        with self.assertRaises(TraderError) as boot:
            StatusClient(self.runtime.pipe, timeout=5, expected_kernel_boot_id='runtime-wrong').workbench()
        self.assertEqual(boot.exception.code, 'E_BOOT_MISMATCH')
        with self.assertRaises(TraderError) as config:
            self.client.command(
                'PAUSE_NEW_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                {'cancel_owned_entry_remainders': False}, expected_config_revision='2')
        self.assertEqual(config.exception.code, 'E_REVISION_CONFLICT')

    def test_projection_has_one_rule_set_two_policies_and_bounded_capabilities(self):
        view = self.synchronize()
        self.assertEqual(view['configuration']['execution_core'], 'DSV_TARGET_EXECUTION')
        self.assertEqual(view['configuration']['rule_set_id'], 'DSV_TARGET_EXECUTION')
        self.assertEqual({row['policy_kind'] for row in view['policies']}, {'AUTHORITY', 'EXECUTION'})
        ops = {row['operation'] for row in view['capabilities']['operations']}
        self.assertEqual(len(ops), 11)
        self.assertTrue(view['configuration']['live_parameter_save'])
        self.assertIn('SAVE_DSV_PARAMETERS', ops)
        self.assertIn('SAFE_STOP_FOR_MAINTENANCE', ops)
        self.assertFalse(view['truncated'])

    def test_new_boot_uses_a_new_ledger_and_does_not_inherit_runtime_commands(self):
        self.synchronize()
        self.client.command('PAUSE_NEW_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                            {'cancel_owned_entry_remainders': False},
                            command_id='qa-old-boot', expected_config_revision='1')
        old_boot, old_ledger = self.runtime.boot_id, self.runtime.ledger
        self.runtime.close()
        self.doCleanups()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = OfflineTargetRuntime(Path(self.temp.name) / 'runtime-2').start()
        self.addCleanup(self.runtime.close)
        self.client = StatusClient(self.runtime.pipe, timeout=5)
        current = self.client.workbench()
        self.assertNotEqual(self.runtime.boot_id, old_boot)
        self.assertNotEqual(self.runtime.ledger, old_ledger)
        self.assertEqual(current['commands'], [])
        self.assertFalse(old_ledger.exists())

    def test_external_broker_position_is_flattened_only_after_arm_not_claimed(self):
        self.synchronize()
        self.runtime.submit(frame(2, owned=2))
        disarmed_view = self.wait_view(
            lambda value: (value.get('connection') or {}).get('account_positions') == [{
                'contract_id': CONTRACT,
                'name': '豆油2601',
                'configured': True,
                'actual_long': 2,
                'actual_short': 0,
                'target_long': 0,
                'target_short': 0,
                'excess_long': 2,
                'excess_short': 0,
                'status': 'PREVIEW_NEEDS_CONVERGENCE',
                'startup_reason': 'UNPROVED_NO_TODAY_FILL',
                'market_session_active': True,
            }] or (value.get('connection') or {}).get('account_positions', [{}])[0].get('actual_long') == 2)
        self.assertEqual(disarmed_view['external_positions'], [])
        self.assertEqual(disarmed_view['orders'], [])
        self.assertEqual(self.runtime.adapter.orders, [])
        self.client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1')
        self.runtime.submit(frame(3, owned=2))
        armed_view = self.wait_view(
            lambda value: len(value['orders']) == 1
            and value['markets'][0]['entry_reasons'] == ['CONVERGENCE_FLATTEN_PENDING'])
        self.assertEqual(armed_view['external_positions'], [])
        self.assertEqual(armed_view['cycles'], [])
        self.assertEqual(armed_view['orders'][0]['effect'], 'REDUCE')
        self.assertEqual(armed_view['orders'][0]['original_qty'], 2)
        self.assertEqual(armed_view['orders'][0]['order_source'], 'CONVERGENCE')
        self.assertEqual(len(self.runtime.adapter.orders), 1)
        self.assertEqual(self.runtime.adapter.orders[0]['native_offset'], 'CLOSE')
        self.assertEqual(self.runtime.adapter.orders[0]['direction'], 'SELL')

    def test_flatten_then_stop_waits_for_original_child_terminal_fact(self):
        self.synchronize()
        self.client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1')
        self.runtime.submit(frame(2))
        working = self.wait_view(lambda value: bool(value['orders']))
        child = working['orders'][0]['child_order_id']
        self.client.command(
            'FLATTEN_THEN_STOP', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'execution_policy_id': 'target-execution-current-boot'},
            expected_config_revision='1')
        time.sleep(.05)
        self.assertTrue(self.runtime.thread.is_alive())
        self.runtime.submit(frame(3, facts=[order_fact(child, 'CANCELLED')]))
        self.runtime.thread.join(5)
        self.assertFalse(self.runtime.thread.is_alive())

    def test_flatten_then_stop_shuts_down_flat_runtime_even_when_unsynchronized(self):
        import subprocess
        from online_trader.runtime.shutdown import pid_alive, wait_pid_exit

        initial = self.client.workbench()
        self.assertFalse(initial['connection']['synchronized'])
        self.assertTrue(next(
            row for row in initial['capabilities']['operations']
            if row['operation'] == 'FLATTEN_THEN_STOP')['allowed'])
        receipt = self.client.command(
            'FLATTEN_THEN_STOP', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'execution_policy_id': 'target-execution-current-boot'},
            expected_config_revision='1')
        self.assertEqual(receipt['status'], 'SUCCEEDED')
        self.runtime.thread.join(5)
        self.assertFalse(self.runtime.thread.is_alive())

        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.2)'])
        self.assertTrue(pid_alive(child.pid))
        self.assertTrue(wait_pid_exit(child.pid, timeout=5.0))
        child.wait(timeout=2)
        self.assertFalse(pid_alive(child.pid))

    def test_history_cache_failure_does_not_change_target_authority(self):
        self.synchronize()
        before = self.client.workbench()
        errors = []
        writer = BrokerHistoryWriter(
            Path(self.temp.name) / 'history.db', before['account_key'], 'OFFLINE',
            on_error=errors.append)
        self.addCleanup(writer.close)
        writer.offer({'malformed': True})
        deadline = time.monotonic() + 3
        while not errors and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(errors)
        after = self.client.workbench()
        for key in ('entry_authority', 'cycles', 'orders', 'current_responsibility'):
            self.assertEqual(after[key], before[key])

    def test_workbench_projection_bounds_history_and_declares_omissions(self):
        self.synchronize()
        snapshot = self.runtime.call_owner(lambda owner: copy.deepcopy(owner.snapshot()))
        snapshot['runtime_commands'] = [
            {'command_id': f'qa-command-{index}', 'status': 'COMMITTED',
             'error_code': 'NONE', 'reason': 'qa', 'receipt_revision': '1'}
            for index in range(100)
        ]
        projected = workbench_view(
            snapshot, profile=profile(), adapter=self.runtime.adapter,
            configuration_revision='1')
        self.assertEqual(len(projected['commands']), 64)
        self.assertEqual(projected['history_omitted']['commands'], 36)
        self.assertTrue(projected['history_truncated'])

    def _open_working_position(self):
        self.synchronize()
        self.client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1')
        self.runtime.submit(frame(2))
        working = self.wait_view(lambda value: bool(value['orders']))
        child = working['orders'][0]['child_order_id']
        trade_fact = {
            'kind': 'TRADE', 'child_order_id': child,
            'native_order_id': child + '|qa-native',
            'native_trade_id': child + '|qa-trade-1',
            'quantity': 5, 'price': '1060',
        }
        self.runtime.submit(frame(3, owned=5, facts=[order_fact(child, 'FILLED', filled=5), trade_fact]))
        return self.wait_view(lambda value: bool(value['cycles']) and value['cycles'][0]['remaining_quantity'] == 5)

    def test_manual_close_requires_and_verifies_expected_target_revision(self):
        opened = self._open_working_position()
        cycle = opened['cycles'][0]
        cycle_id = cycle['position_cycle_id']
        rev = cycle['plan_revision']
        scope = {'scope_type': 'CYCLE', 'scope_ids': [cycle_id]}

        # 1. Missing expected_target_revision is rejected with E_SCHEMA.
        with self.assertRaises(TraderError) as missing:
            self.client.command(
                'MANUAL_CLOSE', scope,
                {'position_cycle_id': cycle_id, 'quantity': 5,
                 'execution_policy_id': 'target-execution-current-boot',
                 'external_takeover_id': None},
                expected_config_revision='1')
        self.assertEqual(missing.exception.code, 'E_SCHEMA')

        # 2. Stale expected_target_revision is rejected with E_REVISION_CONFLICT.
        with self.assertRaises(TraderError) as stale:
            self.client.command(
                'MANUAL_CLOSE', scope,
                {'position_cycle_id': cycle_id, 'quantity': 5,
                 'execution_policy_id': 'target-execution-current-boot',
                 'external_takeover_id': None,
                 'expected_target_revision': str(int(rev) + 99)},
                expected_config_revision='1')
        self.assertEqual(stale.exception.code, 'E_REVISION_CONFLICT')

        # 3. Matching expected_target_revision succeeds and advances target_revision.
        receipt = self.client.command(
            'MANUAL_CLOSE', scope,
            {'position_cycle_id': cycle_id, 'quantity': 5,
             'execution_policy_id': 'target-execution-current-boot',
             'external_takeover_id': None,
             'expected_target_revision': rev},
            expected_config_revision='1')
        self.assertEqual(receipt['status'], 'SUCCEEDED')
        with self.assertRaises(TraderError) as replay_old_rev:
            self.client.command(
                'MANUAL_CLOSE', scope,
                {'position_cycle_id': cycle_id, 'quantity': 5,
                 'execution_policy_id': 'target-execution-current-boot',
                 'external_takeover_id': None,
                 'expected_target_revision': rev},
                expected_config_revision='1')
        self.assertEqual(replay_old_rev.exception.code, 'E_REVISION_CONFLICT')

    def test_manual_close_all_requires_and_verifies_per_cycle_target_revisions(self):
        opened = self._open_working_position()
        cycle = opened['cycles'][0]
        cycle_id = cycle['position_cycle_id']
        rev = cycle['plan_revision']
        scope = {'scope_type': 'ACCOUNT', 'scope_ids': []}

        # 1. Missing target revisions map is rejected with E_SCHEMA.
        with self.assertRaises(TraderError) as missing:
            self.client.command(
                'MANUAL_CLOSE_ALL', scope,
                {'execution_policy_id': 'target-execution-current-boot',
                 'price_mode': '对手价',
                 'positions': {cycle_id: 5}},
                expected_config_revision='1')
        self.assertEqual(missing.exception.code, 'E_SCHEMA')

        # 2. Stale target revision in map is rejected with E_REVISION_CONFLICT.
        with self.assertRaises(TraderError) as stale:
            self.client.command(
                'MANUAL_CLOSE_ALL', scope,
                {'execution_policy_id': 'target-execution-current-boot',
                 'price_mode': '对手价',
                 'positions': {cycle_id: 5},
                 'expected_target_revisions': {cycle_id: str(int(rev) + 5)}},
                expected_config_revision='1')
        self.assertEqual(stale.exception.code, 'E_REVISION_CONFLICT')

        # 3. Matching quantity and revision succeeds.
        receipt = self.client.command(
            'MANUAL_CLOSE_ALL', scope,
            {'execution_policy_id': 'target-execution-current-boot',
             'price_mode': '对手价',
             'positions': {cycle_id: 5},
             'expected_target_revisions': {cycle_id: rev}},
            expected_config_revision='1')
        self.assertEqual(receipt['status'], 'SUCCEEDED')

    def test_live_save_dsv_parameters_updates_revision_triggers_and_preserves_scope(self):
        before = self.synchronize()
        self.assertTrue(before['configuration']['live_parameter_save'])
        self.assertEqual(before['configuration']['config_revision'], '1')
        saved_profiles = []
        self.runtime.call_owner(lambda owner: setattr(owner, 'on_parameters_saved', saved_profiles.append))

        updated = profile().to_record()
        updated['settings']['symbols'][0]['open_offset'] = '90'
        updated['settings']['symbols'][0]['stop_offset'] = '10'
        updated['settings']['symbols'][0]['profit_offset'] = '200'
        receipt = self.client.command(
            'SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'profile': updated}, expected_config_revision='1')
        self.assertEqual(receipt['status'], 'SUCCEEDED')
        self.assertEqual(len(saved_profiles), 1)
        self.assertEqual(saved_profiles[0].to_record()['settings']['symbols'][0]['open_offset'], '90')

        after = self.wait_view(
            lambda value: value['configuration']['config_revision'] == '2'
            and value['configuration']['strategy_symbols'][0]['open_offset'] == '90')
        self.assertEqual(after['configuration']['strategy_profile_hash'], saved_profiles[0].content_hash)

        with self.assertRaises(TraderError) as stale:
            self.client.command(
                'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                {'authority_policy_id': 'target-authority-current-boot',
                 'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
                expected_config_revision='1')
        self.assertEqual(stale.exception.code, 'E_REVISION_CONFLICT')

        bad_scope = copy.deepcopy(updated)
        bad_scope['settings']['symbols'][0]['code'] = 'DCE.l2701'
        with self.assertRaises(TraderError) as wrong_scope:
            self.client.command(
                'SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                {'profile': bad_scope}, expected_config_revision='2')
        self.assertEqual(wrong_scope.exception.code, 'E_SCOPE')

        armed = self.client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '2'},
            expected_config_revision='2')
        self.assertEqual(armed['status'], 'SUCCEEDED')

    def test_live_save_dsv_parameters_persist_failure_leaves_runtime_unchanged(self):
        self.synchronize()
        original = profile()

        def fail_persist(_candidate):
            raise TraderError('E_CONFIG_FILE', 'Cannot replace the local configuration file')

        self.runtime.call_owner(lambda owner: setattr(owner, 'on_parameters_saved', fail_persist))
        updated = original.to_record()
        updated['settings']['symbols'][0]['open_offset'] = '90'
        updated['settings']['trade']['close_before_seconds'] = 300
        with self.assertRaises(TraderError) as err:
            self.client.command(
                'SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                {'profile': updated}, expected_config_revision='1')
        self.assertEqual(err.exception.code, 'E_STORAGE')

        state = self.runtime.call_owner(lambda owner: {
            'owner_hash': owner.profile.content_hash,
            'revision': owner.configuration_revision,
            'adapter_hash': (owner.adapter.profile.content_hash
                             if hasattr(owner.adapter, 'profile') else original.content_hash),
            'session_hash': owner.session._profile.content_hash,
            'close_before': owner.session._trade['close_before_seconds'],
        })
        self.assertEqual(state['owner_hash'], original.content_hash)
        self.assertEqual(state['revision'], '1')
        self.assertEqual(state['adapter_hash'], original.content_hash)
        self.assertEqual(state['session_hash'], original.content_hash)
        self.assertEqual(state['close_before'], original.to_record()['settings']['trade']['close_before_seconds'])

        saved_profiles = []
        self.runtime.call_owner(lambda owner: setattr(owner, 'on_parameters_saved', saved_profiles.append))
        retried = self.client.command(
            'SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'profile': updated}, expected_config_revision='1')
        self.assertEqual(retried['status'], 'SUCCEEDED')
        self.assertEqual(len(saved_profiles), 1)
        self.assertEqual(saved_profiles[0].to_record()['settings']['trade']['close_before_seconds'], 300)

    def test_live_save_dsv_parameters_rejects_open_position(self):
        self._open_working_position()
        original = profile()
        updated = original.to_record()
        updated['settings']['trade']['close_before_seconds'] = 300
        with self.assertRaises(TraderError) as err:
            self.client.command(
                'SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                {'profile': updated}, expected_config_revision='1')
        self.assertEqual(err.exception.code, 'E_SCOPE')
        state = self.runtime.call_owner(lambda owner: {
            'owner_hash': owner.profile.content_hash,
            'revision': owner.configuration_revision,
            'close_before': owner.session._trade['close_before_seconds'],
        })
        self.assertEqual(state['owner_hash'], original.content_hash)
        self.assertEqual(state['revision'], '1')
        self.assertEqual(state['close_before'], original.to_record()['settings']['trade']['close_before_seconds'])

    def test_persistent_disarm_blocks_auto_recovery_and_requires_reconciliation(self):
        # Transient disconnect preserves running state while persistent disarm clears it
        # and blocks re-arming until explicit reconciliation.
        self.synchronize()
        self.client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1')
        self.runtime.call_owner(
            lambda owner: owner.disconnect_for_error('TRANSIENT_OWNER_TICK_ERROR', disarm=False))
        transient_state = self.runtime.call_owner(lambda owner: owner.snapshot())
        self.assertFalse(transient_state['synchronized'])
        self.assertTrue(transient_state['running'])

        # Now trigger persistent disarm on invariant conflict.
        self.runtime.call_owner(
            lambda owner: owner.disconnect_for_error(
                'PERSISTENT_OWNER_FACT_CONFLICT', disarm=True, fault_code='E_DSV_NATIVE_FACT'))
        disarmed_state = self.runtime.call_owner(lambda owner: owner.snapshot())
        self.assertFalse(disarmed_state['synchronized'])
        self.assertFalse(disarmed_state['running'])
        self.assertEqual(disarmed_state['owner_fault_code'], 'E_DSV_NATIVE_FACT')

        # Even after a new synchronized frame arrives, the owner stays DISARMED with
        # recovery_required=True and refuses ARM_ENTRIES until REQUEST_RECONCILIATION succeeds.
        self.runtime.submit(frame(2))
        fault_view = self.wait_view(lambda value: value['connection']['synchronized'])
        self.assertEqual(fault_view['entry_authority'], 'DISARMED')
        self.assertTrue(fault_view['connection']['recovery_required'])
        self.assertIn('E_DSV_NATIVE_FACT', [b['reason_code'] for b in fault_view['blocks']])
        self.assertFalse(next(row for row in fault_view['capabilities']['operations']
                              if row['operation'] == 'ARM_ENTRIES')['allowed'])
        with self.assertRaises(TraderError) as blocked_arm:
            self.client.command(
                'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                {'authority_policy_id': 'target-authority-current-boot',
                 'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
                expected_config_revision='1')
        self.assertEqual(blocked_arm.exception.code, 'E_NOT_READY')

        # Explicit reconciliation clears the persistent fault when facts are clean.
        self.client.command(
            'REQUEST_RECONCILIATION', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'requested_scope': 'FULL_ACCOUNT', 'position_cycle_id': None},
            expected_config_revision='1')
        cleared_view = self.wait_view(lambda value: not value['connection']['recovery_required'])
        self.assertTrue(next(row for row in cleared_view['capabilities']['operations']
                             if row['operation'] == 'ARM_ENTRIES')['allowed'])

    def test_process_runtime_harness_lifecycle_and_public_pipe_control(self):
        proc_root = Path(self.temp.name) / 'process-harness'
        with OfflineTargetOwnerProcess(proc_root) as proc:
            self.assertTrue(proc.is_alive)
            self.assertIsNotNone(proc.boot_id)
            client = StatusClient(proc.pipe, timeout=5)
            initial = client.workbench()
            self.assertEqual(initial['entry_authority'], 'DISARMED')
            self.assertFalse(initial['connection']['synchronized'])

            f1 = frame(1)
            accepted = proc.send_fact_frame(f1)
            self.assertEqual(accepted['event_type'], 'FACT_FRAME_ACCEPTED')
            applied = proc.wait_for_frame_applied(f1['id'])
            self.assertEqual(applied['event_type'], 'FACT_FRAME_APPLIED')

            synced = client.workbench()
            self.assertTrue(synced['connection']['synchronized'])
            receipt = client.command(
                'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
                {'authority_policy_id': 'target-authority-current-boot',
                 'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
                expected_config_revision='1')
            self.assertEqual(receipt['status'], 'SUCCEEDED')

            f2 = frame(2)
            proc.send_fact_frame(f2)
            proc.wait_for_frame_applied(f2['id'])
            outbound = proc.wait_for_event('OUTBOUND_SEND', timeout=5)
            self.assertEqual(outbound['adapter_result'], 'RETURNED')

            stopped = proc.teardown()
            self.assertIsNotNone(stopped)
            self.assertEqual(stopped['event_type'], 'OWNER_STOPPED')
            self.assertTrue(stopped['ledger_removed'])


class TargetOwnerExecutionReliabilityTests(unittest.TestCase):
    """Cross-platform tests for SDK pre-queue refusals, negative reconciliation, exit retry tiers, and diagnostics."""

    def _make_controller(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'facts.db'
        scope = {'account_key': 'DEV:reliability', 'provider': 'OFFLINE',
                 'trading_day': DAY, 'configuration_id': 'profile-1'}
        state = initial_state(DAY, [CONTRACT])
        journal = Journal(path, scope, state)
        self.addCleanup(journal.close)
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter.owner = get_ident()
        adapter.enabled = True
        adapter.account_key = scope['account_key']
        adapter.provider = 'OFFLINE'
        adapter.account = object()
        adapter.epoch = 'epoch-1'
        adapter.connection = None
        adapter.session = SimpleNamespace(_closed=False, _journal=journal)
        adapter._seen_effects = set()
        adapter._local_returns = set()
        adapter._unknown_snapshots = {}
        adapter.dispatch_market = {
            CONTRACT: {
                'ticks': [{'last': '100', 'bid1': '100', 'ask1': '100',
                           'day_high': '120', 'day_low': '80', 'tick_size': '1',
                           'lower_limit': '50', 'upper_limit': '200',
                           'native_time': DAY + 'T01:10:00Z', 'candle_id': 'bar-1'}],
                'external_orders': False,
            },
        }
        adapter.clock = lambda: (datetime(2026, 11, 13, 1, 10, tzinfo=timezone.utc), 0)
        adapter.grant = SimpleNamespace(
            value={'contracts': [CONTRACT]},
            permits_day=lambda day: day == DAY,
            in_window=lambda now: True,
        )
        controller = ExplicitOrderController(scope, journal, adapter)
        return Path(temp.name), state, journal, adapter, controller

    @staticmethod
    def _tick(ts_sec=0, price='100'):
        return {
            'last': price, 'bid1': price, 'ask1': price,
            'day_high': '120', 'day_low': '80', 'tick_size': '1',
            'lower_limit': '50', 'upper_limit': '200',
            'native_time': f'{DAY}T01:10:{ts_sec:02d}Z',
            'candle_id': 'bar-1',
        }

    @staticmethod
    def _position(owned=5, exit_target=0):
        return {
            'id': 'position-1', 'contract_id': CONTRACT, 'direction': 'LONG',
            'quantity': 5, 'owned': owned, 'entry_filled': 5, 'entry_notional': '500',
            'children': [], 'entry_stopped': True, 'exit_target': exit_target,
            'exit_revision': 1, 'mode': 'ACTIVE', 'cap': '100', 'context_at': '0',
            'theory': '100', 'open_day': DAY,
            'fills': [{'event_seq': 1, 'contract_id': CONTRACT, 'child_order_id': 'open-1',
                       'direction': 'LONG', 'trade_key': [DAY, 'native-open', 'trade-open'],
                       'trading_day': DAY, 'quantity': 5, 'price': '100',
                       'effect': 'OPEN', 'offset_kind': 'OPEN'}],
        }

    def test_sdk_pre_queue_risk_refusal_returns_definitely_not_sent_without_unknown_occupation(self):
        from tqsdk.api import TqApi
        from tqsdk.exceptions import TqRiskRuleError
        from tqsdk.risk_manager import TqRiskManager

        _tmp, state, journal, adapter, controller = self._make_controller()

        class DenyOpenCloseRule:
            def _could_insert_order(self, _pack):
                return False, '自成交/撤单次数风控拦截'

            def _on_insert_order(self, _pack):
                pass

        risk_manager = TqRiskManager()
        risk_manager.append(DenyOpenCloseRule())
        sent_packs = []

        api = SimpleNamespace(
            _loop=SimpleNamespace(is_running=lambda: False),
            _account=SimpleNamespace(
                _check_valid=lambda acct: acct,
                _is_stock_type=lambda _acct: False,
                _get_account_key=lambda _acct: 'acc-key',
                _get_account_id=lambda _acct: 'acc-id',
            ),
            _data={'quotes': {CONTRACT: SimpleNamespace(ins_class='FUTURE')}},
            _auth=SimpleNamespace(_has_td_grants=lambda _symbol: True),
            _ensure_symbol=lambda _symbol: True,
            _ensure_shfe_ine_combination_supported=lambda *_args: None,
            _risk_manager=risk_manager,
            _send_pack=lambda pack: sent_packs.append(pack),
            get_order=lambda *_args, **_kwargs: {},
        )
        api._get_insert_order_future_pack = MethodType(TqApi._get_insert_order_future_pack, api)
        api.insert_order = MethodType(TqApi.insert_order, api)
        adapter.api = api

        # 1. REDUCE order refused by TqRiskRuleError before _send_pack -> DEFINITELY_NOT_SENT, target stays free.
        row = state['symbols'][CONTRACT]
        pos = self._position(owned=5, exit_target=0)
        row['position'] = pos
        with journal.transaction():
            effect = controller.new_order(
                state, pos, {'contract_id': CONTRACT, 'tick': self._tick(0)}, 'REDUCE', 5, '100', 'CLOSE')
            journal.save(state)
        controller.dispatch(state, [effect])
        self.assertEqual(sent_packs, [])
        order = state['orders'][effect['child_order_id']]
        self.assertEqual(order['send_state'], 'DEFINITELY_NOT_SENT')
        self.assertTrue(terminal(order))
        self.assertEqual(order['local_send_rejection']['error_code'], 'E_DSV_SDK_SEND_RISK')
        self.assertEqual(order['local_send_rejection']['rejection_reason'], 'SDK_RISK_RULE_REFUSED')
        accounting = controller.target_accounting(state, pos, 'REDUCE')
        self.assertEqual(accounting.needed_qty, 5)
        self.assertEqual(accounting.uncertain_children, ())

        # Fresh quote retries cleanly once risk rule clears.
        risk_manager.clear()
        market = {'contract_id': CONTRACT, 'tick': self._tick(1),
                  'long_qty': 5, 'short_qty': 0, 'long_today': 5, 'long_historical': 0,
                  'short_today': 0, 'short_historical': 0, 'external_orders': False}
        with journal.transaction():
            next_effects = controller.plan(state, row, market, 100.0)
            journal.save(state)
        self.assertEqual(len(next_effects), 1)
        controller.dispatch(state, next_effects)
        self.assertEqual(len(sent_packs), 1)
        self.assertEqual(state['orders'][next_effects[0]['child_order_id']]['send_state'], 'CALL_RETURNED')

        # 2. Exception from _send_pack itself is NOT pre-queue -> stays UNKNOWN (no false positive).
        def fail_send_pack(_pack):
            raise TqRiskRuleError('unexpected after pack built')

        api._send_pack = fail_send_pack
        with journal.transaction():
            uncertain_effect = controller.new_order(
                state, pos, {'contract_id': CONTRACT, 'tick': self._tick(2)}, 'REDUCE', 5, '100', 'CLOSE')
            journal.save(state)
        controller.dispatch(state, [uncertain_effect])
        self.assertEqual(state['orders'][uncertain_effect['child_order_id']]['send_state'], 'UNKNOWN')
        self.assertFalse(terminal(state['orders'][uncertain_effect['child_order_id']]))

    def test_unknown_negative_reconciliation_releases_reduce_target_and_unblocks_sync_after_complete_snapshot(self):
        _tmp, state, journal, adapter, controller = self._make_controller()
        row = state['symbols'][CONTRACT]
        pos = self._position(owned=5, exit_target=0)
        row['position'] = pos
        adapter.connection = SimpleNamespace(
            broker_connected=True,
            source_epoch=1,
            complete=lambda: True,
        )
        adapter.api = SimpleNamespace(
            insert_order=SimpleNamespace(),
            get_order=lambda **_kw: {},
            get_trade=lambda **_kw: {},
        )

        def raise_oserror(**_kw):
            raise OSError('network glitch during insert_order')

        adapter.api.insert_order = raise_oserror
        with journal.transaction():
            effect = controller.new_order(
                state, pos, {'contract_id': CONTRACT, 'tick': self._tick(0)}, 'REDUCE', 5, '100', 'CLOSE')
            journal.save(state)
        child_id = effect['child_order_id']
        controller.dispatch(state, [effect])
        adapter._record_unknown_snapshots(state)
        self.assertEqual(state['orders'][child_id]['send_state'], 'UNKNOWN')
        self.assertEqual(controller.target_accounting(state, pos, 'REDUCE').needed_qty, 0)

        # Same epoch disconnect cannot falsely clear UNKNOWN.
        DsvSession._mark_unsynchronized(state)
        same_epoch_absent = adapter._absent_unknown_orders(
            state, {}, {}, [], reconcile=True, snapshot_complete=True)
        self.assertEqual(same_epoch_absent, [])

        runtime = DsvSession.__new__(DsvSession)
        runtime._controller = controller
        runtime._journal = journal
        runtime._scope = controller.scope
        pending = {child_id}
        runtime._synchronize(state, {'native': [], 'sync_orders': [],
                                     'market': [{'contract_id': CONTRACT, 'long_qty': 5, 'short_qty': 0,
                                                 'long_today': 5, 'long_historical': 0,
                                                 'short_today': 0, 'short_historical': 0}]}, pending)
        self.assertFalse(state['synchronized'])

        # Fail-closed validation: absent_orders without broker_order_snapshot_complete is rejected.
        bad_frame = {
            'id': 'f-bad', 'sequence': 1, 'session_id': 's1', 'wall_time': f'{DAY}T01:10:01Z',
            'monotonic_ms': 1000, 'synchronized': True, 'sync_orders': [], 'absent_orders': [child_id],
            'market': [{'contract_id': CONTRACT, 'ticks': [self._tick(1)], 'opening_session': True,
                        'session_open': f'{DAY}T01:00:00Z', 'session_close': f'{DAY}T02:15:00Z',
                        'long_qty': 5, 'short_qty': 0}],
            'native': [],
        }
        runtime._symbols = {CONTRACT: {}}
        with self.assertRaises(TraderError) as bad_err:
            runtime._validate_frame(bad_frame)
        self.assertEqual(bad_err.exception.code, 'E_DSV_ROUND')

        # Reconnect advances source_epoch and complete snapshot proves absence -> confirm_absent + sync.
        adapter.connection.source_epoch = 2
        absent = adapter._absent_unknown_orders(
            state, {}, {}, [], reconcile=True, snapshot_complete=True)
        self.assertEqual(absent, [child_id])
        reconcile_frame = {
            'id': 'f-reconcile', 'sequence': 2, 'session_id': 's2',
            'wall_time': f'{DAY}T01:10:02Z', 'monotonic_ms': 2000,
            'synchronized': True, 'broker_order_snapshot_complete': True,
            'absent_orders': absent, 'sync_orders': [], 'native': [],
            'market': [{'contract_id': CONTRACT, 'ticks': [self._tick(2)], 'opening_session': True,
                        'session_open': f'{DAY}T01:00:00Z', 'session_close': f'{DAY}T02:15:00Z',
                        'long_qty': 5, 'short_qty': 0, 'long_today': 5, 'long_historical': 0,
                        'short_today': 0, 'short_historical': 0}],
        }
        runtime._validate_frame(reconcile_frame)
        with journal.transaction():
            for absent_id in reconcile_frame['absent_orders']:
                controller.confirm_absent(state, absent_id, frame=reconcile_frame)
            runtime._synchronize(state, reconcile_frame, pending)
            journal.save(state)
        self.assertTrue(state['synchronized'])
        self.assertEqual(state['orders'][child_id]['send_state'], 'DEFINITELY_NOT_SENT')
        self.assertTrue(terminal(state['orders'][child_id]))
        self.assertEqual(controller.target_accounting(state, pos, 'REDUCE').needed_qty, 5)
        event_row = journal._db.execute(
            "SELECT payload FROM events WHERE kind='UNKNOWN_NEGATIVE_RECONCILED'").fetchone()
        self.assertIsNotNone(event_row)
        self.assertEqual(strict_loads(event_row[0])['child_order_id'], child_id)

        # Plan immediately emits replacement REDUCE child on fresh quote.
        market = {'contract_id': CONTRACT, 'tick': self._tick(2),
                  'long_qty': 5, 'short_qty': 0, 'long_today': 5, 'long_historical': 0,
                  'short_today': 0, 'short_historical': 0, 'external_orders': False}
        with journal.transaction():
            replacement = controller.plan(state, row, market, 200.0)
            journal.save(state)
        self.assertEqual(len(replacement), 1)
        self.assertEqual(replacement[0]['kind'], 'SEND')
        self.assertEqual(replacement[0]['quantity'], 5)

    def test_exit_broker_rejection_classifies_structural_vs_transient_with_backoff(self):
        self.assertEqual(classify_broker_rejection('平仓量超过持仓'), ('STRUCTURAL', 'INSUFFICIENT_CLOSE_POSITION'))
        self.assertEqual(classify_broker_rejection('无交易权限'), ('STRUCTURAL', 'NO_TRADING_PERMISSION'))
        self.assertEqual(classify_broker_rejection('柜台系统繁忙，流控限制'), ('TRANSIENT', 'BROKER_RATE_LIMITED'))
        self.assertEqual(classify_broker_rejection('未连接交易前置，超时'), ('TRANSIENT', 'BROKER_DISCONNECTED'))

        _tmp, state, journal, _adapter, controller = self._make_controller()
        row = state['symbols'][CONTRACT]
        market = {'contract_id': CONTRACT, 'tick': self._tick(0),
                  'long_qty': 5, 'short_qty': 0, 'long_today': 5, 'long_historical': 0,
                  'short_today': 0, 'short_historical': 0, 'external_orders': False}

        # 1. Transient rejections retry beyond 3 attempts using backoff schedule.
        pos = self._position(owned=5, exit_target=0)
        row['position'] = pos
        now = 1000.0
        with journal.transaction():
            current_eff = controller.new_order(state, pos, market, 'REDUCE', 5, '100', 'CLOSE')
            journal.save(state)
        for idx in range(4):
            child = current_eff['child_order_id']
            state['orders'][child].update(
                send_state='NATIVE_SEEN', native_id=f'nat-tr-{idx}',
                broker_state='REJECTED', terminal_confirmed=True,
                reported_fill_qty=0, broker_rejection_category='TRANSIENT',
                broker_rejection_detail='BROKER_BUSY')
            market['tick'] = self._tick(idx + 1)
            with journal.transaction():
                first_try = controller.plan(state, row, market, now)
                if idx == 0:
                    self.assertEqual(len(first_try), 1)
                    current_eff = first_try[0]
                else:
                    self.assertEqual(first_try, [])
                    self.assertEqual(pos.get('execution_reason'), 'EXIT_TRANSIENT_RETRY_BACKOFF')
                    now += 10.0
                    after_backoff = controller.plan(state, row, market, now)
                    self.assertEqual(len(after_backoff), 1)
                    current_eff = after_backoff[0]
                journal.save(state)
            self.assertFalse(pos.get('exit_manual_takeover', False))

        # 2. Structural rejection enters EXIT_MANUAL_TAKEOVER_REQUIRED immediately on 1st failure.
        _tmp2, state2, journal2, _adapter2, controller2 = self._make_controller()
        row2 = state2['symbols'][CONTRACT]
        pos2 = self._position(owned=5, exit_target=0) | {'id': 'position-2'}
        row2['position'] = pos2
        with journal2.transaction():
            eff2 = controller2.new_order(state2, pos2, market, 'REDUCE', 5, '100', 'CLOSE')
            state2['orders'][eff2['child_order_id']].update(
                send_state='NATIVE_SEEN', native_id='nat-struct',
                broker_state='REJECTED', terminal_confirmed=True,
                reported_fill_qty=0, broker_rejection_category='STRUCTURAL',
                broker_rejection_detail='INSUFFICIENT_CLOSE_POSITION')
            market['tick'] = self._tick(10)
            blocked = controller2.plan(state2, row2, market, 2000.0)
            journal2.save(state2)
        self.assertEqual(blocked, [])
        self.assertTrue(pos2.get('exit_manual_takeover'))
        self.assertEqual(pos2.get('execution_reason'), 'EXIT_MANUAL_TAKEOVER_REQUIRED')
        self.assertEqual(controller2.target_projection(state2, row2)['status'], 'BLOCKED')

        # 3. Unclassified rejection preserves 3-attempt threshold.
        _tmp3, state3, journal3, _adapter3, controller3 = self._make_controller()
        row3 = state3['symbols'][CONTRACT]
        pos3 = self._position(owned=5, exit_target=0) | {'id': 'position-3'}
        row3['position'] = pos3
        with journal3.transaction():
            eff3 = controller3.new_order(state3, pos3, market, 'REDUCE', 5, '100', 'CLOSE')
            journal3.save(state3)
        for idx in range(3):
            state3['orders'][eff3['child_order_id']].update(
                send_state='NATIVE_SEEN', native_id=f'nat-uncls-{idx}',
                broker_state='REJECTED', terminal_confirmed=True, reported_fill_qty=0)
            market['tick'] = self._tick(15 + idx)
            with journal3.transaction():
                res = controller3.plan(state3, row3, market, 3000.0 + idx * 5.0)
                journal3.save(state3)
            if idx < 2:
                if idx == 1:
                    self.assertEqual(res, [])
                    self.assertEqual(pos3.get('execution_reason'), 'EXIT_TRANSIENT_RETRY_BACKOFF')
                    with journal3.transaction():
                        res = controller3.plan(state3, row3, market, 3000.0 + idx * 5.0 + 2.0)
                        journal3.save(state3)
                self.assertEqual(len(res), 1)
                eff3 = res[0]
                self.assertFalse(pos3.get('exit_manual_takeover', False))
            else:
                self.assertEqual(res, [])
                self.assertTrue(pos3.get('exit_manual_takeover'))
                self.assertEqual(pos3.get('execution_reason'), 'EXIT_MANUAL_TAKEOVER_REQUIRED')

    def test_reduction_route_unavailable_sets_explicit_block_reason_and_diagnostics_log(self):
        tmp, state, journal, _adapter, controller = self._make_controller()
        shfe = 'SHFE.rb2701'
        state['symbols'][shfe] = {'position': None}
        row = state['symbols'][shfe]
        with attach_order_blocking_log(tmp) as log_path:
            pos = self._position(owned=5, exit_target=0) | {'contract_id': shfe}
            pos['fills'][0]['contract_id'] = shfe
            row['position'] = pos
            market = {'contract_id': shfe, 'tick': self._tick(0),
                      'long_qty': 5, 'short_qty': 0, 'long_today': 5, 'long_historical': 0,
                      'short_today': 0, 'short_historical': 0, 'external_orders': False}
            # SHFE with today=5, his=0: proves_position (include_pending=False) succeeds,
            # while pending CLOSE (historical) of 2 lots causes close_routes to fail E_OWNERSHIP.
            fake_pending_id = 'child-pending-bad'
            pos['children'].append(fake_pending_id)
            state['orders'][fake_pending_id] = {
                'child_order_id': fake_pending_id, 'intent_id': 'i-bad', 'attempt_id': fake_pending_id,
                'effect': 'REDUCE', 'original_qty': 2, 'filled_qty': 0, 'reported_fill_qty': 0,
                'limit_price': '100', 'native_offset': 'CLOSE', 'native_trading_day': DAY,
                'send_state': 'NATIVE_SEEN', 'broker_state': 'ALIVE', 'cancel_status': None,
                'terminal_confirmed': False, 'contract_id': shfe, 'direction': 'SELL',
                'position_id': pos['id'], 'native_id': 'nat-bad', 'cancel_reason': None,
            }
            with journal.transaction():
                self.assertEqual(controller.plan(state, row, market, 100.0), [])
                self.assertEqual(controller.plan(state, row, market, 101.0), [])
                journal.save(state)
            self.assertEqual(pos.get('execution_reason'), 'REDUCTION_ROUTE_UNAVAILABLE')
            self.assertEqual(controller.target_projection(state, row)['status'], 'BLOCKED')

        records = [json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines() if line.strip()]
        blocked = [r for r in records if r.get('event') == 'ORDER_BLOCKED'
                   and r.get('reason') == 'REDUCTION_ROUTE_UNAVAILABLE']
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]['contract_id'], shfe)
        self.assertEqual(blocked[0]['effect'], 'REDUCE')
        self.assertIn(blocked[0]['detail'], {'E_OWNERSHIP', 'E_FACT_BARRIER', 'EMPTY_CLOSE_ROUTES'})


class TargetOwnerWorkbenchProjectionTests(unittest.TestCase):
    """Cross-platform unit tests for Target Owner Workbench projections, bars, and fill/order metadata."""

    @staticmethod
    def _make_adapter():
        bars = [
            {'datetime': 0, 'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0},
            {'datetime': 1762740000_000_000_000, 'open': 100.0, 'high': 101.0, 'low': 105.0, 'close': 102.0},
            {'datetime': 1762740060_000_000_000, 'open': 100.0, 'high': 104.0, 'low': 99.0, 'close': 102.0},
        ]
        serial = SimpleNamespace(tail=lambda count: SimpleNamespace(to_dict=lambda _mode: bars[-count:]))
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter.owner = get_ident()
        adapter.enabled = True
        adapter.synchronized = True
        adapter.account_key = 'DEV:proj-test'
        adapter.provider = 'OFFLINE'
        adapter.epoch = 'epoch-proj'
        adapter.contracts = (CONTRACT,)
        adapter.api = SimpleNamespace(is_serial_ready=lambda s: s is serial)
        adapter.connection = SimpleNamespace(
            broker_connected=True,
            rollover_market_facts=lambda code, day: {'main_contract': code, 'expiry_date': '2027-01-15'} if day == DAY else None,
        )
        adapter.calendar = SimpleNamespace(
            session_times=lambda code, day: [['2026-11-13T01:00:00Z', '2026-11-13T02:15:00Z']],
        )
        adapter.grant = SimpleNamespace(value={'calendar_hash': 'cal-hash-1', 'contracts': [CONTRACT]})
        adapter.quotes = {
            CONTRACT: SimpleNamespace(
                open=1025.0, pre_settlement=1030.0, settlement=None, volume=1234,
                open_interest=5678, bid_volume1=10, ask_volume1=12, commission=2.5,
            ),
        }
        adapter.minutes = {CONTRACT: serial}
        adapter._minute_chart_sample_at = None
        adapter._minute_chart_cache = {}
        adapter._health = {'reference_status': 'OBSERVED_OK', 'protection_ready': True}
        adapter.quote_observations = {}
        adapter._local_returns = set()
        adapter.flat_confirmed = False
        adapter.dispatch_market = {
            CONTRACT: {
                'long_qty': 0, 'short_qty': 0, 'multiplier': '10', 'margin_per_lot': '4500',
                'market_session_active': True,
            },
        }
        return adapter

    def test_workbench_view_projects_market_triggers_reasons_day_stop_and_minute_bars(self):
        adapter = self._make_adapter()
        prof = profile()
        snapshot = {
            'scope': {'account_key': 'DEV:proj-test', 'runtime_boot_id': 'runtime-boot-1'},
            'sequence': 7,
            'projection_seq': 7,
            'trading_day': DAY,
            'session_id': 'sess-1',
            'running': False,
            'synchronized': True,
            'orders': {},
            'runtime_commands': [],
            'runtime_identity': {'engine_kind': 'DSV_TARGET_EXECUTION', 'entry_point': 'online-trader-desktop'},
            'run_directory': 'E:/tmp/run-1',
            'latest_decisions': {
                CONTRACT: {
                    'decision_time_utc': '2026-11-13T01:10:05Z',
                    'decisions': [{'reason': 'GEOMETRY_BLOCK'}],
                },
            },
            'symbols': {
                CONTRACT: {
                    'position': None,
                    'day_stop': True,
                    'day_stop_reason': '止盈触发',
                    'net_loss': '-20',
                    'target_count': 1,
                    'market_session_active': True,
                    'tick': {
                        'last': '1060', 'bid1': '1059', 'ask1': '1061',
                        'day_high': '1088', 'day_low': '1020', 'tick_size': '1',
                        'upper_limit': '1200', 'lower_limit': '900',
                        'native_time': '2026-11-13T01:10:05Z',
                    },
                    'quote_observation': {
                        'age_quality': 'VALID',
                        'source_time_raw': '2026-11-13 09:10:05.000000',
                        'source_age_raw_ns': '5000000',
                        'reason': None,
                    },
                },
            },
        }
        view = workbench_view(snapshot, profile=prof, adapter=adapter, configuration_revision='1')
        validate_message(StatusClient._message('WorkbenchSnapshot', {'payload': view, 'content_hash': content_hash(view)}))

        self.assertEqual(view['entry_authority'], 'DISARMED')
        self.assertEqual(view['minute_bars'][CONTRACT], [[29379001, '100', '104', '99', '102']])
        market = view['markets'][0]
        self.assertTrue(market['enabled'])
        self.assertEqual(market['day_stop_reason'], '止盈触发')
        self.assertEqual(market['entry_reasons'], ['GEOMETRY_BLOCK'])
        self.assertEqual(market['decision_time_utc'], '2026-11-13T01:10:05Z')
        self.assertEqual(market['trigger_prices'], {'LONG': '1060', 'SHORT': '1048'})
        self.assertEqual(market['margin'], '4500')
        self.assertEqual(market['volume_multiple'], '10')
        self.assertEqual(market['upper_limit'], '1200')
        self.assertEqual(market['lower_limit'], '900')
        self.assertEqual(market['open'], '1025')
        self.assertEqual(market['volume'], '1234')
        self.assertEqual(market['rollover'], {'main_contract': CONTRACT, 'expiry_date': '2027-01-15'})
        config = view['configuration']
        self.assertEqual(config['run_directory'], 'E:/tmp/run-1')
        self.assertEqual(config['runtime_identity']['engine_kind'], 'DSV_TARGET_EXECUTION')
        self.assertEqual(config['schedule_display']['trading_day'], DAY)
        self.assertEqual(config['strategy_diagnostics']['ntp'], 'NTP_ALIGNED')
        self.assertEqual(view['contract_catalog']['contracts'], [CONTRACT])

        # Disconnecting clears the minute-bar cache.
        adapter.profile = prof
        adapter.session = SimpleNamespace(
            _closed=False, _failed=False,
            _controller=SimpleNamespace(port=adapter),
            snapshot=lambda: {'scope': {
                'account_key': 'DEV:proj-test', 'provider': 'OFFLINE',
                'configuration_id': prof.content_hash,
            }},
            disconnect=lambda _r: None,
        )
        adapter.disconnect(adapter.session)
        self.assertIsNone(adapter._minute_chart_sample_at)
        self.assertEqual(adapter._minute_chart_cache, {})

    def test_workbench_view_projects_cycle_protection_order_and_fill_timestamps(self):
        adapter = self._make_adapter()
        prof = profile()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        scope = {'account_key': 'DEV:proj-test', 'provider': 'OFFLINE',
                 'trading_day': DAY, 'configuration_id': prof.content_hash}
        state = initial_state(DAY, [CONTRACT])
        journal = Journal(Path(temp.name) / 'facts.db', scope, state)
        self.addCleanup(journal.close)
        controller = ExplicitOrderController(scope, journal, adapter)
        pos = {
            'id': 'pos-proj-1', 'contract_id': CONTRACT, 'direction': 'LONG',
            'quantity': 5, 'owned': 0, 'entry_filled': 0, 'entry_notional': '0',
            'children': [], 'entry_stopped': False, 'exit_target': None,
            'target_id': 'pos-proj-1', 'target_revision': 2, 'exit_revision': 0,
            'mode': 'ACTIVE', 'cap': '1065', 'context_at': '0',
            'theory': '1060', 'stop': '8', 'profit': '20', 'peak': '14',
            'half_stage': 1, 'protected': False, 'entry_kind': 'NORMAL_ENTRY',
            'open_day': DAY, 'fills': [],
        }
        state['symbols'][CONTRACT]['position'] = pos
        market = {'contract_id': CONTRACT, 'tick': {'last': '1060'}, 'long_qty': 0, 'short_qty': 0}
        with journal.transaction():
            open_eff = controller.new_order(state, pos, market, 'OPEN', 5, '1060', 'OPEN')
            child_id = open_eff['child_order_id']
            controller.accept_native(state, [
                {
                    'kind': 'ORDER', 'child_order_id': child_id, 'native_order_id': 'nat-open-1',
                    'status': 'FILLED', 'filled_qty': 5, 'source_time_ns': '1762740000100000000',
                    'native_message': '全部成交',
                },
                {
                    'kind': 'TRADE', 'child_order_id': child_id, 'native_order_id': 'nat-open-1',
                    'native_trade_id': 'trd-open-1', 'quantity': 5, 'price': '1060',
                    'fill_day': DAY, 'source_time_ns': '1762740000200000000',
                },
            ])
            pos.update(exit_target=3, exit_manual_takeover=True,
                       rollback_exit={'reason': 'STOP_LOSS', 'price': '1052'})
            journal.save(state)
        snapshot = {
            'scope': {'account_key': 'DEV:proj-test', 'runtime_boot_id': 'runtime-boot-1'},
            'sequence': 9,
            'projection_seq': 9,
            'trading_day': DAY,
            'session_id': 'sess-1',
            'running': True,
            'synchronized': True,
            'orders': state['orders'],
            'runtime_commands': [],
            'symbols': {
                CONTRACT: {
                    'position': pos,
                    'target_execution': {'reduce_needed_qty': 3, 'reduce_pending_qty': 0, 'status': 'BLOCKED',
                                         'decision_reason': 'EXIT_MANUAL_TAKEOVER_REQUIRED'},
                    'day_stop': False,
                    'net_loss': '0',
                    'target_count': 1,
                    'tick': {'last': '1058', 'day_high': '1088', 'day_low': '1020', 'tick_size': '1',
                             'native_time': '2026-11-13T01:12:00Z'},
                    'quote_observation': {'age_quality': 'VALID'},
                },
            },
        }
        view = workbench_view(snapshot, profile=prof, adapter=adapter, configuration_revision='1')
        validate_message(StatusClient._message('WorkbenchSnapshot', {'payload': view, 'content_hash': content_hash(view)}))
        cycle = view['cycles'][0]
        self.assertEqual(cycle['initial_open_confirmation'], {'status': 'CONFIRMED', 'quantity': 5, 'value': '5300'})
        self.assertEqual(cycle['phase1_status'], 'DONE')
        self.assertEqual(cycle['phase2_status'], 'NOT_TRIGGERED')
        self.assertEqual(cycle['phase1_target'], 3)
        self.assertEqual(cycle['max_favorable_move'], '14')
        self.assertEqual(cycle['exit_reason'], 'STOP_LOSS')
        self.assertEqual(cycle['exit_reasons'], ['STOP_LOSS'])
        self.assertEqual(cycle['exit_reference_price'], '1052')
        self.assertEqual(cycle['exit_wait_reason'], 'EXIT_MANUAL_INTERVENTION_REQUIRED')
        self.assertEqual(view['orders'][0]['source_time_ns'], '1762740000100000000')
        self.assertEqual(view['orders'][0]['native_message'], '全部成交')
        self.assertEqual(view['trades'][0]['native_trade_id'], 'trd-open-1')
        self.assertEqual(view['trades'][0]['source_time_ns'], '1762740000200000000')

    def test_standalone_owner_retains_latest_decisions_and_run_directory(self):
        adapter = self._make_adapter()
        prof = profile()
        adapter.step = lambda _session, **_kwargs: {
            'evaluated_at': '2026-11-13T01:15:00Z',
            'decisions': [{'contract_id': CONTRACT, 'reason': 'GEOMETRY_BLOCK'}],
        }
        binding = StandaloneOwnerBinding('runtime-owner-1', 'DEV:proj-test', 'OFFLINE', object(), object())
        fake_session = SimpleNamespace(
            _check_owner=lambda: None,
            _state={'retired_positions': {}},
            _failed=False,
            snapshot=lambda: {
                'scope': {'account_key': 'DEV:proj-test', 'runtime_boot_id': 'runtime-owner-1'},
                'sequence': 1,
                'trading_day': DAY,
                'session_id': 'sess-1',
                'running': False,
                'synchronized': True,
                'orders': {},
                'runtime_identity': {'engine_kind': 'DSV_TARGET_EXECUTION', 'entry_point': 'runtime run'},
                'symbols': {
                    CONTRACT: {
                        'position': None, 'day_stop': False, 'net_loss': '0', 'target_count': 0,
                        'tick': {'last': '1060', 'day_high': '1088', 'day_low': '1020', 'tick_size': '1',
                                 'native_time': '2026-11-13T01:15:00Z'},
                        'quote_observation': {'age_quality': 'VALID'},
                    },
                },
            },
            prepare_shutdown=lambda: {'ready': True},
            close=lambda: None,
        )
        owner = StandaloneDsvOwner(fake_session, adapter, adapter.connection, prof, binding, '1')
        owner.run_directory = 'E:/tmp/owner-run'
        owner.step(trading_day=DAY, complete=True)
        view = owner.workbench()
        self.assertEqual(view['configuration']['run_directory'], 'E:/tmp/owner-run')
        self.assertEqual(view['markets'][0]['decision_time_utc'], '2026-11-13T01:15:00Z')
        self.assertEqual(view['markets'][0]['entry_reasons'], ['GEOMETRY_BLOCK'])

    def test_run_target_owner_accepts_callable_trusted_control_pid_and_preserves_dynamic_lookup(self):
        self.assertIsNone(_resolve_trusted_pid(None))
        self.assertIsNone(_resolve_trusted_pid(True))
        self.assertEqual(_resolve_trusted_pid(-1), -1)
        self.assertEqual(_resolve_trusted_pid(lambda: 4321), 4321)

        class _FakeViewProcess:
            def __init__(self, pid):
                self._pid = pid

            def trusted_pid(self):
                return self._pid

        prof = profile()
        calendar = SimpleNamespace(content_hash='cal-hash-1')
        binding = StandaloneOwnerBinding('boot-1', 'DEV:pid-test', 'OFFLINE', object(), object())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / 'ledger.db'
            grant = SimpleNamespace(
                replacement=False,
                manual_workbench=False,
                automatic_offsets=False,
                account_key='DEV:pid-test',
                provider='OFFLINE',
                content_hash='grant-hash-1',
                value={'ledger_path': str(ledger), 'contracts': [CONTRACT], 'trading_day': DAY},
            )
            session_storage = SimpleNamespace(
                ledger=ledger.resolve(),
                owner_binding=lambda _rl, _al, **_kw: binding,
            )
            fake_connection = SimpleNamespace(
                source_epoch='feed-epoch-1',
                broker_connected=True,
                configure_calendar=lambda _cal: None,
                complete=lambda: True,
                close=lambda: None,
            )
            fake_owner = SimpleNamespace(
                session=SimpleNamespace(runtime_identity=None, prepare_shutdown=lambda: {'ready': True}),
                stop_event=SimpleNamespace(),
                authority_generation=1,
                run_directory=None,
                snapshot=lambda: {'trading_day': DAY},
                close=lambda: None,
            )
            captured_clients = []

            class _FakeControlServer:
                def __init__(self, owner, pipe, *, trusted_client_pid=None, diagnostic_sink=None):
                    captured_clients.append((owner, pipe, trusted_client_pid))

                def serve(self, **_kwargs):
                    return None

            view_process = _FakeViewProcess(4321)
            output_one = root / 'run-positive'
            output_one.mkdir()
            with (
                patch('online_trader.runtime.target_owner_startup.strategy_port',
                      return_value=SimpleNamespace(connect=lambda *_args: fake_connection)),
                patch('online_trader.runtime.target_owner_startup.create_standalone_owner',
                      return_value=fake_owner),
                patch('online_trader.runtime.target_owner_startup.BrokerHistoryWriter',
                      return_value=SimpleNamespace(close=lambda: None)),
                patch('online_trader.runtime.target_owner_startup.ControlServer', _FakeControlServer),
            ):
                rc = run_target_owner(
                    grant=grant, profile=prof, calendar=calendar, credentials='unused',
                    output=output_one, pipe=r'\\.\pipe\test-pid-positive',
                    session_storage=session_storage, runtime_lock=object(), account_lock=object(),
                    clock=SimpleNamespace(epoch='clock-epoch-1'),
                    trusted_control_pid=view_process.trusted_pid,
                    history_cache=root / 'history.db',
                )
            self.assertEqual(rc, 0)
            identity_one = strict_loads((output_one / 'runtime-identity.json').read_bytes())
            self.assertEqual(identity_one['entry_point'], 'online-trader-desktop')
            self.assertEqual(captured_clients[-1][2], view_process.trusted_pid)

            inactive_process = _FakeViewProcess(-1)
            output_two = root / 'run-negative'
            output_two.mkdir()
            with (
                patch('online_trader.runtime.target_owner_startup.strategy_port',
                      return_value=SimpleNamespace(connect=lambda *_args: fake_connection)),
                patch('online_trader.runtime.target_owner_startup.create_standalone_owner',
                      return_value=fake_owner),
                patch('online_trader.runtime.target_owner_startup.BrokerHistoryWriter',
                      return_value=SimpleNamespace(close=lambda: None)),
                patch('online_trader.runtime.target_owner_startup.ControlServer', _FakeControlServer),
            ):
                rc_two = run_target_owner(
                    grant=grant, profile=prof, calendar=calendar, credentials='unused',
                    output=output_two, pipe=r'\\.\pipe\test-pid-negative',
                    session_storage=session_storage, runtime_lock=object(), account_lock=object(),
                    clock=SimpleNamespace(epoch='clock-epoch-1'),
                    trusted_control_pid=inactive_process.trusted_pid,
                    history_cache=root / 'history.db',
                )
            self.assertEqual(rc_two, 0)
            identity_two = strict_loads((output_two / 'runtime-identity.json').read_bytes())
            self.assertEqual(identity_two['entry_point'], 'runtime run')
            self.assertEqual(captured_clients[-1][2], inactive_process.trusted_pid)

    def test_workbench_projection_includes_strategy_profile_and_added_silver_trades(self):
        from online_trader.configuration.dsv import DsvProfile
        from online_trader.runtime.portable_config import PortableAccountStore

        base = profile()
        raw = base.to_record()
        raw['settings']['symbols'].append({
            'name': '白银', 'code': 'SHFE.ag2702',
            'open_offset': '96', 'stop_offset': '15', 'profit_offset': '139',
            'order_volume': 1, 'half_close_ratio': '0.5', 'hysteresis_points': '0',
        })
        added_profile = DsvProfile.from_record(raw)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'private').mkdir(parents=True, exist_ok=True)
        (root / 'runtime.json').write_text(json.dumps({
            'schema_version': 'ONLINE_TRADER_RUNTIME/1',
            'selected_account': 'sim1',
            'accounts': [{'id': 'sim1', 'alias': '测试', 'provider': 'TQKQ', 'broker': '快期模拟', 'line': '默认'}],
        }, ensure_ascii=False), encoding='utf-8')
        (root / 'private' / 'OnlineTrader-defaults.json').write_text(json.dumps({
            'accounts': {'sim1': {'username': 'user-one', 'password': 'secret'}},
        }, ensure_ascii=False), encoding='utf-8')
        (root / 'OnlineTrader-parameters.json').write_text(
            json.dumps(base.to_settings(), ensure_ascii=False), encoding='utf-8')
        store = PortableAccountStore(root)
        store.save_profile(added_profile)
        reloaded = PortableAccountStore(root).profile()
        self.assertEqual(reloaded.content_hash, added_profile.content_hash)

        from qa.recording_port import RecordingPort
        port = RecordingPort()
        with DsvSession(root / 'journal.db', account_key='DEV:ag-proj', provider='OFFLINE',
                        trading_day=DAY, profile=reloaded, port=port) as session:
            adapter = self._make_adapter()
            adapter.dispatch_market = {
                'DCE.p2701': {'margin_per_lot': '5000', 'multiplier': 10, 'market_session_active': True},
                'SHFE.ag2702': {'margin_per_lot': '6000', 'multiplier': 15, 'market_session_active': True},
            }
            snapshot = session.snapshot()
            snapshot['scope'] = snapshot['scope'] | {'runtime_boot_id': 'runtime-ag-proj'}
            view = workbench_view(snapshot, profile=reloaded, adapter=adapter, configuration_revision='1')
            validate_message(StatusClient._message('WorkbenchSnapshot', {'payload': view, 'content_hash': content_hash(view)}))
            self.assertEqual(view['configuration']['strategy_profile'], reloaded.to_record())
            self.assertEqual(view['configuration']['scoped_contracts'], ['DCE.p2701', 'SHFE.ag2702'])
            self.assertEqual([item['contract_id'] for item in view['markets']], ['DCE.p2701', 'SHFE.ag2702'])

            session.command({'id': 'start-ag', 'action': 'START'})
            # Open 1 lot of SHFE.ag2702 when price triggers LONG above 7500 + 96 = 7596
            # after the 5-minute opening block, then confirm fill and verify manual close (REDUCE).
            first = {
                'id': 'qa-ag-1', 'sequence': 1, 'session_id': 'qa-session',
                'wall_time': '2026-11-13T01:10:01Z', 'monotonic_ms': 1000,
                'synchronized': True, 'sync_orders': [],
                'market': [
                    {
                        'contract_id': 'DCE.p2701',
                        'ticks': [{'native_time': '2026-11-13T01:10:01Z', 'candle_id': 'bar-p-1',
                                   'last': '1000', 'day_low': '995', 'day_high': '1005',
                                   'bid1': '999', 'ask1': '1001', 'tick_size': '1',
                                   'lower_limit': '900', 'upper_limit': '1200'}],
                        'opening_session': True,
                        'session_open': '2026-11-13T01:00:00Z', 'session_close': '2026-11-13T02:15:00Z',
                        'long_qty': 0, 'short_qty': 0, 'available': '100000', 'margin_per_lot': '5000',
                    },
                    {
                        'contract_id': 'SHFE.ag2702',
                        'ticks': [
                            {'native_time': '2026-11-13T01:10:01Z', 'candle_id': 'bar-ag-1',
                             'last': '7598', 'day_low': '7500', 'day_high': '7598',
                             'bid1': '7597', 'ask1': '7598', 'tick_size': '1',
                             'lower_limit': '7000', 'upper_limit': '8000'},
                        ],
                        'opening_session': True,
                        'session_open': '2026-11-13T01:00:00Z', 'session_close': '2026-11-13T02:15:00Z',
                        'long_qty': 0, 'short_qty': 0, 'available': '100000', 'margin_per_lot': '6000',
                    },
                ],
                'native': [],
            }
            session.apply_round(first)
            ag_orders = [o for o in session.snapshot()['orders'].values() if o['contract_id'] == 'SHFE.ag2702']
            self.assertEqual(len(ag_orders), 1)
            self.assertEqual(ag_orders[0]['effect'], 'OPEN')
            self.assertEqual(ag_orders[0]['original_qty'], 1)
            open_child = ag_orders[0]['child_order_id']
            second = {
                'id': 'qa-ag-2', 'sequence': 2, 'session_id': 'qa-session',
                'wall_time': '2026-11-13T01:10:02Z', 'monotonic_ms': 2000,
                'synchronized': True, 'sync_orders': [],
                'market': [
                    {
                        'contract_id': 'DCE.p2701', 'ticks': [], 'opening_session': True,
                        'session_open': '2026-11-13T01:00:00Z', 'session_close': '2026-11-13T02:15:00Z',
                        'long_qty': 0, 'short_qty': 0, 'available': '100000', 'margin_per_lot': '5000',
                    },
                    {
                        'contract_id': 'SHFE.ag2702',
                        'ticks': [{'native_time': '2026-11-13T01:10:02Z', 'candle_id': 'bar-ag-2',
                                   'last': '7602', 'day_low': '7500', 'day_high': '7605',
                                   'bid1': '7601', 'ask1': '7603', 'tick_size': '1',
                                   'lower_limit': '7000', 'upper_limit': '8000'}],
                        'opening_session': True,
                        'session_open': '2026-11-13T01:00:00Z', 'session_close': '2026-11-13T02:15:00Z',
                        'long_qty': 1, 'short_qty': 0, 'available': '94000', 'margin_per_lot': '6000',
                    },
                ],
                'native': [
                    order_fact(open_child, 'FILLED', filled=1),
                    {'kind': 'TRADE', 'child_order_id': open_child, 'native_order_id': open_child + '|qa-native',
                     'native_trade_id': 'trd-ag-1', 'quantity': 1, 'price': '7598'},
                ],
            }
            session.apply_round(second)
            ag_pos = session.snapshot()['symbols']['SHFE.ag2702']['position']
            self.assertIsNotNone(ag_pos)
            self.assertEqual(ag_pos['owned'], 1)
            close_receipt = session.command({
                'id': 'close-ag', 'action': 'REDUCE', 'contract_id': 'SHFE.ag2702',
                'position_id': ag_pos['id'], 'quantity': 1,
                'expected_target_revision': ag_pos['target_revision'],
            })
            self.assertTrue(any(d.get('reason') == 'USER_REDUCE' for d in close_receipt['decisions']))
            self.assertEqual(session.snapshot()['symbols']['SHFE.ag2702']['position']['exit_target'], 0)
            third = {
                'id': 'qa-ag-3', 'sequence': 3, 'session_id': 'qa-session',
                'wall_time': '2026-11-13T01:10:03Z', 'monotonic_ms': 3000,
                'synchronized': True, 'sync_orders': [],
                'market': [
                    {
                        'contract_id': 'DCE.p2701', 'ticks': [], 'opening_session': True,
                        'session_open': '2026-11-13T01:00:00Z', 'session_close': '2026-11-13T02:15:00Z',
                        'long_qty': 0, 'short_qty': 0, 'available': '100000', 'margin_per_lot': '5000',
                    },
                    {
                        'contract_id': 'SHFE.ag2702',
                        'ticks': [{'native_time': '2026-11-13T01:10:03Z', 'candle_id': 'bar-ag-2',
                                   'last': '7603', 'day_low': '7500', 'day_high': '7605',
                                   'bid1': '7602', 'ask1': '7604', 'tick_size': '1',
                                   'lower_limit': '7000', 'upper_limit': '8000'}],
                        'opening_session': True,
                        'session_open': '2026-11-13T01:00:00Z', 'session_close': '2026-11-13T02:15:00Z',
                        'long_qty': 1, 'short_qty': 0, 'available': '94000', 'margin_per_lot': '6000',
                    },
                ],
                'native': [],
            }
            session.apply_round(third)
            ag_close_orders = [o for o in session.snapshot()['orders'].values()
                               if o['contract_id'] == 'SHFE.ag2702' and o['effect'] == 'REDUCE']
            self.assertEqual(len(ag_close_orders), 1)

    def _make_hot_add_harness(self, temp_root, *, calendar_rows=True, expired_silver=False):
        root = Path(temp_root)
        (root / 'private').mkdir(parents=True, exist_ok=True)
        base = profile()
        raw_base = base.to_record()
        raw_base['settings']['symbols'][0]['stop_offset'] = '30'
        raw_base['settings']['symbols'][0]['order_volume'] = 1
        base = DsvProfile.from_record(raw_base)
        (root / 'runtime.json').write_text(json.dumps({
            'schema_version': 'ONLINE_TRADER_RUNTIME/1',
            'selected_account': 'sim1',
            'accounts': [{
                'id': 'sim1', 'alias': '开发模拟', 'provider': 'TQKQ',
                'broker': '快期模拟', 'line': '默认',
            }],
        }, ensure_ascii=False), encoding='utf-8')
        (root / 'private' / 'OnlineTrader-defaults.json').write_text(json.dumps({
            'accounts': {'sim1': {'username': 'user-one', 'password': 'secret'}},
        }, ensure_ascii=False), encoding='utf-8')
        (root / 'OnlineTrader-parameters.json').write_text(
            json.dumps(base.to_settings(), ensure_ascii=False), encoding='utf-8',
        )
        store = PortableAccountStore(root)

        calendar = TradingCalendar.from_record({
            'schema_version': 'TRADING_CALENDAR/1',
            'calendar_id': 'cal-hotadd',
            'calendar_version': f'tqsdk-{SDK_VERSION}',
            'complete': True,
            'coverage_from_utc': '2026-11-12T12:00:00Z',
            'coverage_to_utc': '2026-11-13T08:00:00Z',
            'contracts': [{
                'contract_id': 'DCE.p2701',
                'sessions': [{
                    'session_id': '2026-11-13-day-0',
                    'trading_day': '2026-11-13',
                    'open_utc': '2026-11-13T01:00:00Z',
                    'close_utc': '2026-11-13T02:15:00Z',
                    'is_opening_session': True,
                    'force_close_at_end': True,
                }],
            }],
        })
        kline_calls = []
        bar_ns = int(datetime(2026, 11, 13, 1, 5, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
        serials = {}

        def make_serial(code):
            rows = [
                {'datetime': bar_ns - 60_000_000_000, 'open': 7490.0, 'high': 7510.0, 'low': 7480.0, 'close': 7500.0}
                if code == 'SHFE.ag2702'
                else {'datetime': bar_ns - 60_000_000_000, 'open': 1045.0, 'high': 1055.0, 'low': 1040.0, 'close': 1050.0},
                {'datetime': bar_ns, 'open': 7500.0, 'high': 7520.0, 'low': 7490.0, 'close': 7510.0}
                if code == 'SHFE.ag2702'
                else {'datetime': bar_ns, 'open': 1050.0, 'high': 1060.0, 'low': 1045.0, 'close': 1052.0},
            ]

            class _FakeSerial:
                @property
                def iloc(self):
                    return rows

                def tail(self, count):
                    return SimpleNamespace(to_dict=lambda _mode: rows[-count:])

            return _FakeSerial()

        quotes = {
            'DCE.p2701': SimpleNamespace(
                instrument_id='DCE.p2701', expired=False, ins_class='FUTURE', volume_multiple=10,
                trading_time={'day': [['09:00:00', '10:15:00']], 'night': []},
                datetime='2026-11-13 09:10:00.000000', last_price=1052.0, highest=1060.0, lowest=1040.0,
                bid_price1=1051.0, ask_price1=1053.0, price_tick=1.0, lower_limit=950.0, upper_limit=1150.0,
                margin=5000.0, open=1045.0, pre_settlement=1045.0, settlement=None,
                volume=100, open_interest=200, bid_volume1=5, ask_volume1=5, commission=2.5,
            ),
            'SHFE.ag2702': SimpleNamespace(
                instrument_id='SHFE.ag2702', expired=expired_silver, ins_class='FUTURE', volume_multiple=15,
                trading_time={'day': [['09:00:00', '10:15:00']], 'night': [['21:00:00', '23:00:00']]},
                datetime='2026-11-13 09:10:00.000000', last_price=7505.0, highest=7520.0, lowest=7480.0,
                bid_price1=7504.0, ask_price1=7506.0, price_tick=1.0, lower_limit=7000.0, upper_limit=8000.0,
                margin=6000.0, open=7500.0, pre_settlement=7490.0, settlement=None,
                volume=200, open_interest=300, bid_volume1=5, ask_volume1=5, commission=3.5,
            ),
        }
        orders = {}
        trades = {}
        inserted = []
        positions = {
            'DCE.p2701': SimpleNamespace(
                pos_long=0, pos_short=0, pos_long_today=0, pos_long_his=0,
                pos_short_today=0, pos_short_his=0, margin=0.0,
            ),
            'SHFE.ag2702': SimpleNamespace(
                pos_long=0, pos_short=0, pos_long_today=0, pos_long_his=0,
                pos_short_today=0, pos_short_his=0, margin=0.0,
            ),
        }

        def get_kline_serial(code, interval, data_length=500):
            kline_calls.append((code, interval, data_length))
            if code not in serials:
                serials[code] = make_serial(code)
            return serials[code]

        def get_trading_calendar(start_dt, end_dt):
            if not calendar_rows:
                return []
            days = []
            cur = start_dt
            while cur <= end_dt:
                days.append({'date': cur, 'trading': cur.weekday() < 5})
                cur += timedelta(days=1)
            return days

        def insert_order(**kwargs):
            inserted.append(kwargs)
            symbol = kwargs['symbol']
            exch, inst = symbol.split('.')
            oid = kwargs['order_id']
            vol = int(kwargs['volume'])
            price = float(kwargs['limit_price'])
            orders[oid] = SimpleNamespace(
                order_id=oid, exchange_id=exch, instrument_id=inst,
                direction=kwargs['direction'], offset=kwargs['offset'],
                volume_orign=vol, limit_price=price, volume_left=0,
                status='FINISHED', insert_date_time=bar_ns, is_error=False,
            )
            tid = f'tr-{len(trades) + 1}'
            trades[tid] = SimpleNamespace(
                trade_id=tid, order_id=oid, exchange_id=exch, instrument_id=inst,
                direction=kwargs['direction'], offset=kwargs['offset'],
                volume=vol, price=price, trade_date_time=bar_ns,
            )
            if kwargs['offset'] == 'OPEN':
                if kwargs['direction'] == 'BUY':
                    positions[symbol].pos_long += vol
                    positions[symbol].pos_long_today += vol
                else:
                    positions[symbol].pos_short += vol
                    positions[symbol].pos_short_today += vol
            return orders[oid]

        clock_tick = [0]

        def test_clock():
            sec = clock_tick[0]
            clock_tick[0] += 1
            return (datetime(2026, 11, 13, 1, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=sec),
                    1000 + sec * 1000)

        api = SimpleNamespace(
            get_quote=lambda code: quotes[code],
            get_kline_serial=get_kline_serial,
            get_trading_calendar=get_trading_calendar,
            wait_update=lambda deadline=None: False,
            get_order=lambda account=None: orders,
            get_trade=lambda account=None: trades,
            get_account=lambda account=None: SimpleNamespace(
                balance=200000.0, available=150000.0, margin=5000.0, frozen_margin=0.0, risk_ratio=0.1,
            ),
            get_position=lambda code, account=None: positions[code],
            insert_order=insert_order,
            is_serial_ready=lambda _serial: True,
        )
        account = object()
        account_key = 'SIM:KQ:' + '0' * 16
        grant = DsvSimulationGrant.parse(canonical_bytes({
            'schema_version': 'KQ_DSV_DESKTOP/1',
            'grant_id': 'grant-hotadd-1',
            'account_fingerprint': '0' * 16,
            'ledger_path': str((root / 'ledger.db').resolve()),
            'contracts': ['DCE.p2701'],
            'trading_day': '2026-11-13',
            'trading_days': ['2026-11-13'],
            'resume_entries_on_day_change': False,
            'not_before_utc': '2026-11-13T00:00:00Z',
            'not_after_utc': '2026-11-13T08:00:00Z',
            'max_order_quantity': 2,
            'daily_targets_per_contract': 4,
            'risk_budget': '600',
            'profile_hash': base.content_hash,
            'calendar_hash': calendar.content_hash,
            'authorization_reference': 'DESKTOP_CONFIRM:' + 'a' * 64,
        }))
        connection = SimpleNamespace(
            grant=grant, api=api, account=account, broker_connected=True, read_only=False,
            source_epoch=1, complete=lambda: True, poll_updates=lambda _wait: True,
            close=lambda: None,
            snapshot=lambda: {
                'account_key': account_key, 'complete': True, 'trading_day': '2026-11-13',
                'source_epoch': 1, 'funds': {'balance': '200000', 'available': '150000'},
            },
        )
        adapter = DsvSdkAdapter(
            api, account, account_key=account_key, profile=base, calendar=calendar,
            sdk_version=SDK_VERSION, _connection=connection, owner_mode='STANDALONE',
            clock=test_clock,
        )
        adapter.synchronized = True

        class _TestAccountLock(AccountLock):
            def __init__(self, key):
                self.account_key = key
                self._handle = object()
                self.owner_thread = get_ident()

            def matches(self, key):
                return self.account_key == key and self.owner_thread == get_ident()

        class _TestRuntimeLock(RuntimeLock):
            def __init__(self):
                self.owner_thread = get_ident()

            def matches(self):
                return self.owner_thread == get_ident()

        def make_binding(boot_id):
            return StandaloneOwnerBinding(
                boot_id, account_key, 'TQKQ', _TestRuntimeLock(), _TestAccountLock(account_key),
            )

        return SimpleNamespace(
            root=root, store=store, base=base, adapter=adapter, api=api, quotes=quotes,
            positions=positions, connection=connection, account_key=account_key,
            kline_calls=kline_calls, serials=serials, inserted=inserted,
            make_binding=make_binding,
        )

    def test_sdk_adapter_hot_add_contract_updates_calendar_grant_subscriptions_and_trades_while_existing_position_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self._make_hot_add_harness(temp_dir)
            with RuntimeSessionStorage() as storage:
                binding = env.make_binding(storage.boot_id)
                session = DsvSession(
                    storage.ledger, account_key=env.account_key, provider='TQKQ',
                    trading_day='2026-11-13', profile=env.base, port=env.adapter,
                    _execution_lock=binding.account_lock, _owner_binding=binding,
                )
                owner = StandaloneDsvOwner(
                    session, env.adapter, env.connection, env.base, binding,
                    configuration_revision='1',
                )
                try:
                    owner.on_parameters_saved = env.store.replace_profile
                    owner.on_parameters_restore = lambda token: env.store.replace_profile(token) if token is not None else None
                    env.adapter.step(session, trading_day='2026-11-13', complete=True)
                    owner.command({
                        'command_id': 'start-hotadd-1',
                        'operation': 'ARM_ENTRIES',
                        'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
                        'binding': {'account_key': env.account_key, 'kernel_boot_id': storage.boot_id,
                                    'config_revision': owner.configuration_revision},
                        'parameters': {'origin': 'USER'},
                    })
                    # Trigger breakout entry on DCE.p2701 and reconcile its fill.
                    env.quotes['DCE.p2701'].lowest = 1020.0
                    env.quotes['DCE.p2701'].highest = 1060.0
                    env.quotes['DCE.p2701'].last_price = 1060.0
                    env.quotes['DCE.p2701'].bid_price1 = 1059.0
                    env.quotes['DCE.p2701'].ask_price1 = 1060.0
                    env.quotes['DCE.p2701'].datetime = '2026-11-13 09:10:02.000000'
                    env.adapter.step(session, trading_day='2026-11-13', complete=True)
                    env.quotes['DCE.p2701'].datetime = '2026-11-13 09:10:03.000000'
                    env.adapter.step(session, trading_day='2026-11-13', complete=True)
                    self.assertEqual(session.snapshot()['symbols']['DCE.p2701']['position']['owned'], 1)

                    candidate_raw = env.base.to_record()
                    ag_row = copy.deepcopy(candidate_raw['settings']['symbols'][0])
                    ag_row.update({
                        'name': '白银', 'code': 'SHFE.ag2702',
                        'open_offset': '100', 'stop_offset': '15', 'profit_offset': '60',
                    })
                    candidate_raw['settings']['symbols'].append(ag_row)
                    candidate = DsvProfile.from_record(candidate_raw)

                    receipt = owner.command({
                        'command_id': 'save-hotadd-1',
                        'operation': 'SAVE_DSV_PARAMETERS',
                        'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
                        'binding': {'account_key': env.account_key, 'kernel_boot_id': storage.boot_id,
                                    'config_revision': owner.configuration_revision},
                        'parameters': {'profile': candidate.to_record()},
                    })
                    saved_decisions = [d for d in receipt['decisions'] if d.get('reason') == 'DSV_PARAMETERS_SAVED']
                    self.assertEqual(len(saved_decisions), 1)
                    self.assertEqual(saved_decisions[0].get('added_contracts'), ['SHFE.ag2702'])
                    self.assertEqual(env.adapter.contracts, ('DCE.p2701', 'SHFE.ag2702'))
                    self.assertIn(('SHFE.ag2702', 60, 500), env.kline_calls)
                    self.assertEqual(env.adapter.grant.value['contracts'], ['DCE.p2701', 'SHFE.ag2702'])
                    self.assertEqual(env.adapter.grant.value['profile_hash'], candidate.content_hash)
                    # Initial risk_budget 600 + 15 * 15 (multiplier) * 2 (max_order_quantity) = 1050.
                    self.assertEqual(env.adapter.grant.value['risk_budget'], '1050')
                    self.assertEqual(
                        [row['code'] for row in env.store.profile().to_settings()['symbols']],
                        ['DCE.p2701', 'SHFE.ag2702'],
                    )

                    # Feed breakout quote on newly hot-added SHFE.ag2702 and verify live entry + projection.
                    env.quotes['DCE.p2701'].datetime = '2026-11-13 09:10:05.000000'
                    env.quotes['SHFE.ag2702'].lowest = 7480.0
                    env.quotes['SHFE.ag2702'].highest = 7580.0
                    env.quotes['SHFE.ag2702'].last_price = 7580.0
                    env.quotes['SHFE.ag2702'].bid_price1 = 7579.0
                    env.quotes['SHFE.ag2702'].ask_price1 = 7580.0
                    env.quotes['SHFE.ag2702'].datetime = '2026-11-13 09:10:05.000000'
                    env.adapter.step(session, trading_day='2026-11-13', complete=True)
                    env.quotes['DCE.p2701'].datetime = '2026-11-13 09:10:06.000000'
                    env.quotes['SHFE.ag2702'].datetime = '2026-11-13 09:10:06.000000'
                    env.adapter.step(session, trading_day='2026-11-13', complete=True)
                    view = owner.workbench()
                    self.assertEqual(
                        [row['contract_id'] for row in view['markets']],
                        ['DCE.p2701', 'SHFE.ag2702'],
                    )
                    self.assertEqual(
                        [row['code'] for row in view['configuration']['strategy_profile']['settings']['symbols']],
                        ['DCE.p2701', 'SHFE.ag2702'],
                    )
                    self.assertEqual(session.snapshot()['symbols']['DCE.p2701']['position']['owned'], 1)
                    self.assertEqual(session.snapshot()['symbols']['SHFE.ag2702']['position']['owned'], 1)
                    self.assertTrue(any(call['symbol'] == 'SHFE.ag2702' for call in env.inserted))
                finally:
                    owner.close()

    def test_sdk_adapter_hot_add_incomplete_calendar_rolls_back_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self._make_hot_add_harness(temp_dir, calendar_rows=False)
            with RuntimeSessionStorage() as storage:
                binding = env.make_binding(storage.boot_id)
                session = DsvSession(
                    storage.ledger, account_key=env.account_key, provider='TQKQ',
                    trading_day='2026-11-13', profile=env.base, port=env.adapter,
                    _execution_lock=binding.account_lock, _owner_binding=binding,
                )
                owner = StandaloneDsvOwner(
                    session, env.adapter, env.connection, env.base, binding,
                    configuration_revision='1',
                )
                try:
                    owner.on_parameters_saved = env.store.replace_profile
                    owner.on_parameters_restore = lambda token: env.store.replace_profile(token) if token is not None else None
                    candidate_raw = env.base.to_record()
                    ag_row = copy.deepcopy(candidate_raw['settings']['symbols'][0])
                    ag_row.update({'name': '白银', 'code': 'SHFE.ag2702', 'open_offset': '100', 'stop_offset': '15'})
                    candidate_raw['settings']['symbols'].append(ag_row)
                    candidate = DsvProfile.from_record(candidate_raw)

                    with self.assertRaises(TraderError) as ctx:
                        owner.command({
                            'command_id': 'save-hotadd-fail',
                            'operation': 'SAVE_DSV_PARAMETERS',
                            'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
                            'binding': {'account_key': env.account_key, 'kernel_boot_id': storage.boot_id,
                                        'config_revision': owner.configuration_revision},
                            'parameters': {'profile': candidate.to_record()},
                        })
                    self.assertEqual(ctx.exception.code, 'E_CALENDAR_INCOMPLETE')
                    self.assertEqual(env.adapter.contracts, ('DCE.p2701',))
                    self.assertEqual(env.adapter.grant.value['contracts'], ['DCE.p2701'])
                    self.assertNotIn(('SHFE.ag2702', 60, 500), env.kline_calls)
                    self.assertEqual([row['code'] for row in env.store.profile().to_settings()['symbols']], ['DCE.p2701'])
                finally:
                    owner.close()

    def test_hot_add_with_existing_param_edit_while_holding_position_rejects_and_restores_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self._make_hot_add_harness(temp_dir)
            with RuntimeSessionStorage() as storage:
                binding = env.make_binding(storage.boot_id)
                session = DsvSession(
                    storage.ledger, account_key=env.account_key, provider='TQKQ',
                    trading_day='2026-11-13', profile=env.base, port=env.adapter,
                    _execution_lock=binding.account_lock, _owner_binding=binding,
                )
                owner = StandaloneDsvOwner(
                    session, env.adapter, env.connection, env.base, binding,
                    configuration_revision='1',
                )
                try:
                    owner.on_parameters_saved = env.store.replace_profile
                    owner.on_parameters_restore = lambda token: env.store.replace_profile(token) if token is not None else None
                    env.adapter.step(session, trading_day='2026-11-13', complete=True)
                    owner.command({
                        'command_id': 'start-hotadd-2',
                        'operation': 'ARM_ENTRIES',
                        'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
                        'binding': {'account_key': env.account_key, 'kernel_boot_id': storage.boot_id,
                                    'config_revision': owner.configuration_revision},
                        'parameters': {'origin': 'USER'},
                    })
                    env.quotes['DCE.p2701'].lowest = 1020.0
                    env.quotes['DCE.p2701'].highest = 1060.0
                    env.quotes['DCE.p2701'].last_price = 1060.0
                    env.quotes['DCE.p2701'].bid_price1 = 1059.0
                    env.quotes['DCE.p2701'].ask_price1 = 1060.0
                    env.quotes['DCE.p2701'].datetime = '2026-11-13 09:10:02.000000'
                    env.adapter.step(session, trading_day='2026-11-13', complete=True)
                    env.quotes['DCE.p2701'].datetime = '2026-11-13 09:10:03.000000'
                    env.adapter.step(session, trading_day='2026-11-13', complete=True)
                    self.assertEqual(session.snapshot()['symbols']['DCE.p2701']['position']['owned'], 1)

                    candidate_raw = env.base.to_record()
                    candidate_raw['settings']['symbols'][0]['open_offset'] = '99'
                    ag_row = copy.deepcopy(candidate_raw['settings']['symbols'][0])
                    ag_row.update({'name': '白银', 'code': 'SHFE.ag2702', 'open_offset': '100', 'stop_offset': '15'})
                    candidate_raw['settings']['symbols'].append(ag_row)
                    candidate = DsvProfile.from_record(candidate_raw)

                    with self.assertRaises(TraderError) as ctx:
                        owner.command({
                            'command_id': 'save-hotadd-conflict',
                            'operation': 'SAVE_DSV_PARAMETERS',
                            'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
                            'binding': {'account_key': env.account_key, 'kernel_boot_id': storage.boot_id,
                                        'config_revision': owner.configuration_revision},
                            'parameters': {'profile': candidate.to_record()},
                        })
                    self.assertEqual(ctx.exception.code, 'E_DSV_SCOPE')
                    self.assertEqual(env.adapter.contracts, ('DCE.p2701',))
                    self.assertEqual([row['code'] for row in env.store.profile().to_settings()['symbols']], ['DCE.p2701'])
                finally:
                    owner.close()


class TargetOwnerMaintenanceTests(unittest.TestCase):
    """Tests for forced full-round verification and ABORTED projection during maintenance."""

    def test_safe_stop_for_maintenance_bypasses_idle_skip_and_completes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = TargetOwnerWorkbenchProjectionTests._make_hot_add_harness(self, temp_dir)
            env.adapter.clock = lambda: (datetime(2026, 11, 13, 1, 10, 0, tzinfo=timezone.utc), 1000)
            env.connection.last_poll_updated = False
            with RuntimeSessionStorage() as storage:
                binding = env.make_binding(storage.boot_id)
                session = DsvSession(
                    storage.ledger, account_key=env.account_key, provider='TQKQ',
                    trading_day='2026-11-13', profile=env.base, port=env.adapter,
                    _execution_lock=binding.account_lock, _owner_binding=binding,
                )
                owner = StandaloneDsvOwner(
                    session, env.adapter, env.connection, env.base, binding,
                    configuration_revision='1',
                )
                try:
                    # First step commits a round; within 1 second a normal step would hit _can_skip_idle_round.
                    owner.step(trading_day='2026-11-13', complete=True)
                    seq_before = owner.snapshot()['sequence']
                    # Prove idle optimization would skip a normal step here.
                    idle_receipt = owner.step(trading_day='2026-11-13', complete=True)
                    self.assertIsNone(idle_receipt)
                    self.assertEqual(owner.snapshot()['sequence'], seq_before)

                    owner.command({
                        'command_id': 'maint-complete-1',
                        'operation': 'SAFE_STOP_FOR_MAINTENANCE',
                        'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
                        'binding': {'account_key': env.account_key, 'kernel_boot_id': storage.boot_id,
                                    'config_revision': owner.configuration_revision},
                        'parameters': {'reason': 'SWITCH_ACCOUNT'},
                    })
                    self.assertFalse(owner.stop_event.is_set())

                    # Maintenance step must bypass _can_skip_idle_round and perform a full round.
                    maint_receipt = owner.step(trading_day='2026-11-13', complete=True)
                    self.assertIsNotNone(maint_receipt)
                    self.assertGreater(owner.snapshot()['sequence'], seq_before)
                    self.assertTrue(owner.stop_event.is_set())
                    self.assertEqual(owner.maintenance_completion['command_id'], 'maint-complete-1')
                    self.assertEqual(owner.maintenance_completion['state'], 'COMPLETED')
                finally:
                    owner.close()

    def test_safe_stop_for_maintenance_aborts_and_projects_when_late_position_or_disconnect_occurs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = TargetOwnerWorkbenchProjectionTests._make_hot_add_harness(self, temp_dir)
            with RuntimeSessionStorage() as storage:
                binding = env.make_binding(storage.boot_id)
                session = DsvSession(
                    storage.ledger, account_key=env.account_key, provider='TQKQ',
                    trading_day='2026-11-13', profile=env.base, port=env.adapter,
                    _execution_lock=binding.account_lock, _owner_binding=binding,
                )
                owner = StandaloneDsvOwner(
                    session, env.adapter, env.connection, env.base, binding,
                    configuration_revision='1',
                )
                try:
                    owner.step(trading_day='2026-11-13', complete=True)
                    owner.command({
                        'command_id': 'maint-abort-1',
                        'operation': 'SAFE_STOP_FOR_MAINTENANCE',
                        'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
                        'binding': {'account_key': env.account_key, 'kernel_boot_id': storage.boot_id,
                                    'config_revision': owner.configuration_revision},
                        'parameters': {'reason': 'UPDATE'},
                    })
                    # Disconnect during verification must NOT mark maintenance as verified.
                    disc = owner.step(trading_day='2026-11-13', complete=False)
                    self.assertEqual(disc['type'], 'DISCONNECTED')
                    self.assertFalse(owner._maintenance_verified_step)
                    self.assertFalse(owner.stop_event.is_set())

                    # Now broker reconnects with a late-arriving unresolved convergence cancel / native conflict;
                    # owner must ABORT maintenance and project ABORTED immediately.
                    env.adapter.api.get_order()['late-ext-order-1'] = SimpleNamespace(
                        order_id='late-ext-order-1',
                        instrument_id='DCE.p2701',
                        exchange_id='DCE',
                        direction='BUY',
                        offset='OPEN',
                        status='ALIVE',
                        volume_orign=1,
                        volume_left=1,
                        limit_price=1050.0,
                    )
                    session._state['convergence_cancels']['late-ext-order-1'] = {'cancel_status': 'UNKNOWN'}
                    owner.step(trading_day='2026-11-13', complete=True)
                    self.assertFalse(owner.stop_event.is_set())
                    self.assertIsNone(owner.maintenance)
                    self.assertEqual(owner.maintenance_completion['command_id'], 'maint-abort-1')
                    self.assertEqual(owner.maintenance_completion['state'], 'ABORTED')
                    view = owner.workbench()
                    validate_message({
                        'protocol_version': '1.0',
                        'type': 'WorkbenchSnapshot',
                        'message_id': 'msg-maint-abort',
                        'connection_id': 'conn-1',
                        'correlation_id': None,
                        'sent_at_utc': '2026-11-25T01:00:00Z',
                        'kernel_boot_id': storage.boot_id,
                        'account_key': env.account_key,
                        'body': {'payload': view, 'content_hash': 'b' * 64},
                    })
                    self.assertEqual(view['maintenance_completion']['state'], 'ABORTED')
                    self.assertEqual(view['maintenance_completion']['command_id'], 'maint-abort-1')
                finally:
                    owner.close()

    def test_disarm_clears_strategy_intent_while_temporary_disconnect_preserves_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = TargetOwnerWorkbenchProjectionTests._make_hot_add_harness(self, temp_dir)
            with RuntimeSessionStorage() as storage:
                binding = env.make_binding(storage.boot_id)
                session = DsvSession(
                    storage.ledger, account_key=env.account_key, provider='TQKQ',
                    trading_day='2026-11-13', profile=env.base, port=env.adapter,
                    _execution_lock=binding.account_lock, _owner_binding=binding,
                )
                owner = StandaloneDsvOwner(
                    session, env.adapter, env.connection, env.base, binding,
                    configuration_revision='1',
                )
                try:
                    owner.step(trading_day='2026-11-13', complete=True)
                    owner.start_confirmed()
                    self.assertEqual(owner.strategy_intent, 'RUNNING')
                    # Temporary disconnect (disarm=False) keeps strategy_intent RUNNING.
                    owner.disconnect_for_error('TEMP_NET', disarm=False)
                    self.assertEqual(owner.strategy_intent, 'RUNNING')
                    # Disarming disconnect clears strategy_intent so maintenance is not stuck on STRATEGY_RUNNING.
                    owner.disconnect_for_error('FATAL_DISARM', disarm=True)
                    self.assertEqual(owner.strategy_intent, 'STOPPED')
                    summary = owner.responsibility_summary()
                    self.assertNotIn('STRATEGY_RUNNING', summary['blockers'])
                finally:
                    owner.close()

    def test_clock_pause_preserves_strategy_intent_until_resume_or_user_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = TargetOwnerWorkbenchProjectionTests._make_hot_add_harness(self, temp_dir)
            with RuntimeSessionStorage() as storage:
                binding = env.make_binding(storage.boot_id)
                session = DsvSession(
                    storage.ledger, account_key=env.account_key, provider='TQKQ',
                    trading_day='2026-11-13', profile=env.base, port=env.adapter,
                    _execution_lock=binding.account_lock, _owner_binding=binding,
                )
                owner = StandaloneDsvOwner(
                    session, env.adapter, env.connection, env.base, binding,
                    configuration_revision='1',
                )
                try:
                    owner.step(trading_day='2026-11-13', complete=True)
                    owner.start_confirmed()
                    self.assertEqual(owner.strategy_intent, 'RUNNING')
                    self.assertTrue(session._state['running'])

                    # Simulate temporary clock pause: entries disabled, clock_resume_pending=True.
                    clock_ok = {'ready': False}
                    initial_sample = env.adapter._clock_sample()
                    env.adapter.clock = SimpleNamespace(
                        poll=lambda: None,
                        capture=lambda: initial_sample,
                        entry_ready=lambda: clock_ok['ready'],
                        entry_stable=lambda: clock_ok['ready'],
                        protection_ready=lambda: True,
                        health=lambda _sample: {'reference_status': 'TRUSTED_UTC', 'reason': None},
                    )
                    owner.step(trading_day='2026-11-13', complete=True)
                    self.assertFalse(session._state['running'])
                    self.assertTrue(env.adapter.clock_resume_pending)
                    self.assertEqual(owner.strategy_intent, 'RUNNING')

                    # Clock recovers: adapter auto-resumes entries and strategy_intent stays RUNNING.
                    clock_ok['ready'] = True
                    owner.step(trading_day='2026-11-13', complete=True)
                    self.assertTrue(session._state['running'])
                    self.assertFalse(env.adapter.clock_resume_pending)
                    self.assertEqual(owner.strategy_intent, 'RUNNING')

                    # Pause again, then issue explicit user STOP: clears clock_resume_pending and intent.
                    clock_ok['ready'] = False
                    owner.step(trading_day='2026-11-13', complete=True)
                    self.assertTrue(env.adapter.clock_resume_pending)
                    self.assertEqual(owner.strategy_intent, 'RUNNING')

                    owner.command({
                        'command_id': 'cmd-stop-during-clock-pause',
                        'operation': 'STOP_DSV_STRATEGY',
                        'scope': {'scope_type': 'ACCOUNT', 'scope_ids': []},
                        'binding': {'account_key': env.account_key, 'kernel_boot_id': storage.boot_id,
                                    'config_revision': owner.configuration_revision},
                        'parameters': {},
                    })
                    self.assertEqual(owner.strategy_intent, 'STOPPED')
                    self.assertFalse(env.adapter.clock_resume_pending)
                    self.assertFalse(session._state['running'])

                    owner.step(trading_day='2026-11-13', complete=True)
                    self.assertEqual(owner.strategy_intent, 'STOPPED')
                finally:
                    owner.close()


if __name__ == '__main__':
    unittest.main()
