from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from online_trader.errors import TraderError
from online_trader.runtime.startup_environment import DesktopRuntime, launch_bootstrap, process_running, resolve_environment


class StartupEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source_files(self):
        package = self.root / 'src' / 'online_trader' / '__init__.py'
        runner = self.root / 'scripts' / 'run_online_trader.py'
        python = self.root / 'output' / 'online' / 'sdk-venv' / 'Scripts' / 'python.exe'
        for path in (package, runner, python, self.root / 'pyproject.toml'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return package, runner, python

    def test_source_resolves_sdk_and_current_source_runner_without_spawning(self):
        package, runner, python = self.source_files()
        with (patch('online_trader.runtime.startup_environment.bundled_cli', return_value=None),
              patch('online_trader.runtime.startup_environment.online_trader.__file__', str(package)),
              patch('subprocess.run') as probe, patch('subprocess.Popen') as spawn):
            runtime = resolve_environment()
        self.assertEqual(runtime.executable, python)
        self.assertEqual(runtime.source_runner, runner)
        self.assertEqual(runtime.command('desktop-start'), [str(python), '-I', '-X', 'utf8', str(runner), 'desktop-start'])
        probe.assert_not_called()
        spawn.assert_not_called()

    def test_missing_source_sdk_refuses_instead_of_using_stale_or_core_python(self):
        package, runner, python = self.source_files()
        python.unlink()
        with (patch('online_trader.runtime.startup_environment.bundled_cli', return_value=None),
              patch('online_trader.runtime.startup_environment.online_trader.__file__', str(package)),
              self.assertRaises(TraderError) as raised):
            resolve_environment()
        self.assertEqual(raised.exception.code, 'E_DEPENDENCY')

    def test_bundled_runtime_keeps_sibling_cli_protocol(self):
        executable = self.root / 'OnlineTraderCLI.exe'
        executable.touch()
        with patch('online_trader.runtime.startup_environment.bundled_cli', return_value=executable):
            runtime = resolve_environment()
        self.assertEqual(runtime.command('desktop-start'), [str(executable), 'desktop-start'])
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=0)) as probe:
            runtime.probe()
        self.assertEqual(probe.call_args.args[0][:2], [str(executable), '--verify-runtime'])

    def test_probe_exercises_same_source_runner_and_checks_resolved_package_identity(self):
        package, runner, python = self.source_files()
        runtime = DesktopRuntime(python, runner, package)
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=0)) as probe:
            runtime.probe()
        arguments = probe.call_args.args[0]
        self.assertEqual(arguments[:5], [str(python), '-I', '-X', 'utf8', '-c'])
        script = arguments[-1]
        self.assertIn(repr(str(runner)), script)
        self.assertIn(repr(str(package)), script)
        self.assertIn("run_name='online_trader_desktop_probe'", script)
        self.assertIn("metadata.version('tqsdk') == '3.10.4'", script)
        self.assertNotIn('TqApi', script)

    def test_failed_or_timed_out_probe_exposes_no_subprocess_details_and_never_launches(self):
        package, runner, python = self.source_files()
        request = self.root / 'request.json'
        request.write_text('{}', encoding='utf-8')
        runtime = DesktopRuntime(python, runner, package)
        for error in (None, subprocess.TimeoutExpired('private-interpreter', 15)):
            with self.subTest(timeout=error is not None):
                with (patch('subprocess.run', return_value=SimpleNamespace(returncode=1, stderr=b'secret'), side_effect=error),
                      patch('subprocess.Popen') as spawn, self.assertRaises(TraderError) as raised):
                    launch_bootstrap(request, self.root / 'run', runtime=runtime)
                spawn.assert_not_called()
                self.assertNotIn('secret', str(raised.exception))
                self.assertNotIn('private-interpreter', str(raised.exception))
                self.assertFalse((self.root / 'run').exists())

    def test_launch_uses_only_request_reference_and_leaves_output_creation_to_bootstrap(self):
        package, runner, python = self.source_files()
        request = self.root / 'request.json'
        request.write_text('{"account_id":"local-account-reference"}', encoding='utf-8')
        output = self.root / 'run'
        with (patch('subprocess.run', return_value=SimpleNamespace(returncode=0)),
              patch('subprocess.Popen', return_value=SimpleNamespace(pid=42)) as spawn):
            result = launch_bootstrap(request, output, runtime=DesktopRuntime(python, runner, package))
        arguments = spawn.call_args.args[0]
        self.assertEqual(arguments[-5:], ['desktop-start', '--request', str(request), '--output', str(output)])
        self.assertNotIn('local-account-reference', ' '.join(arguments))
        self.assertFalse(output.exists())
        self.assertTrue(Path(result['log_path']).is_file())
        self.assertEqual(result['pid'], 42)
        self.assertFalse(result['broker_connected'])
        self.assertEqual(spawn.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_existing_output_or_log_never_restarts_previous_bootstrap(self):
        package, runner, python = self.source_files()
        request = self.root / 'request.json'
        request.write_text('{}', encoding='utf-8')
        output = self.root / 'run'
        output.mkdir()
        with patch('subprocess.run') as probe, self.assertRaises(TraderError):
            launch_bootstrap(request, output, runtime=DesktopRuntime(python, runner, package))
        probe.assert_not_called()
        output.rmdir()
        (self.root / 'run.executor.log').write_text('previous evidence', encoding='utf-8')
        with (patch('subprocess.run', return_value=SimpleNamespace(returncode=0)),
              patch('subprocess.Popen') as spawn, self.assertRaises(TraderError)):
            launch_bootstrap(request, output, runtime=DesktopRuntime(python, runner, package))
        spawn.assert_not_called()
        self.assertEqual((self.root / 'run.executor.log').read_text('utf-8'), 'previous evidence')

    def test_process_state_reads_only_and_closes_handle_for_live_or_exited_process(self):
        for exit_code, expected in ((259, True), (0, False), (2, False)):
            api = Mock()
            api.OpenProcess.return_value = 123
            def state(handle, destination):
                destination._obj.value = exit_code
                return True
            api.GetExitCodeProcess.side_effect = state
            with (patch('online_trader.runtime.startup_environment.sys.platform', 'win32'),
                  patch('ctypes.WinDLL', return_value=api)):
                self.assertIs(process_running(42), expected)
            api.OpenProcess.assert_called_once_with(0x1000, False, 42)
            api.CloseHandle.assert_called_once_with(123)
            api.TerminateProcess.assert_not_called()

    def test_missing_process_is_distinct_from_access_denied_or_failed_query(self):
        api = Mock()
        api.OpenProcess.return_value = None
        with (patch('online_trader.runtime.startup_environment.sys.platform', 'win32'),
              patch('ctypes.WinDLL', return_value=api), patch('ctypes.get_last_error', return_value=87)):
            self.assertIs(process_running(42), False)
        with (patch('online_trader.runtime.startup_environment.sys.platform', 'win32'),
              patch('ctypes.WinDLL', return_value=api), patch('ctypes.get_last_error', return_value=5)):
            self.assertIsNone(process_running(42))
        api.OpenProcess.return_value = 123
        api.GetExitCodeProcess.return_value = False
        with (patch('online_trader.runtime.startup_environment.sys.platform', 'win32'),
              patch('ctypes.WinDLL', return_value=api)):
            self.assertIsNone(process_running(42))
        api.CloseHandle.assert_called_once_with(123)

    def test_invalid_pid_cannot_issue_process_queries(self):
        with patch('ctypes.WinDLL') as windows:
            for pid in (None, -1, 0, True, '42'):
                self.assertIsNone(process_running(pid))
        windows.assert_not_called()
