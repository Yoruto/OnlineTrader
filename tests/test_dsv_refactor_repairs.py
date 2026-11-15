"""F01-F03 regressions; no SDK connection and no account lock is created here.

Small fixtures exercise the real methods and SQLite journal. The final Windows
case additionally uses the public DsvSession constructor and existing fixture.
"""

import copy
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from online_trader.contracts.codec import strict_loads
from online_trader.domain.orders import OrderState
from online_trader.dsv_runtime.controller import ExplicitOrderController, SendNotAttempted, terminal, tighten_target
from online_trader.dsv_runtime.runner import DsvSession, initial_state
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.dsv_storage.journal import Journal
from online_trader.errors import TraderError


CODE = 'DCE.p2701'
DAY = '2026-11-13'
UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))


def tick(price='100'):
    return {'last': price, 'bid1': price, 'ask1': price, 'day_high': '150', 'day_low': '50',
            'tick_size': '1', 'lower_limit': '1', 'upper_limit': '200',
            'native_time': DAY + 'T01:10:00Z', 'candle_id': 'bar-1'}


def position(owned=0):
    return {'id': 'position-1', 'contract_id': CODE, 'direction': 'LONG', 'quantity': 2,
            'owned': owned, 'entry_filled': owned, 'entry_notional': str(owned * 100),
            'children': [], 'entry_stopped': False, 'exit_target': None, 'exit_revision': 0,
            'mode': 'ACTIVE', 'cap': '100', 'context_at': '0', 'theory': '100',
            'open_day': DAY, 'fills': []}


class DispatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'facts.db'
        self.scope = {'account_key': 'DEV:repair', 'provider': 'OFFLINE', 'trading_day': DAY,
                      'configuration_id': 'profile-1'}
        self.state = initial_state(DAY, [CODE])
        row = self.state['symbols'][CODE]
        row.update(position=position(), target_count=1, last_signal_bar='bar-1')
        self.state['sdk_scope_targets'] = 1
        self.journal = Journal(self.path, self.scope, self.state)
        self.addCleanup(lambda: self.journal.close())
        self.adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        self.adapter.owner = get_ident()
        self.adapter.enabled = True
        self.adapter.account_key = self.scope['account_key']
        self.adapter.provider = 'OFFLINE'
        self.adapter.account = object()
        self.adapter.session = SimpleNamespace(_closed=False, _journal=self.journal)
        self.adapter._seen_effects = set()
        self.adapter._local_returns = set()
        self.adapter.api = SimpleNamespace(insert_order=Mock(), cancel_order=Mock())
        self.adapter.dispatch_market = {CODE: {'ticks': [tick()], 'external_orders': False}}
        self.adapter.clock = lambda: (datetime(2026, 11, 13, 1, 10, tzinfo=UTC), 0)
        self.adapter.grant = SimpleNamespace(value={'contracts': [CODE]},
                                            permits_day=lambda day: day == DAY, in_window=lambda now: False)
        self.controller = ExplicitOrderController(self.scope, self.journal, self.adapter)
        with self.journal.transaction():
            self.effect = self.controller.new_order(self.state, row['position'], {'contract_id': CODE},
                                                    'OPEN', 2, '100', 'OPEN')
            self.journal.save(self.state)
        self.child = self.effect['child_order_id']

    def order(self):
        return self.state['orders'][self.child]

    def dispatch(self):
        self.controller.dispatch(self.state, [self.effect])

    def result_event(self):
        row = self.journal._db.execute("SELECT payload FROM events WHERE kind='EFFECT_RESULT' ORDER BY seq DESC").fetchone()
        return strict_loads(row[0])

    def reload(self):
        self.journal.close()
        self.journal = Journal(self.path, self.scope, initial_state(DAY, [CODE]))
        self.state = self.journal.load()
        self.controller = ExplicitOrderController(self.scope, self.journal, self.adapter)
        self.adapter.session._journal = self.journal

    def synchronize(self):
        runtime = DsvSession.__new__(DsvSession)
        runtime._controller = self.controller
        self.state['synchronized'] = False
        pending = {key for key, order in self.state['orders'].items() if not terminal(order)}
        runtime._synchronize(self.state, {'native': [], 'sync_orders': [],
                              'market': [{'contract_id': CODE, 'long_qty': 0, 'short_qty': 0}]}, pending)

    def test_local_window_refusal_never_calls_sdk_and_is_not_broker_rejection(self):
        self.dispatch()
        self.adapter.api.insert_order.assert_not_called()
        self.assertEqual(self.order()['send_state'], 'DEFINITELY_NOT_SENT')
        self.assertTrue(terminal(self.order()))
        self.assertIsNone(self.order()['native_id'])
        self.assertFalse(self.order()['terminal_confirmed'])
        self.assertEqual(self.order()['broker_state'], 'NOT_SEEN')
        self.assertEqual(self.result_event()['stage'], 'PRE_SEND_VALIDATION')
        self.assertEqual(self.result_event()['error_code'], 'E_DSV_SDK_SCOPE')
        self.assertTrue(self.result_event()['exception']['stack'])

    def test_illegal_price_is_also_local_and_keeps_original_request(self):
        self.adapter.grant.in_window = lambda now: True
        self.adapter.dispatch_market[CODE]['ticks'][0]['upper_limit'] = '99'
        self.dispatch()
        self.adapter.api.insert_order.assert_not_called()
        self.assertEqual(self.order()['send_state'], 'DEFINITELY_NOT_SENT')
        stored = self.journal._db.execute('SELECT request FROM effects WHERE id=?', (self.effect['id'],)).fetchone()
        self.assertEqual(strict_loads(stored[0]), self.effect)

    def test_error_code_from_inside_sdk_does_not_prove_unsent(self):
        self.adapter.grant.in_window = lambda now: True
        self.adapter.api.insert_order.side_effect = TraderError('E_DSV_SDK_SCOPE', 'synthetic broker text')
        self.dispatch()
        self.adapter.api.insert_order.assert_called_once()
        self.assertEqual(self.order()['send_state'], 'UNKNOWN')
        self.assertFalse(terminal(self.order()))
        self.assertNotIn('local_send_rejection', self.order())
        self.assertEqual(self.result_event()['stage'], 'PORT_CALL')
        self.assertEqual(self.result_event()['error_code'], 'E_DSV_SDK_SCOPE')

    def test_sdk_oserror_stays_unknown_and_message_is_not_exported(self):
        self.adapter.grant.in_window = lambda now: True
        self.adapter.api.insert_order.side_effect = OSError('fake-password=must-not-escape')
        self.dispatch()
        event = self.result_event()
        self.assertEqual(event['result'], 'UNKNOWN')
        self.assertEqual(event['exception']['stack'][0]['type'], 'OSError')
        self.assertNotIn('must-not-escape', repr(event))
        self.assertFalse(terminal(self.order()))

    def test_success_is_call_returned_not_a_fill_or_terminal(self):
        self.adapter.grant.in_window = lambda now: True
        self.dispatch()
        self.adapter.api.insert_order.assert_called_once()
        self.assertEqual(self.order()['send_state'], 'CALL_RETURNED')
        self.assertEqual(self.order()['filled_qty'], 0)
        self.assertFalse(terminal(self.order()))

    def test_current_sdk_local_order_allows_cancel_without_claiming_native_acceptance(self):
        self.adapter.grant.in_window = lambda now: True
        self.dispatch()
        identity = self.adapter.order_id(self.child)
        local = SimpleNamespace(order_id=identity, exchange_id='DCE', instrument_id='p2701',
                                direction='BUY', offset='OPEN', volume_orign=2, volume_left=2,
                                limit_price=100, status='ALIVE', insert_date_time=0,
                                exchange_order_id='', _this_session=True)
        facts = self.adapter._order_facts({identity: local}, {identity: self.order()})
        self.assertEqual([fact['kind'] for fact in facts], ['LOCAL_ORDER'])
        with self.journal.transaction():
            self.controller.accept_native(self.state, facts)
            request = self.controller.cancel(self.order(), 'REPRICE')
            self.journal.save(self.state)
        self.controller.dispatch(self.state, [request])
        self.adapter.api.cancel_order.assert_called_once_with(identity, account=self.adapter.account)
        self.assertEqual(self.order()['send_state'], 'CALL_RETURNED')
        self.assertIsNone(self.order()['native_id'])
        self.assertFalse(terminal(self.order()))
        self.assertEqual(self.journal._db.execute('SELECT COUNT(*) FROM native_orders').fetchone()[0], 0)
        DsvSession._mark_unsynchronized(self.state)
        self.adapter._local_returns.clear()
        self.assertNotIn('local_order_id', self.order())
        with self.assertRaises(TraderError):
            self.adapter._order_facts({identity: local}, {identity: self.order()})

    def test_duplicate_effect_does_not_get_unsent_proof(self):
        self.adapter._seen_effects.add(self.effect['id'])
        self.dispatch()
        self.adapter.api.insert_order.assert_not_called()
        self.assertEqual(self.order()['send_state'], 'UNKNOWN')
        self.assertNotIn('local_send_rejection', self.order())

    def test_reopen_local_refusal_syncs_without_invented_native_order(self):
        self.dispatch()
        self.reload()
        self.synchronize()
        self.assertTrue(self.state['synchronized'])
        row = self.state['symbols'][CODE]
        self.assertEqual((row['target_count'], row['last_signal_bar'], self.state['sdk_scope_targets']), (1, 'bar-1', 1))
        self.controller.settle(self.state, row)
        self.assertIsNone(row['position'])
        self.assertEqual(row['target_count'], 1)
        self.adapter.api.insert_order.assert_not_called()

    def test_reopen_unknown_still_waits_for_native_evidence(self):
        self.adapter.grant.in_window = lambda now: True
        self.adapter.api.insert_order.side_effect = OSError('uncertain native send')
        self.dispatch()
        self.reload()
        self.synchronize()
        self.assertFalse(self.state['synchronized'])
        self.assertFalse(terminal(self.order()))
        self.assertEqual(self.order()['original_qty'] - self.order()['filled_qty'], 2)

    def test_storage_failure_does_not_persist_an_unproved_release(self):
        with patch.object(self.journal, 'resolve', side_effect=sqlite3.OperationalError('synthetic disk failure')):
            with self.assertRaisesRegex(TraderError, 'prepared effect requires reconciliation'):
                self.dispatch()
        saved = self.journal.load()['orders'][self.child]
        self.assertEqual(saved['send_state'], 'SEND_POSSIBLE')
        self.assertFalse(terminal(saved))
        self.assertTrue(self.journal.effect_is_prepared(self.effect))
        self.adapter.api.insert_order.assert_not_called()

    def test_conflicting_native_evidence_for_unsent_child_is_not_ignored(self):
        self.dispatch()
        with self.assertRaisesRegex(TraderError, 'contradicts'):
            with self.journal.transaction():
                self.controller.accept_native(self.state, [{'kind': 'ORDER', 'child_order_id': self.child,
                    'native_order_id': 'unexpected-native', 'trading_day': DAY, 'status': 'ALIVE', 'filled_qty': 0}])
        self.assertEqual(self.order()['send_state'], 'DEFINITELY_NOT_SENT')
        self.assertIsNone(self.order()['native_id'])

    def test_result_for_another_effect_cannot_release_this_child(self):
        with patch.object(self.adapter, 'send', return_value=SendNotAttempted('other-send', 'E_TEST', {})):
            self.dispatch()
        self.assertEqual(self.order()['send_state'], 'UNKNOWN')
        self.assertEqual(self.result_event()['error_code'], 'E_DSV_DISPATCH_RESULT')
        self.assertFalse(terminal(self.order()))

    def test_bare_definitely_not_sent_label_without_proof_is_not_terminal(self):
        self.order()['send_state'] = 'DEFINITELY_NOT_SENT'
        self.assertFalse(terminal(self.order()))

    def test_cancel_call_exception_does_not_release_original_order(self):
        order = self.order()
        native = self.adapter.order_id(self.child)
        order.update(send_state='NATIVE_SEEN', native_id=native, reported_fill_qty=0)
        with self.journal.transaction():
            effect = self.controller.cancel(order, 'USER_CANCEL')
            self.journal.save(self.state)
        self.adapter.api.cancel_order.side_effect = TraderError('E_DSV_SDK_SCOPE')
        self.controller.dispatch(self.state, [effect])
        self.assertEqual(order['send_state'], 'NATIVE_SEEN')
        self.assertEqual(order['cancel_status'], 'UNKNOWN')
        self.assertFalse(terminal(order))
        self.assertEqual(self.result_event()['kind'], 'CANCEL')
        self.adapter.api.cancel_order.assert_called_once_with(native, account=self.adapter.account)

    def test_local_reduction_refusal_resumes_on_fresh_quote_under_original_target(self):
        row = self.state['symbols'][CODE]
        pos = row['position']
        self.order().update(send_state='NATIVE_SEEN', native_id='open-native', broker_state='FILLED',
                            terminal_confirmed=True, reported_fill_qty=2, filled_qty=2)
        pos.update(owned=2, entry_filled=2, entry_stopped=True, exit_target=0, exit_revision=1)
        with self.journal.transaction():
            effect = self.controller.new_order(self.state, pos, {'contract_id': CODE, 'tick': tick()},
                                               'REDUCE', 2, '100', 'CLOSE')
            self.journal.save(self.state)
        self.adapter.dispatch_market[CODE]['ticks'] = []
        self.controller.dispatch(self.state, [effect])
        self.assertEqual((pos['owned'], pos['exit_target']), (2, 0))
        rejected = self.state['orders'][effect['child_order_id']]
        self.assertTrue(terminal(rejected))
        market = {'contract_id': CODE, 'tick': tick(), 'long_qty': 2, 'short_qty': 0}
        with patch.object(self.controller, 'proves_position', return_value=True), patch.object(
                self.controller, '_reduction_routes', return_value=[{'quantity': 2, 'native_offset': 'CLOSE'}]):
            with self.journal.transaction():
                self.assertEqual(self.controller.plan(self.state, row, market, 0), [])
                market['tick'].update(native_time=DAY + 'T01:10:01Z', bid1='NaN', last='NaN')
                self.assertEqual(self.controller.plan(self.state, row, market, 1), [])
                market['tick'] = tick() | {'native_time': DAY + 'T01:10:02Z'}
                requests = self.controller.plan(self.state, row, market, 2)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['quantity'], 2)
            self.assertNotEqual(requests[0]['child_order_id'], effect['child_order_id'])
            self.assertEqual(pos['exit_revision'], 1)
            self.assertEqual(self.state['orders'][requests[0]['child_order_id']]['parent_child_order_id'],
                             effect['child_order_id'])
        self.adapter.api.insert_order.assert_not_called()


