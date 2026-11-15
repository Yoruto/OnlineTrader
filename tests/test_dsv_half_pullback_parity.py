"""Frozen DSV gui.decide_half_close (1847-1879) and _run (7621-7623).

Directly exercise the product protect() function; no SDK, account or network.
Expected targets are literal source-derived cases, not candidate-generated data.
"""

import copy
import unittest

from online_trader.dsv_core.transition import protect


def settings(**changes):
    return {
        'protect_80_enabled': False, 'protect_80_ratio': '0.8',
        'protect_keep_ratio': '0.5', 'half_close_enabled': True,
        'half_stage2': True, 'half_stage2_trigger_ratio': '0.6',
        'half_pullback_close': True, 'trailing_activate_ratio': '0',
        'trailing_pullback_ratio': '0.1', **changes,
    }


def position(**changes):
    return {
        'direction': 'LONG', 'owned': 4, 'exit_target': None,
        'theory': '1000', 'stop': '20', 'profit': '100',
        'peak': '45', 'protected': False, 'half_stage': 1, **changes,
    }


class HalfPullbackParityTests(unittest.TestCase):
    def apply(self, move=35, *, state=None, config=None):
        state = position() if state is None else state
        price = 1000 + move if state['direction'] == 'LONG' else 1000 - move
        return protect(state, {'last': str(price)}, settings() if config is None else config,
                       {'half_close_ratio': '0.4'})

    def test_stage_two_pullback_reduces_four_to_two(self):
        updated, events = self.apply()
        self.assertEqual(updated['exit_target'], 2)
        self.assertEqual(updated['half_stage'], 2)
        self.assertEqual(events, [{'reason': 'PARTIAL_STAGE_2', 'remaining': 2}])

    def test_pullback_threshold_is_inclusive(self):
        updated, events = self.apply(40)
        self.assertEqual(updated['exit_target'], 2)
        self.assertEqual(events[0]['reason'], 'PARTIAL_STAGE_2')

    def test_between_thresholds_does_not_reduce(self):
        updated, events = self.apply(41)
        self.assertIsNone(updated['exit_target'])
        self.assertEqual(events, [])

    def test_second_upward_threshold_still_works(self):
        for move in (60, 65):
            with self.subTest(move=move):
                updated, events = self.apply(move, config=settings(half_pullback_close=False))
                self.assertEqual(updated['exit_target'], 2)
                self.assertEqual(events, [{'reason': 'PARTIAL_STAGE_2', 'remaining': 2}])

    def test_second_stage_disabled_disables_pullback_too(self):
        updated, events = self.apply(config=settings(half_stage2=False))
        self.assertIsNone(updated['exit_target'])
        self.assertEqual(events, [])

    def test_pullback_disabled(self):
        updated, events = self.apply(config=settings(half_pullback_close=False))
        self.assertIsNone(updated['exit_target'])
        self.assertEqual(events, [])

    def test_missing_optional_switch_preserves_old_profile_behavior(self):
        config = settings()
        config.pop('half_pullback_close')
        updated, events = self.apply(config=config)
        self.assertIsNone(updated['exit_target'])
        self.assertEqual(events, [])

    def test_half_close_disabled(self):
        updated, events = self.apply(config=settings(half_close_enabled=False))
        self.assertIsNone(updated['exit_target'])
        self.assertEqual(events, [])

    def test_completed_stage_does_not_repeat(self):
        updated, events = self.apply(state=position(half_stage=2))
        self.assertEqual(updated['half_stage'], 2)
        self.assertEqual(events, [])

    def test_one_remaining_lot_is_not_halved(self):
        updated, events = self.apply(state=position(owned=1))
        self.assertIsNone(updated['exit_target'])
        self.assertEqual(events, [])

    def test_odd_lots_keep_source_round_half_even(self):
        for owned, target in ((2, 1), (3, 1), (5, 3), (7, 3)):
            with self.subTest(owned=owned):
                updated, events = self.apply(state=position(owned=owned))
                self.assertEqual(updated['exit_target'], target)
                self.assertEqual(events[0]['remaining'], target)

    def test_short_direction_has_same_profit_thresholds(self):
        updated, events = self.apply(state=position(direction='SHORT'))
        self.assertEqual(updated['exit_target'], 2)
        self.assertEqual(events[0]['remaining'], 2)

    def test_pending_first_reduction_uses_single_virtual_remaining_target(self):
        updated, events = self.apply(state=position(owned=8, exit_target=4))
        self.assertEqual(updated['owned'], 8)
        self.assertEqual(updated['exit_target'], 2)
        self.assertEqual(events, [{'reason': 'PARTIAL_STAGE_2', 'remaining': 2}])

    def test_updated_stage_prevents_repeat_while_fill_is_pending(self):
        updated, first = self.apply()
        repeated, events = self.apply(state=updated)
        self.assertEqual(len(first), 1)
        self.assertEqual(repeated['exit_target'], 2)
        self.assertEqual(events, [])

    def test_full_exit_target_cannot_be_reopened(self):
        state = position(exit_target=0)
        updated, events = self.apply(state=state)
        self.assertEqual(updated['exit_target'], 0)
        self.assertEqual(events, [])

    def test_existing_protection_takes_precedence(self):
        updated, events = self.apply(state=position(protected=True))
        self.assertEqual(updated['exit_target'], 0)
        self.assertEqual(updated['half_stage'], 1)
        self.assertEqual([event['reason'] for event in events], ['PROTECTED_FLOOR'])

    def test_stop_loss_tightens_same_round_partial_target_to_zero(self):
        updated, events = self.apply(-20)
        self.assertEqual(updated['exit_target'], 0)
        self.assertEqual([event['reason'] for event in events], ['PARTIAL_STAGE_2', 'STOP_LOSS'])

    def test_first_stage_is_not_a_pullback_rule(self):
        state = position(half_stage=0, owned=8)
        updated, events = self.apply(35, state=state)
        self.assertIsNone(updated['exit_target'])
        self.assertEqual(events, [])
        updated, events = self.apply(40, state=state)
        self.assertEqual(updated['exit_target'], 4)
        self.assertEqual(events, [{'reason': 'PARTIAL_STAGE_1', 'remaining': 4}])

    def test_does_not_mutate_input_position(self):
        state = position()
        before = copy.deepcopy(state)
        self.apply(state=state)
        self.assertEqual(state, before)


if __name__ == '__main__':
    unittest.main()
