"""G2 recovery contracts: injected native facts, no real account operations."""
from pathlib import Path
from types import SimpleNamespace as NS
import copy
import tempfile
import unittest
from unittest.mock import patch

from online_trader.domain.orders import OrderState
from online_trader.errors import TraderError
from online_trader.gateway.tqkq import TqKqPort
from online_trader.kernel import Kernel
from tests.test_online_simulation import FakeClock, FakeOnlinePort, OnlineSimulationFixture, grant_for
from tests.test_online_simulation_boundary import NativeApi


class RecoveryTests(OnlineSimulationFixture, unittest.TestCase):
    def order(self, cid): return self.kernel._staged[cid]['orders'][0]

    def test_zero_fill_cancel_keeps_reservation_until_matching_terminal(self):
        child, plan = self.open(); self.kernel.wait_idle(); cid = plan['position_cycle_id']
        self.kernel.request_cancel(child); self.kernel.wait_idle()
        self.assertEqual(OrderState.from_dict(self.order(cid)).potential_remaining, 1)
        self.assertNotEqual(self.kernel._staged[cid]['lifecycle'], 'CLOSED')
        self.port.terminal(child); self.sim.tick()
        self.assertEqual(OrderState.from_dict(self.order(cid)).potential_remaining, 0)
        self.assertEqual(self.kernel._committed[cid]['lifecycle'], 'CLOSED')
        self.assertEqual(self.kernel.ledger.query('SELECT * FROM fill_facts'), [])
        self.assertEqual(len(self.port.sent), 1)
        with self.assertRaises(TraderError): self.sim.arm()

    def test_terminal_before_fill_details_cannot_release_or_close(self):
        child, plan = self.open(); self.kernel.wait_idle(); cid = plan['position_cycle_id']
        self.port.observed[child].update(status='FINISHED', reported_fill_qty=1, remaining=0)
        self.port.native[self.port.order_id(child)].update(status='FINISHED', remaining=0)
        self.sim.tick()
        self.assertEqual(OrderState.from_dict(self.order(cid)).potential_remaining, 1)
        self.assertNotEqual(self.kernel._staged[cid]['lifecycle'], 'CLOSED')
        self.port.fill(child); self.sim.tick(); self.sim.tick()
        self.assertEqual(self.kernel._committed[cid]['protection']['owned_qty'], 1)
        self.assertEqual(len(self.kernel.ledger.query('SELECT * FROM fill_facts')), 1)

    def test_fill_after_zero_terminal_invalidates_closed_cache_and_recovers_original_exit(self):
        child, plan = self.open(); self.kernel.wait_idle(); cid = plan['position_cycle_id']
        self.port.terminal(child); self.sim.tick()
        self.assertEqual(self.kernel._committed[cid]['lifecycle'], 'CLOSED')
        self.port.fill(child); self.sim.tick()
        self.assertFalse(self.kernel.recovery_required)
        self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.kernel._committed[cid]['protection']['owned_qty'], 1)
        due = int(self.kernel._committed[cid]['online_deadline']['due_monotonic_ns'])
        self.port.prices['source_time'] = 'fresh-at-original-deadline'
        with patch('online_trader.kernel.simulation.time.monotonic_ns', return_value=due): self.sim.tick()
        self.kernel.wait_idle()
        self.assertEqual([r['effect'] for r in self.port.sent], ['OPEN', 'REDUCE'])

    def test_snapshot_same_quantity_wrong_order_identity_cannot_reconcile(self):
        child, plan = self.filled(); cid = plan['position_cycle_id']; original = self.port.snapshot
        for change in ({'direction': 'SELL'}, {'offset': 'CLOSE'}, {'contract_id': 'DCE.l2701'},
                       {'quantity': 2, 'remaining': 1}, {'limit_price': '1001'}):
            def snapshot():
                value = original(); value['orders'][0].update(change); return value
            with self.subTest(change=change), patch.object(self.port, 'snapshot', snapshot):
                with self.assertRaises(TraderError): self.sim.reconcile(cid)

    def test_snapshot_same_total_wrong_fill_identity_or_economics_cannot_reconcile(self):
        _, plan = self.filled(); cid = plan['position_cycle_id']; original = self.port.snapshot
        for change in ({'trade_id': 'other'}, {'price': '1001'}, {'contract_id': 'DCE.l2701'},
                       {'direction': 'SELL'}, {'offset': 'CLOSE'}, {'source_time_ns': '1'}):
            def snapshot():
                value = original(); value['trades'][0].update(change); return value
            with self.subTest(change=change), patch.object(self.port, 'snapshot', snapshot):
                with self.assertRaises(TraderError): self.sim.reconcile(cid)

    def test_duplicate_snapshot_identities_cannot_be_collapsed_to_one(self):
        _, plan = self.filled(); original = self.port.snapshot
        for table in ('orders', 'trades', 'positions'):
            def snapshot():
                value = original(); value[table].append(copy.deepcopy(value[table][0])); return value
            with self.subTest(table=table), patch.object(self.port, 'snapshot', snapshot):
                with self.assertRaises(TraderError): self.sim.reconcile(plan['position_cycle_id'])

    def test_unknown_send_absent_from_full_snapshot_stays_reserved_across_restart(self):
        original = self.port.send
        self.port.send = lambda request: (_ for _ in ()).throw(OSError('uncertain'))
        child, plan = self.open(); self.kernel.wait_idle(); cid = plan['position_cycle_id']
        self.sim.tick(); self.assertTrue(self.kernel._has_unknown())
        self.kernel.close(); self.port.send = original
        self.kernel = Kernel(self.path, self.grant.account_key, port=self.port,
                             simulation_grant=self.grant, risk_budget='1000')
        self.sim = self.kernel.simulation; self.sim.initialize(); self.sim.tick()
        self.assertEqual(self.order(cid)['send_state'], 'UNKNOWN')
        self.assertEqual(OrderState.from_dict(self.order(cid)).potential_remaining, 1)
        self.assertEqual(self.port.sent, [])
        with self.assertRaises(TraderError): self.sim.arm()

    def test_unknown_send_found_terminal_reconciles_once_without_resending(self):
        original = self.port.send
        def send_then_raise(request): original(request); raise OSError('response lost')
        self.port.send = send_then_raise
        child, plan = self.open(); self.kernel.wait_idle(); self.assertTrue(self.kernel._has_unknown())
        self.port.terminal(child); self.sim.tick(); self.sim.tick()
        self.assertFalse(self.kernel._has_unknown()); self.assertEqual(len(self.port.sent), 1)
        self.assertEqual(self.kernel._committed[plan['position_cycle_id']]['lifecycle'], 'CLOSED')
        self.assertFalse(self.kernel.snapshot()['armed'])

    def test_reconnect_incomplete_or_foreign_position_never_restores_exits(self):
        _, plan = self.filled(); cid = plan['position_cycle_id']
        self.port.fault = True; self.port.synced = False; self.sim.tick()
        self.assertTrue(self.port.fault); self.assertFalse(self.kernel.snapshot()['armed'])
        self.port.synced = True; self.port.long = 2
        with self.assertRaises(TraderError): self.sim.tick()
        self.assertTrue(self.port.fault)
        self.port.long = 1; self.sim.tick()
        self.assertFalse(self.port.fault); self.assertFalse(self.kernel.snapshot()['armed'])
        self.assertEqual(self.kernel._committed[cid]['protection']['owned_qty'], 1)

    def test_changed_native_trade_time_is_conflict_before_replacing_evidence(self):
        child, plan = self.filled(); before = copy.deepcopy(self.sim._observed_orders)
        self.port.observed[child]['fills'][0]['source_time_ns'] = '1'
        with self.assertRaises(TraderError): self.sim.tick()
        self.assertEqual(self.sim._observed_orders, before)
        self.assertEqual(len(self.kernel.ledger.query('SELECT * FROM fill_facts')), 1)
        self.assertTrue(self.kernel.recovery_required); self.assertFalse(self.kernel.snapshot()['armed'])

    def test_restart_cannot_substitute_new_grant_for_existing_owned_cycle(self):
        self.filled(); self.kernel.close()
        other = grant_for(self.path, account_fingerprint=self.grant.value['account_fingerprint'])
        with self.assertRaises(TraderError):
            Kernel(self.path, other.account_key, port=FakeOnlinePort(other), simulation_grant=other, risk_budget='1000')
        self.kernel = Kernel(self.path, self.grant.account_key, port=self.port,
                             simulation_grant=self.grant, risk_budget='1000')
        self.sim = self.kernel.simulation; self.sim.initialize(); self.sim.tick()
        self.assertFalse(self.kernel.recovery_required); self.assertEqual(len(self.port.sent), 1)

    def test_preexisting_foreign_order_mutation_is_not_implicitly_owned(self):
        foreign = {'order_id': 'foreign-history', 'contract_id': 'DCE.p2701', 'status': 'FINISHED',
                   'quantity': 1, 'remaining': 1, 'direction': 'BUY', 'offset': 'OPEN', 'limit_price': '990'}
        self.port.native[foreign['order_id']] = foreign
        _, plan = self.filled(); self.port.native[foreign['order_id']]['limit_price'] = '991'
        with self.assertRaises(TraderError): self.sim.reconcile(plan['position_cycle_id'])

    def test_wrong_snapshot_source_epoch_or_day_never_releases_barrier(self):
        _, plan = self.filled(); original = self.port.snapshot
        for change in ({'source_epoch': self.port.source_epoch+1}, {'trading_day': '2026-11-11'},
                       {'gateway_identity': 'TQKQ:foreign'}, {'account_key': 'SIM:KQ:foreign'}):
            with self.subTest(change=change), patch.object(self.port, 'snapshot', lambda: original()|change):
                with self.assertRaises(TraderError): self.sim.reconcile(plan['position_cycle_id'])


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.grant = grant_for(Path(self.temp.name)/'ledger.db')
        self.api = NativeApi(); self.port = TqKqPort(self.api, self.api._account, self.grant, FakeClock())
        self.request = {'account_key': self.grant.account_key, 'contract_id': 'DCE.p2701',
                        'trading_day': '2026-11-10', 'original_qty': 1, 'effect': 'OPEN',
                        'direction': 'BUY', 'limit_price': '1000', 'child_order_id': 'owned-child'}

    def test_callback_limit_price_must_match_committed_binding(self):
        self.port.send(self.request); oid = self.port.order_id('owned-child')
        self.api.orders[oid] = NS(volume_orign=1, volume_left=1, exchange_id='DCE', instrument_id='p2701',
                                  direction='BUY', offset='OPEN', status='ALIVE', trade_records={}, limit_price=1001.)
        with self.assertRaises(TraderError): self.port.observations()

    def test_callback_trade_identity_and_quantities_must_be_valid_before_terminal(self):
        self.port.send(self.request); oid = self.port.order_id('owned-child')
        order = NS(volume_orign=1, volume_left=0, exchange_id='DCE', instrument_id='p2701',
                   direction='BUY', offset='OPEN', status='FINISHED', limit_price=1000., trade_records={})
        self.api.orders[oid] = order
        for change in ({'volume': 0}, {'volume': True}, {'volume': 2}, {'price': float('nan')},
                       {'trade_date_time': 0}):
            fill = NS(order_id=oid, exchange_id='DCE', instrument_id='p2701', direction='BUY',
                      offset='OPEN', volume=1, price=1000., trade_date_time=1_794_288_601_000_000_000)
            vars(fill).update(change); order.trade_records = {'native-trade': fill}
            with self.subTest(change=change), self.assertRaises(TraderError): self.port.observations()

    def test_new_adapter_restores_native_ids_and_original_deadline_after_held_restart(self):
        path = Path(self.grant.value['ledger_path']); kernel = None
        try:
            kernel = Kernel(path, self.grant.account_key, port=self.port, simulation_grant=self.grant, risk_budget='1000')
            sim = kernel.simulation; sim.initialize(); sim.arm(); kernel.wait_idle()
            plan = sim.plan('DCE.p2701', '1000', stop_points='16', take_profit_points='50')
            cid = plan['position_cycle_id']; context = kernel.issue_entry_context('fixture', 'DCE.p2701')
            child = kernel.admit_entry(plan, 'native-restart', '1000', context_id=context['context_id'], peer_id='fixture')
            kernel.wait_idle(); oid = self.port.order_id(child)
            fill = NS(order_id=oid, exchange_id='DCE', instrument_id='p2701', direction='BUY', offset='OPEN',
                      volume=1, price=1000., trade_date_time=int(self.port.clock.now().timestamp())*1_000_000_000)
            self.api.orders[oid] = NS(volume_orign=1, volume_left=0, exchange_id='DCE', instrument_id='p2701',
                direction='BUY', offset='OPEN', status='FINISHED', limit_price=1000., trade_records={'native-fill': fill})
            self.api.trades['native-fill'] = fill; self.api.pos.pos_long = 1
            sim.tick(); deadline = copy.deepcopy(kernel._committed[cid]['online_deadline']); boot = kernel.boot_id
            kernel.close()
            # A different adapter instance reconstructs bindings solely from the original ledger.
            new_api = NativeApi(); new_api.orders = copy.deepcopy(self.api.orders)
            new_api.trades = copy.deepcopy(self.api.trades); new_api.pos.pos_long = 1
            fresh = TqKqPort(new_api, new_api._account, self.grant, FakeClock())
            kernel = Kernel(path, self.grant.account_key, port=fresh, simulation_grant=self.grant, risk_budget='1000')
            self.assertNotEqual(kernel.boot_id, boot); self.assertTrue(kernel.recovery_required)
            kernel.simulation.initialize(); kernel.simulation.tick()
            self.assertFalse(kernel.recovery_required); self.assertFalse(kernel.snapshot()['armed'])
            self.assertEqual(new_api.inserted, []); self.assertEqual(fresh.order_id(child), oid)
            self.assertEqual(kernel._committed[cid]['online_deadline'], deadline)
            self.assertEqual(len(kernel.ledger.query('SELECT * FROM fill_facts')), 1)
        finally:
            if kernel is not None: kernel.close()


