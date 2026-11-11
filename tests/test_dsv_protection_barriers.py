"""A reconciliation barrier cannot manufacture the next DSV market event."""

import unittest

from online_trader.protection import initial_state, reduce_event
from test_dsv_protection import plan


class DsvProtectionBarrierTests(unittest.TestCase):
    def test_repeated_entry_reconciliation_cannot_consume_a_new_second_stage(self):
        value=plan(quantity=5)
        state=initial_state(value)
        for event in (
            {"type":"FILL","quantity":5,"price":"1000"},
            {"type":"ENTRY_TERMINAL_RECONCILED","owned_quantity":5,"reconciliation_id":"first-proof"},
            {"type":"QUOTE","price":"1035"},
        ):
            state=reduce_event(value,state,event).state
        self.assertEqual(state.phase1_status,"TARGET_COMMITTED")
        self.assertEqual(state.phase2_status,"NOT_TRIGGERED")
        self.assertEqual(state.target_remaining_qty,3)
        observed=state.last_observed_event_seq
        state=reduce_event(value,state,{"type":"ENTRY_TERMINAL_RECONCILED",
            "owned_quantity":5,"reconciliation_id":"second-proof"}).state
        self.assertEqual(state.phase2_status,"NOT_TRIGGERED")
        self.assertEqual(state.target_remaining_qty,3)
        self.assertEqual(state.last_observed_event_seq,observed)
        state=reduce_event(value,state,{"type":"QUOTE","price":"1035"}).state
        self.assertEqual(state.phase2_status,"TARGET_COMMITTED")
        self.assertEqual(state.target_remaining_qty,1)

    def test_a_latched_first_stage_still_materializes_after_pullback_and_terminal_proof(self):
        value=plan(quantity=5)
        state=initial_state(value)
        for event in (
            {"type":"FILL","quantity":5,"price":"1000"},
            {"type":"QUOTE","price":"1035"},
            {"type":"QUOTE","price":"1010"},
        ):
            state=reduce_event(value,state,event).state
        self.assertEqual(state.phase1_status,"WAITING_ENTRY_TERMINAL")
        self.assertIsNone(state.target_remaining_qty)
        state=reduce_event(value,state,{"type":"ENTRY_TERMINAL_RECONCILED",
            "owned_quantity":5,"reconciliation_id":"complete-proof"}).state
        self.assertEqual(state.phase1_status,"TARGET_COMMITTED")
        self.assertEqual(state.phase2_status,"NOT_TRIGGERED")
        self.assertEqual(state.target_remaining_qty,3)
        self.assertEqual(state.q0,5)
        self.assertEqual(state.q0_reconciliation_id,"complete-proof")


if __name__=="__main__":unittest.main()
