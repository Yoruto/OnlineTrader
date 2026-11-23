"""Same-user Workbench command interface owned by the Runtime view process."""

from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4

from online_trader.contracts.codec import strict_loads
from online_trader.configuration.dsv import DsvProfile
from online_trader.domain.values import identifier, sequence
from online_trader.errors import TraderError
from online_trader.transport.named_pipe import CancellablePipeClient
from online_trader.transport.status_client import StatusClient


WORKBENCH_PROTOCOL = 'ONLINE_TRADER_RUNTIME_WORKBENCH_COMMAND/1'
WORKBENCH_ACTOR = 'workbench-user'
_COMMAND_FIELDS = {'command_id', 'operation', 'scope', 'parameters'}

# Closed set of operations exposed by the local Workbench channel.  Keep this
# independent from the schema enum: adding a Kernel/operator operation must not
# silently grant it to every same-SID local process that can reach this pipe.
WORKBENCH_OPERATIONS = frozenset({
    'ADOPT_EXTERNAL_POSITION',
    'ARM_ENTRIES',
    'CANCEL_NATIVE_ACCOUNT',
    'CANCEL_OWNED',
    'CANCEL_SELECTED',
    'FLATTEN_THEN_STOP',
    'MANUAL_CLOSE',
    'MANUAL_CLOSE_ALL',
    'MANUAL_OPEN',
    'PAUSE_NEW_ENTRIES',
    'REQUEST_RECONCILIATION',
    'SAVE_DSV_PARAMETERS',
    'SET_ENTRY_POLICY',
    'STOP_DSV_STRATEGY',
})


def validate_workbench_wire(message):
    if type(message) is not dict or message.get('protocol') != WORKBENCH_PROTOCOL:
        raise TraderError('E_SCHEMA', 'Invalid Runtime Workbench command frame')
    identifier(message.get('request_id'))
    if message.keys() == {'protocol', 'request_id', 'result'}:
        return
    if message.keys() == {'protocol', 'request_id', 'error'}:
        if type(message['error']) is not dict or type(message['error'].get('code')) is not str:
            raise TraderError('E_SCHEMA', 'Invalid Runtime Workbench command error')
        return
    if message.keys() != {'protocol', 'request_id', 'operation', 'body'}:
        raise TraderError('E_SCHEMA', 'Invalid Runtime Workbench command request')
    if message['operation'] not in ('submit', 'receipt', 'preview-config') or type(message['body']) is not dict:
        raise TraderError('E_SCHEMA', 'Unknown Runtime Workbench command operation')


