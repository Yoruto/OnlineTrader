"""Public-entry acceptance for the isolated Standalone target owner."""

from pathlib import Path
import copy
import sys
import tempfile
import time
import unittest

from online_trader.dsv_runtime.standalone_projection import workbench_view
from online_trader.errors import TraderError
from online_trader.runtime.account_cache import BrokerHistoryWriter
from online_trader.transport.status_client import StatusClient
from qa.target_owner.offline_runtime import OfflineTargetRuntime, frame, order_fact, profile


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


if __name__ == '__main__':
    unittest.main()
