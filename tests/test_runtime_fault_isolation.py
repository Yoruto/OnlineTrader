"""Fault-injection and regression tests for P0 Runtime isolation, Workbench lifecycle, and DSV fact recovery."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.files import publish_replace_bytes, publish_runtime_file_bytes
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.desktop.runtime_client.client import RuntimeSubscriptionWorker
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.desktop.workbench_layout import _describe_owner_risk
from online_trader.diagnostics.catalog import lookup_diagnostic
from online_trader.diagnostics.reporter import _collect_bootstrap_context
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.errors import TraderError
from online_trader.runtime.desktop_bootstrap import ERROR_MESSAGES
from online_trader.runtime.owner_tick import execute_owner_tick
from qa.recording_port import RecordingPort


CONTRACT = 'DCE.p2701'
DAY = '2026-11-13'


def _round_frame(sequence, prices, *, short_qty=0, long_qty=0, facts=(), bid=None, ask=None,
                 sync_orders=None, standalone_external_model=False):
    wall = f'2026-11-13T01:10:{sequence:02d}Z'
    ticks = [{
        'native_time': wall,
        'candle_id': 'bar-1',
        'last': str(price),
        'day_low': '1020',
        'day_high': '1088',
        'bid1': str(price if bid is None else bid),
        'ask1': str(price if ask is None else ask),
        'tick_size': '1',
        'lower_limit': '900',
        'upper_limit': '1200',
    } for price in prices]
    market = {
        'contract_id': CONTRACT,
        'ticks': ticks,
        'opening_session': True,
        'session_open': '2026-11-13T01:00:00Z',
        'session_close': '2026-11-13T02:15:00Z',
        'long_qty': long_qty,
        'short_qty': short_qty,
        'available': '100000',
        'margin_per_lot': '1000',
    }
    if standalone_external_model:
        market['standalone_external_model'] = True
    payload = {
        'id': f'round-{sequence}',
        'sequence': sequence,
        'session_id': 'session-1',
        'wall_time': wall,
        'monotonic_ms': sequence * 1000,
        'synchronized': True,
        'market': [market],
        'native': list(facts),
    }
    if sync_orders is not None:
        payload['sync_orders'] = list(sync_orders)
    return payload


def _short_profile(order_volume=30):
    settings = load_confirmed_baseline()[0].to_settings()
    settings['symbols'] = [settings['symbols'][0] | {
        'open_offset': 40,
        'stop_offset': 8,
        'profit_offset': 20,
        'order_volume': order_volume,
    }]
    settings['trade'].update(
        no_chase_after_act=False,
        continuation_entry=False,
        protect_80_enabled=False,
        trailing_activate_ratio=0,
        passive_entry_first=False,
    )
    return DsvProfile.from_settings(settings)


class _TickStubOwner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.session = SimpleNamespace(_failed=False)
        self.state = {'synchronized': True, 'running': True, 'owner_fault_code': None}
        self.disconnect_calls = []
        self.fault_details = []
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
        self.state['running'] = True

    def disconnect_for_error(self, reason, *, disarm=False, fault_code=None):
        self.disconnect_calls.append((reason, disarm, fault_code))
        self.state['synchronized'] = False
        if disarm:
            self.state['running'] = False
            self.state['owner_fault_code'] = fault_code

    def set_owner_fault_detail(self, detail):
        self.fault_details.append(detail)

    def native_account_snapshot(self):
        return None


class _RecordingDiagnosticSink:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    def record(self, event_type, **kwargs):
        if self.fail:
            raise RuntimeError('diagnostic sink unavailable')
        self.events.append((event_type, kwargs))


class RuntimeWriteIsolationTests(unittest.TestCase):
    def test_publish_runtime_file_bytes_error_code_and_cleanup_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'target-owner-status.json'
            config_target = root / 'config.json'

            with patch('os.replace', side_effect=PermissionError(13, 'WinError 5 denied')):
                with self.assertRaises(TraderError) as rt_err:
                    publish_runtime_file_bytes(b'{}', target)
                self.assertEqual(rt_err.exception.code, 'E_RUNTIME_STATUS_WRITE')

                with self.assertRaises(TraderError) as cfg_err:
                    publish_replace_bytes(b'{}', config_target)
                self.assertEqual(cfg_err.exception.code, 'E_CONFIG_FILE')

            # Cleanup failure in finally must never mask original write exception.
            with (
                patch('os.replace', side_effect=PermissionError(13, 'sharing violation')),
                patch.object(Path, 'unlink', side_effect=PermissionError(13, 'unlink blocked')),
            ):
                with self.assertRaises(TraderError) as masked_err:
                    publish_runtime_file_bytes(b'{}', target)
                self.assertEqual(masked_err.exception.code, 'E_RUNTIME_STATUS_WRITE')

        self.assertEqual(lookup_diagnostic('E_RUNTIME_STATUS_WRITE').user_code, 'OT-SYS-9001')
        self.assertEqual(lookup_diagnostic('E_DSV_STORE').user_code, 'OT-DB-5001')
        self.assertIn('E_RUNTIME_STATUS_WRITE', ERROR_MESSAGES)
        self.assertIn('E_DSV_NATIVE_FACT', ERROR_MESSAGES)

    def test_status_write_failure_does_not_exit_or_alter_authority_and_flushes_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / 'target-owner-status.json'
            diag_path = root / 'target-owner-diagnostics.json'
            owner = _TickStubOwner([{'running': True}, {'running': True}])
            sink = _RecordingDiagnosticSink()
            binding = SimpleNamespace(boot_id='boot-iso-1', account_key='DEV:iso')
            loop_state = {
                'last_status': {'phase': 'READY', 'entry_authority': 'ARMED'},
                'last_history_capture': 0.0,
            }

            with patch(
                'online_trader.runtime.owner_tick.publish_runtime_file_bytes',
                side_effect=TraderError('E_RUNTIME_STATUS_WRITE', 'locked by scanner'),
            ):
                execute_owner_tick(
                    owner, binding=binding, current_trading_day=DAY, is_complete=lambda: True,
                    history_writer=None, status_path=status_path, diagnostics_path=diag_path,
                    loop_state=loop_state, sleep_fn=lambda _s: None, diagnostic_sink=sink,
                )

            self.assertTrue(owner.state['running'])
            self.assertEqual(owner.disconnect_calls, [])
            self.assertIn('pending_status_bytes', loop_state)
            warn_events = [e for e in sink.events if e[0] == 'RUNTIME_STATUS_WRITE_FAILED']
            self.assertEqual(len(warn_events), 1)
            self.assertEqual(warn_events[0][1]['error_code'], 'E_RUNTIME_STATUS_WRITE')

            # Backoff prevents immediate blocking retry; once retry window elapses, next tick flushes status.
            loop_state['status_retry_at'] = 0.0
            execute_owner_tick(
                owner, binding=binding, current_trading_day=DAY, is_complete=lambda: True,
                history_writer=None, status_path=status_path, diagnostics_path=diag_path,
                loop_state=loop_state, sleep_fn=lambda _s: None, diagnostic_sink=sink,
            )
            self.assertTrue(status_path.exists())
            self.assertNotIn('pending_status_bytes', loop_state)

    def test_diagnostics_write_failure_records_sink_first_and_even_sink_failure_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / 'target-owner-status.json'
            diag_path = root / 'target-owner-diagnostics.json'
            owner = _TickStubOwner([TraderError('E_DSV_NATIVE_FACT', 'fact mismatch on close')])
            sink = _RecordingDiagnosticSink()
            binding = SimpleNamespace(boot_id='boot-iso-2', account_key='DEV:iso')
            loop_state = {'last_status': None, 'last_history_capture': 0.0}

            with patch(
                'online_trader.runtime.owner_tick.publish_runtime_file_bytes',
                side_effect=TraderError('E_RUNTIME_STATUS_WRITE', 'file in use'),
            ):
                execute_owner_tick(
                    owner, binding=binding, current_trading_day=DAY, is_complete=lambda: True,
                    history_writer=None, status_path=status_path, diagnostics_path=diag_path,
                    loop_state=loop_state, sleep_fn=lambda _s: None, diagnostic_sink=sink,
                )

            tick_failed = [e for e in sink.events if e[0] == 'OWNER_TICK_FAILED']
            self.assertEqual(len(tick_failed), 1)
            self.assertEqual(tick_failed[0][1]['error_code'], 'E_DSV_NATIVE_FACT')
            self.assertIn('fact mismatch on close', tick_failed[0][1]['payload']['detail'])
            self.assertTrue(any(e[0] == 'RUNTIME_DIAGNOSTICS_WRITE_FAILED' for e in sink.events))
            self.assertEqual(owner.disconnect_calls, [('PERSISTENT_OWNER_FACT_CONFLICT', True, 'E_DSV_NATIVE_FACT')])

            # Sink raising alongside file write failure still must not escape execute_owner_tick.
            owner2 = _TickStubOwner([TraderError('E_DSV_NATIVE_FACT', 'fact mismatch 2')])
            failing_sink = _RecordingDiagnosticSink(fail=True)
            loop_state2 = {'last_status': None, 'last_history_capture': 0.0}
            with patch(
                'online_trader.runtime.owner_tick.publish_runtime_file_bytes',
                side_effect=OSError('disk busy'),
            ):
                execute_owner_tick(
                    owner2, binding=binding, current_trading_day=DAY, is_complete=lambda: True,
                    history_writer=None, status_path=status_path, diagnostics_path=diag_path,
                    loop_state=loop_state2, sleep_fn=lambda _s: None, diagnostic_sink=failing_sink,
                )
            self.assertEqual(owner2.disconnect_calls, [('PERSISTENT_OWNER_FACT_CONFLICT', True, 'E_DSV_NATIVE_FACT')])

    def test_reporter_collects_both_startup_and_runtime_failure_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / 'staging'
            staging.mkdir()
            runtime_root = root / 'runtime'
            run_dir = runtime_root / 'desktop-starts' / 'acc1' / 'run'
            run_dir.mkdir(parents=True)
            (run_dir / 'runtime-failure.json').write_bytes(canonical_bytes({
                'schema_version': 'DSV_RUNTIME_FAILURE/1',
                'status': 'RUNTIME_FAILED',
                'runtime_boot_id': 'boot-fail-1',
                'error_code': 'E_DSV_STORE',
                'detail': 'sqlite io fault',
            }))
            (run_dir / 'startup-failure.json').write_bytes(canonical_bytes({
                'status': 'FAILED',
                'error_code': 'E_TIMEOUT',
            }))
            files = []
            _collect_bootstrap_context(staging, runtime_root, {'runtime_boot_id': 'boot-fail-1'}, files)
            staged = [strict_loads(line.encode('utf-8')) for line in (staging / 'bootstrap' / 'startup-context.jsonl').read_text(encoding='utf-8').splitlines()]
            by_source = {item['source']: item for item in staged}
            self.assertIn('runtime-failure', by_source)
            self.assertIn('startup-failure', by_source)
            self.assertEqual(by_source['runtime-failure']['message'], 'sqlite io fault')


class WorkbenchLifecycleIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self, tmp_dir):
        settings = QSettings(str(Path(tmp_dir) / 'wb.ini'), QSettings.IniFormat)
        with (
            patch('online_trader.desktop.workbench.workbench_preferences', return_value=settings),
            patch('online_trader.desktop.runtime_client.client.RuntimeClientCoordinator.start'),
            patch('online_trader.desktop.runtime_client.client.RuntimeClientCoordinator.close'),
        ):
            win = WorkbenchWindow('Runtime.Fault.Test', runtime_view=True)
        self.addCleanup(lambda: (setattr(win, '_closing', True), win.close(), self.app.processEvents()))
        return win

    @staticmethod
    def _base_view():
        return {
            'view_schema': 'workbench/1',
            'account_key': 'DEV:fault-wb',
            'kernel_boot_id': 'boot-wb-1',
            'runtime_boot_id': 'boot-wb-1',
            'checkpoint_event_seq': '2',
            'entry_authority': 'DISARMED',
            'execution_scope': 'DISARMED',
            'connection': {
                'broker_connected': True,
                'synchronized': False,
                'fault': False,
                'recovery_required': False,
                'storage_failed': False,
                'clock_fault': None,
                'pending_transactions': 0,
                'account_positions': [
                    {'contract_id': 'DCE.pp2701', 'actual_long': 0, 'actual_short': 0,
                     'target_long': 0, 'target_short': 30, 'status': 'CONVERGENCE_PENDING'},
                ],
            },
            'configuration': {'rule_set_id': 'rule-1', 'config_revision': '1'},
            'markets': [],
            'cycles': [{
                'position_cycle_id': 'c1',
                'contract_id': 'DCE.pp2701',
                'direction': 'SHORT',
                'side': 'SHORT',
                'remaining_quantity': 30,
                'owned_quantity': 30,
                'target_quantity': 30,
            }],
            'orders': [],
            'trades': [],
            'blocks': [],
            'commands': [],
            'policies': [],
            'external_positions': [],
            'truncated': False,
        }

    def test_describe_owner_risk_does_not_infer_actual_positions_or_safe_flat_from_target(self):
        unsynced = self._base_view()
        lines = _describe_owner_risk(unsynced)
        self.assertIn('最后已知实仓 0 手', lines[0])
        self.assertIn('本地策略归属 30 手', lines[0])
        self.assertIn('同步不完整', lines[0])
        self.assertNotIn('实际空 30', lines[0])
        self.assertNotIn('已安全平仓', lines[0])

        synced_holding = self._base_view()
        synced_holding['connection']['synchronized'] = True
        lines2 = _describe_owner_risk(synced_holding)
        self.assertIn('不会继续执行自动平仓或保护', lines2[0])

        synced_flat = self._base_view()
        synced_flat['connection']['synchronized'] = True
        synced_flat['cycles'] = []
        lines3 = _describe_owner_risk(synced_flat)
        self.assertIn('策略目标不等同于实际持仓', lines3[0])

    def test_workbench_clears_stale_broker_recovery_and_notifies_once_on_owner_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            win = self._make_window(tmp)
            win._boot = 'boot-wb-1'
            win.view = self._base_view()
            win._stale = False
            win.snapshot_published_ns = time.monotonic_ns() - 60_000_000_000
            win.connection_recovery_source = 'broker'

            tray_statuses = []
            with (
                patch.object(win._tray, 'set_status', side_effect=tray_statuses.append),
                patch.object(win._sounds, 'observe_request_error') as sound_err,
            ):
                win._runtime_unavailable('E_RUNTIME_VIEW')
                self.assertIsNone(win.connection_recovery_source)

                win._runtime_lifecycle_changed({
                    'state': 'OWNER_CRASHED',
                    'runtime_boot_id': 'boot-wb-1',
                    'error_code': 'E_DSV_NATIVE_FACT',
                    'detail': 'timestamp conflict on close',
                })
                # Duplicate crash signal for same boot_id should not re-trigger alarm sound.
                win._runtime_lifecycle_changed({
                    'state': 'OWNER_CRASHED',
                    'runtime_boot_id': 'boot-wb-1',
                    'error_code': 'E_DSV_NATIVE_FACT',
                    'detail': 'timestamp conflict on close',
                })

            self.assertTrue(tray_statuses and all(s == 'error' for s in tray_statuses))
            sound_err.assert_called_once()
            self.assertEqual(win.dashboard.home_state.text(), '后台已异常退出')
            self.assertIn('Runtime 后台服务已异常退出', win.dashboard.home_pending.text())
            self.assertNotIn('Runtime 仍独立运行', win.dashboard.home_pending.text())
            self.assertFalse(win.command_buttons['arm'].isEnabled())
            self.assertFalse(win.command_buttons['reconcile'].isEnabled())
            self.assertFalse(win.pause_button.isEnabled())

            # Unresponsive lock-held state must say status unknown, never running normally.
            win._runtime_lifecycle_changed({
                'state': 'OWNER_UNRESPONSIVE',
                'runtime_boot_id': 'boot-wb-1',
                'owner_locked': True,
            })
            self.assertEqual(win.dashboard.home_state.text(), '后台状态未知')
            self.assertIn('无法确认后台 Runtime 状态', win.dashboard.home_pending.text())

    def test_executor_log_surfaces_diagnostic_detail_and_runtime_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / 'DEV_fault-wb' / 'run'
            run_dir.mkdir(parents=True)
            (run_dir / 'target-owner-status.json').write_bytes(canonical_bytes({
                'runtime_boot_id': 'boot-wb-1',
                'account_key': 'DEV:fault-wb',
                'phase': 'NEEDS_REVIEW',
                'entry_authority': 'DISARMED',
                'error_code': 'E_DSV_NATIVE_FACT',
                'detail': 'status detail visible',
                'observed_at_utc': '2026-11-25T01:34:46Z',
            }))
            (run_dir / 'target-owner-diagnostics.json').write_bytes(canonical_bytes({
                'runtime_boot_id': 'boot-wb-1',
                'account_key': 'DEV:fault-wb',
                'diagnostic': 'OWNER_TICK_FAILED',
                'authority_effect': 'DISARMED',
                'error_code': 'E_DSV_NATIVE_FACT',
                'detail': 'native fact mismatch detail',
                'observed_at_utc': '2026-11-25T01:34:46Z',
            }))
            (run_dir / 'runtime-failure.json').write_bytes(canonical_bytes({
                'schema_version': 'DSV_RUNTIME_FAILURE/1',
                'runtime_boot_id': 'boot-wb-1',
                'account_key': 'DEV:fault-wb',
                'error_code': 'E_DSV_STORE',
                'detail': 'ledger write failed',
                'observed_at_utc': '2026-11-25T01:34:56Z',
            }))
            win = self._make_window(tmp)
            win.view = self._base_view() | {'run_directory': str(run_dir)}
            win.dashboard.log_binding = (('DEV:fault-wb', 'boot-wb-1'), str(run_dir), None, (), None)
            lines = win.dashboard.executor_log(run_dir)
            joined = '\n'.join(line.text for line in lines)
            self.assertIn('status detail visible', joined)
            self.assertIn('native fact mismatch detail', joined)
            self.assertIn('ledger write failed', joined)

    def test_subscription_worker_detects_owner_crash_stops_dead_pipe_and_discovers_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'DEV_acct' / 'run'
            run_dir.mkdir(parents=True)
            (run_dir / 'runtime-failure.json').write_bytes(canonical_bytes({
                'schema_version': 'DSV_RUNTIME_FAILURE/1',
                'status': 'RUNTIME_FAILED',
                'runtime_boot_id': 'boot-1',
                'account_key': 'DEV:acct',
                'error_code': 'E_DSV_NATIVE_FACT',
                'detail': 'injected crash',
                'observed_at_utc': '2026-11-25T01:34:56Z',
            }))
            subscribe_calls = 0
            state_lock = threading.Lock()
            phase = {'owner_alive': True, 'new_instance': None}
            crashed_emitted = threading.Event()
            replaced_emitted = threading.Event()

            baseline_body = {
                'base': {'records': []},
                'view': {
                    'account_key': 'DEV:acct',
                    'kernel_boot_id': 'boot-1',
                    'runtime_boot_id': 'boot-1',
                    'configuration': {'run_directory': str(run_dir)},
                },
            }
            baseline_cursor = {'generation': 'g1', 'sequence': '1', 'digest': content_hash(baseline_body)}

            class FakeViewClient:
                def __init__(self, _pipe):
                    self.closed = False

                def subscribe(self, _cursor=None):
                    nonlocal subscribe_calls
                    with state_lock:
                        subscribe_calls += 1
                        call_no = subscribe_calls
                    if call_no == 1:
                        yield {
                            'kind': 'BASELINE',
                            'cursor': baseline_cursor,
                            'published_ns': str(time.monotonic_ns()),
                            'body': baseline_body,
                        }
                        phase['owner_alive'] = False
                        raise OSError('named pipe broken')
                    raise AssertionError('Worker must not retry dead pipe after OWNER_CRASHED')

                def close(self):
                    self.closed = True

            def fake_owner_exists():
                return phase['owner_alive']

            def fake_read_instance():
                return phase['new_instance']

            verdicts = []
            conn_states = []

            with (
                patch('online_trader.desktop.runtime_client.client.RuntimeViewClient', FakeViewClient),
                patch('online_trader.desktop.runtime_client.client.runtime_owner_exists', side_effect=fake_owner_exists),
                patch('online_trader.desktop.runtime_client.client.read_runtime_instance', side_effect=fake_read_instance),
            ):
                worker = RuntimeSubscriptionWorker(r'\\.\pipe\old-pipe')
                worker.lifecycle_changed.connect(
                    lambda payload: (
                        verdicts.append(payload),
                        crashed_emitted.set() if payload.get('state') == 'OWNER_CRASHED' else None,
                        replaced_emitted.set() if payload.get('state') == 'OWNER_REPLACED' else None,
                    ),
                    Qt.DirectConnection,
                )
                worker.connection_changed.connect(lambda st, code: conn_states.append((st, code)), Qt.DirectConnection)
                worker.start()
                self.assertTrue(crashed_emitted.wait(5), f'verdicts={verdicts}')
                time.sleep(0.3)
                self.assertEqual(subscribe_calls, 1)

                phase['new_instance'] = {
                    'schema_version': 'RUNTIME_INSTANCE/1',
                    'instance_id': 'inst-2',
                    'pid': 9999,
                    'pipe': r'\\.\pipe\new-pipe',
                    'account_key': 'DEV:acct',
                    'runtime_root': str(Path(tmp)),
                }
                phase['owner_alive'] = True
                self.assertTrue(replaced_emitted.wait(5), f'verdicts={verdicts}')
                worker.shutdown()
                worker.wait(2000)

            self.assertIn(('RESYNC_REQUIRED', 'E_DSV_NATIVE_FACT'), conn_states)
            self.assertEqual(verdicts[-1]['state'], 'OWNER_REPLACED')
            self.assertEqual(verdicts[-1]['pipe'], r'\\.\pipe\new-pipe')

    def test_subscription_worker_ignores_mismatched_boot_id_crash_file_and_reports_unresponsive(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'DEV_acct' / 'run'
            run_dir.mkdir(parents=True)
            (run_dir / 'runtime-failure.json').write_bytes(canonical_bytes({
                'schema_version': 'DSV_RUNTIME_FAILURE/1',
                'runtime_boot_id': 'boot-OLD',
                'account_key': 'DEV:acct',
                'error_code': 'E_DSV_STORE',
                'detail': 'stale error from old boot',
            }))

            baseline_body = {
                'base': {'records': []},
                'view': {
                    'account_key': 'DEV:acct',
                    'kernel_boot_id': 'boot-NEW',
                    'runtime_boot_id': 'boot-NEW',
                    'configuration': {'run_directory': str(run_dir)},
                },
            }
            baseline_cursor = {'generation': 'g1', 'sequence': '1', 'digest': content_hash(baseline_body)}

            class FakeViewClient:
                def __init__(self, _pipe):
                    self.stop_event = threading.Event()

                def subscribe(self, _cursor=None):
                    yield {
                        'kind': 'BASELINE',
                        'cursor': baseline_cursor,
                        'published_ns': str(time.monotonic_ns()),
                        'body': baseline_body,
                    }
                    yield {'kind': 'UNAVAILABLE'}
                    self.stop_event.wait(5)
                    raise OSError('closed')

                def close(self):
                    self.stop_event.set()

            verdicts = []
            got_unresponsive = threading.Event()
            with (
                patch('online_trader.desktop.runtime_client.client.RuntimeViewClient', FakeViewClient),
                patch('online_trader.desktop.runtime_client.client.runtime_owner_exists', return_value=True),
                patch('online_trader.desktop.runtime_client.client.read_runtime_instance', return_value=None),
            ):
                worker = RuntimeSubscriptionWorker(r'\\.\pipe\boot-check')
                worker.lifecycle_changed.connect(
                    lambda payload: (
                        verdicts.append(payload),
                        got_unresponsive.set() if payload.get('state') == 'OWNER_UNRESPONSIVE' else None,
                    ),
                    Qt.DirectConnection,
                )
                worker.start()
                self.assertTrue(got_unresponsive.wait(5), f'verdicts={verdicts}')
                worker.shutdown()
                worker.wait(2000)

            self.assertEqual(verdicts[0]['state'], 'OWNER_UNRESPONSIVE')
            self.assertTrue(verdicts[0]['owner_locked'])
            self.assertEqual(verdicts[0]['runtime_boot_id'], 'boot-NEW')


@unittest.skipUnless(sys.platform == 'win32', 'Uses the Windows account owner lock')
class DsvFactAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.port = RecordingPort()
        self.session = self.enterContext(DsvSession(
            Path(self.directory) / 'facts.db',
            account_key='DEV:dsv-fault-recovery',
            provider='OFFLINE',
            trading_day=DAY,
            profile=_short_profile(30),
            port=self.port,
        ))
        self.session.command({'id': 'start', 'action': 'START'})

    def _open_short_30(self):
        open_eff, = self.session.apply_round(_round_frame(1, [1055, 1048]))['effects']
        open_child = open_eff['child_order_id']
        t0 = 1_763_000_100_000_000_000
        self.session.apply_round(_round_frame(2, [1048], short_qty=30, facts=[
            {'kind': 'ORDER', 'child_order_id': open_child, 'native_order_id': open_child + '|nat',
             'status': 'FILLED', 'filled_qty': 30, 'source_time_ns': str(t0)},
            {'kind': 'TRADE', 'child_order_id': open_child, 'native_order_id': open_child + '|nat',
             'native_trade_id': 'open-fill-30', 'quantity': 30, 'price': '1048', 'source_time_ns': str(t0 + 1_000_000_000)},
        ]))
        return t0

    def test_adjusted_insert_time_within_60s_skew_accepts_subsequent_trade_and_preserves_bounds(self):
        t0 = self._open_short_30()
        # Trigger stop-loss exit at 1056 (short opened at 1048, stop_offset=8).
        close_eff, = self.session.apply_round(_round_frame(3, [1056], short_qty=30))['effects']
        close_child = close_eff['child_order_id']
        native_id = close_child + '|nat'

        # First OnRtnOrder from front arrives with t_front = t0 + 20s; exchange ack adjusts InsertTime back 15s (within 60s).
        t_front = t0 + 20_000_000_000
        t_exch = t_front - 15_000_000_000
        self.session.apply_round(_round_frame(4, [1056], short_qty=30, facts=[
            {'kind': 'ORDER', 'child_order_id': close_child, 'native_order_id': native_id,
             'status': 'ALIVE', 'filled_qty': 0, 'source_time_ns': str(t_front)},
        ]))
        self.session.apply_round(_round_frame(5, [1056], short_qty=30, facts=[
            {'kind': 'ORDER', 'child_order_id': close_child, 'native_order_id': native_id,
             'status': 'FILLED', 'filled_qty': 30, 'source_time_ns': str(t_exch)},
            {'kind': 'TRADE', 'child_order_id': close_child, 'native_order_id': native_id,
             'native_trade_id': 'close-fill-30', 'quantity': 30, 'price': '1056',
             'source_time_ns': str(t_exch + 1_000_000_000)},
        ]))
        pos = self.session.snapshot()['symbols'][CONTRACT]['position']
        self.assertIsNotNone(pos)
        self.assertEqual(pos['owned'], 0)
        self.assertEqual(pos['fills'][-1]['native_trade_id'], 'close-fill-30')
        # After the 5-second protection sync barrier with native short_qty=0, position settles cleanly.
        self.session.apply_round({
            'id': 'round-6',
            'sequence': 6,
            'session_id': 'session-1',
            'wall_time': '2026-11-13T01:10:12Z',
            'monotonic_ms': 12_000,
            'synchronized': True,
            'market': [{
                'contract_id': CONTRACT,
                'ticks': [{
                    'native_time': '2026-11-13T01:10:12Z', 'candle_id': 'bar-1',
                    'last': '1048', 'day_low': '1020', 'day_high': '1088',
                    'bid1': '1048', 'ask1': '1048', 'tick_size': '1',
                    'lower_limit': '900', 'upper_limit': '1200',
                }],
                'opening_session': True,
                'session_open': '2026-11-13T01:00:00Z',
                'session_close': '2026-11-13T02:15:00Z',
                'long_qty': 0, 'short_qty': 0,
                'available': '100000', 'margin_per_lot': '1000',
            }],
            'native': [],
        })
        self.assertIsNone(self.session.snapshot()['symbols'][CONTRACT]['position'])

    def test_gt_60s_order_skew_and_pre_order_trade_still_fail_closed(self):
        t0 = self._open_short_30()
        close_eff, = self.session.apply_round(_round_frame(3, [1056], short_qty=30))['effects']
        close_child = close_eff['child_order_id']
        native_id = close_child + '|nat'
        self.session.apply_round(_round_frame(4, [1056], short_qty=30, facts=[
            {'kind': 'ORDER', 'child_order_id': close_child, 'native_order_id': native_id,
             'status': 'ALIVE', 'filled_qty': 0, 'source_time_ns': str(t0 + 10_000_000_000)},
        ]))
        with self.assertRaises(TraderError) as skew_err:
            self.session.apply_round(_round_frame(5, [1056], short_qty=30, facts=[
                {'kind': 'ORDER', 'child_order_id': close_child, 'native_order_id': native_id,
                 'status': 'ALIVE', 'filled_qty': 0, 'source_time_ns': str(t0 + 75_000_000_000)},
            ]))
        self.assertEqual(skew_err.exception.code, 'E_DSV_NATIVE_FACT')

        # Previous round rolled back sequence 5, so next round continues at sequence 5.
        with self.assertRaises(TraderError) as trade_err:
            self.session.apply_round(_round_frame(5, [1056], short_qty=30, facts=[
                {'kind': 'TRADE', 'child_order_id': close_child, 'native_order_id': native_id,
                 'native_trade_id': 'early-trade', 'quantity': 30, 'price': '1056',
                 'source_time_ns': str(t0)},  # 10s earlier than anchor > 5s limit
            ]))
        self.assertEqual(trade_err.exception.code, 'E_DSV_NATIVE_FACT')
        self.assertIn(CONTRACT, str(trade_err.exception))

    def test_post_rollback_close_fill_reconciles_without_duplicate_close_or_reopen(self):
        self._open_short_30()
        close_eff, = self.session.apply_round(_round_frame(3, [1056], short_qty=30))['effects']
        close_child = close_eff['child_order_id']
        native_id = close_child + '|nat'
        effects_before = len(self.port.orders)

        # Simulate conflicting packet rolling back apply_round on the close round.
        with self.assertRaises(TraderError) as err:
            self.session.apply_round(_round_frame(4, [1056], short_qty=0, facts=[
                {'kind': 'ORDER', 'child_order_id': close_child, 'native_order_id': native_id,
                 'status': 'REJECTED', 'filled_qty': 30},
            ]))
        self.assertEqual(err.exception.code, 'E_DSV_NATIVE_FACT')
        # Position remained 30 in rolled-back state.
        self.assertEqual(self.session.snapshot()['symbols'][CONTRACT]['position']['owned'], 30)

        # Persistent disarm marks unsynchronized + fault_code.
        self.session.disconnect('PERSISTENT_DISARM', disarm=True, fault_code='E_DSV_NATIVE_FACT')

        # Next round continues at sequence 4 (rolled back round did not advance sequence)
        # and recovers with trusted broker facts (filled close, actual short_qty=0).
        recovery = self.session.apply_round(_round_frame(
            4, [1048], short_qty=0,
            facts=[
                {'kind': 'ORDER', 'child_order_id': close_child, 'native_order_id': native_id,
                 'status': 'FILLED', 'filled_qty': 30},
                {'kind': 'TRADE', 'child_order_id': close_child, 'native_order_id': native_id,
                 'native_trade_id': 'close-fill-30', 'quantity': 30, 'price': '1056'},
            ],
            sync_orders=[close_child],
        ))
        self.assertEqual(recovery['effects'], [])
        snap = self.session.snapshot()
        self.assertTrue(snap['synchronized'])
        self.assertEqual(snap['symbols'][CONTRACT]['position']['owned'], 0)
        self.assertEqual(len(self.port.orders), effects_before)

        # User triggers RECONCILE -> succeeds cleanly, stays disarmed, no re-open.
        self.session.command({'id': 'reconcile-1', 'action': 'RECONCILE'})
        self.assertIsNone(self.session.snapshot()['owner_fault_code'])
        self.assertEqual(len(self.port.orders), effects_before)

    def test_undercovered_position_without_fill_forbids_forged_reconcile(self):
        self._open_short_30()
        close_eff, = self.session.apply_round(_round_frame(3, [1056], short_qty=30))['effects']
        close_child = close_eff['child_order_id']
        self.session.disconnect('TRANSIENT_DISCONNECT', disarm=True, fault_code='E_DSV_NATIVE_FACT')

        # Broker reports short_qty=0 in standalone mode WITHOUT supplying any closing fill!
        self.session.apply_round(_round_frame(
            4, [1056], short_qty=0,
            facts=[
                {'kind': 'ORDER', 'child_order_id': close_child, 'native_order_id': close_child + '|nat',
                 'status': 'ALIVE', 'filled_qty': 0},
            ],
            sync_orders=[close_child],
            standalone_external_model=True,
        ))
        snap = self.session.snapshot()
        self.assertEqual(snap['symbols'][CONTRACT]['ownership_conflict'], 'POSITION_UNDERCOVERED')
        self.assertEqual(snap['symbols'][CONTRACT]['position']['owned'], 30)
        with self.assertRaises(TraderError) as rec_err:
            self.session.command({'id': 'reconcile-undercovered', 'action': 'RECONCILE'})
        self.assertEqual(rec_err.exception.code, 'E_DSV_OWNERSHIP')


if __name__ == '__main__':
    unittest.main()
