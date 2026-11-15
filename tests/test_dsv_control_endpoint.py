"""Real control validator/codec; explicit session, SDK and pipe doubles only."""

import ast
import copy
from pathlib import Path
import subprocess
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from online_trader.contracts.codec import encode_frame
from online_trader.dsv_runtime import control_endpoint as control
from online_trader.errors import TraderError


def request(op='snapshot', body=None):
    return {'protocol': control.PROTOCOL, 'op': op, 'body': {} if body is None else body}


class Pipe:
    def __init__(self, message):
        self.message, self.replies = message, []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def recv(self, **kwargs): return self.message
    def send(self, message, **kwargs):
        encode_frame(message)
        self.replies.append(message)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.sdk = Mock()
        self.session.command_receipt.return_value = None

    def dispatch(self, op, body=None):
        return control.handle_control(self.session, request(op, body), sdk=self.sdk)

    def test_existing_client_wire_label_is_preserved(self):
        self.assertEqual(control.PROTOCOL, 'DSV_OFFLINE/1')
        for op in control.CONTROL_OPS | {'reply', 'error'}:
            control.validate_control_message(request(op))

    def test_online_validator_rejects_replay_and_disconnect(self):
        for op in ('round', 'disconnect'):
            with self.subTest(op=op), self.assertRaises(TraderError):
                control.validate_control_message(request(op))

    def test_malformed_envelopes_have_typed_error(self):
        for value in (None, [], {}, request(body=[]), request() | {'extra': True},
                      request() | {'op': []}, request() | {'protocol': 'OTHER'}):
            with self.subTest(value=value), self.assertRaises(TraderError):
                control.validate_control_message(value)

    def test_direct_handler_rejects_injection_before_owner_access(self):
        for op in ('round', 'disconnect', 'reply', 'error'):
            with self.subTest(op=op), self.assertRaises(TraderError):
                self.dispatch(op)
        self.assertEqual(self.session.mock_calls, [])
        self.assertEqual(self.sdk.mock_calls, [])

    def test_detail_snapshot_and_evidence_are_read_only(self):
        value = {'orders': {'o': {'send_state': 'UNKNOWN'}}, 'symbols': {}}
        self.session.snapshot.side_effect = lambda: copy.deepcopy(value)
        self.session.workbench_evidence.return_value = {'history_status': 'DEGRADED'}
        result = self.dispatch('snapshot', {'detail': True, 'workbench_evidence': True})
        self.assertTrue(result['sdk_connection'])
        self.assertEqual(result['orders'], value['orders'])
        self.assertEqual(result['history_status'], 'DEGRADED')
        self.assertNotIn('sdk_connection', value)
        self.assertEqual(self.sdk.mock_calls, [])

    def test_compact_projection_delegates_terminal_policy(self):
        value = {'orders': {'a': {'tag': 'active'}, 'b': {'tag': 'done'}},
                 'symbols': {'X': {'position': {'fills': [1]}, 'tick': {'last': '1'}}}}
        self.session.snapshot.side_effect = lambda: copy.deepcopy(value)
        module = ModuleType('online_trader.dsv_runtime.controller')
        module.terminal = Mock(side_effect=lambda order: order['tag'] == 'done')
        with patch.dict(sys.modules, {module.__name__: module}):
            result = self.dispatch('snapshot')
        self.assertEqual(set(result['orders']), {'a'})
        self.assertEqual(module.terminal.call_count, 2)
        self.assertNotIn('tick', result['symbols']['X'])
        self.assertNotIn('fills', result['symbols']['X']['position'])
        self.assertIn('fills', value['symbols']['X']['position'])

    def test_new_command_checks_scope_then_receipt_then_day(self):
        body = {'id': 'cmd', 'action': 'STOP', 'expected_scope': {'account_key': 'DEV:a'}, 'expected_day': '2026-11-16'}
        self.dispatch('command', body)
        self.assertEqual([call[0] for call in self.session.mock_calls],
                         ['verify_scope', 'command_receipt', 'verify_day'])
        self.sdk.command.assert_called_once_with(self.session, body)
        self.session.command.assert_not_called()

    def test_duplicate_command_does_not_recheck_new_day(self):
        self.session.command_receipt.return_value = {'receipt': 'old'}
        self.dispatch('command', {'id': 'cmd', 'action': 'STOP'})
        self.session.verify_day.assert_not_called()
        self.sdk.command.assert_called_once()

    def test_wrong_scope_cannot_query_receipt_or_execute(self):
        self.session.verify_scope.side_effect = TraderError('E_DSV_SCOPE')
        with self.assertRaises(TraderError):
            self.dispatch('command', {'id': 'cmd'})
        self.session.command_receipt.assert_not_called()
        self.sdk.command.assert_not_called()

    def test_wrong_day_cannot_execute_new_command(self):
        self.session.verify_day.side_effect = TraderError('E_DSV_SCOPE')
        with self.assertRaises(TraderError):
            self.dispatch('command', {'id': 'cmd'})
        self.sdk.command.assert_not_called()

    def test_receipt_checks_scope_and_remains_read_only(self):
        self.session.command_receipt.return_value = {'id': 'cmd'}
        self.assertEqual(self.dispatch('receipt', {'id': 'cmd'}), {'id': 'cmd'})
        self.session.verify_scope.assert_called_once_with(None)
        self.assertEqual(self.sdk.mock_calls, [])

    def test_offline_control_uses_same_command_ordering(self):
        body = {'id': 'cmd', 'action': 'STOP'}
        control.handle_control(self.session, request('command', body))
        self.session.command.assert_called_once_with(body)

    def test_shutdown_not_implemented_as_implicit_close(self):
        with self.assertRaises(TraderError):
            self.dispatch('shutdown')
        self.assertEqual(self.session.mock_calls, [])
        self.assertEqual(self.sdk.mock_calls, [])

    def exchange(self, message, handler):
        pipe = Pipe(message)
        listener = Mock()
        listener.accept.return_value = pipe
        control.serve_control_request(listener, handler, timeout=0.01)
        listener.accept.assert_called_once_with(timeout=0.01)
        return pipe.replies

    def test_server_rejects_qa_even_when_transport_validator_bypassed(self):
        handler = Mock()
        for op in ('round', 'disconnect', 'reply', 'error'):
            replies = self.exchange(request(op), handler)
            self.assertEqual(replies[0]['op'], 'error')
            self.assertEqual(replies[0]['body']['code'], 'E_DSV_PROTOCOL')
        handler.assert_not_called()

    def test_server_returns_successful_bounded_reply(self):
        handler = Mock(return_value={'sequence': 1})
        replies = self.exchange(request('snapshot'), handler)
        self.assertEqual(replies, [request('reply', {'value': {'sequence': 1}})])
        handler.assert_called_once()

    def test_generic_key_error_does_not_export_payload_text(self):
        replies = self.exchange(request('command'), Mock(side_effect=KeyError('secret-test-value')))
        self.assertEqual(replies[0]['op'], 'error')
        self.assertNotIn('secret-test-value', repr(replies))

    def test_typed_refusal_preserves_internal_code(self):
        replies = self.exchange(request(), Mock(side_effect=TraderError('E_DSV_SCOPE', 'Scope changed')))
        self.assertEqual(replies[0]['body'], {'code': 'E_DSV_SCOPE', 'message': 'Scope changed'})

    def test_oversized_reply_becomes_error_not_partial_snapshot(self):
        replies = self.exchange(request(), Mock(return_value='x' * 1_048_576))
        self.assertEqual(replies[0]['op'], 'error')
        self.assertEqual(replies[0]['body']['code'], 'E_FRAME')

    def test_transport_timeout_has_no_owner_side_effect(self):
        listener, handler = Mock(), Mock()
        listener.accept.side_effect = TraderError('E_PIPE_TIMEOUT')
        control.serve_control_request(listener, handler)
        handler.assert_not_called()

    def test_storage_error_is_not_swallowed_as_transport_timeout(self):
        listener = Mock()
        listener.accept.side_effect = TraderError('E_DSV_STORE')
        with self.assertRaises(TraderError):
            control.serve_control_request(listener, Mock())

    def test_endpoint_import_does_not_load_execution_or_offline_driver(self):
        script = '''import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("online_trader.kernel", "online_trader.gateway", "tqsdk", "PySide6")) or fullname in ("online_trader.dsv_runtime.runner", "online_trader.dsv_runtime.offline_endpoint", "online_trader.transport.named_pipe"):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Deny())
import online_trader.dsv_runtime.control_endpoint
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_startup_no_longer_imports_offline_endpoint(self):
        path = Path(control.__file__).with_name('sdk_startup.py')
        imports = [node.module for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
                   if isinstance(node, ast.ImportFrom)]
        self.assertNotIn('online_trader.dsv_runtime.offline_endpoint', imports)
        self.assertIn('online_trader.dsv_runtime.control_endpoint', imports)


if __name__ == '__main__':
    unittest.main()
