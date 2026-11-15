"""Real _serve orchestration with explicit SDK/session/IPC test doubles.

No actual connection, account owner, Journal or Windows pipe is constructed.
"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from online_trader.dsv_runtime import sdk_startup
from online_trader.dsv_runtime.runtime_diagnostics import RuntimeDiagnostics
from online_trader.errors import TraderError


class RuntimeDiagnosticServeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.status = Path(self.temp.name) / 'status.json'
        self.log = self.status.with_name('runtime-diagnostics.jsonl')
        self.state = {'synchronized': True, 'running': False, 'trading_day': '2026-11-13',
                      'scope': {'account_key': 'DEV:diagnostic-test'}}
        self.session = SimpleNamespace(_failed=False, snapshot=lambda: dict(self.state),
                                       prepare_shutdown=lambda: {'ready': True})
        self.adapter = SimpleNamespace(flat_confirmed=True, command=Mock())
        self.polls = 0
        self.history_at_poll = []

    def run_loop(self, actions, *, diagnostics=None):
        actions = list(actions)
        def step(*args, **kwargs):
            action = actions.pop(0)
            self.state['synchronized'] = action is None
            if isinstance(action, BaseException):
                raise action
        def poll(_listener, handler, *, timeout):
            self.polls += 1
            self.history_at_poll.append(self.log.read_text() if self.log.exists() else '')
            if not actions:
                handler({'op': 'shutdown'})
        self.adapter.step = step
        with patch.object(sdk_startup, 'serve_request', side_effect=poll), patch.object(sdk_startup.time, 'sleep'):
            if diagnostics is None:
                return sdk_startup._serve(self.session, self.adapter, object(), object(), self.status, lambda: True)
            return sdk_startup._serve(self.session, self.adapter, object(), object(), self.status, lambda: True,
                                      diagnostics=diagnostics)

    def test_failure_remains_after_status_recovers(self):
        self.assertEqual(self.run_loop([ValueError('password=must-not-escape'), None]), 0)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([row['kind'] for row in rows], ['EXCEPTION', 'RECOVERED'])
        self.assertTrue(rows[0]['exception']['stack'][0]['frames'])
        self.assertNotIn('must-not-escape', self.log.read_text())
        status = json.loads(self.status.read_text())
        self.assertIsNone(status['error_code'])
        self.assertEqual(status['phase'], 'STOPPED')
        self.assertEqual(status['diagnostics']['last_exception_id'], rows[0]['episode_id'])
        self.adapter.command.assert_not_called()

    def test_critical_storage_failure_is_logged_then_reraised_unchanged(self):
        failure = TraderError('E_DSV_STORE', 'synthetic critical failure')
        self.session._failed = True
        with self.assertRaises(TraderError) as caught:
            self.run_loop([failure])
        self.assertIs(caught.exception, failure)
        row = json.loads(self.log.read_text().splitlines()[0])
        self.assertEqual(row['exception']['stack'][0]['code'], 'E_DSV_STORE')
        self.assertEqual(self.polls, 0)

    def test_log_failure_does_not_change_loop_or_dispatch_commands(self):
        sink = RuntimeDiagnostics(self.log)
        with patch.object(sink, '_encode', side_effect=OSError('disk unavailable')):
            self.assertEqual(self.run_loop([RuntimeError('failure'), None], diagnostics=sink), 0)
        status = json.loads(self.status.read_text())
        self.assertEqual(status['diagnostics']['state'], 'DEGRADED')
        self.assertEqual(status['entry_authority'], 'DISARMED')
        self.assertEqual(self.polls, 2)
        self.adapter.command.assert_not_called()

    def test_log_failure_does_not_mask_critical_storage_failure(self):
        failure = TraderError('E_DSV_STORE', 'original')
        self.session._failed = True
        sink = RuntimeDiagnostics(self.log)
        with patch.object(sink, '_encode', side_effect=RuntimeError('logger failure')):
            with self.assertRaises(TraderError) as caught:
                self.run_loop([failure], diagnostics=sink)
        self.assertIs(caught.exception, failure)

    def test_success_without_native_sync_is_not_reported_as_recovery(self):
        self.run_loop([ValueError('failure'), 'WAITING_FOR_SYNC', None])
        self.assertNotIn('RECOVERED', self.history_at_poll[1])
        self.assertIn('RECOVERED', self.history_at_poll[2])

    def test_no_exception_leaves_no_diagnostic_file(self):
        self.assertEqual(self.run_loop([None]), 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(json.loads(self.status.read_text())['diagnostics']['state'], 'OK')


if __name__ == '__main__':
    unittest.main()
