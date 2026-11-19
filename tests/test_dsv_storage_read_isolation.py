"""Real SQLite/WAL and offline owner tests. No SDK, account or no-op product lock.

The offline owner uses its existing lease injection, backed by an OS file lock
on POSIX and the real AccountLock on Windows. RecordingPort is the send boundary.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import strict_loads
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.runtime_diagnostics import RuntimeDiagnostics
from online_trader.dsv_storage.journal import Journal
from online_trader.errors import TraderError, exception_record
from qa.recording_port import RecordingPort

CODE = 'DCE.p2701'
DAY = '2026-11-16'
SCOPE = {'account_key': 'DEV:storage-read-regression', 'provider': 'OFFLINE', 'trading_day': DAY,
         'configuration_id': 'fixture'}


@contextmanager
def lease(root):
    if os.name == 'nt':
        from online_trader.runtime.instance import AccountLock
        with AccountLock(SCOPE['account_key']) as value:
            yield value
    else:
        import fcntl
        with (root / 'owner.lock').open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield handle
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def profile():
    settings = load_confirmed_baseline()[0].to_settings()
    settings['symbols'] = [settings['symbols'][0] | {
        'code': CODE, 'open_offset': 40, 'stop_offset': 8, 'profit_offset': 20, 'order_volume': 5}]
    settings['trade'].update(no_chase_after_act=False, continuation_entry=False,
        protect_80_enabled=False, trailing_activate_ratio=0, passive_entry_first=False)
    return DsvProfile.from_settings(settings)


def frame(seq, price=1060, owned=0, facts=()):
    wall = (datetime(2026, 11, 16, 5, 40, tzinfo=timezone.utc) + timedelta(seconds=seq)).isoformat().replace('+00:00', 'Z')
    tick = {'native_time': wall, 'candle_id': CODE + ':20261116T0540',
            'last': str(price), 'bid1': str(price), 'ask1': str(price),
            'day_high': '1088', 'day_low': '1020', 'tick_size': '1', 'lower_limit': '900', 'upper_limit': '1200'}
    market = {'contract_id': CODE, 'ticks': [tick], 'opening_session': False,
              'session_open': DAY + 'T05:30:00Z', 'session_close': DAY + 'T07:00:00Z',
              'long_qty': owned, 'short_qty': 0, 'available': '100000', 'margin_per_lot': '1000'}
    return {'id': f'round-{seq}', 'sequence': seq, 'session_id': 'storage-test', 'wall_time': wall,
            'monotonic_ms': seq * 1000, 'market': [market], 'native': list(facts)}


class JournalIsolationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / 'facts.db'
        self.journal = Journal(self.path, SCOPE, {'value': 0})
        self.addCleanup(self.journal.close)

    def seed(self):
        with self.journal.transaction():
            for i in range(96):
                self.journal.event('ROUND', {'value': i})

    def test_online_version_check_refuses_before_creating_database(self):
        path = self.root / 'online.db'
        with patch('sqlite3.sqlite_version_info', (3, 46, 1)):
            with self.assertRaises(TraderError) as raised:
                Journal(path, SCOPE | {'provider': 'TQ_SIMNOW'}, {})
        self.assertEqual(raised.exception.code, 'E_DSV_STORE_SQLITE_VERSION')
        self.assertFalse(path.exists())

    def test_wal_reset_fix_version_boundaries(self):
        from online_trader.dsv_storage.journal import wal_runtime_supported
        for version in ((3, 51, 3), (3, 52, 0), (3, 50, 7), (3, 44, 6)):
            self.assertTrue(wal_runtime_supported(version), version)
        for version in ((3, 51, 2), (3, 50, 6), (3, 44, 5), (3, 46, 1)):
            self.assertFalse(wal_runtime_supported(version), version)

    def test_file_store_uses_wal_full_and_bounded_autocheckpoint(self):
        self.assertEqual(self.journal._db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(self.journal._db.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.assertEqual(self.journal._db.execute('PRAGMA wal_autocheckpoint').fetchone()[0], 1000)

    def test_in_memory_qa_store_still_supported(self):
        with closing(Journal(':memory:', SCOPE, {'value': 1})) as j:
            self.assertEqual(j.load(), {'value': 1})

    def test_suspended_read_cursor_does_not_block_committed_facts(self):
        self.seed()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as r:
            with closing(r.execute('SELECT * FROM events')) as cursor:
                cursor.fetchone()
                with self.journal.transaction():
                    self.journal.save({'value': 1})
                    self.journal.event('ROUND', {'value': 96})
                self.assertEqual(self.journal.load(), {'value': 1})
                self.assertEqual(len(cursor.fetchall()), 95)
            self.assertEqual(r.execute('SELECT COUNT(*) FROM events').fetchone()[0], 97)

    def test_explicit_reader_snapshot_stays_consistent_without_blocking_writer(self):
        self.seed()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as r:
            r.execute('BEGIN')
            before = r.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            with self.journal.transaction():
                self.journal.event('ROUND', {'value': 97})
            self.assertEqual(r.execute('SELECT COUNT(*) FROM events').fetchone()[0], before)
            r.rollback()
            self.assertEqual(r.execute('SELECT COUNT(*) FROM events').fetchone()[0], before + 1)

    def test_sqlite_backup_includes_committed_wal_not_only_main_file(self):
        self.seed()
        with closing(sqlite3.connect(self.root / 'backup.db')) as dest:
            self.journal._db.backup(dest)
            self.assertEqual(dest.execute('SELECT COUNT(*) FROM events').fetchone()[0], 96)
            self.assertEqual(dest.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_body_failure_rolls_back_all_facts_and_keeps_sqlite_category(self):
        with self.assertRaises(sqlite3.Error) as raised:
            with self.journal.transaction():
                self.journal.save({'value': 99})
                self.journal._db.execute('SELECT * FROM no_such_private_table')
        self.assertEqual(self.journal.load(), {'value': 0})
        self.assertFalse(self.journal._db.in_transaction)
        detail = RuntimeDiagnostics._detail(raised.exception)
        self.assertEqual(detail['stack'][0]['sqlite_errorname'], 'SQLITE_ERROR')
        self.assertEqual(detail['stack'][0]['storage_context']['transaction_stage'], 'BODY')
        self.assertNotIn('no_such_private_table', str(detail))

    def test_writer_contention_still_raises_busy_before_body(self):
        entered = False
        with closing(sqlite3.connect(self.path, isolation_level=None)) as other:
            other.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError) as raised:
                with self.journal.transaction():
                    entered = True
            other.rollback()
        self.assertFalse(entered)
        self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
        self.assertEqual(RuntimeDiagnostics._detail(raised.exception)['stack'][0]['storage_context']['transaction_stage'], 'BEGIN')

    def test_delete_store_upgrades_only_after_matching_scope(self):
        path = self.root / 'old.db'
        with closing(Journal(path, SCOPE, {'value': 7})):
            pass
        with closing(sqlite3.connect(path)) as old:
            old.execute('PRAGMA journal_mode=DELETE').fetchone()
        with self.assertRaises(TraderError):
            Journal(path, SCOPE | {'account_key': 'DEV:wrong'}, {})
        with closing(sqlite3.connect(path)) as old:
            self.assertEqual(old.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        with closing(Journal(path, SCOPE, {})) as j:
            self.assertEqual(j.load(), {'value': 7})
            self.assertEqual(j._db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_publish_closed_migration_store_keeps_wal_commits(self):
        from online_trader.dsv_runtime.migration import _publish
        target = self.root / 'published.db'
        _publish(target, SCOPE, {'value': 8}, {'type': 'test'})
        with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as r:
            self.assertEqual(r.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
            self.assertEqual(r.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_committed_wal_recovers_after_process_exit_without_close(self):
        path = self.root / 'crashed.db'
        root = Path(__file__).resolve().parents[1]
        code = """import os,sys
