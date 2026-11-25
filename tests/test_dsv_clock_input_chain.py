"""Deterministic raw SDK input checks for the clock plan, sections 4 and 14."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.calendar import TradingCalendar


CONTRACT = 'DCE.p2701'
DAY = '2026-11-13'
BEIJING = timezone(timedelta(hours=8))
UTC = timezone.utc


class FakeClock:
    def __init__(self, utc, monotonic_ms=0):
        self.utc = datetime.fromisoformat(utc)
        self.monotonic_ms = monotonic_ms

    def __call__(self):
        return self.utc, self.monotonic_ms

    def advance(self, milliseconds, *, wall_ms=None):
        self.utc += timedelta(milliseconds=milliseconds if wall_ms is None else wall_ms)
        self.monotonic_ms += milliseconds


class RawSdk:
    """SDK Quote, Position, Account and Order shapes, with local call recording.

    wait_update=False leaves already-read entities available, like TqSdk's
    timeout. There is no network, broker fill simulation or order confirmation
    unless a test explicitly supplies native facts.
    """

    def __init__(self):
        self.quote = SimpleNamespace(datetime='', expired=False, last_price=1060, highest=1088,
                                     lowest=1020, bid_price1=1060, ask_price1=1060, price_tick=1,
                                     lower_limit=900, upper_limit=1200, volume_multiple=10, margin=1000)
        self.klines = SimpleNamespace(iloc=[{'datetime': 0}])
        self.position = SimpleNamespace(pos_long=0, pos_long_today=0, pos_long_his=0, pos_short=0,
                                        pos_short_today=0, pos_short_his=0, margin=0)
        self.funds = SimpleNamespace(available=100000, balance=100000, margin=0, frozen_margin=0, risk_ratio=0)
        self.orders, self.trades = {}, {}
        self.calls = []

    def update_quote(self, time, *, last=1060):
        quote_time = datetime.fromisoformat(time)
        self.quote.datetime = quote_time.astimezone(BEIJING).strftime('%Y-%m-%d %H:%M:%S.%f')
        self.quote.last_price = last
        self.quote.ask_price1 = self.quote.bid_price1 = last
        self.klines.iloc[0]['datetime'] = int(quote_time.timestamp()) // 60 * 60 * 10**9

    def get_quote(self, symbol):
        return self.quote

    def get_kline_serial(self, symbol, duration, *, data_length):
        return self.klines

    def get_position(self, symbol, *, account):
        return self.position

    def get_account(self, *, account):
        return self.funds

    def get_order(self, *, account):
        return self.orders

    def get_trade(self, *, account):
        return self.trades

    def wait_update(self, *, deadline):
        return False

    def insert_order(self, *, symbol, direction, offset, volume, limit_price, order_id, account):
        exchange, instrument = symbol.split('.')
        self.orders[order_id] = SimpleNamespace(order_id=order_id, exchange_id=exchange,
            instrument_id=instrument, direction=direction, offset=offset, volume_orign=volume,
            volume_left=volume, limit_price=limit_price, status='ALIVE', insert_date_time=0,
            is_error=False, _this_session=True)
        self.calls.append(('SEND', order_id, volume))
        return self.orders[order_id]

    def cancel_order(self, order_id, *, account):
        self.calls.append(('CANCEL', order_id))


def product_inputs(settings):
    base = load_confirmed_baseline()[0].to_settings()
    base['symbols'] = [base['symbols'][0] | {
        'open_offset': 40, 'stop_offset': 8, 'profit_offset': 20, 'order_volume': 5,
    }]
    base['trade'].update({'continuation_entry': False, 'no_chase_after_act': False,
                          'passive_entry_first': False, 'max_quote_age_seconds': 0,
                          'stale_force_close_seconds': 0, 'close_before_seconds': 30} | settings)
    calendar = TradingCalendar.from_record({
        'schema_version': 'TRADING_CALENDAR/1', 'calendar_id': 'timer-replay',
        'calendar_version': '1', 'complete': True, 'coverage_from_utc': DAY + 'T00:00:00Z',
        'coverage_to_utc': DAY + 'T10:00:00Z', 'contracts': [{'contract_id': CONTRACT, 'sessions': [
            {'session_id': 'morning', 'trading_day': DAY, 'open_utc': DAY + 'T01:00:00Z',
             'close_utc': DAY + 'T02:15:00Z', 'is_opening_session': True, 'force_close_at_end': False},
            {'session_id': 'afternoon', 'trading_day': DAY, 'open_utc': DAY + 'T05:30:00Z',
             'close_utc': DAY + 'T07:00:00Z', 'is_opening_session': False, 'force_close_at_end': True},
        ]}],
    })
    return DsvProfile.from_settings(base), calendar


@unittest.skipUnless(sys.platform == 'win32', 'Uses the native Windows account owner lock')
class DsvClockInputChainTests(unittest.TestCase):
    def start(self, time='01:40:00', **settings):
        self.clock = FakeClock(DAY + 'T' + time + 'Z')
        self.api = RawSdk()
        self.api.update_quote(self.clock.utc.isoformat())
        profile, calendar = product_inputs(settings)
        account = f'DEV:clock-{uuid4().hex}'
        self.adapter = DsvSdkAdapter(self.api, object(), account_key=account, profile=profile,
                                     calendar=calendar, sdk_version=SDK_VERSION, clock=self.clock)
        directory = self.enterContext(TemporaryDirectory())
        self.session = self.enterContext(DsvSession(Path(directory) / 'facts.db', account_key=account,
                                         provider='OFFLINE', trading_day=DAY, profile=profile, port=self.adapter))
        self.session.command({'id': 'start', 'action': 'START'})

    def step(self, elapsed_ms=0):
        self.clock.advance(elapsed_ms)
        return self.adapter.step(self.session, trading_day=DAY, complete=True)

    def confirm_open(self, price=1060, quantity=5):
        order, = self.api.orders.values()
        exchange_time = int(self.clock.utc.timestamp()) * 10**9
        order.insert_date_time = exchange_time
        order.exchange_order_id = 'cabinet-open'
        order.volume_left -= quantity
        if not order.volume_left:
            order.status = 'FINISHED'
        self.api.trades['entry-fill'] = SimpleNamespace(trade_id='entry-fill', order_id=order.order_id,
            exchange_id=order.exchange_id, instrument_id=order.instrument_id, direction=order.direction,
            offset='OPEN', volume=quantity, price=price, trade_date_time=exchange_time)
        self.api.position.pos_long = self.api.position.pos_long_today = quantity

    def test_entry_timeout_advances_on_duplicate_quote_and_keeps_strict_boundary(self):
        self.start(entry_confirm_timeout=60)
        opening, = self.step()['effects']
        for _ in range(60):
            self.assertEqual(self.step(1000)['effects'], [])
        late = self.step(1)
        self.assertIn('ENTRY_CONFIRM_TIMEOUT', [row['reason'] for row in late['decisions']])
        self.assertEqual([(effect['kind'], effect['child_order_id']) for effect in late['effects']],
                         [('CANCEL', opening['child_order_id'])])

    def test_required_cancel_does_not_need_a_quote(self):
        self.start(entry_confirm_timeout=60)
        first, = self.step()['effects']
        self.api.quote.datetime = ''
        for _ in range(60):
            self.assertEqual(self.step(1000)['effects'], [])
        last = self.step(1)
        self.assertEqual([(effect['kind'], effect['child_order_id']) for effect in last['effects']],
                         [('CANCEL', first['child_order_id'])])
        self.assertEqual([call[0] for call in self.api.calls], ['SEND', 'CANCEL'])

    def test_first_old_snapshot_does_not_pass_a_two_second_age_limit(self):
        self.start(max_quote_age_seconds=2)
        self.api.update_quote(DAY + 'T01:38:00Z')
        result = self.step()
        self.assertEqual(result['effects'], [])
        self.assertEqual([row['reason'] for row in result['decisions']], ['QUOTE_AGE'])

    def test_leading_quote_keeps_original_timestamp_and_uses_independent_decision_time(self):
        self.start(max_quote_age_seconds=2)
        self.api.update_quote(DAY + 'T01:40:00.970000Z')
        initial = self.step()
        row = self.session.snapshot()['symbols'][CONTRACT]
        self.assertEqual(initial['evaluated_at'], DAY + 'T01:40:00Z')
        self.assertEqual(row['quote_observation']['source_age_raw_ns'], '-970000000')
        self.assertEqual(row['quote_observation']['reason'], 'QUOTE_LEADS_LOCAL_UTC')
        self.assertEqual(self.api.quote.datetime, '2026-11-13 09:40:00.970000')
        self.assertEqual([effect['kind'] for effect in initial['effects']], ['SEND'])

    def test_passive_wait_is_inclusive_even_if_market_time_does_not_advance(self):
        self.start(passive_entry_first=True)
        self.step()
        self.step(2999)
        self.assertEqual(self.session.snapshot()['symbols'][CONTRACT]['position']['mode'], 'PASSIVE')
        at_boundary = self.step(1)
        self.assertEqual(self.session.snapshot()['symbols'][CONTRACT]['position']['mode'], 'CAP')
        due = [event for event in at_boundary['time_events'] if event['result'] == 'DUE']
        self.assertEqual([(event['kind'], event['lateness_ns']) for event in due], [('passive_wait', '0')])

    def test_arriving_partial_fill_precedes_the_old_zero_fill_timeout(self):
        self.start(entry_confirm_timeout=60)
        self.step()
        for _ in range(60):
            self.step(1000)
        self.confirm_open(quantity=2)
        result = self.step(1)
        position = self.session.snapshot()['symbols'][CONTRACT]['position']
        self.assertEqual((position['owned'], position['quantity']), (2, 5))
        self.assertNotIn('ENTRY_CONFIRM_TIMEOUT', [row['reason'] for row in result['decisions']])
        self.assertEqual([call[0] for call in self.api.calls], ['SEND'])

    def test_wall_jump_disarms_without_advancing_monotonic_entry_timeout(self):
        self.start(entry_confirm_timeout=3)
        self.step()
        self.clock.advance(1000, wall_ms=31000)
        self.assertEqual(self.step()['effects'], [])
        self.assertEqual(self.session.snapshot()['clock_recovery_required'], 'WALL_CLOCK_DISCONTINUITY')
        self.assertEqual(self.step(2000)['effects'], [])
        self.assertFalse(self.session.snapshot()['symbols'][CONTRACT]['position']['entry_stopped'])
        late = self.step(1)
        self.assertIn('ENTRY_CONFIRM_TIMEOUT', [d['reason'] for d in late['decisions']])
        self.assertTrue(self.session.snapshot()['symbols'][CONTRACT]['position']['entry_stopped'])
        self.assertEqual(late['effects'], [])
        self.assertFalse(self.session.snapshot()['running'])

    def test_close_without_quote_is_durable_without_a_nominal_price_and_stays_closed(self):
        self.start(time='06:59:20', no_open_before_close_minutes=0)
        self.step()
        self.confirm_open()
        self.step(1000)
        self.api.quote.datetime = ''
        for _ in range(9):
            self.step(1000)
        state = self.session.snapshot()['symbols'][CONTRACT]
        key = state['position']['calendar_exit']['id']
        self.assertEqual(state['position']['exit_target'], 0)
        self.assertIsNone(state['position']['rollback_exit']['price'])
        self.assertEqual(state['exit_wait_reason'], 'EXIT_WAITING_FOR_PRICE')
        for _ in range(31):
            self.step(1000)
        state = self.session.snapshot()['symbols'][CONTRACT]
        self.assertFalse(state['market_session_active'])
        self.assertEqual(state['position']['calendar_exit']['id'], key)
        self.assertEqual(state['exit_wait_reason'], 'EXIT_WAITING_FOR_SESSION')
        self.assertEqual([call[0] for call in self.api.calls], ['SEND'])