class PartialFillTests(unittest.TestCase):
    def test_partial_entry_cancel_and_late_detail_share_one_exit_target(self):
        from tests import test_kernel as fixtures
        fixture = fixtures.KernelTests(); fixture.setUp()
        try:
            kernel = fixture.kernel; fixture.arm(); plan = fixtures.plan_for(kernel, 5)
            child = fixtures.admit(kernel, plan); kernel.wait_idle(); cid = plan['position_cycle_id']
            kernel.record_fill(child, 'first-partial', 2, '1000'); kernel.wait_idle()
            kernel.manual_close(cid, 2); kernel.wait_idle()
            opening = kernel._staged[cid]['orders'][0]
            self.assertEqual(OrderState.from_dict(opening).potential_remaining, 3)
            with self.assertRaises(TraderError): kernel.confirm_terminal(child, 'CANCELLED', 3)
            kernel.record_fill(child, 'late-detail', 1, '1000'); kernel.wait_idle()
            kernel.confirm_terminal(child, 'CANCELLED', 3); kernel.wait_idle()
            kernel.observe(cid, '1000', bid1='999', ask1='1001'); kernel.wait_idle()
            self.assertEqual(kernel._committed[cid]['protection']['owned_qty'], 3)
            self.assertEqual(kernel._committed[cid]['protection']['target_remaining_qty'], 0)
            exits = [o for o in kernel._staged[cid]['orders'] if o['effect'] == 'REDUCE']
            self.assertEqual(sum(OrderState.from_dict(o).potential_remaining for o in exits), 3)
            self.assertEqual(len(fixture.port.orders), 2)
        finally: fixture.tearDown()
