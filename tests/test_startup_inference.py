"""Unit tests for strict Plan B startup target inference."""

from __future__ import annotations

import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.dsv_runtime.startup_inference import infer_startup_symbol_state


DAY = '2026-11-13'
CONTRACT = 'DCE.p2701'


def _spec_and_trade():
    settings = load_confirmed_baseline()[0].to_settings()
    spec = settings['symbols'][0] | {
        'code': CONTRACT,
        'open_offset': 40,
        'stop_offset': 8,
        'profit_offset': 20,
        'order_volume': 5,
        'half_close_ratio': '0.5',
    }
    trade = settings['trade'] | {
        'max_volume': 10,
        'max_daily_orders': 4,
        'max_net_loss_stops': 2,
        'participate_auction': False,
        'no_open_after_open': False,
        'half_close_enabled': True,
        'protect_80_enabled': False,
        'trailing_activate_ratio': 0,
        'stop_day_on_profit': True,
    }
    return spec, trade


def _market(*, long_today=0, long_his=0, short_today=0, short_his=0, last='1062'):
    return {
        'contract_id': CONTRACT,
        'long_qty': long_today + long_his,
        'short_qty': short_today + short_his,
        'long_today': long_today,
        'long_historical': long_his,
        'short_today': short_today,
        'short_historical': short_his,
        'opening_session': True,
        'session_open': f'{DAY}T01:00:00Z',
        'session_close': f'{DAY}T02:15:00Z',
        'tick': {'native_time': f'{DAY}T01:15:00Z', 'last': last, 'day_high': '1065', 'day_low': '1020'},
    }


def _bars(rows):
    return [
        {
            'bar_id': f'bar-{idx}',
            'time_utc': f'{DAY}T01:{10 + idx:02d}:00Z',
            'open': o, 'high': h, 'low': l, 'close': c,
        }
        for idx, (o, h, l, c) in enumerate(rows)
    ]


