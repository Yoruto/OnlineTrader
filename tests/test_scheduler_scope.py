"""Current-session and quote-identity boundaries for scheduled management."""

import copy
import unittest

import test_dsv_execution_runtime as execution_fixtures
import test_scheduled_runtime as scheduling_fixtures
from test_dsv_runtime import facts

from online_trader.errors import TraderError
from online_trader.kernel.scheduler import ScheduledRuntime


class SchedulerScopeTests(unittest.TestCase):
    def setUp(self):
        self.owner = scheduling_fixtures.ScheduledRuntimeTests()
        self.owner.setUp()
        self.addCleanup(self.owner.tearDown)

    def start_driver(self, *, now="2026-11-09T01:06:00Z", interval_ms=500):
        owner = self.owner
        owner.clock = scheduling_fixtures.ManualClock(now)
        owner.calendar = scheduling_fixtures.calendar()
        owner.driver = ScheduledRuntime(owner.kernel, owner.calendar, clock=owner.clock,
                                        interval_ms=interval_ms)
        owner.driver.activate()
        owner.kernel.wait_idle()
        owner.kernel.arm_development()
        owner.kernel.wait_idle()
        return owner.driver

    def source_event(self, timestamp):
        owner = self.owner
        event, account, instrument = facts(owner.key)
        session = owner.calendar.query(event["contract_id"], timestamp)["session"]
        event.update(source_time_utc=timestamp, session_id=session["session_id"],
                     session_open_utc=session["open_utc"], session_close_utc=session["close_utc"],
                     is_opening_session=session["is_opening_session"])
        return event, account, instrument

    def test_recent_pre_break_quote_cannot_open_in_the_current_recess(self):
        driver = self.start_driver(now="2026-11-09T02:15:05Z")
        event, account, instrument = self.source_event("2026-11-09T02:14:59Z")
        self.assertFalse(self.owner.calendar.query(event["contract_id"],
                                                  self.owner.clock.sample()["now_utc"])["is_trading"])
        with self.assertRaises(TraderError) as caught:
            driver.on_market(event, account, instrument)
        self.assertEqual(caught.exception.code, "E_SCOPE")
        self.owner.kernel.wait_idle()
        self.assertEqual(self.owner.port.orders, [])
        self.assertEqual(self.owner.kernel.snapshot()["cycles"], [])
        self.assertEqual(self.owner.kernel.ledger.query("SELECT COUNT(*) AS n FROM order_intents"), [{"n": 0}])

    def test_manual_original_plan_without_entry_time_is_refused_without_type_error_or_mutation(self):
        manual = execution_fixtures.DsvExecutionRuntimeTests()
        manual.setUp()
        self.addCleanup(manual.tearDown)
        _, plan = manual.create_cycle(quantity=3, fill=3, execution_version="1.2")
        before = copy.deepcopy(manual.cycle())
        driver = ScheduledRuntime(manual.kernel, scheduling_fixtures.calendar(),
                                  clock=scheduling_fixtures.ManualClock())
        driver.activate()
        manual.kernel.wait_idle()
        with self.assertRaises(TraderError) as caught:
            driver.bind_existing_cycle(plan["position_cycle_id"])
        self.assertEqual(caught.exception.code, "E_UNSUPPORTED")
        after = manual.cycle()
        self.assertEqual(after["plan"], before["plan"])
        self.assertEqual(after["protection"], before["protection"])
        self.assertEqual(after["orders"], before["orders"])
        self.assertNotIn("schedule_binding", after)
        self.assertEqual(len(manual.port.orders), 1)

    def test_repeated_quote_does_not_postpone_clock_tick_or_refresh_quote_age(self):
        driver = self.start_driver(interval_ms=500)
        event, account, instrument = self.source_event(self.owner.clock.sample()["now_utc"])
        first = driver.on_market(event, account, instrument)
        self.owner.kernel.wait_idle()
        self.assertEqual(first.result()["status"], "ENTRY_COMMITTED")
        contract = event["contract_id"]
        original_received = driver._quotes[contract]["received_ns"]
        key = (contract, event["trading_day"])
        original_strategy_received = driver.strategy._latest[key]["received_ns"]
        for _ in range(3):
            self.owner.clock.advance(200)
            duplicate = driver.on_market(event, account, instrument)
            self.owner.kernel.poll()
            self.owner.kernel.wait_idle()
            self.assertIn("DUPLICATE_MARKET_EVENT", duplicate.result()["reasons"])
        cycle = self.owner.cycle()
        self.assertEqual(cycle["execution"]["elapsed_ms"], 600)
        self.assertEqual(driver._quotes[contract]["received_ns"], original_received)
        self.assertEqual(driver.strategy._latest[key]["received_ns"], original_strategy_received)
        self.assertGreaterEqual(self.owner.kernel._quote_age_ns(original_received), 600_000_000)
        self.assertEqual(len(self.owner.port.orders), 1)
        self.assertEqual(self.owner.port.cancellations, [])


if __name__ == "__main__":unittest.main()
