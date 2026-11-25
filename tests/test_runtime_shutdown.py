"""Unit and integration tests for strict maintenance shutdown proof verification."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from online_trader.errors import TraderError
from online_trader.runtime.shutdown import (
    _is_valid_maintenance_completion,
    maintenance_stop_runtime_and_wait,
)


def _sample_view(run_dir: Path, *, boot_id='boot-1', account_key='SIM:KQ:0001'):
    return {
        'account_key': account_key,
        'kernel_boot_id': boot_id,
        'configuration': {
            'config_revision': '3',
            'run_directory': str(run_dir),
        },
    }


def _completion_payload(*, command_id, boot_id='boot-1', account_key='SIM:KQ:0001',
                        state='COMPLETED', maintenance_allowed=True, blockers=()):
    return {
        'schema_version': 'DSV_MAINTENANCE_COMPLETION/1',
        'runtime_boot_id': boot_id,
        'account_key': account_key,
        'command_id': command_id,
        'reason': 'SWITCH_ACCOUNT',
        'state': state,
        'responsibility': {
            'maintenance_allowed': maintenance_allowed,
            'blockers': list(blockers),
        },
        'completed_at_utc': '2026-11-25T01:00:00Z',
    }


class RuntimeShutdownProofTests(unittest.TestCase):
    def test_valid_completion_proof_requires_matching_identity_and_completed_state(self):
        proof = _completion_payload(command_id='maint-1')
        self.assertTrue(_is_valid_maintenance_completion(
            proof, command_id='maint-1', boot_id='boot-1', account_key='SIM:KQ:0001',
        ))
        self.assertFalse(_is_valid_maintenance_completion(
            None, command_id='maint-1', boot_id='boot-1', account_key='SIM:KQ:0001',
        ))
        self.assertFalse(_is_valid_maintenance_completion(
            _completion_payload(command_id='maint-other'),
            command_id='maint-1', boot_id='boot-1', account_key='SIM:KQ:0001',
        ))
        self.assertFalse(_is_valid_maintenance_completion(
            _completion_payload(command_id='maint-1', boot_id='boot-other'),
            command_id='maint-1', boot_id='boot-1', account_key='SIM:KQ:0001',
        ))
        self.assertFalse(_is_valid_maintenance_completion(
            _completion_payload(command_id='maint-1', account_key='SIM:KQ:OTHER'),
            command_id='maint-1', boot_id='boot-1', account_key='SIM:KQ:0001',
        ))
        self.assertFalse(_is_valid_maintenance_completion(
            _completion_payload(command_id='maint-1', state='ABORTED', maintenance_allowed=False),
            command_id='maint-1', boot_id='boot-1', account_key='SIM:KQ:0001',
        ))

    def test_allows_when_no_runtime_is_active(self):
        with (
            patch('online_trader.runtime.shutdown.matching_runtime_instance', return_value=None),
            patch('online_trader.runtime.shutdown.inspect_runtime_for_exit', return_value=(None, None, None)),
            patch('online_trader.runtime.shutdown.runtime_owner_exists', return_value=False),
        ):
            ok, msg = maintenance_stop_runtime_and_wait(r'\\.\pipe\none', reason='UPDATE')
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_rejects_when_process_exits_without_completion_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            run_dir.mkdir()
            view = _sample_view(run_dir)
            submitted = []

            class FakeWorkbenchClient:
                def __init__(self, _pipe): pass
                def request(self, action, body):
                    submitted.append((action, body))
                    return {'status': 'SUCCEEDED'}

            with (
                patch('online_trader.runtime.shutdown.matching_runtime_instance', return_value=None),
                patch('online_trader.runtime.shutdown.RuntimeWorkbenchClient', FakeWorkbenchClient),
                patch('online_trader.runtime.shutdown.pid_alive', return_value=False),
                patch('online_trader.runtime.shutdown.runtime_owner_exists', return_value=False),
                patch('online_trader.runtime.shutdown._process_creation_time', return_value=None),
            ):
                ok, msg = maintenance_stop_runtime_and_wait(
                    r'\\.\pipe\test', reason='SWITCH_ACCOUNT', pid=1234, view=view, timeout=2.0,
                )
        self.assertFalse(ok)
        self.assertIn('未能确认无责任受控停机证明', msg)
        self.assertEqual(len(submitted), 1)

    def test_rejects_when_submit_fails_even_if_process_dies(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            run_dir.mkdir()
            view = _sample_view(run_dir)

            class FailingWorkbenchClient:
                def __init__(self, _pipe): pass
                def request(self, _action, _body):
                    raise OSError('broken pipe after crash')

            with (
                patch('online_trader.runtime.shutdown.matching_runtime_instance', return_value=None),
                patch('online_trader.runtime.shutdown.RuntimeWorkbenchClient', FailingWorkbenchClient),
                patch('online_trader.runtime.shutdown._wait_runtime_exit', return_value=(True, None)),
                patch('online_trader.runtime.shutdown._process_creation_time', return_value=None),
            ):
                ok, msg = maintenance_stop_runtime_and_wait(
                    r'\\.\pipe\test', reason='UPDATE', pid=1234, view=view, timeout=2.0,
                )
        self.assertFalse(ok)
        self.assertIn('未完成安全校验', msg)

    def test_succeeds_only_with_matching_completed_proof_after_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            run_dir.mkdir()
            view = _sample_view(run_dir)

            class CompletingWorkbenchClient:
                def __init__(self, _pipe): pass
                def request(self, _action, body):
                    (run_dir / 'maintenance-completion.json').write_text(
                        json.dumps(_completion_payload(command_id=body['command_id'])),
                        encoding='utf-8',
                    )
                    return {'status': 'SUCCEEDED'}

            with (
                patch('online_trader.runtime.shutdown.matching_runtime_instance', return_value=None),
                patch('online_trader.runtime.shutdown.RuntimeWorkbenchClient', CompletingWorkbenchClient),
                patch('online_trader.runtime.shutdown.pid_alive', return_value=False),
                patch('online_trader.runtime.shutdown.runtime_owner_exists', return_value=False),
                patch('online_trader.runtime.shutdown._process_creation_time', return_value=None),
            ):
                ok, msg = maintenance_stop_runtime_and_wait(
                    r'\\.\pipe\test', reason='SWITCH_ACCOUNT', pid=1234, view=view, timeout=2.0,
                )
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_aborted_projection_returns_immediately_without_waiting_full_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            run_dir.mkdir()
            view = _sample_view(run_dir)
            issued_ids = []

            class FakeWorkbenchClient:
                def __init__(self, _pipe): pass
                def request(self, _action, body):
                    issued_ids.append(body['command_id'])
                    return {'status': 'SUCCEEDED'}

            class FakeViewClient:
                def __init__(self, _pipe): pass
                def read(self):
                    return {
                        'kind': 'BASELINE',
                        'body': {
                            'view': {
                                'maintenance_completion': {
                                    'command_id': issued_ids[-1],
                                    'reason': 'SWITCH_ACCOUNT',
                                    'state': 'ABORTED',
                                    'blockers': ['ACTIVE_PROTECTED_POSITIONS'],
                                    'completed_at_utc': '2026-11-25T01:00:01Z',
                                },
                            },
                        },
                    }

            started = time.monotonic()
            with (
                patch('online_trader.runtime.shutdown.matching_runtime_instance', return_value={'pid': 1234, 'pipe': r'\\.\pipe\test'}),
                patch('online_trader.runtime.shutdown.RuntimeWorkbenchClient', FakeWorkbenchClient),
                patch('online_trader.runtime.shutdown.RuntimeViewClient', FakeViewClient),
                patch('online_trader.runtime.shutdown.pid_alive', return_value=True),
                patch('online_trader.runtime.shutdown.wait_pid_exit', return_value=False),
                patch('online_trader.runtime.shutdown.runtime_owner_exists', return_value=True),
                patch('online_trader.runtime.shutdown._process_creation_time', return_value=None),
            ):
                ok, msg = maintenance_stop_runtime_and_wait(
                    r'\\.\pipe\test', reason='SWITCH_ACCOUNT', pid=1234, view=view, timeout=15.0,
                )
            elapsed = time.monotonic() - started
        self.assertFalse(ok)
        self.assertLess(elapsed, 2.0)
        self.assertIn('ACTIVE_PROTECTED_POSITIONS', msg)

    def test_runtime_failure_file_aborts_wait_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'run'
            run_dir.mkdir()
            view = _sample_view(run_dir)

            class FakeWorkbenchClient:
                def __init__(self, _pipe): pass
                def request(self, _action, _body):
                    (run_dir / 'runtime-failure.json').write_text(
                        json.dumps({'runtime_boot_id': 'boot-1', 'error_code': 'E_DSV_OWNER'}),
                        encoding='utf-8',
                    )
                    return {'status': 'SUCCEEDED'}

            started = time.monotonic()
            with (
                patch('online_trader.runtime.shutdown.matching_runtime_instance', return_value={'pid': 1234, 'pipe': r'\\.\pipe\test'}),
                patch('online_trader.runtime.shutdown.RuntimeWorkbenchClient', FakeWorkbenchClient),
                patch('online_trader.runtime.shutdown.pid_alive', return_value=True),
                patch('online_trader.runtime.shutdown.wait_pid_exit', return_value=False),
                patch('online_trader.runtime.shutdown.runtime_owner_exists', return_value=True),
                patch('online_trader.runtime.shutdown._process_creation_time', return_value=None),
            ):
                ok, msg = maintenance_stop_runtime_and_wait(
                    r'\\.\pipe\test', reason='UPDATE', pid=1234, view=view, timeout=15.0,
                )
            elapsed = time.monotonic() - started
        self.assertFalse(ok)
        self.assertLess(elapsed, 2.0)
        self.assertIn('E_DSV_OWNER', msg)


if __name__ == '__main__':
    unittest.main()
