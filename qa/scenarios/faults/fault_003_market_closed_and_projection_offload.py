"""PR4 Fault Scenario: Closed session classification & off-thread slow projection isolation."""

from __future__ import annotations

from pathlib import Path
import time
from uuid import uuid4

from online_trader.transport.workbench_snapshot import WorkbenchPublication
from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import DAY

CONTRACTS = ("DCE.p2701", "SHFE.ag2702")


def _verify_slow_projection_offload() -> None:
    """Verify WorkbenchPublication(deferred=True) never blocks the Owner thread during slow encode."""
    pipe = f"qa-proj-offload-{uuid4().hex}"
    pub = WorkbenchPublication(pipe, interval_ns=0, deferred=True)
    completed: list[int] = []
    counter = {"i": 0}

    def prepare():
        counter["i"] += 1
        seq = counter["i"]

        def complete():
            time.sleep(0.25)
            completed.append(seq)
            return {"projection_seq": seq}, {"projection_seq": seq, "markets": []}

        return complete

    try:
        t0 = time.monotonic()
        for _ in range(3):
            pub.publish(prepare)
            time.sleep(0.01)
        elapsed = time.monotonic() - t0
        if elapsed >= 0.2:
            raise AssertionError(f"WorkbenchPublication.publish() blocked Owner thread ({elapsed:.3f}s)")
        deadline = time.monotonic() + 3.0
        while not completed and time.monotonic() < deadline:
            time.sleep(0.02)
        if not completed:
            raise AssertionError("Background deferred encoder did not complete")
    finally:
        pub.close()


def run(work_dir: Path) -> dict[str, object]:
    _verify_slow_projection_offload()

    with TradingScenario(
        work_dir,
        scenario_name="E2E-FAULT-003-closed-session-and-offload",
        contract=CONTRACTS[0],
        contracts=CONTRACTS,
        trading_day=DAY,
        max_quote_age_seconds=5,
    ) as scenario:
        # Both contracts closed -> MARKET_CLOSED per symbol, HEALTHY global feed (never false stall)
        r1 = scenario.step(
            {"wall_time": f"{DAY}T01:10:00Z"},
            ticks_by_contract={
                code: {
                    "last": "1054",
                    "bid1": "1053",
                    "ask1": "1054",
                    "candle_id": "bar-0110",
                    "opening_session": False,
                    "market_session_active": False,
                }
                for code in CONTRACTS
            },
        )
        mh = (r1.workbench.get("configuration") or {}).get("market_health") or {}
        if mh.get("status") != "HEALTHY" or mh.get("feed_entry_blocked"):
            raise AssertionError(f"Closed session must not report stalled/blocked feed, got {mh}")
        symbols = mh.get("symbols") or {}
        for code in CONTRACTS:
            if symbols.get(code) != "MARKET_CLOSED":
                raise AssertionError(f"Expected {code}=MARKET_CLOSED, got {symbols}")

        return scenario.finish(passed=True)