class ExpiryParityTests(unittest.TestCase):
    def market(self, wall, expiry='2026-11-16'):
        adapter = DsvSdkAdapter.__new__(DsvSdkAdapter)
        period = {'open_utc': '2026-11-10T01:00:00Z', 'close_utc': '2026-11-10T07:00:00Z',
                  'is_opening_session': True, 'force_close_at_end': True, 'trading_day': '2026-11-10'}
        adapter.calendar = SimpleNamespace(query=lambda *args: {'is_trading': False, 'session': None, 'complete': True})
        adapter.periods = {CODE: [period]}
        adapter.grant = None
        adapter.account = object()
        native_position = SimpleNamespace(pos_long=0, pos_short=0, pos_long_today=0, pos_long_his=0,
                                          pos_short_today=0, pos_short_his=0, margin=0)
        adapter.api = SimpleNamespace(get_position=lambda *args, **kw: native_position)
        adapter.quotes = {CODE: SimpleNamespace(expire_datetime=expiry, margin=10, volume_multiple=1)}
        funds = SimpleNamespace(available=1000, balance=1000, margin=0, frozen_margin=0, risk_ratio=0)
        return adapter._market(CODE, '2026-11-10', datetime.fromisoformat(wall), funds, {}, False, {})

    def test_expiry_midnight_full_wall_time_boundaries(self):
        cases = (
            ('2026-11-09T23:59:59.999999+08:00', 6),
            ('2026-11-10T00:00:00+08:00', 6),
            ('2026-11-10T00:00:00.000001+08:00', 5),
            ('2026-11-10T09:06:00+08:00', 5),
            ('2026-11-10T01:06:00+00:00', 5),
            ('2026-11-10T10:06:00+09:00', 5),
            ('2026-11-15T23:59:59.999999+08:00', 0),
            ('2026-11-16T00:00:00+08:00', 0),
            ('2026-11-16T00:00:00.000001+08:00', -1),
        )
        for wall, expected in cases:
            with self.subTest(wall=wall):
                self.assertEqual(self.market(wall)['days_to_expiry'], str(expected))

    def test_threshold_five_blocks_after_midnight_but_not_at_six(self):
        self.assertGreater(int(self.market('2026-11-10T00:00:00+08:00')['days_to_expiry']), 5)
        self.assertLessEqual(int(self.market('2026-11-10T09:06:00+08:00')['days_to_expiry']), 5)

    def test_unknown_expiry_remains_unknown(self):
        self.assertNotIn('days_to_expiry', self.market('2026-11-10T09:06:00+08:00', None))


