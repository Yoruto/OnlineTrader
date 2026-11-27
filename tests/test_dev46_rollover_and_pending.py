"""Targeted regression tests for dev46 cross-day rollover and pending command recovery."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.desktop.runtime_client.client import CommandBus
from online_trader.desktop.runtime_client.state import (
    ClientStateStore,
    PendingCommandStatus,
)
from online_trader.desktop.workbench import WorkbenchWindow
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.dsv_runtime.standalone_projection import workbench_view
from online_trader.dsv_storage.journal import Journal
from online_trader.errors import TraderError
from online_trader.kernel.server import ControlServer
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.owner_tick import classify_owner_tick_error
from online_trader.runtime.startup_metadata import SDK_VERSION, build_metadata
from online_trader.runtime.target_owner_startup import resolve_active_trading_day
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.scheduling.clock import ClockContinuity, ClockSample
from qa.recording_port import RecordingPort


ACCOUNT = 'SIM:KQ:0123456789abcdef'
CONTRACT = 'DCE.p2701'
DAY1 = '2026-11-12'
DAY2 = '2026-11-13'
BEIJING = timezone(timedelta(hours=8))


def _profile(contracts=(CONTRACT,)):
    settings = load_confirmed_baseline()[0].to_settings()
    base = settings['symbols'][0]
    settings['symbols'] = [
        base | {
            'code': code,
            'open_offset': 40,
            'stop_offset': 8,
            'profit_offset': 20,
            'order_volume': 2,
        }
        for code in contracts
    ]
    return DsvProfile.from_settings(settings)


def _quote(code=CONTRACT, *, expired=False):
    return SimpleNamespace(
        instrument_id=code,
        expired=expired,
        ins_class='FUTURE',
        volume_multiple=10,
        trading_time=SimpleNamespace(
            day=[['09:00:00', '10:15:00'], ['10:30:00', '11:30:00'], ['13:30:00', '15:00:00']],
            night=[['21:00:00', '23:00:00']],
        ),
        last_price=8000.0,
        highest=8020.0,
        lowest=7980.0,
        bid_price1=7998.0,
        ask_price1=8002.0,
        price_tick=2.0,
        lower_limit=7500.0,
        upper_limit=8500.0,
        margin=8000.0,
        datetime='2026-11-12 14:59:59.000000',
    )


def _calendar_rows(start: date, end: date):
    rows = []
    cur = start
    while cur <= end:
        rows.append({'date': cur.isoformat(), 'trading': cur.weekday() < 5})
        cur += timedelta(days=1)
    return rows


def _make_day1_scope(tmpdir: Path, profile: DsvProfile):
    start_dt = datetime(2026, 11, 12, 9, 30, 0, tzinfo=BEIJING)
    dates = {
        date.fromisoformat(row['date']): row['trading']
        for row in _calendar_rows(date(2026, 10, 20), date(2026, 12, 5))
    }
    meta = build_metadata(dates, {CONTRACT: _quote(CONTRACT)}, [CONTRACT], now=start_dt, provider='TQKQ')
    calendar = TradingCalendar.from_record(meta['calendar'])
    ledger = tmpdir / 'ledger.db'
    grant = DsvSimulationGrant.parse(canonical_bytes({
        'schema_version': 'KQ_DSV_DESKTOP/1',
        'grant_id': 'desktop-dev46-test',
        'account_fingerprint': '0123456789abcdef',
        'ledger_path': str(ledger.resolve()),
        'contracts': [CONTRACT],
        'trading_day': DAY1,
        'trading_days': [DAY1],
        'resume_entries_on_day_change': False,
        'not_before_utc': '2026-11-12T01:30:00Z',
        'not_after_utc': '2026-11-12T07:00:00Z',
        'max_order_quantity': 5,
        'daily_targets_per_contract': 10,
        'risk_budget': '100000',
        'profile_hash': profile.content_hash,
        'calendar_hash': calendar.content_hash,
        'authorization_reference': 'DESKTOP_CONFIRM:test',
    }))
    return ledger, calendar, grant


class _RecordingSink:
    def __init__(self):
        self.events = []

    def record(self, event_type, **kwargs):
        self.events.append({'event_type': event_type, **kwargs})


class _FakeConnection:
    def __init__(self, grant, calendar):
        self.grant = grant
        self.calendar = calendar
        self.updated = []

    def update_scope(self, grant, calendar):
        self.grant = grant
        self.calendar = calendar
        self.updated.append((grant.content_hash, calendar.content_hash))


def _build_rollover_harness(test_case: unittest.TestCase, *, name: str, calendar_fn=None):
    tmp = tempfile.TemporaryDirectory()
    test_case.addCleanup(tmp.cleanup)
    profile = _profile()
    ledger, calendar, grant = _make_day1_scope(Path(tmp.name), profile)
    port = RecordingPort()
    session = DsvSession(
        ledger,
        account_key=f'DEV:dev46-{name}',
        provider='OFFLINE',
        trading_day=DAY1,
        profile=profile,
        port=port,
    )
    test_case.addCleanup(session.close)
    adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
    adapter.owner_mode = 'STANDALONE'
    adapter.account_key = ACCOUNT
    adapter.provider = 'TQKQ'
    adapter.profile = profile
    adapter.contracts = (CONTRACT,)
    adapter.calendar = calendar
    adapter.periods = {row['contract_id']: row['sessions'] for row in calendar.to_record()['contracts']}
    adapter.trading_days = [DAY1]
    adapter.grant = grant
    adapter.quotes = {CONTRACT: _quote(CONTRACT)}
    adapter.connection = _FakeConnection(grant, calendar)
    adapter._schedule_display_cache = {'cached': True}
    adapter._last_idle_signature = 'sig'
    adapter._foreign_trades = set()
    adapter._last_calendar_refresh_attempt_mono = None
    adapter._last_calendar_refresh_diag = None
    sink = _RecordingSink()
    from online_trader.dsv_runtime.order_diagnostics import OrderDiagnosticTracker
    adapter._diagnostics = OrderDiagnosticTracker(account_key=ACCOUNT, evidence_sink=sink)
    adapter.api = SimpleNamespace(
        get_quote=lambda code: adapter.quotes[code],
        get_trading_calendar=calendar_fn or _calendar_rows,
    )
    return session, adapter, sink


class StandaloneCalendarRolloverTests(unittest.TestCase):
    def test_1_expired_grant_rollover_forces_disarm_and_preserves_position(self):
        """Old grant just expired (before runner set scope_expired_at): rollover disarms & keeps position."""
        session, adapter, _sink = _build_rollover_harness(self, name='t1')
        session._state['running'] = True
        session._state['symbols'][CONTRACT]['position'] = {
            'id': 'pos-1',
            'contract_id': CONTRACT,
            'direction': 'LONG',
            'owned': 2,
            'entry_filled': 2,
            'exit_target': 2,
            'entry_stopped': False,
            'preserve_open_on_stop': True,
            'children': [],
            'fills': [],
        }
        self.assertIsNone(session._state.get('scope_expired_at'))

        sample = ClockSample('2026-11-12T07:00:05Z', '1000000000')
        refreshed = adapter._maybe_refresh_standalone_calendar(session, sample)

        self.assertTrue(refreshed)
        self.assertIn(DAY2, adapter.trading_days)
        self.assertIn(DAY2, adapter.grant.trading_days)
        self.assertEqual(len(adapter.connection.updated), 1)
        snap = session.snapshot()
        self.assertFalse(snap['running'])
        pos = snap['symbols'][CONTRACT]['position']
        self.assertIsNotNone(pos)
        self.assertEqual(pos['owned'], 2)
        self.assertTrue(pos['entry_stopped'])
        self.assertFalse(pos['preserve_open_on_stop'])

    def test_2_sdk_calendar_generic_exception_does_not_crash_and_emits_rate_limited_diag(self):
        """Generic Exception from TqSdk get_trading_calendar keeps Runtime alive & emits sanitized diag."""
        def _boom(_start, _end):
            raise Exception('secret-token-should-not-leak calendar range exceeded')

        session, adapter, sink = _build_rollover_harness(self, name='t2', calendar_fn=_boom)
        old_cal_hash = adapter.calendar.content_hash
        old_grant_hash = adapter.grant.content_hash
        sample = ClockSample('2026-11-12T07:00:05Z', '1000000000')

        self.assertFalse(adapter._maybe_refresh_standalone_calendar(session, sample))
        self.assertEqual(adapter.calendar.content_hash, old_cal_hash)
        self.assertEqual(adapter.grant.content_hash, old_grant_hash)
        self.assertEqual(len(sink.events), 1)
        event = sink.events[0]
        self.assertEqual(event['event_type'], 'CALENDAR_ROLLOVER_FAILED')
        self.assertEqual(event['payload']['stage'], 'SDK_CALENDAR_FETCH')
        self.assertEqual(event['payload']['category'], 'Exception')
        self.assertNotIn('secret-token-should-not-leak', str(event))

        # Even after the 30s retry backoff passes, identical failure diag is rate-limited for 300s.
        adapter._last_calendar_refresh_attempt_mono = time.monotonic() - 35.0
        self.assertFalse(adapter._maybe_refresh_standalone_calendar(session, sample))
        self.assertEqual(len(sink.events), 1)

    def test_3_failed_rollover_then_subsequent_success_recovers_without_auto_arming(self):
        """First rollover fails -> scope_expired_at set; next rollover succeeds -> clears expiry, stays DISARMED."""
        attempts = {'n': 0}

        def _flaky(start, end):
            attempts['n'] += 1
            if attempts['n'] == 1:
                raise Exception('temporary calendar failure')
            return _calendar_rows(start, end)

        session, adapter, _sink = _build_rollover_harness(self, name='t3', calendar_fn=_flaky)
        session._state['scope_expired_at'] = '2026-11-12T07:00:02Z'
        session._state['running'] = False
        sample = ClockSample('2026-11-12T07:00:05Z', '1000000000')

        self.assertFalse(adapter._maybe_refresh_standalone_calendar(session, sample))
        self.assertEqual(session.snapshot().get('scope_expired_at'), '2026-11-12T07:00:02Z')

        adapter._last_calendar_refresh_attempt_mono = time.monotonic() - 35.0
        self.assertTrue(adapter._maybe_refresh_standalone_calendar(session, sample))
        snap = session.snapshot()
        self.assertIsNone(snap.get('scope_expired_at'))
        self.assertFalse(snap['running'])
        self.assertIn(DAY2, adapter.trading_days)

    def test_4_day_rollover_preserves_open_position_and_reconciles_unfilled_order(self):
        """Cross-day rollover resets daily stop, keeps position protection, and reconciles unfilled open order."""
        session, adapter, _sink = _build_rollover_harness(self, name='t4')
        session._state['running'] = True
        session._state['symbols'][CONTRACT]['day_stop'] = True
        session._state['symbols'][CONTRACT]['day_stop_reason'] = 'NET_LOSS_STOP'
        session._state['symbols'][CONTRACT]['net_loss'] = '8'
        session._state['symbols'][CONTRACT]['position'] = {
            'id': 'pos-overnight',
            'contract_id': CONTRACT,
            'direction': 'LONG',
            'owned': 1,
            'entry_filled': 1,
            'exit_target': 1,
            'entry_stopped': False,
            'preserve_open_on_stop': True,
            'children': ['child-open-1'],
            'fills': [],
        }
        session._state['orders']['child-open-1'] = {
            'child_order_id': 'child-open-1',
            'intent_id': 'intent-1',
            'attempt_id': 'attempt-1',
            'position_id': 'pos-overnight',
            'contract_id': CONTRACT,
            'direction': 'BUY',
            'effect': 'OPEN',
            'native_offset': 'OPEN',
            'native_trading_day': DAY1,
            'original_qty': 1,
            'filled_qty': 0,
            'reported_fill_qty': 0,
            'limit_price': '8000',
            'send_state': 'NATIVE_SEEN',
            'broker_state': 'ALIVE',
            'cancel_status': 'NONE',
            'terminal_confirmed': False,
            'native_id': 'native-open-1',
        }

        session._roll_day(session._state, DAY2)
        row = session._state['symbols'][CONTRACT]
        self.assertEqual(session._state['trading_day'], DAY2)
        self.assertFalse(row['day_stop'])
        self.assertEqual(row['net_loss'], '0')
        self.assertIsNotNone(row['position'])
        self.assertEqual(row['position']['owned'], 1)
        self.assertTrue(row['position']['entry_stopped'])
        self.assertFalse(row['position']['preserve_open_on_stop'])
        self.assertIn('child-open-1', session._state['orders'])
        self.assertFalse(session._state['synchronized'])

    def test_5_sqlite_failure_during_rollover_propagates_fail_stop_and_blocks_trading(self):
        """SQLite write error during rollover must propagate E_DSV_STORE (FAIL_STOP) and fail session."""
        session, adapter, _sink = _build_rollover_harness(self, name='t5')
        session._state['running'] = True
        old_cal_hash = adapter.calendar.content_hash
        old_grant_hash = adapter.grant.content_hash
        sample = ClockSample('2026-11-12T07:00:05Z', '1000000000')

        def _fail_save(_state):
            raise sqlite3.OperationalError('disk I/O error')

        with patch.object(session._journal, 'save', side_effect=_fail_save):
            with self.assertRaises(TraderError) as caught:
                adapter._maybe_refresh_standalone_calendar(session, sample)

        self.assertEqual(caught.exception.code, 'E_DSV_STORE')
        self.assertEqual(classify_owner_tick_error(caught.exception), 'FAIL_STOP')
        self.assertTrue(session._failed)
        self.assertEqual(adapter.calendar.content_hash, old_cal_hash)
        self.assertEqual(adapter.grant.content_hash, old_grant_hash)
        with self.assertRaises(TraderError):
            session.snapshot()


class _CommandsStub:
    def __init__(self):
        self.requests = []

    def enqueue(self, request):
        self.requests.append(request)


class _CoordinatorStub(QObject):
    command_result = Signal(object)
    command_failed = Signal(object)

    def __init__(self):
        super().__init__()
        self.commands = _CommandsStub()


def _runtime_state():
    return {
        'view': {
            'view_schema': 'workbench/1',
            'account_key': ACCOUNT,
            'kernel_boot_id': 'boot-dev46',
            'checkpoint_event_seq': '1',
            'entry_authority': 'DISARMED',
            'execution_scope': 'ONLINE_SIMULATION',
            'configuration': {'config_revision': '1', 'rule_set_id': 'DSV_TARGET_EXECUTION'},
            'connection': {'broker_connected': True, 'synchronized': True, 'fault': False},
            'capabilities': {
                'revision': '1',
                'operations': [
                    {'operation': 'MANUAL_CLOSE', 'scope_type': 'CYCLE', 'allowed': True, 'reason': None},
                    {'operation': 'SAVE_DSV_PARAMETERS', 'scope_type': 'ACCOUNT', 'allowed': True, 'reason': None},
                ],
            },
            'truncated': False,
        },
    }


def _cursor():
    return {
        'view_schema': 'workbench/1',
        'account_key': ACCOUNT,
        'kernel_boot_id': 'boot-dev46',
        'view_id': 'view-1',
        'checkpoint_event_seq': '1',
    }


class PendingCommandRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _new_bus(self):
        store = ClientStateStore()
        store.apply(_runtime_state(), _cursor(), 1)
        coord = _CoordinatorStub()
        bus = CommandBus(coord, store)
        return bus, coord, store

    def test_6_two_same_operation_pending_commands_unlocks_only_targeted_command(self):
        """Two MANUAL_CLOSE commands for different cycles: E_PENDING_COMMAND targets only the conflicting ID."""
        bus, coord, store = self._new_bus()
        scope1 = {'scope_type': 'CYCLE', 'scope_ids': ['cycle-1']}
        scope2 = {'scope_type': 'CYCLE', 'scope_ids': ['cycle-2']}

        cmd1 = bus.submit('MANUAL_CLOSE', scope1, {'position_cycle_id': 'cycle-1'})
        coord.command_failed.emit(coord.commands.requests[-1] | {'code': 'E_TIMEOUT', 'message': 'timeout'})
        cmd2 = bus.submit('MANUAL_CLOSE', scope2, {'position_cycle_id': 'cycle-2'})
        coord.command_failed.emit(coord.commands.requests[-1] | {'code': 'E_TIMEOUT', 'message': 'timeout'})

        self.assertEqual(store.pending[cmd1].status, PendingCommandStatus.SUBMISSION_UNKNOWN)
        self.assertEqual(store.pending[cmd2].status, PendingCommandStatus.SUBMISSION_UNKNOWN)

        with self.assertRaises(TraderError) as caught:
            bus.submit('MANUAL_CLOSE', scope2, {'position_cycle_id': 'cycle-2'})

        self.assertEqual(caught.exception.code, 'E_PENDING_COMMAND')
        self.assertEqual(getattr(caught.exception, 'blocked_command_id', None), cmd2)

        bus.acknowledge_unknown(caught.exception.blocked_command_id)
        self.assertNotIn(cmd2, store.pending)
        self.assertIn(cmd1, store.pending)

    def test_7_save_parameters_acknowledge_unknown_does_not_trigger_failure_modal(self):
        """Acknowledging an unknown SAVE_DSV_PARAMETERS clears fallback neutrally without failure modal."""
        bus, coord, store = self._new_bus()
        cmd = bus.submit('SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []}, {'profile': {}})
        coord.command_failed.emit(coord.commands.requests[-1] | {'code': 'E_TIMEOUT', 'message': 'timeout'})

        feedbacks = []
        rendered = []
        fake_window = SimpleNamespace(
            _closing=False,
            _stale=False,
            _boot='boot-dev46',
            _diagnostic_sink=None,
            _diagnostic_account_pseudonym=None,
            _pending_parameter_command_ids={cmd},
            _pending_parameter_fallback={cmd: 'fallback-profile'},
            _pending_focus_contracts=(CONTRACT,),
            _runtime_command_bus=bus,
            receipt_identity={'command_id': cmd},
            view=_runtime_state()['view'],
            dashboard=SimpleNamespace(
                set_parameter_feedback=lambda text, error=False: feedbacks.append((text, error)),
                tabs=SimpleNamespace(currentWidget=lambda: 'param-tab'),
                parameter_tab='param-tab',
            ),
            render_view=lambda: rendered.append(True),
            _sync_runtime_pending=lambda: None,
            _enable_commands=lambda _enabled: None,
        )

        observed = []
        bus.command_changed.connect(observed.append)
        with patch.object(QMessageBox, 'warning') as mock_warn, patch.object(QMessageBox, 'exec') as mock_exec:
            bus.acknowledge_unknown(cmd)
            self.assertEqual(len(observed), 1)
            WorkbenchWindow._runtime_command_changed(fake_window, observed[0])
            mock_warn.assert_not_called()
            mock_exec.assert_not_called()

        self.assertNotIn(cmd, fake_window._pending_parameter_command_ids)
        self.assertNotIn(cmd, fake_window._pending_parameter_fallback)
        self.assertEqual(fake_window._pending_focus_contracts, ())
        self.assertEqual(len(feedbacks), 1)
        self.assertFalse(feedbacks[0][1])
        self.assertEqual(len(rendered), 1)

    def test_8_e2e_overnight_rollover_clears_day_stop_accepts_day2_quotes_and_allows_start(self):
        """Fake-data E2E: Day 1 day_stop -> overnight rollover -> Day 2 09:19 valid quote & check_start passes."""
        session, adapter, _sink = _build_rollover_harness(self, name='t8-e2e')
        session._state['running'] = True
        session._state['synchronized'] = True
        session._state['symbols'][CONTRACT]['day_stop'] = True
        session._state['symbols'][CONTRACT]['day_stop_reason'] = 'NET_LOSS_STOP'
        session._state['symbols'][CONTRACT]['net_loss'] = '8'
        adapter.synchronized = True
        adapter.epoch = 'epoch-e2e'
        adapter.clock = None
        adapter._health = {'reason': None, 'reference_status': 'ALIGNED'}
        adapter.quote_observations = {}
        adapter._continuity = ClockContinuity()

        # 1) Day 1 closes at 15:00 -> at 15:00:05 calendar & grant roll over to Day 2 and force DISARMED.
        close_sample = ClockSample('2026-11-12T07:00:05Z', '1000000000')
        self.assertTrue(adapter._maybe_refresh_standalone_calendar(session, close_sample))
        self.assertFalse(session.snapshot()['running'])

        # 2) Next morning Day 2 09:19:18 -> resolve_active_trading_day advances to DAY2.
        day2_now = '2026-11-13T01:19:18Z'
        resolved = resolve_active_trading_day(adapter.calendar, adapter.contracts, DAY1, now=day2_now)
        self.assertEqual(resolved, DAY2)

        # 3) Day 2 live quote is recognized as VALID in current session (not QUOTE_SESSION_UNKNOWN).
        day2_quote = _quote(CONTRACT)
        day2_quote.datetime = '2026-11-13 09:19:18.000000'
        adapter.quotes[CONTRACT] = day2_quote
        day2_sample = ClockSample(day2_now, '2000000000')
        day2_window = adapter.calendar.query(CONTRACT, day2_now)
        self.assertTrue(day2_window['complete'])
        self.assertTrue(day2_window['is_trading'])
        self.assertEqual(day2_window['trading_day'], DAY2)
        quote_time, obs = adapter._observe_quote(CONTRACT, day2_sample, day2_window, DAY2)
        self.assertIsNotNone(quote_time)
        self.assertEqual(obs['age_quality'], 'VALID')
        self.assertNotEqual(obs['reason'], 'QUOTE_SESSION_UNKNOWN')

        # 4) Session rolls day to DAY2 (clearing Day 1 day_stop), re-syncs, and check_start passes.
        session._roll_day(session._state, DAY2)
        session._state['synchronized'] = True
        self.assertFalse(session._state['symbols'][CONTRACT]['day_stop'])
        adapter._continuity.observe(day2_sample)
        adapter._clock_sample = lambda: day2_sample
        adapter.check_start(session._state)

    def test_9_control_server_maps_precommit_errors_so_command_bus_never_locks(self):
        """Pre-commit SDK/metadata errors map to definite rejections in ControlServer and never lock CommandBus."""
        bus, coord, store = self._new_bus()
        server = ControlServer.__new__(ControlServer)
        server._reply = lambda peer, msg, kind, body: {'kind': kind, 'body': body}

        class _Peer:
            def __init__(self):
                self.sent = []

            def enqueue(self, payload):
                self.sent.append(payload)

        for raw_code in (
            'E_DSV_SDK_SYNC',
            'E_DSV_SDK_SCOPE',
            'E_STARTUP_METADATA',
            'E_CALENDAR_INCOMPLETE',
            'E_ONLINE_SCOPE',
            'E_DSV_DUPLICATE_CONFLICT',
        ):
            with self.subTest(raw_code=raw_code):
                peer = _Peer()
                server._error(peer, {'message_id': 'm1'}, TraderError(raw_code, 'pre-commit rejection'))
                mapped_code = peer.sent[-1]['body']['error_code']
                self.assertNotEqual(mapped_code, 'E_INTERNAL')

                cmd = bus.submit('SAVE_DSV_PARAMETERS', {'scope_type': 'ACCOUNT', 'scope_ids': []}, {'profile': {}})
                coord.command_failed.emit(
                    coord.commands.requests[-1] | {'code': mapped_code, 'message': 'rejected'},
                )
                self.assertNotIn(cmd, store.pending)

    def test_10_scope_expired_projection_disables_arm_and_surfaces_block(self):
        """When scope_expired_at is set, projection disables ARM_ENTRIES with SCOPE_EXPIRED and emits block."""
        session, adapter, _sink = _build_rollover_harness(self, name='t10-proj')
        session._state['synchronized'] = True
        session._state['running'] = False
        session._state['scope_expired_at'] = '2026-11-12T07:00:05Z'
        snap = session.snapshot()
        snap['scope'] = {'account_key': ACCOUNT, 'runtime_boot_id': 'boot-dev47'}
        view = workbench_view(
            snap,
            profile=adapter.profile,
            configuration_revision='1',
            projection_inputs={
                'profile_hash': adapter.profile.content_hash,
                'broker_connected': True,
                'adapter_synchronized': True,
                'calendar_hash': adapter.calendar.content_hash,
                'dispatch_market': {},
                'unconfigured_dispatch_market': {},
                'market_details': {},
                'minute_bars': {},
            },
        )
        ops = {row['operation']: row for row in view['capabilities']['operations']}
        self.assertFalse(ops['ARM_ENTRIES']['allowed'])
        self.assertEqual(ops['ARM_ENTRIES']['reason'], 'SCOPE_EXPIRED')
        block_codes = [row['reason_code'] for row in view['blocks'] if row.get('active')]
        self.assertIn('OPENING_SCOPE_EXPIRED', block_codes)


if __name__ == '__main__':
    unittest.main()
