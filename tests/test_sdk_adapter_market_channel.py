"""Contract tests for real production DsvSdkAdapter under MarketChannel fault conditions.

Verifies quote delay, freeze/silence tracking, timestamp regression, limit-locked
NaN book levels -> DEFERRED_LIMIT_LOCKED, multi-contract session boundaries
(including SHFE silver night session), and malformed timestamp fail-closed handling.
"""

from __future__ import annotations

from decimal import Decimal
import unittest

from online_trader.configuration.baseline import load_confirmed_baseline
from online_trader.configuration.dsv import DsvProfile
from online_trader.dsv_runtime.controller import ExplicitOrderController
from qa.scenario_engine.channel import MarketChannel
from qa.scenario_engine.models import (
    ChannelFaultKind,
    FaultRule,
    InstrumentSpec,
    MarketTickTruth,
    ObservedTick,
    SessionWindow,
    TriggerKind,
)
from qa.scenario_engine.sdk_bridge import DsvSdkAdapterBridge, build_calendar_from_sessions


DAY = "2026-11-25"
PALM = "DCE.p2701"
SILVER = "SHFE.ag2702"


def _build_profile(contracts: tuple[str, ...], *, max_quote_age_seconds: int = 10) -> DsvProfile:
    settings = load_confirmed_baseline()[0].to_settings()
    template = settings["symbols"][0]
    settings["symbols"] = [
        template | {
            "code": code,
            "open_offset": 40,
            "stop_offset": 8,
            "profit_offset": 20,
            "order_volume": 5,
        }
        for code in contracts
    ]
    settings["trade"].update(
        continuation_entry=False,
        no_chase_after_act=False,
        passive_entry_first=False,
        max_quote_age_seconds=max_quote_age_seconds,
    )
    return DsvProfile.from_settings(settings)


def _build_two_contract_calendar():
    sessions = {
        PALM: [
            # Palm oil night ends at 23:00 CST (15:00Z), morning 09:00-11:30 CST (01:00Z-03:30Z)
            SessionWindow(
                symbol=PALM,
                trading_day=DAY,
                session_id="night",
                session_kind="NIGHT",
                open_utc="2026-11-24T13:00:00Z",
                close_utc="2026-11-24T15:00:00Z",
                open_ns=0,
                close_ns=0,
                is_opening_session=True,
                force_close_at_end=False,
            ),
            SessionWindow(
                symbol=PALM,
                trading_day=DAY,
                session_id="morning",
                session_kind="DAY",
                open_utc=f"{DAY}T01:00:00Z",
                close_utc=f"{DAY}T03:30:00Z",
                open_ns=0,
                close_ns=0,
                is_opening_session=False,
                force_close_at_end=False,
            ),
        ],
        SILVER: [
            # Silver night runs until 02:30 CST (18:30Z)
            SessionWindow(
                symbol=SILVER,
                trading_day=DAY,
                session_id="night",
                session_kind="NIGHT",
                open_utc="2026-11-24T13:00:00Z",
                close_utc="2026-11-24T18:30:00Z",
                open_ns=0,
                close_ns=0,
                is_opening_session=True,
                force_close_at_end=False,
            ),
            SessionWindow(
                symbol=SILVER,
                trading_day=DAY,
                session_id="morning",
                session_kind="DAY",
                open_utc=f"{DAY}T01:00:00Z",
                close_utc=f"{DAY}T03:30:00Z",
                open_ns=0,
                close_ns=0,
                is_opening_session=False,
                force_close_at_end=False,
            ),
        ],
    }
    return build_calendar_from_sessions(
        contracts=(PALM, SILVER),
        sessions_by_symbol=sessions,
        coverage_from_utc="2026-11-24T12:00:00Z",
        coverage_to_utc=f"{DAY}T08:00:00Z",
    )


def _make_bridge(initial_utc: str = f"{DAY}T01:30:00Z") -> DsvSdkAdapterBridge:
    contracts = (PALM, SILVER)
    instruments = {
        PALM: InstrumentSpec(
            symbol=PALM,
            trading_day=DAY,
            exchange_id="DCE",
            instrument_id="p2701",
            price_tick=Decimal("2"),
            volume_multiple=Decimal("10"),
            upper_limit="9000",
            lower_limit="7000",
        ),
        SILVER: InstrumentSpec(
            symbol=SILVER,
            trading_day=DAY,
            exchange_id="SHFE",
            instrument_id="ag2702",
            price_tick=Decimal("1"),
            volume_multiple=Decimal("15"),
            upper_limit="8500",
            lower_limit="6500",
        ),
    }
    return DsvSdkAdapterBridge(
        contracts=contracts,
        profile=_build_profile(contracts),
        calendar=_build_two_contract_calendar(),
        initial_utc=initial_utc,
        instruments=instruments,
    )


