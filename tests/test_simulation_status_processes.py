from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest


class HeldStatusProcessTests(unittest.TestCase):
    def run_rehearsal(self, *args):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'rehearsal'
            run = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(root/'qa/simulation_status_rehearsal.py'),
                                  '--output', str(output), *args], cwd=root, capture_output=True,
                                 encoding='utf-8', timeout=30, env=os.environ|{'PYTHONUTF8':'1'},
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
            report = json.loads((output/'result.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'PASS'); self.assertFalse(report['broker_connected'])
            self.assertEqual(report['remaining_owned_quantity'], 1)
            self.assertEqual(report['entry_authority'], 'DISARMED')
            return report

    def test_held_snapshot_and_durable_pause_succeed_while_reference_is_slow(self):
        report = self.run_rehearsal()
        self.assertFalse(report['outcomes'][0]['refresh_finished'])
        self.assertFalse(report['outcomes'][1]['refresh_finished'])

    def test_negotiated_snapshot_finishes_during_bounded_owner_shutdown(self):
        self.assertTrue(self.run_rehearsal('--closing-read')['read_overlapped_owner_stop'])
