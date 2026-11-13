from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from online_trader.accounts import AccountStore
from online_trader.errors import TraderError
from online_trader.runtime.startup_metadata import _sdk_factory, build_metadata, read_metadata
from online_trader.scheduling.calendar import TradingCalendar
from tests.test_accounts import MemoryVault


CODE = 'SHFE.ag2702'


def quote(**changes):
    return {'instrument_id': CODE, 'ins_class': 'FUTURE', 'expired': False, 'volume_multiple': 15,
            'trading_time': {'day': [['09:00:00', '10:15:00'], ['10:30:00', '11:30:00'],
                                     ['13:30:00', '15:00:00']],
                             'night': [['21:00:00', '26:30:00']]}} | changes


def dates(start=date(2026, 10, 20), end=date(2026, 12, 2), closed=()):
    return {start + timedelta(days=i): (start + timedelta(days=i)).weekday() < 5
            and start + timedelta(days=i) not in closed for i in range((end - start).days + 1)}


class Api:
    def __init__(self):
        self.closed = False
        self.quote = quote()
        self.calendar_error = False

    def get_quote(self, code):
        if code != CODE: raise AssertionError('Unexpected contract')
        return self.quote

    def get_trading_calendar(self, start, end):
        if self.calendar_error: raise RuntimeError('credentials-must-never-appear')
        return [{'date': day, 'trading': trading} for day, trading in dates(start, end).items()]

    def close(self): self.closed = True

    def insert_order(self, *args, **kwargs): raise AssertionError('No order call is permitted')
    def cancel_order(self, *args, **kwargs): raise AssertionError('No cancel call is permitted')


class StartupMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(Path(self.temp.name), vault=MemoryVault())
        self.state = self.store.initialize_simulation_accounts({
            'simnow_username': 'offline-simnow', 'simnow_password': 'offline-trade-secret',
            'kq_username': 'offline-market', 'kq_password': 'offline-market-secret'})
        self.reference = self.store.credential_reference(self.state['selected_id'])
        self.now = datetime(2026, 11, 12, 11, 30, tzinfo=timezone.utc)

    def build(self, at, **kwargs):
        return build_metadata(kwargs.get('days', dates()), {CODE: kwargs.get('quote', quote())}, [CODE],
                              now=datetime.fromisoformat(at), provider='TQ_SIMNOW')

    def test_evening_selects_next_trading_day_and_covers_wait_before_night(self):
        result = self.build('2026-11-12T11:30:00+00:00')
        self.assertEqual(result['trading_day'], '2026-11-13')
        self.assertEqual(result['last_completed_day'], '2026-11-12')
        calendar = TradingCalendar.from_record(result['calendar'])
        at = calendar.query(CODE, '2026-11-12T11:30:00Z')
        self.assertTrue(at['complete']); self.assertFalse(at['is_trading'])
        self.assertEqual(at['next_session']['open_utc'], '2026-11-12T13:00:00Z')
        self.assertEqual(calendar.session_times(CODE, '2026-11-13')[-1][1], '2026-11-13T07:00:00Z')

    def test_midnight_and_friday_night_preserve_exact_next_trading_day(self):
        result = self.build('2026-11-13T17:00:00+00:00')
        self.assertEqual(result['trading_day'], '2026-11-16')
        calendar = TradingCalendar.from_record(result['calendar'])
        active = calendar.query(CODE, '2026-11-13T17:00:00Z')
        self.assertTrue(active['is_trading'])
        self.assertEqual(active['trading_day'], '2026-11-16')
        self.assertEqual(active['session']['close_utc'], '2026-11-13T18:30:00Z')

    def test_weekday_holiday_gap_does_not_fabricate_prior_night(self):
        with self.assertRaises(TraderError) as error:
            self.build('2026-11-13T11:30:00+00:00', days=dates(closed=(date(2026, 11, 16),)))
        self.assertEqual(error.exception.code, 'E_CALENDAR_INCOMPLETE')

    def test_no_night_contract_has_only_confirmed_day_periods(self):
        value = quote(trading_time={'day': [['09:00:00', '15:00:00']], 'night': []})
        result = self.build('2026-11-12T11:30:00+00:00', quote=value)
        sessions = result['calendar']['contracts'][0]['sessions']
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]['open_utc'], '2026-11-13T01:00:00Z')

    def test_reader_uses_only_market_auth_and_closes_before_return(self):
        api = Api(); passed = []
        def factory(auth, *, deadline): passed.append(dict(auth)); return api
        result = read_metadata(self.reference, [CODE], now=self.now, sdk_factory=factory)
        self.assertTrue(api.closed)
        self.assertEqual(passed, [{'username': 'offline-market', 'password': 'offline-market-secret'}])
        self.assertEqual(result['instruments'][CODE]['multiplier'], '15')
        self.assertIn('not cabinet reconciliation', result['source'])
        self.assertNotIn('secret', repr(result))
        self.assertFalse(self.store.ledger_path(self.store.get(self.state['selected_id'])).exists())

    def test_reader_closes_on_bad_quote_and_sanitizes_sdk_failure(self):
        for kind in ('calendar', 'multiplier', 'expired', 'time'):
            with self.subTest(kind=kind):
                api = Api()
                if kind == 'calendar': api.calendar_error = True
                if kind == 'multiplier': api.quote['volume_multiple'] = float('nan')
                if kind == 'expired': api.quote['expired'] = True
                if kind == 'time': api.quote['trading_time']['night'] = [['21:00:00', '02:30:00']]
                with self.assertRaises(TraderError) as error:
                    read_metadata(self.reference, [CODE], now=self.now, sdk_factory=lambda *_a, **_k: api)
                self.assertTrue(api.closed)
                self.assertNotIn('credentials-must-never-appear', str(error.exception))

    def test_elapsed_budget_closes_reader_and_does_not_return_partial_calendar(self):
        api = Api()
        with patch('online_trader.runtime.startup_metadata.time.monotonic', side_effect=[0, 0, 61]):
            with self.assertRaises(TraderError) as error:
                read_metadata(self.reference, [CODE], now=self.now, sdk_factory=lambda *_a, **_k: api)
        self.assertEqual(error.exception.code, 'E_STARTUP_METADATA_TIMEOUT')
        self.assertTrue(api.closed)

    def test_changed_selection_refuses_before_reading_credentials_or_opening_sdk(self):
        other = next(row for row in self.state['accounts'] if row['provider'] == 'TQKQ')
        self.store.change('select', expected_revision=self.state['revision'], account_id=other['account_id'])
        with patch.object(self.store.vault, 'get') as read, self.assertRaises(TraderError):
            read_metadata(self.reference, [CODE], now=self.now, sdk_factory=lambda *_a, **_k: self.fail('No SDK'))
        read.assert_not_called()

    def test_unclosed_metadata_session_prevents_execution_handoff(self):
        api = Api()
        with patch.object(api, 'close', side_effect=RuntimeError('close error')), self.assertRaises(TraderError) as error:
            read_metadata(self.reference, [CODE], now=self.now, sdk_factory=lambda *_a, **_k: api)
        self.assertEqual(error.exception.code, 'E_STARTUP_METADATA_CLOSE')

    def test_real_factory_shape_uses_local_account_and_disables_order_methods(self):
        class BaseApi:
            def __init__(self, **kwargs): self.arguments = kwargs
            def wait_update(self, **kwargs): self.wait = kwargs; return True
            def close(self): pass
        class LocalAccount: pass
        sdk = SimpleNamespace(TqApi=BaseApi, TqSim=LocalAccount, TqAuth=lambda user, password: (user, password))
        with patch.dict('sys.modules', {'tqsdk': sdk}), patch('online_trader.runtime.startup_metadata.version', return_value='3.10.4'):
            api = _sdk_factory({'username': 'market-user', 'password': 'market-password'}, deadline=time.monotonic() + 10)
        self.assertIsInstance(api.arguments['account'], LocalAccount)
        self.assertEqual(api.arguments['auth'], ('market-user', 'market-password'))
        for method in (api.insert_order, api.cancel_order):
            with self.assertRaises(TraderError) as error: method()
            self.assertEqual(error.exception.code, 'E_READ_ONLY')
        api.wait_update(deadline=time.time() + 100)
        self.assertLessEqual(api.wait['deadline'], time.time() + 10)


if __name__ == '__main__': unittest.main()
