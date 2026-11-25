"""Unit tests for automatic configuration apply-marker recovery and auto-arm gating."""

from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.repository import ParameterConfigRepository
from online_trader.contracts.codec import canonical_bytes
from online_trader.contracts.validation import validate_message
from online_trader.dsv_runtime.runner import DsvSession, initial_state
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
from online_trader.dsv_runtime.standalone_owner import StandaloneDsvOwner
from online_trader.dsv_runtime.standalone_projection import workbench_view
from online_trader.dsv_storage.journal import Journal
from online_trader.errors import TraderError
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.instance import AccountLock, RuntimeLock
from online_trader.runtime.owner_tick import execute_owner_tick
from online_trader.runtime.portable_config import PortableAccountStore
from online_trader.runtime.session_storage import RuntimeSessionStorage
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.runtime.target_owner_startup import run_target_owner
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.scheduling.clock import ClockSample
from qa.target_owner.offline_runtime import CONTRACT, DAY, profile as make_profile


def _make_calendar():
    return TradingCalendar.from_record({
        'schema_version': 'TRADING_CALENDAR/1',
        'calendar_id': 'cal-recovery-1',
        'calendar_version': '1',
        'complete': True,
        'coverage_from_utc': '2026-11-13T00:00:00Z',
        'coverage_to_utc': '2026-11-13T08:00:00Z',
        'contracts': [{
            'contract_id': CONTRACT,
            'sessions': [{
                'session_id': 'day-1',
                'trading_day': DAY,
                'open_utc': '2026-11-13T01:00:00Z',
                'close_utc': '2026-11-13T07:00:00Z',
                'is_opening_session': True,
                'force_close_at_end': True,
            }],
        }],
    })


def _make_runtime_root(root: Path, active_profile: DsvProfile) -> PortableAccountStore:
    (root / 'private').mkdir(parents=True, exist_ok=True)
    (root / 'runtime.json').write_text(json.dumps({
        'schema_version': 'ONLINE_TRADER_RUNTIME/1',
        'selected_account': 'sim1',
        'accounts': [{
            'id': 'sim1',
            'alias': '模拟1',
            'provider': 'TQKQ',
            'broker': '快期模拟',
            'line': '默认',
        }],
    }), encoding='utf-8')
    (root / 'private' / 'OnlineTrader-defaults.json').write_text(json.dumps({
        'accounts': {'sim1': {'username': 'user-one', 'password': 'secret'}},
    }), encoding='utf-8')
    (root / 'OnlineTrader-parameters.json').write_text(
        json.dumps(active_profile.to_settings(), ensure_ascii=False),
        encoding='utf-8',
    )
    return PortableAccountStore(root)


