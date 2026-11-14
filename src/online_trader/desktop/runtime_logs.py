"""Resolve the current owner's log files and read bounded, complete records."""
from collections import deque
from pathlib import Path
import os

from online_trader.contracts.codec import strict_loads
from online_trader.errors import TraderError


def read_record(path, limit=65_536):
    try:
        with path.open('rb') as stream:
            raw = stream.read(limit + 1)
        value = strict_loads(raw) if len(raw) <= limit else None
    except (OSError, ValueError, TraderError):
        return None
    return value if isinstance(value, dict) else None


def _comparison_runs(directory):
    return [path for path in directory.glob('comparison-*') if path.is_dir()]


def comparison_file(directory, identity):
    runs = _comparison_runs(directory)
    if len(runs) != 1:
        return None
    value = read_record(runs[0] / 'manifest.json')
    if (value and value.get('schema_version') == 'DSV_COMPARISON/1'
            and value.get('events_file') == 'events.jsonl'
            and (value.get('account_key'), value.get('kernel_boot_id')) == identity):
        return runs[0] / 'events.jsonl'
    return None


def supplied_run_directory(view):
    supplied = view['configuration'].get('run_directory')
    if isinstance(supplied, str) and 0 < len(supplied) <= 4096 and '\0' not in supplied:
        directory = Path(supplied)
        if directory.is_absolute():
            return directory
    return None


def matches_run_identity(directory, identity):
    record = read_record(directory / 'run-identity.json')
    if record is not None:
        try:
            return (record.get('schema_version') == 'DSV_RUN_IDENTITY/1'
                    and (record.get('account_key'), record.get('kernel_boot_id')) == identity
                    and Path(record['run_directory']).resolve() == directory.resolve())
        except (KeyError, TypeError, ValueError, OSError):
            return False
    return comparison_file(directory, identity) is not None


def resolve_run(view, launch_paths, account_root=None, paired_root=None, *, finished=None):
    if view is None:
        if finished is not None:
            account, boot, directory = finished
            return directory if matches_run_identity(directory, (account, boot)) else None
        # Paths from the current desktop launch receipt belong to the selected
        # account even if comparison files already exist or startup has failed.
        return next(iter(launch_paths), None)
    identity = (view['account_key'], view['kernel_boot_id'])
    directory = supplied_run_directory(view)
    if directory is not None and matches_run_identity(directory, identity):
        return directory
    candidates = list(launch_paths)
    if account_root is not None:
        candidates.extend(account_root.glob('desktop-*/run'))
    if paired_root is not None:
        candidates.extend(paired_root.glob('*'))
    matched = [path for path in dict.fromkeys(candidates) if matches_run_identity(path, identity)]
    return matched[0] if len(matched) == 1 else None


def executor_log_path(directory):
    inside = directory / 'executor.log'
    return inside if inside.is_file() else directory.with_name(directory.name + '.executor.log')


def matches_operation_log_recovery(record, directory, identity):
    if (identity is not None and record.get('schema_version') == 'DSV_OPERATION_LOG_RECOVERY/1'
            and (record.get('account_key'), record.get('kernel_boot_id')) == identity):
        try:
            return Path(record['run_directory']).resolve() == directory.resolve()
        except (KeyError, TypeError, ValueError, OSError):
            pass
    return False


def operation_log_message(record):
    state = record.get('status')
    first, last = record.get('first_event_seq'), record.get('through_event_seq')
    interval = (f' · 账本事件 {first}–{last}'
                if all(isinstance(n, str) and n.isdecimal() and len(n) <= 19 for n in (first, last))
                and int(first) <= int(last) else '')
    labels = {'DEGRADED': '操作日志降级，等待从账本补回',
              'REBUILDING': '正在从账本补回操作日志',
              'RECOVERED': '已从账本补回操作日志'}
    label = labels.get(state, '操作日志写入失败')
    if state == 'RECOVERED' and record.get('auxiliary_rows_unavailable'):
        label += '，部分临时诊断可能缺失'
    return label + interval


class JsonLogTail:
    """Retain the operator's view across appends, including incomplete writes."""
    def __init__(self, limit, *, document=False):
        self.limit = limit
        self.document = document
        self.records = deque()
        self.identity = None
        self.offset = 0
        self.anchor = b''
        self.discard_fragment = False

    def _position(self, stream, end):
        self.offset = max(0, end - self.limit)
        self.discard_fragment = False
        if self.offset:
            stream.seek(self.offset - 1)
            self.discard_fragment = stream.read(1) != b'\n'

    def _current(self, stream, stat):
        identity = (stat.st_dev, stat.st_ino)
        valid = self.identity == identity and stat.st_size >= self.offset
        if valid and self.anchor:
            stream.seek(self.offset - len(self.anchor))
            valid = stream.read(len(self.anchor)) == self.anchor
        if not valid:
            self.records.clear()
            self.identity = identity
            self._position(stream, stat.st_size)
        return stat.st_size

    def _remember(self, stream, offset):
        self.offset = offset
        start = max(0, offset - 64)
        stream.seek(start)
        self.anchor = stream.read(offset - start)

    def read(self, path):
        try:
            with path.open('rb') as stream:
                end = self._current(stream, os.fstat(stream.fileno()))
                if end - self.offset > 2 * self.limit:
                    self.records.clear()
                    self._position(stream, end)
                stream.seek(self.offset)
                raw = stream.read(self.limit)
                if not raw:
                    return self.records
                if self.document:
                    try:
                        value = strict_loads(raw) if self.offset == 0 and end <= self.limit else None
                    except (TraderError, ValueError):
                        value = None
                    if value is not None and self.offset == 0:
                        if isinstance(value, dict):
                            self.records.append(value)
                        self._remember(stream, end)
                    return self.records
                if self.discard_fragment:
                    boundary = raw.find(b'\n') + 1
                    if not boundary:
                        self._remember(stream, self.offset + len(raw))
                        return self.records
                    self._remember(stream, self.offset + boundary)
                    raw = raw[boundary:]
                    self.discard_fragment = False
                complete = raw.rfind(b'\n') + 1
                if not complete and len(raw) >= self.limit:
                    self.discard_fragment = True
                    self._remember(stream, self.offset + len(raw))
                    return self.records
                for line in raw[:complete].splitlines():
                    if not line.startswith(b'{'):
                        continue
                    try:
                        value = strict_loads(line)
                    except (TraderError, ValueError):
                        continue
                    if isinstance(value, dict):
                        self.records.append(value)
                        if len(self.records) > 5_000:
                            for _ in range(2_001):
                                self.records.popleft()
                self._remember(stream, self.offset + complete)
        except OSError:
            self.records.clear()
            self.identity = None
        return self.records

    def clear(self, path):
        self.records.clear()
        try:
            with path.open('rb') as stream:
                end = self._current(stream, os.fstat(stream.fileno()))
                self._remember(stream, end)
                self.discard_fragment = bool(end and not self.anchor.endswith(b'\n'))
        except OSError:
            self.identity = None
