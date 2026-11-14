"""F6 stop and native terminal evidence using the product simulation owner."""
from datetime import timedelta
from types import SimpleNamespace
import unittest

from online_trader.kernel.workbench import workbench_view
from qa.dsv_native_facts import FactClock
import test_control
import test_online_dsv


class DsvStopCommandTests(unittest.TestCase):
    setUp = test_online_dsv.OnlineDsvTests.setUp
    tearDown = test_online_dsv.OnlineDsvTests.tearDown
    start = test_online_dsv.OnlineDsvTests.start
    quote = test_online_dsv.OnlineDsvTests.quote
    cycle = test_online_dsv.OnlineDsvTests.cycle
    entered = test_online_dsv.OnlineDsvTests.entered
    message = test_control.ControlTests.message
    run_command = test_control.ControlTests.run_command

    def stop(self, params=None):
        self.account = self.kernel.account_key
        return self.run_command('STOP_DSV_STRATEGY', params or {})

    def command_status(self, receipt):
        return self.kernel.control.get_status(receipt['command_id'], receipt['payload_hash']).result()

    def test_flat_stop_ends_session_after_current_broker_proof(self):
        self.start()
        self.stop()
        self.session.step()
        self.assertTrue(self.session.stop.is_set())
        self.assertEqual(self.session.result['status'], 'DSV_STOPPED')
        self.assertTrue(self.session.result['verification']['whole_account_flat_claimed'])
        self.assertEqual(self.api.inserted, [])

    def test_stop_keeps_own_protection_until_exit_fill(self):
        self.entered(trade={'half_close_enabled':False})
        self.stop()
        self.quote(1058)
        self.assertFalse(self.session.stop.is_set())
        self.assertIn('继续管理', workbench_view(self.kernel)['configuration']['strategy_stop_wait'])
        self.quote(1038)
        close = self.api.inserted[-1]
        self.assertEqual(close['offset'], 'CLOSE')
        self.api.fill(close['order_id'], close['volume'], 1038)
        self.session.step()
        self.assertEqual(self.cycle()['lifecycle'], 'CLOSED')
        self.assertTrue(self.session.stop.is_set())

    def test_stop_waits_for_foreign_active_order_without_cancelling_it(self):
        self.start()
        self.foreign_order('foreign')
        self.stop()
        self.session.step()
        self.assertFalse(self.session.stop.is_set())
        self.assertEqual(self.api.cancelled, [])
        self.api.terminal('foreign')
        self.session.step()
        self.assertTrue(self.session.stop.is_set())

    def test_stop_request_survives_restart_of_same_scope(self):
        from online_trader.errors import TraderError
        self.entered(trade={'half_close_enabled':False})
        original_boot = self.kernel.boot_id
        self.stop()
        test_online_dsv.OnlineDsvTests.restart(self)
        self.assertNotEqual(self.kernel.boot_id,original_boot)
        self.assertTrue(self.kernel.control.strategy_stop_requested())
        with self.assertRaises(TraderError):
            self.kernel.simulation.arm()
        self.quote(1038)
        close = self.api.inserted[-1]
        self.assertEqual(close['offset'],'CLOSE')
        self.api.fill(close['order_id'],close['volume'],1038)
        self.session.step()
        self.assertTrue(self.session.stop.is_set())

    def foreign_order(self, oid):
        self.api.orders[oid] = SimpleNamespace(
            exchange_id='DCE', instrument_id='p2701', direction='BUY', offset='OPEN',
            status='ALIVE', volume_orign=2, volume_left=2, limit_price=1040., trade_records={},
        )

    def test_stop_cancels_only_proved_owned_order_and_waits_for_terminal(self):
        self.start()
        self.quote()
        order_id = self.api.inserted[0]['order_id']
        self.foreign_order('outside-automated-ownership')
        receipt = self.stop()
        self.assertEqual(receipt['status'], 'EXECUTING', receipt)
        self.assertEqual(self.api.cancelled, [order_id])
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertTrue(workbench_view(self.kernel)['configuration']['stop_cancel_pending'])
        self.assertEqual(self.command_status(receipt)['status'], 'EXECUTING')
        self.assertEqual(self.kernel.ledger.query("SELECT status FROM reservations WHERE status='HELD'"),
                         [{'status': 'HELD'}])
        self.stop()
        self.assertEqual(self.api.cancelled, [order_id])
        self.api.fill(order_id, 1, 1055, finish=False)
        self.api.terminal(order_id)
        self.kernel.control.poll()
        self.kernel.wait_idle()
        self.assertEqual(self.command_status(receipt)['status'], 'EXECUTING')
        self.session.step()
        final = self.command_status(receipt)
        self.assertEqual(final['status'], 'SUCCEEDED', final)
        self.assertEqual(self.cycle()['protection']['owned_qty'], 1)
        self.assertFalse(workbench_view(self.kernel)['configuration']['stop_cancel_pending'])
        self.assertEqual(self.api.orders['outside-automated-ownership'].status, 'ALIVE')

    def test_cancel_on_stop_false_leaves_the_native_order_alone(self):
        self.start(trade={'cancel_on_stop': False})
        self.quote()
        self.assertFalse(workbench_view(self.kernel)['configuration']['cancel_on_stop'])
        receipt = self.stop()
        self.assertEqual(receipt['status'], 'SUCCEEDED', receipt)
        self.assertEqual(self.api.cancelled, [])
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.quote(1056)
        self.clock.advance(61_000)
        self.quote(1057)
        self.assertEqual(self.api.cancelled, [])

    def test_stop_without_cancel_does_not_prevent_protection_from_cancelling_a_remainder(self):
        self.start(trade={'cancel_on_stop':False})
        self.quote()
        oid = self.api.inserted[0]['order_id']
        self.api.fill(oid,1,1055,finish=False)
        self.session.step()
        self.stop()
        self.assertEqual(self.api.cancelled,[])
        self.quote(1038)
        self.assertEqual(self.api.cancelled,[oid])
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'],0)

    def test_stop_disarms_without_sending_when_channel_needs_reconciliation(self):
        self.start()
        self.quote()
        self.port.fault = True
        receipt = self.stop()
        self.assertEqual(receipt['status'], 'NEEDS_RECONCILIATION', receipt)
        self.assertEqual(self.api.cancelled, [])
        self.assertFalse(self.kernel.snapshot()['armed'])

    def test_stop_after_wall_jump_cancels_owned_native_exit_and_keeps_target(self):
        cycle = self.entered(trade={'cancel_on_stop': True, 'half_close_enabled': False})
        self.quote(1038)
        original_exit = self.api.inserted[-1]
        self.api.fill(original_exit['order_id'], 2, 1037, finish=False)
        self.session.step()
        source = FactClock(self.clock.now().isoformat())
        self.api.clock = source
        self.clock.value += timedelta(days=1)
        self.session.step()
        receipt = self.stop()
        self.assertEqual(receipt['status'], 'EXECUTING', receipt)
        self.assertEqual(self.api.cancelled, [original_exit['order_id']])
        self.api.terminal(original_exit['order_id'])
        self.clock.advance(1000)
        source.advance(1000)
        self.api.tick(self.code, last=1038)
        self.session.step()
        self.assertEqual(self.command_status(receipt)['status'], 'SUCCEEDED')
        self.assertEqual(self.cycle()['position_cycle_id'], cycle['position_cycle_id'])
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'], 0)
        self.assertEqual([(o['offset'], o['volume']) for o in self.api.inserted],
                         [('OPEN', 5), ('CLOSE', 5), ('CLOSE', 3)])
        final = self.api.inserted[-1]
        self.api.fill(final['order_id'], 3, 1037)
        self.session.step()
        self.assertTrue(self.session.stop.is_set())
        self.assertEqual(self.session.result['status'], 'DSV_STOPPED')

    def test_stop_reports_existing_position_and_keeps_the_exit_target(self):
        cycle = self.entered()
        self.kernel.manual_close(cycle['position_cycle_id'], 2)
        self.kernel.wait_idle()
        self.quote(1055)
        exit_id = self.api.inserted[-1]['order_id']
        self.assertEqual(self.api.inserted[-1]['offset'], 'CLOSE')
        receipt = self.stop()
        self.assertEqual(receipt['status'], 'EXECUTING', receipt)
        self.assertIn('仍有自有持仓 5 手', receipt['reason'])
        self.assertEqual(self.api.cancelled, [exit_id])
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'], 3)
        self.api.terminal(exit_id)
        self.session.step()
        self.assertEqual(self.command_status(receipt)['status'], 'SUCCEEDED')
        self.assertEqual(self.cycle()['protection']['target_remaining_qty'], 3)

    def test_manual_scope_uses_exact_reviewed_external_order_set(self):
        self.start(grant_changes={'schema_version': 'KQ_WORKBENCH_SIM/1', 'trading_days': ['2026-11-10'],
                                  'resume_entries_on_day_change': False})
        self.foreign_order('reviewed')
        config = workbench_view(self.kernel)['configuration']
        params = {'evidence_hash': config['native_cancel_review']['evidence_hash']}
        self.foreign_order('arrived-late')
        receipt = self.stop(params)
        self.assertEqual(receipt['status'], 'SUCCEEDED', receipt)
        self.assertEqual(self.api.cancelled, [])
        del self.api.orders['arrived-late']
        receipt = self.stop(params)
        self.assertEqual(receipt['status'], 'EXECUTING', receipt)
        self.assertEqual(self.api.cancelled, ['reviewed'])
        self.api.terminal('reviewed')
        self.session.step()
        self.assertEqual(self.command_status(receipt)['status'], 'SUCCEEDED')
