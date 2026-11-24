"""Independent local read interface for an original simulation owner."""

import os
import hashlib
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Thread
import re

from online_trader.errors import TraderError
from online_trader.runtime.account_cache import read_account_days, recent_history
from online_trader.runtime.executable import bundled_cli
from online_trader.runtime.operator_api import WorkbenchEndpoint, validate_workbench_wire
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


def _start(pipe, account_key, root, ledger, history):
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
                                    '--root', str(root), '--ledger', str(ledger), '--history', str(history)],
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

    def __init__(self, pipe, account_key, root, ledger, history):
        self.args = (pipe, account_key, root, ledger, history)
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


def _runtime_log_page(root, *, before=None, limit=100):
    path = Path(root).resolve() / 'logs' / 'runtime-view.log'
    if not path.is_file():
        return {'rows': [], 'next_before': None, 'complete': True}
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 1_048_576))
            raw = stream.read(1_048_576)
    except OSError:
        raise TraderError('E_RUNTIME_LOG', 'The Runtime interface log cannot be read') from None
    lines = raw.decode('utf-8', errors='replace').splitlines()[-10_000:]
    end = len(lines) if before is None else min(int(before), len(lines))
    start = max(0, end - limit)
    pattern = re.compile(
        r'^(\S+) (?:SUPERVISOR )?('
        r'START pid=\d+|EXIT code=-?\d+ uptime_ms=\d+|START_FAILED error=[A-Za-z0-9_]+|'
        r'VIEW_CLIENT pid=\d+ boot=[A-Za-z0-9._-]+|'
        r'COMMAND source=(?:WORKBENCH|OPERATOR) '
        r'(?:peer_pid=\d+ peer_sid=[A-Za-z0-9._-]+ actor_kind=[A-Z_]+ '
        r'actor=[A-Za-z0-9._-]+ account=[a-f0-9]{16} boot=[A-Za-z0-9._-]+ )?'
        r'request=[A-Za-z0-9._-]+ '
        r'action=[A-Za-z0-9._-]+ operation=[A-Za-z0-9._-]+ command=[A-Za-z0-9._-]+ '
        r'result=(?:ACCEPTED|REJECTED) code=[A-Za-z0-9._-]+)$')
    rows = []
    for index, line in enumerate(lines[start:end], start=start):
        match = pattern.fullmatch(line)
        rows.append({'sequence': str(index + 1),
                     'occurred_at': match.group(1) if match else None,
                     'source': 'RUNTIME_VIEW_SUPERVISOR',
                     'message': match.group(2) if match else 'INTERFACE_PROCESS_OUTPUT_REDACTED'})
    return {'rows': rows, 'next_before': str(start) if start else None, 'complete': start == 0}


def _account_display(root, account_key):
    """Read only the already-masked account label for the dev workbench shell."""
    try:
        from online_trader.runtime.portable_config import PortableAccountStore
        store = PortableAccountStore(root)
        row = next(item for item in store.read()['accounts'] if item['account_key'] == account_key)
        return {key: row[key] for key in ('provider', 'masked_login', 'alias')}
    except (TraderError, OSError, KeyError, StopIteration, TypeError, ValueError):
        return None


def _serve_peer(connection, history, history_page, day_history, runtime_log, root, stopped, slots):
    try:
        with connection:
            request = connection.recv(timeout=1)
            if request['kind'] in ('history', 'history-days', 'runtime-log'):
                try:
                    if request['kind'] == 'history':
                        page = history_page(before=request['before'], limit=request['limit'])
                        reply = {'protocol': PROTOCOL, 'kind': 'HISTORY', 'body': page}
                    elif request['kind'] == 'runtime-log':
                        page = runtime_log(before=request['before'], limit=request['limit'])
                        reply = {'protocol': PROTOCOL, 'kind': 'RUNTIME_LOG', 'body': page}
                    else:
                        page = day_history(**{field: request[field] for field in DAY_HISTORY_FIELDS})
                        reply = {'protocol': PROTOCOL, 'kind': 'HISTORY', 'body': page}
                except TraderError as error:
                    reply = {'protocol': PROTOCOL,
                             'kind': 'RUNTIME_LOG_ERROR' if request['kind'] == 'runtime-log' else 'HISTORY_ERROR',
                             'body': {'code': error.code, 'message': str(error)}}
                connection.send(reply, timeout=.5)
                return
            cursor = request['cursor']
            response = history.response(cursor)
            boot = None
            try:
                boot = response['body']['view']['kernel_boot_id']
            except (KeyError, TypeError):
                pass
            _supervisor_log(
                root,
                f'VIEW_CLIENT pid={max(0, connection.peer_pid or 0)} '
                f'boot={_safe_log_token(boot)}',
            )
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


