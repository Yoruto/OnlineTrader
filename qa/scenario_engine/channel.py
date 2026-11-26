"""Market data channel fault simulator separating market truth from software observation."""

from __future__ import annotations

from copy import deepcopy
from qa.market_data.schema import ns_to_utc_iso
from qa.scenario_engine.models import (
    ChannelFaultKind,
    FaultRule,
    MarketTickTruth,
    ObservedTick,
)


class MarketChannel:
    """Transforms Layer-1 market truth into Layer-3 software-observed ticks.

    Maintains separate ``source_time`` and ``delivery_time``, and implements:
    - DROP (permanent data loss)
    - DELAY (lagging delivery while preserving true exchange source_time)
    - FREEZE (repeatedly delivering stale frozen quote)
    - BURST (buffering ticks during outage and flushing them together on recovery)
    - REORDER_DUPLICATE (out-of-order and duplicate tick delivery)
    - ONE_SIDED_BOOK (limit-up without ask1 or limit-down without bid1)
    """

    def __init__(self, contracts: tuple[str, ...]) -> None:
        self.contracts = contracts
        self.latest_truth: dict[str, MarketTickTruth] = {}
        self._last_delivered: dict[str, ObservedTick] = {}
        self._delayed_queues: dict[str, list[tuple[int, ObservedTick]]] = {c: [] for c in contracts}
        self._burst_buffers: dict[str, list[ObservedTick]] = {c: [] for c in contracts}
        self._frozen_quotes: dict[str, ObservedTick] = {}
        self._was_impaired: dict[str, bool] = {c: False for c in contracts}
        self.recovered_contracts_last_round: list[str] = []

    def _base_observed(self, truth: MarketTickTruth, delivery_utc: str) -> ObservedTick:
        return ObservedTick(
            symbol=truth.symbol,
            source_time_utc=truth.source_time_utc,
            delivery_time_utc=delivery_utc,
            last=truth.last,
            bid1=truth.bid1,
            ask1=truth.ask1,
            day_low=truth.day_low,
            day_high=truth.day_high,
            candle_id=truth.candle_id,
            opening_session=truth.opening_session,
            market_session_active=truth.market_session_active,
            drop_tick=False,
        )

    def _apply_one_sided(self, obs: ObservedTick, truth: MarketTickTruth, side: str | None) -> ObservedTick:
        if side == "LIMIT_UP":
            obs.ask1 = None
            if truth.upper_limit is not None:
                obs.last = truth.upper_limit
                obs.bid1 = truth.upper_limit
                if obs.day_high is not None:
                    obs.day_high = truth.upper_limit
        elif side == "LIMIT_DOWN":
            obs.bid1 = None
            if truth.lower_limit is not None:
                obs.last = truth.lower_limit
                obs.ask1 = truth.lower_limit
                if obs.day_low is not None:
                    obs.day_low = truth.lower_limit
        return obs

    def process_round(
        self,
        *,
        now_ns: int,
        now_utc: str,
        truth_by_contract: dict[str, MarketTickTruth],
        active_rules: list[FaultRule],
    ) -> dict[str, dict[str, object]]:
        """Update market truth and compute per-contract tick payloads visible to Runtime."""
        for sym, truth in truth_by_contract.items():
            self.latest_truth[sym] = truth

        # Index active channel fault rules by contract
        rules_by_contract: dict[str, list[FaultRule]] = {c: [] for c in self.contracts}
        for rule in active_rules:
            if rule.fault_kind == ChannelFaultKind.NONE:
                continue
            targets = rule.contracts if rule.contracts else self.contracts
            for sym in targets:
                if sym in rules_by_contract:
                    rules_by_contract[sym].append(rule)

        ticks_by_contract: dict[str, dict[str, object]] = {}
        self.recovered_contracts_last_round = []

        for sym in self.contracts:
            truth = truth_by_contract.get(sym)
            sym_rules = rules_by_contract[sym]
            impaired_now = any(
                r.fault_kind in {ChannelFaultKind.DROP, ChannelFaultKind.FREEZE, ChannelFaultKind.BURST}
                for r in sym_rules
            )

            # Track recovery transition (impaired -> healthy)
            if self._was_impaired[sym] and not impaired_now:
                self.recovered_contracts_last_round.append(sym)
                self._frozen_quotes.pop(sym, None)
            self._was_impaired[sym] = impaired_now

            # Collect any delayed ticks whose delivery time has arrived
            due_ticks: list[ObservedTick] = []
            remaining_delayed: list[tuple[int, ObservedTick]] = []
            for deliver_at_ns, queued_obs in self._delayed_queues[sym]:
                if deliver_at_ns <= now_ns:
                    queued_obs.delivery_time_utc = now_utc
                    due_ticks.append(queued_obs)
                else:
                    remaining_delayed.append((deliver_at_ns, queued_obs))
            self._delayed_queues[sym] = remaining_delayed

            # Flush burst buffer if BURST is no longer active
            burst_active = any(r.fault_kind == ChannelFaultKind.BURST for r in sym_rules)
            if not burst_active and self._burst_buffers[sym]:
                for buffered in self._burst_buffers[sym]:
                    buffered.delivery_time_utc = now_utc
                    due_ticks.append(buffered)
                self._burst_buffers[sym].clear()

            # Process current market truth tick through active channel rules
            drop_active = any(r.fault_kind == ChannelFaultKind.DROP for r in sym_rules)
            freeze_active = any(r.fault_kind == ChannelFaultKind.FREEZE for r in sym_rules)
            delay_rules = [r for r in sym_rules if r.fault_kind == ChannelFaultKind.DELAY and r.delay_seconds > 0]
            reorder_active = any(r.fault_kind == ChannelFaultKind.REORDER_DUPLICATE for r in sym_rules)
            one_sided_rules = [r for r in sym_rules if r.fault_kind == ChannelFaultKind.ONE_SIDED_BOOK]

            if drop_active:
                ticks_by_contract[sym] = {
                    "drop_tick": True,
                    "last": None,
                    "delivery_time": now_utc,
                }
                continue

            if freeze_active:
                frozen = self._frozen_quotes.get(sym) or self._last_delivered.get(sym)
                if frozen is None and truth is not None:
                    frozen = self._base_observed(truth, now_utc)
                if frozen is not None:
                    self._frozen_quotes[sym] = deepcopy(frozen)
                    stale_copy = deepcopy(frozen)
                    stale_copy.delivery_time_utc = now_utc
                    ticks_by_contract[sym] = stale_copy.to_step_dict()
                continue

            if truth is not None:
                obs = self._base_observed(truth, now_utc)
                for os_rule in one_sided_rules:
                    obs = self._apply_one_sided(obs, truth, os_rule.one_sided_side)

                if burst_active:
                    self._burst_buffers[sym].append(obs)
                    ticks_by_contract[sym] = {
                        "drop_tick": True,
                        "last": None,
                        "delivery_time": now_utc,
                    }
                    continue

                if delay_rules:
                    max_delay_ns = max(r.delay_seconds for r in delay_rules) * 1_000_000_000
                    self._delayed_queues[sym].append((now_ns + max_delay_ns, obs))
                else:
                    due_ticks.append(obs)

            if not due_ticks:
                continue

            if reorder_active:
                # Duplicate the newest tick and interleave with previous delivered tick if available
                prev = self._last_delivered.get(sym)
                extra: list[ObservedTick] = []
                if prev is not None:
                    old_copy = deepcopy(prev)
                    old_copy.delivery_time_utc = now_utc
                    extra.append(old_copy)
                dup_copy = deepcopy(due_ticks[-1])
                dup_copy.delivery_time_utc = now_utc
                due_ticks = due_ticks + extra + [dup_copy]

            self._last_delivered[sym] = deepcopy(due_ticks[-1])
            if len(due_ticks) == 1:
                ticks_by_contract[sym] = due_ticks[0].to_step_dict()
            else:
                ticks_by_contract[sym] = {
                    "ticks": [t.to_step_dict() for t in due_ticks],
                }

        return ticks_by_contract

    def is_limit_locked(self, symbol: str, *, close_buy: bool) -> bool:
        """Check whether market truth is currently limit-locked against closing."""
        truth = self.latest_truth.get(symbol)
        if truth is None:
            return False
        if close_buy:
            return truth.upper_limit is not None and truth.last >= truth.upper_limit and truth.ask1 is None
        return truth.lower_limit is not None and truth.last <= truth.lower_limit and truth.bid1 is None
