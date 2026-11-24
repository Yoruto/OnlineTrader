import hashlib
import io
import json
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import Request
from zipfile import ZipFile

from online_trader.errors import TraderError
from online_trader.update_install import (
    active_version,
    assert_payload_managed_launch,
    launch_managed,
    managed_root,
    startup_check_and_download,
)
from online_trader.updates import (
    GitLab,
    GitLabRedirects,
    PAYLOAD_MARKER,
    PAYLOAD_SCHEMA,
    UpdateError,
    atomic_json,
    check_and_download,
    extract_bundle,
    load_update_source,
)


def _write_payload_zip(path, version='0.1.0.dev35', *, include_marker=True, extra_entries=None):
    with ZipFile(path, 'w') as archive:
        archive.writestr('OnlineTrader.exe', b'new workbench')
        archive.writestr('OnlineTraderCLI.exe', b'new command line')
        archive.writestr('_internal/runtime', b'new runtime')
        if include_marker:
            archive.writestr(
                PAYLOAD_MARKER,
                json.dumps(
                    {
                        'schema_version': PAYLOAD_SCHEMA,
                        'version': version,
                        'standalone': False,
                        'requires_managed_root': True,
                    }
                ),
            )
        for name, content in (extra_entries or {}).items():
            archive.writestr(name, content)


class UpdateInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / 'OnlineTrader.exe'
        self.original.write_bytes(b'original executable')
        self.preferences = self.root / 'OnlineTrader.ini'
        self.preferences.write_bytes(b'original preferences')
        self.runtime_dir = self.root / 'runtime'
        (self.runtime_dir / 'private').mkdir(parents=True)
        (self.runtime_dir / 'runtime.json').write_text(
            json.dumps(
                {
                    'schema_version': 'ONLINE_TRADER_RUNTIME/1',
                    'selected_account': 'configured',
                    'accounts': [
                        {
                            'id': 'configured',
                            'alias': 'Configured',
                            'provider': 'TQKQ',
                            'broker': 'KQ',
                            'line': 'SIM',
                        }
                    ],
                }
            ),
            encoding='utf-8',
        )
        (self.runtime_dir / 'private' / 'OnlineTrader-defaults.json').write_text(
            json.dumps({'accounts': {'configured': {'username': 'u', 'password': 'p'}}}),
            encoding='utf-8',
        )
        (self.runtime_dir / 'OnlineTrader-overrides.json').write_text('{"custom":true}\n', encoding='utf-8')
        self.cache = self.root / 'updates'
        self.cache.mkdir()
        atomic_json(
            self.root / 'update-source.json',
            {'host': 'jihulab.com', 'project_id': 389806, 'channel': 'preview', 'portable_version': '0.1.0.dev34'},
        )
        for name in ('launcher_lock', 'runtime_lease', 'quiescent_installation'):
            patcher = patch(f'online_trader.update_install.{name}', side_effect=lambda *args, **kw: nullcontext())
            self.addCleanup(patcher.stop)
            patcher.start()
        checker = patch('online_trader.update_install._offline_check')
        self.addCleanup(checker.stop)
        checker.start()

    def stage_update(self, version='0.1.0.dev35'):
        path = self.cache / 'download.zip'
        _write_payload_zip(path, version=version)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        staged = path.rename(self.cache / f'{version}-{digest}.zip')
        atomic_json(
            self.cache / 'pending.json',
            {'version': version, 'size': staged.stat().st_size, 'sha256': digest},
        )
        return staged, digest

    def launch(self, *, ready=True):
        def start(arguments, *, cwd, env):
            self.launched = Path(arguments[0])
            if ready:
                Path(env['ONLINETRADER_UPDATE_READY']).touch()
            return process.return_value

        with (patch('online_trader.update_install.subprocess.Popen') as process,
              patch('online_trader.update_install.subprocess.call', return_value=0) as fallback):
            process.side_effect = start
            child = process.return_value.__enter__.return_value
            child.poll.return_value = 0
            child.wait.return_value = 0
            self.assertEqual(launch_managed(self.root, [], portable_version='0.1.0.dev34'), 0)
            self.fallback = Path(fallback.call_args.args[0][0]) if fallback.called else None

    def test_first_launch_selects_original_portable_program(self):
        self.assertEqual(managed_root(self.original), self.root)
        self.launch()
        self.assertEqual(active_version(self.root), '0.1.0.dev34')
        self.assertEqual(self.launched, self.original)

    def test_verified_update_preserves_portable_program_and_preferences(self):
        self.stage_update()
        self.launch()
        self.assertEqual(active_version(self.root), '0.1.0.dev35')
        self.assertEqual(self.launched, self.root / 'versions' / '0.1.0.dev35' / 'OnlineTrader.exe')
        self.assertEqual(managed_root(self.launched), self.root)
        self.assertEqual(self.original.read_bytes(), b'original executable')
        self.assertEqual(self.preferences.read_bytes(), b'original preferences')
        self.assertEqual((self.runtime_dir / 'OnlineTrader-overrides.json').read_text(encoding='utf-8'), '{"custom":true}\n')
        assert_payload_managed_launch(self.launched, [])

    def test_process_or_account_lock_defers_update(self):
        self.stage_update()
        with patch('online_trader.update_install.quiescent_installation',
                   side_effect=UpdateError('installation is active')):
            self.launch()
        self.assertEqual(active_version(self.root), '0.1.0.dev34')
        self.assertFalse((self.cache / '0.1.0.dev35.failed').exists())

    def test_failed_start_rolls_back_to_original_portable_program(self):
        self.stage_update()
        self.launch(ready=False)
        self.assertEqual(self.fallback, self.original)
        self.assertEqual(active_version(self.root), '0.1.0.dev34')
        self.assertTrue((self.cache / '0.1.0.dev35.failed').is_file())

    def test_extract_bundle_rejects_missing_payload_marker_and_root_configs(self):
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            no_marker = root / 'no-marker.zip'
            _write_payload_zip(no_marker, include_marker=False)
            with self.assertRaises(UpdateError):
                extract_bundle(no_marker, root / 'out1', expected_version='0.1.0.dev35')

            for idx, bad_member in enumerate((
                'OnlineTrader-parameters.json',
                'OnlineTrader-overrides.json',
                'update-source.json',
                'OnlineTrader.ini',
                'runtime/runtime.json',
            )):
                bad_zip = root / (bad_member.replace('/', '_') + '.zip')
                _write_payload_zip(bad_zip, extra_entries={bad_member: b'{}'})
                with self.assertRaises(UpdateError):
                    extract_bundle(bad_zip, root / f'out_{idx}', expected_version='0.1.0.dev35')

    def test_payload_guard_blocks_standalone_execution_and_allows_offline_smoke(self):
        with TemporaryDirectory() as scratch:
            standalone = Path(scratch)
            (standalone / 'runtime').mkdir()
            (standalone / 'runtime' / 'runtime.json').write_text('{}\n', encoding='utf-8')
            (standalone / PAYLOAD_MARKER).write_text(
                json.dumps(
                    {
                        'schema_version': PAYLOAD_SCHEMA,
                        'version': '0.1.0.dev40',
                        'standalone': False,
                        'requires_managed_root': True,
                    }
                ),
                encoding='utf-8',
            )
            gui_exe = standalone / 'OnlineTrader.exe'
            cli_exe = standalone / 'OnlineTraderCLI.exe'
            gui_exe.write_bytes(b'exe')
            cli_exe.write_bytes(b'cli')

            with self.assertRaises(TraderError) as ctx:
                assert_payload_managed_launch(gui_exe, [])
            self.assertEqual(ctx.exception.code, 'E_RUNTIME_CONFIG')
            with self.assertRaises(TraderError):
                assert_payload_managed_launch(cli_exe, ['runtime', 'run'])
            assert_payload_managed_launch(gui_exe, ['--update-smoke', '0.1.0.dev40'])
            assert_payload_managed_launch(cli_exe, ['--verify-runtime', '0.1.0.dev40'])

    def test_jihulab_source_client_and_redirect_token_isolation(self):
        atomic_json(
            self.root / 'update-source.json',
            {
                'host': 'jihulab.com',
                'project_id': 389806,
                'channel': 'preview',
                'portable_version': '0.1.0.dev34',
                'read_token': 'read-token-123',
            },
        )
        source = load_update_source(self.root)
        self.assertEqual(source['host'], 'jihulab.com')
        self.assertEqual(source['read_token'], 'read-token-123')

        atomic_json(self.root / 'active.json', {'version': '0.1.0.dev34'})
        payload_path = self.root / 'built.zip'
        _write_payload_zip(payload_path, version='0.1.0.dev35')
        payload_bytes = payload_path.read_bytes()
        payload_sha = hashlib.sha256(payload_bytes).hexdigest()

        seen_headers = []

        class _FakeResp(io.BytesIO):
            def __init__(self, body, status=200, headers=None):
                super().__init__(body)
                self.status = status
                self.headers = headers or {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def fake_open(req, timeout=10):
            url = req.full_url if isinstance(req, Request) else str(req)
            hdrs = dict(req.header_items()) if isinstance(req, Request) else {}
            seen_headers.append((url, hdrs))
            self.assertTrue(url.startswith('https://jihulab.com/api/v4/projects/389806/'))
            self.assertEqual(hdrs.get('Private-token'), 'read-token-123')
            if '/releases?' in url:
                return _FakeResp(json.dumps([{'tag_name': 'onlinetrader-0.1.0.dev35'}]).encode('utf-8'))
            if '/packages?' in url:
                return _FakeResp(
                    json.dumps(
                        [{'id': 9, 'name': 'online-trader-windows-x64', 'version': '0.1.0.dev35', 'package_type': 'generic'}]
                    ).encode('utf-8')
                )
            if '/packages/9/package_files?' in url:
                return _FakeResp(
                    json.dumps(
                        [
                            {
                                'file_name': 'OnlineTrader-0.1.0.dev35-windows-x64.zip',
                                'size': len(payload_bytes),
                                'file_sha256': payload_sha,
                            }
                        ]
                    ).encode('utf-8')
                )
            if '/packages/generic/online-trader-windows-x64/0.1.0.dev35/' in url:
                return _FakeResp(payload_bytes)
            raise AssertionError(f'Unexpected URL: {url}')

        with patch('online_trader.updates.GitLab') as mock_cls:
            mock_cls.side_effect = lambda pid, host='gitlab.com', token=None: GitLab(
                pid, host=host, token=token, opener=fake_open
            )
            found = check_and_download(self.root, current='0.1.0.dev34')
        self.assertEqual(found, '0.1.0.dev35')
        self.assertTrue(seen_headers)

        redirects = GitLabRedirects(
            api_host='jihulab.com', allowed_hosts={'jihulab.com', 'cdn.example.invalid'}
        )
        req = Request(
            'https://jihulab.com/api/v4/projects/389806/packages/generic/x/1/y.zip',
            headers={'PRIVATE-TOKEN': 'secret'},
        )
        redirected = redirects.redirect_request(
            req, io.BytesIO(b''), 302, 'Found', {}, 'https://cdn.example.invalid/blob.zip'
        )
        self.assertIsNotNone(redirected)
        self.assertNotIn('Private-token', dict(redirected.header_items()))
        with self.assertRaises(UpdateError):
            redirects.redirect_request(
                req, io.BytesIO(b''), 302, 'Found', {}, 'https://untrusted.example.com/blob.zip'
            )

    def test_publish_gitlab_update_uses_publish_token_when_read_token_absent(self):
        import scripts.publish_gitlab_update as pub

        archive = self.root / 'OnlineTrader-0.1.0.dev40-windows-x64.zip'
        _write_payload_zip(archive, version='0.1.0.dev40')
        token_file = self.root / 'publish-token.txt'
        token_file.write_text('publish-rw-token\n', encoding='utf-8')

        created_clients = []

        class _FakeClient:
            def __init__(self, project_id, *, host='gitlab.com', token=None):
                self.project_id = project_id
                self.host = host
                self.token = token
                self.api = f'https://{host}/api/v4/projects/{project_id}'
                created_clients.append(self)

            def pages(self, path, **_params):
                return []

            def metadata(self, version):
                if self.token != 'publish-rw-token':
                    raise AssertionError(f'Expected publish token for read-back, got {self.token!r}')
                from online_trader.updates import Candidate
                raw = archive.read_bytes()
                return Candidate(version, len(raw), hashlib.sha256(raw).hexdigest())

            def download(self, expected, dest):
                if self.token != 'publish-rw-token':
                    raise AssertionError(f'Expected publish token for download, got {self.token!r}')
                dest.write_bytes(archive.read_bytes())

        class _UploadReply:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *_): return False

        class _Opener:
            def open(self, req, timeout=300):
                return _UploadReply()

        with (patch.object(pub, 'GitLab', _FakeClient),
              patch.object(pub, 'build_opener', return_value=_Opener())):
            rc = pub.main([
                '--host', 'jihulab.com',
                '--version', '0.1.0.dev40',
                '--archive', str(archive),
                '--project-id', '389806',
                '--ref', 'main',
                '--token-file', str(token_file),
                '--publish',
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(created_clients), 1)
        self.assertEqual(created_clients[0].token, 'publish-rw-token')

    def test_check_on_launch_downloads_and_upgrades_in_same_startup(self):
        payload = self.root / 'remote-dev35.zip'
        _write_payload_zip(payload, version='0.1.0.dev35')
        payload_bytes = payload.read_bytes()
        payload_sha = hashlib.sha256(payload_bytes).hexdigest()

        from online_trader.updates import Candidate
        candidate = Candidate('0.1.0.dev35', len(payload_bytes), payload_sha)

        class _FakeGitLab:
            def __init__(self, project_id, *, host='gitlab.com', token=None):
                pass

            def latest(self, current, channel):
                return candidate if current == '0.1.0.dev34' else None

            def download(self, cand, dest, *, on_progress=None):
                if on_progress is not None:
                    on_progress('DOWNLOADING', cand.version, len(payload_bytes), len(payload_bytes))
                dest.write_bytes(payload_bytes)

        progress_events = []
        with patch('online_trader.updates.GitLab', _FakeGitLab):
            def run_check(root, current):
                startup_check_and_download(
                    root, current, on_progress=lambda *args: progress_events.append(args)
                )

            def start(arguments, *, cwd, env):
                self.launched = Path(arguments[0])
                Path(env['ONLINETRADER_UPDATE_READY']).touch()
                return process.return_value

            with (patch('online_trader.update_install.subprocess.Popen') as process,
                  patch('online_trader.update_install.subprocess.call', return_value=0)):
                process.side_effect = start
                child = process.return_value.__enter__.return_value
                child.poll.return_value = 0
                child.wait.return_value = 0
                rc = launch_managed(
                    self.root, [], portable_version='0.1.0.dev34',
                    check_on_launch=True, startup_check_fn=run_check,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(active_version(self.root), '0.1.0.dev35')
        self.assertEqual(self.launched, self.root / 'versions' / '0.1.0.dev35' / 'OnlineTrader.exe')
        self.assertTrue(any(ev[0] == 'CHECKING' for ev in progress_events))
        self.assertTrue(any(ev[0] == 'DOWNLOADING' for ev in progress_events))
        self.assertTrue(any(ev[0] == 'VERIFYING' for ev in progress_events))

    def test_check_on_launch_uses_active_version_when_already_upgraded(self):
        atomic_json(self.root / 'active.json', {'version': '0.1.0.dev35'})
        (self.root / 'versions' / '0.1.0.dev35').mkdir(parents=True)
        payload = self.root / 'remote-dev36.zip'
        _write_payload_zip(payload, version='0.1.0.dev36')
        payload_bytes = payload.read_bytes()
        payload_sha = hashlib.sha256(payload_bytes).hexdigest()

        from online_trader.updates import Candidate
        candidate = Candidate('0.1.0.dev36', len(payload_bytes), payload_sha)
        seen_currents = []

        class _FakeGitLab:
            def __init__(self, project_id, *, host='gitlab.com', token=None):
                pass

            def latest(self, current, channel):
                seen_currents.append(current)
                return candidate if current == '0.1.0.dev35' else None

            def download(self, cand, dest, *, on_progress=None):
                dest.write_bytes(payload_bytes)

        with patch('online_trader.updates.GitLab', _FakeGitLab):
            def start(arguments, *, cwd, env):
                self.launched = Path(arguments[0])
                Path(env['ONLINETRADER_UPDATE_READY']).touch()
                return process.return_value

            with (patch('online_trader.update_install.subprocess.Popen') as process,
                  patch('online_trader.update_install.subprocess.call', return_value=0)):
                process.side_effect = start
                child = process.return_value.__enter__.return_value
                child.poll.return_value = 0
                child.wait.return_value = 0
                rc = launch_managed(
                    self.root, [], portable_version='0.1.0.dev34', check_on_launch=True,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(seen_currents, ['0.1.0.dev35'])
        self.assertEqual(active_version(self.root), '0.1.0.dev36')
        self.assertEqual(self.launched, self.root / 'versions' / '0.1.0.dev36' / 'OnlineTrader.exe')

    def test_check_on_launch_network_error_falls_back_to_current_version(self):
        with patch('online_trader.update_install.check_and_download', side_effect=OSError('network unreachable')):
            def start(arguments, *, cwd, env):
                self.launched = Path(arguments[0])
                return process.return_value

            with (patch('online_trader.update_install.subprocess.Popen') as process,
                  patch('online_trader.update_install.subprocess.call', return_value=0)):
                process.side_effect = start
                child = process.return_value.__enter__.return_value
                child.poll.return_value = 0
                child.wait.return_value = 0
                rc = launch_managed(
                    self.root, [], portable_version='0.1.0.dev34', check_on_launch=True,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(active_version(self.root), '0.1.0.dev34')
        self.assertEqual(self.launched, self.original)
        status = json.loads((self.cache / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['stage'], 'FAILED')
