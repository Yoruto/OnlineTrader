"""Targeted diagnostic tests for unfilled-order stall and blocking scenarios."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.controller import CancelNotAttempted, ExplicitOrderController, terminal
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.errors import TraderError
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.instance import AccountLock
from qa.target_owner.fake_broker import FakeBroker


DAY = '2026-11-13'
CONTRACTS = ('DCE.p2701', 'DCE.m2701', 'DCE.y2701', 'DCE.a2701')


class _FakeBrokerPort:
    """Adapter port forwarding SEND/CANCEL calls into FakeBroker."""

    def __init__(self, broker: FakeBroker):
        self.broker = broker
        self.orders = []
        self.cancellations = []

    def send(self, request):
        self.orders.append(dict(request))
        self.broker.observe_adapter_event({
            'event_type': 'OUTBOUND_SEND',
            'request': dict(request),
            'adapter_result': 'RETURNED',
        })
        return {'stage': 'fake-broker-send-returned'}

    def cancel(self, request):
        self.cancellations.append(dict(request))
        self.broker.observe_adapter_event({
            'event_type': 'OUTBOUND_CANCEL',
            'request': dict(request),
            'adapter_result': 'RETURNED',
        })
        return {'stage': 'fake-broker-cancel-returned'}


def _multi_contract_profile(contracts=CONTRACTS, *, entry_confirm_timeout=15):
    settings = load_confirmed_baseline()[0].to_settings()
    base_sym = settings['symbols'][0]
    settings['symbols'] = [
        base_sym | {
            'code': code,
            'open_offset': 40,
            'stop_offset': 8,
            'profit_offset': 20,
            'order_volume': 5,
        }
        for code in contracts
    ]
    settings['trade'].update(
        continuation_entry=False,
        no_chase_after_act=False,
        passive_entry_first=False,
        protect_80_enabled=False,
        trailing_activate_ratio=0,
        entry_confirm_timeout=entry_confirm_timeout,
    )
    return DsvProfile.from_settings(settings)


def _set_contract_prices(frame: dict, prices: dict[str, str]):
    for row in frame['market']:
        code = row['contract_id']
        if code in prices and row['ticks']:
            tick = dict(row['ticks'][0])
            p = str(prices[code])
            tick['last'] = p
            tick['bid1'] = p
            tick['ask1'] = p
            row['ticks'] = [tick]


class UnfilledOrderStallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _make_session(self, name: str, contracts=CONTRACTS, *, entry_confirm_timeout=15):
        account_key = f'DEV:{name}'
        broker = FakeBroker(
            account_key=account_key,
            contract=contracts[0],
            contracts=contracts,
            trading_day=DAY,
            trace_path=self.root / f'{name}-broker.jsonl',
        )
        port = _FakeBrokerPort(broker)
        prof = _multi_contract_profile(contracts, entry_confirm_timeout=entry_confirm_timeout)
        session = DsvSession(
            self.root / f'{name}-journal.sqlite',
            account_key=account_key,
            provider='OFFLINE',
            trading_day=DAY,
            profile=prof,
            port=port,
        )
        self.addCleanup(session.close)
        return broker, port, session

    def test_1_unfilled_60s_on_one_contract_does_not_block_other_contracts(self):
        broker, port, session = self._make_session('unfilled-60s', entry_confirm_timeout=15)
        session.command({'id': 'cmd-start', 'action': 'START'})

        # Round 1: establish baseline at 1055 (between short=1048 and long=1060).
        f1 = broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1')
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        # Round 2: DCE.p2701 breaks out to 1061 -> sends 5-lot LONG open; others stay at 1055.
        f2 = broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1055', candle_id='bar-1')
        _set_contract_prices(f2, {'DCE.p2701': '1061'})
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])

        self.assertEqual(len(port.orders), 1)
        p_child = port.orders[0]['child_order_id']
        broker.accept_order(p_child)  # ALIVE, 0 filled

        # Round 3..15: 60+ seconds pass; p2701 remains unfilled, while m2701/y2701/a2701 break out, fill, and hit stop-loss.
        f3 = broker.prepare_frame(wall_time=f'{DAY}T01:10:10Z', monotonic_ms=11000, last='1061', candle_id='bar-1')
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])

        other_children = {}
        for req in port.orders[1:]:
            cid = req['contract_id']
            other_children[cid] = req['child_order_id']
            broker.accept_order(req['child_order_id'])
            broker.fill(req['child_order_id'], 5, '1061')

        # Round 4: confirm fills on m2701/y2701/a2701 while p2701 stays ALIVE 0-filled.
        f4 = broker.prepare_frame(wall_time=f'{DAY}T01:10:15Z', monotonic_ms=16000, last='1061', candle_id='bar-1')
        session.apply_round(f4)
        broker.acknowledge_frame(f4['id'])

        snap4 = session.snapshot()
        for cid in ('DCE.m2701', 'DCE.y2701', 'DCE.a2701'):
            self.assertEqual(snap4['symbols'][cid]['position']['owned'], 5, f'{cid} should own 5 lots')

        # Advance past entry_confirm_timeout (15s) up to 70s; drop m2701/y2701/a2701 to 1051 (stop_offset=8 -> <=1052)
        f5 = broker.prepare_frame(wall_time=f'{DAY}T01:11:15Z', monotonic_ms=76000, last='1051', candle_id='bar-1')
        _set_contract_prices(f5, {'DCE.p2701': '1061'})
        session.apply_round(f5)
        broker.acknowledge_frame(f5['id'])

        # Other 3 contracts MUST have sent REDUCE close orders despite p2701 still being unfilled!
        reduce_contracts = {req['contract_id'] for req in port.orders if req['native_offset'] != 'OPEN'}
        self.assertEqual(reduce_contracts, {'DCE.m2701', 'DCE.y2701', 'DCE.a2701'})

        # Meanwhile, p2701 triggered ENTRY_CONFIRM_TIMEOUT and issued a CANCEL.
        cancel_children = {req['child_order_id'] for req in port.cancellations}
        self.assertIn(p_child, cancel_children)

    def test_2_cancel_without_terminal_fact_keeps_contract_in_cancel_pending_forever(self):
        broker, port, session = self._make_session('cancel-no-terminal', entry_confirm_timeout=10)
        session.command({'id': 'cmd-start', 'action': 'START'})

        f1 = broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1')
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        f2 = broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1061', candle_id='bar-1')
        _set_contract_prices(f2, {'DCE.m2701': '1055', 'DCE.y2701': '1055', 'DCE.a2701': '1055'})
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])

        p_child = port.orders[0]['child_order_id']
        broker.accept_order(p_child)

        # Trigger ENTRY_CONFIRM_TIMEOUT -> sends 1st cancel (CALL_RETURNED), but broker NEVER confirms cancel.
        f3 = broker.prepare_frame(wall_time=f'{DAY}T01:10:20Z', monotonic_ms=21000, last='1061', candle_id='bar-1')
        _set_contract_prices(f3, {'DCE.m2701': '1055', 'DCE.y2701': '1055', 'DCE.a2701': '1055'})
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])
        self.assertEqual(len(port.cancellations), 1)

        # Advance 10 more rounds (60 seconds in both wall_time and monotonic_ms) with p_child still ALIVE.
        base_dt = datetime.fromisoformat(f'{DAY}T01:10:20+00:00')
        for i in range(1, 11):
            fk = broker.prepare_frame(
                wall_time=(base_dt + timedelta(seconds=i * 6)).isoformat().replace('+00:00', 'Z'),
                monotonic_ms=21000 + i * 6000,
                last='1061',
                candle_id='bar-1',
            )
            _set_contract_prices(fk, {'DCE.m2701': '1055', 'DCE.y2701': '1055', 'DCE.a2701': '1055'})
            session.apply_round(fk)
            broker.acknowledge_frame(fk['id'])

        snap = session.snapshot()
        p_order = snap['orders'][p_child]
        p_proj = snap['symbols']['DCE.p2701']['target_execution']

        # Fixed behavior: stale CALL_RETURNED cancel retries up to _MAX_CANCEL_ATTEMPTS (4 total),
        # while still preserving pending capacity until broker confirms terminal.
        self.assertEqual(p_order['cancel_status'], 'CALL_RETURNED')
        self.assertEqual(len(port.cancellations), 4)
        self.assertEqual(p_order['cancel_attempt'], 4)
        self.assertEqual(p_proj['status'], 'CANCELLING')
        self.assertEqual(p_proj['reason'], 'CANCEL_PENDING')

    def test_3_pending_child_missing_from_sync_blocks_entire_account_resync(self):
        broker, port, session = self._make_session('resync-block', contracts=('DCE.p2701', 'DCE.m2701'))
        session.command({'id': 'cmd-start', 'action': 'START'})

        f1 = broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1')
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        # Both contracts open; m2701 fills 5 lots, p2701 stays ALIVE 0-filled.
        f2 = broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1061', candle_id='bar-1')
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])

        by_contract = {req['contract_id']: req['child_order_id'] for req in port.orders}
        broker.accept_order(by_contract['DCE.p2701'])
        broker.accept_order(by_contract['DCE.m2701'])
        broker.fill(by_contract['DCE.m2701'], 5, '1061')

        f3 = broker.prepare_frame(wall_time=f'{DAY}T01:10:06Z', monotonic_ms=7000, last='1061', candle_id='bar-1')
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])
        self.assertEqual(session.snapshot()['symbols']['DCE.m2701']['position']['owned'], 5)

        # Simulate disconnect -> mark unsynchronized.
        session.disconnect(reason='SIMULATED_DISCONNECT')
        self.assertFalse(session.snapshot()['synchronized'])

        # Next frame claims synchronized=True, but DCE.p2701's pending child is missing from sync_orders/native!
        # Meanwhile DCE.m2701 drops to 1051 (hits stop-loss!).
        f4 = broker.prepare_frame(wall_time=f'{DAY}T01:10:08Z', monotonic_ms=9000, last='1051', candle_id='bar-1')
        p_child = by_contract['DCE.p2701']
        f4['sync_orders'] = [c for c in f4['sync_orders'] if c != p_child]
        f4['native'] = [fact for fact in f4['native'] if fact.get('child_order_id') != p_child]
        orders_before = len(port.orders)
        session.apply_round(f4)
        broker.acknowledge_frame(f4['id'])

        snap4 = session.snapshot()
        # Fixed behavior: only DCE.p2701 (missing child) is isolated; account sync succeeds
        # and DCE.m2701 immediately sends its 5-lot stop-loss REDUCE order!
        self.assertTrue(snap4['synchronized'])
        self.assertEqual((snap4.get('sync_pending_by_contract') or {}).get('DCE.p2701'), [p_child])
        self.assertIsNone((snap4.get('sync_pending_by_contract') or {}).get('DCE.m2701'))
        self.assertEqual(len(port.orders), orders_before + 1)
        self.assertEqual(port.orders[-1]['contract_id'], 'DCE.m2701')
        self.assertEqual(port.orders[-1]['native_offset'], 'CLOSE')

    def test_4_partial_fill_protection_and_entry_confirm_timeout_exit(self):
        broker, port, session = self._make_session('partial-fill', contracts=('DCE.p2701',), entry_confirm_timeout=10)
        session.command({'id': 'cmd-start', 'action': 'START'})

        f1 = broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1')
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        f2 = broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1061', candle_id='bar-1')
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])

        p_child = port.orders[0]['child_order_id']
        broker.accept_order(p_child)
        broker.fill(p_child, 1, '1061')  # Partial fill: 1 of 5 filled, 4 remain ALIVE

        f3 = broker.prepare_frame(wall_time=f'{DAY}T01:10:06Z', monotonic_ms=7000, last='1061', candle_id='bar-1')
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])

        snap3 = session.snapshot()
        pos3 = snap3['symbols']['DCE.p2701']['position']
        self.assertEqual(pos3['entry_filled'], 1)
        self.assertEqual(pos3['owned'], 1)
        self.assertFalse(pos3['entry_stopped'])

        # Advance 30s (> entry_confirm_timeout=10s) without hitting stop-loss:
        # Because entry_filled > 0, entry_confirm_timeout does NOT fire; 4-lot open remains ALIVE.
        f4 = broker.prepare_frame(wall_time=f'{DAY}T01:10:36Z', monotonic_ms=37000, last='1061', candle_id='bar-1')
        session.apply_round(f4)
        broker.acknowledge_frame(f4['id'])
        snap4 = session.snapshot()
        self.assertFalse(snap4['symbols']['DCE.p2701']['position']['entry_stopped'])
        self.assertEqual(len(port.cancellations), 0)

        # Now price drops to 1051 -> stop-loss triggers on the 1 owned lot!
        f5 = broker.prepare_frame(wall_time=f'{DAY}T01:10:37Z', monotonic_ms=38000, last='1051', candle_id='bar-1')
        session.apply_round(f5)
        broker.acknowledge_frame(f5['id'])

        # Should stop entry (cancel remaining 4 lots) AND send a 1-lot REDUCE order.
        self.assertEqual([c['child_order_id'] for c in port.cancellations], [p_child])
        reduce_orders = [o for o in port.orders if o['native_offset'] != 'OPEN']
        self.assertEqual(len(reduce_orders), 1)
        self.assertEqual(reduce_orders[0]['quantity'], 1)

    def test_5_active_unfilled_order_disables_idle_skip(self):
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter.account = 'fake-acct'
        adapter.contracts = ['DCE.p2701']
        adapter.synchronized = True
        adapter.owner_mode = 'STANDALONE'
        adapter._clock_resume_day = None
        adapter._health = {}
        adapter._last_committed_ref_status = None
        adapter._last_committed_round_mono_ns = 1_000_000_000
        adapter._last_idle_signature = None
        adapter.quotes = {}
        adapter._safe_all_positions = lambda: {}
        adapter._live_trading_status = lambda _c: 'CONTINUOUS'

        fake_orders = {}
        adapter.api = SimpleNamespace(
            get_order=lambda **_kw: fake_orders,
            get_trade=lambda **_kw: {},
        )
        state = {
            'running': True,
            'synchronized': True,
            'sequence': 5,
            'trading_day': DAY,
            'startup_inferred': True,
            'convergence_cancels': {},
            'symbols': {'DCE.p2701': {'position': None}},
            'orders': {},
        }
        sample = SimpleNamespace(monotonic=1_100_000_000)
        adapter._last_idle_signature = adapter._idle_state_signature(state, DAY)

        self.assertTrue(adapter._can_skip_idle_round(
            state, sample, DAY, updated=False, timers=(), day_held=False))

        # Add one non-terminal unfilled order -> idle skip is immediately disabled.
        state['orders']['c-1'] = {
            'child_order_id': 'c-1',
            'terminal_confirmed': False,
            'send_state': 'CALL_RETURNED',
            'broker_state': 'ALIVE',
            'cancel_status': None,
        }
        self.assertFalse(adapter._can_skip_idle_round(
            state, sample, DAY, updated=False, timers=(), day_held=False))

    def test_6_sdk_port_call_hang_blocks_dispatch_synchronously(self):
        scope = {'account_key': 'DEV:hang-1', 'provider': 'OFFLINE', 'trading_day': DAY, 'configuration_id': 'cfg-1'}
        from online_trader.dsv_runtime.runner import initial_state
        from online_trader.dsv_storage.journal import Journal

        journal = Journal(self.root / 'hang-journal.sqlite', scope, initial_state(DAY, ['DCE.p2701']))
        self.addCleanup(journal.close)

        class _HangingPort:
            def send(self, _req):
                return {'ok': True}

            def cancel(self, _req):
                time.sleep(0.25)
                return {'ok': True}

        controller = ExplicitOrderController(scope, journal, _HangingPort())
        state = journal.load()
        order = {
            'child_order_id': 'c-hang-1', 'intent_id': 'i1', 'attempt_id': 'c-hang-1',
            'position_id': 'p1', 'contract_id': 'DCE.p2701', 'direction': 'BUY',
            'native_offset': 'OPEN', 'effect': 'OPEN', 'original_qty': 5, 'filled_qty': 0,
            'limit_price': '1060', 'send_state': 'CALL_RETURNED', 'broker_state': 'ALIVE',
            'cancel_status': None, 'terminal_confirmed': False, 'reported_fill_qty': 0,
            'native_id': None, 'native_trading_day': DAY, 'local_order_id': 'loc-1',
        }
        state['orders']['c-hang-1'] = order
        with journal.transaction():
            cancel_effect = controller.cancel(order, 'USER_CANCEL')
            journal.save(state)

        t0 = time.monotonic_ns()
        controller.dispatch(state, [cancel_effect])
        elapsed_ms = (time.monotonic_ns() - t0) / 1_000_000
        self.assertGreaterEqual(elapsed_ms, 200.0)

    def test_7_isolated_contract_recovers_automatically_and_retains_stop_loss_protection(self):
        broker, port, session = self._make_session('auto-recover', contracts=('DCE.p2701', 'DCE.m2701'))
        session.command({'id': 'cmd-start', 'action': 'START'})

        f1 = broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1')
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        f2 = broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1061', candle_id='bar-1')
        _set_contract_prices(f2, {'DCE.m2701': '1055'})
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])

        p_child = port.orders[0]['child_order_id']
        broker.accept_order(p_child)
        broker.fill(p_child, 2, '1061')  # 2 of 5 filled, 3 remain ALIVE

        f3 = broker.prepare_frame(wall_time=f'{DAY}T01:10:06Z', monotonic_ms=7000, last='1061', candle_id='bar-1')
        _set_contract_prices(f3, {'DCE.m2701': '1055'})
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])
        self.assertEqual(session.snapshot()['symbols']['DCE.p2701']['position']['owned'], 2)

        # Disconnect and resync while p_child is missing -> DCE.p2701 isolated, account synchronized.
        session.disconnect(reason='SIMULATED_DISCONNECT')
        f4 = broker.prepare_frame(wall_time=f'{DAY}T01:10:08Z', monotonic_ms=9000, last='1051', candle_id='bar-1')
        f4['sync_orders'] = []
        f4['native'] = []
        orders_before = len(port.orders)
        session.apply_round(f4)
        broker.acknowledge_frame(f4['id'])

        snap4 = session.snapshot()
        self.assertTrue(snap4['synchronized'])
        self.assertEqual((snap4.get('sync_pending_by_contract') or {}).get('DCE.p2701'), [p_child])
        # Stop-loss target (exit_target=0) WAS calculated even while isolated, without unsafe dispatch!
        self.assertEqual(snap4['symbols']['DCE.p2701']['position']['exit_target'], 0)
        self.assertEqual(len(port.orders), orders_before)

        # Next frame: missing p_child reappears in sync_orders & native -> automatic recovery!
        f5 = broker.prepare_frame(wall_time=f'{DAY}T01:10:10Z', monotonic_ms=11000, last='1051', candle_id='bar-1')
        f5['native'].append({
            'kind': 'ORDER',
            'child_order_id': p_child,
            'native_order_id': broker.orders[p_child]['native_order_id'],
            'status': 'ALIVE',
            'filled_qty': 2,
        })
        session.apply_round(f5)
        broker.acknowledge_frame(f5['id'])

        snap5 = session.snapshot()
        self.assertFalse(snap5.get('sync_pending_by_contract'))
        self.assertIn(p_child, {c['child_order_id'] for c in port.cancellations})
        reduce_orders = [o for o in port.orders if o['contract_id'] == 'DCE.p2701' and o['native_offset'] != 'OPEN']
        self.assertEqual(len(reduce_orders), 1)
        self.assertEqual(reduce_orders[0]['quantity'], 2)

    def test_8_sdk_adapter_reconcile_runs_when_isolated_and_runner_blocks_until_resolved(self):
        broker, _port, session = self._make_session('adapter-reconcile', contracts=('DCE.p2701', 'DCE.m2701'))
        session.command({'id': 'cmd-start', 'action': 'START'})

        f1 = broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1')
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        f2 = broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1061', candle_id='bar-1')
        _set_contract_prices(f2, {'DCE.m2701': '1055'})
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])
        p_child = _port.orders[0]['child_order_id']
        broker.accept_order(p_child)

        session.disconnect(reason='SIMULATED_DISCONNECT')
        f3 = broker.prepare_frame(wall_time=f'{DAY}T01:10:08Z', monotonic_ms=9000, last='1055', candle_id='bar-1')
        f3['sync_orders'] = []
        f3['native'] = []
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])
        self.assertTrue(session.snapshot()['synchronized'])

        # Runner RECONCILE must refuse to report BROKER_FACTS_RECONCILED while DCE.p2701 is isolated.
        with self.assertRaises(TraderError) as err:
            session.command({'id': 'cmd-rec-1', 'action': 'RECONCILE'})
        self.assertEqual(err.exception.code, 'E_DSV_SYNC')

        # DsvSdkAdapter.command('RECONCILE') must still clear caches and invoke step() when isolated!
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter.account = 'fake-acct'
        adapter.synchronized = True
        adapter.connection = None
        adapter._verified_order_facts = {'stale': 1}
        adapter._verified_trade_facts = {'stale': 1}
        adapter._native_fact_cache = {('ORDER', 'x', None): {}}
        adapter._clock_resume_day = None
        adapter._bind = lambda _s: session.snapshot()
        adapter.prepare_added_contracts = lambda *_a, **_kw: None
        step_calls = []
        adapter.step = lambda _s, **_kw: step_calls.append(_kw)

        with self.assertRaises(TraderError) as adapter_err:
            adapter.command(session, {'id': 'cmd-rec-2', 'action': 'RECONCILE'})
        self.assertEqual(adapter_err.exception.code, 'E_DSV_SYNC')
        self.assertEqual(len(step_calls), 1)
        self.assertEqual(adapter._verified_order_facts, {})
        self.assertEqual(adapter._native_fact_cache, {})

    def test_9_cancel_retry_local_refusal_preserves_prior_unknown_status(self):
        from online_trader.dsv_runtime.runner import initial_state
        from online_trader.dsv_storage.journal import Journal

        scope = {'account_key': 'DEV:cancel-unknown', 'provider': 'OFFLINE', 'trading_day': DAY, 'configuration_id': 'cfg-1'}
        journal = Journal(self.root / 'cancel-unknown-journal.sqlite', scope, initial_state(DAY, ['DCE.p2701']))
        self.addCleanup(journal.close)

        class _FailThenRefusePort:
            def __init__(self):
                self.calls = 0

            def send(self, _req):
                return {'ok': True}

            def cancel(self, req):
                self.calls += 1
                if self.calls == 1:
                    raise OSError('network timeout on first cancel')
                return CancelNotAttempted(req['id'], req['native_order_id'], 'E_DSV_MARKET_PHASE', {'message': 'phase'})

        port = _FailThenRefusePort()
        controller = ExplicitOrderController(scope, journal, port)
        state = journal.load()
        order = {
            'child_order_id': 'c-unk-1', 'intent_id': 'i1', 'attempt_id': 'c-unk-1',
            'position_id': 'p1', 'contract_id': 'DCE.p2701', 'direction': 'BUY',
            'native_offset': 'OPEN', 'effect': 'OPEN', 'original_qty': 5, 'filled_qty': 0,
            'limit_price': '1060', 'send_state': 'CALL_RETURNED', 'broker_state': 'ALIVE',
            'cancel_status': None, 'terminal_confirmed': False, 'reported_fill_qty': 0,
            'native_id': None, 'native_trading_day': DAY, 'local_order_id': 'loc-1',
        }
        state['orders']['c-unk-1'] = order
        with journal.transaction():
            eff1 = controller.cancel(order, 'USER_STOP', wall_time=100.0)
            journal.save(state)
        controller.dispatch(state, [eff1])
        self.assertEqual(order['cancel_status'], 'UNKNOWN')
        self.assertEqual(order['cancel_attempt'], 1)

        # Retry at t=106s hits local CancelNotAttempted -> MUST preserve UNKNOWN and NOT reset to None!
        with journal.transaction():
            effects = controller._retry_stale_cancellations(
                state, 'DCE.p2701', allow_cancel=True, wall_time=106.0)
            journal.save(state)
        self.assertEqual(len(effects), 1)
        controller.dispatch(state, effects)
        self.assertEqual(order['cancel_status'], 'UNKNOWN')
        self.assertEqual(order['cancel_attempt'], 2)

    def test_10_dual_clock_and_unconfigured_contract_sync_isolation(self):
        from online_trader.dsv_runtime.runner import TIME_SEMANTICS
        from online_trader.dsv_runtime.standalone_projection import projection_batch
        from online_trader.scheduling.clock import ClockSample

        broker, port, session = self._make_session('dual-clock-iso', contracts=('DCE.p2701', 'DCE.m2701'))
        session.command({'id': 'cmd-start', 'action': 'START'})

        def _with_paired_clock(frm, wall_iso, mono_ms):
            sample = ClockSample(
                now_utc=wall_iso,
                monotonic_ns=str(mono_ms * 1_000_000),
                clock_epoch='a' * 32,
            )
            frm['time_semantics_version'] = TIME_SEMANTICS
            frm['time_sample'] = sample.paired_record()
            frm['wall_time'] = sample.now_utc
            frm['decision_utc'] = sample.now_utc
            frm['authorization_time'] = sample.now_utc
            frm['clock_health'] = {'status': 'VALID', 'reason': None, 'continuous_samples': 3, 'utc_error_ns': '0'}
            return frm

        f1 = _with_paired_clock(
            broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1'),
            f'{DAY}T01:10:00Z', 1000,
        )
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        f2 = _with_paired_clock(
            broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1061', candle_id='bar-1'),
            f'{DAY}T01:10:05Z', 6000,
        )
        _set_contract_prices(f2, {'DCE.m2701': '1055'})
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])
        p_child = port.orders[0]['child_order_id']
        broker.accept_order(p_child)
        broker.fill(p_child, 2, '1061')

        f3 = _with_paired_clock(
            broker.prepare_frame(wall_time=f'{DAY}T01:10:06Z', monotonic_ms=7000, last='1061', candle_id='bar-1'),
            f'{DAY}T01:10:06Z', 7000,
        )
        _set_contract_prices(f3, {'DCE.m2701': '1055'})
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])

        # Disconnect + resync on dual-clock path with p_child missing and price dropping to stop-loss 1051:
        # controller.plan_priced_effects MUST block REDUCE on DCE.p2701 even on the dual-clock path!
        session.disconnect(reason='SIMULATED_DISCONNECT')
        f4 = _with_paired_clock(
            broker.prepare_frame(wall_time=f'{DAY}T01:10:08Z', monotonic_ms=9000, last='1051', candle_id='bar-1'),
            f'{DAY}T01:10:08Z', 9000,
        )
        f4['sync_orders'] = []
        f4['native'] = []
        orders_before = len(port.orders)
        session.apply_round(f4)
        broker.acknowledge_frame(f4['id'])

        snap4 = session.snapshot()
        self.assertTrue(snap4['synchronized'])
        self.assertEqual((snap4.get('sync_pending_by_contract') or {}).get('DCE.p2701'), [p_child])
        self.assertEqual(snap4['symbols']['DCE.p2701']['position']['exit_target'], 0)
        self.assertEqual(len(port.orders), orders_before)

        # Projection must mark DCE.p2701 CYCLE protection_quality='UNKNOWN' via sync_pending_by_contract.
        batch = projection_batch(snap4)
        p_cycles = [r for r in batch['records'] if r['record_type'] == 'CYCLE' and r['contract_id'] == 'DCE.p2701']
        self.assertEqual(p_cycles[0]['protection_quality'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
