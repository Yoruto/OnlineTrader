"""Authenticated command interface owned by the standalone interface process."""

from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from hmac import compare_digest
from importlib.resources import files
from pathlib import Path
import re
import sqlite3
from threading import Lock
from uuid import uuid4

from online_trader.contracts.codec import strict_loads
from online_trader.domain.values import identifier, sequence
from online_trader.errors import TraderError
from online_trader.runtime.portable_config import _read
from online_trader.scheduling.calendar import _utc
from online_trader.transport.named_pipe import connect
from online_trader.transport.status_client import StatusClient


PROTOCOL = 'ONLINE_TRADER_RUNTIME_COMMAND/1'
_COMMAND_FIELDS = {'command_id', 'operation', 'scope', 'parameters'}


@lru_cache(maxsize=1)
def _operator_capabilities():
    schema = strict_loads(files('online_trader').joinpath('resources/ipc.schema.json').read_bytes())
    return frozenset(schema['$defs']['OperatorCommand']['properties']['operation']['enum']) | {'SCHEDULE_STRATEGY'}


def validate_wire(message):
    if type(message) is not dict or message.get('protocol') != PROTOCOL:
        raise TraderError('E_SCHEMA', 'Invalid runtime command frame')
    identifier(message.get('request_id'))
    if message.keys() == {'protocol', 'request_id', 'result'}:
        return
    if message.keys() == {'protocol', 'request_id', 'error'}:
        if type(message['error']) is not dict or type(message['error'].get('code')) is not str:
            raise TraderError('E_SCHEMA', 'Invalid runtime command error')
        return
    if message.keys() != {'protocol', 'request_id', 'operation', 'body', 'operator', 'token'}:
        raise TraderError('E_SCHEMA', 'Invalid runtime command request')
    if message['operation'] not in ('authenticate', 'submit', 'receipt') or type(message['body']) is not dict:
        raise TraderError('E_SCHEMA', 'Unknown runtime command operation')
    identifier(message['operator'])
    if type(message['token']) is not str or not 32 <= len(message['token']) <= 256:
        raise TraderError('E_AUTH', 'A valid delegated operator token is required')


def operator_records(root):
    try:
        config = _read(Path(root), 'private/operators.json')
        if (type(config) is not dict or config.keys() != {'schema_version', 'operators'}
                or config['schema_version'] != 'ONLINE_TRADER_OPERATORS/1'
                or type(config['operators']) is not list or not 1 <= len(config['operators']) <= 64):
            raise ValueError()
        result = {}
        for row in config['operators']:
            if (type(row) is not dict or row.keys() != {'id', 'token_sha256', 'account_keys', 'operations', 'expires_at_utc'}
                    or type(row['token_sha256']) is not str or re.fullmatch('[0-9a-f]{64}', row['token_sha256']) is None
                    or type(row['account_keys']) is not list or not row['account_keys']
                    or type(row['operations']) is not list or not row['operations']):
                raise ValueError()
            operator_id = identifier(row['id'])
            identifier('operator-' + operator_id)
            if operator_id in result:
                raise ValueError()
            accounts = frozenset(identifier(key) for key in row['account_keys'])
            operations = frozenset(identifier(key) for key in row['operations'])
            if not operations <= _operator_capabilities():
                raise ValueError()
            result[operator_id] = (row['token_sha256'], accounts, operations, _utc(row['expires_at_utc']))
        return result
    except (AttributeError, ValueError, KeyError, TypeError):
        raise TraderError('E_RUNTIME_CONFIG', 'The operator grant configuration is invalid') from None


def authenticate_operator(root, account_key, operator_id, token):
    try:
        if type(token) is not str or not 32 <= len(token) <= 256:
            raise ValueError()
        expected, accounts, operations, expiry = operator_records(root)[operator_id]
        if (account_key not in accounts or datetime.now(timezone.utc) >= expiry
                or not compare_digest(expected, sha256(token.encode()).hexdigest())):
            raise ValueError()
        return 'operator-' + operator_id, operations, expiry
    except (ValueError, KeyError, TypeError):
        raise TraderError('E_AUTH', 'The operator token or its account grant is invalid') from None


