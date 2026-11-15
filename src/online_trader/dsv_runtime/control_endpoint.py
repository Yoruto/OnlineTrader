"""Shared control IPC, with no replay driver or execution owner at import time.

Keep the existing wire label for client compatibility. The label does not grant
replay capability: the online validator and dispatcher admit only control ops.
"""

from online_trader.contracts.codec import encode_frame
from online_trader.errors import TraderError


PROTOCOL = 'DSV_OFFLINE/1'  # Wire compatibility only; not an online replay mode.
CONTROL_OPS = frozenset({'snapshot', 'command', 'receipt', 'shutdown'})
_TIMEOUT = frozenset({'E_TIMEOUT', 'E_PIPE', 'E_PIPE_CLOSED', 'E_PIPE_TIMEOUT'})
_IGNORED = _TIMEOUT | {'E_AUTH', 'E_DSV_PROTOCOL', 'E_FRAME', 'E_VERSION', 'E_SCHEMA'}


def validate_envelope(message, operations):
    if (type(message) is not dict or message.keys() != {'protocol', 'op', 'body'}
            or message['protocol'] != PROTOCOL or type(message['body']) is not dict
            or type(message['op']) is not str or message['op'] not in operations):
        raise TraderError('E_DSV_PROTOCOL', 'Unsupported DSV control envelope')


def validate_control_message(message):
    """Validate either direction on the transport, without admitting QA inputs."""
    validate_envelope(message, CONTROL_OPS | {'reply', 'error'})


def validate_control_request(message):
    """A response envelope must never be dispatched as an owner command."""
    validate_envelope(message, CONTROL_OPS)


def snapshot(session, *, detail=False):
    state = session.snapshot()
    if not detail:
        # Use the original quantity/UNKNOWN rules, not a second terminal policy.
        from online_trader.dsv_runtime.controller import terminal
        state['orders'] = {key: order for key, order in state['orders'].items() if not terminal(order)}
        for row in state['symbols'].values():
            if row['position']:
                row['position'].pop('fills', None)
            row.pop('tick', None)
    return state


def handle_control(session, request, *, sdk=None):
    """Dispatch user control only. The owning loop handles shutdown separately."""
    validate_control_request(request)
    body, op = request['body'], request['op']
    if op == 'snapshot':
        result = snapshot(session, detail=body.get('detail') is True)
        if body.get('workbench_evidence') is True:
            result.update(session.workbench_evidence())
        if sdk is not None:
            result['sdk_connection'] = True
        return result
    if op == 'receipt':
        session.verify_scope(body.get('expected_scope'))
        return session.command_receipt(body['id'])
    if op == 'command':
        session.verify_scope(body.get('expected_scope'))
        previous = session.command_receipt(body['id'])
        if previous is None:
            session.verify_day(body.get('expected_day'))
        return session.command(body) if sdk is None else sdk.command(session, body)
    raise TraderError('E_DSV_PROTOCOL', 'Shutdown must be reviewed by the execution owner')


def serve_validated_request(listener, handle, *, request_validator, timeout=0.25):
    """One serialized request; validate again even if a transport was substituted.

    This helper owns neither the connection listener nor an SDK handle. The
    offline wrapper supplies its own validator, not a production mode switch.
    """
    try:
        with listener.accept(timeout=timeout) as pipe:
            request = pipe.recv(timeout=1.0)
            try:
                request_validator(request)
                result = handle(request)
                reply = {'protocol': PROTOCOL, 'op': 'reply', 'body': {'value': result}}
                encode_frame(reply)
            except (KeyError, TypeError, TraderError) as error:
                reply = {'protocol': PROTOCOL, 'op': 'error', 'body': {
                    'code': error.code if isinstance(error, TraderError) else 'E_DSV_PROTOCOL',
                    'message': str(error) if isinstance(error, TraderError) else 'Malformed DSV control request'}}
            pipe.send(reply, timeout=1.0)
    except TraderError as error:
        if error.code not in _IGNORED:
            raise


def serve_control_request(listener, handle, *, timeout=0.25):
    """Online requests cannot opt into the offline operation set."""
    return serve_validated_request(listener, handle, request_validator=validate_control_request, timeout=timeout)
