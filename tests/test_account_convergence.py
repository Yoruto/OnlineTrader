"""Account-level convergence, Plan B startup inference, and UI tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.transport.status_client import StatusClient
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.instance import AccountLock, RuntimeLock
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.calendar import TradingCalendar
from qa.target_owner.offline_runtime import OfflineTargetRuntime, profile as base_qa_profile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from online_trader.desktop.workbench import WorkbenchWindow


CONTRACT = 'DCE.p2701'
DAY = '2026-11-13'


def _make_serial(bars):
    class _FakeSerial:
        @property
        def iloc(self):
            return bars

        def tail(self, count):
            return SimpleNamespace(to_dict=lambda _mode: bars[-count:])

    return _FakeSerial()


def _order_fact(child_id, native_id, contract, direction, offset, qty, status='FILLED', filled=None, day=DAY):
    return {
        'kind': 'ORDER',
        'child_order_id': child_id,
        'native_order_id': native_id,
        'contract_id': contract,
        'direction': direction,
        'native_offset': offset,
        'trading_day': day,
        'status': status,
        'filled_qty': qty if filled is None else filled,
    }


def _trade_fact(child_id, native_id, trade_id, contract, direction, offset, qty, price, day=DAY):
    return {
        'kind': 'TRADE',
        'child_order_id': child_id,
        'native_order_id': native_id,
        'native_trade_id': trade_id,
        'contract_id': contract,
        'direction': direction,
        'native_offset': offset,
        'trading_day': day,
        'fill_day': day,
        'quantity': qty,
        'price': str(price),
    }


def _offline_frame(
    sequence,
    *,
    long_qty=0,
    short_qty=0,
    last='1050',
    day_low='1020',
    day_high='1088',
    facts=(),
    non_strategy_orders=None,
    unconfigured_markets=None,
    candle_id='bar-1',
):
    wall = f'{DAY}T01:10:{sequence:02d}Z'
    result = {
        'id': f'conv-round-{sequence}',
        'sequence': sequence,
        'session_id': 'qa-session',
        'wall_time': wall,
        'monotonic_ms': sequence * 1000,
        'complete': True,
        'synchronized': True,
        'sync_orders': [],
        'market': [{
            'contract_id': CONTRACT,
            'ticks': [{
                'native_time': wall,
                'candle_id': candle_id,
                'last': str(last),
                'day_low': str(day_low),
                'day_high': str(day_high),
                'bid1': str(last),
                'ask1': str(last),
                'tick_size': '1',
                'lower_limit': '900',
                'upper_limit': '1200',
            }],
            'opening_session': True,
            'market_session_active': True,
            'standalone_external_model': True,
            'external_active_orders': bool(
                any(o['contract_id'] == CONTRACT for o in (non_strategy_orders or ()))
            ),
            'session_open': f'{DAY}T01:00:00Z',
            'session_close': f'{DAY}T02:15:00Z',
            'long_qty': long_qty,
            'short_qty': short_qty,
            'long_today': long_qty,
            'long_historical': 0,
            'short_today': short_qty,
            'short_historical': 0,
            'available': '200000',
            'margin_per_lot': '1000',
        }],
        'native': list(facts),
    }
    if non_strategy_orders is not None:
        result['non_strategy_orders'] = list(non_strategy_orders)
    if unconfigured_markets is not None:
        result['unconfigured_markets'] = list(unconfigured_markets)
    return result


class AccountConvergenceTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = tmp
        self.root = Path(tmp.name)

    def tearDown(self):
        pass

    def _start_runtime(self):
        runtime = OfflineTargetRuntime(self.root / 'rt').start()
        self.addCleanup(runtime.close)
        client = StatusClient(runtime.pipe, timeout=5)
        runtime.submit(_offline_frame(1, long_qty=0, last='1050'))
        deadline = datetime.now() + timedelta(seconds=5)
        while not client.workbench()['connection']['synchronized']:
            if datetime.now() >= deadline:
                self.fail('Runtime failed initial sync')
        return runtime, client

    def _wait_view(self, client, predicate, timeout=5.0):
        deadline = datetime.now() + timedelta(seconds=timeout)
        last = None
        while datetime.now() < deadline:
            last = client.workbench()
            if predicate(last):
                return last
        self.fail(f'View condition not reached: {last}')

    def test_plastic_30_lot_external_holding_previews_disarmed_then_flattens_on_arm(self):
        runtime, client = self._start_runtime()
        # 30-lot external LONG holding while price 1050 gives target 0.
        runtime.submit(_offline_frame(2, long_qty=30, last='1050'))
        disarmed = self._wait_view(
            client,
            lambda v: (v.get('connection') or {}).get('account_positions', [{}])[0].get('actual_long') == 30,
        )
        acc = disarmed['connection']['account_positions'][0]
        self.assertEqual(acc['actual_long'], 30)
        self.assertEqual(acc['target_long'], 0)
        self.assertEqual(acc['excess_long'], 30)
        self.assertEqual(disarmed['external_positions'], [])
        self.assertEqual(disarmed['orders'], [])
        self.assertEqual(runtime.adapter.orders, [])

        # ARM sends aggressive convergence close for 30 lots.
        client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1',
        )
        runtime.submit(_offline_frame(3, long_qty=30, last='1050'))
        closing_view = self._wait_view(client, lambda v: len(v['orders']) == 1)
        close_order = closing_view['orders'][0]
        self.assertEqual(close_order['order_source'], 'CONVERGENCE')
        self.assertEqual(close_order['effect'], 'REDUCE')
        self.assertEqual(close_order['direction'], 'LONG')
        self.assertEqual(close_order['original_qty'], 30)
        self.assertEqual(close_order['limit_price'], '900')  # aggressive lower_limit
        self.assertEqual(len(runtime.adapter.orders), 1)

        # Fill all 30 -> broker becomes flat -> aligned!
        cid = close_order['child_order_id']
        runtime.submit(_offline_frame(
            4, long_qty=0, last='1050',
            facts=[
                _order_fact(cid, 'nat-1', CONTRACT, 'SELL', 'CLOSE', 30),
                _trade_fact(cid, 'nat-1', 'trd-1', CONTRACT, 'SELL', 'CLOSE', 30, '1050'),
            ],
        ))
        aligned = self._wait_view(
            client,
            lambda v: (v.get('connection') or {}).get('account_positions', [{}])[0].get('status') == 'ALIGNED',
        )
        self.assertEqual(aligned['connection']['account_positions'][0]['actual_long'], 0)
        self.assertEqual(len(aligned['trades']), 1)
        self.assertEqual(aligned['trades'][0]['order_source'], 'CONVERGENCE')

    def test_opposite_short_flattens_before_opening_long_on_arm(self):
        runtime, client = self._start_runtime()
        # Trigger price 1060 wants LONG, while broker holds opposite SHORT 5.
        runtime.submit(_offline_frame(2, short_qty=5, last='1060'))
        self._wait_view(
            client,
            lambda v: (v.get('connection') or {}).get('account_positions', [{}])[0].get('actual_short') == 5,
        )
        self.assertEqual(runtime.adapter.orders, [])

        client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1',
        )
        runtime.submit(_offline_frame(3, short_qty=5, last='1060'))
        flatten_view = self._wait_view(client, lambda v: len(v['orders']) == 1)
        self.assertEqual(flatten_view['orders'][0]['order_source'], 'CONVERGENCE')
        self.assertEqual(flatten_view['orders'][0]['effect'], 'REDUCE')
        self.assertEqual(runtime.adapter.orders[0]['direction'], 'BUY')
        self.assertEqual(runtime.adapter.orders[0]['native_offset'], 'CLOSE')
        # Strategy did NOT open LONG simultaneously while SHORT is still held.
        self.assertEqual(flatten_view['markets'][0]['entry_reasons'], ['CONVERGENCE_FLATTEN_PENDING'])
        self.assertEqual(flatten_view['cycles'], [])

        # Confirm SHORT flatten -> next round opens LONG cleanly.
        cid = flatten_view['orders'][0]['child_order_id']
        runtime.submit(_offline_frame(
            4, short_qty=0, last='1060', candle_id='bar-2',
            facts=[
                _order_fact(cid, 'nat-short-1', CONTRACT, 'BUY', 'CLOSE', 5),
                _trade_fact(cid, 'nat-short-1', 'trd-short-1', CONTRACT, 'BUY', 'CLOSE', 5, '1060'),
            ],
        ))
        opened_view = self._wait_view(client, lambda v: len(v['orders']) == 2)
        open_order = next(o for o in opened_view['orders'] if o['order_source'] == 'STRATEGY')
        self.assertEqual(open_order['effect'], 'OPEN')
        self.assertEqual(open_order['direction'], 'LONG')
        self.assertEqual(open_order['original_qty'], 5)

    def test_convergence_unknown_send_reserves_capacity_without_duplicate_close(self):
        runtime, client = self._start_runtime()
        runtime.adapter.fail_send = True
        client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1',
        )
        runtime.submit(_offline_frame(2, long_qty=4, last='1050'))
        view_unknown = self._wait_view(
            client,
            lambda v: len(v['orders']) == 1 and v['orders'][0]['send_state'] == 'UNKNOWN',
        )
        self.assertEqual(view_unknown['orders'][0]['potential_remaining'], 4)
        self.assertEqual(len(runtime.adapter.orders), 1)

        # Next round still holding 4 lots with UNKNOWN pending MUST NOT send a second close order.
        runtime.adapter.fail_send = False
        runtime.submit(_offline_frame(3, long_qty=4, last='1051'))
        settled = self._wait_view(client, lambda v: v['checkpoint_event_seq'] >= view_unknown['checkpoint_event_seq'])
        self.assertEqual(len(settled['orders']), 1)
        self.assertEqual(len(runtime.adapter.orders), 1)

    def _build_real_sdk_harness(self, *, quotes, positions, orders, trades, serials, configured_code='DCE.p2701', extra_calendar=()):
        settings = load_confirmed_baseline()[0].to_settings()
        settings['symbols'] = [settings['symbols'][0] | {
            'code': configured_code, 'name': configured_code,
            'open_offset': 40, 'stop_offset': 30, 'profit_offset': 20, 'order_volume': 5,
        }]
        settings['trade'].update(
            continuation_entry=False, no_chase_after_act=False,
            passive_entry_first=False, protect_80_enabled=False,
            trailing_activate_ratio=0, max_quote_age_seconds=10,
            no_open_after_open=False, no_open_before_close_minutes=0,
        )
        prof = DsvProfile.from_settings(settings)
        contracts_cal = [{
            'contract_id': configured_code,
            'sessions': [{
                'session_id': f'{DAY}-day-0',
                'trading_day': DAY,
                'open_utc': f'{DAY}T01:00:00Z',
                'close_utc': f'{DAY}T02:15:00Z',
                'is_opening_session': True,
                'force_close_at_end': True,
            }],
        }]
        contracts_cal.extend(extra_calendar)
        calendar = TradingCalendar.from_record({
            'schema_version': 'TRADING_CALENDAR/1',
            'calendar_id': 'cal-conv',
            'calendar_version': f'tqsdk-{SDK_VERSION}',
            'complete': True,
            'coverage_from_utc': '2026-11-12T12:00:00Z',
            'coverage_to_utc': '2026-11-13T08:00:00Z',
            'contracts': contracts_cal,
        })
        account = object()
        account_key = 'SIM:KQ:' + '1' * 16
        grant = DsvSimulationGrant.parse(canonical_bytes({
            'schema_version': 'KQ_DSV_DESKTOP/1',
            'grant_id': 'grant-conv-1',
            'account_fingerprint': '1' * 16,
            'ledger_path': str((self.root / 'ledger.db').resolve()),
            'contracts': [configured_code],
            'trading_day': DAY,
            'trading_days': [DAY],
            'resume_entries_on_day_change': False,
            'not_before_utc': f'{DAY}T00:00:00Z',
            'not_after_utc': f'{DAY}T08:00:00Z',
            'max_order_quantity': 20,
            'daily_targets_per_contract': 10,
            'risk_budget': '1000000',
            'profile_hash': prof.content_hash,
            'calendar_hash': calendar.content_hash,
            'authorization_reference': 'DESKTOP_CONFIRM:' + 'b' * 64,
        }))
        inserted = []
        cancelled = []

        def insert_order(**kwargs):
            inserted.append(kwargs)
            oid = kwargs['order_id']
            exch, inst = kwargs['symbol'].split('.')
            orders[oid] = SimpleNamespace(
                order_id=oid, exchange_id=exch, instrument_id=inst,
                direction=kwargs['direction'], offset=kwargs['offset'],
                volume_orign=kwargs['volume'], limit_price=kwargs['limit_price'],
                volume_left=kwargs['volume'], status='ALIVE',
                insert_date_time=int(datetime(2026, 11, 13, 1, 10, 0, tzinfo=timezone.utc).timestamp()) * 1_000_000_000,
                is_error=False,
            )
            return orders[oid]

        def cancel_order(order_id, account=None):
            cancelled.append(order_id)
            if order_id in orders:
                orders[order_id].status = 'FINISHED'
                orders[order_id].volume_left = 0

        def get_position(code=None, account=None):
            if code is None:
                return positions
            return positions[code]

        api = SimpleNamespace(
            get_quote=lambda code: quotes[code],
            get_kline_serial=lambda code, _iv, data_length=500: serials[code],
            get_trading_calendar=lambda *_args: [{'date': datetime(2026, 11, 13), 'trading': True}],
            wait_update=lambda deadline=None: True,
            get_order=lambda account=None: orders,
            get_trade=lambda account=None: trades,
            get_account=lambda account=None: SimpleNamespace(
                balance=300000.0, available=200000.0, margin=5000.0, frozen_margin=0.0, risk_ratio=0.1,
            ),
            get_position=get_position,
            insert_order=insert_order,
            cancel_order=cancel_order,
            is_serial_ready=lambda _s: True,
        )
        ticks = [0]

        def clock_fn():
            sec = ticks[0]
            ticks[0] += 1
            return (datetime(2026, 11, 13, 1, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=sec),
                    1000 + sec * 1000)

        connection = SimpleNamespace(
            grant=grant, api=api, account=account, broker_connected=True, read_only=False,
            source_epoch=1, complete=lambda: True, poll_updates=lambda _wait: True,
            close=lambda: None,
        )
        adapter = DsvSdkAdapter(
            api, account, account_key=account_key, profile=prof, calendar=calendar,
            sdk_version=SDK_VERSION, _connection=connection, clock=clock_fn, owner_mode='STANDALONE',
        )

        class _TestAccountLock(AccountLock):
            def __init__(self, key):
                self.account_key = key
                self._handle = object()
                self.owner_thread = get_ident()

            def matches(self, key):
                return self.account_key == key and self.owner_thread == get_ident()

        class _TestRuntimeLock(RuntimeLock):
            def __init__(self):
                self._handle = object()
                self.owner_thread = get_ident()

            def matches(self):
                return self.owner_thread == get_ident()

        lock = _TestAccountLock(account_key)
        rt_lock = _TestRuntimeLock()
        binding = StandaloneOwnerBinding('boot-conv-1', account_key, 'TQKQ', rt_lock, lock)
        session = DsvSession(
            self.root / 'journal.sqlite', account_key=account_key, provider='TQKQ',
            trading_day=DAY, profile=prof, port=adapter,
            _execution_lock=lock, _owner_binding=binding,
        )
        self.addCleanup(session.close)
        return adapter, session, prof, inserted, cancelled

    def test_unconfigured_contract_and_external_order_cancel_with_closed_session_isolation(self):
        bar_ns = int(datetime(2026, 11, 13, 1, 9, 0, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
        quotes = {
            'DCE.p2701': SimpleNamespace(
                instrument_id='DCE.p2701', expired=False, ins_class='FUTURE', volume_multiple=10,
                trading_time={'day': [['09:00:00', '10:15:00']], 'night': []},
                datetime='2026-11-13 09:10:00.000000', last_price=1050.0, highest=1065.0, lowest=1020.0,
                bid_price1=1050.0, ask_price1=1050.0, price_tick=1.0, lower_limit=950.0, upper_limit=1150.0, margin=4000.0,
            ),
            'SHFE.rb2701': SimpleNamespace(
                instrument_id='SHFE.rb2701', expired=False, ins_class='FUTURE', volume_multiple=10,
                trading_time={'day': [['09:00:00', '10:15:00']], 'night': []},
                datetime='2026-11-13 09:10:00.000000', last_price=3500.0, highest=3550.0, lowest=3480.0,
                bid_price1=3499.0, ask_price1=3501.0, price_tick=1.0, lower_limit=3300.0, upper_limit=3700.0, margin=4000.0,
            ),
            'CZCE.TA701': SimpleNamespace(
                instrument_id='CZCE.TA701', expired=True, ins_class='FUTURE', volume_multiple=5,
                trading_time={'day': [], 'night': []},
                datetime='2026-11-12 15:00:00.000000', last_price=5000.0, highest=5050.0, lowest=4950.0,
                bid_price1=4998.0, ask_price1=5002.0, price_tick=2.0, lower_limit=4600.0, upper_limit=5400.0, margin=3000.0,
            ),
        }
        positions = {
            'DCE.p2701': SimpleNamespace(pos_long=0, pos_short=0, pos_long_today=0, pos_long_his=0, pos_short_today=0, pos_short_his=0, margin=0.0),
            'SHFE.rb2701': SimpleNamespace(pos_long=3, pos_short=0, pos_long_today=0, pos_long_his=3, pos_short_today=0, pos_short_his=0, margin=12000.0),
            'CZCE.TA701': SimpleNamespace(pos_long=0, pos_short=2, pos_long_today=0, pos_long_his=0, pos_short_today=2, pos_short_his=0, margin=6000.0),
        }
        orders = {
            'ext-ord-1': SimpleNamespace(
                order_id='ext-ord-1', exchange_id='SHFE', instrument_id='rb2701',
                direction='BUY', offset='OPEN', volume_orign=1, limit_price=3490.0,
                volume_left=1, status='ALIVE', insert_date_time=bar_ns, is_error=False,
            ),
        }
        trades = {}
        serials = {
            'DCE.p2701': _make_serial([
                {'datetime': bar_ns - 60_000_000_000, 'open': 1040.0, 'high': 1052.0, 'low': 1038.0, 'close': 1048.0},
                {'datetime': bar_ns, 'open': 1048.0, 'high': 1054.0, 'low': 1046.0, 'close': 1050.0},
            ]),
        }
        adapter, session, _prof, inserted, cancelled = self._build_real_sdk_harness(
            quotes=quotes, positions=positions, orders=orders, trades=trades, serials=serials,
            extra_calendar=[{
                'contract_id': 'CZCE.TA701',
                'sessions': [{
                    'session_id': f'{DAY}-closed',
                    'trading_day': DAY,
                    'open_utc': f'{DAY}T05:00:00Z',
                    'close_utc': f'{DAY}T07:00:00Z',
                    'is_opening_session': False,
                    'force_close_at_end': True,
                }],
            }],
        )
        # Round 1 (DISARMED): syncs cleanly without touching external order or unconfigured holdings.
        adapter.step(session, trading_day=DAY, complete=True)
        self.assertEqual(inserted, [])
        self.assertEqual(cancelled, [])

        # ARM -> Round 2 cancels ext-ord-1 on SHFE.rb2701 cleanly; CZCE.TA701 defers without error.
        session.command({'id': 'cmd-arm', 'action': 'START'})
        adapter.step(session, trading_day=DAY, complete=True)
        self.assertEqual(cancelled, ['ext-ord-1'])
        self.assertEqual(inserted, [])
        snap = session.snapshot()
        self.assertEqual(snap['convergence_status']['CZCE.TA701']['status'], 'DEFERRED_SESSION_OR_QUOTE')

        # Round 3 (ext-ord-1 now FINISHED): SHFE.rb2701 flattens historical 3 lots via 'CLOSE' while DCE.p2701 is unblocked.
        adapter.step(session, trading_day=DAY, complete=True)
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0]['symbol'], 'SHFE.rb2701')
        self.assertEqual(inserted[0]['direction'], 'SELL')
        self.assertEqual(inserted[0]['offset'], 'CLOSE')
        self.assertEqual(inserted[0]['volume'], 3)
        # Closed unconfigured CZCE.TA701 still must NOT block configured DCE.p2701 from entering if signal fires!
        quotes['DCE.p2701'].last_price = 1060.0
        quotes['DCE.p2701'].bid_price1 = 1060.0
        quotes['DCE.p2701'].ask_price1 = 1060.0
        quotes['DCE.p2701'].datetime = '2026-11-13 09:10:01.000000'
        step_receipt = adapter.step(session, trading_day=DAY, complete=True)
        dce_opens = [item for item in inserted if item['symbol'] == 'DCE.p2701' and item['offset'] == 'OPEN']
        self.assertEqual(len(dce_opens), 1, msg=f'step_receipt={step_receipt}')

    def test_sdk_adapter_plan_b_startup_retains_proved_and_flattens_excess_only_on_arm(self):
        t0 = int(datetime(2026, 11, 13, 1, 5, 0, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
        t_fill = int(datetime(2026, 11, 13, 1, 8, 10, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
        quotes = {
            'SHFE.ag2702': SimpleNamespace(
                instrument_id='SHFE.ag2702', expired=False, ins_class='FUTURE', volume_multiple=15,
                trading_time={'day': [['09:00:00', '10:15:00']], 'night': []},
                datetime='2026-11-13 09:10:00.000000', last_price=7550.0, highest=7560.0, lowest=7500.0,
                bid_price1=7549.0, ask_price1=7551.0, price_tick=1.0, lower_limit=7000.0, upper_limit=8000.0, margin=6000.0,
            ),
        }
        positions = {
            'SHFE.ag2702': SimpleNamespace(pos_long=8, pos_short=2, pos_long_today=8, pos_long_his=0, pos_short_today=2, pos_short_his=0, margin=60000.0),
        }
        orders = {}
        trades = {
            'hist-t1': SimpleNamespace(
                trade_id='hist-t1', order_id='hist-o1', exchange_id='SHFE', instrument_id='ag2702',
                direction='BUY', offset='OPEN', volume=5, price=7540.0, trade_date_time=t_fill,
            ),
        }
        serials = {
            'SHFE.ag2702': _make_serial([
                {'datetime': t0, 'open': 7505.0, 'high': 7520.0, 'low': 7500.0, 'close': 7515.0},
                {'datetime': t0 + 180_000_000_000, 'open': 7515.0, 'high': 7544.0, 'low': 7512.0, 'close': 7542.0},
            ]),
        }
        adapter, session, _prof, inserted, cancelled = self._build_real_sdk_harness(
            quotes=quotes, positions=positions, orders=orders, trades=trades, serials=serials,
            configured_code='SHFE.ag2702',
        )
        # DISARMED startup sync runs Plan B cleanly without raising canonical_bytes range errors or placing orders!
        adapter.step(session, trading_day=DAY, complete=True)
        self.assertEqual(inserted, [])
        snap = session.snapshot()
        self.assertTrue(snap['startup_inferred'])
        inf = snap['startup_inference']['SHFE.ag2702']
        self.assertEqual(inf['target_long'], 5, msg=f'Unexpected Plan B inference: {inf}')
        self.assertEqual(inf['target_short'], 0)
        self.assertEqual(snap['symbols']['SHFE.ag2702']['position']['owned'], 5)
        self.assertEqual(snap['convergence_status']['SHFE.ag2702']['excess_long'], 3)
        self.assertEqual(snap['convergence_status']['SHFE.ag2702']['excess_short'], 2)

        # Even if quote jumps while still DISARMED, ZERO exits or closes fire before START.
        quotes['SHFE.ag2702'].datetime = '2026-11-13 09:10:01.000000'
        quotes['SHFE.ag2702'].last_price = 7505.0
        quotes['SHFE.ag2702'].bid_price1 = 7504.0
        adapter.step(session, trading_day=DAY, complete=True)
        self.assertEqual(inserted, [])

        # User clicks START -> flattens 3 excess LONG (CLOSETODAY) and 2 opposite SHORT (CLOSETODAY), AND stops out the 5 retained lots at 7505!
        session.command({'id': 'cmd-start', 'action': 'START'})
        quotes['SHFE.ag2702'].datetime = '2026-11-13 09:10:02.000000'
        adapter.step(session, trading_day=DAY, complete=True)
        # Expect: 1 strategy STOP_LOSS exit for 5 + 1 convergence excess close for 3 + 1 convergence opposite SHORT close for 2
        self.assertTrue(any(o['offset'] == 'CLOSETODAY' and o['direction'] == 'SELL' and o['volume'] == 3 for o in inserted))
        self.assertTrue(any(o['offset'] == 'CLOSETODAY' and o['direction'] == 'BUY' and o['volume'] == 2 for o in inserted))
        self.assertTrue(any(o['offset'] == 'CLOSETODAY' and o['direction'] == 'SELL' and o['volume'] == 5 for o in inserted))

    def test_standalone_ui_hides_external_banner_and_displays_account_target_delta(self):
        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        window = WorkbenchWindow('test-pipe-conv')
        self.addCleanup(window.close)
        prof_rec = base_qa_profile().to_record()
        window._boot = 'boot-ui-conv'
        window._stale = False
        window.view = {
            'view_schema': 'workbench/1',
            'account_key': 'DEV:ui-conv',
            'kernel_boot_id': 'boot-ui-conv',
            'checkpoint_event_seq': '5',
            'entry_authority': 'DISARMED',
            'execution_scope': 'ONLINE_SIMULATION',
            'truncated': False,
            'connection': {
                'broker_connected': True,
                'synchronized': True,
                'fault': False,
                'recovery_required': False,
                'storage_failed': False,
                'clock_fault': False,
                'execution_mode': 'STANDALONE',
                'account_positions': [
                    {
                        'contract_id': CONTRACT, 'name': '棕榈油', 'configured': True,
                        'actual_long': 30, 'actual_short': 0, 'target_long': 0, 'target_short': 0,
                        'excess_long': 30, 'excess_short': 0, 'status': 'PREVIEW_NEEDS_CONVERGENCE',
                    },
                    {
                        'contract_id': 'SHFE.cu2505', 'name': 'SHFE.cu2505', 'configured': False,
                        'actual_long': 2, 'actual_short': 0, 'target_long': 0, 'target_short': 0,
                        'excess_long': 2, 'excess_short': 0, 'last': '71000',
                        'status': 'PREVIEW_NEEDS_CONVERGENCE',
                    },
                ],
            },
            'configuration': {
                'rule_set_id': 'DSV_TARGET_EXECUTION',
                'config_revision': '1',
                'scoped_contracts': [CONTRACT],
                'strategy_symbols': prof_rec['settings']['symbols'],
                'market_health': {'status': 'HEALTHY', 'feed_entry_blocked': False, 'symbols': {CONTRACT: 'FRESH'}},
            },
            'markets': [{
                'contract_id': CONTRACT, 'name': '棕榈油', 'last': '1050',
                'entry_reasons': [], 'trigger_prices': {'LONG': '1060', 'SHORT': '1048'},
                'fresh': True, 'enabled': True, 'market_session_active': True,
            }],
            'cycles': [],
            'orders': [],
            'trades': [],
            'blocks': [],
            'commands': [],
            'policies': [],
            'external_positions': [],
        }
        window.dashboard.render_home()
        self.assertIsNone(window.dashboard.home_attention_summary(window.view, (), stale=False, truncated=False))
        self.assertTrue(window.dashboard.home_attention.isHidden())
        grid = window.dashboard.dashboard_markets
        self.assertEqual(grid.rowCount(), 2)
        self.assertIn('实多 30', grid.item(0, 3).text())
        self.assertEqual(grid.item(0, 4).text(), '待收敛 (未启动)')
        self.assertIn('[非配置]', grid.item(1, 0).text())
        self.assertEqual(grid.item(1, 4).text(), '待清仓 (未启动)')


if __name__ == '__main__':
    unittest.main()
