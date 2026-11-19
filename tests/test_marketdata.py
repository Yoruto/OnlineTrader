import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from online_trader.contracts.codec import canonical_bytes,content_hash
from online_trader.errors import TraderError
from online_trader.marketdata import ReferenceStore,review_calendar
from online_trader.scheduling.calendar import TradingCalendar
from tests import test_online_dsv as fixtures

day_calendar=fixtures.day_calendar


def package():
    return {'schema_version':'CALENDAR_PACKAGE/1','source':{'kind':'EXCHANGE_FILE','reference':'offline-source-document','sha256':'0'*64},
        'observed_at_utc':'2026-11-10T00:00:00Z','valid_until_utc':'2026-11-10T08:00:00Z',
        'date_flags':[{'date':'2026-11-10','trading':True}],'calendar':day_calendar().to_record()}


class ReferenceDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store=ReferenceStore(self.root/'references')
    def test_daily_cache_roundtrip_keeps_exact_dataset_and_creates_no_ledger(self):
        path=self.root/'daily.csv';path.write_text('series_id,date,high,low,close\nDCE.p2701,2026-11-09,1100,1000,1050\n')
        result=self.store.import_daily(path);self.assertFalse(result['runtime_activation'])
        output=self.store.daily_csv(result['content_hash'],self.root/'export.csv')
        from online_trader.configuration.offsets import load_daily
        self.assertEqual(load_daily(output).content_hash,result['data_hash']);self.assertFalse((self.root/'ledger.db').exists())
    def test_duplicate_daily_import_is_idempotent_and_export_never_overwrites(self):
        path=Path(__file__).parents[1]/'docs/examples/offset-daily-sample.csv'
        first=self.store.import_daily(path);self.assertEqual(first,self.store.import_daily(path))
        out=self.root/'export.csv';self.store.daily_csv(first['content_hash'],out)
        with self.assertRaises(TraderError):self.store.daily_csv(first['content_hash'],out)
    def test_calendar_source_gap_and_closed_trading_date_are_rejected(self):
        for flags in ([{'date':'2026-11-10','trading':False}],
            [{'date':'2026-11-10','trading':True},{'date':'2026-11-12','trading':True}]):
            with self.subTest(flags=flags),self.assertRaises(TraderError):review_calendar(package()|{'date_flags':flags},as_of_utc='2026-11-10T01:00:00Z')
    def test_valid_reference_keeps_online_source_verification_separate(self):
        result=review_calendar(package(),as_of_utc='2026-11-10T01:00:00Z')
        self.assertTrue(result['current_reference']);self.assertFalse(result['source_verified_online']);self.assertFalse(result['runtime_activation'])
    def test_expired_future_and_user_reference_remain_explicit(self):
        self.assertIn('SOURCE_EXPIRED',review_calendar(package(),as_of_utc='2026-11-10T09:00:00Z')['reasons'])
        self.assertIn('SOURCE_FROM_FUTURE',review_calendar(package(),as_of_utc='2026-11-09T23:00:00Z')['reasons'])
        value=package();value['source']['kind']='USER_REFERENCE'
        self.assertIn('USER_REFERENCE_NOT_AUTHORITATIVE',review_calendar(value,as_of_utc='2026-11-10T01:00:00Z')['reasons'])
    def test_calendar_identity_cannot_be_replaced_or_activate_running_owner(self):
        path=self.root/'calendar.json';value=package();path.write_bytes(canonical_bytes(value))
        first=self.store.import_calendar(path,as_of_utc='2026-11-10T01:00:00Z')
        self.assertEqual(first,self.store.import_calendar(path,as_of_utc='2026-11-10T01:00:00Z'))
        value['calendar']['contracts'][0]['sessions'][0]['close_utc']='2026-11-10T06:59:00Z';path.write_bytes(canonical_bytes(value))
        with self.assertRaises(TraderError):self.store.import_calendar(path,as_of_utc='2026-11-10T01:00:00Z')
    def test_tampered_history_cannot_be_exported(self):
        result=self.store.import_daily(Path(__file__).parents[1]/'docs/examples/offset-daily-sample.csv')
        Path(result['path']).write_text('{}')
        with self.assertRaises(TraderError):self.store.daily_csv(result['content_hash'],self.root/'bad.csv')
    def test_midnight_inside_night_session_is_not_a_cabinet_day_change(self):
        record=day_calendar().to_record();record['coverage_from_utc']='2026-11-09T12:00:00Z'
        record['contracts'][0]['sessions'].insert(0,{'session_id':'night','trading_day':'2026-11-10','open_utc':'2026-11-09T13:00:00Z',
            'close_utc':'2026-11-09T18:30:00Z','is_opening_session':True,'force_close_at_end':False})
        calendar=TradingCalendar.from_record(record)
        result=calendar.transition('DCE.p2701','2026-11-09T15:59:00Z','2026-11-09T16:01:00Z')
        self.assertFalse(result['changed']);self.assertEqual(result['days'],['2026-11-10'])
    def test_friday_to_monday_labels_are_explicit_not_weekday_arithmetic(self):
        record=day_calendar().to_record();record['coverage_to_utc']='2026-11-16T08:00:00Z'
        record['contracts'][0]['sessions'].append({'session_id':'monday-night','trading_day':'2026-11-16','open_utc':'2026-11-13T13:00:00Z',
            'close_utc':'2026-11-13T15:00:00Z','is_opening_session':True,'force_close_at_end':False})
        result=TradingCalendar.from_record(record).transition('DCE.p2701','2026-11-10T02:00:00Z','2026-11-13T14:00:00Z')
        self.assertEqual(result['days'],['2026-11-10','2026-11-16']);self.assertTrue(result['changed']);self.assertTrue(result['entry_reauthorization_required'])
    def test_missing_coverage_never_proves_day_reset(self):
        result=day_calendar().transition('DCE.p2701','2026-11-10T02:00:00Z','2026-11-11T02:00:00Z')
        self.assertFalse(result['complete']);self.assertIsNone(result['changed']);self.assertTrue(result['preserve_original_protection'])


