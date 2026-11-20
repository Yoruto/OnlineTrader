"""Manual-only Runtime Workbench command contract tests."""

from pathlib import Path
import tempfile
import unittest

from online_trader.errors import TraderError
from online_trader.runtime.operator_api import (
    WORKBENCH_OPERATIONS,
    WORKBENCH_PROTOCOL,
    WorkbenchEndpoint,
    validate_workbench_wire,
)


class RuntimeOperatorContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.endpoint = WorkbenchEndpoint('Runtime.Workbench.Test', self.root,
                                           self.root / 'ledger.db', 'TQKQ:configured-user')

    def request(self, operation='submit', body=None):
        return {'protocol': WORKBENCH_PROTOCOL, 'request_id': 'request-workbench',
                'operation': operation, 'body': {} if body is None else body}

    def test_local_workbench_wire_has_no_operator_or_token(self):
        request = self.request('receipt', {'command_id': 'manual-close-one'})
        validate_workbench_wire(request)
        self.assertNotIn('operator', request)
        self.assertNotIn('token', request)

    def test_retired_operator_wire_is_rejected(self):
        request = self.request('receipt', {'command_id': 'manual-close-one'})
        request.update(operator='desk', token='retired-secret')
        with self.assertRaises(TraderError) as caught:
            validate_workbench_wire(request)
        self.assertEqual(caught.exception.code, 'E_SCHEMA')

    def test_workbench_capabilities_are_a_closed_manual_set(self):
        self.assertEqual(WORKBENCH_OPERATIONS, frozenset({
            'ADOPT_EXTERNAL_POSITION', 'ARM_ENTRIES', 'CANCEL_NATIVE_ACCOUNT',
            'CANCEL_OWNED', 'CANCEL_SELECTED', 'FLATTEN_THEN_STOP', 'MANUAL_CLOSE',
            'MANUAL_CLOSE_ALL', 'MANUAL_OPEN', 'PAUSE_NEW_ENTRIES',
            'REQUEST_RECONCILIATION', 'SAVE_DSV_PARAMETERS', 'SET_ENTRY_POLICY',
            'STOP_DSV_STRATEGY',
        }))

    def test_scheduler_capability_is_rejected_before_ledger_access(self):
        command = {'command_id': 'schedule-denied', 'operation': 'SCHEDULE_STRATEGY',
                   'scope': {'scope_type': 'ACCOUNT', 'scope_ids': ['TQKQ:configured-user']},
                   'parameters': {}}
        with self.assertRaises(TraderError) as caught:
            self.endpoint.handle(self.request('submit', command))
        self.assertEqual(caught.exception.code, 'E_AUTH')
        self.assertFalse((self.root / 'ledger.db').exists())


if __name__ == '__main__':
    unittest.main()
