"""Serial, same-user IPC for the Windows offline DSV owner."""

import json
from pathlib import Path
import sqlite3

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import strict_loads
from online_trader.dsv_runtime.control_endpoint import (
    PROTOCOL as _PROTOCOL, CONTROL_OPS, handle_control, snapshot as _snapshot,
    serve_validated_request, validate_envelope,
)
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import PipeListener, connect


_OPS = {'snapshot', 'command', 'round', 'receipt', 'disconnect', 'shutdown'}


def validate_offline_message(message):
    validate_envelope(message, _OPS | {'reply', 'error'})


def _validate_offline_request(message):
    validate_envelope(message, _OPS)


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


def _handle(session, request, *, sdk=None):
    # The internal legacy helper accepted op/body without a wire envelope.
    # Network requests still require the complete validated envelope.
    if type(request) is dict and request.keys() == {'op', 'body'}:
        request = {'protocol': _PROTOCOL, **request}
    # Retain the old helper API for existing offline tests/clients. Online
    # compatibility calls still pass through the strict control dispatcher.
    if sdk is not None:
        return handle_control(session, request, sdk=sdk)
    _validate_offline_request(request)
    body, op = request['body'], request['op']
    if op in CONTROL_OPS:
        return handle_control(session, request)
    if op == 'round':
        return session.apply_round(body)
    if op == 'disconnect':
        return session.disconnect()
    raise TraderError('E_DSV_PROTOCOL', 'Expected an offline request')


def serve_request(listener, handle, *, timeout=0.25):
    return serve_validated_request(listener, handle, request_validator=_validate_offline_request, timeout=timeout)


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
            serve_request(listener, lambda request: _handle(session, request))


def cli(args):
    try:
        if args.offline_action == 'rollback-review':
            from online_trader.dsv_runtime.migration import rollback_review
            result = rollback_review(args.source, args.db)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['eligible'] else 2
        value = strict_loads(Path(args.input).read_bytes())
        if args.offline_action == 'archive-log':
            from online_trader.dsv_runtime.log_archive import archive_log
            print(json.dumps(archive_log(args.source, args.runs_root, args.boot_id, value, args.target), ensure_ascii=False))
        elif args.offline_action == 'rollback-rehearse':
            from online_trader.dsv_runtime.rollback import rehearse
            result = rehearse(args.source, args.db, value, args.target)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['eligible'] else 2
        elif args.offline_action == 'rollback-same-day':
            from online_trader.dsv_runtime.same_day_rollback import convert_same_day
            result = convert_same_day(args.source, args.db, value, args.target)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['eligible'] else 2
        elif args.offline_action == 'migrate':
            from online_trader.dsv_runtime.migration import migrate
            result = migrate(args.source, value, args.target)
            print(json.dumps(result, ensure_ascii=False))
            return 2 if result['blockers'] else 0
        elif args.offline_action == 'serve':
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
