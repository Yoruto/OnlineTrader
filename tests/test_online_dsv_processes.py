from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest


class DsvProcessTests(unittest.TestCase):
    def test_real_pipe_pause_retains_second_stage_and_original_protection(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'rehearsal'
            run=subprocess.run([sys.executable,'-I','-X','utf8',str(root/'qa/dsv_session_rehearsal.py'),'--output',str(out)],
                cwd=root,capture_output=True,encoding='utf-8',timeout=35,env=os.environ|{'PYTHONUTF8':'1'},
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            result=json.loads((out/'result.json').read_text(encoding='utf-8'))
            self.assertFalse(result['broker_connected']);self.assertEqual(result['real_native_orders_sent'],0)
            self.assertEqual(result['final_owned_quantity'],1);self.assertTrue(result['pause_preserves_advanced_protection'])