class DsvSdkAdapterMarketChannelTests(unittest.TestCase):
    def test_delayed_quote_reports_exact_source_age_ns(self) -> None:
        bridge = _make_bridge(f"{DAY}T01:30:30Z")
        bridge.api.apply_observed_tick(
            PALM,
            ObservedTick(
                symbol=PALM,
                source_time_utc=f"{DAY}T01:30:00Z",
                delivery_time_utc=f"{DAY}T01:30:30Z",
                last="8000",
                bid1="7998",
                ask1="8002",
                day_low="7950",
                day_high="8050",
                candle_id="bar-0930",
            ),
        )
        row, obs = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertEqual(obs["source_age_raw_ns"], str(30 * 1_000_000_000))
        self.assertEqual(obs["source_time_utc"], f"{DAY}T01:30:00.000000Z")
        self.assertEqual(row["contract_id"], PALM)

    def test_frozen_quote_accumulates_silence_ns_without_refreshing_distinct_time(self) -> None:
        bridge = _make_bridge(f"{DAY}T01:30:00Z")
        tick = ObservedTick(
            symbol=PALM,
            source_time_utc=f"{DAY}T01:30:00Z",
            delivery_time_utc=f"{DAY}T01:30:00Z",
            last="8000",
            bid1="7998",
            ask1="8002",
            day_low="7950",
            day_high="8050",
            candle_id="bar-0930",
        )
        bridge.api.apply_observed_tick(PALM, tick)
        _, obs1 = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertEqual(obs1["silence_ns"], "0")
        first_distinct = obs1["last_distinct_quote_mono_ns"]

        # Advance 15 seconds while quote.datetime remains frozen
        bridge.clock.advance(milliseconds=15_000)
        _, obs2 = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertEqual(obs2["last_distinct_quote_mono_ns"], first_distinct)
        self.assertEqual(obs2["silence_ns"], str(15 * 1_000_000_000))

    def test_out_of_order_time_regression_flagged_as_source_anomaly(self) -> None:
        bridge = _make_bridge(f"{DAY}T01:30:10Z")
        bridge.api.apply_observed_tick(
            PALM,
            ObservedTick(
                symbol=PALM,
                source_time_utc=f"{DAY}T01:30:10Z",
                delivery_time_utc=f"{DAY}T01:30:10Z",
                last="8010",
                bid1="8008",
                ask1="8012",
                day_low="7950",
                day_high="8050",
                candle_id="bar-0930",
            ),
        )
        row1, obs1 = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertEqual(obs1["age_quality"], "VALID")
        self.assertEqual(len(row1["ticks"]), 1)

        # Deliver an older tick (01:30:05Z < 01:30:10Z)
        bridge.clock.advance(milliseconds=1_000)
        bridge.api.apply_observed_tick(
            PALM,
            ObservedTick(
                symbol=PALM,
                source_time_utc=f"{DAY}T01:30:05Z",
                delivery_time_utc=f"{DAY}T01:30:11Z",
                last="8000",
                bid1="7998",
                ask1="8002",
                day_low="7950",
                day_high="8050",
                candle_id="bar-0930",
            ),
        )
        row2, obs2 = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertEqual(obs2["age_quality"], "SOURCE_ANOMALY")
        self.assertEqual(obs2["reason"], "QUOTE_TIME_REGRESSION")
        self.assertEqual(row2["ticks"], [])

    def test_limit_locked_nan_book_converts_to_none_and_triggers_deferred_limit_locked(self) -> None:
        bridge = _make_bridge(f"{DAY}T01:30:00Z")
        # Limit-up on PALM: last == upper_limit (9000), ask1 is missing (NaN in TqSdk)
        bridge.api.apply_observed_tick(
            PALM,
            ObservedTick(
                symbol=PALM,
                source_time_utc=f"{DAY}T01:30:00Z",
                delivery_time_utc=f"{DAY}T01:30:00Z",
                last="9000",
                bid1="9000",
                ask1=None,
                day_low="7950",
                day_high="9000",
                candle_id="bar-0930",
            ),
            use_nan_for_missing_book=True,
        )
        row_up, _ = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertEqual(len(row_up["ticks"]), 1)
        tick_up = row_up["ticks"][0]
        self.assertIsNone(tick_up["ask1"])
        self.assertEqual(tick_up["bid1"], "9000")
        self.assertTrue(ExplicitOrderController._convergence_limit_locked(tick_up, close_buy=True))
        self.assertFalse(ExplicitOrderController._convergence_limit_locked(tick_up, close_buy=False))

        # Limit-down on SILVER: last == lower_limit (6500), bid1 is missing (NaN in TqSdk)
        bridge.api.apply_observed_tick(
            SILVER,
            ObservedTick(
                symbol=SILVER,
                source_time_utc=f"{DAY}T01:30:00Z",
                delivery_time_utc=f"{DAY}T01:30:00Z",
                last="6500",
                bid1=None,
                ask1="6500",
                day_low="6500",
                day_high="7500",
                candle_id="bar-0930",
            ),
            use_nan_for_missing_book=True,
        )
        row_down, _ = bridge.observe_market_row(SILVER, trading_day=DAY)
        tick_down = row_down["ticks"][0]
        self.assertIsNone(tick_down["bid1"])
        self.assertEqual(tick_down["ask1"], "6500")
        self.assertTrue(ExplicitOrderController._convergence_limit_locked(tick_down, close_buy=False))

    def test_cross_session_silver_night_active_while_palm_closed(self) -> None:
        # 2026-11-24T17:00:00Z = 01:00 CST: SILVER night is active (until 18:30Z), PALM closed at 15:00Z
        bridge = _make_bridge("2026-11-24T17:00:00Z")
        bridge.api.apply_observed_tick(
            SILVER,
            ObservedTick(
                symbol=SILVER,
                source_time_utc="2026-11-24T17:00:00Z",
                delivery_time_utc="2026-11-24T17:00:00Z",
                last="7500",
                bid1="7499",
                ask1="7501",
                day_low="7450",
                day_high="7550",
                candle_id="bar-0100",
            ),
        )
        bridge.api.apply_observed_tick(
            PALM,
            ObservedTick(
                symbol=PALM,
                source_time_utc="2026-11-24T14:59:59Z",
                delivery_time_utc="2026-11-24T17:00:00Z",
                last="8000",
                bid1="7998",
                ask1="8002",
                day_low="7950",
                day_high="8050",
                candle_id="bar-2259",
            ),
        )
        row_ag, obs_ag = bridge.observe_market_row(SILVER, trading_day=DAY)
        row_p, obs_p = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertTrue(row_ag["market_session_active"])
        self.assertEqual(obs_ag["age_quality"], "VALID")
        self.assertFalse(row_p["market_session_active"])
        self.assertEqual(obs_p["reason"], "QUOTE_OUTSIDE_CURRENT_SESSION")
        self.assertEqual(row_p["ticks"], [])

    def test_malformed_quote_timestamp_fails_closed(self) -> None:
        bridge = _make_bridge(f"{DAY}T01:30:00Z")
        bridge.api.set_raw_quote_datetime(PALM, "NOT-A-VALID-TIMESTAMP")
        row, obs = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertEqual(obs["age_quality"], "SOURCE_ANOMALY")
        self.assertEqual(obs["reason"], "QUOTE_TIMESTAMP_INVALID")
        self.assertEqual(row["ticks"], [])

    def test_market_channel_one_sided_fault_penetrates_real_sdk_adapter(self) -> None:
        bridge = _make_bridge(f"{DAY}T01:30:00Z")
        channel = MarketChannel((PALM, SILVER))
        truth = {
            PALM: MarketTickTruth(
                symbol=PALM,
                trading_day=DAY,
                source_time_ns=1795570200_000_000_000,
                source_time_utc=f"{DAY}T01:30:00Z",
                last="8000",
                bid1="7998",
                bid_volume1=10,
                ask1="8002",
                ask_volume1=10,
                day_low="7950",
                day_high="8050",
                lower_limit="7000",
                upper_limit="9000",
                candle_id="bar-0930",
            ),
        }
        one_sided_rule = FaultRule(
            rule_id="TEST_LIMIT_DOWN",
            trigger_kind=TriggerKind.TIME_RANGE,
            fault_kind=ChannelFaultKind.ONE_SIDED_BOOK,
            contracts=(PALM,),
            one_sided_side="LIMIT_DOWN",
        )
        observed_map = channel.process_round(
            now_ns=1795570200_000_000_000,
            now_utc=f"{DAY}T01:30:00Z",
            truth_by_contract=truth,
            active_rules=[one_sided_rule],
        )
        bridge.api.apply_observed_tick(PALM, observed_map[PALM], use_nan_for_missing_book=True)
        row, obs = bridge.observe_market_row(PALM, trading_day=DAY)
        self.assertEqual(obs["age_quality"], "VALID")
        self.assertEqual(len(row["ticks"]), 1)
        tick = row["ticks"][0]
        self.assertEqual(tick["last"], "7000")
        self.assertIsNone(tick["bid1"])
        self.assertTrue(ExplicitOrderController._convergence_limit_locked(tick, close_buy=False))


if __name__ == "__main__":
    unittest.main()
