"""Runtime supervisor configuration stays explicit and separate from durable trading facts."""

import json
from pathlib import Path
import tempfile
import unittest

from online_trader.errors import TraderError
from online_trader.runtime.portable_config import read_runtime_control


class RuntimeControlConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value):
        (self.root / 'runtime-control.json').write_text(json.dumps(value), encoding='utf-8')

    def test_explicit_scheduled_start_gate_is_loaded(self):
        self.write({'schema_version': 'ONLINE_TRADER_RUNTIME_CONTROL/1',
                    'scheduled_start': {'enabled': True}})
        self.assertTrue(read_runtime_control(self.root)['scheduled_start']['enabled'])

    def test_missing_control_file_fails_closed_for_scheduler(self):
        with self.assertRaises(TraderError) as caught:
            read_runtime_control(self.root)
        self.assertEqual(caught.exception.code, 'E_RUNTIME_CONFIG')

    def test_unknown_control_fields_are_rejected(self):
        self.write({'schema_version': 'ONLINE_TRADER_RUNTIME_CONTROL/1',
                    'scheduled_start': {'enabled': False, 'unexpected': True}})
        with self.assertRaises(TraderError) as caught:
            read_runtime_control(self.root)
        self.assertEqual(caught.exception.code, 'E_RUNTIME_CONFIG')


if __name__ == '__main__':
    unittest.main()