class ProtectedFloorParityTests(unittest.TestCase):
    def fixture(self, direction='LONG', last='120', keep='0.5', protected=True):
        runtime = DsvSession.__new__(DsvSession)
        runtime._trade = dict(entry_slip_abort=False, stale_force_close_seconds='0', entry_confirm_timeout=60,
            protect_80_enabled=True, protect_80_ratio='0.8', protect_keep_ratio=keep,
            half_close_enabled=False, half_stage2=False, half_stage2_trigger_ratio='0.6',
            trailing_activate_ratio='0', trailing_pullback_ratio='0.2', protect_stop_day=False,
            stop_day_on_trailing=False, cont_stop_day_stop=False, stop_cooldown_minutes='0', stop_cooldown_enabled=False)
        pos = position(5) | {'quantity': 5, 'direction': direction, 'theory': '100', 'profit': '50', 'stop': '10',
            'peak': '40' if protected else '0', 'protected': protected, 'half_stage': 0,
            'entry_slip_checked': True, 'submitted_at': str(datetime(2026, 11, 13, 1, tzinfo=UTC).timestamp())}
        row = {'position': pos, 'net_loss': '30', 'stop_streak': 2, 'day_stop': False}
        frame = {'wall_time': DAY + 'T01:10:00Z', 'monotonic_ms': '1000'}
        market = {'tick': tick(last), 'session_open': DAY + 'T01:00:00Z', 'session_close': DAY + 'T07:00:00Z'}
        return runtime, row, market, frame, {'half_close_ratio': '0.4'}

    def evaluate(self, *args, **kwargs):
        runtime, row, market, frame, spec = self.fixture(*args, **kwargs)
        original_tick = copy.deepcopy(market['tick'])
        original_notional = row['position']['entry_notional']
        result = runtime._maintain_position(row, row['position'], market, frame, spec)
        self.assertEqual(market['tick'], original_tick)
        self.assertEqual(row['position']['entry_notional'], original_notional)
        return runtime, row, market, frame, spec, result

    def test_long_and_short_gap_use_line_not_trigger_price(self):
        for direction, last, reference in (('LONG', '120', '125'), ('SHORT', '80', '75')):
            with self.subTest(direction=direction):
                *_, result = values = self.evaluate(direction, last)
                row = values[1]
                self.assertEqual(row['net_loss'], '5')
                self.assertEqual(row['position']['rollback_exit']['price'], reference)
                self.assertEqual(result[0]['move'], '20.0')
                self.assertEqual(Decimal(result[0]['nominal_move']), Decimal(25))
                self.assertEqual(result[0]['nominal_reference_price'], reference)

    def test_exact_line_and_zero_ratio_fallback(self):
        for direction, last, keep in (('LONG', '125', '0.5'), ('SHORT', '75', '0.5'), ('LONG', '120', '0')):
            with self.subTest(direction=direction, keep=keep):
                self.assertEqual(self.evaluate(direction, last, keep)[1]['net_loss'], '5')

    def test_jump_beyond_entry_still_books_original_protection_line(self):
        for direction, last in (('LONG', '80'), ('SHORT', '120')):
            with self.subTest(direction=direction):
                values = self.evaluate(direction, last)
                self.assertEqual(values[1]['net_loss'], '5')
                self.assertEqual(values[-1][0]['reason'], 'PROTECTED_FLOOR')

    def test_repeat_quote_does_not_book_twice(self):
        runtime, row, market, frame, spec, _ = self.evaluate()
        runtime._maintain_position(row, row['position'], market, frame, spec)
        self.assertEqual(row['net_loss'], '5')
        self.assertEqual(row['position']['rollback_exit']['price'], '125')

    def test_other_exit_statistics_are_unchanged(self):
        for last, expected, reason in (('89', '40', 'STOP_LOSS'), ('160', '-20', 'TAKE_PROFIT')):
            with self.subTest(reason=reason):
                values = self.evaluate(last=last, protected=False)
                self.assertEqual(values[1]['net_loss'], expected)
                self.assertEqual(values[1]['position']['rollback_exit']['price'], last)
                self.assertEqual(values[-1][-1]['reason'], reason)

    def test_partial_stage_does_not_book_full_exit(self):
        runtime, row, market, frame, spec = self.fixture(last='125', protected=False)
        runtime._trade.update(protect_80_enabled=False, half_close_enabled=True)
        result = runtime._maintain_position(row, row['position'], market, frame, spec)
        self.assertEqual(result[0]['reason'], 'PARTIAL_STAGE_1')
        self.assertEqual(row['net_loss'], '30')
        self.assertNotIn('rollback_exit', row['position'])

    def test_journal_restart_keeps_nominal_basis_without_rewriting_native_trade(self):
        _, row, _, _, _, _ = self.evaluate()
        scope = {'account_key': 'DEV:protection', 'provider': 'OFFLINE', 'trading_day': DAY, 'configuration_id': 'test'}
        native = {'price': '119', 'quantity': 5, 'fill_day': DAY}
        with tempfile.TemporaryDirectory() as location:
            path = Path(location) / 'facts.db'
            journal = Journal(path, scope, {'symbols': {CODE: row}})
            try:
                with journal.transaction():
                    journal.record_trade(DAY, 'native-order', 'native-trade', native)
                    journal.save({'symbols': {CODE: row}})
            finally:
                journal.close()
            journal = Journal(path, scope, {})
            try:
                saved = journal.load()['symbols'][CODE]
                self.assertEqual(saved['net_loss'], '5')
                self.assertEqual(saved['position']['rollback_exit']['price'], '125')
                trade = journal._read('SELECT payload FROM native_trades')
                self.assertEqual(trade, native)
            finally:
                journal.close()