class OperatorEndpoint:
    def __init__(self, pipe, root, ledger, account_key):
        self.pipe, self.root, self.ledger, self.account_key = pipe, Path(root), Path(ledger), account_key
        self._submission = Lock()

    def _receipt(self, command_id, actor, operations):
        if not self.ledger.is_file():
            return None
        try:
            with closing(sqlite3.connect(self.ledger.as_uri() + '?mode=ro', uri=True, timeout=.5)) as stream:
                scope = stream.execute('SELECT account_key, environment FROM ledger_scope').fetchall()
                if scope != [(self.account_key, 'ONLINE_SIM')]:
                    raise TraderError('E_SCOPE', 'Execution ledger does not match the selected simulation account')
                row = stream.execute('''SELECT cmd.payload_json, cmd.result_json, event.payload_json
                    FROM operator_commands AS cmd JOIN event_journal AS event ON event.event_seq = cmd.created_event_seq
                    WHERE cmd.command_id = ?''', (command_id,)).fetchone()
        except sqlite3.Error:
            raise TraderError('E_STORAGE', 'Cannot read the current command checkpoint') from None
        if row is None:
            return None
        command, receipt, event = map(strict_loads, row)
        recorded = event['data'].get('control_metadata', {}).get('metadata', {}).get('actor', {})
        if recorded.get('id') != actor or command['operation'] not in operations:
            raise TraderError('E_AUTH', 'The command belongs to a different operator or grant')
        return {key: command[key] for key in _COMMAND_FIELDS}, receipt

    def handle(self, request):
        actor, operations, expiry = authenticate_operator(
            self.root, self.account_key, request['operator'], request['token'])
        body = request['body']
        if request['operation'] == 'authenticate':
            if body:
                raise TraderError('E_SCHEMA', 'Operator authentication does not accept a command body')
            return {'status': 'AUTHENTICATED', 'operator': request['operator'], 'actor': actor,
                    'account_key': self.account_key, 'operations': sorted(operations),
                    'expires_at_utc': expiry.isoformat().replace('+00:00', 'Z')}
        command_id = identifier(body.get('command_id'))
        if request['operation'] == 'receipt':
            if body.keys() != {'command_id'}:
                raise TraderError('E_SCHEMA', 'Provide only the original command ID to query its receipt')
            found = self._receipt(command_id, actor, operations)
            if found is None:
                raise TraderError('E_OWNERSHIP', 'No committed receipt is currently visible for this command ID')
            return found[1]
        if (body.keys() - {'binding'} != _COMMAND_FIELDS or type(body['operation']) is not str
                or type(body['scope']) is not dict or type(body['parameters']) is not dict):
            raise TraderError('E_SCHEMA', 'Provide the command ID, operation, scope and parameters')
        binding = body.get('binding')
        if 'binding' in body:
            if type(binding) is not dict or binding.keys() != {'account_key', 'kernel_boot_id', 'config_revision'}:
                raise TraderError('E_SCHEMA', 'The operator draft has an invalid account and configuration binding')
            if binding['account_key'] != self.account_key:
                raise TraderError('E_SCOPE', 'The operator draft belongs to another account')
            identifier(binding['kernel_boot_id'])
            sequence(binding['config_revision'], positive=True)
        if body['operation'] not in operations:
            raise TraderError('E_AUTH', 'The operator does not hold this command capability')
        if not self._submission.acquire(blocking=False):
            raise TraderError('E_BUSY', 'The command was not enqueued because the interface is busy')
        try:
            found = self._receipt(command_id, actor, operations)
            if found is not None:
                if found[0] != {key: body[key] for key in _COMMAND_FIELDS}:
                    raise TraderError('E_DUPLICATE_CONFLICT', 'The command ID has different business content')
                return found[1]
            client = StatusClient(self.pipe, timeout=5, expected_account_key=self.account_key, client_instance_id=actor,
                                  expected_kernel_boot_id=binding['kernel_boot_id'] if binding else None)
            return client.command(body['operation'], body['scope'], body['parameters'], command_id=command_id,
                                  expected_config_revision=binding['config_revision'] if binding else None)
        finally:
            self._submission.release()


class RuntimeCommandClient:
    def __init__(self, pipe):
        self.pipe = pipe + '.control'

    def request(self, operation, body, *, operator, token):
        request_id = 'request-' + uuid4().hex
        request = {'protocol': PROTOCOL, 'request_id': request_id, 'operation': operation,
                   'body': body, 'operator': operator, 'token': token}
        with connect(self.pipe, timeout=2, validator=validate_wire) as connection:
            connection.send(request, timeout=2)
            response = connection.recv(timeout=8)
        if response['request_id'] != request_id:
            raise TraderError('E_SCOPE', 'The command response has a different request identity')
        if error := response.get('error'):
            raise TraderError(error['code'], error.get('message', 'The runtime refused the command'))
        return response['result']

    def authenticate(self, *, operator, token):
        return self.request('authenticate', {}, operator=operator, token=token)
