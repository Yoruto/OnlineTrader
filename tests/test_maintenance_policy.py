"""Unit tests for Standalone Runtime responsibility and mode derivation."""

import unittest

from online_trader.runtime.maintenance_policy import derive_runtime_mode, evaluate_responsibility


def _base_snapshot(*, synchronized=True, running=False, symbols=None, orders=None,
                   convergence_cancels=None, retired=None, storage_failed=False,
                   clock_recovery_required=None, owner_fault_code=None):
    return {
        'scope': {'runtime_boot_id': 'runtime-boot-1', 'account_key': 'SIM:KQ:0001'},
        'trading_day': '2026-11-24',
        'sequence': 5,
        'synchronized': synchronized,
        'running': running,
        'storage_failed': storage_failed,
        'clock_recovery_required': clock_recovery_required,
        'owner_fault_code': owner_fault_code,
        'symbols': symbols or {},
        'orders': orders or {},
        'convergence_cancels': convergence_cancels or {},
        'retired_positions': retired or {},
    }


class MaintenancePolicyTests(unittest.TestCase):
    def test_preview_only_startup_inference_position_allows_maintenance_in_monitoring(self):
        snap = _base_snapshot(symbols={
            'DCE.p2701': {
                'position': {
                    'id': 'pos-preview-1',
                    'owned': 3,
                    'awaiting_start_arm': True,
                    'activated_in_boot': False,
                    'children': [],
                },
            },
        })
        summary = evaluate_responsibility(snap, strategy_intent='STOPPED')
        self.assertEqual(summary['state'], 'NONE')
        self.assertEqual(summary['mode'], 'MONITORING')
        self.assertEqual(summary['protected_lots_total'], 0)
        self.assertEqual(summary['preview_only_contracts'], ['DCE.p2701'])
        self.assertTrue(summary['maintenance_allowed'])
        self.assertEqual(summary['blockers'], [])

    def test_activated_protection_position_blocks_maintenance_and_derives_draining(self):
        snap = _base_snapshot(symbols={
            'SHFE.ag2702': {
                'position': {
                    'id': 'pos-active-1',
                    'owned': 2,
                    'awaiting_start_arm': False,
                    'activated_in_boot': True,
                    'children': ['child-1'],
                },
            },
        })
        summary = evaluate_responsibility(snap, strategy_intent='STOPPED')
        self.assertEqual(summary['state'], 'DRAINING')
        self.assertEqual(summary['mode'], 'DRAINING')
        self.assertEqual(summary['protected_lots'], {'SHFE.ag2702': 2})
        self.assertFalse(summary['maintenance_allowed'])
        self.assertIn('ACTIVE_PROTECTED_POSITIONS', summary['blockers'])

    def test_unresolved_orders_and_convergence_cancels_block_maintenance(self):
        snap = _base_snapshot(
            orders={
                'child-open-1': {
                    'send_state': 'NATIVE_SEEN',
                    'broker_state': 'ALIVE',
                    'cancel_status': 'NONE',
                    'terminal_confirmed': False,
                },
            },
            convergence_cancels={'native-ext-1': {'cancel_status': 'UNKNOWN'}},
        )
        summary = evaluate_responsibility(snap, strategy_intent='STOPPED')
        self.assertEqual(summary['state'], 'DRAINING')
        self.assertEqual(summary['mode'], 'DRAINING')
        self.assertFalse(summary['maintenance_allowed'])
        self.assertIn('UNRESOLVED_CHILDREN', summary['blockers'])
        self.assertIn('UNRESOLVED_CONVERGENCE_CANCELS', summary['blockers'])

    def test_uncertain_child_and_detached_conflict_mark_unknown(self):
        snap = _base_snapshot(
            symbols={
                'DCE.p2701': {'detached_positions': ['retired-1'], 'native_conflicts': ['conflict-1']},
            },
            retired={'retired-1': {'owned': 4}},
            orders={
                'child-unknown-1': {
                    'send_state': 'UNKNOWN',
                    'broker_state': 'ALIVE',
                    'cancel_status': 'NONE',
                    'terminal_confirmed': False,
                },
            },
        )
        summary = evaluate_responsibility(snap, strategy_intent='STOPPED')
        self.assertEqual(summary['state'], 'UNKNOWN')
        self.assertEqual(summary['mode'], 'DRAINING')
        self.assertEqual(summary['protected_lots'], {'DCE.p2701': 4})
        self.assertEqual(summary['conflict_contracts'], ['DCE.p2701'])
        self.assertFalse(summary['maintenance_allowed'])
        self.assertIn('ALLOCATION_OR_OWNERSHIP_CONFLICT', summary['blockers'])

    def test_running_intent_unsynced_or_fault_blocks_maintenance(self):
        self.assertEqual(derive_runtime_mode('RUNNING', 'NONE'), 'TRADING')
        self.assertEqual(derive_runtime_mode('STOPPED', 'NONE', has_start_pending=True), 'TRADING')
        self.assertEqual(derive_runtime_mode('STOPPED', 'UNKNOWN'), 'DRAINING')
        self.assertEqual(derive_runtime_mode('STOPPED', 'NONE'), 'MONITORING')

        fault_snap = _base_snapshot(
            synchronized=False,
            storage_failed=True,
            clock_recovery_required='CLOCK_SKEW',
            owner_fault_code='E_DSV_NATIVE_FACT',
        )
        summary = evaluate_responsibility(
            fault_snap,
            strategy_intent='RUNNING',
            has_start_pending=True,
            maintenance={'command_id': 'm1', 'reason': 'UPDATE', 'state': 'VERIFYING'},
            pending_commands=True,
        )
        self.assertEqual(summary['state'], 'UNKNOWN')
        self.assertEqual(summary['mode'], 'TRADING')
        self.assertFalse(summary['maintenance_allowed'])
        for blocker in (
            'STRATEGY_RUNNING',
            'BROKER_FACTS_INCOMPLETE',
            'STORAGE_FAILED',
            'CLOCK_FAULT',
            'OWNER_FAULT',
            'PENDING_COMMANDS',
            'MAINTENANCE_IN_FLIGHT',
        ):
            self.assertIn(blocker, summary['blockers'])


if __name__ == '__main__':
    unittest.main()
