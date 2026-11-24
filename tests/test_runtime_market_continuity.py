"""Offline unit tests for Runtime market continuity, owner offload, and stale-recheck flows."""

import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from online_trader.desktop.runtime_client.state import (
    ClientConnectionState, ClientStateStore, OWNER_RECHECK_OPERATIONS,
)
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.desktop.workbench_layout import market_quote_states
from online_trader.dsv_runtime.market_health import evaluate_market_health
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.dsv_runtime.standalone_projection import format_minute_chart_bars
from online_trader.dsv_runtime.target_diagnostics import TargetMarketDiagnostics
from online_trader.gateway.tq_wait import _pinned_sdk_available, wait_update_slice
from online_trader.kernel.server import ControlServer
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.clock import NS_PER_SECOND
from qa.target_owner.offline_runtime import OfflineTargetRuntime, profile as single_contract_profile


def _market_payload(
    *,
    active: bool = True,
    fresh: bool = True,
    age_quality: str | None = None,
    age_ns: int = 100_000_000,
    silence_ns: int | None = None,
    source_time_raw: str | None = '2026-11-24T01:10:00Z',
    reason: str | None = None,
):
    quality = age_quality if age_quality is not None else ('VALID' if fresh else 'STALE')
    return {
        'market_session_active': active,
        'ticks': [{'last': '1060'}] if (fresh or source_time_raw is not None) else [],
        'quote_observation': {
            'age_quality': quality,
            'source_age_raw_ns': str(age_ns),
            'silence_ns': str(age_ns if silence_ns is None else silence_ns),
            'source_time_raw': source_time_raw,
            'reason': reason,
        },
    }


def _client_snapshot(operations):
    return {
        'account_key': 'DEV:continuity',
        'kernel_boot_id': 'boot-continuity-1',
        'checkpoint_event_seq': '5',
        'configuration': {'rule_set_id': 'rule-1', 'config_revision': '1'},
        'revisions': {'rule_set': '1'},
        'capabilities': {
            'operations': [{'operation': op, 'allowed': allowed, 'reason': reason}
                           for op, allowed, reason in operations],
        },
    }


class StaleSnapshotRecheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_recheck_operations_remain_allowed_when_stale_or_synchronizing(self):
        store = ClientStateStore()
        store.apply({'view': _client_snapshot([
            ('PAUSE_NEW_ENTRIES', False, 'STALE'),
            ('STOP_DSV_STRATEGY', True, None),
            ('CANCEL_OWNED', False, 'SYNCHRONIZING'),
            ('MANUAL_CLOSE', True, None),
            ('REQUEST_RECONCILIATION', True, None),
            ('ARM_ENTRIES', True, None),
            ('SAVE_DSV_PARAMETERS', True, None),
            ('MANUAL_OPEN', True, None),
        ])}, {'event_seq': '5'}, 100)

        for state, expected_block_reason in (
            (ClientConnectionState.STALE, 'STALE'),
            (ClientConnectionState.SYNCHRONIZING, 'SYNCHRONIZING'),
        ):
            store.set_connection(state, expected_block_reason)
            for op in OWNER_RECHECK_OPERATIONS:
                if op in {'PAUSE_NEW_ENTRIES', 'STOP_DSV_STRATEGY', 'CANCEL_OWNED',
                          'MANUAL_CLOSE', 'REQUEST_RECONCILIATION'}:
                    allowed, reason = store.capability(op)
                    self.assertTrue(allowed, f'{op} should be allowed in {state}: {reason}')
                    self.assertIsNone(reason)
            for blocked_op in ('ARM_ENTRIES', 'SAVE_DSV_PARAMETERS', 'MANUAL_OPEN'):
                allowed, reason = store.capability(blocked_op)
                self.assertFalse(allowed, f'{blocked_op} must stay blocked in {state}')
                self.assertEqual(reason, expected_block_reason)

        store.apply({'view': _client_snapshot([
            ('PAUSE_NEW_ENTRIES', False, 'E_STORAGE'),
        ])}, {'event_seq': '6'}, 200)
        store.set_connection(ClientConnectionState.STALE, 'STALE')
        allowed, reason = store.capability('PAUSE_NEW_ENTRIES')
        self.assertFalse(allowed)
        self.assertEqual(reason, 'E_STORAGE')

    def test_snapshot_age_tiers_and_symbol_quote_labels(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = QSettings(str(Path(temp.name) / 'workbench.ini'), QSettings.IniFormat)
        with (
            patch('online_trader.desktop.workbench.workbench_preferences', return_value=settings),
            patch('online_trader.desktop.runtime_client.client.RuntimeClientCoordinator.start'),
            patch('online_trader.desktop.runtime_client.client.RuntimeClientCoordinator.close'),
        ):
            window = WorkbenchWindow('Continuity.Tier.Test', runtime_view=True)
            self.addCleanup(window.close)
            window.snapshot_published_ns = None
            self.assertEqual(window.snapshot_age_tier(), ('UNKNOWN_BACKEND', None))

            now = time.monotonic_ns()
            window.snapshot_published_ns = now - 500_000_000
            self.assertEqual(window.snapshot_age_tier()[0], 'NORMAL')

            window.snapshot_published_ns = now - 3_000_000_000
            self.assertEqual(window.snapshot_age_tier()[0], 'SLOW')

            window.snapshot_published_ns = now - 7_000_000_000
            self.assertEqual(window.snapshot_age_tier()[0], 'STALE')

            window.snapshot_published_ns = now - 16_000_000_000
            self.assertEqual(window.snapshot_age_tier()[0], 'UNKNOWN_BACKEND')

        states = market_quote_states(
            {
                'A': {'symbol_health': 'FRESH'},
                'B': {'symbol_health': 'QUIET'},
                'C': {'symbol_health': 'STALE'},
                'D': {'symbol_health': 'MARKET_CLOSED'},
                'E': {'symbol_health': 'WAITING_FIRST_QUOTE'},
                'F': {'symbol_health': 'INVALID_QUOTE'},
            },
            ['A', 'B', 'C', 'D', 'E', 'F'],
            {},
        )
        self.assertEqual(states, {
            'A': '新鲜',
            'B': '盘口静默',
            'C': '行情已滞后',
            'D': '休市',
            'E': '等待首笔报价',
            'F': '报价异常',
        })


class TargetMarketDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_emits_summary_and_anomaly_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diag = TargetMarketDiagnostics(temp_dir, boot_id='boot-diag-1', sdk_version=SDK_VERSION)
            owner = SimpleNamespace(
                adapter=SimpleNamespace(
                    metrics={
                        'source_epoch': 'epoch-1',
                        'sdk_updated': False,
                        'sdk_pump_ns': 12_000_000,
                        'sdk_pump_gap_ns': 100_000_000,
                        'native_facts_ns': 2_000_000,
                        'transaction_ns': 3_000_000,
                        'dispatch_ns': 1_000_000,
                    },
                    _health={'reference_status': 'TRUSTED_UTC', 'reason': None, 'utc_error_ns': -250_000},
                    dispatch_market={
                        'DCE.p2701': _market_payload(active=True, age_quality='VALID', silence_ns=4_000_000_000),
                        'SHFE.ag2702': _market_payload(active=True, age_quality='VALID', silence_ns=4_500_000_000),
                    },
                ),
                connection=SimpleNamespace(source_epoch='epoch-1'),
                publication_status={'last_prepare_ns': 4_000_000, 'last_encode_ns': 9_000_000},
            )
            base_ns = time.monotonic_ns()
            diag.observe_tick(
                owner=owner,
                owner_loop_ns=15_000_000,
                synchronized=True,
                now_ns=base_ns + 100_000_000,
            )
            diag.observe_tick(
                owner=owner,
                owner_loop_ns=20_000_000,
                synchronized=True,
                now_ns=base_ns + 1_300_000_000,
            )
            diag.close()

            self.assertEqual(diag._write_failures, 0)
            summaries = [
                json.loads(line)
                for line in (Path(temp_dir) / 'target-market-summary.jsonl').read_text(encoding='utf-8').splitlines()
                if line.strip()
            ]
            anomalies = [
                json.loads(line)
                for line in (Path(temp_dir) / 'target-market-anomaly.jsonl').read_text(encoding='utf-8').splitlines()
                if line.strip()
            ]
            self.assertGreaterEqual(len(summaries), 1)
            self.assertEqual(summaries[0]['schema_version'], 'DSV_TARGET_MARKET_SUMMARY/1')
            self.assertEqual(summaries[0]['queue_drops'], 0)
            self.assertEqual(len(anomalies), 1)
            self.assertEqual(anomalies[0]['schema_version'], 'DSV_TARGET_MARKET_ANOMALY/1')
            self.assertEqual(anomalies[0]['trigger'], 'ALL_ACTIVE_SYMBOLS_SILENT')
            self.assertEqual(len(anomalies[0]['recent_samples']), 1)

    def test_diagnostics_drops_when_queue_full_without_raising(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            diag = TargetMarketDiagnostics(temp_dir, boot_id='boot-diag-2', sdk_version=SDK_VERSION)
            diag._closing.set()
            if diag._writer is not None:
                diag._writer.join(timeout=1.5)
            diag._writer = None
            ok = diag._enqueue(Path(temp_dir) / 'x.jsonl', {'a': 1})
            self.assertFalse(ok)
            self.assertEqual(diag._queue_drops, 1)


class OwnerProjectionOffloadAndIdleSkipTests(unittest.TestCase):
    def test_prepare_workbench_publication_defers_projection_and_formats_bars_in_background(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with OfflineTargetRuntime(temp_dir) as runtime:
                calls = []
                real_wb = __import__(
                    'online_trader.dsv_runtime.standalone_owner', fromlist=['workbench_view']
                ).workbench_view

                def recording_workbench_view(*args, **kwargs):
                    calls.append((args, kwargs))
                    return real_wb(*args, **kwargs)

                def check(owner):
                    raw_bars = {'DCE.p2701': [(1762995600_000_000_000, 1050.0, 1065.0, 1048.0, 1060.0)]}
                    owner.adapter.minute_chart_bars = lambda: format_minute_chart_bars(raw_bars)
                    with patch('online_trader.dsv_runtime.standalone_owner.workbench_view',
                               side_effect=recording_workbench_view):
                        complete = owner.prepare_workbench_publication()
                        self.assertEqual(calls, [])
                        base, view = complete()
                        self.assertEqual(len(calls), 1)
                        self.assertIn('projection_inputs', calls[0][1])
                        self.assertGreaterEqual(len(base['records']), 1)
                        self.assertEqual(view['account_key'], owner.account_key)
                        self.assertEqual(view['view_schema'], 'workbench/1')

                runtime.call_owner(check)

            formatted = format_minute_chart_bars(
                {'DCE.p2701': [(1762995600_000_000_000, 1050.0, 1065.0, 1048.0, 1060.0)]},
            )
            self.assertEqual(len(formatted['DCE.p2701']), 1)
            self.assertEqual(formatted['DCE.p2701'][0][4], '1060')

    def test_control_server_skips_stream_projection_without_subscribers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with OfflineTargetRuntime(temp_dir) as runtime:
                def check(owner):
                    server = ControlServer(owner, f'online-trader-continuity-test-{os.getpid()}')
                    try:
                        self.assertFalse(owner._track_commit_views)
                        with patch.object(server, '_projection',
                                          side_effect=AssertionError('must not build stream projection')):
                            server._sync_streams()
                    finally:
                        server.listener.close()

                runtime.call_owner(check)

    def test_idle_round_skip_respects_open_positions_orders_timers_and_1s_cadence(self):
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter.contracts = ('DCE.p2701',)
        adapter.quotes = {}
        adapter.synchronized = True
        adapter._clock_resume_day = None
        adapter._health = {'reference_status': 'TRUSTED_UTC', 'reason': None}
        adapter._last_committed_ref_status = 'TRUSTED_UTC'
        adapter._last_committed_round_mono_ns = 10 * NS_PER_SECOND
        adapter.account = object()
        adapter.api = SimpleNamespace(get_order=lambda **_: {}, get_trade=lambda **_: {})
        state = {
            'synchronized': True,
            'sequence': 2,
            'trading_day': '2026-11-24',
            'running': True,
            'owner_fault_code': None,
            'clock_recovery_required': False,
            'orders': {},
            'symbols': {'DCE.p2701': {'position': None}},
        }
        adapter._last_idle_signature = adapter._idle_state_signature(state, '2026-11-24')
        sample = SimpleNamespace(monotonic=10 * NS_PER_SECOND + 100_000_000)

        self.assertTrue(adapter._can_skip_idle_round(
            state, sample, '2026-11-24', updated=False, timers=[], day_held=False))
        self.assertFalse(adapter._can_skip_idle_round(
            state, sample, '2026-11-24', updated=True, timers=[], day_held=False))
        self.assertFalse(adapter._can_skip_idle_round(
            state, sample, '2026-11-24', updated=False, timers=['timer-1'], day_held=False))

        later = SimpleNamespace(monotonic=11 * NS_PER_SECOND)
        self.assertFalse(adapter._can_skip_idle_round(
            state, later, '2026-11-24', updated=False, timers=[], day_held=False))

        with_position = dict(state, symbols={'DCE.p2701': {'position': {'position_id': 'pos-1'}}})
        self.assertFalse(adapter._can_skip_idle_round(
            with_position, sample, '2026-11-24', updated=False, timers=[], day_held=False))


class MarketHealthClassificationTests(unittest.TestCase):
    def test_quiet_symbol_is_isolated_while_all_active_silence_blocks_feed(self):
        quiet_health = evaluate_market_health(
            ['SHFE.ag2702', 'DCE.p2701'],
            {
                'SHFE.ag2702': _market_payload(active=True, age_quality='VALID', age_ns=200_000_000),
                'DCE.p2701': _market_payload(active=True, age_quality='VALID', age_ns=6_000_000_000),
            },
            clock_health={'reference_status': 'TRUSTED_UTC', 'reason': None, 'protection_ready': True},
            synchronized=True,
            connection_complete=True,
            sdk_pump_gap_ns=10_000_000,
            sdk_updated=True,
            max_quote_age_seconds=2,
        )
        self.assertEqual(quiet_health['status'], 'DEGRADED')
        self.assertFalse(quiet_health['feed_entry_blocked'])
        self.assertEqual(quiet_health['symbols']['SHFE.ag2702'], 'FRESH')
        self.assertEqual(quiet_health['symbols']['DCE.p2701'], 'QUIET')

        stalled_health = evaluate_market_health(
            ['SHFE.ag2702', 'DCE.p2701'],
            {
                'SHFE.ag2702': _market_payload(active=True, age_quality='VALID', silence_ns=6_000_000_000),
                'DCE.p2701': _market_payload(active=True, age_quality='VALID', silence_ns=6_500_000_000),
            },
            clock_health={'reference_status': 'TRUSTED_UTC', 'reason': None, 'protection_ready': True},
            synchronized=True,
            connection_complete=True,
            sdk_pump_gap_ns=10_000_000,
            sdk_updated=False,
            max_quote_age_seconds=2,
        )
        self.assertEqual(stalled_health['status'], 'FEED_SUSPECTED_STALLED')
        self.assertTrue(stalled_health['feed_entry_blocked'])

    def test_zero_max_quote_age_flags_disabled_but_keeps_feed_health_gate(self):
        health = evaluate_market_health(
            ['SHFE.ag2702', 'DCE.p2701'],
            {
                'SHFE.ag2702': _market_payload(active=True, age_quality='VALID', age_ns=1_000_000_000, silence_ns=5_500_000_000),
                'DCE.p2701': _market_payload(active=True, age_quality='VALID', age_ns=1_000_000_000, silence_ns=5_500_000_000),
            },
            clock_health={'reference_status': 'TRUSTED_UTC', 'reason': None, 'protection_ready': True},
            synchronized=True,
            connection_complete=True,
            sdk_pump_gap_ns=10_000_000,
            sdk_updated=False,
            max_quote_age_seconds=0,
        )
        self.assertTrue(health['age_filter_disabled'])
        self.assertTrue(health['feed_entry_blocked'])

        session = object.__new__(DsvSession)
        session.profile = single_contract_profile()
        spec = {'code': 'DCE.p2701'}
        state = {'orders': {}, 'symbols': {}, 'running': True}
        row = {'position': None, 'day_stop': False}
        observed = {
            'contract_id': 'DCE.p2701',
            'tick': {'last': '1060'},
            'long_qty': 0,
            'short_qty': 0,
        }
        frame = {
            'id': 'f-feed-block',
            'session_id': 'qa-session',
            'wall_time': '2026-11-24T01:10:00Z',
            'feed_entry_blocked': health['feed_entry_blocked'],
        }
        result = session._evaluate_symbol(state, row, observed, frame, spec)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['reason'], 'FEED_HEALTH_BLOCK')


class TqSdkUpgradeHookRegressionTests(unittest.TestCase):
    def test_pinned_sdk_version_and_wait_timeout_hook_match_installed_tqsdk(self):
        self.assertEqual(SDK_VERSION, '3.10.5')
        self.assertEqual(version('tqsdk'), SDK_VERSION)
        _pinned_sdk_available.cache_clear()
        self.assertTrue(_pinned_sdk_available())

        from tqsdk.api import TqApi

        self.assertIn('call_later', TqApi._run_until_task_done.__code__.co_names)
        self.assertIn('_set_wait_timeout', TqApi._run_until_task_done.__code__.co_names)
        self.assertTrue(callable(TqApi._set_wait_timeout))

        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        api = TqApi.__new__(TqApi)
        api._loop = loop
        api._set_wait_timeout = lambda *_: None
        api.wait_update = lambda deadline=None: (loop.call_later(0.05, api._set_wait_timeout), False)[1]
        updated, wait_mode = wait_update_slice(api, 0.1)
        self.assertFalse(updated)
        self.assertEqual(wait_mode, 'SDK_MONOTONIC_TIMEOUT')

    def test_pre_queue_send_and_cancel_refusal_hooks_match_real_tqsdk_frames(self):
        from tqsdk import TqSim
        from tqsdk.api import TqApi
        from tqsdk.auth import TqAuth
        from tqsdk.multiaccount import TqMultiAccount
        from tqsdk.objs import Order, Quote
        from tqsdk.risk_manager import TqRiskManager
        from tqsdk.risk_rule import TqRiskRule

        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        api = TqApi.__new__(TqApi)
        api._loop = loop
        acct = TqSim()
        api._account = TqMultiAccount([acct])
        quote = Quote(api)
        quote.ins_class = 'FUTURE'
        quote.price_tick = 2.0
        quote.volume_multiple = 10
        quote.exchange_id = 'DCE'
        quote.instrument_id = 'p2701'
        api._data = {'quotes': {'DCE.p2701': quote}}
        api._auth = TqAuth('u', 'p')
        api._ensure_symbol = lambda symbol: None
        api._generate_uuid = lambda: 'oid-1'
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter.api = api

        try:
            api.insert_order('DCE.p2701', 'BUY', 'OPEN', 1, advanced='BAD', account=acct)
            self.fail('expected validation refusal')
        except Exception as exc_arg:
            self.assertEqual(
                adapter._sdk_send_refused_before_queue(exc_arg),
                ('E_DSV_SDK_SEND_VALIDATION', 'SDK_PRE_SEND_REFUSED'),
            )

        try:
            api.insert_order('DCE.p2701', 'BUY', 'OPEN', 1, limit_price=1000, account=acct)
            self.fail('expected permission refusal')
        except Exception as exc_auth:
            self.assertEqual(
                adapter._sdk_send_refused_before_queue(exc_auth),
                ('E_DSV_SDK_SEND_VALIDATION', 'SDK_PERMISSION_REFUSED'),
            )

        api._auth._has_td_grants = lambda symbol: True

        class _BlockRule(TqRiskRule):
            def _could_insert_order(self, pack):
                return False, 'risk_open'

            def _could_cancel_order(self, pack):
                return False, 'risk_cancel'

        rm = TqRiskManager()
        rm.append(_BlockRule(api))
        api._risk_manager = rm
        try:
            api.insert_order('DCE.p2701', 'BUY', 'OPEN', 1, limit_price=1000, account=acct)
            self.fail('expected risk insert refusal')
        except Exception as exc_risk:
            self.assertEqual(
                adapter._sdk_send_refused_before_queue(exc_risk),
                ('E_DSV_SDK_SEND_RISK', 'SDK_RISK_RULE_REFUSED'),
            )

        order = Order(api)
        order.order_id = 'o1'
        api._data['trade'] = {acct._get_account_key(): {'orders': {'o1': order}}}
        try:
            api.cancel_order('o1', account=acct)
            self.fail('expected risk cancel refusal')
        except Exception as exc_cancel:
            self.assertTrue(adapter._sdk_cancel_refused_before_queue(exc_cancel))


if __name__ == '__main__':
    unittest.main()