def _safe_log_token(value):
    value = value if isinstance(value, str) else '-'
    return value if re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value) else '-'


def _serve_command(connection, endpoint, root, slots):
    try:
        with connection:
            request = connection.recv(timeout=1)
            reply = {'protocol': endpoint.protocol, 'request_id': request['request_id']}
            body = request.get('body', {})
            action = _safe_log_token(request.get('operation'))
            operation = _safe_log_token(body.get('operation'))
            command_id = _safe_log_token(body.get('command_id'))
            binding = body.get('binding') if isinstance(body.get('binding'), dict) else {}
            actor_kind = 'LOCAL_USER'
            actor = 'workbench-user'
            account = hashlib.sha256(endpoint.account_key.encode('utf-8')).hexdigest()[:16]
            peer_pid = connection.peer_pid if isinstance(connection.peer_pid, int) else 0
            peer = (f'peer_pid={max(0, peer_pid)} '
                    f'peer_sid={_safe_log_token(connection.peer_sid)} '
                    f'actor_kind={actor_kind} actor={actor} account={account} '
                    f'boot={_safe_log_token(binding.get("kernel_boot_id"))}')
            try:
                reply['result'] = endpoint.handle(request)
                if isinstance(reply['result'], dict):
                    operation = _safe_log_token(reply['result'].get('operation') or operation)
                    command_id = _safe_log_token(reply['result'].get('command_id') or command_id)
                _supervisor_log(root, f'COMMAND source={endpoint.channel} {peer} '
                                f'request={_safe_log_token(request.get("request_id"))} '
                                f'action={action} operation={operation} command={command_id} result=ACCEPTED code=-')
            except TraderError as error:
                reply['error'] = {'code': error.code, 'message': str(error)}
                _supervisor_log(root, f'COMMAND source={endpoint.channel} {peer} '
                                f'request={_safe_log_token(request.get("request_id"))} '
                                f'action={action} operation={operation} command={command_id} '
                                f'result=REJECTED code={_safe_log_token(error.code)}')
            except (KeyError, TypeError, ValueError, IndexError):
                reply['error'] = {'code': 'E_SCHEMA', 'message': 'The command could not be validated'}
                _supervisor_log(root, f'COMMAND source={endpoint.channel} {peer} '
                                f'request={_safe_log_token(request.get("request_id"))} '
                                f'action={action} operation={operation} command={command_id} '
                                'result=REJECTED code=E_SCHEMA')
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


def serve(pipe, account_key, root, ledger, history_cache):
    history = ViewHistory()
    stopped = Event()
    command_submission = Lock()
    workbench_endpoint = WorkbenchEndpoint(
        pipe, root, ledger, account_key, submission_lock=command_submission)
    account_display = _account_display(root, account_key)
    history_page = partial(recent_history, history_cache, account_key)
    runtime_log = partial(_runtime_log_page, root)
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
            return read_account_days(history_cache, account_key, **query)
        finally:
            day_history_slots.release()

    def observe_parent():
        try:
            sys.stdin.buffer.read(1)
        finally:
            stopped.set()

    def read_owner():
        while not stopped.is_set():
            try:
                value = read_workbench_publication(pipe, account_key)
                if value is not None:
                    if account_display is not None:
                        value = value | {'view': value['view'] | {'account_display': account_display}}
                    history.publish(value)
            except (OSError, TraderError, KeyError, TypeError, ValueError):
                pass
            stopped.wait(.15)

    Thread(target=observe_parent, daemon=True).start()
    Thread(target=read_owner, daemon=True).start()
    accounts = None
    try:
        with (PipeListener(pipe + '.workbench-control', validator=validate_workbench_wire) as workbench_commands,
              PipeListener(pipe + '.view', validator=validate_wire) as views):
            Thread(target=_accept, args=(workbench_commands, _serve_command, (workbench_endpoint, root),
                                         BoundedSemaphore(4), stopped),
                   daemon=True).start()
            try:
                accounts = PipeListener(pipe + '.account', validator=validate_account)
            except (OSError, TraderError) as error:
                logging.getLogger(__name__).warning('E_ACCOUNT_VIEW_BIND: %s', getattr(error, 'code', 'OSError'))
            if accounts is not None:
                Thread(target=read_account_owner, daemon=True).start()
                Thread(target=_accept, args=(accounts, _serve_account, (current_account,), BoundedSemaphore(4), stopped),
                       daemon=True).start()
            _accept(views, _serve_peer, (history, history_page, day_history, runtime_log, root, stopped),
                    BoundedSemaphore(12), stopped)
    finally:
        stopped.set()
        if accounts is not None:
            accounts.close()
    return 0
