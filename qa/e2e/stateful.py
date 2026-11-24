"""Deterministic seeded stateful fuzzing and trace replay for PR4 (no third-party deps)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random

from qa.e2e.scenario import TradingScenario
from qa.target_owner.offline_runtime import CONTRACT, DAY


def _next_trading_day(current_day: str) -> str:
    parsed = datetime.strptime(current_day, "%Y-%m-%d").date()
    candidate = parsed + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.isoformat()


class StatefulFuzzRunner:
    """Deterministic seeded action explorer over TradingScenario + ReferenceOracle."""

    def __init__(self, *, seed: int, max_steps: int = 25, contract: str = CONTRACT) -> None:
        self.seed = int(seed)
        self.max_steps = int(max_steps)
        self.contract = contract
        self._rng = random.Random(self.seed)

    def _choose_action(self, scenario: TradingScenario, *, state: dict[str, object]) -> dict[str, object]:
        assert scenario.broker is not None
        snap = scenario.broker.snapshot()
        orders: dict[str, dict[str, object]] = snap["orders"]  # type: ignore[assignment]
        trades: dict[str, dict[str, object]] = snap["trades"]  # type: ignore[assignment]
        order_ids = list(orders.keys())
        trade_ids = list(trades.keys())
        pos = snap["positions"]  # type: ignore[assignment]
        long_qty = int(pos["long_qty"])

        undecided = [
            (idx, cid) for idx, cid in enumerate(order_ids)
            if orders[cid]["status"] == "SEND_REQUESTED"
        ]
        alive = [
            (idx, cid) for idx, cid in enumerate(order_ids)
            if orders[cid]["status"] == "ALIVE"
        ]
        cancellable = [
            (idx, cid) for idx, cid in alive
            if orders[cid]["cancel_effect_ids"]
        ]
        cancelled_with_rem = [
            (idx, cid) for idx, cid in enumerate(order_ids)
            if orders[cid]["status"] == "CANCELLED"
            and int(orders[cid]["traded_qty"]) < int(orders[cid]["request"]["quantity"])
        ]

        candidates: list[tuple[int, dict[str, object]]] = []

        # Broker progression actions (high priority when orders exist)
        if undecided:
            idx, cid = self._rng.choice(undecided)
            row = orders[cid]
            if row.get("send_adapter_result") == "ERROR":
                candidates.append((5, {"kind": "CONFIRM_ABSENT", "order_index": idx}))
                candidates.append((3, {"kind": "ACCEPT_PENDING", "order_index": idx}))
            else:
                candidates.append((6, {"kind": "ACCEPT_PENDING", "order_index": idx}))
                candidates.append((2, {"kind": "REJECT_PENDING", "order_index": idx}))

        if cancellable:
            idx, _cid = self._rng.choice(cancellable)
            candidates.append((6, {"kind": "CONFIRM_CANCEL", "order_index": idx}))

        if alive:
            idx, cid = self._rng.choice(alive)
            row = orders[cid]
            rem = int(row["request"]["quantity"]) - int(row["traded_qty"])
            price = str(row["request"]["limit_price"])
            if rem > 1:
                qty = self._rng.randint(1, rem - 1)
                candidates.append((5, {"kind": "PARTIAL_FILL", "order_index": idx, "quantity": qty, "price": price}))
            candidates.append((4, {"kind": "FULL_FILL", "order_index": idx, "quantity": rem, "price": price}))

        if cancelled_with_rem:
            idx, cid = self._rng.choice(cancelled_with_rem)
            row = orders[cid]
            rem = int(row["request"]["quantity"]) - int(row["traded_qty"])
            candidates.append((3, {
                "kind": "LATE_FILL_AFTER_CANCEL",
                "order_index": idx,
                "quantity": 1 if rem > 1 else rem,
                "price": str(row["request"]["limit_price"]),
            }))

        if trade_ids:
            t_idx = self._rng.randrange(len(trade_ids))
            candidates.append((2, {"kind": "REPLAY_TRADE", "trade_index": t_idx}))

        # Strategy / control / fault actions
        if not state["armed"] and not state["clock_fault"]:
            candidates.append((5, {"kind": "ARM"}))
        elif state["armed"]:
            candidates.append((2, {"kind": "DISARM"}))

        if state["clock_fault"]:
            candidates.append((6, {"kind": "RECOVER_CLOCK"}))
        elif not state["clock_fault_used"]:
            candidates.append((2, {"kind": "CLOCK_FAULT"}))

        if state["fail_send"]:
            candidates.append((4, {"kind": "CLEAR_SEND_FAULT"}))
        elif not state["send_fault_used"] and state["armed"] and long_qty == 0 and not undecided:
            candidates.append((2, {"kind": "SET_SEND_FAULT"}))

        # Market ticks
        price_choices = ["1054", "1060", "1052", "1080"] if long_qty > 0 else ["1054", "1060", "1055"]
        price = self._rng.choice(price_choices)
        candidates.append((4, {"kind": "TICK", "last": price, "advance_seconds": self._rng.randint(1, 3)}))

        if not state["rolled_day"] and not undecided and not alive and not state["clock_fault"]:
            candidates.append((1, {"kind": "ROLL_DAY"}))

        weights = [w for w, _ in candidates]
        chosen = self._rng.choices([act for _, act in candidates], weights=weights, k=1)[0]
        return dict(chosen)

    @staticmethod
    def _apply_action(
        scenario: TradingScenario,
        action: dict[str, object],
        *,
        state: dict[str, object],
        candle_counter: list[int],
    ) -> None:
        assert scenario.broker is not None
        kind = str(action["kind"])
        if kind == "ARM":
            receipt = scenario.arm_strategy()
            if receipt.get("status") == "SUCCEEDED":
                state["armed"] = True
                scenario.oracle.set_target_expectation(scenario.contract, direction="LONG", quantity=5)
            return
        if kind == "DISARM":
            receipt = scenario.disarm_strategy()
            if receipt.get("status") == "SUCCEEDED":
                state["armed"] = False
            return
        if kind == "SET_SEND_FAULT":
            scenario.set_adapter_fault(fail_send=True)
            state["fail_send"] = True
            state["send_fault_used"] = True
            return
        if kind == "CLEAR_SEND_FAULT":
            scenario.set_adapter_fault(fail_send=False)
            state["fail_send"] = False
            return
        def resolve_order_id() -> str:
            order_keys = list(scenario.broker.snapshot()["orders"].keys())  # type: ignore[union-attr]
            return str(order_keys[int(action["order_index"])])

        if kind == "ACCEPT_PENDING":
            scenario.broker.accept(resolve_order_id())
            scenario.step({"advance_seconds": 1})
            return
        if kind == "REJECT_PENDING":
            scenario.broker.reject(resolve_order_id())
            scenario.step({"advance_seconds": 1})
            return
        if kind == "CONFIRM_ABSENT":
            cid = resolve_order_id()
            scenario.broker.confirm_absent(cid)
            scenario.step({"advance_seconds": 1}, absent_orders=[cid])
            return
        if kind == "CONFIRM_CANCEL":
            scenario.broker.confirm_cancel(resolve_order_id())
            scenario.step({"advance_seconds": 1})
            return
        if kind in {"PARTIAL_FILL", "FULL_FILL"}:
            scenario.broker.fill(
                resolve_order_id(),
                int(action["quantity"]),
                str(action["price"]),
            )
            scenario.step({"advance_seconds": 1})
            snap = scenario.broker.snapshot()
            pos = snap["positions"]  # type: ignore[index]
            if int(pos["long_qty"]) == 0 and int(pos["short_qty"]) == 0 and not snap["active_children"]:
                scenario.oracle.exit_active.pop(scenario.contract, None)
            return
        if kind == "LATE_FILL_AFTER_CANCEL":
            scenario.broker.late_fill(
                resolve_order_id(),
                int(action["quantity"]),
                str(action["price"]),
            )
            scenario.step({"advance_seconds": 1})
            snap = scenario.broker.snapshot()
            pos = snap["positions"]  # type: ignore[index]
            if int(pos["long_qty"]) == 0 and int(pos["short_qty"]) == 0 and not snap["active_children"]:
                scenario.oracle.exit_active.pop(scenario.contract, None)
            return
        if kind == "REPLAY_TRADE":
            trade_keys = list(scenario.broker.snapshot()["trades"].keys())  # type: ignore[union-attr]
            tid = str(trade_keys[int(action["trade_index"])])
            scenario.broker.replay_trade_fact(tid)
            scenario.step({"advance_seconds": 1})
            return
        if kind == "CLOCK_FAULT":
            state["clock_fault"] = True
            state["clock_fault_used"] = True
            state["armed"] = False
            scenario.oracle.set_authority_expectation("DISARMED")
            scenario.step({"advance_seconds": 1, "last": "1060"}, health_reason="OWNER_SCHEDULING_GAP")
            return
        if kind == "RECOVER_CLOCK":
            scenario.step({"advance_seconds": 1, "last": "1054"})
            outcome = scenario.step({"advance_seconds": 1, "last": "1054"})
            state["clock_fault"] = bool((outcome.workbench.get("connection") or {}).get("clock_fault"))
            return
        if kind == "ROLL_DAY":
            new_day = _next_trading_day(scenario.trading_day)
            scenario.roll_trading_day(
                new_day,
                session_open=f"{new_day}T01:00:00Z",
                session_close=f"{new_day}T02:15:00Z",
            )
            state["rolled_day"] = True
            candle_counter[0] += 1
            scenario.step({
                "wall_time": f"{new_day}T01:10:00Z",
                "last": "1054",
                "candle_id": f"bar-day-{candle_counter[0]}",
            })
            return
        if kind == "TICK":
            candle_counter[0] += 1
            last = str(action["last"])
            outcome = scenario.step({
                "advance_seconds": int(action.get("advance_seconds", 1)),
                "last": last,
                "bid1": str(int(last) - 1),
                "ask1": last,
                "candle_id": f"bar-{candle_counter[0]:03d}",
            })
            # Sync exit expectation only from observed cycle exit_reason in public view
            for cycle in outcome.workbench.get("cycles") or []:
                if cycle.get("contract_id") == scenario.contract and cycle.get("exit_reason"):
                    scenario.oracle.set_target_expectation(
                        scenario.contract,
                        direction="LONG",
                        quantity=5,
                        exit_target=0,
                    )
            snap = scenario.broker.snapshot()
            pos = snap["positions"]  # type: ignore[index]
            if int(pos["long_qty"]) == 0 and int(pos["short_qty"]) == 0 and not snap["active_children"]:
                scenario.oracle.exit_active.pop(scenario.contract, None)
            return
        raise ValueError(f"Unsupported stateful action kind: {kind}")

    def run(self, work_dir: Path) -> dict[str, object]:
        script_steps: list[dict[str, object]] = []
        work_dir = Path(work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        candle_counter = [1]
        state: dict[str, object] = {
            "armed": False,
            "fail_send": False,
            "send_fault_used": False,
            "clock_fault": False,
            "clock_fault_used": False,
            "rolled_day": False,
        }

        with TradingScenario(
            work_dir,
            scenario_name=f"E2E-STATEFUL-seed-{self.seed}",
            contract=self.contract,
            trading_day=DAY,
        ) as scenario:
            # Initial baseline sync
            scenario.step({
                "wall_time": f"{DAY}T01:10:00Z",
                "last": "1054",
                "bid1": "1053",
                "ask1": "1054",
                "candle_id": "bar-001",
            })
            for step_idx in range(1, self.max_steps + 1):
                action = self._choose_action(scenario, state=state)
                script_steps.append({"step": step_idx, **action})
                self._apply_action(scenario, action, state=state, candle_counter=candle_counter)

            assert scenario.broker is not None
            final_broker = scenario.broker.snapshot()
            report = scenario.finish(passed=True)

        script_obj = {
            "schema_version": "ONLINE_TRADER_STATEFUL_SCRIPT/1",
            "seed": self.seed,
            "contract": self.contract,
            "trading_day": DAY,
            "steps": script_steps,
            "expected_summary": {
                "rounds_recorded": report.get("rounds_recorded"),
                "final_positions": final_broker["positions"],
                "order_count": len(final_broker["orders"]),
                "trade_count": len(final_broker["trades"]),
            },
        }
        script_path = work_dir / "evidence" / "stateful-script.json"
        script_path.write_text(json.dumps(script_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "seed": self.seed,
            "script_path": str(script_path),
            "report": report,
            "summary": script_obj["expected_summary"],
        }


def replay_stateful_trace(script_path: str | Path, replay_dir: str | Path) -> dict[str, object]:
    """Deterministically replay a saved stateful-script.json and assert identical outcome."""
    script = json.loads(Path(script_path).read_text(encoding="utf-8"))
    if script.get("schema_version") != "ONLINE_TRADER_STATEFUL_SCRIPT/1":
        raise ValueError(f"Unsupported stateful script schema: {script.get('schema_version')}")
    replay_root = Path(replay_dir).resolve()
    replay_root.mkdir(parents=True, exist_ok=True)
    candle_counter = [1]
    state: dict[str, object] = {
        "armed": False,
        "fail_send": False,
        "send_fault_used": False,
        "clock_fault": False,
        "clock_fault_used": False,
        "rolled_day": False,
    }
    contract = str(script["contract"])
    trading_day = str(script["trading_day"])

    with TradingScenario(
        replay_root,
        scenario_name=f"E2E-REPLAY-seed-{script['seed']}",
        contract=contract,
        trading_day=trading_day,
    ) as scenario:
        scenario.step({
            "wall_time": f"{trading_day}T01:10:00Z",
            "last": "1054",
            "bid1": "1053",
            "ask1": "1054",
            "candle_id": "bar-001",
        })
        for step in script["steps"]:
            StatefulFuzzRunner._apply_action(scenario, step, state=state, candle_counter=candle_counter)

        assert scenario.broker is not None
        final_broker = scenario.broker.snapshot()
        report = scenario.finish(passed=True)

    actual_summary = {
        "rounds_recorded": report.get("rounds_recorded"),
        "final_positions": final_broker["positions"],
        "order_count": len(final_broker["orders"]),
        "trade_count": len(final_broker["trades"]),
    }
    expected_summary = script["expected_summary"]
    if actual_summary != expected_summary:
        raise AssertionError(
            f"Deterministic replay diverged: expected={expected_summary}, actual={actual_summary}"
        )
    return {
        "status": "PASS",
        "seed": script["seed"],
        "summary": actual_summary,
        "report_path": str(replay_root / "evidence" / "report.json"),
    }
