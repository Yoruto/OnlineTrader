"""Real wrappers and SDK owner loop; explicit in-memory transport/owner doubles.

No SDK connection, account lock or Windows named pipe is constructed.
"""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from online_trader.dsv_runtime import control_endpoint as control, offline_endpoint as offline, sdk_startup
from online_trader.errors import TraderError


def request(op, body=None):
    return {'protocol': control.PROTOCOL, 'op': op, 'body': {} if body is None else body}


class Pipe:
    def __init__(self, message, replies):
        self.message, self.replies = message, replies
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def recv(self, **kwargs): return self.message
    def send(self, message, **kwargs): self.replies.append(message)


class EndpointOwnerTests(unittest.TestCase):
    def test_offline_replay_still_calls_only_offline_owner(self):
        session = Mock()
        body = {'id': 'round-1'}
        offline._handle(session, request('round', body))
        session.apply_round.assert_called_once_with(body)
        session.command.assert_not_called()

    def test_offline_disconnect_still_supported(self):
        session = Mock()
        offline._handle(session, request('disconnect'))
        session.disconnect.assert_called_once_with()

    def test_internal_legacy_op_body_call_stays_compatible(self):
        session = Mock()
        offline._handle(session, {'op': 'round', 'body': {'id': 'r'}})
        session.apply_round.assert_called_once_with({'id': 'r'})
        with self.assertRaises(TraderError):
            offline.validate_offline_message({'op': 'round', 'body': {}})

    def test_legacy_helper_cannot_inject_when_bound_to_sdk(self):
        session, sdk = Mock(), Mock()
        for op in ('round', 'disconnect'):
            for message in (request(op), {'op': op, 'body': {}}):
                with self.subTest(message=message), self.assertRaises(TraderError):
                    offline._handle(session, message, sdk=sdk)
        self.assertEqual(session.mock_calls, [])
        self.assertEqual(sdk.mock_calls, [])

    def test_offline_and_online_validators_have_different_allowlists(self):
        offline.validate_offline_message(request('round'))
        with self.assertRaises(TraderError):
            control.validate_control_message(request('round'))

    def test_offline_transport_dispatches_round_without_online_mode_flag(self):
        replies = []
        listener = Mock()
        listener.accept.return_value = Pipe(request('round'), replies)
        handler = Mock(return_value={'type': 'ROUND'})
        offline.serve_request(listener, handler)
        handler.assert_called_once()
        self.assertEqual(replies[0]['body']['value'], {'type': 'ROUND'})

    def test_control_wrapper_retains_receipt_day_idempotency(self):
        session, sdk = Mock(), Mock()
        session.command_receipt.return_value = {'id': 'known'}
        body = {'id': 'known', 'action': 'STOP'}
        offline._handle(session, request('command', body), sdk=sdk)
        session.verify_scope.assert_called_once_with(None)
        session.verify_day.assert_not_called()
        sdk.command.assert_called_once_with(session, body)

    def test_sdk_loop_rejects_injected_round_then_handles_snapshot_and_shutdown(self):
        self.run_loop(initial_running=False, deny_shutdown=False)

    def test_sdk_loop_keeps_shutdown_gate_until_owner_is_stopped(self):
        self.run_loop(initial_running=True, deny_shutdown=True)

    def run_loop(self, *, initial_running, deny_shutdown):
        with tempfile.TemporaryDirectory() as directory:
            state = {'synchronized': True, 'running': initial_running, 'trading_day': '2026-11-16',
                     'scope': {'account_key': 'DEV:control'}, 'orders': {}, 'symbols': {}}
            session = SimpleNamespace(_failed=False, snapshot=lambda: dict(state),
                                      prepare_shutdown=lambda: {'ready': True})
            adapter = SimpleNamespace(flat_confirmed=True, command=Mock())
            queue = [request('round'), request('disconnect'), request('reply'),
                     request('snapshot', {'detail': True})]
            if deny_shutdown:
                queue.append(request('shutdown'))
            queue.append(request('shutdown'))
            calls = []
            def step(*args, **kwargs):
                calls.append('step')
                if not queue:
                    raise AssertionError('Owner failed to finish on reviewed shutdown')
                if deny_shutdown and len(queue) == 1:
                    state['running'] = False
            adapter.step = step
            replies = []
            listener = Mock()
            listener.accept.side_effect = lambda **kwargs: Pipe(queue.pop(0), replies)
            result = sdk_startup._serve(session, adapter, listener, object(), Path(directory) / 'status.json', lambda: True)
            self.assertEqual(result, 0)
            self.assertEqual([r['op'] for r in replies[:4]], ['error', 'error', 'error', 'reply'])
            self.assertTrue(replies[3]['body']['value']['sdk_connection'])
            if deny_shutdown:
                self.assertEqual(replies[-2]['body']['code'], 'E_DSV_SDK_SHUTDOWN')
            self.assertEqual(replies[-1]['op'], 'reply')
            self.assertEqual(len(calls), len(replies))
            adapter.command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
