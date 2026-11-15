"""Real diagnostic sink and filesystem; no SDK, account or transaction store."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from online_trader.dsv_runtime.runtime_diagnostics import RuntimeDiagnostics
from online_trader.errors import TraderError


def error(message='password=synthetic-secret', cls=ValueError):
    try:
        raise cls(message)
    except Exception as caught:
        return caught


class RuntimeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'runtime-diagnostics.jsonl'
        self.sink = RuntimeDiagnostics(self.path)

    def rows(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_stack_is_persisted_without_raw_secret_or_local_path(self):
        self.sink.report(error(), stage='SDK_ROUND')
        row = self.rows()[0]
        self.assertEqual(row['kind'], 'EXCEPTION')
        self.assertEqual(row['exception']['stack'][0]['type'], 'ValueError')
        self.assertTrue(row['exception']['stack'][0]['frames'])
        raw = self.path.read_text()
        self.assertNotIn('synthetic-secret', raw)
        self.assertNotIn(self.temp.name, raw)
        self.assertNotIn(str(Path(__file__).parent), raw)

    def test_recovery_appends_and_does_not_overwrite_first_failure(self):
        self.sink.report(error(), stage='SDK_ROUND')
        before = self.path.read_bytes()
        identity = self.sink.status()['last_exception_id']
        self.sink.recovered()
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertEqual([row['kind'] for row in self.rows()], ['EXCEPTION', 'RECOVERED'])
        self.assertEqual(self.sink.status()['last_exception_id'], identity)

    def test_repeated_failures_are_counted_at_powers_of_two_then_recovery(self):
        caught = error()
        for _ in range(10):
            self.sink.report(caught, stage='SDK_ROUND')
        self.assertEqual([row['occurrences'] for row in self.rows()], [1, 2, 4, 8])
        self.sink.recovered()
        self.assertEqual(self.rows()[-1]['occurrences'], 10)
        self.assertEqual(self.rows()[-1]['kind'], 'RECOVERED')

    def test_new_error_summarizes_unsaved_repeat_count(self):
        caught = error('first')
        for _ in range(3):
            self.sink.report(caught, stage='SDK_ROUND')
        self.sink.report(error('second'), stage='SDK_ROUND')
        self.assertEqual([row['kind'] for row in self.rows()], ['EXCEPTION', 'EXCEPTION', 'SUMMARY', 'EXCEPTION'])
        self.assertEqual(self.rows()[-2]['occurrences'], 3)
        self.assertNotEqual(self.rows()[0]['episode_id'], self.rows()[-1]['episode_id'])

    def test_separate_stages_do_not_coalesce(self):
        caught = error()
        self.sink.report(caught, stage='SDK_ROUND')
        self.sink.report(caught, stage='STARTUP_OR_CLEANUP')
        self.assertEqual([r['occurrences'] for r in self.rows()], [1, 1])
        self.assertEqual([r['stage'] for r in self.rows()], ['SDK_ROUND', 'STARTUP_OR_CLEANUP'])

    def test_no_error_does_not_create_a_file(self):
        self.sink.recovered()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.sink.status()['state'], 'OK')

    def test_io_failure_is_visible_and_never_raised_into_caller(self):
        with patch.object(Path, 'open', side_effect=OSError('private-path-and-secret')):
            self.sink.report(error(), stage='SDK_ROUND')
        self.assertEqual(self.sink.status()['state'], 'DEGRADED')
        self.assertEqual(self.sink.status()['write_failures'], 1)
        self.assertNotIn('private-path', repr(self.sink.status()))
        self.sink.recovered()
        self.assertEqual(self.sink.status()['state'], 'OK')
        self.assertEqual(self.rows()[0]['kind'], 'RECOVERED')
        self.assertTrue(self.rows()[0]['exception']['stack'])

    def test_formatter_failure_is_noncritical(self):
        with patch.object(self.sink, '_encode', side_effect=RuntimeError('synthetic formatting failure')):
            self.sink.report(error(), stage='SDK_ROUND')
        self.assertEqual(self.sink.status()['state'], 'DEGRADED')

    def test_broken_exception_string_does_not_mask_original_error(self):
        class BrokenText(Exception):
            def __str__(self):
                raise ValueError('formatting must not escape')
        self.sink.report(BrokenText(), stage='SDK_ROUND')
        self.assertEqual(self.sink.status()['state'], 'DEGRADED')

    def test_failed_recovery_is_retried(self):
        self.sink.report(error(), stage='SDK_ROUND')
        with patch.object(Path, 'open', side_effect=PermissionError()):
            self.sink.recovered()
        self.assertEqual(len(self.rows()), 1)
        self.sink.recovered()
        self.assertEqual([row['kind'] for row in self.rows()], ['EXCEPTION', 'RECOVERED'])
        self.sink.recovered()
        self.assertEqual(len(self.rows()), 2)

    def test_rotation_is_bounded_and_preserves_unrelated_files(self):
        self.sink = RuntimeDiagnostics(self.path, max_bytes=1024, backups=2)
        untouched = self.path.parent / 'facts.db'
        untouched.write_bytes(b'untouched')
        for number in range(20):
            self.sink.report(error(str(number)), stage='SDK_ROUND')
        files = list(self.path.parent.glob('runtime-diagnostics.jsonl*'))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(self.path.with_name(self.path.name + '.2').is_file())
        for path in files:
            self.assertLessEqual(path.stat().st_size, 1024)
            for line in path.read_text().splitlines():
                json.loads(line)
        self.assertEqual(untouched.read_bytes(), b'untouched')

    def test_incomplete_tail_does_not_corrupt_the_next_row(self):
        self.path.write_bytes(b'{"partial":')
        self.sink.report(error(), stage='SDK_ROUND')
        lines = self.path.read_text().splitlines()
        self.assertEqual(lines[0], '{"partial":')
        self.assertEqual(json.loads(lines[1])['kind'], 'EXCEPTION')

    def test_exception_chain_and_codes_are_sanitized(self):
        try:
            try:
                raise OSError('credentials=hidden')
            except OSError as cause:
                raise TraderError('E_DSV_STORE', 'account=private') from cause
        except TraderError as caught:
            self.sink.report(caught, stage='SDK_ROUND')
        stack = self.rows()[0]['exception']['stack']
        self.assertEqual([item['type'] for item in stack], ['TraderError', 'OSError'])
        self.assertEqual(stack[0]['code'], 'E_DSV_STORE')
        self.assertNotIn('credentials=', self.path.read_text())
        self.assertNotIn('account=private', self.path.read_text())

    def test_unknown_error_code_is_not_exported_as_arbitrary_text(self):
        self.sink.report(TraderError('secret=123', 'private'), stage='SDK_ROUND')
        self.assertIsNone(self.rows()[0]['exception']['stack'][0]['code'])
        self.assertNotIn('secret=123', self.path.read_text())

    def test_existing_history_survives_a_new_sink_instance(self):
        self.sink.report(error('one'), stage='SDK_ROUND')
        RuntimeDiagnostics(self.path).report(error('two'), stage='SDK_ROUND')
        self.assertEqual(len(self.rows()), 2)

    def test_unexpected_detail_fields_are_not_exported(self):
        payload = {'exception_id': 'exception-test', 'fingerprint': 'a' * 64, 'stack': [],
                   'message': 'unexpected-secret', 'locals': {'password': 'must-not-export'}}
        with patch('online_trader.dsv_runtime.runtime_diagnostics.exception_record', return_value=payload):
            self.sink.report(error(), stage='SDK_ROUND')
        self.assertNotIn('unexpected-secret', self.path.read_text())
        self.assertNotIn('must-not-export', self.path.read_text())

    def test_invalid_rotation_configuration_is_rejected(self):
        for values in ({'max_bytes': 1}, {'max_bytes': True}, {'backups': 0}, {'backups': 17}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RuntimeDiagnostics(self.path, **values)


if __name__ == '__main__':
    unittest.main()
