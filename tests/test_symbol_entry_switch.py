from concurrent.futures import Future
from types import SimpleNamespace
import unittest

from online_trader.kernel.workbench import workbench_view
from tests import test_control as control_fixtures
from tests import test_online_dsv as fixtures


class SymbolEntrySwitchTests(unittest.TestCase):
    def setUp(self):self.x=fixtures.OnlineDsvTests();self.x.setUp();self.x.start()
    def tearDown(self):self.x.tearDown()

    def command(self,enabled,*,cancel=True,code=None,wait=True):
        k=self.x.kernel;scope={'scope_type':'CONTRACT','scope_ids':[code or self.x.code]}
        if enabled:
            policy=k.ledger.query("SELECT policy_id FROM policy_revisions WHERE policy_kind='AUTHORITY'")[0]['policy_id']
            parameters={'authority_policy_id':policy,'rule_set_id':k._configuration['rule_set_id'],
                        'config_revision':str(k._config_revision)}
        else:parameters={'cancel_owned_entry_remainders':cancel}
        message=control_fixtures.ControlTests.message(SimpleNamespace(kernel=k,account=k.account_key),
            'ARM_ENTRIES' if enabled else 'PAUSE_NEW_ENTRIES',parameters,scope)
        future=k.execute_command(message,'desktop')
        if wait:k.wait_idle()
        return future,message

    def test_contract_pause_blocks_only_that_symbol_and_enable_does_not_arm_global(self):
        x=self.x;future,_=self.command(False)
        self.assertEqual(future.result()['status'],'SUCCEEDED');self.assertTrue(x.kernel._armed)
        self.assertFalse(x.kernel.control.can_increase(x.code));self.assertTrue(x.kernel.control.can_increase('DCE.l2701'))
        x.quote();self.assertEqual(x.api.inserted,[])
        x.kernel.pause();x.kernel.wait_idle();future,_=self.command(True)
        self.assertEqual(future.result()['status'],'SUCCEEDED');self.assertFalse(x.kernel._armed)
        self.assertTrue(x.kernel.control.can_increase(x.code))

    def test_enable_waits_for_correct_commit_and_pause_receipt_is_idempotent(self):
        k=self.x.kernel;_,message=self.command(False)
        replay=k.execute_command(message,'desktop');self.assertEqual(replay.result()['status'],'SUCCEEDED')
        future,_=self.command(True,wait=False);pending=k._pending[0];ack=pending.future.result(timeout=3)
        held=Future();pending.future=held;k.poll()
        self.assertFalse(k.control.can_increase(self.x.code));self.assertFalse(future.done())
        held.set_result(ack);k.wait_idle();self.assertTrue(k.control.can_increase(self.x.code))

    def test_symbol_pause_cancels_entry_remainder_and_keeps_original_stop_exit(self):
        x=self.x;x.quote();opening=x.api.inserted[0]
        x.api.fill(opening['order_id'],2,1055);x.session.step()
        future,_=self.command(False)
        self.assertIn(opening['order_id'],x.api.cancelled)
        self.assertEqual(x.cycle()['protection']['owned_qty'],2)
        self.assertIsNone(x.cycle()['protection']['target_remaining_qty'])
        x.quote(1038)
        self.assertEqual(x.api.inserted[-1]['offset'],'CLOSE');self.assertEqual(x.api.inserted[-1]['volume'],2)
        self.assertFalse(x.kernel.control.can_increase(x.code))

    def test_switch_survives_owner_restart_and_is_visible_at_accepted_watermark(self):
        x=self.x;self.command(False);self.assertFalse(x.kernel.control.symbol_entry_enabled(x.code,committed=True))
        x.restart();self.assertFalse(x.kernel.control.can_increase(x.code))
        x.quote();self.assertEqual(x.api.inserted,[])
        view=workbench_view(x.kernel)
        self.assertFalse(view['symbol_entry_switches'][x.code]['enabled'])
        self.assertFalse(view['markets'][-1]['enabled'])
        self.assertFalse(view['markets'][-1]['fresh'])
        self.assertIn('WAITING_FRESH_QUOTE',view['markets'][-1]['entry_reasons'])

    def test_out_of_scope_symbol_is_rejected_without_switch_or_account_pause(self):
        future,_=self.command(False,code='DCE.l2701')
        self.assertEqual(future.result()['status'],'REJECTED')
        self.assertEqual(self.x.kernel.control.symbol_entries,{})
        self.assertTrue(self.x.kernel._armed)
