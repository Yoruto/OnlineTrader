"""Runtime public IPC process rehearsal; offline native-shaped facts only."""

import json
from pathlib import Path
import subprocess
import sys
import unittest
import uuid


class RuntimeProcessTests(unittest.TestCase):
    def test_partial_fill_and_close_use_runtime_public_clients(self):
        root = Path(__file__).parents[1]
        output = root / 'output/runtime-validation' / ('test-' + uuid.uuid4().hex)
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', '-m', 'qa.runtime_replay', '--output', str(output),
             '--fixture', 'partial-fill-and-close'], cwd=root, capture_output=True, text=True,
            encoding='utf-8', timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads((output / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(value['status'], 'PASS')
        self.assertEqual(value['source'], 'OFFLINE_NATIVE_SHAPED_FACTS')
        self.assertFalse(value['broker_connected'])
        self.assertEqual(value['real_orders_sent'], 0)
        self.assertEqual(value['observations']['authentication']['status'], 'AUTHENTICATED')
        self.assertEqual(value['observations']['account']['status'], 'READY')
        self.assertIn('observations', value['observations']['history'])
        self.assertEqual(value['observations']['receipt']['status'], 'SUCCEEDED')
        self.assertEqual(value['observations']['duplicate'], value['observations']['receipt'])
        self.assertEqual(value['observations']['duplicate_conflict'], 'E_DUPLICATE_CONFLICT')


if __name__ == '__main__':
    unittest.main()