class ConfigurationRecoveryTests(unittest.TestCase):
    def test_repository_evaluates_all_pending_marker_states(self):
        base = make_profile()
        raw = base.to_record()
        raw['settings']['symbols'][0]['open_offset'] = '88'
        changed = DsvProfile.from_record(raw)
        raw2 = base.to_record()
        raw2['settings']['symbols'][0]['open_offset'] = '99'
        other = DsvProfile.from_record(raw2)

        with tempfile.TemporaryDirectory() as tmp:
            repo = ParameterConfigRepository(Path(tmp))
            self.assertEqual(repo.evaluate_pending_apply_state(base.content_hash)['state'], 'NONE')

            repo.write_pending_apply_marker(
                account_key='SIM:KQ:1', boot_id='boot-1',
                old_hash=base.content_hash, new_hash=changed.content_hash, apply_id='apply-1',
            )
            self.assertEqual(repo.evaluate_pending_apply_state(base.content_hash)['state'], 'MATCHES_OLD')
            self.assertEqual(repo.evaluate_pending_apply_state(changed.content_hash)['state'], 'MATCHES_NEW')
            self.assertEqual(repo.evaluate_pending_apply_state(other.content_hash)['state'], 'HASH_CONFLICT')

            repo.marker_path().write_text('{broken json', encoding='utf-8')
            self.assertEqual(repo.evaluate_pending_apply_state(base.content_hash)['state'], 'CORRUPTED')

    def test_owner_tick_blocks_auto_arm_when_config_apply_pending_without_diagnostics(self):
        class StubOwner:
            def __init__(self):
                self.session = SimpleNamespace(_failed=False)
                self.config_apply_pending = True
                self.authority_generation = 0
                self.start_confirmed_calls = 0

            def step(self, **_kw):
                return {'type': 'APPLIED'}

            def snapshot(self):
                return {'synchronized': True, 'running': False, 'owner_fault_code': None}

            def start_confirmed(self):
                self.start_confirmed_calls += 1

            def native_account_snapshot(self):
                return None

        owner = StubOwner()
        loop_state = {'last_status': None, 'last_history_capture': 0.0, 'initial_start_pending': True}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            execute_owner_tick(
                owner,
                binding=SimpleNamespace(boot_id='boot-1', account_key='SIM:KQ:1'),
                current_trading_day=DAY,
                is_complete=lambda: True,
                history_writer=None,
                status_path=root / 'status.json',
                diagnostics_path=root / 'diag.json',
                loop_state=loop_state,
                diagnostic_sink=None,
                sleep_fn=lambda _s: None,
            )
        self.assertEqual(owner.start_confirmed_calls, 0)
        self.assertFalse(loop_state['initial_start_pending'])

    def test_standalone_owner_start_confirmed_refuses_when_config_apply_pending(self):
        base = make_profile()
        cal = _make_calendar()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        adapter.owner = get_ident()
        adapter.account_key = 'DEV:cfg-1'
        adapter.provider = 'OFFLINE'
        adapter.profile = base
        adapter.calendar = cal
        adapter.contracts = (CONTRACT,)
        adapter.grant = SimpleNamespace(value={'calendar_hash': cal.content_hash})
        adapter.connection = SimpleNamespace(broker_connected=True, close=lambda: None)
        adapter.synchronized = True
        adapter.dispatch_market = {}
        adapter._health = None
        adapter.market_health = None
        session = DsvSession(
            root / 'ledger.db', account_key='DEV:cfg-1', provider='OFFLINE',
            trading_day=DAY, profile=base, port=adapter,
        )
        self.addCleanup(session.close)
        adapter.session = session
        binding = StandaloneOwnerBinding(
            'boot-1', 'DEV:cfg-1', 'OFFLINE',
            SimpleNamespace(matches=lambda: True),
            SimpleNamespace(matches=lambda _k: True),
        )
        owner = StandaloneDsvOwner(session, adapter, adapter.connection, base, binding, '1')
        owner.config_apply_pending = True
        with self.assertRaises(TraderError) as err:
            owner.start_confirmed()
        self.assertEqual(err.exception.code, 'E_NOT_READY')

    def test_run_target_owner_auto_recovers_all_marker_cases_disarmed_without_diagnostics(self):
        base = make_profile()
        raw = base.to_record()
        raw['settings']['symbols'][0]['open_offset'] = '88'
        changed = DsvProfile.from_record(raw)
        raw2 = base.to_record()
        raw2['settings']['symbols'][0]['open_offset'] = '99'
        other = DsvProfile.from_record(raw2)
        cal = _make_calendar()

        for case_name, setup_marker in (
            ('MATCHES_OLD', lambda repo: repo.write_pending_apply_marker(
                account_key=store.read()['accounts'][0]['account_key'], boot_id='old-boot',
                old_hash=base.content_hash, new_hash=changed.content_hash, apply_id='apply-old',
            )),
            ('MATCHES_NEW', lambda repo: repo.write_pending_apply_marker(
                account_key=store.read()['accounts'][0]['account_key'], boot_id='old-boot',
                old_hash=other.content_hash, new_hash=base.content_hash, apply_id='apply-new',
            )),
            ('HASH_CONFLICT', lambda repo: repo.write_pending_apply_marker(
                account_key=store.read()['accounts'][0]['account_key'], boot_id='old-boot',
                old_hash=changed.content_hash, new_hash=other.content_hash, apply_id='apply-conflict',
            )),
            ('CORRUPTED', lambda repo: repo.marker_path().write_text('{bad-json', encoding='utf-8')),
        ):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                rt_root = Path(tmp) / 'runtime'
                store = _make_runtime_root(rt_root, base)
                account = store.read()['accounts'][0]
                account_key = account['account_key']
                repo = store.config_repository()
                setup_marker(repo)

                with RuntimeSessionStorage() as storage:
                    grant = DsvSimulationGrant.parse(canonical_bytes({
                        'schema_version': 'KQ_DSV_DESKTOP/1',
                        'grant_id': 'grant-rec-' + case_name.lower(),
                        'account_fingerprint': account_key.split(':')[-1],
                        'ledger_path': str(storage.ledger),
                        'contracts': [CONTRACT],
                        'trading_day': DAY,
                        'trading_days': [DAY],
                        'resume_entries_on_day_change': False,
                        'not_before_utc': '2026-11-13T00:00:00Z',
                        'not_after_utc': '2026-11-13T08:00:00Z',
                        'max_order_quantity': 2,
                        'daily_targets_per_contract': 4,
                        'risk_budget': '600',
                        'profile_hash': base.content_hash,
                        'calendar_hash': cal.content_hash,
                        'authorization_reference': 'DESKTOP_CONFIRM:' + 'a' * 64,
                    }))
                    quote = SimpleNamespace(
                        instrument_id=CONTRACT, expired=False, ins_class='FUTURE',
                        volume_multiple=10, price_tick=1.0, last_price=1000.0,
                        highest=1010.0, lowest=990.0, bid_price1=999.0, ask_price1=1001.0,
                        lower_limit=900.0, upper_limit=1100.0, datetime='2026-11-13 09:05:00.000000',
                    )
                    pos = SimpleNamespace(pos_long=0, pos_short=0, pos_long_today=0, pos_long_his=0,
                                          pos_short_today=0, pos_short_his=0)
                    api = SimpleNamespace(
                        get_quote=lambda _c, **_kw: quote,
                        get_kline_serial=lambda *_a, **_kw: [],
                        wait_update=lambda **_kw: False,
                        get_order=lambda *_a, **_kw: {},
                        get_trade=lambda *_a, **_kw: {},
                        get_account=lambda *_a, **_kw: SimpleNamespace(balance=100000.0, available=90000.0, margin=0.0),
                        get_position=lambda _c, *_a, **_kw: pos,
                        is_serial_ready=lambda _s: True,
                    )
                    conn = SimpleNamespace(
                        grant=grant, api=api, account=object(), broker_connected=True, read_only=False,
                        source_epoch=1, complete=lambda: True, poll_updates=lambda _w: None,
                        configure_calendar=lambda _cal: None,
                        close=lambda: None,
                        snapshot=lambda: {
                            'account_key': account_key, 'complete': True, 'trading_day': DAY,
                            'source_epoch': 1, 'funds': {'balance': '100000', 'available': '90000'},
                        },
                    )
                    captured_views = []

                    class FakeServer:
                        def __init__(self, owner, _pipe, **_kw):
                            self.owner = owner

                        def serve(self, *, stop_event, owner_tick, publish_workbench=True):
                            owner_tick()
                            captured_views.append(self.owner.workbench())
                            stop_event.set()

                    fake_clock = SimpleNamespace(
                        epoch='clock-1',
                        capture=lambda: ClockSample('2026-11-13T01:05:00Z', '1000000', 'clock-1'),
                        health=lambda _s: {'reference_status': 'OBSERVED_OK', 'reason': None},
                        entry_ready=lambda: True,
                        protection_ready=lambda: True,
                    )
                    class _TestRuntimeLock(RuntimeLock):
                        def __init__(self):
                            self.owner_thread = get_ident()
                        def matches(self):
                            return self.owner_thread == get_ident()

                    class _TestAccountLock(AccountLock):
                        def __init__(self, key):
                            self.account_key = key
                            self._handle = object()
                            self.owner_thread = get_ident()
                        def matches(self, key):
                            return self.account_key == key and self.owner_thread == get_ident()

                    with (
                        patch('online_trader.runtime.target_owner_startup.strategy_port',
                              return_value=SimpleNamespace(connect=lambda *_a: conn)),
                        patch('online_trader.runtime.target_owner_startup.ControlServer', FakeServer),
                        patch('online_trader.runtime.target_owner_startup.BrokerHistoryWriter', side_effect=OSError('no hist')),
                        patch('online_trader.runtime.target_owner_startup.attach_order_blocking_log',
                              return_value=nullcontext()),
                    ):
                        rc = run_target_owner(
                            grant=grant, profile=base, calendar=cal,
                            credentials={'username': 'u', 'password': 'p'},
                            output=Path(tmp) / 'out', pipe=r'\\.\pipe\test-' + case_name,
                            session_storage=storage,
                            runtime_lock=_TestRuntimeLock(),
                            account_lock=_TestAccountLock(account_key),
                            clock=fake_clock, trusted_control_pid=-1,
                            history_cache=Path(tmp) / 'history.db',
                            arm=True, configured_store=store, diagnostic_sink=None,
                        )
                    self.assertEqual(rc, 0)
                    self.assertFalse(repo.marker_path().exists())
                    self.assertEqual(len(captured_views), 1)
                    view = captured_views[0]
                    self.assertEqual(view['entry_authority'], 'DISARMED')
                    self.assertIsNotNone(view['config_recovery_notice'])
                    self.assertEqual(view['config_recovery_notice']['state'], case_name)
                    validate_message({
                        'protocol_version': '1.0',
                        'type': 'WorkbenchSnapshot',
                        'message_id': 'msg-1',
                        'connection_id': 'conn-1',
                        'correlation_id': None,
                        'sent_at_utc': '2026-11-25T01:00:00Z',
                        'kernel_boot_id': storage.boot_id,
                        'account_key': account_key,
                        'body': {
                            'payload': view,
                            'content_hash': 'a' * 64,
                        },
                    })


if __name__ == '__main__':
    unittest.main()
