import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from online_trader.accounts import AccountStore
from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.errors import TraderError
from online_trader.runtime.desktop_bootstrap import build_start_request, compile_start_scope, run_desktop_start
from online_trader.runtime.instance import AccountLock
from tests.test_accounts import MemoryVault


class DesktopBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AccountStore(self.root / 'accounts', vault=MemoryVault())
        state = self.store.initialize_simulation_accounts({
            'simnow_username': 'offline-sn-bootstrap', 'simnow_password': 'offline-sn-secret',
            'kq_username': 'offline-kq-bootstrap', 'kq_password': 'offline-kq-secret'})
        self.row = self.store.get(state['selected_id'])
        self.profile = load_confirmed_baseline()[0]
        self.now = datetime(2026, 11, 12, 12, 55, tzinfo=timezone.utc)

    def request(self, **kwargs):
        return build_start_request(self.store, self.row['account_id'], self.profile, now=self.now, **kwargs)

    def metadata(self, request):
        codes = [s['code'] for s in self.profile.to_record()['settings']['symbols']]
        return {'provider': request['provider'], 'trading_day': '2026-11-13',
            'last_completed_day': '2026-11-12', 'observed_at_utc': '2026-11-12T12:55:00Z',
            'instruments': {code: {'multiplier': '10'} for code in codes},
            'calendar': {'schema_version': 'TRADING_CALENDAR/1', 'calendar_id': 'offline-calendar',
                'calendar_version': '1', 'complete': True, 'coverage_from_utc': '2026-11-12T12:00:00Z',
                'coverage_to_utc': '2026-11-13T08:00:00Z', 'contracts': [
                    {'contract_id': code, 'sessions': [
                        {'session_id': 'night', 'trading_day': '2026-11-13', 'open_utc': '2026-11-12T13:00:00Z',
                         'close_utc': '2026-11-12T15:00:00Z', 'is_opening_session': True, 'force_close_at_end': True},
                        {'session_id': 'day', 'trading_day': '2026-11-13', 'open_utc': '2026-11-13T01:00:00Z',
                         'close_utc': '2026-11-13T07:00:00Z', 'is_opening_session': True, 'force_close_at_end': True}]
                    } for code in codes]}}

    def save_request(self, request):
        path = self.root / 'request.json'
        path.write_bytes(canonical_bytes(request))
        return path

    def test_request_is_offline_selected_account_snapshot_without_plaintext_credentials(self):
        with patch.object(self.store.vault, 'get', side_effect=AssertionError('vault not needed')):
            request = self.request()
        self.assertEqual(request['profile_hash'], self.profile.content_hash)
        self.assertEqual(request['account_key'], self.row['account_key'])
        self.assertTrue(request['arm'])
        self.assertEqual(datetime.fromisoformat(request['expires_at_utc']) - self.now, timedelta(minutes=10))
        self.assertNotIn('offline-sn-secret', repr(request))
        self.assertFalse(self.store.ledger_path(self.row).exists())

    def test_scope_uses_live_multipliers_per_contract_limits_and_original_zero_repair(self):
        request = self.request()
        grant, effective, calendar, details = compile_start_scope(request, self.store, self.metadata(request), now=self.now)
        self.assertEqual(grant.account_key, self.row['account_key'])
        self.assertEqual(grant.value['trading_days'], ['2026-11-13'])
        self.assertFalse(grant.value['resume_entries_on_day_change'])
        self.assertEqual(grant.value['not_after_utc'], '2026-11-13T07:00:00Z')
        self.assertEqual(grant.value['opening_attempts'], 1000)
        self.assertEqual(grant.value['risk_budget'], '23400')
        white = effective.to_record()['settings']['symbols'][-1]
        self.assertEqual([white[k] for k in ('open_offset', 'stop_offset', 'profit_offset')], ['100', '15', '60'])
        self.assertEqual(self.profile.to_record()['settings']['symbols'][-1]['stop_offset'], '0')
        self.assertEqual(grant.value['profile_hash'], effective.content_hash)
        self.assertFalse(details['existing_plans_changed'])

    def test_invalid_calendar_and_multiplier_facts_never_create_a_scope(self):
        request = self.request()
        original = self.metadata(request)
        cases = []
        value = copy.deepcopy(original); value['calendar']['complete'] = False; cases.append(value)
        value = copy.deepcopy(original); value['calendar']['contracts'].pop(); cases.append(value)
        value = copy.deepcopy(original); value['provider'] = 'TQKQ'; cases.append(value)
        value = copy.deepcopy(original); value['instruments'][next(iter(value['instruments']))]['multiplier'] = '0'; cases.append(value)
        value = copy.deepcopy(original); value['observed_at_utc'] = '2026-11-12T12:30:00Z'; cases.append(value)
        for value in cases:
            with self.subTest(case=value), self.assertRaises(TraderError):
                compile_start_scope(request, self.store, value, now=self.now)

    def test_friday_night_metadata_compiles_one_monday_cabinet_day_without_rollover_authority(self):
        from datetime import date
        from online_trader.runtime.startup_metadata import build_metadata
        instant = datetime(2026, 11, 13, 12, 55, tzinfo=timezone.utc)
        request = build_start_request(self.store, self.row['account_id'], self.profile, now=instant)
        codes = [s['code'] for s in self.profile.to_record()['settings']['symbols']]
        dates = {date(2026, 11, 12): True, date(2026, 11, 13): True,
                 date(2026, 11, 14): False, date(2026, 11, 15): False, date(2026, 11, 16): True}
        quotes = {code: {'expired': False, 'ins_class': 'FUTURE', 'instrument_id': code,
                        'volume_multiple': 10, 'trading_time': {
                            'day': [['09:00:00', '11:30:00'], ['13:30:00', '15:00:00']],
                            'night': [['21:00:00', '23:00:00']]}} for code in codes}
        metadata = build_metadata(dates, quotes, codes, now=instant, provider=self.row['provider'])
        grant, _, calendar, _ = compile_start_scope(request, self.store, metadata, now=instant)
        self.assertEqual(grant.value['trading_days'], ['2026-11-16'])
        self.assertEqual(grant.value['not_before_utc'], '2026-11-13T12:55:00Z')
        self.assertEqual(grant.value['not_after_utc'], '2026-11-16T07:00:00Z')
        self.assertFalse(grant.value['resume_entries_on_day_change'])
        self.assertEqual({s['trading_day'] for row in calendar.to_record()['contracts'] for s in row['sessions']},
                         {'2026-11-16'})

    def test_user_selected_account_change_invalidates_prepared_request(self):
        request = self.request()
        state = self.store.read()
        other = next(r for r in state['accounts'] if r['provider'] == 'TQKQ')
        self.store.change('select', expected_revision=state['revision'], account_id=other['account_id'])
        with self.assertRaises(TraderError):
            compile_start_scope(request, self.store, self.metadata(request), now=self.now)

    def test_bootstrap_delegates_to_original_owner_after_metadata_connection_is_closed(self):
        request = self.request(); path = self.save_request(request)
        metadata = self.metadata(request)
        def metadata_read(reference, codes, **kwargs):
            with self.assertRaises(TraderError):
                AccountLock(self.row['account_key'])
            return metadata
        def original_run(scope, reference, profile, calendar, output, **kwargs):
            lease = kwargs.pop('_execution_lock')
            self.assertTrue(lease.matches(self.row['account_key']))
            with self.assertRaises(TraderError):
                AccountLock(self.row['account_key'])
            self.assertEqual(kwargs, {'pipe': self.store.pipe_name(self.row), 'arm': True})
            self.assertEqual(strict_loads(Path(scope).read_bytes())['account_fingerprint'], self.row['account_key'].split(':')[-1])
            self.assertNotIn('password', Path(scope).read_text('utf-8'))
            return 0
        with (patch('online_trader.runtime.desktop_bootstrap.AccountStore', return_value=self.store),
              patch('online_trader.runtime.startup_metadata.read_metadata', side_effect=metadata_read) as read,
              patch('online_trader.runtime.desktop_bootstrap.dsv_simulation.run', side_effect=original_run) as run):
            self.assertEqual(run_desktop_start(path, self.root / 'run', now=self.now), 0)
        read.assert_called_once(); run.assert_called_once()
        self.assertFalse(self.store.ledger_path(self.row).exists())
        self.assertEqual(strict_loads((self.root / 'run' / 'phase-002-bootstrap.json').read_bytes())['phase'], 'STARTUP_PREPARED')

    def test_expired_or_changed_requests_fail_before_metadata_and_owner(self):
        request = self.request(); request['profile_hash'] = '0' * 64
        cases = [(request, self.now), (self.request(), self.now + timedelta(minutes=11))]
        with (patch('online_trader.runtime.desktop_bootstrap.AccountStore', return_value=self.store),
              patch('online_trader.runtime.startup_metadata.read_metadata') as read,
              patch('online_trader.runtime.desktop_bootstrap.dsv_simulation.run') as run):
            for index, (request, instant) in enumerate(cases):
                path = self.save_request(request); output = self.root / f'invalid-{index}'
                self.assertEqual(run_desktop_start(path, output, now=instant), 2)
                self.assertEqual(strict_loads((output / 'startup-failure.json').read_bytes())['error_code'], 'E_DESKTOP_START_REQUEST')
        read.assert_not_called(); run.assert_not_called()

    def test_same_confirmation_cannot_create_second_connection_or_scope(self):
        request = self.request(); path = self.save_request(request)
        with (patch('online_trader.runtime.desktop_bootstrap.AccountStore', return_value=self.store),
              patch('online_trader.runtime.startup_metadata.read_metadata', return_value=self.metadata(request)) as read,
              patch('online_trader.runtime.desktop_bootstrap.dsv_simulation.run', return_value=0) as run):
            self.assertEqual(run_desktop_start(path, self.root / 'first', now=self.now), 0)
            self.assertEqual(run_desktop_start(path, self.root / 'duplicate', now=self.now), 2)
        read.assert_called_once(); run.assert_called_once()
        failure = strict_loads((self.root / 'duplicate' / 'startup-failure.json').read_bytes())
        self.assertEqual(failure['error_code'], 'E_DESKTOP_START_REPLAY')

    def test_existing_closed_cycle_demands_original_scope_before_any_connection(self):
        request = self.request(); path = self.save_request(request)
        ledger = self.store.ledger_path(self.row); ledger.parent.mkdir(parents=True)
        connection = sqlite3.connect(ledger)
        connection.execute('CREATE TABLE position_cycles(cycle_id TEXT)')
        connection.execute("INSERT INTO position_cycles VALUES('old-cycle')")
        connection.commit(); connection.close()
        with (patch('online_trader.runtime.desktop_bootstrap.AccountStore', return_value=self.store),
              patch.object(self.store, '_settled'),
              patch('online_trader.runtime.startup_metadata.read_metadata') as read,
              patch('online_trader.runtime.desktop_bootstrap.dsv_simulation.run') as run):
            self.assertEqual(run_desktop_start(path, self.root / 'recovery', now=self.now), 2)
        read.assert_not_called(); run.assert_not_called()
        failure = strict_loads((self.root / 'recovery' / 'startup-failure.json').read_bytes())
        self.assertEqual(failure['error_code'], 'E_DESKTOP_START_RECOVERY')

    def test_metadata_error_is_reported_without_credentials_or_owner_start(self):
        request = self.request(); path = self.save_request(request)
        with (patch('online_trader.runtime.desktop_bootstrap.AccountStore', return_value=self.store),
              patch('online_trader.runtime.startup_metadata.read_metadata', side_effect=RuntimeError('credential-secret')),
              patch('online_trader.runtime.desktop_bootstrap.dsv_simulation.run') as run):
            self.assertEqual(run_desktop_start(path, self.root / 'failed', now=self.now), 2)
        run.assert_not_called()
        self.assertNotIn('credential-secret', (self.root / 'failed' / 'startup-failure.json').read_text('utf-8'))

    def test_native_runner_accepts_only_live_same_account_lease_and_releases_on_prelogin_failure(self):
        from online_trader.runtime.dsv_simulation import run
        request = self.request()
        grant, profile, calendar, _ = compile_start_scope(request, self.store, self.metadata(request), now=self.now)
        files = []
        for name, value in (('scope', grant.value), ('profile', profile.to_record()), ('calendar', calendar.to_record())):
            path = self.root / (name + '.json'); path.write_bytes(canonical_bytes(value)); files.append(path)
        reference = self.store.credential_reference(self.row['account_id'])
        with AccountLock('offline-wrong-account') as wrong, patch('online_trader.runtime.dsv_simulation.measure_reference') as clock:
            with self.assertRaises(TraderError):
                run(files[0], reference, files[1], files[2], self.root / 'wrong', _execution_lock=wrong)
            clock.assert_not_called()
        lease = AccountLock(self.row['account_key'])
        with patch('online_trader.runtime.dsv_simulation.measure_reference', side_effect=TraderError('E_CLOCK', 'Offline clock failure')):
            self.assertEqual(run(files[0], reference, files[1], files[2], self.root / 'clock-failure', _execution_lock=lease), 2)
        self.assertFalse(lease.matches(self.row['account_key']))
        with AccountLock(self.row['account_key']):
            pass


if __name__ == '__main__':
    unittest.main()