class WorkbenchEndpoint:
    """Authorize explicit Qt button commands at the local Windows-user boundary."""

    protocol = WORKBENCH_PROTOCOL
    channel = 'WORKBENCH'

    def __init__(self, pipe, root, ledger, account_key, *, submission_lock=None):
        self.pipe, self.root, self.ledger, self.account_key = pipe, Path(root), Path(ledger), account_key
        self._submission = submission_lock if submission_lock is not None else Lock()

    def _receipt(self, command_id):
        if not self.ledger.is_file():
            return None
        try:
            with closing(sqlite3.connect(self.ledger.as_uri() + '?mode=ro', uri=True, timeout=.5)) as stream:
                application = stream.execute('PRAGMA application_id').fetchone()[0]
                if application == 0x44535631:
                    scope_row = stream.execute('SELECT payload FROM scope WHERE id=1').fetchone()
                    receipt_row = stream.execute(
                        "SELECT payload FROM receipts WHERE kind='RUNTIME_COMMAND' AND id=?", (command_id,)).fetchone()
                    if scope_row is None or receipt_row is None:
                        return None
                    scope = strict_loads(scope_row[0])
                    bundle = strict_loads(receipt_row[0])
                    command, receipt = bundle['command'], bundle['receipt']
                    if (scope.get('account_key') != self.account_key
                            or scope.get('owner_mode') != 'STANDALONE'
                            or command.get('operation') not in WORKBENCH_OPERATIONS
                            or command.get('command_id') != command_id
                            or receipt.get('command_id') != command_id):
                        raise TraderError('E_AUTH', 'The command belongs to a different Runtime authority')
                    return {key: command[key] for key in _COMMAND_FIELDS}, receipt
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
        if (recorded.get('id') != WORKBENCH_ACTOR or recorded.get('kind') != 'LOCAL_USER'
                or command['operation'] not in WORKBENCH_OPERATIONS):
            raise TraderError('E_AUTH', 'The command belongs to a different command authority')
        return {key: command[key] for key in _COMMAND_FIELDS}, receipt

    def handle(self, request):
        body = request['body']
        if request['operation'] == 'preview-config':
            if body.keys() != {'profile', 'binding'} or type(body['binding']) is not dict:
                raise TraderError('E_SCHEMA', 'Provide a profile and its Runtime binding')
            binding = body['binding']
            if binding.keys() != {'account_key', 'kernel_boot_id', 'config_revision'}:
                raise TraderError('E_SCHEMA', 'The configuration preview binding is invalid')
            if binding['account_key'] != self.account_key:
                raise TraderError('E_SCOPE', 'The configuration draft belongs to another account')
            identifier(binding['kernel_boot_id'])
            sequence(binding['config_revision'], positive=True)
            profile = DsvProfile.from_record(body['profile'])
            try:
                with closing(sqlite3.connect(self.ledger.as_uri() + '?mode=ro', uri=True, timeout=.5)) as stream:
                    application = stream.execute('PRAGMA application_id').fetchone()[0]
                    if application == 0x44535631:
                        row = stream.execute('SELECT payload FROM scope WHERE id=1').fetchone()
                        target_scope = None if row is None else strict_loads(row[0])
                        saved = stream.execute(
                            "SELECT payload FROM events WHERE kind='DSV_PARAMETERS_SAVED' ORDER BY seq DESC LIMIT 1"
                        ).fetchone()
                        current_revision = '1' if saved is None else str(strict_loads(saved[0])['config_revision'])
                        if (target_scope is None or target_scope.get('account_key') != self.account_key
                                or target_scope.get('owner_mode') != 'STANDALONE'
                                or target_scope.get('runtime_boot_id') != binding['kernel_boot_id']
                                or binding['config_revision'] != current_revision):
                            raise TraderError('E_CONFIG_EDIT', 'The target-execution configuration changed before preview')
                        return {'status': 'VALID', 'account_key': self.account_key,
                                'kernel_boot_id': binding['kernel_boot_id'],
                                'config_revision': binding['config_revision'],
                                'profile_hash': profile.content_hash, 'profile': profile.to_record()}
                    scope = stream.execute('SELECT account_key, environment FROM ledger_scope').fetchall()
                    current = stream.execute('SELECT config_revision FROM authority WHERE singleton=1').fetchone()
            except sqlite3.Error:
                raise TraderError('E_STORAGE', 'Cannot read the current configuration revision') from None
            if scope != [(self.account_key, 'ONLINE_SIM')] or current is None:
                raise TraderError('E_SCOPE', 'The configuration ledger does not match this Runtime')
            if str(current[0]) != binding['config_revision']:
                raise TraderError('E_CONFIG_EDIT', 'The effective configuration changed before preview')
            return {'status': 'VALID', 'account_key': self.account_key,
                    'kernel_boot_id': binding['kernel_boot_id'], 'config_revision': binding['config_revision'],
                    'profile_hash': profile.content_hash, 'profile': profile.to_record()}
        command_id = identifier(body.get('command_id'))
        if request['operation'] == 'receipt':
            if body.keys() != {'command_id'}:
                raise TraderError('E_SCHEMA', 'Provide only the original command ID to query its receipt')
            found = self._receipt(command_id)
            if found is None:
                raise TraderError('E_OWNERSHIP', 'No committed receipt is currently visible for this command ID')
            return found[1] | {'operation': found[0]['operation'], 'account_key': self.account_key}
        requested_operation = body.get('operation')
        if type(requested_operation) is str and requested_operation not in WORKBENCH_OPERATIONS:
            raise TraderError('E_AUTH', 'This command channel does not hold the requested capability')
        if (body.keys() != _COMMAND_FIELDS | {'binding'} or type(body['binding']) is not dict
                or type(body['operation']) is not str
                or type(body['scope']) is not dict or type(body['parameters']) is not dict):
            raise TraderError('E_SCHEMA', 'Provide the command and its Runtime binding')
        binding = body['binding']
        if binding.keys() != {'account_key', 'kernel_boot_id', 'config_revision'}:
            raise TraderError('E_SCHEMA', 'The command draft has an invalid account and configuration binding')
        if binding['account_key'] != self.account_key:
            raise TraderError('E_SCOPE', 'The command draft belongs to another account')
        identifier(binding['kernel_boot_id'])
        sequence(binding['config_revision'], positive=True)
        if not self._submission.acquire(blocking=False):
            raise TraderError('E_BUSY', 'The command was not enqueued because the interface is busy')
        try:
            found = self._receipt(command_id)
            if found is not None:
                if found[0] != {key: body[key] for key in _COMMAND_FIELDS}:
                    raise TraderError('E_DUPLICATE_CONFLICT', 'The command ID has different business content')
                return found[1] | {'operation': found[0]['operation'], 'account_key': self.account_key}
            client = StatusClient(self.pipe, timeout=5, expected_account_key=self.account_key,
                                  client_instance_id=WORKBENCH_ACTOR,
                                  expected_kernel_boot_id=binding['kernel_boot_id'])
            receipt = client.command(body['operation'], body['scope'], body['parameters'], command_id=command_id,
                                     expected_config_revision=binding['config_revision'])
            return receipt | {'operation': body['operation'], 'account_key': self.account_key}
        finally:
            self._submission.release()


class RuntimeWorkbenchClient(CancellablePipeClient):
    def __init__(self, pipe):
        super().__init__()
        self.pipe = pipe + '.workbench-control'

    def request(self, operation, body):
        request_id = 'request-' + uuid4().hex
        request = {'protocol': WORKBENCH_PROTOCOL, 'request_id': request_id,
                   'operation': operation, 'body': body}
        with self.open_connection(self.pipe, timeout=2, validator=validate_workbench_wire) as connection:
            connection.send(request, timeout=2)
            response = connection.recv(timeout=8)
        if response['request_id'] != request_id:
            raise TraderError('E_SCOPE', 'The Workbench command response has a different request identity')
        if error := response.get('error'):
            raise TraderError(error['code'], error.get('message', 'The Runtime refused the Workbench command'))
        return response['result']
