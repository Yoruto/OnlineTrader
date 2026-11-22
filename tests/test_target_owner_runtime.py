"""Public-entry acceptance for the isolated Standalone target owner."""

from pathlib import Path
from types import SimpleNamespace
import copy
import sys
import tempfile
import time
import unittest

from online_trader.dsv_runtime.standalone_projection import workbench_view
from online_trader.errors import TraderError
from online_trader.runtime.account_cache import BrokerHistoryWriter
from online_trader.runtime.target_owner_startup import (
    classify_owner_tick_error, execute_owner_tick, resolve_active_trading_day,
)
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
        scope = {'scope_type': 'ACCOUNT', 'scope_ids': []}
        command_id = 'qa-command-idempotent'
        pause = {'cancel_owned_entry_remainders': False}
        first = self.client.command('PAUSE_NEW_ENTRIES', scope, pause, command_id=command_id,
                                    expected_config_revision='1')
        second = self.client.command('PAUSE_NEW_ENTRIES', scope, pause, command_id=command_id,
                                     expected_config_revision='1')
        self.assertEqual(first, second)
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
        self.assertEqual(len({row['operation'] for row in view['capabilities']['operations']}), 9)
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

    def test_external_broker_position_is_not_claimed_or_extended(self):
        self.synchronize()
        self.client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1')
        self.runtime.submit(frame(2, owned=2))
        view = self.wait_view(lambda value: value['checkpoint_event_seq'] == '2')
        self.assertEqual(view['cycles'], [])
        self.assertEqual(view['orders'], [])
        self.assertEqual(self.runtime.adapter.orders, [])

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


if __name__ == '__main__':
    unittest.main()