@unittest.skipUnless(sys.platform == 'win32', 'Public session integration requires the Windows account lock')
class WindowsSessionRepairTests(unittest.TestCase):
    def test_public_round_refusal_reopen_and_sync_preserves_consumed_opportunity(self):
        from tests.test_dsv_round_replay import DsvRoundReplayTests, round_frame
        case = DsvRoundReplayTests()
        case.setUp()
        try:
            with patch.object(case.port, 'send', side_effect=lambda request:
                              SendNotAttempted(request['id'], 'E_DSV_SDK_SCOPE', {})):
                result = case.session.apply_round(round_frame(1, [1060]))
            child = result['effects'][0]['child_order_id']
            self.assertEqual(case.session.snapshot()['orders'][child]['send_state'], 'DEFINITELY_NOT_SENT')
            case.session.close()
            with DsvSession(Path(case.directory) / 'facts.db', account_key='DEV:round-regression',
                    provider='OFFLINE', trading_day=DAY, profile=case.profile, port=case.port) as restarted:
                restarted.disconnect()
                frame = round_frame(2, [1060]) | {'synchronized': True, 'sync_orders': []}
                self.assertEqual(restarted.apply_round(frame)['effects'], [])
                self.assertTrue(restarted.snapshot()['synchronized'])
                self.assertEqual(restarted.snapshot()['symbols'][CODE]['target_count'], 1)
                self.assertEqual(restarted.snapshot()['symbols'][CODE]['last_signal_bar'], 'bar-1')
                self.assertEqual(restarted.apply_round(round_frame(3, [1060]))['effects'], [])
                self.assertEqual(case.port.orders, [])
        finally:
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()
