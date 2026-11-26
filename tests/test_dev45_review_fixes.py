"""Regression and multi-contract combination tests for dev45 review fixes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QMessageBox

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.desktop.ui_hang_monitor import UiHangMonitor
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.dsv_runtime.controller import CancelNotAttempted, ExplicitOrderController
from online_trader.dsv_runtime.runner import DsvSession, initial_state
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter, NS_PER_SECOND
from online_trader.dsv_storage.journal import Journal
from qa.recording_port import RecordingPort
from qa.target_owner.fake_broker import FakeBroker
from tests.qt_lifecycle import dispose_widget


DAY = '2026-11-13'
CONTRACT = 'DCE.p2701'
UNCONF_CONTRACT = 'SHFE.ag2702'


class _FakeBrokerPort:
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


def _multi_profile(contracts=(CONTRACT, 'DCE.m2701')):
    settings = load_confirmed_baseline()[0].to_settings()
    base = settings['symbols'][0]
    settings['symbols'] = [
        base | {
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
    )
    return DsvProfile.from_settings(settings)


def _set_prices(frame: dict, prices: dict[str, str], *, extra: dict[str, dict] | None = None):
    for row in frame['market']:
        code = row['contract_id']
        if row['ticks'] and (code in prices or (extra and code in extra)):
            tick = dict(row['ticks'][0])
            if code in prices:
                p = str(prices[code])
                tick['last'] = p
                tick['bid1'] = p
                tick['ask1'] = p
            if extra and code in extra:
                tick.update(extra[code])
            row['ticks'] = [tick]


class BrokerPhaseStaleStatusBackoffTests(unittest.TestCase):
    def test_stale_continuous_status_preserves_progressive_backoff_until_real_transition(self):
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter.owner_mode = 'STANDALONE'
        adapter._trading_status_supported = True
        adapter._trading_status_objs = {CONTRACT: SimpleNamespace(trade_status='CONTINOUS')}
        adapter.quotes = {CONTRACT: SimpleNamespace(datetime='2026-11-12 21:05:00.000000')}
        adapter.api = SimpleNamespace(_backtest=False, get_trading_status=lambda _c: adapter._trading_status_objs[CONTRACT])

        adapter._record_broker_phase_rejection(CONTRACT, '交易所暂停交易')
        first = adapter._broker_phase_overrides[CONTRACT]
        self.assertEqual(first['recorded_live_status'], 'CONTINUOUS')
        self.assertTrue(first['awaiting_status_transition'])
        self.assertEqual(first['backoff_seconds'], 5)
        t0 = first['recorded_mono_ns']
        cal_cont = {'market_phase': 'CONTINUOUS', 'complete': True, 'is_trading': True}

        # Stale cached CONTINUOUS must NOT clear backoff before the 5s timer expires.
        active_1s = adapter._active_broker_phase_override(
            CONTRACT, cal_cont, 'CONTINUOUS', True, SimpleNamespace(monotonic=t0 + 1 * NS_PER_SECOND),
        )
        self.assertIsNotNone(active_1s)
        self.assertEqual(active_1s['phase'], 'HALTED')
        self.assertEqual(adapter._broker_phase_reject_counts[CONTRACT][0], 1)

        # At 5s, timed backoff expires for one probe while preserving consecutive reject history.
        probe_5s = adapter._active_broker_phase_override(
            CONTRACT, cal_cont, 'CONTINUOUS', True, SimpleNamespace(monotonic=t0 + 5 * NS_PER_SECOND),
        )
        self.assertIsNone(probe_5s)
        self.assertIn(CONTRACT, adapter._broker_phase_reject_counts)

        # Second rejection escalates to 15s and continues resisting stale CONTINUOUS.
        adapter._record_broker_phase_rejection(CONTRACT, '交易所暂停交易')
        second = adapter._broker_phase_overrides[CONTRACT]
        self.assertEqual(second['backoff_seconds'], 15)
        t1 = second['recorded_mono_ns']
        self.assertIsNotNone(adapter._active_broker_phase_override(
            CONTRACT, cal_cont, 'CONTINUOUS', True, SimpleNamespace(monotonic=t1 + 10 * NS_PER_SECOND),
        ))

        # Once live_status reflects non-CONTINUOUS, awaiting_status_transition clears;
        # a subsequent transition back to CONTINUOUS clears override and reject history immediately.
        still_halted = adapter._active_broker_phase_override(
            CONTRACT, cal_cont, 'NOTRADING', True, SimpleNamespace(monotonic=t1 + 11 * NS_PER_SECOND),
        )
        self.assertIsNotNone(still_halted)
        self.assertFalse(still_halted['awaiting_status_transition'])

        recovered = adapter._active_broker_phase_override(
            CONTRACT, cal_cont, 'CONTINUOUS', True, SimpleNamespace(monotonic=t1 + 12 * NS_PER_SECOND),
        )
        self.assertIsNone(recovered)
        self.assertNotIn(CONTRACT, adapter._broker_phase_reject_counts)


class ConvergenceCancelRecoveryAndPhaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _new_controller(self, port=None):
        scope = {'account_key': 'DEV:conv-fix', 'provider': 'OFFLINE', 'trading_day': DAY, 'configuration_id': 'cfg-1'}
        journal = Journal(self.root / f'journal-{id(self)}-{len(list(self.root.iterdir()))}.sqlite', scope, initial_state(DAY, [CONTRACT]))
        self.addCleanup(journal.close)
        port = port or RecordingPort()
        return ExplicitOrderController(scope, journal, port), journal, port

    @staticmethod
    def _unconf_market(code=UNCONF_CONTRACT, *, long_qty=0):
        return {
            'contract_id': code,
            'position_known': True,
            'long_qty': long_qty,
            'short_qty': 0,
            'long_today': long_qty,
            'long_historical': 0,
            'short_today': 0,
            'short_historical': 0,
            'ticks': [{
                'native_time': f'{DAY}T01:10:00Z',
                'last': '5000',
                'bid1': '4999',
                'ask1': '5001',
                'tick_size': '1',
                'lower_limit': '4500',
                'upper_limit': '5500',
            }],
            'market_permission': {
                'contract_id': code,
                'market_phase': 'CONTINUOUS',
                'evidence_state': 'EXPLICIT',
                'status_source': 'EXPLICIT_FRAME',
                'continuous_matching': True,
                'can_open': True,
                'can_close': True,
                'can_cancel': True,
            },
        }

    def test_convergence_order_stale_cancel_recovers_in_armed_and_disarmed(self):
        controller, journal, port = self._new_controller()
        state = journal.load()
        order = {
            'child_order_id': 'conv-c-1',
            'intent_id': f'conv-{UNCONF_CONTRACT}-long',
            'attempt_id': 'conv-c-1',
            'position_id': f'conv-{UNCONF_CONTRACT}-long',
            'contract_id': UNCONF_CONTRACT,
            'direction': 'SELL',
            'native_offset': 'CLOSE',
            'effect': 'REDUCE',
            'original_qty': 2,
            'filled_qty': 0,
            'limit_price': '5000',
            'send_state': 'NATIVE_SEEN',
            'broker_state': 'ALIVE',
            'cancel_status': None,
            'terminal_confirmed': False,
            'reported_fill_qty': 0,
            'native_id': 'nat-conv-1',
            'native_trading_day': DAY,
            'convergence_order': True,
            'convergence_side': 'LONG',
        }
        state['orders']['conv-c-1'] = order
        um = [self._unconf_market(long_qty=0)]

        with journal.transaction():
            effs, _st, _ev = controller.plan_convergence(state, {}, um, [], 1000.0)
            journal.save(state)
        self.assertEqual(len(effs), 1)
        self.assertEqual(effs[0]['kind'], 'CANCEL')
        controller.dispatch(state, effs)
        self.assertEqual(order['cancel_status'], 'CALL_RETURNED')
        self.assertEqual(order['cancel_attempt'], 1)

        # Within 5s -> no retry.
        with journal.transaction():
            effs_early, _st, _ev = controller.plan_convergence(state, {}, um, [], 1002.0)
            journal.save(state)
        self.assertEqual(effs_early, [])

        # Attempts 2, 3, 4 at 5s intervals.
        for idx, wt in enumerate((1005.0, 1010.0, 1015.0), start=2):
            with journal.transaction():
                effs_r, _st, evs_r = controller.plan_convergence(state, {}, um, [], wt)
                journal.save(state)
            self.assertEqual(len(effs_r), 1)
            self.assertEqual(evs_r[0]['action'], 'RETRY_STALE_CONVERGENCE_CANCEL')
            controller.dispatch(state, effs_r)
            self.assertEqual(order['cancel_attempt'], idx)

        # Exhausted rapid attempts wait 120s before recovery probe (attempt 5).
        with journal.transaction():
            effs_wait, _st, _ev = controller.plan_convergence(state, {}, um, [], 1030.0)
            journal.save(state)
        self.assertEqual(effs_wait, [])

        with journal.transaction():
            effs_probe, _st, _ev = controller.plan_convergence(state, {}, um, [], 1136.0)
            journal.save(state)
        self.assertEqual(len(effs_probe), 1)
        controller.dispatch(state, effs_probe)
        self.assertEqual(order['cancel_attempt'], 5)

        # DISARMED path (reconcile_stopped_convergence) also probes after 120s without SEND effects.
        with journal.transaction():
            effs_stopped = controller.reconcile_stopped_convergence(state, {}, um, [], wall_time=1257.0)
            journal.save(state)
        self.assertEqual(len(effs_stopped), 1)
        self.assertEqual(effs_stopped[0]['kind'], 'CANCEL')
        controller.dispatch(state, effs_stopped)
        self.assertEqual(order['cancel_attempt'], 6)
        self.assertEqual(port.orders, [])

    def test_external_order_stale_cancel_recovers_and_preserves_prior_status_on_local_refusal(self):
        controller, journal, _port = self._new_controller()
        state = journal.load()
        ext = [{
            'native_order_id': 'ext-1',
            'contract_id': UNCONF_CONTRACT,
            'direction': 'BUY',
            'native_offset': 'OPEN',
            'volume_left': 2,
            'limit_price': '5000',
        }]
        um = [self._unconf_market(long_qty=0)]

        with journal.transaction():
            eff1, _st, _ev = controller.plan_convergence(state, {}, um, ext, 1000.0)
            journal.save(state)
        self.assertEqual(len(eff1), 1)
        controller.dispatch(state, eff1)
        rec = state['convergence_cancels']['ext-1']
        self.assertEqual(rec['cancel_status'], 'CALL_RETURNED')
        self.assertEqual(rec['cancel_attempt'], 1)

        with journal.transaction():
            eff_early, _st, _ev = controller.plan_convergence(state, {}, um, ext, 1002.0)
            journal.save(state)
        self.assertEqual(eff_early, [])

        for idx, wt in enumerate((1005.0, 1010.0, 1015.0), start=2):
            with journal.transaction():
                eff_r, _st, _ev = controller.plan_convergence(state, {}, um, ext, wt)
                journal.save(state)
            self.assertEqual(len(eff_r), 1)
            controller.dispatch(state, eff_r)
            rec = state['convergence_cancels']['ext-1']
            self.assertEqual(rec['cancel_attempt'], idx)
            self.assertEqual(rec['cancel_seq'], idx)

        with journal.transaction():
            eff_wait, _st, _ev = controller.plan_convergence(state, {}, um, ext, 1030.0)
            journal.save(state)
        self.assertEqual(eff_wait, [])

        with journal.transaction():
            eff_probe, _st, _ev = controller.plan_convergence(state, {}, um, ext, 1136.0)
            journal.save(state)
        self.assertEqual(len(eff_probe), 1)
        controller.dispatch(state, eff_probe)
        self.assertEqual(state['convergence_cancels']['ext-1']['cancel_attempt'], 5)

        # Retry of UNKNOWN external cancel encountering CancelNotAttempted preserves UNKNOWN.
        class _FailThenRefusePort:
            def __init__(self):
                self.calls = 0

            def send(self, _req):
                return {'ok': True}

            def cancel(self, req):
                self.calls += 1
                if self.calls == 1:
                    raise OSError('network drop during external cancel')
                return CancelNotAttempted(req['id'], req['native_order_id'], 'E_DSV_MARKET_PHASE', {'message': 'phase'})

        c2, j2, _p2 = self._new_controller(port=_FailThenRefusePort())
        s2 = j2.load()
        ext2 = [{
            'native_order_id': 'ext-2',
            'contract_id': UNCONF_CONTRACT,
            'direction': 'BUY',
            'native_offset': 'OPEN',
            'volume_left': 1,
            'limit_price': '5000',
        }]
        with j2.transaction():
            e_first, _st, _ev = c2.plan_convergence(s2, {}, um, ext2, 100.0)
            j2.save(s2)
        c2.dispatch(s2, e_first)
        self.assertEqual(s2['convergence_cancels']['ext-2']['cancel_status'], 'UNKNOWN')

        with j2.transaction():
            e_retry, _st, _ev = c2.plan_convergence(s2, {}, um, ext2, 106.0)
            j2.save(s2)
        self.assertEqual(len(e_retry), 1)
        c2.dispatch(s2, e_retry)
        self.assertEqual(s2['convergence_cancels']['ext-2']['cancel_status'], 'UNKNOWN')
        self.assertEqual(s2['convergence_cancels']['ext-2']['cancel_attempt'], 2)
        self.assertEqual(s2['convergence_cancels']['ext-2']['cancel_failure_count'], 0)

    def test_first_cancel_local_rejection_does_not_consume_cancel_attempt(self):
        class _LocalRejectPort:
            def send(self, _req):
                return {'ok': True}

            def cancel(self, req):
                return CancelNotAttempted(
                    req['id'], req['native_order_id'], 'E_DSV_SDK_CANCEL_RISK', {'message': 'local risk'},
                )

        controller, journal, _port = self._new_controller(port=_LocalRejectPort())
        state = journal.load()
        order = {
            'child_order_id': 'c-local-rej-1',
            'intent_id': f'conv-{UNCONF_CONTRACT}-long',
            'attempt_id': 'c-local-rej-1',
            'position_id': f'conv-{UNCONF_CONTRACT}-long',
            'contract_id': UNCONF_CONTRACT,
            'direction': 'SELL',
            'native_offset': 'CLOSE',
            'effect': 'REDUCE',
            'original_qty': 1,
            'filled_qty': 0,
            'limit_price': '5000',
            'send_state': 'NATIVE_SEEN',
            'broker_state': 'ALIVE',
            'cancel_status': None,
            'terminal_confirmed': False,
            'reported_fill_qty': 0,
            'native_id': 'nat-local-1',
            'native_trading_day': DAY,
        }
        state['orders']['c-local-rej-1'] = order
        with journal.transaction():
            eff = controller.cancel(order, 'USER_STOP', wall_time=100.0)
            journal.save(state)
        self.assertIsNotNone(eff)
        controller.dispatch(state, [eff])
        self.assertIsNone(order['cancel_status'])
        self.assertEqual(order['cancel_attempt'], 0)
        self.assertEqual(order['cancel_seq'], 1)

        # Same for external convergence cancel on first local rejection.
        um = [self._unconf_market(long_qty=0)]
        ext = [{
            'native_order_id': 'ext-local-1',
            'contract_id': UNCONF_CONTRACT,
            'direction': 'BUY',
            'native_offset': 'OPEN',
            'volume_left': 1,
            'limit_price': '5000',
        }]
        with journal.transaction():
            effs, _st, _ev = controller.plan_convergence(state, {}, um, ext, 100.0)
            journal.save(state)
        self.assertEqual(len(effs), 1)
        controller.dispatch(state, effs)
        rec = state['convergence_cancels']['ext-local-1']
        self.assertIsNone(rec['cancel_status'])
        self.assertEqual(rec['cancel_attempt'], 0)
        self.assertEqual(rec['cancel_seq'], 1)
        self.assertEqual(rec['cancel_failure_count'], 1)

    def test_convergence_broker_market_phase_does_not_penalize_failures_and_uses_5s_backoff(self):
        controller, journal, _port = self._new_controller()
        state = journal.load()
        state['orders']['conv-fail-1'] = {
            'child_order_id': 'conv-fail-1',
            'intent_id': f'conv-{UNCONF_CONTRACT}-long',
            'attempt_id': 'conv-fail-1',
            'position_id': f'conv-{UNCONF_CONTRACT}-long',
            'contract_id': UNCONF_CONTRACT,
            'direction': 'SELL',
            'native_offset': 'CLOSE',
            'effect': 'REDUCE',
            'original_qty': 2,
            'filled_qty': 0,
            'limit_price': '5000',
            'send_state': 'CALL_RETURNED',
            'broker_state': 'REJECTED',
            'cancel_status': None,
            'terminal_confirmed': True,
            'reported_fill_qty': 0,
            'native_id': 'nat-fail-1',
            'native_trading_day': DAY,
            'convergence_order': True,
            'convergence_side': 'LONG',
            'broker_rejection_category': 'TRANSIENT',
            'broker_rejection_detail': 'BROKER_MARKET_PHASE',
            'convergence_failure_at': '1000.0',
        }
        um = [self._unconf_market(long_qty=2)]

        with journal.transaction():
            eff_2s, st_2s, _ev = controller.plan_convergence(state, {}, um, [], 1002.0)
            journal.save(state)
        self.assertEqual(eff_2s, [])
        self.assertEqual(st_2s[UNCONF_CONTRACT]['status'], 'DEFERRED_TRANSIENT_RETRY_BACKOFF')
        self.assertEqual(st_2s[UNCONF_CONTRACT]['wait_reason'], 'BROKER_MARKET_PHASE')
        self.assertEqual(st_2s[UNCONF_CONTRACT]['failure_count'], 0)

        with journal.transaction():
            eff_6s, st_6s, _ev = controller.plan_convergence(state, {}, um, [], 1006.0)
            journal.save(state)
        self.assertEqual(len(eff_6s), 1)
        self.assertEqual(eff_6s[0]['kind'], 'SEND')
        self.assertEqual(st_6s[UNCONF_CONTRACT]['failure_count'], 0)


class MultiContractCombinationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _make_session(self, name: str):
        contracts = (CONTRACT, 'DCE.m2701')
        broker = FakeBroker(
            account_key=f'DEV:{name}',
            contract=CONTRACT,
            contracts=contracts,
            trading_day=DAY,
            trace_path=self.root / f'{name}-broker.jsonl',
        )
        port = _FakeBrokerPort(broker)
        session = DsvSession(
            self.root / f'{name}-journal.sqlite',
            account_key=f'DEV:{name}',
            provider='OFFLINE',
            trading_day=DAY,
            profile=_multi_profile(contracts),
            port=port,
        )
        self.addCleanup(session.close)
        return broker, port, session

    def test_limit_down_on_contract_a_does_not_block_contract_b(self):
        broker, port, session = self._make_session('limit-down-a-normal-b')
        session.command({'id': 'cmd-start', 'action': 'START'})

        f1 = broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1')
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        # Contract A breaks out and fills 5 lots; Contract B stays neutral at 1055.
        f2 = broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1055', candle_id='bar-1')
        _set_prices(f2, {CONTRACT: '1061'})
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])
        p_open = port.orders[-1]['child_order_id']
        broker.accept_order(p_open)
        broker.fill(p_open, 5, '1061')

        f3 = broker.prepare_frame(wall_time=f'{DAY}T01:10:06Z', monotonic_ms=7000, last='1055', candle_id='bar-1')
        _set_prices(f3, {CONTRACT: '1061'})
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])
        self.assertEqual(session.snapshot()['symbols'][CONTRACT]['position']['owned'], 5)

        # Contract A crashes to limit-down (900, bid1=None) and queues exit;
        # Contract B simultaneously breaks out to 1061 and fills!
        f4 = broker.prepare_frame(wall_time=f'{DAY}T01:11:00Z', monotonic_ms=61000, last='1061', candle_id='bar-2')
        _set_prices(
            f4,
            {CONTRACT: '900', 'DCE.m2701': '1061'},
            extra={CONTRACT: {'bid1': None, 'ask1': '900', 'lower_limit': '900', 'upper_limit': '1200'}},
        )
        session.apply_round(f4)
        broker.acknowledge_frame(f4['id'])

        p_exits = [o for o in port.orders if o['contract_id'] == CONTRACT and o['native_offset'] != 'OPEN']
        m_opens = [o for o in port.orders if o['contract_id'] == 'DCE.m2701' and o['native_offset'] == 'OPEN']
        self.assertEqual(len(p_exits), 1)
        self.assertEqual(p_exits[0]['limit_price'], '900')
        self.assertEqual(len(m_opens), 1)
        broker.accept_order(p_exits[0]['child_order_id'])
        broker.accept_order(m_opens[0]['child_order_id'])
        broker.fill(m_opens[0]['child_order_id'], 5, '1061')

        f5 = broker.prepare_frame(wall_time=f'{DAY}T01:11:05Z', monotonic_ms=66000, last='1061', candle_id='bar-2')
        _set_prices(
            f5,
            {CONTRACT: '900'},
            extra={CONTRACT: {'bid1': None, 'ask1': '900', 'lower_limit': '900', 'upper_limit': '1200'}},
        )
        session.apply_round(f5)
        broker.acknowledge_frame(f5['id'])
        self.assertEqual(session.snapshot()['symbols']['DCE.m2701']['position']['owned'], 5)

        # Contract B hits stop-loss (1051) while Contract A stays limit-down -> B exits cleanly, A has no cancel churn.
        f6 = broker.prepare_frame(wall_time=f'{DAY}T01:11:10Z', monotonic_ms=71000, last='1051', candle_id='bar-2')
        _set_prices(
            f6,
            {CONTRACT: '900'},
            extra={CONTRACT: {'bid1': None, 'ask1': '900', 'lower_limit': '900', 'upper_limit': '1200'}},
        )
        session.apply_round(f6)
        broker.acknowledge_frame(f6['id'])

        m_exits = [o for o in port.orders if o['contract_id'] == 'DCE.m2701' and o['native_offset'] != 'OPEN']
        self.assertEqual(len(m_exits), 1)
        self.assertEqual(len([o for o in port.orders if o['contract_id'] == CONTRACT and o['native_offset'] != 'OPEN']), 1)
        self.assertEqual([c for c in port.cancellations if c['contract_id'] == CONTRACT], [])

    def test_late_fill_after_stop_updates_owned_position_without_duplicate_open(self):
        broker, port, session = self._make_session('late-fill-after-stop')
        session.command({'id': 'cmd-start', 'action': 'START'})

        f1 = broker.prepare_frame(wall_time=f'{DAY}T01:10:00Z', monotonic_ms=1000, last='1055', candle_id='bar-1')
        session.apply_round(f1)
        broker.acknowledge_frame(f1['id'])

        f2 = broker.prepare_frame(wall_time=f'{DAY}T01:10:05Z', monotonic_ms=6000, last='1055', candle_id='bar-1')
        _set_prices(f2, {CONTRACT: '1061'})
        session.apply_round(f2)
        broker.acknowledge_frame(f2['id'])
        self.assertEqual(len(port.orders), 1)
        p_open = port.orders[0]['child_order_id']
        broker.accept_order(p_open)

        # Operator stops strategy while order is ALIVE; then delayed fill arrives.
        session.command({'id': 'cmd-stop', 'action': 'STOP'})
        broker.fill(p_open, 5, '1061')

        f3 = broker.prepare_frame(wall_time=f'{DAY}T01:10:08Z', monotonic_ms=9000, last='1061', candle_id='bar-1')
        session.apply_round(f3)
        broker.acknowledge_frame(f3['id'])

        snap = session.snapshot()
        self.assertFalse(snap['running'])
        self.assertEqual(len(port.orders), 1)
        self.assertEqual(snap['symbols'][CONTRACT]['position']['owned'], 5)
        self.assertTrue(snap['symbols'][CONTRACT]['position']['entry_stopped'])


class TradeParametersDraftAndSavedStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _new_window(self, *, runtime_root=None):
        win = WorkbenchWindow(None, runtime_root=runtime_root)
        self.addCleanup(dispose_widget, win)
        return win

    def test_trade_panel_draft_survives_symbol_edit_runtime_sync_and_invalid_mid_typing(self):
        win = self._new_window()
        panel = win.dashboard.trade_params_panel

        # Edit a switch and an int field on TradeParametersPanel.
        sw = panel.parameter_switches['no_open_after_open']
        target_checked = not sw.isChecked()
        sw.setChecked(target_checked)
        max_vol = panel.parameter_fields['max_volume']
        max_vol.setText('5')
        max_vol.editingFinished.emit()
        self.assertTrue(panel.has_unsaved_changes())

        # Mutate symbols through _run_symbol_dialog_edit -> trade edits must stay intact in both panel and dialog.
        def _add_silver(dialog):
            rec = dialog.profile.to_record()
            base_sym = dict(rec['settings']['symbols'][0])
            rec['settings']['symbols'].append(base_sym | {'code': 'SHFE.ag2702', 'name': '白银'})
            return dialog.apply_edited_preview(DsvProfile.from_record(rec), expected_hash=dialog.profile.content_hash)

        self.assertTrue(win.dashboard._run_symbol_dialog_edit(_add_silver))
        self.assertEqual(sw.isChecked(), target_checked)
        self.assertEqual(max_vol.text(), '5')
        dialog_rec = win.dashboard.parameter_dialog().profile.to_record()
        self.assertEqual(dialog_rec['settings']['trade']['max_volume'], 5)
        self.assertEqual(dialog_rec['settings']['trade']['no_open_after_open'], target_checked)
        self.assertIn('SHFE.ag2702', [s['code'] for s in dialog_rec['settings']['symbols']])

        # Background runtime profile sync with a different remote baseline preserves valid user draft.
        remote_rec = win.dashboard.profile.to_record()
        remote_rec['settings']['trade']['max_daily_orders'] = 12
        win.dashboard.sync_runtime_profile({'configuration': {'strategy_profile': remote_rec}})
        self.assertEqual(sw.isChecked(), target_checked)
        self.assertEqual(max_vol.text(), '5')
        self.assertTrue(panel.has_unsaved_changes())

        # Invalid mid-typing text in panel is also preserved when runtime baseline updates.
        max_vol.setText('')
        remote_rec2 = win.dashboard.profile.to_record()
        remote_rec2['settings']['trade']['max_daily_orders'] = 15
        win.dashboard.sync_runtime_profile({'configuration': {'strategy_profile': remote_rec2}})
        self.assertEqual(max_vol.text(), '')
        self.assertTrue(panel.has_unsaved_changes())

    def test_next_boot_saved_profile_is_distinguished_from_unsaved_edits(self):
        win = self._new_window()
        panel = win.dashboard.trade_params_panel

        night_close = panel.parameter_fields['night_session_close']
        night_close.setText('23:30')
        night_close.editingFinished.emit()
        self.assertTrue(panel.has_unsaved_changes())

        candidate = panel.build_profile(win.dashboard.profile)
        win.dashboard.mark_next_boot_saved(candidate)
        self.assertFalse(panel.has_unsaved_changes())
        self.assertIn('已保存至启动配置', panel.dirty_label.text())

        # Subsequent edit flips back to unsaved.
        panel.parameter_fields['max_volume'].setText('7')
        self.assertTrue(panel.has_unsaved_changes())
        self.assertIn('已修改', panel.dirty_label.text())

    def test_runtime_profile_sync_conflict_prompts_user(self):
        win = self._new_window()
        panel = win.dashboard.trade_params_panel
        panel.parameter_fields['max_volume'].setText('6')
        self.assertTrue(panel.has_unsaved_changes())

        remote_rec = win.dashboard.profile.to_record()
        remote_rec['settings']['trade']['max_daily_orders'] = 22
        win.dashboard.sync_runtime_profile({'configuration': {'strategy_profile': remote_rec}})
        self.assertIn('Runtime 运行配置已更新', panel.feedback_label.text())
        self.assertEqual(panel.parameter_fields['max_volume'].text(), '6')

    def test_offset_apply_blocked_while_trade_panel_has_invalid_input(self):
        win = self._new_window()
        panel = win.dashboard.trade_params_panel
        panel.parameter_fields['max_volume'].setText('')
        self.assertTrue(panel.has_unsaved_changes())

        calc = win.dashboard.calculator
        cand_rec = calc.base_profile.to_record()
        cand_rec['settings']['symbols'][0]['open_offset'] = '99'
        calc._result_profile = DsvProfile.from_record(cand_rec)
        calc.accepted.emit()

        self.assertIn('非法输入', calc.error_label.text())
        self.assertEqual(panel.parameter_fields['max_volume'].text(), '')

    def test_arm_prompts_when_trade_parameters_have_unsaved_changes(self):
        from unittest.mock import patch

        win = self._new_window()
        win.pipe_name = 'test-pipe'
        win._boot = 1
        win._stale = False
        win.view = {
            'account_key': 'sim:test',
            'entry_authority': 'DISARMED',
            'policies': [{'policy_kind': 'AUTHORITY', 'policy_id': 'auth-1'}],
            'configuration': {
                'rule_set_id': 'dsv-v1',
                'config_revision': 'rev-1',
            },
        }
        sent = []
        win.send = lambda action, scope, params: sent.append((action, scope, params))
        win._action = lambda fn: fn()

        panel = win.dashboard.trade_params_panel
        panel.parameter_fields['max_volume'].setText('7')
        self.assertTrue(panel.has_unsaved_changes())
        self.assertFalse(win.dashboard.has_unsaved_symbol_changes())

        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No) as mock_q:
            win.arm()
        mock_q.assert_called_once()
        self.assertEqual(sent, [])
        self.assertIs(win.dashboard.tabs.currentWidget(), win.dashboard.parameter_tab)

        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes) as mock_q:
            win.arm()
        mock_q.assert_called_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 'ARM_ENTRIES')

    def test_revert_trade_parameters_respects_saved_next_boot_and_cancels_cleanly(self):
        win = self._new_window()
        panel = win.dashboard.trade_params_panel
        active_profile = win.dashboard.profile
        orig_night = active_profile.to_record()['settings']['trade']['night_session_close']
        orig_max_vol = str(active_profile.to_record()['settings']['trade']['max_volume'])

        saved_profiles = []
        fake_store = SimpleNamespace(save_profile=lambda p: saved_profiles.append(p))
        win._portable_store_and_account = lambda: (fake_store, {'account_key': 'sim:test'})

        # Step 1: Save B (night_session_close='23:30') as next-boot config.
        panel.parameter_fields['night_session_close'].setText('23:30')
        panel.parameter_fields['night_session_close'].editingFinished.emit()
        profile_b = panel.build_profile(active_profile)
        win.dashboard.mark_next_boot_saved(profile_b)
        self.assertFalse(panel.has_unsaved_changes())

        # Step 2: Edit C on top of B, then click revert -> should revert to B, NOT A.
        panel.parameter_fields['max_volume'].setText('9')
        self.assertTrue(panel.has_unsaved_changes())
        win.dashboard.revert_trade_parameters()
        self.assertEqual(panel.parameter_fields['night_session_close'].text(), '23:30')
        self.assertEqual(panel.parameter_fields['max_volume'].text(), orig_max_vol)
        self.assertIsNotNone(win.dashboard._saved_next_boot_profile)
        self.assertFalse(panel.has_unsaved_changes())
        self.assertEqual(saved_profiles, [])

        # Step 3: Click revert again while viewing B -> should cancel B on disk and restore A.
        win.dashboard.revert_trade_parameters()
        self.assertEqual(len(saved_profiles), 1)
        self.assertEqual(saved_profiles[0].content_hash, active_profile.content_hash)
        self.assertIsNone(win.dashboard._saved_next_boot_profile)
        self.assertIsNone(win.dashboard._saved_next_boot_hash)
        self.assertEqual(panel.parameter_fields['night_session_close'].text(), orig_night)

    def test_manual_edit_back_to_active_while_next_boot_saved_is_still_unsaved(self):
        win = self._new_window()
        panel = win.dashboard.trade_params_panel
        active_profile = win.dashboard.profile
        orig_night = active_profile.to_record()['settings']['trade']['night_session_close']

        # Save B (night_session_close='23:30') as pending next-boot config.
        panel.parameter_fields['night_session_close'].setText('23:30')
        panel.parameter_fields['night_session_close'].editingFinished.emit()
        profile_b = panel.build_profile(active_profile)
        win.dashboard.mark_next_boot_saved(profile_b)
        self.assertFalse(panel.has_unsaved_changes())

        # Manually edit UI back to A without saving -> disk still holds B.
        panel.parameter_fields['night_session_close'].setText(orig_night)
        panel.parameter_fields['night_session_close'].editingFinished.emit()
        self.assertTrue(panel.has_unsaved_changes())
        self.assertTrue(panel.revert_button.isEnabled())
        self.assertIn('尚未保存覆盖待重启启动配置', panel.dirty_label.text())

        # Clicking revert restores saved next-boot config B without touching disk.
        win.dashboard.revert_trade_parameters()
        self.assertEqual(panel.parameter_fields['night_session_close'].text(), '23:30')
        self.assertFalse(panel.has_unsaved_changes())
        self.assertIsNotNone(win.dashboard._saved_next_boot_profile)

        # Symbol tab symmetry: when B has different symbols, reverting dialog draft back to A is still unsaved.
        record_sym_b = active_profile.to_record()
        record_sym_b['settings']['symbols'] = record_sym_b['settings']['symbols'][:-1]
        profile_sym_b = type(active_profile).from_record(record_sym_b)
        dialog = win.dashboard.parameter_dialog()
        win.dashboard.mark_next_boot_saved(profile_sym_b)
        self.assertFalse(win.dashboard.has_unsaved_symbol_changes())
        dialog.set_preview_profile(active_profile, emit_signal=False)
        self.assertTrue(win.dashboard.has_unsaved_symbol_changes())

    def test_store_none_blocks_clearing_next_boot_on_revert_and_save(self):
        from unittest.mock import patch

        win = self._new_window()
        panel = win.dashboard.trade_params_panel
        active_profile = win.dashboard.profile

        panel.parameter_fields['night_session_close'].setText('23:30')
        panel.parameter_fields['night_session_close'].editingFinished.emit()
        profile_b = panel.build_profile(active_profile)
        win.dashboard.mark_next_boot_saved(profile_b)
        win._portable_store_and_account = lambda: (None, None)

        # 1) Revert while viewing B with store=None must fail closed and keep next-boot state.
        with patch.object(QMessageBox, 'critical') as mock_crit:
            win.dashboard.revert_trade_parameters()
        mock_crit.assert_called_once()
        self.assertEqual(win.dashboard._saved_next_boot_hash, profile_b.content_hash)
        self.assertIsNotNone(win.dashboard._saved_next_boot_profile)

        # 2) Saving active profile A to overwrite B with store=None must also fail closed.
        with patch.object(QMessageBox, 'critical') as mock_crit:
            ok = win.save_runtime_parameters(active_profile)
        self.assertFalse(ok)
        mock_crit.assert_called_once()
        self.assertEqual(win.dashboard._saved_next_boot_hash, profile_b.content_hash)
        self.assertIsNotNone(win.dashboard._saved_next_boot_profile)

    def test_portable_store_init_error_is_caught_on_revert_and_save(self):
        from unittest.mock import patch
        from online_trader.errors import TraderError

        win = self._new_window()
        panel = win.dashboard.trade_params_panel
        active_profile = win.dashboard.profile

        panel.parameter_fields['night_session_close'].setText('23:30')
        panel.parameter_fields['night_session_close'].editingFinished.emit()
        profile_b = panel.build_profile(active_profile)
        win.dashboard.mark_next_boot_saved(profile_b)

        def _broken_store():
            raise TraderError('E_RUNTIME_CONFIG', 'corrupt runtime.json')

        win._portable_store_and_account = _broken_store

        # 1) Revert while viewing B catches store init TraderError and preserves next-boot state.
        with patch.object(QMessageBox, 'critical') as mock_crit:
            win.dashboard.revert_trade_parameters()
        mock_crit.assert_called_once()
        self.assertEqual(win.dashboard._saved_next_boot_hash, profile_b.content_hash)

        # 2) Saving active profile A to clear B catches store init TraderError.
        with patch.object(QMessageBox, 'critical') as mock_crit:
            ok_restore = win.save_runtime_parameters(active_profile)
        self.assertFalse(ok_restore)
        mock_crit.assert_called_once()
        self.assertEqual(win.dashboard._saved_next_boot_hash, profile_b.content_hash)

        # 3) Saving startup-only profile B catches store init TraderError.
        with patch.object(QMessageBox, 'critical') as mock_crit:
            ok_save = win.save_runtime_parameters(profile_b)
        self.assertFalse(ok_save)
        mock_crit.assert_called_once()


class UiHangMonitorAsyncRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_heartbeat_recovery_writes_on_monitor_thread_not_caller_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            caller_ident = threading.get_ident()
            emit_records = []
            monitor = UiHangMonitor(
                threshold_s=0.05,
                poll_interval_s=0.02,
                cooldown_s=0.5,
                log_dir_getter=lambda: run_dir,
            )
            orig_emit = monitor._emit

            def _tracked_emit(payload, *, level):
                emit_records.append((payload.get('event'), threading.get_ident()))
                orig_emit(payload, level=level)

            monitor._emit = _tracked_emit
            monitor.start()
            try:
                deadline = time.monotonic() + 1.0
                while monitor.dump_count < 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(monitor.dump_count, 1)
                monitor.heartbeat()
            finally:
                monitor.stop()

            diag_file = run_dir / 'ui-hang-diagnostics.jsonl'
            self.assertTrue(diag_file.is_file())
            events = [json.loads(line)['event'] for line in diag_file.read_text(encoding='utf-8').splitlines() if line.strip()]
            self.assertIn('UI_HANG_DETECTED', events)
            self.assertIn('UI_HANG_RECOVERED', events)
            recovered_threads = [tid for ev, tid in emit_records if ev == 'UI_HANG_RECOVERED']
            self.assertTrue(recovered_threads)
            self.assertTrue(all(tid != caller_ident for tid in recovered_threads))

    def test_workbench_hang_diagnostic_directory_falls_back_to_runtime_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        diag_dir = root / 'diagnostics'
        diag_dir.mkdir()
        win = WorkbenchWindow(None, runtime_root=root)
        self.addCleanup(dispose_widget, win)

        self.assertIsNone(win.dashboard.log_directory)
        self.assertEqual(win.dashboard._hang_diagnostic_directory(), diag_dir)

        run_dir = root / 'run'
        run_dir.mkdir()
        win.dashboard.log_directory = run_dir
        self.assertEqual(win.dashboard._hang_diagnostic_directory(), run_dir)


if __name__ == '__main__':
    unittest.main()
