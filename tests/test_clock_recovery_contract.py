from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from online_trader.dsv_runtime.migration import migrate_time_copy
from online_trader.dsv_storage.journal import Journal
from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock
from online_trader.scheduling.clock import ClockSample
from online_trader.scheduling.timers import due_check, recover_timer, timer_record


class TimerRecoveryTests(unittest.TestCase):
    def test_monotonic_values_from_another_boot_are_not_compared(self):
        start = ClockSample('2026-11-13T01:40:00Z', '1000', 'a' * 32)
        restarted = ClockSample('2026-11-13T01:41:01Z', '20000000000000', 'b' * 32)
        timer = timer_record('ENTRY_CONFIRM', 'entry-1', 'submit-1', start, 60, strict=True)
        self.assertIsNone(due_check(timer, restarted, 'entry-1'))
        self.assertEqual(recover_timer(timer, restarted, None), 'TIMER_RECOVERY_UNCERTAIN')
        self.assertEqual(timer['start_mono_ns'], '1000')
        self.assertIsNone(due_check(timer, restarted, 'entry-1'))

    def test_proved_utc_elapsed_is_rebased_conservatively(self):
        first = ClockSample('2026-11-13T01:40:00Z', '20000000000000', 'a' * 32)
        current = ClockSample('2026-11-13T01:41:00.010Z', '0', 'b' * 32)
        timer = timer_record('ENTRY_CONFIRM', 'entry-1', 'submit-1', first, 60,
                             strict=True, utc_error_ns='5000000')
        self.assertEqual(recover_timer(timer, current, '5000000'), 'TIMER_REBASED')
        self.assertIsNone(due_check(timer, current, 'entry-1'))
        progressed = ClockSample(current.now_utc, '1', current.clock_epoch)
        self.assertIsNone(due_check(timer, progressed, 'entry-2'))
        event = due_check(timer, progressed, 'entry-1')
        self.assertEqual((event['start_event_id'], event['lateness_ns']), ('submit-1', '1'))


@unittest.skipUnless(sys.platform == 'win32', 'Uses the native Windows account owner lock')
class ColdClockMigrationTests(unittest.TestCase):
    def test_copy_preserves_checkpoint_and_rejects_an_active_writer(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        source, target = directory / 'old.db', directory / 'clock.db'
        scope = {'account_key': 'DEV:cold-' + uuid4().hex, 'provider': 'OFFLINE', 'trading_day': '2026-11-13'}
        state = {'trading_day': '2026-11-13', 'submitted_at': '1794531600.0'}
        with closing(Journal(source, scope, state)):
            pass
        with AccountLock(scope['account_key']), self.assertRaises(TraderError):
            migrate_time_copy(source, target)
        self.assertFalse(target.exists())
        receipt = migrate_time_copy(source, target)
        self.assertEqual((receipt['source_schema'], receipt['target_schema'], receipt['minimum_writer']), (2, 3, 3))
        self.assertEqual(receipt['timer_recovery'], 'VERIFY_AFTER_NATIVE_SYNC')
        with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as migrated:
            self.assertEqual(original.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(migrated.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertEqual(original.execute('SELECT payload FROM checkpoint').fetchone(),
                             migrated.execute('SELECT payload FROM checkpoint').fetchone())
            self.assertEqual(migrated.execute('SELECT kind FROM events').fetchall(), [('TIME_SCHEMA_MIGRATED',)])
        with self.assertRaises(TraderError):
            migrate_time_copy(source, target)