class StartupInferencePlanBTests(unittest.TestCase):
    def test_proved_long_entry_and_partial_excess_flattening(self):
        spec, trade = _spec_and_trade()
        # Bar 0: range 1020..1030 -> long trigger = 1020 + 40 = 1060
        # Bar 1: breaks up to 1062 -> BUY OPEN 5 @ 1060 matches strategy rule
        bars = _bars([
            (1025, 1030, 1020, 1028),
            (1028, 1062, 1027, 1061),
            (1061, 1065, 1059, 1063),
        ])
        trades = [{
            'trade_id': 't1', 'order_id': 'o1', 'direction': 'BUY', 'offset': 'OPEN',
            'quantity': 8, 'price': '1060', 'timestamp_utc': f'{DAY}T01:11:10Z',
        }]
        # Actual account has 8 lots (e.g. 5 strategy + 3 manual) -> Plan B proves 5 lots (order_volume=5)
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=8, last='1063'), today_trades=trades, minute_bars=bars,
        )
        self.assertEqual(res['target_long'], 5)
        self.assertEqual(res['target_short'], 0)
        self.assertEqual(res['reason'], 'PROVED_PARTIAL_RETAIN')
        self.assertIsNotNone(res['proved_position'])
        self.assertEqual(res['proved_position']['theory'], '1060')
        self.assertEqual(res['proved_position']['quantity'], 5)

    def test_unproved_no_today_fill_returns_zero(self):
        spec, trade = _spec_and_trade()
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=30, last='1063'),
            today_trades=[], minute_bars=_bars([(1020, 1063, 1020, 1062)]),
        )
        self.assertEqual(res['target_long'], 0)
        self.assertEqual(res['reason'], 'UNPROVED_NO_TODAY_FILL')
        self.assertIsNone(res['proved_position'])

    def test_unproved_signal_mismatch_returns_zero(self):
        spec, trade = _spec_and_trade()
        # High never reached 1020 + 40 = 1060 -> manual buy at 1035 is NOT strategy-proved
        bars = _bars([(1025, 1040, 1020, 1035)])
        trades = [{
            'trade_id': 't1', 'order_id': 'o1', 'direction': 'BUY', 'offset': 'OPEN',
            'quantity': 5, 'price': '1035', 'timestamp_utc': f'{DAY}T01:10:30Z',
        }]
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=5, last='1036'), today_trades=trades, minute_bars=bars,
        )
        self.assertEqual(res['target_long'], 0)
        self.assertEqual(res['reason'], 'UNPROVED_SIGNAL_MISMATCH')

    def test_proved_entry_that_already_hit_stop_loss_returns_zero(self):
        spec, trade = _spec_and_trade()
        bars = _bars([
            (1025, 1030, 1020, 1028),
            (1028, 1062, 1027, 1061),
            # Subsequent bar drops to 1051 <= 1060 - 8 (1052) -> STOP_LOSS triggered!
            (1061, 1062, 1051, 1054),
        ])
        trades = [{
            'trade_id': 't1', 'order_id': 'o1', 'direction': 'BUY', 'offset': 'OPEN',
            'quantity': 5, 'price': '1060', 'timestamp_utc': f'{DAY}T01:11:10Z',
        }]
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=5, last='1054'), today_trades=trades, minute_bars=bars,
        )
        self.assertEqual(res['target_long'], 0)
        self.assertEqual(res['reason'], 'PROVED_SHOULD_HAVE_EXITED')

    def test_proved_half_close_stage_narrows_target(self):
        spec, trade = _spec_and_trade()
        # profit=20, half_close_ratio=0.5 -> half close triggers at 1060 + 10 = 1070
        bars = _bars([
            (1025, 1030, 1020, 1028),
            (1028, 1062, 1027, 1061),
            (1061, 1071, 1060, 1070),
        ])
        trades = [{
            'trade_id': 't1', 'order_id': 'o1', 'direction': 'BUY', 'offset': 'OPEN',
            'quantity': 5, 'price': '1060', 'timestamp_utc': f'{DAY}T01:11:10Z',
        }]
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=5, last='1070'), today_trades=trades, minute_bars=bars,
        )
        # half_close_target(5) closes ROUND_HALF_EVEN(2.5)=2 lots -> keeps 3
        self.assertEqual(res['target_long'], 3)
        self.assertEqual(res['proved_position']['half_stage'], 1)
        self.assertEqual(res['reason'], 'PROVED_PARTIAL_RETAIN')

    def test_overnight_historical_position_on_force_close_contract_returns_zero(self):
        spec, trade = _spec_and_trade()
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=0, long_his=5, last='1062'),
            today_trades=[], minute_bars=[], has_prior_day_force_close=True,
        )
        self.assertEqual(res['target_long'], 0)
        self.assertEqual(res['reason'], 'UNPROVED_OVERNIGHT_POSITION')

    def test_flat_account_reconstructs_day_stop_from_today_trades(self):
        spec, trade = _spec_and_trade()
        trades = [
            {'trade_id': 't1', 'order_id': 'o1', 'direction': 'BUY', 'offset': 'OPEN',
             'quantity': 5, 'price': '1060', 'timestamp_utc': f'{DAY}T01:10:10Z'},
            {'trade_id': 't2', 'order_id': 'o2', 'direction': 'SELL', 'offset': 'CLOSE',
             'quantity': 5, 'price': '1080', 'timestamp_utc': f'{DAY}T01:20:10Z'},
        ]
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=0, last='1080'), today_trades=trades, minute_bars=[],
        )
        self.assertTrue(res['day_stop'])
        self.assertEqual(res['target_count'], 1)
        self.assertEqual(res['reason'], 'FLAT_DAY_STOP_RECONSTRUCTED')

    def test_filters_prior_day_bars_and_preserves_cross_midnight_night_session(self):
        spec, trade = _spec_and_trade()
        # Mix prior trading day's day-session bar (extreme high/low 900..1500 that would break geometry_limit)
        # with valid cross-midnight night session bars (2026-11-12 21:05 BJT = 13:05Z and 2026-11-13 00:10 BJT = 2026-11-12 16:10Z).
        bars = [
            {'bar_id': 'prior-day', 'time_utc': '2026-11-12T06:55:00Z', 'open': 900, 'high': 1500, 'low': 900, 'close': 1025},
            {'bar_id': 'night-1', 'time_utc': '2026-11-12T13:05:00Z', 'open': 1025, 'high': 1030, 'low': 1020, 'close': 1028},
            {'bar_id': 'night-2', 'time_utc': '2026-11-12T16:10:00Z', 'open': 1028, 'high': 1062, 'low': 1027, 'close': 1061},
        ]
        trades = [{
            'trade_id': 't-night', 'order_id': 'o-night', 'direction': 'BUY', 'offset': 'OPEN',
            'quantity': 5, 'price': '1060', 'timestamp_utc': '2026-11-12T16:10:15Z',
        }]
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=5, last='1062'), today_trades=trades, minute_bars=bars,
        )
        self.assertEqual(res['target_long'], 5)
        self.assertEqual(res['reason'], 'PROVED_OPEN_MATCH')

    def test_repriced_child_orders_count_as_one_target_and_daily_limit_does_not_flatten_proved_position(self):
        spec, trade = _spec_and_trade()
        trade = trade | {'max_daily_orders': 1}
        bars = _bars([
            (1025, 1030, 1020, 1028),
            (1028, 1062, 1027, 1061),
            (1061, 1065, 1059, 1063),
        ])
        # Single 5-lot strategy open target filled across 2 repriced native order_ids (2 + 3 = 5).
        trades = [
            {'trade_id': 't1', 'order_id': 'child-1', 'direction': 'BUY', 'offset': 'OPEN',
             'quantity': 2, 'price': '1060', 'timestamp_utc': f'{DAY}T01:11:05Z'},
            {'trade_id': 't2', 'order_id': 'child-2', 'direction': 'BUY', 'offset': 'OPEN',
             'quantity': 3, 'price': '1060', 'timestamp_utc': f'{DAY}T01:11:20Z'},
        ]
        res = infer_startup_symbol_state(
            contract_id=CONTRACT, spec=spec, trade_settings=trade, trading_day=DAY,
            market=_market(long_today=5, last='1063'), today_trades=trades, minute_bars=bars,
        )
        self.assertEqual(res['target_count'], 1)
        self.assertTrue(res['day_stop'])
        self.assertEqual(res['day_stop_reason'], 'DAILY_TARGET_LIMIT')
        # Reaching max_daily_orders=1 blocks NEW entries, but MUST NOT flatten the proved 5-lot position.
        self.assertEqual(res['target_long'], 5)
        self.assertEqual(res['reason'], 'PROVED_OPEN_MATCH')


if __name__ == '__main__':
    unittest.main()
