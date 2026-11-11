import copy
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import sqlite3
import tempfile
import unittest
import uuid

from online_trader.errors import TraderError
from online_trader.kernel import Kernel
from online_trader.kernel.dsv_strategy import DsvStrategyRuntime
from online_trader.kernel.strategy import StrategyCoordinator
from qa.recording_port import RecordingPort


def facts(account_key):
    market = {"account_key": account_key, "contract_id": "DCE.p2701", "trading_day": "2026-11-09",
        "session_id": "day-20261109", "source_epoch": 1, "sequence": 1,
        "source_time_utc": "2026-11-09T01:05:00Z", "session_open_utc": "2026-11-09T01:00:00Z",
        "session_close_utc": "2026-11-09T07:00:00Z", "last": "1055", "bid1": "1054", "ask1": "1056",
        "day_high": "1090", "day_low": "1000", "tick_size": "1", "quality": "CONTIGUOUS",
        "day_range_complete": True, "path_complete": True, "is_trading": True, "calendar_complete": True,
        "candle_id": "20261109-0105"}
    account = {"account_key": account_key, "trading_day": "2026-11-09", "mode": "simulation", "revision": 1,
        "complete": True, "equity": "100000", "available": "80000", "margin": "10000", "frozen_margin": "1000",
        "pending_margin": "0", "symbol_margin": "1000", "risk_ratio": "0.1", "daily_loss": "0",
        "orders_today": 0, "active_symbols": [], "stopped_symbols": [], "unknown_orders": False,
        "local_reservations_excluded": True}
    instrument = {"contract_id": "DCE.p2701", "trading_day": "2026-11-09", "complete": True,
        "tick_size": "1", "multiplier": "10", "margin_rate": "0.1", "margin_per_lot": "1000",
        "lower_limit": "800", "upper_limit": "1200", "min_order_qty": 1, "max_order_qty": 100,
        "days_to_expiry": 30}
    return market, account, instrument


class DsvRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.db"
        self.account_key = "DEV:dsv-runtime-" + uuid.uuid4().hex
        self.port = RecordingPort()
        self.kernel = Kernel(self.path, self.account_key, development=True, port=self.port, risk_budget="1000000")
        self.runtime = DsvStrategyRuntime(self.kernel)
        self.market, self.account, self.instrument = facts(self.account_key)

    def tearDown(self):
        try:self.kernel.close()
        except TraderError:
            if not self.kernel._failed:raise
        finally:self.temp.cleanup()

    def arm(self):
        activation = self.runtime.activate()
        self.assertFalse(activation["armed"])
        self.kernel.arm_development();self.kernel.wait_idle()

    def observe(self, market=None, account=None, instrument=None):
        future = self.runtime.on_market(market or self.market, account or self.account, instrument or self.instrument)
        self.kernel.wait_idle()
        return future.result()

    def cycle(self):return self.kernel.snapshot()["cycles"][0]

    def test_default_kernel_or_missing_development_port_cannot_activate(self):
        for development in (False, True):
            with self.subTest(development=development):
                key = "DEV:no-activation-" + uuid.uuid4().hex
                port = RecordingPort() if not development else None
                with Kernel(Path(self.temp.name) / (key.rsplit("-", 1)[1] + ".db"), key,
                            development=development, port=port) as kernel:
                    runtime = DsvStrategyRuntime(kernel)
                    with self.assertRaises(TraderError) as error:runtime.activate()
                    self.assertEqual(error.exception.code, "E_RULE_UNAPPROVED")
                    self.assertFalse(kernel.snapshot()["armed"])

    def test_signal_commits_one_strategy_intent_before_any_recorded_send(self):
        self.arm()
        future = self.runtime.on_market(self.market, self.account, self.instrument)
        pending = self.kernel._pending[0]
        pending.future.result(timeout=3)
        self.assertFalse(future.done())
        self.assertEqual(self.port.orders, [])
        self.assertEqual(self.kernel._committed, {})
        durable = self.kernel.ledger.query("SELECT origin,strategy_epoch FROM order_intents WHERE effect='OPEN'")
        self.assertEqual(durable, [{"origin": "STRATEGY", "strategy_epoch": self.runtime.epoch}])
        self.kernel.wait_idle();report = future.result()
        self.assertEqual(report["status"], "ENTRY_COMMITTED")
        self.assertEqual(len(self.port.orders), 1)
        self.assertEqual(report["child_order_id"], self.port.orders[0]["child_order_id"])
        self.assertEqual(self.kernel._entry_gates,{})
        cycle = self.cycle()
        self.assertEqual(cycle["plan"]["theoretical_entry_price"], "1055")
        self.assertEqual(cycle["plan"]["fixed_stop_loss"]["distance_points"], "16")
        self.assertEqual(cycle["plan"]["execution_scope"], "DEVELOPMENT")
        proposal = self.kernel.ledger.query("SELECT proposal_kind,intent_id,status FROM strategy_proposals")
        self.assertEqual(len(proposal), 1)
        self.assertEqual((proposal[0]["proposal_kind"], proposal[0]["status"]), ("DECISION", "COMMITTED"))
        self.assertIsNotNone(proposal[0]["intent_id"])

    def test_first_partial_and_late_fill_keep_the_frozen_original_protection(self):
        self.account.update(equity="300000", available="200000")
        self.arm();report = self.observe();child = report["child_order_id"]
        self.assertEqual(self.port.orders[0]["original_qty"], 3)
        original = copy.deepcopy(self.cycle()["plan"])
        self.kernel.record_fill(child, "dsv-first-partial", 1, "1055");self.kernel.wait_idle()
        cycle_id = self.cycle()["position_cycle_id"]
        self.kernel.observe(cycle_id, "1095", bid1="1095", ask1="1096");self.kernel.wait_idle()
        self.assertEqual(self.cycle()["protection"]["mode"], "PROTECTED")
        self.kernel.record_fill(child, "dsv-late-partial", 1, "1055");self.kernel.wait_idle()
        cycle = self.cycle()
        self.assertEqual(cycle["plan"], original)
        self.assertEqual((cycle["protection"]["mode"], cycle["protection"]["owned_qty"]), ("PROTECTED", 2))
        self.assertTrue(self.port.cancellations)
        self.assertEqual(len([order for order in self.port.orders if order["effect"] == "OPEN"]), 1)

    def test_duplicate_market_and_reverse_direction_share_the_original_candle(self):
        self.arm();self.observe()
        before = self.kernel.ledger.query("SELECT COUNT(*) AS n FROM event_journal")[0]["n"]
        duplicate = self.observe()
        self.assertIn("DUPLICATE_MARKET_EVENT", duplicate["reasons"])
        self.assertEqual(self.kernel.ledger.query("SELECT COUNT(*) AS n FROM event_journal")[0]["n"], before)
        market = self.market | {"sequence": 2, "source_time_utc": "2026-11-09T01:05:01Z",
                                "last": "1035", "bid1": "1034", "ask1": "1036"}
        report = self.observe(market)
        self.assertIn("CANDLE_OPPORTUNITY_CONSUMED", report["reasons"])
        self.assertEqual(len(self.port.orders), 1)
        self.assertEqual(len(self.kernel.ledger.query("SELECT * FROM order_intents WHERE effect='OPEN'")), 1)

    def test_risk_refusal_consumes_candle_and_survives_restart_without_sending(self):
        self.arm();self.account.update(available="0")
        refused = self.observe()
        self.assertEqual(refused["status"], "OBSERVED")
        self.assertFalse(refused["risk"]["allowed"])
        self.assertIsNone(refused["child_order_id"])
        self.assertEqual(self.port.orders, [])
        self.kernel.close();self.port = RecordingPort()
        self.kernel = Kernel(self.path, self.account_key, development=True, port=self.port, risk_budget="1000000")
        self.runtime = DsvStrategyRuntime(self.kernel)
        self.assertFalse(self.kernel.snapshot()["armed"])
        self.arm()
        self.account.update(available="80000", revision=2)
        market = self.market | {"sequence": 2, "source_time_utc": "2026-11-09T01:05:01Z"}
        replay = self.observe(market)
        self.assertIn("CANDLE_OPPORTUNITY_CONSUMED", replay["reasons"])
        self.assertEqual(self.port.orders, [])
        self.assertEqual(self.kernel.ledger.query("SELECT * FROM order_intents"), [])

    def test_pause_while_intent_ack_is_pending_blocks_final_send(self):
        self.arm();future = self.runtime.on_market(self.market, self.account, self.instrument)
        self.kernel.pause();self.kernel.wait_idle()
        self.assertEqual(future.result()["status"], "ENTRY_COMMITTED")
        self.assertEqual(self.port.orders, [])
        self.assertEqual(self.cycle()["orders"][0]["send_state"], "DEFINITELY_NOT_SENT")

    def test_changed_opponent_quote_during_commit_cannot_authorize_old_limit(self):
        self.arm();future = self.runtime.on_market(self.market, self.account, self.instrument)
        changed = self.market | {"sequence": 2, "source_time_utc": "2026-11-09T01:05:01Z",
                                 "last": "1056", "bid1": "1055", "ask1": "1057"}
        self.runtime.update_facts(changed, self.account, self.instrument)
        self.kernel.wait_idle();self.assertIsNotNone(future.result()["child_order_id"])
        self.assertEqual(self.port.orders, [])
        self.assertEqual(self.cycle()["orders"][0]["send_state"], "DEFINITELY_NOT_SENT")

    def test_new_account_revision_is_checked_again_at_dispatch(self):
        self.arm();future = self.runtime.on_market(self.market, self.account, self.instrument)
        changed = self.account | {"revision": 2, "available": "0", "risk_ratio": "0.9"}
        self.runtime.update_facts(self.market, changed, self.instrument)
        self.kernel.wait_idle();future.result()
        self.assertEqual(self.port.orders, [])
        self.assertEqual(self.cycle()["orders"][0]["send_state"], "DEFINITELY_NOT_SENT")

    def test_ambiguous_send_preserves_unknown_occupancy_and_prevents_another_entry(self):
        self.arm();self.port.fail_send = True;self.observe()
        self.assertEqual(self.cycle()["orders"][0]["send_state"], "UNKNOWN")
        reservation = self.kernel.ledger.query("SELECT amount,status FROM reservations")[0]
        self.assertEqual(reservation["status"], "UNKNOWN")
        self.assertNotEqual(reservation["amount"], "0")
        with self.assertRaises(TraderError):self.kernel.arm_development()
        market = self.market | {"sequence": 2, "candle_id": "20261109-0106", "source_time_utc": "2026-11-09T01:06:00Z"}
        report = self.observe(market)
        self.assertIn("ENTRY_AUTHORITY_DISARMED", report["reasons"])
        self.assertEqual(len(self.port.orders), 1)

    def test_market_source_gap_is_persisted_without_an_entry(self):
        self.arm();first = self.market | {"last": "1045", "bid1": "1044", "ask1": "1046"}
        self.observe(first)
        gap = self.market | {"sequence": 3, "candle_id": "20261109-0106", "source_time_utc": "2026-11-09T01:06:00Z"}
        report = self.observe(gap)
        self.assertIn("MARKET_PATH_INCOMPLETE", report["reasons"])
        self.assertEqual(self.port.orders, [])
        self.assertEqual(self.kernel.ledger.query("SELECT quality FROM observation_windows"), [{"quality": "DATA_GAP"}])

    def test_old_strategy_epoch_cannot_submit_another_market_decision(self):
        self.arm();coordinator = StrategyCoordinator(self.kernel)
        _, digest = coordinator.checkpoint()
        new_epoch = coordinator.register_shadow("replacement-" + uuid.uuid4().hex, digest)
        barrier, digest = coordinator.checkpoint()
        coordinator.switch(new_epoch, expected_old_epoch=self.kernel.active_epoch, barrier_seq=barrier, state_hash=digest)
        self.kernel.wait_idle()
        with self.assertRaises(TraderError) as error:self.runtime.on_market(self.market, self.account, self.instrument)
        self.assertEqual(error.exception.code, "E_EPOCH_REVOKED")
        self.assertEqual(self.port.orders, [])

    def test_duplicate_quote_cannot_refresh_the_final_send_deadline(self):
        self.arm()
        with patch("online_trader.kernel.dsv_strategy.time.monotonic_ns",return_value=1_000_000_000):
            future=self.runtime.on_market(self.market,self.account,self.instrument)
        self.kernel._pending[0].future.result(timeout=3)
        with patch("online_trader.kernel.dsv_strategy.time.monotonic_ns",return_value=40_000_000_000):
            self.runtime.update_facts(self.market,self.account,self.instrument)
            self.kernel.wait_idle()
        self.assertEqual(future.result()["status"],"ENTRY_COMMITTED")
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.cycle()["orders"][0]["send_state"],"DEFINITELY_NOT_SENT")

    def test_instrument_multiplier_change_during_commit_cannot_reprice_frozen_risk(self):
        self.arm();future=self.runtime.on_market(self.market,self.account,self.instrument)
        self.kernel._pending[0].future.result(timeout=3)
        self.runtime.update_facts(self.market,self.account,self.instrument|{"multiplier":"1000"})
        self.kernel.wait_idle();future.result()
        self.assertEqual(self.port.orders,[])
        self.assertEqual(self.cycle()["risk_per_lot"],"160")

    def test_uncovered_partial_position_retains_margin_until_explicit_fact_coverage(self):
        self.arm();child=self.observe()["child_order_id"]
        self.kernel.record_fill(child,"partial-margin-coverage",1,"1056");self.kernel.wait_idle()
        candidate=self.cycle()["strategy_metadata"]["candidate"]|{"contract_id":"DCE.l2701","stop_distance":"20"}
        facts={"account":copy.deepcopy(self.account),"instrument":self.instrument|{"contract_id":"DCE.l2701"}}
        result=self.runtime._risk(candidate,facts)
        self.assertEqual(result["metrics"]["used_margin"],"12000")
        facts["account"].update(margin="11000",covered_local_cycle_ids=[self.cycle()["position_cycle_id"]])
        covered=self.runtime._risk(candidate,facts)
        self.assertEqual(covered["metrics"]["used_margin"],"12000")

    def test_month_stop_is_durable_and_blocks_a_later_day_after_restart(self):
        self.account["strategy_stop_facts"]={"net_loss_points":"32","stop_streak":2,"continuation_stopped":False,
            "day_stopped":False,"day_stopped_reason":None,"stop_days":["2026-11-04","2026-11-05","2026-11-06"],
            "month_stop":None,"trading_days":["2026-11-04","2026-11-05","2026-11-06","2026-11-09"]}
        self.arm();report=self.observe()
        self.assertEqual(report["stops"]["state"]["month_stop"],"2026-11")
        self.assertEqual(self.port.orders,[])
        self.kernel.close();self.port=RecordingPort()
        self.kernel=Kernel(self.path,self.account_key,development=True,port=self.port,risk_budget="1000000")
        self.runtime=DsvStrategyRuntime(self.kernel);self.arm()
        market=self.market|{"trading_day":"2026-11-10","session_id":"day-20261110","candle_id":"20261110-0105",
            "source_time_utc":"2026-11-10T01:05:00Z","session_open_utc":"2026-11-10T01:00:00Z","session_close_utc":"2026-11-10T07:00:00Z"}
        account={k:v for k,v in self.account.items() if k!="strategy_stop_facts"}|{"trading_day":"2026-11-10","revision":2}
        report=self.observe(market,account,self.instrument|{"trading_day":"2026-11-10"})
        self.assertIn("DSV_PERSISTED_DAY_OR_MONTH_STOP",report["reasons"])
        self.assertEqual(self.port.orders,[])

    def test_continuation_only_streak_does_not_block_normal_entry(self):
        self.account["strategy_stop_facts"]={"net_loss_points":"0","stop_streak":2,"continuation_stopped":False,
            "day_stopped":False,"day_stopped_reason":None,"stop_days":[],"month_stop":None,"trading_days":[]}
        self.arm();report=self.observe()
        self.assertTrue(report["stops"]["allow_new_entries"])
        self.assertFalse(report["stops"]["allow_continuation"])
        self.assertEqual(len(self.port.orders),1)

    def test_later_fill_snapshot_cannot_roll_back_the_persisted_entry_observation(self):
        self.account.update(equity="300000",available="200000")
        self.arm();child=self.observe()["child_order_id"]
        self.kernel.record_fill(child,"observation-fill-1",1,"1056");self.kernel.wait_idle()
        second=self.market|{"sequence":2,"candle_id":"20261109-0106","source_time_utc":"2026-11-09T01:06:00Z"}
        self.observe(second)
        self.kernel.record_fill(child,"observation-fill-2",1,"1056");self.kernel.wait_idle()
        self.kernel.close();self.port=RecordingPort()
        self.kernel=Kernel(self.path,self.account_key,development=True,port=self.port,risk_budget="1000000")
        self.runtime=DsvStrategyRuntime(self.kernel)
        self.assertEqual(self.runtime.status()["states"][0]["last_signal_bar"],"20261109-0106")
        self.assertEqual(self.port.orders,[])

    def test_consumed_refusal_state_is_in_the_strategy_handoff_checkpoint(self):
        self.arm();coordinator=StrategyCoordinator(self.kernel)
        _,before=coordinator.checkpoint()
        self.account["available"]="0"
        self.observe()
        _,after=coordinator.checkpoint()
        self.assertNotEqual(before,after)
        self.assertEqual(self.kernel.ledger.query("SELECT * FROM order_intents"),[])

    def test_complete_path_rebuild_does_not_execute_replayed_signals_or_rearm(self):
        self.arm()
        first=self.market|{"last":"1045","bid1":"1044","ask1":"1046"}
        self.observe(first)
        third=self.market|{"sequence":3,"candle_id":"20261109-0107","source_time_utc":"2026-11-09T01:07:00Z"}
        self.observe(third)
        second=self.market|{"sequence":2,"candle_id":"20261109-0106","source_time_utc":"2026-11-09T01:06:00Z"}
        rebuilt=self.runtime.rebuild_observation([first,second,third],evidence_id="complete-path-1")
        self.kernel.wait_idle()
        self.assertEqual(rebuilt.result()["signals_executed"],0)
        self.assertFalse(self.kernel.snapshot()["armed"])
        self.assertEqual(self.port.orders,[])
        self.assertFalse(self.runtime.status()["states"][0]["coverage_lost"])
        count=self.kernel.ledger.query("SELECT COUNT(*) AS n FROM event_journal")[0]["n"]
        self.assertEqual(self.runtime.rebuild_observation([first,second,third],evidence_id="complete-path-1").result(),rebuilt.result())
        self.assertEqual(self.kernel.ledger.query("SELECT COUNT(*) AS n FROM event_journal")[0]["n"],count)
        with self.assertRaises(TraderError):self.runtime.rebuild_observation([first,second],evidence_id="complete-path-1")
        self.kernel.arm_development();self.kernel.wait_idle()
        fresh=self.market|{"sequence":4,"candle_id":"20261109-0108","source_time_utc":"2026-11-09T01:08:00Z"}
        self.observe(fresh)
        self.assertEqual(len(self.port.orders),1)

    def test_incomplete_path_rebuild_cannot_clear_the_source_gap(self):
        self.arm()
        first=self.market|{"last":"1045","bid1":"1044","ask1":"1046"};self.observe(first)
        third=self.market|{"sequence":3,"candle_id":"20261109-0107","source_time_utc":"2026-11-09T01:07:00Z"};self.observe(third)
        with self.assertRaises(TraderError):self.runtime.rebuild_observation([first,third],evidence_id="missing-middle")
        self.assertTrue(self.runtime.status()["states"][0]["coverage_lost"])
        self.assertEqual(self.port.orders,[])

    def test_rollback_or_wrong_boot_ack_never_sends_or_completes_successfully(self):
        for mode in ("rollback", "wrong-ack"):
            with self.subTest(mode=mode):
                account_key = "DEV:dsv-failure-" + uuid.uuid4().hex
                port = RecordingPort()
                kernel = Kernel(Path(self.temp.name) / (mode + ".db"), account_key, development=True,
                                port=port, risk_budget="1000000")
                try:
                    runtime = DsvStrategyRuntime(kernel);runtime.activate();kernel.arm_development();kernel.wait_idle()
                    if mode == "rollback":
                        original = kernel.ledger.submit
                        def fail(kind, event_type, payload, mutate=None, **kwargs):
                            if event_type == "ENTRY_PREPARED":
                                def rolled_back(connection, sequence):
                                    mutate(connection, sequence)
                                    raise sqlite3.OperationalError("Injected full disk before required commit")
                                return original(kind, event_type, payload, rolled_back, **kwargs)
                            return original(kind, event_type, payload, mutate, **kwargs)
                        kernel.ledger.submit = fail
                    market, account, instrument = facts(account_key)
                    future = runtime.on_market(market, account, instrument)
                    if mode == "wrong-ack":
                        pending = kernel._pending[0];ack = pending.future.result(timeout=3)
                        pending.future = Future();pending.future.set_result(replace(ack, kernel_boot_id="boot-old"))
                    with self.assertRaises(TraderError):kernel.wait_idle()
                    self.assertTrue(future.done())
                    with self.assertRaises(TraderError):future.result()
                    self.assertEqual(port.orders, [])
                    if mode == "rollback":self.assertEqual(kernel.ledger.query("SELECT * FROM order_intents"), [])
                finally:
                    try:kernel.close()
                    except TraderError:
                        if not kernel._failed:raise


if __name__ == "__main__":unittest.main()
