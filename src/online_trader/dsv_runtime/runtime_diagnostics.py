"""Best-effort, bounded runtime diagnostics; never a trading fact or authority.

One owner uses this sink. Repeated failures emit at counts 1, 2, 4, 8, ... and
on recovery/change. An abrupt crash can lose the unsummarized repeat count.
No raw exception text, locals, account snapshots or absolute paths are written.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from online_trader.errors import exception_record


_CODE = re.compile(r'E_[A-Z0-9_]{1,80}\Z')


class RuntimeDiagnostics:
    def __init__(self, path, *, max_bytes=2 * 1024 * 1024, backups=3):
        if type(max_bytes) is not int or max_bytes < 1024 or type(backups) is not int or not 1 <= backups <= 16:
            raise ValueError('Invalid diagnostic rotation bounds')
        self.path = Path(path)
        self.max_bytes, self.backups = max_bytes, backups
        self._active = None
        self._last_emitted_count = 0
        self._last_exception_id = None
        self._degraded = False
        self._write_failures = 0

    def status(self):
        return {'state': 'DEGRADED' if self._degraded else 'OK',
                'write_failures': self._write_failures, 'last_exception_id': self._last_exception_id,
                'log': self.path.name}

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _detail(error):
        detail = exception_record(error)
        stack = detail['stack']
        truncated = len(stack) > 8 or any(len(item['frames']) > 32 for item in stack[:8])
        bounded = []
        for item in stack[:8]:
            code = item['code']
            bounded.append({
                'type': item['type'][:80],
                'code': code if type(code) is str and _CODE.fullmatch(code) else None,
                'frames': [{'file': frame['file'].replace('\\', '/').rsplit('/', 1)[-1][:160],
                            'line': frame['line'], 'function': frame['function'][:160]}
                           for frame in item['frames'][-32:]],
            })
        return {'exception_id': detail['exception_id'], 'fingerprint': detail['fingerprint'],
                'stack': bounded, 'truncated': truncated}

    def report(self, error, *, stage):
        """Observe an error, swallowing diagnostic failures but not process exit."""
        try:
            detail = self._detail(error)
            now = self._now()
            stage = stage if stage in {'SDK_ROUND', 'STARTUP_OR_CLEANUP'} else 'RUNTIME'
            active = self._active
            if active is not None and (active['stage'], active['exception']['fingerprint']) != (
                    stage, detail['fingerprint']):
                if active['occurrences'] != self._last_emitted_count:
                    self._append(active | {'kind': 'SUMMARY'})
                active = None
            if active is None:
                active = {'schema_version': 'DSV_RUNTIME_DIAGNOSTIC/1',
                          'episode_id': detail['exception_id'], 'stage': stage,
                          'first_seen_utc': now, 'last_seen_utc': now, 'occurrences': 0,
                          'exception': detail}
                self._last_emitted_count = 0
            active['occurrences'] += 1
            active['last_seen_utc'] = now
            self._active = active
            self._last_exception_id = active['episode_id']
            count = active['occurrences']
            if self._degraded or count & (count - 1) == 0:
                if self._append(active | {'kind': 'EXCEPTION'}):
                    self._last_emitted_count = count
        except Exception:
            self._failed_write()

    def recovered(self):
        """Only call after a successful synchronized round; retain history."""
        try:
            if self._active is not None and self._append(
                    self._active | {'kind': 'RECOVERED', 'recovered_at_utc': self._now()}):
                self._active = None
                self._last_emitted_count = 0
        except Exception:
            self._failed_write()

    def _failed_write(self):
        self._degraded = True
        self._write_failures = min(2_147_483_647, self._write_failures + 1)

    def _encode(self, record):
        limit = min(65_536, self.max_bytes)
        raw = (json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > limit:
            detail = record['exception']
            minimal = {**detail, 'truncated': True,
                       'stack': [{'type': item['type'], 'code': item['code'], 'frames': []}
                                 for item in detail['stack'][:1]]}
            raw = (json.dumps(record | {'exception': minimal}, ensure_ascii=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > limit:
            raise ValueError('Diagnostic record exceeds its bound')
        return raw

    def _append(self, record):
        try:
            raw = self._encode(record)
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(raw) + 1 > self.max_bytes:
                for index in range(self.backups, 0, -1):
                    source = self.path if index == 1 else self.path.with_name(self.path.name + f'.{index - 1}')
                    if source.exists():
                        source.replace(self.path.with_name(self.path.name + f'.{index}'))
            # Do not join a new JSON row onto an incomplete crash/disk-full tail.
            with self.path.open('a+b') as stream:
                stream.seek(0, 2)
                if stream.tell():
                    stream.seek(-1, 2)
                    if stream.read(1) != b'\n':
                        stream.write(b'\n')
                stream.write(raw)
            self._degraded = False
            return True
        except Exception:
            self._failed_write()
            return False