class ExtendedNativeHistoryTests(unittest.TestCase):
    def setUp(self):self.case=fixtures.OnlineDsvTests();self.case.setUp()
    def tearDown(self):self.case.tearDown()
    def test_native_full_window_and_over_4096_history_rebuild_without_entry_replay(self):
        from datetime import datetime,timedelta
        x=self.case
        first=datetime.fromisoformat('2026-11-10T01:00:00Z')
        ticks=({'at': (first + timedelta(microseconds=index*85_500)).isoformat(), 'last': 1055}
               for index in range(4200))
        x.start(history=False, native_ticks=ticks, arm=False)
        self.assertFalse(x.kernel._armed);self.assertEqual(x.api.inserted,[])
        self.assertEqual(x.session.feed._accepted[(x.code,x.port.source_epoch)],4199)
    def test_native_time_regression_cannot_fake_a_complete_path(self):
        x=self.case;x.start();rows=x.api.serials[x.code].rows
        rows[-1]['datetime']=rows[0]['datetime']-1000
        with self.assertRaises(TraderError):x.session.feed.batches()

    def test_day_change_latches_original_exit_barrier_without_clearing_protection(self):
        from datetime import timedelta
        record=day_calendar().to_record();record['coverage_to_utc']='2026-11-11T08:00:00Z'
        period=copy.deepcopy(record['contracts'][0]['sessions'][0]);period.update(session_id='next-day',trading_day='2026-11-11',
            open_utc='2026-11-11T01:00:00Z',close_utc='2026-11-11T07:00:00Z')
        record['contracts'][0]['sessions'].append(period);calendar=TradingCalendar.from_record(record)
        x=self.case
        with patch.object(fixtures,'day_calendar',return_value=calendar):x.entered()
        before=copy.deepcopy(x.cycle()['protection']);plan=copy.deepcopy(x.cycle()['plan']);calls=len(x.api.inserted)
        x.clock.value+=timedelta(days=1)
        self.assertTrue(x.session.check_day_transition());x.kernel.wait_idle()
        self.assertFalse(x.kernel._armed);self.assertTrue(x.kernel.recovery_required)
        self.assertEqual(x.cycle()['plan'],plan);self.assertEqual(x.cycle()['protection'],before);self.assertEqual(len(x.api.inserted),calls)
        x.session.check_day_transition();x.kernel.wait_idle()
        self.assertEqual(len(x.kernel.ledger.query("SELECT event_seq FROM event_journal WHERE event_type='ONLINE_DSV_DAY_CHANGE_REQUIRES_RECONCILIATION'")),1)
