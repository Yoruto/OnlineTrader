"""Serial, same-user IPC for the Windows offline DSV owner."""

import json
from pathlib import Path
import sqlite3

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import encode_frame, strict_loads
from online_trader.dsv_runtime.controller import terminal
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import PipeListener, connect


_PROTOCOL = 'DSV_OFFLINE/1'
_OPS = {'snapshot', 'command', 'round', 'receipt', 'disconnect'}
_TIMEOUT = {'E_TIMEOUT', 'E_PIPE', 'E_PIPE_CLOSED', 'E_PIPE_TIMEOUT'}


def validate_offline_message(message):
    if message.get('protocol') != _PROTOCOL or type(message.get('body')) is not dict:
        raise TraderError('E_DSV_PROTOCOL', 'Expected an offline DSV envelope')
    if message.keys() != {'protocol', 'op', 'body'} or message['op'] not in _OPS | {'reply', 'error'}:
        raise TraderError('E_DSV_PROTOCOL', 'Unsupported offline DSV operation')


class OfflineClient:
    def __init__(self, pipe):
        self.pipe = pipe

    def request(self, operation, body=None, *, timeout=1.0):
        payload = {'protocol': _PROTOCOL, 'op': operation, 'body': {} if body is None else body}
        with connect(self.pipe, timeout=timeout, validator=validate_offline_message) as pipe:
            pipe.send(payload, timeout=timeout)
            message = pipe.recv(timeout=timeout)
        if message['op'] == 'error':
            raise TraderError(message['body']['code'], message['body']['message'])
        if message['op'] != 'reply':
            raise TraderError('E_DSV_PROTOCOL', 'Expected a reply from the offline owner')
        return message['body']['value']


class OfflinePort:
    """The owner persists the outbound request; replay drivers supply any native response."""

    def send(self, request):
        return None

    def cancel(self, request):
        return None


def _snapshot(session, *, detail=False):
    state = session.snapshot()
    if not detail:
        state['orders'] = {key: order for key, order in state['orders'].items() if not terminal(order)}
        for row in state['symbols'].values():
            if row['position']:
                row['position'].pop('fills', None)
            row.pop('tick', None)
    return state


def _handle(session, request):
    body, op = request['body'], request['op']
    if op == 'snapshot':
        return _snapshot(session, detail=body.get('detail') is True)
    if op == 'receipt':
        session.verify_scope(body.get('expected_scope'))
        return session.command_receipt(body['id'])
    if op == 'command':
        session.verify_scope(body.get('expected_scope'))
        previous = session.command_receipt(body['id'])
        if previous is None:
            session.verify_day(body.get('expected_day'))
        return session.command(body)
    if op == 'round':
        return session.apply_round(body)
    if op == 'disconnect':
        return session.disconnect()
    raise TraderError('E_DSV_PROTOCOL', 'Expected an offline request')


def serve(pipe_name, database, header, *, ready_file=None):
    header = dict(header)
    if header.pop('type', 'session') != 'session':
        raise TraderError('E_DSV_SCOPE', 'Expected an offline session header')
    profile = DsvProfile.from_record(header.pop('profile'))
    with (DsvSession(database, profile=profile, port=OfflinePort(), **header) as session,
          PipeListener(pipe_name, validator=validate_offline_message) as listener):
        session.disconnect('OWNER_STARTED')
        if ready_file is not None:
            ready_file.write_text(json.dumps({'pipe': pipe_name, 'offline': True}), encoding='utf-8')
        print('DSV 离线执行器已启动', flush=True)
        while True:
            try:
                with listener.accept(timeout=0.25) as pipe:
                    request = pipe.recv(timeout=1.0)
                    try:
                        result = _handle(session, request)
                        reply = {'protocol': _PROTOCOL, 'op': 'reply', 'body': {'value': result}}
                        encode_frame(reply)
                    except (KeyError, TypeError, TraderError) as error:
                        reply = {'protocol': _PROTOCOL, 'op': 'error', 'body': {
                            'code': getattr(error, 'code', 'E_DSV_PROTOCOL'), 'message': str(error)}}
                    pipe.send(reply, timeout=1.0)
            except TraderError as error:
                if error.code not in _TIMEOUT | {'E_AUTH', 'E_DSV_PROTOCOL', 'E_FRAME', 'E_VERSION', 'E_SCHEMA'}:
                    raise


def cli(args):
    try:
        value = strict_loads(Path(args.input).read_bytes())
        if args.offline_action == 'serve':
            try:
                serve(args.pipe, args.db, value, ready_file=args.ready_file)
            except KeyboardInterrupt:
                pass
        else:
            reply = OfflineClient(args.pipe).request(value['op'], value.get('body'))
            print(json.dumps(reply, ensure_ascii=False))
    except (OSError, sqlite3.Error, KeyError, TypeError) as error:
        raise TraderError('E_DSV_OFFLINE', str(error)) from error
    return 0
