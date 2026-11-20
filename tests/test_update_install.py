import hashlib
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from online_trader.update_install import active_version, launch_managed, managed_root
from online_trader.updates import UpdateError, atomic_json


class UpdateInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / 'OnlineTrader.exe'
        self.original.write_bytes(b'original executable')
        self.preferences = self.root / 'OnlineTrader.ini'
        self.preferences.write_bytes(b'original preferences')
        self.cache = self.root / 'updates'
        self.cache.mkdir()
        atomic_json(self.root / 'update-source.json', {'project_id': 89047383, 'channel': 'preview',
                                                      'portable_version': '0.1.0.dev34'})
        for name in ('launcher_lock', 'runtime_lease', 'quiescent_installation'):
            patcher = patch(f'online_trader.update_install.{name}', side_effect=lambda *args, **kw: nullcontext())
            self.addCleanup(patcher.stop)
            patcher.start()
        checker = patch('online_trader.update_install._offline_check')
        self.addCleanup(checker.stop)
        checker.start()

    def stage_update(self):
        path = self.cache / 'download.zip'
        with ZipFile(path, 'w') as archive:
            archive.writestr('OnlineTrader.exe', b'new workbench')
            archive.writestr('OnlineTraderCLI.exe', b'new command line')
            archive.writestr('_internal/runtime', b'new runtime')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        staged = path.rename(self.cache / f'0.1.0.dev35-{digest}.zip')
        atomic_json(self.cache / 'pending.json', {'version': '0.1.0.dev35', 'size': staged.stat().st_size,
                                                  'sha256': digest})

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