from online_trader.dsv_storage.journal import Journal
j=Journal(sys.argv[1], {'account_key':'DEV:crash'}, {'value':0})
with j.transaction():
    j.save({'value':9})
    j.event('COMMITTED', {'value':9})
os._exit(0)
"""
        env = dict(os.environ, PYTHONPATH=str(root / 'src'))
        subprocess.run([sys.executable, '-c', code, str(path)], check=True, env=env, timeout=10)
        with closing(Journal(path, {'account_key': 'DEV:crash'}, {})) as j:
            self.assertEqual(j.load(), {'value': 9})
            self.assertEqual(j._db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
            self.assertEqual(j._db.execute('PRAGMA quick_check').fetchone()[0], 'ok')


class OwnerReadIsolationTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / 'facts.db'
        lock = self.enterContext(lease(root))
        self.port = RecordingPort()
        self.session = self.enterContext(DsvSession(self.path, account_key=SCOPE['account_key'], provider='OFFLINE',
            trading_day=DAY, profile=profile(), port=self.port, _execution_lock=lock))

    def test_stopped_owned_position_can_commit_stop_loss_under_long_reader(self):
        s = self.session
        s.command({'id': 'start', 'action': 'START'})
        child = s.apply_round(frame(1))['effects'][0]['child_order_id']
        s.apply_round(frame(2, owned=5, facts=[
            {'kind': 'ORDER', 'child_order_id': child, 'native_order_id': 'open-native', 'status': 'FILLED', 'filled_qty': 5},
            {'kind': 'TRADE', 'child_order_id': child, 'native_order_id': 'open-native',
             'native_trade_id': 'synthetic-fill', 'quantity': 5, 'price': '1060'}]))
        s.command({'id': 'stop', 'action': 'STOP'})
        for seq in range(3, 60):
            s.apply_round(frame(seq, owned=5))
        before = len(self.port.orders)
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as r:
            with closing(r.execute('SELECT seq,payload FROM events')) as cursor:
                cursor.fetchone()
                send = self.port.send
                def committed_send(request):
                    with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as proof:
                        checkpoint = strict_loads(proof.execute('SELECT payload FROM checkpoint WHERE id=1').fetchone()[0])
                        self.assertEqual(checkpoint['sequence'], 60)
                        self.assertEqual(checkpoint['symbols'][CODE]['position']['exit_target'], 0)
                        self.assertIsNotNone(proof.execute('SELECT id FROM effects WHERE id=? AND result IS NULL',
                                                          (request['id'],)).fetchone())
                    return send(request)
                with patch.object(self.port, 'send', side_effect=committed_send):
                    receipt = s.apply_round(frame(60, price=1051, owned=5))
                saved = s._journal.load()
                self.assertEqual(saved['symbols'][CODE]['position']['exit_target'], 0)
                self.assertFalse(saved['running'])
                self.assertFalse(s._failed)
        self.assertEqual(len(self.port.orders) - before, 1)
        self.assertEqual(receipt, s.apply_round(frame(60, price=1051, owned=5)))
        self.assertEqual(len(self.port.orders) - before, 1)

    def test_real_storage_failure_still_prevents_send_and_latches_owner(self):
        s = self.session
        s.command({'id': 'start', 'action': 'START'})
        with patch.object(s._journal, 'save_checkpoint', side_effect=sqlite3.OperationalError('pre-dispatch disk failure')):
            with self.assertRaises(TraderError) as raised:
                s.apply_round(frame(1))
        self.assertEqual(raised.exception.code, 'E_DSV_STORE')
        self.assertTrue(s._failed)
        self.assertEqual(self.port.orders, [])
        self.assertEqual(s._journal.load()['sequence'], 0)
        self.assertEqual(s._journal._db.execute('SELECT COUNT(*) FROM effects').fetchone()[0], 0)


class DiagnosticMetadataTests(unittest.TestCase):
    def test_non_sqlite_exception_cannot_export_forged_sqlite_fields(self):
        error = ValueError('secret')
        error.sqlite_errorcode = 5
        error.sqlite_errorname = 'SQLITE_BUSY'
        self.assertNotIn('sqlite_errorcode', exception_record(error)['stack'][0])

    def test_sqlite_metadata_whitelist_drops_unknown_fields_and_wrong_types(self):
        error = sqlite3.OperationalError('secret')
        error.sqlite_errorcode, error.sqlite_errorname = 5, 'SQLITE_BUSY_password'
        error.storage_context = {'transaction_stage': 'COMMIT', 'journal_mode': 'wal', 'sql': 'secret',
                                 'busy_timeout_ms': True, 'in_transaction': True, 'path': '/secret'}
        detail = RuntimeDiagnostics._detail(error)
        item = detail['stack'][0]
        self.assertEqual(item['sqlite_errorcode'], 5)
        self.assertNotIn('sqlite_errorname', item)
        self.assertEqual(item['storage_context'], {'transaction_stage': 'COMMIT', 'journal_mode': 'wal', 'in_transaction': True})
        self.assertNotIn('secret', str(detail))

    def test_chained_sqlite_error_survives_runtime_json(self):
        try:
            try:
                with closing(sqlite3.connect(':memory:')) as db:
                    db.execute('SELECT * FROM private_name')
            except sqlite3.Error as e:
                raise TraderError('E_DSV_STORE', 'store failed') from e
        except TraderError as error:
            with tempfile.TemporaryDirectory() as directory:
                import json
                path = Path(directory) / 'diagnostics.jsonl'
                RuntimeDiagnostics(path).report(error, stage='SDK_ROUND')
                item = json.loads(path.read_text())['exception']['stack'][1]
                self.assertEqual(item['sqlite_errorname'], 'SQLITE_ERROR')
                self.assertNotIn('private_name', path.read_text())


if __name__ == '__main__':
    unittest.main()
