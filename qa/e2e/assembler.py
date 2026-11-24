"""Assemble DsvSession FACT_FRAME objects from FakeMarket, FakeBroker, and VirtualClock."""

from __future__ import annotations

from copy import deepcopy

from qa.e2e.clock import VirtualClock
from qa.e2e.market import FakeMarket
from qa.target_owner.fake_broker import FakeBroker


class FrameAssembler:
    """Combines independent market, broker and clock components into a FACT_FRAME."""

    def __init__(
        self,
        *,
        market: FakeMarket,
        broker: FakeBroker,
        clock: VirtualClock,
        trading_day: str,
        trading_days: list[str] | None = None,
        initial_sequence: int = 0,
    ) -> None:
        self.market = market
        self.broker = broker
        self.clock = clock
        self.trading_day = trading_day
        self.trading_days = list(trading_days) if trading_days is not None else [trading_day]
        self._sequence = int(initial_sequence)

    @property
    def sequence(self) -> int:
        return self._sequence

    def roll_trading_day(self, new_trading_day: str) -> None:
        if new_trading_day not in self.trading_days:
            self.trading_days.append(new_trading_day)
            self.trading_days.sort()
        self.trading_day = new_trading_day

    def build(
        self,
        *,
        complete: bool = True,
        synchronized: bool | None = None,
        broker_order_snapshot_complete: bool | None = None,
        absent_orders: list[str] | None = None,
        health_reason: str | None = None,
        sdk_pump_gap_ns: int | None = None,
        sdk_updated: bool | None = None,
        connection_complete: bool | None = None,
    ) -> dict[str, object]:
        self._sequence += 1
        seq = self._sequence
        frame_id = f"e2e-round-{seq}"
        clock_fields = self.clock.frame_fields(health_reason=health_reason)
        broker_snap = self.broker.prepare_broker_snapshot(
            frame_id=frame_id,
            sequence=seq,
            complete=complete,
            synchronized=synchronized,
            broker_order_snapshot_complete=broker_order_snapshot_complete,
            absent_orders=absent_orders,
        )
        positions_by_contract = broker_snap.get("positions_by_contract") or {
            self.broker.contract: broker_snap["positions"]
        }
        market_rows = self.market.snapshot(wall_time=str(clock_fields["wall_time"]))
        merged_market: list[dict[str, object]] = []
        for row in market_rows:
            merged = deepcopy(row)
            pos = positions_by_contract.get(merged["contract_id"])
            if pos is not None:
                merged.update({
                    "long_qty": pos["long_qty"],
                    "short_qty": pos["short_qty"],
                    "long_today": pos["long_today"],
                    "long_historical": pos["long_historical"],
                    "short_today": pos["short_today"],
                    "short_historical": pos["short_historical"],
                })
            else:
                merged.update({
                    "long_qty": 0,
                    "short_qty": 0,
                    "long_today": 0,
                    "long_historical": 0,
                    "short_today": 0,
                    "short_historical": 0,
                })
            merged_market.append(merged)

        frame: dict[str, object] = {
            "id": frame_id,
            "sequence": seq,
            "session_id": broker_snap["session_id"],
            "trading_day": self.trading_day,
            "trading_days": list(self.trading_days),
            "complete": broker_snap["complete"],
            "synchronized": broker_snap["synchronized"],
            "sync_orders": broker_snap["sync_orders"],
            "market": merged_market,
            "native": broker_snap["native"],
            **clock_fields,
        }
        if sdk_pump_gap_ns is not None or sdk_updated is not None or connection_complete is not None:
            frame["qa_market_health_inputs"] = {
                "sdk_pump_gap_ns": sdk_pump_gap_ns,
                "sdk_updated": sdk_updated,
                "connection_complete": connection_complete,
            }
        if "broker_order_snapshot_complete" in broker_snap:
            frame["broker_order_snapshot_complete"] = broker_snap["broker_order_snapshot_complete"]
        if "absent_orders" in broker_snap:
            frame["absent_orders"] = broker_snap["absent_orders"]
        if "non_strategy_orders" in broker_snap:
            frame["non_strategy_orders"] = deepcopy(broker_snap["non_strategy_orders"])
        return frame
