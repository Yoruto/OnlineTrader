"""Independent local read interface for an original simulation owner."""

import os
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
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


_VIEW_LOG_LIMIT = 4 * 1024 * 1024


def _view_log(root):
    path = Path(root).resolve() / 'logs' / 'runtime-view.log'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size >= _VIEW_LOG_LIMIT:
            previous = path.with_suffix('.log.1')
            previous.unlink(missing_ok=True)
            path.replace(previous)
    except OSError:
        pass
    return path


def _supervisor_log(root, message):
    try:
        path = _view_log(root)
        stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        with path.open('a', encoding='utf-8') as stream:
            stream.write(f'{stamp} SUPERVISOR {message}\n')
    except OSError:
        pass


def _start(pipe, account_key, root, ledger):
    program = bundled_cli()
    prefix = [str(program)] if program is not None else [sys.executable, '-m', 'online_trader']
    log_path = _view_log(root)
    output = None
    try:
        output = log_path.open('ab', buffering=0)
    except OSError:
        pass
    try:
        process = subprocess.Popen([*prefix, 'runtime-view-process', '--pipe', pipe, '--account-key', account_key,
                                    '--root', str(root), '--ledger', str(ledger)],
            stdin=subprocess.PIPE, stdout=output if output is not None else subprocess.DEVNULL,
            stderr=output if output is not None else subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'))
    finally:
        if output is not None:
            output.close()
    _supervisor_log(root, f'START pid={process.pid}')
    return process


def _close(process):
    if process is None:
        return
    try:
        process.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
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
        started_at = None
        rapid_failures = 0
        root = self.args[2]
        try:
            while not self._stopped.is_set():
                if self.trusted_pid() <= 0:
                    if self._process is not None:
                        code = self._process.poll()
                        uptime = 0 if started_at is None else max(0, time.monotonic() - started_at)
                        _supervisor_log(root, f'EXIT code={code} uptime_ms={int(uptime * 1000)}')
                        rapid_failures = rapid_failures + 1 if uptime < 10 else 0
                        _close(self._process)
                        self._process = None
                        delay = min(10.0, .5 * (2 ** min(rapid_failures, 5)))
                        if self._stopped.wait(delay):
                            break
                    try:
                        self._process = _start(*self.args)
                        started_at = time.monotonic()
                        last_error = None
                    except OSError as error:
                        _supervisor_log(root, f'START_FAILED error={type(error).__name__}')
                        if type(error) is not last_error:
                            print(f'E_RUNTIME_VIEW: {type(error).__name__}', file=sys.stderr)
                        last_error = type(error)
                        rapid_failures = min(rapid_failures + 1, 5)
                        delay = min(10.0, .5 * (2 ** rapid_failures))
                        if self._stopped.wait(delay):
                            break
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
