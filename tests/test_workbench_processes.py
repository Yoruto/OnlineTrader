from pathlib import Path
import os
import json
import subprocess
import sys
import unittest
import uuid


class WorkbenchProcessTests(unittest.TestCase):
    def test_product_workbench_view_separate_cli_pause_preserves_advanced_protection(self):
        root=Path(__file__).parents[1];out=root/'output/workbench'/('test-'+uuid.uuid4().hex)
        result=subprocess.run([sys.executable,'-X','utf8','-m','qa.workbench_rehearsal','--output',str(out)],cwd=root,
            capture_output=True,text=True,encoding='utf-8',timeout=38,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        value=json.loads((out/'result.json').read_text(encoding='utf-8'))
        self.assertEqual(value['status'],'PASS');self.assertFalse(value['broker_connected']);self.assertEqual(value['final_owned_quantity'],1)
        self.assertFalse(value['development_kernel']);self.assertEqual(value['execution_scope'],'ONLINE_SIMULATION')
        self.assertTrue(value['client_disconnect_kept_owner_protection']);self.assertIsNone(value['gui_close_kept_owner_protection'])
