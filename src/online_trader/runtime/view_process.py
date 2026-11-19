"""Independent local read interface for an original simulation owner."""

import os
import logging
import subprocess
import sys
from functools import partial
from threading import BoundedSemaphore, Event, Thread

from online_trader.errors import TraderError
from online_trader.runtime.account_day_history import read_account_days
from online_trader.runtime.account_history import account_history
from online_trader.runtime.executable import bundled_cli
from online_trader.runtime.operator_api import PROTOCOL as COMMAND_PROTOCOL, OperatorEndpoint
from online_trader.runtime.operator_api import validate_wire as validate_command
from online_trader.transport.named_pipe import PipeListener
from online_trader.transport.runtime_account import AccountObservation, read_observation
from online_trader.transport.runtime_account import validate_wire as validate_account
from online_trader.transport.runtime_view import DAY_HISTORY_FIELDS, PROTOCOL, ViewHistory, validate_wire
from online_trader.transport.workbench_snapshot import read_workbench_publication


def _start(pipe, account_key, root, ledger):
    program = bundled_cli()
    prefix = [str(program)] if program is not None else [sys.executable, '-m', 'online_trader']
    return subprocess.Popen([*prefix, 'runtime-view-process', '--pipe', pipe, '--account-key', account_key,
                             '--root', str(root), '--ledger', str(ledger)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))


def _close(process):
    if process is None:
        return
    process.stdin.close()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class ViewProcess:
    """Restart the disposable interface while the original execution owner keeps running."""

    def __init__(self, pipe, account_key, root, ledger):
        self.args = (pipe, account_key, root, ledger)
        self._stopped = Event()
        self._process = None
        self._thread = Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def trusted_pid(self):
        process = self._process
        return process.pid if process is not None and process.poll() is None else -1

    def _monitor(self):
        last_error = None
        try:
            while not self._stopped.is_set():
                if self.trusted_pid() <= 0:
                    _close(self._process)
                    self._process = None
                    try:
                        self._process = _start(*self.args)
                        last_error = None
                    except OSError as error:
                        if type(error) is not last_error:
                            print(f'E_RUNTIME_VIEW: {type(error).__name__}', file=sys.stderr)
                        last_error = type(error)
                self._stopped.wait(.5)
        finally:
            _close(self._process)

    def close(self):
        self._stopped.set()
        self._thread.join(timeout=5)


def _serve_peer(connection, history, history_page, day_history, stopped, slots):
    try:
        with connection:
            request = connection.recv(timeout=1)
            if request['kind'] in ('history', 'history-days'):
                try:
                    if request['kind'] == 'history':
                        page = history_page(before=request['before'], limit=request['limit'])
                    else:
                        page = day_history(**{field: request[field] for field in DAY_HISTORY_FIELDS})
                    reply = {'protocol': PROTOCOL, 'kind': 'HISTORY', 'body': page}
                except TraderError as error:
                    reply = {'protocol': PROTOCOL, 'kind': 'HISTORY_ERROR',
                             'body': {'code': error.code, 'message': str(error)}}
                connection.send(reply, timeout=.5)
                return
            cursor = request['cursor']
            response = history.response(cursor)
            connection.send(response, timeout=.5)
            if request['kind'] == 'subscribe':
                while not stopped.is_set():
                    cursor = response['cursor']
                    if cursor is None:
                        stopped.wait(.5)
                    response = history.wait(cursor, .5)
                    connection.send(response, timeout=.5)
    except (OSError, TraderError, KeyError):
        pass
    finally:
        slots.release()


def _serve_command(connection, endpoint, slots):
    try:
        with connection:
            request = connection.recv(timeout=1)
            reply = {'protocol': COMMAND_PROTOCOL, 'request_id': request['request_id']}
            try:
                reply['result'] = endpoint.handle(request)
            except TraderError as error:
                reply['error'] = {'code': error.code, 'message': str(error)}
            except (KeyError, TypeError, ValueError, IndexError):
                reply['error'] = {'code': 'E_SCHEMA', 'message': 'The command could not be validated'}
            connection.send(reply, timeout=.5)
    except (OSError, TraderError, KeyError):
        pass
    finally:
        slots.release()


def _serve_account(connection, current, slots):
    try:
        with connection:
            request = connection.recv(timeout=1)
            if request['kind'] == 'read':
                current().send(connection, request['known_id'])
    except (OSError, TraderError):
        pass
    finally:
        slots.release()


def _accept(listener, target, subjects, slots, stopped):
    while not stopped.is_set():
        try:
            connection = listener.accept(timeout=.25)
        except (OSError, TraderError):
            stopped.wait(.025)
            continue
        if not slots.acquire(blocking=False):
            connection.close()
            continue
        Thread(target=target, args=(connection, *subjects, slots), daemon=True).start()


def serve(pipe, account_key, root, ledger):
    history = ViewHistory()
    stopped = Event()
    endpoint = OperatorEndpoint(pipe, root, ledger, account_key)
    history_page = partial(account_history, ledger, account_key)
    day_history_slots = BoundedSemaphore(2)
    account_value = AccountObservation.waiting(account_key, 'E_ACCOUNT_SOURCE_UNAVAILABLE')

    def read_account_owner():
        nonlocal account_value
        while not stopped.is_set():
            try:
                account_value = read_observation(pipe + '.account-source', account_key=account_key, previous=account_value)
            except (OSError, TraderError):
                account_value = account_value.unavailable('E_ACCOUNT_SOURCE_UNAVAILABLE')
            stopped.wait(.15)

    def current_account():
        return account_value.current()

    def day_history(**query):
        if not day_history_slots.acquire(blocking=False):
            raise TraderError('E_BUSY', 'The account history reader is busy')
        try:
            return read_account_days(ledger, account_key, **query)
        finally:
            day_history_slots.release()

    def observe_parent():
        try:
            sys.stdin.buffer.read(1)
        finally:
            stopped.set()

    def read_owner():
        while not stopped.is_set():
            value = read_workbench_publication(pipe, account_key)
            if value is not None:
                history.publish(value)
            stopped.wait(.15)

    Thread(target=observe_parent, daemon=True).start()
    Thread(target=read_owner, daemon=True).start()
    accounts = None
    try:
        with (PipeListener(pipe + '.control', validator=validate_command) as commands,
              PipeListener(pipe + '.view', validator=validate_wire) as views):
            Thread(target=_accept, args=(commands, _serve_command, (endpoint,), BoundedSemaphore(4), stopped),
                   daemon=True).start()
            try:
                accounts = PipeListener(pipe + '.account', validator=validate_account)
            except (OSError, TraderError) as error:
                logging.getLogger(__name__).warning('E_ACCOUNT_VIEW_BIND: %s', getattr(error, 'code', 'OSError'))
            if accounts is not None:
                Thread(target=read_account_owner, daemon=True).start()
                Thread(target=_accept, args=(accounts, _serve_account, (current_account,), BoundedSemaphore(4), stopped),
                       daemon=True).start()
            _accept(views, _serve_peer, (history, history_page, day_history, stopped), BoundedSemaphore(12), stopped)
    finally:
        stopped.set()
        if accounts is not None:
            accounts.close()
    return 0
