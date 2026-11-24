"""Tests for unified market permissions, call-auction handling, retry recovery, and UI visibility."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes
from online_trader.dsv_runtime.market_permissions import (
    classify_market_phase_rejection,
    evaluate_market_permissions,
    normalize_trading_status,
)
from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
from online_trader.dsv_runtime.runner import DsvSession
from online_trader.dsv_runtime.sdk_adapter import DsvSdkAdapter
from online_trader.online_scope import DsvSimulationGrant
from online_trader.runtime.instance import AccountLock, RuntimeLock
from online_trader.runtime.startup_metadata import SDK_VERSION
from online_trader.scheduling.calendar import TradingCalendar
from online_trader.transport.status_client import StatusClient
from qa.target_owner.fake_broker import FakeBroker
from qa.target_owner.offline_runtime import OfflineTargetRuntime

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from online_trader.desktop.workbench import WorkbenchWindow


CONTRACT = 'DCE.p2701'
DAY = '2026-11-13'
_BEIJING = timezone(timedelta(hours=8))


def _make_serial(bars):
    class _FakeSerial:
        @property
        def iloc(self):
            return bars

        def tail(self, count):
            return SimpleNamespace(to_dict=lambda _mode: bars[-count:])

    return _FakeSerial()


def _night_day_calendar(contract=CONTRACT):
    # 2026-11-12 21:00..23:00 BJ == 2026-11-12T13:00Z..15:00Z
    # 2026-11-13 09:00..10:15 BJ == 2026-11-13T01:00Z..02:15Z
    return TradingCalendar.from_record({
        'schema_version': 'TRADING_CALENDAR/1',
        'calendar_id': 'cal-auction-test',
        'calendar_version': f'tqsdk-{SDK_VERSION}',
        'complete': True,
        'coverage_from_utc': '2026-11-12T12:00:00Z',
        'coverage_to_utc': '2026-11-13T08:00:00Z',
        'contracts': [{
            'contract_id': contract,
            'sessions': [
                {
                    'session_id': f'{DAY}-night-0',
                    'trading_day': DAY,
                    'open_utc': '2026-11-12T13:00:00Z',
                    'close_utc': '2026-11-12T15:00:00Z',
                    'is_opening_session': True,
                    'force_close_at_end': False,
                },
                {
                    'session_id': f'{DAY}-day-0',
                    'trading_day': DAY,
                    'open_utc': f'{DAY}T01:00:00Z',
                    'close_utc': f'{DAY}T02:15:00Z',
                    'is_opening_session': True,
                    'force_close_at_end': False,
                },
                {
                    'session_id': f'{DAY}-day-1',
                    'trading_day': DAY,
                    'open_utc': f'{DAY}T02:30:00Z',
                    'close_utc': f'{DAY}T03:30:00Z',
                    'is_opening_session': False,
                    'force_close_at_end': True,
                },
            ],
        }],
    })


def _auction_frame(
    sequence,
    *,
    wall_time,
    market_phase,
    can_open,
    can_close,
    can_cancel,
    long_qty=0,
    short_qty=0,
    long_today=None,
    long_historical=None,
    short_today=None,
    short_historical=None,
    last='1060',
    include_tick=False,
    candle_id='bar-1',
    facts=(),
    non_strategy_orders=None,
    unconfigured_markets=None,
    evidence_state='INFERRED',
):
    ticks = []
    if include_tick:
        ticks = [{
            'native_time': wall_time,
            'candle_id': candle_id,
            'last': str(last),
            'day_low': '1020',
            'day_high': '1088',
            'bid1': str(last),
            'ask1': str(last),
            'tick_size': '1',
            'lower_limit': '900',
            'upper_limit': '1200',
        }]
    lt = long_qty if long_today is None else long_today
    lh = 0 if long_historical is None else long_historical
    st = short_qty if short_today is None else short_today
    sh = 0 if short_historical is None else short_historical
    result = {
        'id': f'auc-round-{sequence}',
        'sequence': sequence,
        'session_id': 'qa-session',
        'wall_time': wall_time,
        'monotonic_ms': sequence * 1000,
        'complete': True,
        'synchronized': True,
        'sync_orders': [],
        'market': [{
            'contract_id': CONTRACT,
            'ticks': ticks,
            'opening_session': True,
            'market_session_active': market_phase == 'CONTINUOUS',
            'standalone_external_model': True,
            'external_active_orders': bool(
                any(o['contract_id'] == CONTRACT for o in (non_strategy_orders or ()))
            ),
            'market_permission': {
                'contract_id': CONTRACT,
                'market_phase': market_phase,
                'evidence_state': evidence_state,
                'status_source': 'EXPLICIT_FRAME',
                'continuous_matching': market_phase == 'CONTINUOUS',
                'can_open': can_open,
                'can_close': can_close,
                'can_cancel': can_cancel,
            },
            'session_open': '2026-11-12T13:00:00Z',
            'session_close': '2026-11-12T15:00:00Z',
            'long_qty': long_qty,
            'short_qty': short_qty,
            'long_today': lt,
            'long_historical': lh,
            'short_today': st,
            'short_historical': sh,
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


def _order_fact(child_id, native_id, contract, direction, offset, qty, *,
                status='FILLED', filled=None, day=DAY, message=None,
                category=None, detail=None):
    fact = {
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
    if message is not None:
        fact['native_message'] = message
    if category is not None:
        fact['rejection_category'] = category
    if detail is not None:
        fact['rejection_detail'] = detail
    return fact


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


class MarketPermissionsUnitTests(unittest.TestCase):
    def test_calendar_phase_query_preserves_schema_and_identifies_auction_phases(self):
        cal = _night_day_calendar()
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-12T12:50:00Z')['market_phase'], 'PRE_OPEN')
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-12T12:56:00Z')['market_phase'], 'AUCTION_ORDERING')
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-12T12:59:30Z')['market_phase'], 'AUCTION_MATCH')
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-12T13:00:00Z')['market_phase'], 'CONTINUOUS')
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-12T16:00:00Z')['market_phase'], 'PRE_OPEN')
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-13T00:56:00Z')['market_phase'], 'AUCTION_ORDERING')
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-13T00:59:15Z')['market_phase'], 'AUCTION_MATCH')
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-13T02:20:00Z')['market_phase'], 'INTRADAY_BREAK')
        self.assertEqual(cal.phase_query(CONTRACT, '2026-11-13T04:00:00Z')['market_phase'], 'CLOSED')
        self.assertEqual(cal.phase_query('SHFE.rb2701', '2026-11-13T01:10:00Z')['market_phase'], 'UNKNOWN')

    def test_layered_permissions_handle_tqsdk_spelling_halt_fallback_and_broker_override(self):
        self.assertEqual(normalize_trading_status('CONTINOUS'), 'CONTINUOUS')
        self.assertEqual(normalize_trading_status('AUCTIONORDERING'), 'AUCTIONORDERING')
        self.assertEqual(normalize_trading_status('NOTRADING'), 'NOTRADING')

        cal = _night_day_calendar()
        cont_cal = cal.phase_query(CONTRACT, '2026-11-12T13:00:05Z')

        # Confirmed CONTINOUS with valid quote enables open, close, cancel.
        p_cont = evaluate_market_permissions(
            contract_id=CONTRACT, calendar_phase=cont_cal,
            live_trading_status='CONTINOUS', has_valid_quote=True,
        )
        self.assertEqual(p_cont['evidence_state'], 'CONFIRMED')
        self.assertTrue(p_cont['can_open'] and p_cont['can_close'] and p_cont['can_cancel'])

        # Live NOTRADING during continuous hours overrides calendar -> HALTED.
        p_halt = evaluate_market_permissions(
            contract_id=CONTRACT, calendar_phase=cont_cal,
            live_trading_status='NOTRADING', has_valid_quote=True,
        )
        self.assertEqual(p_halt['market_phase'], 'HALTED')
        self.assertEqual(p_halt['evidence_state'], 'CONFIRMED')
        self.assertFalse(p_halt['can_open'] or p_halt['can_close'] or p_halt['can_cancel'])
        self.assertEqual(p_halt['wait_reason'], 'DEFERRED_HALTED')

        # No live status falls back cleanly to INFERRED.
        auc_order = cal.phase_query(CONTRACT, '2026-11-12T12:56:00Z')
        p_auc = evaluate_market_permissions(
            contract_id=CONTRACT, calendar_phase=auc_order,
            live_trading_status=None, has_valid_quote=False,
        )
        self.assertEqual(p_auc['evidence_state'], 'INFERRED')
        self.assertEqual(p_auc['market_phase'], 'AUCTION_ORDERING')
        self.assertFalse(p_auc['can_open'] or p_auc['can_close'])
        self.assertTrue(p_auc['can_cancel'])

        # Broker rejection override blocks immediate retry until recovered.
        override = classify_market_phase_rejection('CTP:集合竞价期间禁止报单')
        self.assertIsNotNone(override)
        p_ovr = evaluate_market_permissions(
            contract_id=CONTRACT, calendar_phase=cont_cal,
            live_trading_status=None, has_valid_quote=True,
            broker_phase_override=override,
        )
        self.assertEqual(p_ovr['status_source'], 'BROKER_REJECTION_OVERRIDE')
        self.assertFalse(p_ovr['can_open'] or p_ovr['can_close'])


class AuctionAndConvergenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = tmp
        self.root = Path(tmp.name)

    def _start_runtime(self):
        runtime = OfflineTargetRuntime(self.root / 'rt').start()
        self.addCleanup(runtime.close)
        client = StatusClient(runtime.pipe, timeout=5)
        runtime.submit(_auction_frame(
            1, wall_time='2026-11-12T12:56:00Z', market_phase='AUCTION_ORDERING',
            can_open=False, can_close=False, can_cancel=True, long_qty=5,
        ))
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

    def test_auction_to_open_jump_cancels_external_in_ordering_defers_close_then_flattens_at_210015(self):
        runtime, client = self._start_runtime()
        ext_order = {
            'native_order_id': 'ext-auc-1',
            'contract_id': CONTRACT,
            'direction': 'BUY',
            'native_offset': 'OPEN',
            'volume_left': 2,
            'limit_price': '1055',
        }
        client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1',
        )
        # 20:56 BJ (AUCTION_ORDERING): external cancel IS sent, close order is NOT sent, failure_count == 0.
        runtime.submit(_auction_frame(
            2, wall_time='2026-11-12T12:56:10Z', market_phase='AUCTION_ORDERING',
            can_open=False, can_close=False, can_cancel=True,
            long_qty=5, non_strategy_orders=[ext_order],
        ))
        v_ord = self._wait_view(client, lambda v: len(runtime.adapter.cancellations) == 1)
        self.assertEqual(runtime.adapter.orders, [])
        acc = v_ord['connection']['account_positions'][0]
        self.assertEqual(acc['status'], 'CANCELLING_NON_STRATEGY_ORDERS')
        self.assertEqual(acc['failure_count'], 0)

        # 20:59:30 BJ (AUCTION_MATCH, external order gone): close still deferred, 0 orders, 0 failures.
        runtime.submit(_auction_frame(
            3, wall_time='2026-11-12T12:59:30Z', market_phase='AUCTION_MATCH',
            can_open=False, can_close=False, can_cancel=False, long_qty=5,
        ))
        v_match = self._wait_view(
            client,
            lambda v: (v.get('connection') or {}).get('account_positions', [{}])[0].get('status') == 'DEFERRED_AUCTION_MATCH',
        )
        self.assertEqual(runtime.adapter.orders, [])
        self.assertEqual(v_match['connection']['account_positions'][0]['failure_count'], 0)
        self.assertEqual(v_match['connection']['account_positions'][0]['wait_reason'], 'DEFERRED_AUCTION_MATCH')

        # Simulate lag / time jump straight to 21:00:15 BJ (CONTINUOUS): automatically flattens without restart!
        runtime.submit(_auction_frame(
            4, wall_time='2026-11-12T13:00:15Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=5, include_tick=True, last='1060',
        ))
        v_close = self._wait_view(client, lambda v: len(v['orders']) == 1)
        self.assertEqual(v_close['orders'][0]['order_source'], 'CONVERGENCE')
        self.assertEqual(v_close['orders'][0]['original_qty'], 5)

        cid = v_close['orders'][0]['child_order_id']
        runtime.submit(_auction_frame(
            5, wall_time='2026-11-12T13:00:16Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=0, include_tick=True, last='1060',
            facts=[
                _order_fact(cid, 'nat-auc-1', CONTRACT, 'SELL', 'CLOSE', 5),
                _trade_fact(cid, 'nat-auc-1', 'trd-auc-1', CONTRACT, 'SELL', 'CLOSE', 5, '1060'),
            ],
        ))
        v_aligned = self._wait_view(
            client,
            lambda v: (v.get('connection') or {}).get('account_positions', [{}])[0].get('status') == 'ALIGNED',
        )
        self.assertEqual(v_aligned['connection']['account_positions'][0]['actual_long'], 0)

    def test_structural_reject_recovers_when_buckets_change_and_transient_never_permanently_locks(self):
        runtime, client = self._start_runtime()
        client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1',
        )
        # First transient auction rejection does NOT trigger structural cap.
        runtime.submit(_auction_frame(
            2, wall_time='2026-11-12T13:00:01Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=5, long_today=5, long_historical=0, include_tick=True,
        ))
        v1 = self._wait_view(client, lambda v: len(v['orders']) == 1)
        cid1 = v1['orders'][0]['child_order_id']
        runtime.submit(_auction_frame(
            3, wall_time='2026-11-12T13:00:02Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=5, long_today=5, long_historical=0, include_tick=True,
            facts=[_order_fact(
                cid1, 'nat-t1', CONTRACT, 'SELL', 'CLOSE', 5,
                status='REJECTED', filled=0,
                message='集合竞价撮合阶段不能报单', category='TRANSIENT', detail='BROKER_TEMP_STATE',
            )],
        ))
        # Backoff (0s for 1st? schedule[0]=0 -> immediately sends 2nd), still no permanent lock!
        v2 = self._wait_view(client, lambda v: len(v['orders']) >= 2)
        cid2 = next(o['child_order_id'] for o in v2['orders'] if o['child_order_id'] != cid1)

        # Now simulate two structural INSUFFICIENT_CLOSE_POSITION rejects under unchanged buckets.
        runtime.submit(_auction_frame(
            4, wall_time='2026-11-12T13:00:04Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=5, long_today=5, long_historical=0, include_tick=True,
            facts=[_order_fact(
                cid2, 'nat-s1', CONTRACT, 'SELL', 'CLOSE', 5,
                status='REJECTED', filled=0,
                message='平仓量超过持仓', category='STRUCTURAL', detail='INSUFFICIENT_CLOSE_POSITION',
            )],
        ))
        self._wait_view(
            client,
            lambda v: (v.get('connection') or {}).get('account_positions', [{}])[0].get('status') == 'DEFERRED_TRANSIENT_RETRY_BACKOFF',
        )
        # Wait 1s for structural backoff, then 2nd structural attempt cid3 sends and fails.
        runtime.submit(_auction_frame(
            5, wall_time='2026-11-12T13:00:05Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=5, long_today=5, long_historical=0, include_tick=True,
        ))
        v3 = self._wait_view(client, lambda v: len(v['orders']) >= 3)
        cid3 = next(o['child_order_id'] for o in v3['orders'] if o['child_order_id'] not in {cid1, cid2})
        runtime.submit(_auction_frame(
            6, wall_time='2026-11-12T13:00:06Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=5, long_today=5, long_historical=0, include_tick=True,
            facts=[_order_fact(
                cid3, 'nat-s2', CONTRACT, 'SELL', 'CLOSE', 5,
                status='REJECTED', filled=0,
                message='平仓量超过持仓', category='STRUCTURAL', detail='INSUFFICIENT_CLOSE_POSITION',
            )],
        ))
        v_deferred = self._wait_view(
            client,
            lambda v: (v.get('connection') or {}).get('account_positions', [{}])[0].get('status') == 'DEFERRED_STRUCTURAL_REJECTION',
        )
        orders_before_change = len(v_deferred['orders'])

        # Unchanged condition on next round must NOT tight-loop.
        runtime.submit(_auction_frame(
            7, wall_time='2026-11-12T13:00:08Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=5, long_today=5, long_historical=0, include_tick=True,
        ))
        v_still = self._wait_view(client, lambda v: int(v['checkpoint_event_seq']) > int(v_deferred['checkpoint_event_seq']))
        self.assertEqual(len(v_still['orders']), orders_before_change)

        # Broker buckets update (long_historical=5) -> condition fingerprint changes -> automatically retries!
        runtime.submit(_auction_frame(
            8, wall_time='2026-11-12T13:00:10Z', market_phase='CONTINUOUS',
            can_open=True, can_close=True, can_cancel=True,
            long_qty=5, long_today=0, long_historical=5, include_tick=True,
        ))
        v_retried = self._wait_view(client, lambda v: len(v['orders']) == orders_before_change + 1)
        self.assertEqual(v_retried['orders'][-1]['native_offset'], 'CLOSE')

    def test_workbench_displays_auction_wait_status_and_remaining_lots(self):
        app = QApplication.instance() or QApplication([])
        runtime, client = self._start_runtime()
        client.command(
            'ARM_ENTRIES', {'scope_type': 'ACCOUNT', 'scope_ids': []},
            {'authority_policy_id': 'target-authority-current-boot',
             'rule_set_id': 'DSV_TARGET_EXECUTION', 'config_revision': '1'},
            expected_config_revision='1',
        )
        runtime.submit(_auction_frame(
            2, wall_time='2026-11-12T12:56:05Z', market_phase='AUCTION_ORDERING',
            can_open=False, can_close=False, can_cancel=True, long_qty=5,
        ))
        view = self._wait_view(
            client,
            lambda v: (v.get('connection') or {}).get('account_positions', [{}])[0].get('status') == 'DEFERRED_AUCTION_ORDERING',
        )
        win = WorkbenchWindow(client)
        self.addCleanup(win.close)
        win._boot = view['kernel_boot_id']
        win._stale = False
        win.view = view
        win.dashboard.render_home()
        app.processEvents()
        self.assertEqual(win.dashboard.dashboard_markets.item(0, 4).text(), '集合竞价申报待平')
        self.assertIn('待平 5 手', win.dashboard.decision_reason.text())


class SdkAdapterAuctionAndPermissionsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _harness(self, *, quotes, positions, orders, trades, serials, clock_times,
                 participate_auction=False, no_open_after_open=True, get_trading_status=None,
                 clock_fn=None):
        settings = load_confirmed_baseline()[0].to_settings()
        settings['symbols'] = [settings['symbols'][0] | {
            'code': CONTRACT, 'name': CONTRACT,
            'open_offset': 40, 'stop_offset': 30, 'profit_offset': 20, 'order_volume': 5,
        }]
        settings['trade'].update(
            continuation_entry=False, no_chase_after_act=False,
            passive_entry_first=False, protect_80_enabled=False,
            trailing_activate_ratio=0, max_quote_age_seconds=10,
            participate_auction=participate_auction,
            no_open_after_open=no_open_after_open,
            no_open_after_open_minutes=5,
            no_open_before_close_minutes=0,
        )
        prof = DsvProfile.from_settings(settings)
        calendar = _night_day_calendar()
        orig_cal_hash = calendar.content_hash
        account = object()
        account_key = 'SIM:KQ:' + '1' * 16
        grant = DsvSimulationGrant.parse(canonical_bytes({
            'schema_version': 'KQ_DSV_DESKTOP/1',
            'grant_id': 'grant-auc-1',
            'account_fingerprint': '1' * 16,
            'ledger_path': str((self.root / 'ledger.db').resolve()),
            'contracts': [CONTRACT],
            'trading_day': DAY,
            'trading_days': [DAY],
            'resume_entries_on_day_change': False,
            'not_before_utc': '2026-11-12T12:00:00Z',
            'not_after_utc': '2026-11-13T08:00:00Z',
            'max_order_quantity': 20,
            'daily_targets_per_contract': 10,
            'risk_budget': '1000000',
            'profile_hash': prof.content_hash,
            'calendar_hash': orig_cal_hash,
            'authorization_reference': 'DESKTOP_CONFIRM:' + 'c' * 64,
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
                insert_date_time=int((clock_fn()[0] if clock_fn is not None else clock_times[min(idx[0], len(clock_times) - 1)]).timestamp()) * 1_000_000_000,
                is_error=False,
            )
            return orders[oid]

        def cancel_order(order_id, account=None):
            cancelled.append(order_id)
            if order_id in orders:
                orders[order_id].status = 'FINISHED'
                orders[order_id].volume_left = 0

        api_kwargs = dict(
            get_quote=lambda code: quotes[code],
            get_kline_serial=lambda code, _iv, data_length=500: serials[code],
            get_trading_calendar=lambda *_args: [{'date': datetime(2026, 11, 13), 'trading': True}],
            wait_update=lambda deadline=None: True,
            get_order=lambda account=None: orders,
            get_trade=lambda account=None: trades,
            get_account=lambda account=None: SimpleNamespace(
                balance=300000.0, available=200000.0, margin=5000.0, frozen_margin=0.0, risk_ratio=0.1,
            ),
            get_position=lambda code=None, account=None: positions if code is None else positions[code],
            insert_order=insert_order,
            cancel_order=cancel_order,
            is_serial_ready=lambda _s: True,
        )
        if get_trading_status is not None:
            api_kwargs['get_trading_status'] = get_trading_status
        api = SimpleNamespace(**api_kwargs)
        idx = [0]
        base_dt = clock_times[0]

        def default_clock_fn():
            i = min(idx[0], len(clock_times) - 1)
            dt = clock_times[i]
            idx[0] += 1
            mono_ms = 1000 + max(0, int((dt - base_dt).total_seconds() * 1000)) + i * 100
            return dt, mono_ms

        effective_clock = clock_fn if clock_fn is not None else default_clock_fn

        connection = SimpleNamespace(
            grant=grant, api=api, account=account, broker_connected=True, read_only=False,
            source_epoch=1, complete=lambda: True, poll_updates=lambda _wait: True,
            close=lambda: None,
        )
        adapter = DsvSdkAdapter(
            api, account, account_key=account_key, profile=prof, calendar=calendar,
            sdk_version=SDK_VERSION, _connection=connection, clock=effective_clock, owner_mode='STANDALONE',
        )

        class _Lock(AccountLock):
            def __init__(self, key):
                self.account_key = key
                self._handle = object()
                self.owner_thread = get_ident()

            def matches(self, key):
                return self.account_key == key and self.owner_thread == get_ident()

        class _RtLock(RuntimeLock):
            def __init__(self):
                self._handle = object()
                self.owner_thread = get_ident()

            def matches(self):
                return self.owner_thread == get_ident()

        lock = _Lock(account_key)
        rt_lock = _RtLock()
        binding = StandaloneOwnerBinding('boot-auc-1', account_key, 'TQKQ', rt_lock, lock)
        session = DsvSession(
            self.root / 'journal.sqlite', account_key=account_key, provider='TQKQ',
            trading_day=DAY, profile=prof, port=adapter,
            _execution_lock=lock, _owner_binding=binding,
        )
        self.addCleanup(session.close)
        return adapter, session, inserted, cancelled, orig_cal_hash

    def test_first_minute_2100_allows_convergence_close_while_blocking_ordinary_open_and_unsupported_status_degrades(self):
        # 20:56:00 BJ -> 20:59:30 BJ -> 21:00:05 BJ (within first minute 21:00-21:01!)
        t_2056 = datetime(2026, 11, 12, 12, 56, 0, tzinfo=timezone.utc)
        t_2059 = datetime(2026, 11, 12, 12, 59, 30, tzinfo=timezone.utc)
        t_2100 = datetime(2026, 11, 12, 13, 0, 5, tzinfo=timezone.utc)
        bar_ns = int(t_2100.timestamp()) * 1_000_000_000
        quotes = {
            CONTRACT: SimpleNamespace(
                instrument_id=CONTRACT, expired=False, ins_class='FUTURE', volume_multiple=10,
                trading_time={'day': [['09:00:00', '10:15:00'], ['10:30:00', '11:30:00']], 'night': [['21:00:00', '23:00:00']]},
                datetime='2026-11-12 20:59:00.000000', last_price=1060.0, highest=1060.0, lowest=1020.0,
                bid_price1=1060.0, ask_price1=1060.0, price_tick=1.0, lower_limit=950.0, upper_limit=1150.0, margin=4000.0,
            ),
            # Unconfigured contract with valid schedule and 20:59 quote MUST NOT be treated as active during auction!
            'SHFE.rb2701': SimpleNamespace(
                instrument_id='SHFE.rb2701', expired=False, ins_class='FUTURE', volume_multiple=10,
                trading_time={'day': [['09:00:00', '10:15:00'], ['10:30:00', '11:30:00']], 'night': [['21:00:00', '23:00:00']]},
                datetime='2026-11-12 20:59:00.000000', last_price=3500.0, highest=3500.0, lowest=3480.0,
                bid_price1=3499.0, ask_price1=3501.0, price_tick=1.0, lower_limit=3300.0, upper_limit=3700.0, margin=4000.0,
            ),
        }
        positions = {
            CONTRACT: SimpleNamespace(pos_long=1, pos_short=0, pos_long_today=0, pos_long_his=1, pos_short_today=0, pos_short_his=0, margin=4000.0),
            'SHFE.rb2701': SimpleNamespace(pos_long=2, pos_short=0, pos_long_today=0, pos_long_his=2, pos_short_today=0, pos_short_his=0, margin=8000.0),
        }
        orders = {}
        trades = {}
        serials = {
            CONTRACT: _make_serial([{'datetime': bar_ns, 'open': 1050.0, 'high': 1060.0, 'low': 1020.0, 'close': 1060.0}]),
        }
        status_calls = [0]

        def unsupported_status(_code):
            status_calls[0] += 1
            raise Exception('您的账户不支持查看交易状态信息，需要购买后才能使用。')

        clock_state = [t_2056, 1000]

        def step_clock_fn():
            return clock_state[0], clock_state[1]

        adapter, session, inserted, cancelled, orig_cal_hash = self._harness(
            quotes=quotes, positions=positions, orders=orders, trades=trades, serials=serials,
            clock_times=[t_2056],
            participate_auction=False, no_open_after_open=True,
            get_trading_status=unsupported_status,
            clock_fn=step_clock_fn,
        )

        def advance_step(target_dt):
            while clock_state[0] < target_dt:
                delta_s = min(10, int((target_dt - clock_state[0]).total_seconds()))
                if delta_s <= 0:
                    break
                clock_state[0] = clock_state[0] + timedelta(seconds=delta_s)
                clock_state[1] += delta_s * 1000
                if clock_state[0] < target_dt:
                    adapter.step(session, trading_day=DAY, complete=True)
            return adapter.step(session, trading_day=DAY, complete=True)

        # Round 1 (DISARMED at 20:56): syncs cleanly and caches unsupported trading_status without crashing!
        adapter.step(session, trading_day=DAY, complete=True)
        self.assertEqual(adapter.calendar.content_hash, orig_cal_hash)
        self.assertEqual(status_calls[0], 1)

        session.command({'id': 'cmd-arm-first-min', 'action': 'START'})
        # Round 2 (20:56:01 AUCTION_ORDERING): 0 orders placed for both configured and unconfigured!
        advance_step(datetime(2026, 11, 12, 12, 56, 1, tzinfo=timezone.utc))
        self.assertEqual(inserted, [])
        self.assertEqual(status_calls[0], 1)  # Never re-calls unsupported get_trading_status on every tick.
        snap_auc = session.snapshot()
        self.assertEqual(snap_auc['convergence_status'][CONTRACT]['status'], 'DEFERRED_AUCTION_ORDERING')
        self.assertEqual(snap_auc['convergence_status']['SHFE.rb2701']['status'], 'DEFERRED_AUCTION_ORDERING')
        self.assertEqual(snap_auc['convergence_status'][CONTRACT]['evidence_state'], 'INFERRED')

        # Round 3 (20:59:30 AUCTION_MATCH): 20:59 auction quote must NOT become SOURCE_ANOMALY; 0 orders placed!
        advance_step(t_2059)
        self.assertEqual(inserted, [])
        snap_match = session.snapshot()
        self.assertEqual(snap_match['convergence_status'][CONTRACT]['status'], 'DEFERRED_AUCTION_MATCH')
        self.assertNotEqual(adapter.market_health['symbols'][CONTRACT], 'INVALID_QUOTE')

        # Round 4 (21:00:05 continuous open, still inside 1st minute!): close orders fire immediately, NO OPEN!
        quotes[CONTRACT].datetime = '2026-11-12 21:00:05.000000'
        quotes['SHFE.rb2701'].datetime = '2026-11-12 21:00:05.000000'
        advance_step(t_2100)
        self.assertEqual(len(inserted), 2)
        self.assertEqual({item['symbol'] for item in inserted}, {CONTRACT, 'SHFE.rb2701'})
        self.assertTrue(all(item['offset'] == 'CLOSE' for item in inserted))

        # Fill both closes; Round 5 at 21:00:06 still inside AUCTION_DISABLED / OPENING_WAIT -> no ordinary OPEN!
        positions[CONTRACT].pos_long = 0
        positions[CONTRACT].pos_long_his = 0
        positions['SHFE.rb2701'].pos_long = 0
        positions['SHFE.rb2701'].pos_long_his = 0
        fill_ns = int(t_2100.timestamp()) * 1_000_000_000
        for idx_o, item in enumerate(inserted):
            oid = item['order_id']
            exch, inst = item['symbol'].split('.')
            orders[oid].status = 'FINISHED'
            orders[oid].volume_left = 0
            orders[oid].exchange_order_id = f'ex-close-{idx_o}'
            trades[f'trd-close-{idx_o}'] = SimpleNamespace(
                trade_id=f'trd-close-{idx_o}', order_id=oid,
                exchange_id=exch, instrument_id=inst,
                direction=item['direction'], offset=item['offset'],
                volume=item['volume'], price=item['limit_price'],
                trade_date_time=fill_ns,
            )
        quotes[CONTRACT].datetime = '2026-11-12 21:00:06.000000'
        receipt = advance_step(datetime(2026, 11, 12, 13, 0, 6, tzinfo=timezone.utc))
        self.assertEqual(len(inserted), 2)
        reasons = [d['reason'] for d in receipt['decisions'] if d.get('contract_id') == CONTRACT]
        self.assertIn('AUCTION_DISABLED', reasons)

    def test_presend_and_precancel_market_phase_guard_never_consumes_cancel_budget(self):
        t_2056 = datetime(2026, 11, 12, 12, 56, 0, tzinfo=timezone.utc)
        t_2059 = datetime(2026, 11, 12, 12, 59, 30, tzinfo=timezone.utc)
        t_2100 = datetime(2026, 11, 12, 13, 0, 5, tzinfo=timezone.utc)
        bar_ns = int(t_2100.timestamp()) * 1_000_000_000
        quotes = {
            CONTRACT: SimpleNamespace(
                instrument_id=CONTRACT, expired=False, ins_class='FUTURE', volume_multiple=10,
                trading_time={'day': [['09:00:00', '10:15:00']], 'night': [['21:00:00', '23:00:00']]},
                datetime='2026-11-12 20:56:00.000000', last_price=1060.0, highest=1060.0, lowest=1020.0,
                bid_price1=1060.0, ask_price1=1060.0, price_tick=1.0, lower_limit=950.0, upper_limit=1150.0, margin=4000.0,
            ),
        }
        positions = {
            CONTRACT: SimpleNamespace(pos_long=1, pos_short=0, pos_long_today=0, pos_long_his=1, pos_short_today=0, pos_short_his=0, margin=4000.0),
        }
        orders = {
            'ext-cancel-guard': SimpleNamespace(
                order_id='ext-cancel-guard', exchange_id='DCE', instrument_id='p2701',
                direction='BUY', offset='OPEN', volume_orign=1, limit_price=1050.0,
                volume_left=1, status='ALIVE', insert_date_time=bar_ns, is_error=False,
            ),
        }
        adapter, session, inserted, cancelled, _ = self._harness(
            quotes=quotes, positions=positions, orders=orders, trades={},
            serials={CONTRACT: _make_serial([{'datetime': bar_ns, 'open': 1050.0, 'high': 1060.0, 'low': 1020.0, 'close': 1060.0}])},
            clock_times=[t_2056],
        )
        adapter.step(session, trading_day=DAY, complete=True)
        # Simulate pre-cancel guard catching AUCTION_MATCH at dispatch time without burning failure budget.
        adapter._clock_sample = lambda: SimpleNamespace(
            now_utc='2026-11-12T12:59:30Z',
            utc=t_2059,
            monotonic=2000 * 1_000_000,
            monotonic_ns=str(2000 * 1_000_000),
            clock_epoch=adapter._clock_epoch,
            paired_record=lambda: {
                'now_utc': '2026-11-12T12:59:30Z',
                'monotonic_ns': str(2000 * 1_000_000),
                'clock_epoch': adapter._clock_epoch,
            },
        )
        adapter.enabled = True
        state = session._state
        ext = {
            'native_order_id': 'ext-cancel-guard', 'contract_id': CONTRACT,
            'direction': 'BUY', 'native_offset': 'OPEN', 'volume_left': 1,
        }
        with session._journal.transaction():
            eff1 = session._controller._cancel_non_strategy_order(state, ext)
            session._journal.save(state)
        session._controller.dispatch(state, [eff1])
        self.assertEqual(cancelled, [])
        rec = state['convergence_cancels']['ext-cancel-guard']
        self.assertEqual(rec['cancel_seq'], 1)
        self.assertEqual(rec['cancel_failure_count'], 0)
        self.assertIsNone(rec['cancel_status'])

        # Move to continuous 21:00:05 -> next cancel gets cancel_seq=2 (unique effect_id) and executes!
        adapter._clock_sample = lambda: SimpleNamespace(
            now_utc='2026-11-12T13:00:05Z',
            utc=t_2100,
            monotonic=3000 * 1_000_000,
            monotonic_ns=str(3000 * 1_000_000),
            clock_epoch=adapter._clock_epoch,
            paired_record=lambda: {
                'now_utc': '2026-11-12T13:00:05Z',
                'monotonic_ns': str(3000 * 1_000_000),
                'clock_epoch': adapter._clock_epoch,
            },
        )
        with session._journal.transaction():
            eff2 = session._controller._cancel_non_strategy_order(state, ext)
            session._journal.save(state)
        self.assertNotEqual(eff1['id'], eff2['id'])
        session._controller.dispatch(state, [eff2])
        self.assertEqual(cancelled, ['ext-cancel-guard'])


class FakeBrokerAuctionScenarioTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_fake_broker_auction_wait_and_recovery_flow(self):
        broker = FakeBroker(
            account_key='DEV:fake-auction-1',
            trace_path=self.root / 'broker.jsonl',
        )
        broker.seed_external_position(CONTRACT, long_today=2)
        f1 = broker.prepare_frame(
            wall_time='2026-11-13T00:56:00Z', monotonic_ms=1000,
            session_open='2026-11-13T01:00:00Z', session_close='2026-11-13T02:15:00Z',
            market_session_active=False, include_ticks=False, include_external_orders=True,
        )
        self.assertFalse(f1['market'][0]['market_session_active'])
        self.assertEqual(f1['market'][0]['long_qty'], 2)
        broker.acknowledge_frame(f1['id'])

        f2 = broker.prepare_frame(
            wall_time='2026-11-13T01:00:05Z', monotonic_ms=2000,
            session_open='2026-11-13T01:00:00Z', session_close='2026-11-13T02:15:00Z',
            market_session_active=True, include_ticks=True, include_external_orders=True,
        )
        self.assertTrue(f2['market'][0]['market_session_active'])
        self.assertEqual(len(f2['market'][0]['ticks']), 1)
        broker.acknowledge_frame(f2['id'])
