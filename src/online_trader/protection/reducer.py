"""Pure G0 ProtectionPlan reducer with an exact bounded simulation capability.

The owner authenticates facts, deduplicates trades, persists this result, and routes a
single target through its OMS. A target here is never an order or a confirmed fill.
Online use requires the exact plan and SimulationGrant-bound typed permit. A boolean
cannot enable an online plan. Network activity belongs to the execution owner.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from typing import Any, Mapping

from online_trader.dsv_core.protection_rules import half_close_target, trailing_floor
from online_trader.errors import TraderError


_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]{0,17})(\.[0-9]{0,11}[1-9])?$")
_PHASES = frozenset({"NOT_TRIGGERED", "WAITING_ENTRY_TERMINAL", "TARGET_COMMITTED", "EXECUTING", "DONE", "NO_OP", "SUPPRESSED_PROTECTED"})
_CONSUMED = frozenset({"TARGET_COMMITTED", "EXECUTING", "DONE", "NO_OP"})
_UNCONSUMED = frozenset({"NOT_TRIGGERED", "WAITING_ENTRY_TERMINAL"})
_MAX_INT = 2_147_483_647
_MAX_SEQ = 9_223_372_036_854_775_807


def _decimal(value: Any, *, input_value: bool = True) -> Decimal:
    if not isinstance(value, str) or (input_value and (not _DECIMAL.fullmatch(value) or value == "-0")):
        raise TraderError("PLAN_DECIMAL_RANGE", "Expected a canonical decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise TraderError("PLAN_DECIMAL_RANGE", "Invalid decimal") from error
    if not result.is_finite():
        raise TraderError("PLAN_DECIMAL_RANGE", "Non-finite decimal")
    return result


def _text(value: Decimal) -> str:
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _quantity(value: Any, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _MAX_INT:
        raise TraderError("E_QUANTITY", "Invalid confirmed quantity")
    return value


def _sequence(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= _MAX_SEQ:
        raise TraderError("E_FACT_BARRIER", "Invalid event sequence")
    return value


def _hash(value: Any) -> str:
    from online_trader.contracts.codec import canonical_bytes
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True,slots=True)
class SimulationPlanPermit:
    plan_hash:str
    grant_hash:str
    account_key:str
    approval_record_id:str

    @classmethod
    def issue(cls,plan,grant):
        from online_trader.online_scope import SimulationGrant
        if (not isinstance(grant,SimulationGrant) or plan.get('execution_scope')!='ONLINE_SIMULATION' or plan.get('schema_version')!='1.0'
                or plan.get('account_key')!=grant.account_key or plan.get('approval_record_id')!=grant.value['grant_id']
                or plan.get('contract_id') not in grant.value['contracts'] or plan.get('initial_open_quantity')!=1
                or any(plan.get(key,{}).get('enabled') for key in ('profit_protection','trailing_stop','partial_reduction','close_session','slippage_exit','data_gap_exit','emergency_exit'))):
            raise TraderError('PLAN_APPROVAL_REQUIRED','A concrete source-bound manual simulation plan grant is required')
        return cls(_hash(dict(plan)),grant.content_hash,grant.account_key,grant.value['grant_id'])


def _permitted(plan,permit):
    return (type(permit) in (SimulationPlanPermit,DsvSimulationPlanPermit) and plan.get('execution_scope')=='ONLINE_SIMULATION'
            and permit.plan_hash==_hash(dict(plan)) and permit.account_key==plan.get('account_key') and permit.approval_record_id==plan.get('approval_record_id'))


@dataclass(frozen=True,slots=True)
class DsvSimulationPlanPermit:
    plan_hash:str
    grant_hash:str
    account_key:str
    approval_record_id:str

    @classmethod
    def issue(cls,plan,grant):
        from online_trader.online_scope import DsvSimulationGrant
        binding=plan.get('online_binding',{})
        if (type(grant) is not DsvSimulationGrant or plan.get('schema_version')!='1.3'
                or plan.get('execution_scope')!='ONLINE_SIMULATION' or plan.get('account_key')!=grant.account_key
                or plan.get('approval_record_id')!=grant.value['grant_id'] or not grant.permits_day(plan.get('trading_day'))
                or plan.get('contract_id') not in grant.value['contracts'] or type(plan.get('initial_open_quantity')) is not int
                or not 1<=plan['initial_open_quantity']<=grant.value['max_order_quantity']
                or binding!={'grant_hash':grant.content_hash,'profile_hash':grant.value['profile_hash'],'calendar_hash':grant.value['calendar_hash']}):
            raise TraderError('PLAN_APPROVAL_REQUIRED','The exact separate DSV strategy plan scope is required')
        return cls(_hash(dict(plan)),grant.content_hash,grant.account_key,grant.value['grant_id'])


def _validate_plan(plan: Mapping[str, Any],permit=None) -> None:
    from online_trader.contracts.validation import validate_plan
    validate_plan(dict(plan))
    if plan["execution_scope"] != "DEVELOPMENT" and not _permitted(plan,permit):
        raise TraderError("PLAN_APPROVAL_REQUIRED", "This interpreter only accepts isolated DEVELOPMENT plans")
    for field in ("plan_revision", "config_revision"):
        _sequence(plan[field])
    protection = plan["profit_protection"]
    if protection["enabled"] and plan["schema_version"] == "1.0":
        floor = _decimal(protection["floor_ratio"])
        activation = _decimal(protection["activation_ratio"])
        if not Decimal("0.4") <= floor <= Decimal("0.6") or floor >= activation:
            raise TraderError("PLAN_THRESHOLD_ORDER", "Invalid protected floor or activation order")
    partial = plan["partial_reduction"]
    if partial["enabled"] and partial["second_stage"]["enabled"]:
        second = _decimal(partial["second_stage"]["trigger_ratio"], input_value=plan["schema_version"] == "1.0")
        if second <= _decimal(partial["first_trigger_ratio"]):
            raise TraderError("PLAN_THRESHOLD_ORDER", "Second stage must follow the first")
        if plan["schema_version"] != "1.0" and second >= Decimal("1.5"):
            raise TraderError("PLAN_THRESHOLD_ORDER", "DSV second stage exceeds its derived ratio bound")
    if plan["schema_version"] != "1.0" and partial["enabled"] and _decimal(partial["first_trigger_ratio"]) >= 1:
        raise TraderError("PLAN_THRESHOLD_ORDER", "DSV first reduction ratio must be below one")
    trailing = plan["trailing_stop"]
    if trailing["enabled"] and plan["schema_version"] != "1.0" and not 0 <= _decimal(trailing["drawdown_value"]) <= 1:
        raise TraderError("PLAN_THRESHOLD_ORDER", "DSV drawdown ratio is outside zero to one")
    if trailing["enabled"] and trailing["drawdown_mode"] == "PEAK_FAVORABLE_RATIO":
        if not 0 < _decimal(trailing["drawdown_value"]) < 1:
            raise TraderError("PLAN_THRESHOLD_ORDER", "Peak drawdown ratio must be between zero and one")
    steps = plan["execution_policy"]["price_steps"]
    if any(right["after_check_count"] <= left["after_check_count"] for left, right in zip(steps, steps[1:])):
        raise TraderError("PLAN_THRESHOLD_ORDER", "Price escalation steps must increase")


@dataclass(frozen=True)
class ProtectionState:
    plan_id: str
    plan_revision: int
    plan_hash: str
    account_key: str
    position_cycle_id: str
    contract_id: str
    direction: str
    owned_qty: int = 0
    entry_filled_qty: int = 0
    exit_filled_qty: int = 0
    entry_value: Decimal = Decimal(0)
    mode: str = "NORMAL"
    quality: str = "UNKNOWN"
    max_favorable_move: Decimal = Decimal(0)
    protected_event_seq: int | None = None
    last_observed_event_seq: int | None = None
    last_event_seq: int | None = None
    last_price: Decimal | None = None
    state_revision: int = 1
    target_remaining_qty: int | None = None
    exit_reasons: tuple[str, ...] = ()
    block_new: bool = False
    block_new_for_trading_day: bool = False
    reconciliation_required: bool = False
    entry_terminal_reconciled: bool = False
    entry_reconciliation_id: str | None = None
    reconciled_entry_qty: int | None = None
    q0: int | None = None
    q0_reconciliation_id: str | None = None
    phase1_status: str = "NOT_TRIGGERED"
    phase2_status: str = "NOT_TRIGGERED"
    phase1_target: int | None = None
    phase2_target: int | None = None
    phase1_confirmed_reduction_qty: int = 0
    phase2_confirmed_reduction_qty: int = 0
    break_even_status: str = "OFF"
    trailing_activated_event_seq: int | None = None
    trailing_floor_move: Decimal | None = None
    gap_day_stop_pending: bool = False
    # This bounded local history is supplementary. Durable fill/command deduplication
    # remains the owner/ledger's responsibility across its entire retained history.
    seen_events: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        sequence_fields = {"plan_revision", "state_revision", "protected_event_seq", "last_observed_event_seq", "last_event_seq", "trailing_activated_event_seq"}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, Decimal):
                value = _text(value)
            elif value is not None and field.name in sequence_fields:
                value = str(value)
            elif isinstance(value, tuple):
                value = [list(item) if isinstance(item, tuple) else item for item in value]
            result[field.name] = value
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProtectionState":
        allowed = {field.name for field in fields(cls)}
        if set(value) - allowed:
            raise TraderError("E_STATE", "Unknown protection state field")
        data = dict(value)
        for key in ("entry_value", "max_favorable_move", "last_price", "trailing_floor_move"):
            if data.get(key) is not None:
                data[key] = _decimal(data[key], input_value=False)
        for key in ("plan_revision", "state_revision", "protected_event_seq", "last_observed_event_seq", "last_event_seq", "trailing_activated_event_seq"):
            if data.get(key) is not None:
                data[key] = _sequence(data[key])
        if "exit_reasons" in data:
            data["exit_reasons"] = tuple(data["exit_reasons"])
        if "seen_events" in data:
            data["seen_events"] = tuple(tuple(item) for item in data["seen_events"])
        try:
            result = cls(**data)
        except TypeError as error:
            raise TraderError("E_STATE", "Incomplete protection state") from error
        _validate_state(result)
        return result


@dataclass(frozen=True)
class ProtectionDecision:
    state: ProtectionState
    reasons: tuple[str, ...]
    target_remaining_qty: int | None
    cancel_entry_remainders: bool
    block_new: bool


def _validate_state(state: ProtectionState) -> None:
    for key in ("owned_qty", "entry_filled_qty", "exit_filled_qty", "phase1_confirmed_reduction_qty", "phase2_confirmed_reduction_qty"):
        _quantity(getattr(state, key))
    if state.owned_qty != state.entry_filled_qty - state.exit_filled_qty:
        raise TraderError("E_STATE", "Confirmed protection quantities disagree")
    if state.mode not in {"NORMAL", "PROTECTED"} or state.quality not in {"CONTIGUOUS", "DATA_GAP", "UNKNOWN"}:
        raise TraderError("E_STATE", "Invalid protection mode or quality")
    if state.mode == "PROTECTED" and state.protected_event_seq is None:
        raise TraderError("E_STATE", "Protected state lacks latch evidence")
    if state.phase1_status not in _PHASES or state.phase2_status not in _PHASES:
        raise TraderError("E_STATE", "Invalid reduction phase")
    if state.break_even_status not in {"OFF", "WAITING_FILL", "ACTIVE", "SUPPRESSED_PROTECTED"}:
        raise TraderError("E_STATE", "Invalid break-even state")
    if state.max_favorable_move < 0 or not state.max_favorable_move.is_finite():
        raise TraderError("E_STATE", "Invalid favorable high-water mark")
    if state.target_remaining_qty is not None:
        _quantity(state.target_remaining_qty)
    for name in ("plan_revision", "state_revision", "protected_event_seq", "last_observed_event_seq", "last_event_seq", "trailing_activated_event_seq"):
        if getattr(state, name) is not None:
            _sequence(getattr(state, name))
    for name in ("block_new", "block_new_for_trading_day", "reconciliation_required", "entry_terminal_reconciled", "gap_day_stop_pending"):
        if type(getattr(state, name)) is not bool:
            raise TraderError("E_STATE", "Invalid protection boolean")
    if len(state.seen_events) > 256 or any(len(item) != 2 or not isinstance(item[0], str)
            or not isinstance(item[1], str) or not re.fullmatch(r"[0-9a-f]{64}", item[1]) for item in state.seen_events):
        raise TraderError("E_STATE", "Invalid supplementary event history")
    if any(not isinstance(reason, str) for reason in state.exit_reasons):
        raise TraderError("E_STATE", "Invalid exit reason identity")
    if state.q0 is not None:
        _quantity(state.q0, positive=True)
        if state.q0_reconciliation_id is None:
            raise TraderError("E_STATE", "Frozen quantity lacks reconciliation evidence")
    for name in ("phase1_target", "phase2_target"):
        if getattr(state, name) is not None:
            _quantity(getattr(state, name))
            if state.q0 is None or getattr(state, name) > state.q0:
                raise TraderError("E_STATE", "Reduction target lacks its original quantity bound")


def initial_state(plan: Mapping[str, Any],*,simulation_permit=None) -> ProtectionState:
    _validate_plan(plan,simulation_permit)
    partial = plan["partial_reduction"]
    break_even = bool(partial["enabled"] and partial["break_even"]["enabled"])
    return ProtectionState(
        plan_id=plan["plan_id"], plan_revision=int(plan["plan_revision"]), plan_hash=_hash(dict(plan)),
        account_key=plan["account_key"], position_cycle_id=plan["position_cycle_id"],
        contract_id=plan["contract_id"], direction=plan["direction"],
        break_even_status="WAITING_FILL" if break_even else "OFF",
    )


def _bind(plan: Mapping[str, Any], state: ProtectionState,permit=None) -> None:
    if plan.get("execution_scope") != "DEVELOPMENT" and not _permitted(plan,permit):
        raise TraderError("PLAN_APPROVAL_REQUIRED", "Non-development execution is unavailable")
    if (plan.get("plan_id"), int(plan.get("plan_revision", 0)), plan.get("account_key"),
        plan.get("position_cycle_id"), plan.get("contract_id"), plan.get("direction")) != (
        state.plan_id, state.plan_revision, state.account_key, state.position_cycle_id, state.contract_id, state.direction
    ) or _hash(dict(plan)) != state.plan_hash:
        raise TraderError("PLAN_BINDING_MISMATCH", "Plan differs from the bound immutable revision")
    _validate_state(state)
    if state.mode == "PROTECTED" and not plan["profit_protection"]["enabled"]:
        raise TraderError("E_STATE", "Protected mode requires its original protection primitive")


def _target(state: ProtectionState, quantity: int, *reasons: str) -> ProtectionState:
    desired = quantity if state.target_remaining_qty is None else min(state.target_remaining_qty, quantity)
    all_reasons = tuple(dict.fromkeys((*state.exit_reasons, *reasons)))
    return replace(state, target_remaining_qty=desired, exit_reasons=all_reasons, block_new=True)


def _suppress(state: ProtectionState) -> ProtectionState:
    return replace(
        state,
        phase1_status="SUPPRESSED_PROTECTED" if state.phase1_status in _UNCONSUMED else state.phase1_status,
        phase2_status="SUPPRESSED_PROTECTED" if state.phase2_status in _UNCONSUMED else state.phase2_status,
        break_even_status="SUPPRESSED_PROTECTED" if state.break_even_status != "OFF" else "OFF",
    )


def _partial(plan: Mapping[str, Any], state: ProtectionState, seq: int) -> ProtectionState:
    if plan["schema_version"] != "1.0":
        return _dsv_partial(plan, state)
    partial = plan["partial_reduction"]
    if not partial["enabled"] or state.mode != "NORMAL" or state.owned_qty <= 0:
        return state
    profit = _decimal(plan["fixed_take_profit"]["distance_points"])
    stages = [(1, _decimal(partial["first_trigger_ratio"]))]
    if partial["second_stage"]["enabled"]:
        stages.append((2, _decimal(partial["second_stage"]["trigger_ratio"])))
    for number, ratio in stages:
        status_name = f"phase{number}_status"
        status = getattr(state, status_name)
        if status in _UNCONSUMED and state.max_favorable_move >= ratio * profit:
            state = replace(state, **{status_name: "WAITING_ENTRY_TERMINAL"}, block_new=True)
    waiting = any(getattr(state, f"phase{n}_status") == "WAITING_ENTRY_TERMINAL" for n, _ in stages)
    if not waiting or not state.entry_terminal_reconciled or state.reconciliation_required:
        return state
    if state.q0 is None:
        # The entry terminal barrier is invalidated by any subsequent entry fill.
        if state.reconciled_entry_qty != state.owned_qty or not state.entry_reconciliation_id:
            return replace(state, reconciliation_required=True, block_new=True)
        state = replace(state, q0=state.owned_qty, q0_reconciliation_id=state.entry_reconciliation_id)
    for number, _ratio in stages:
        status_name = f"phase{number}_status"
        if getattr(state, status_name) != "WAITING_ENTRY_TERMINAL":
            continue
        divisor = 2 if number == 1 else 4
        target = (state.q0 + divisor - 1) // divisor
        previous = state.q0 if number == 1 else (state.q0 + 1) // 2
        no_op = previous <= target or state.owned_qty <= target
        state = replace(state, **{status_name: "NO_OP" if no_op else "TARGET_COMMITTED", f"phase{number}_target": target})
        state = _target(state, target, f"PARTIAL_STAGE_{number}")
    return state


def _dsv_partial(plan: Mapping[str, Any], state: ProtectionState) -> ProtectionState:
    """One source stage per event, with actual fills separate from the target.

    DSV updates a virtual remaining volume immediately. We retain that quantity
    rule, but the owner must first resolve entry remainders and never releases an
    UNKNOWN exit or counts a send return as a reduction.
    """
    partial = plan["partial_reduction"]
    if not partial["enabled"] or state.mode != "NORMAL" or state.owned_qty < 2:
        return state
    if state.target_remaining_qty == 0:
        return state
    if state.phase1_status in _UNCONSUMED:
        number, ratio = 1, _decimal(partial["first_trigger_ratio"])
    elif state.phase1_status in _CONSUMED and partial["second_stage"]["enabled"] and state.phase2_status in _UNCONSUMED:
        number, ratio = 2, _decimal(partial["second_stage"]["trigger_ratio"], input_value=False)
    else:
        return state
    name = f"phase{number}_status"
    if getattr(state, name) == "NOT_TRIGGERED":
        if state.last_price is None or state.quality != "CONTIGUOUS":
            return state
        sign = Decimal(1) if state.direction == "LONG" else Decimal(-1)
        move = sign * (state.last_price - _decimal(plan["theoretical_entry_price"]))
        take = _decimal(plan["fixed_take_profit"]["distance_points"])
        pullback = (number == 2 and partial["second_stage"].get("pullback_to_first", False)
                    and move <= _decimal(partial["first_trigger_ratio"]) * take)
        if move < ratio * take and not pullback:
            return state
        state = replace(state, **{name: "WAITING_ENTRY_TERMINAL"}, block_new=True)
    if not state.entry_terminal_reconciled or state.reconciliation_required:
        return state
    if state.q0 is None:
        if state.reconciled_entry_qty != state.owned_qty or not state.entry_reconciliation_id:
            return replace(state, reconciliation_required=True, block_new=True)
        state = replace(state, q0=state.owned_qty, q0_reconciliation_id=state.entry_reconciliation_id)
    base = state.owned_qty
    if number == 2 and state.phase1_target is not None:
        base = min(base, state.phase1_target)
    if state.target_remaining_qty is not None:
        base = min(base, state.target_remaining_qty)
    if base < 2:
        # The source gate does not consume a stage on a one-lot remainder.
        return replace(state, **{name: "NOT_TRIGGERED"})
    target = half_close_target(base)
    state = replace(state, **{name: "TARGET_COMMITTED", f"phase{number}_target": target})
    return _target(state, target, f"PARTIAL_STAGE_{number}")


def _quote(plan: Mapping[str, Any], state: ProtectionState, event: Mapping[str, Any], seq: int) -> tuple[ProtectionState, list[str]]:
    if event.get("valid", True) is not True or event.get("fresh", True) is not True or event.get("quality", "CONTIGUOUS") != "CONTIGUOUS":
        return replace(state, quality="DATA_GAP"), ["INVALID_MARKET_DATA"]
    field = "price" if plan["price_field"] == "LAST" else ("bid1" if state.direction == "LONG" else "ask1")
    try:
        price = _decimal(event.get(field))
        if price <= 0:
            raise TraderError("E_DECIMAL", "A market price must be positive")
    except TraderError:
        return replace(state, quality="DATA_GAP"), ["INVALID_MARKET_DATA"]
    state = replace(state, quality="CONTIGUOUS", last_price=price, last_observed_event_seq=seq)
    if not state.owned_qty:
        return state, []
    direction = Decimal(1) if state.direction == "LONG" else Decimal(-1)
    move = direction * (price - _decimal(plan["theoretical_entry_price"]))
    high = max(Decimal(0), state.max_favorable_move, move)
    state = replace(state, max_favorable_move=high)
    profit = _decimal(plan["fixed_take_profit"]["distance_points"])
    stop = _decimal(plan["fixed_stop_loss"]["distance_points"])
    protection = plan["profit_protection"]
    reasons: list[str] = []
    if protection["enabled"] and state.mode == "NORMAL" and high >= _decimal(protection["activation_ratio"]) * profit:
        state = _suppress(replace(state, mode="PROTECTED", protected_event_seq=seq, block_new=True,
                                  block_new_for_trading_day=state.block_new_for_trading_day or protection["block_new_for_trading_day"]))
        reasons.append("PROTECTION_ACTIVATED")
    partial_reasons: list[str] = []
    if plan["schema_version"] != "1.0" and state.mode == "NORMAL":
        # The source runs at most one partial branch before stop/TP/trailing.
        # Multiple consequences still merge into one monotonic owner target.
        before = state.exit_reasons
        state = _dsv_partial(plan, state)
        partial_reasons.extend(reason for reason in state.exit_reasons if reason not in before)
    if move >= profit:
        reasons.append("TAKE_PROFIT")
    if state.mode == "PROTECTED":
        if move <= _decimal(protection["floor_ratio"]) * profit:
            reasons.append("PROTECTED_FLOOR")
    elif move <= -stop:
        reasons.append("STOP_LOSS")
    if state.mode == "NORMAL" and state.break_even_status == "ACTIVE" and move <= 0:
        reasons.append("BREAK_EVEN")
    full_exit = any(reason != "PROTECTION_ACTIVATED" for reason in reasons)
    if not full_exit and state.mode == "NORMAL":
        trailing = plan["trailing_stop"]
        if trailing["enabled"] and high >= _decimal(trailing["activation_ratio"]) * profit:
            value = _decimal(trailing["drawdown_value"])
            if trailing["drawdown_mode"] == "DSV_MAX_TP_AND_PEAK_RATIO":
                floor = trailing_floor(high, profit, value)
            else:
                floor = high - value if trailing["drawdown_mode"] == "POINTS" else high * (1 - value)
            floor = max(floor, state.trailing_floor_move) if state.trailing_floor_move is not None else floor
            state = replace(state, trailing_floor_move=floor, trailing_activated_event_seq=state.trailing_activated_event_seq or seq)
            if move <= floor:
                reasons.append("TRAILING_STOP")
                full_exit = True
                state = replace(state, block_new_for_trading_day=state.block_new_for_trading_day or trailing.get("block_new_for_trading_day", False))
    if full_exit:
        state = _target(state, 0, *[reason for reason in reasons if reason != "PROTECTION_ACTIVATED"])
    elif state.mode == "NORMAL" and plan["schema_version"] == "1.0":
        before = state.exit_reasons
        state = _partial(plan, state, seq)
        reasons.extend(reason for reason in state.exit_reasons if reason not in before)
    reasons.extend(partial_reasons)
    return state, reasons


def _entry_fill(plan: Mapping[str, Any], state: ProtectionState, event: Mapping[str, Any]) -> tuple[ProtectionState, list[str]]:
    quantity = _quantity(event.get("quantity"), positive=True)
    price = _decimal(event.get("price"))
    if state.entry_filled_qty + quantity > plan["initial_open_quantity"]:
        raise TraderError("E_FILL_OVER_TARGET", "Entry fill exceeds the original proven target")
    late = state.entry_terminal_reconciled
    first_confirmation = state.entry_filled_qty == 0
    state = replace(state, owned_qty=state.owned_qty + quantity,
                    entry_filled_qty=state.entry_filled_qty + quantity,
                    entry_value=state.entry_value + price * quantity,
                    entry_terminal_reconciled=False if late else state.entry_terminal_reconciled,
                    reconciliation_required=state.reconciliation_required or late,
                    quality="DATA_GAP" if state.last_price is None or late else state.quality)
    reasons = ["LATE_ENTRY_AFTER_BARRIER"] if late else []
    slip = plan["slippage_exit"]
    confirmation = event.get("initial_entry_confirmation")
    if not slip["enabled"] or (plan["schema_version"] == "1.3" and
                               (event.get("skip_entry_slippage") is True or
                                (not first_confirmation and confirmation is None))):
        return state, reasons
    direction = Decimal(1) if state.direction == "LONG" else Decimal(-1)
    confirmed_qty, confirmed_value = state.entry_filled_qty, state.entry_value
    if plan["schema_version"] == "1.3" and confirmation is not None:
        confirmed_qty = _quantity(confirmation["quantity"], positive=True)
        confirmed_value = _decimal(confirmation["value"], input_value=False)
        if (not state.entry_filled_qty <= confirmed_qty <= plan["initial_open_quantity"]
                or confirmed_value < state.entry_value):
            raise TraderError("E_FACT_BARRIER", "The first broker entry confirmation does not cover its fill")
    # Compare weighted values without rounding a repeated average through the threshold.
    adverse_value = direction * (confirmed_value - _decimal(plan["theoretical_entry_price"]) * confirmed_qty)
    if adverse_value > _decimal(slip["adverse_threshold_points"]) * confirmed_qty:
        if state.mode == "NORMAL" or event.get("allow_protected_override") is True:
            state = _target(state, 0, "ADVERSE_SLIPPAGE")
            state = replace(state, block_new_for_trading_day=state.block_new_for_trading_day or slip["block_new_for_trading_day"])
            reasons.append("ADVERSE_SLIPPAGE")
    return state, reasons


def _exit_fill(plan: Mapping[str, Any], state: ProtectionState, event: Mapping[str, Any]) -> ProtectionState:
    quantity = _quantity(event.get("quantity"), positive=True)
    if quantity > state.owned_qty:
        raise TraderError("E_OVER_CLOSE", "Exit fill exceeds proven owned quantity")
    left = quantity
    first = state.phase1_confirmed_reduction_qty
    second = state.phase2_confirmed_reduction_qty
    p1, p2 = state.phase1_status, state.phase2_status
    if state.q0 is not None and event.get("external", False) is not True:
        if plan["schema_version"] != "1.0":
            stage1_total = state.q0 - state.phase1_target if state.phase1_target is not None else 0
            stage2_total = state.phase1_target - state.phase2_target if state.phase1_target is not None and state.phase2_target is not None else 0
        else:
            stage1_total = state.q0 - (state.q0 + 1) // 2
            stage2_total = (state.q0 + 1) // 2 - (state.q0 + 3) // 4
        if p1 in _CONSUMED and p1 != "NO_OP":
            allocated = min(left, max(0, stage1_total - first)); first += allocated; left -= allocated
            p1 = "DONE" if first >= stage1_total else "EXECUTING"
        if p2 in _CONSUMED and p2 != "NO_OP":
            allocated = min(left, max(0, stage2_total - second)); second += allocated; left -= allocated
            p2 = "DONE" if second >= stage2_total else "EXECUTING"
    break_even = state.break_even_status
    if first > 0 and break_even == "WAITING_FILL" and state.mode == "NORMAL":
        break_even = "ACTIVE"
    remaining = state.owned_qty - quantity
    return replace(state, owned_qty=remaining, exit_filled_qty=state.exit_filled_qty + quantity,
                   phase1_confirmed_reduction_qty=first, phase2_confirmed_reduction_qty=second,
                   phase1_status=p1, phase2_status=p2, break_even_status=break_even,
                   block_new_for_trading_day=state.block_new_for_trading_day or (remaining == 0 and state.gap_day_stop_pending))


def _decision(state: ProtectionState, reasons: list[str] | tuple[str, ...], cancel: bool = False) -> ProtectionDecision:
    blocked = state.block_new or state.reconciliation_required or (state.owned_qty > 0 and state.quality != "CONTIGUOUS")
    cancel = cancel or state.target_remaining_qty is not None or state.mode == "PROTECTED" or any(
        phase == "WAITING_ENTRY_TERMINAL" for phase in (state.phase1_status, state.phase2_status))
    return ProtectionDecision(state, tuple(dict.fromkeys(reasons)), state.target_remaining_qty, cancel, blocked)


def reduce_event(plan: Mapping[str, Any], state: ProtectionState, event: Mapping[str, Any],*,simulation_permit=None) -> ProtectionDecision:
    """Return a new state and target only; the caller owns durable effects and OMS.

    Event metadata may include event_id, event_seq, account_key, position_cycle_id
    and contract_id. Supplied bindings must match. All event facts must originate
    from the owner's validated internal entry, never an external pipe payload.
    """
    _bind(plan, state,simulation_permit)
    if not isinstance(event, Mapping):
        raise TraderError("E_EVENT", "Expected a protection event")
    for key in ("account_key", "position_cycle_id", "contract_id"):
        if key in event and event[key] != getattr(state, key):
            raise TraderError("PLAN_BINDING_MISMATCH", "Event is outside the original protection scope")
    seq = _sequence(event.get("event_seq", (state.last_event_seq or 0) + 1))
    event_id = event.get("event_id")
    event_hash: str | None = None
    if event_id is not None:
        if not isinstance(event_id, str) or not event_id:
            raise TraderError("E_EVENT", "Invalid event identity")
        event_hash = _hash(dict(event))
        previous = dict(state.seen_events).get(event_id)
        if previous is not None:
            if previous != event_hash:
                raise TraderError("E_DUPLICATE_CONFLICT", "Event identity has conflicting content")
            return _decision(state, ())
    if state.last_event_seq is not None and seq < state.last_event_seq:
        raise TraderError("E_FACT_BARRIER", "Regressive business sequence")
    reasons: list[str] = []
    kind = event.get("type")
    with localcontext() as context:
        context.prec = 80
        if kind in {"FILL", "FILL_CONFIRMED", "LATE_ENTRY_FILL"}:
            state, reasons = _entry_fill(plan, state, event)
            if "current_price" in event:
                field = "price" if plan["price_field"] == "LAST" else ("bid1" if state.direction == "LONG" else "ask1")
                quote = {field: event["current_price"], "valid": event.get("quote_valid", False)}
                state, quote_reasons = _quote(plan, state, quote, seq)
                reasons.extend(quote_reasons)
        elif kind == "EXIT_FILL":
            state = _exit_fill(plan, state, event)
            # A newly confirmed reduction can activate BE while an already
            # observed price is at/beyond its line. Re-evaluate only a fresh
            # owner-provided quote; this must not consume another partial stage.
            if plan["schema_version"] != "1.0" and state.break_even_status == "ACTIVE" and "current_price" in event and event.get("quote_valid") is True:
                sign = Decimal(1) if state.direction == "LONG" else Decimal(-1)
                if sign * (_decimal(event["current_price"]) - _decimal(plan["theoretical_entry_price"])) <= 0 and state.owned_qty:
                    state = _target(state, 0, "BREAK_EVEN")
                    reasons.append("BREAK_EVEN")
        elif kind == "EXTERNAL_ALLOCATION_REDUCTION":
            state=_exit_fill(plan,state,dict(event)|{'external':True})
            target=state.target_remaining_qty
            state=replace(state,target_remaining_qty=None if target is None else min(target,state.owned_qty),
                          reconciliation_required=True,block_new=True)
            reasons.append("EXTERNAL_ALLOCATION_REDUCTION")
        elif kind == "QUOTE":
            state, reasons = _quote(plan, state, event, seq)
        elif kind == "ENTRY_TERMINAL_RECONCILED":
            owned = _quantity(event.get("owned_quantity"))
            if owned != state.owned_qty:
                raise TraderError("E_FACT_BARRIER", "Reconciliation quantity differs from owned quantity")
            identity = event.get("reconciliation_id", f"development-barrier-{seq}")
            if not isinstance(identity, str) or not identity:
                raise TraderError("E_FACT_BARRIER", "Reconciliation identity is missing")
            state = replace(state, entry_terminal_reconciled=True, entry_reconciliation_id=identity,
                            reconciled_entry_qty=owned, reconciliation_required=False)
            # A latched phase survives pullback while waiting for terminal evidence.
            if state.mode == "NORMAL" and state.owned_qty and (plan["schema_version"] == "1.0" or
                    "WAITING_ENTRY_TERMINAL" in (state.phase1_status, state.phase2_status)):
                before = state.exit_reasons
                state = _partial(plan, state, seq)
                reasons.extend(reason for reason in state.exit_reasons if reason not in before)
        elif kind in {"EXIT_ORDER_SENT", "EXIT_SUBMITTED"}:
            state = replace(state,
                            phase1_status="EXECUTING" if state.phase1_status == "TARGET_COMMITTED" else state.phase1_status,
                            phase2_status="EXECUTING" if state.phase2_status == "TARGET_COMMITTED" else state.phase2_status)
        elif kind == "SESSION_CLOSE_DUE":
            policy = plan["close_session"]
            if policy["enabled"] and state.owned_qty:
                if event.get("schedule_policy_id", policy["schedule_policy_id"]) != policy["schedule_policy_id"]:
                    raise TraderError("PLAN_POLICY_UNRESOLVED", "Schedule policy mismatch")
                state = _target(state, 0, "SESSION_CLOSE"); reasons.append("SESSION_CLOSE")
        elif kind == "DATA_GAP":
            if event.get("active_trading") is True:
                state = replace(state, quality="DATA_GAP")
                policy = plan["data_gap_exit"]
                gap = _quantity(event.get("gap_ms", 0))
                if plan["schema_version"] != "1.0":
                    elapsed = _quantity(event.get("session_elapsed_ms", 0))
                    if gap > elapsed:
                        raise TraderError("E_FACT_BARRIER", "Active gap cannot exceed the proven session duration")
                    session_ready = elapsed >= 60_001
                else:
                    session_ready = True
                reasons.append("DATA_GAP")
                if policy["enabled"] and session_ready and gap >= policy["active_trading_gap_ms"] and state.owned_qty:
                    if event.get("policy_authorized") is not True or event.get("policy_id") != policy["data_quality_policy_id"]:
                        raise TraderError("PLAN_APPROVAL_REQUIRED", "Gap exit policy is not authorized")
                    if event.get("legal_price_available") is not True:
                        state = replace(state, reconciliation_required=True, block_new=True)
                        reasons.append("ALERT_AND_TAKEOVER")
                    elif plan["schema_version"] != "1.0" or state.mode != "PROTECTED" or event.get("allow_protected_override") is True:
                        state = _target(state, 0, "DATA_GAP_EXIT"); reasons.append("DATA_GAP_EXIT")
                        state = replace(state, gap_day_stop_pending=state.gap_day_stop_pending or policy.get("block_new_for_trading_day", False))
        elif kind == "EMERGENCY_EXIT":
            policy = plan["emergency_exit"]
            if not policy["enabled"] or event.get("policy_authorized") is not True or event.get("policy_id") != policy.get("approved_policy_id"):
                raise TraderError("PLAN_APPROVAL_REQUIRED", "Emergency policy is not authorized")
            quantity = _quantity(event.get("quantity"), positive=True)
            if quantity > policy["max_reduce_quantity"] or quantity > state.owned_qty:
                raise TraderError("E_QUANTITY", "Emergency request exceeds its approved range")
            if state.mode == "PROTECTED" and event.get("allow_protected_override") is not True:
                raise TraderError("PLAN_APPROVAL_REQUIRED", "Emergency policy cannot override this protection mode")
            state = _target(state, state.owned_qty - quantity, "EMERGENCY_EXIT"); reasons.append("EMERGENCY_EXIT")
        elif kind == "RESTART_KERNEL_WITH_GAP":
            state = replace(state, reconciliation_required=True, quality="DATA_GAP", block_new=True,
                            entry_terminal_reconciled=False)
            reasons.append("RECONCILIATION_REQUIRED")
        elif kind in {"COMMIT_ACK", "KILL_STRATEGY", "RESTART_GUI", "CANCEL_ENTRY_REQUEST", "KILL_KERNEL_BEFORE_COMMIT_ACK", "CANCEL_UNKNOWN"}:
            # Lifetime/cancellation does not manufacture a fill or clear protection.
            pass
        else:
            raise TraderError("E_UNSUPPORTED", "Unsupported protection event")
    seen = state.seen_events
    if event_id is not None:
        seen = (*seen[-255:], (event_id, event_hash))
    state = replace(state, state_revision=state.state_revision + 1, seen_events=seen, last_event_seq=seq)
    _validate_state(state)
    return _decision(state, reasons)


reduce = reduce_event


def available_exit_quantity(owned_qty: int, target_remaining_qty: int | None,
                            potential_exit_qty: int = 0, unknown_exit_qty: int = 0,
                            available_qty: int | None = None) -> int:
    """Quantity guard; known-pending and UNKNOWN buckets must not overlap."""
    for value in (owned_qty, potential_exit_qty, unknown_exit_qty):
        _quantity(value)
    if target_remaining_qty is None:
        return 0
    _quantity(target_remaining_qty)
    quantity = max(0, owned_qty - target_remaining_qty - potential_exit_qty - unknown_exit_qty)
    return quantity if available_qty is None else min(quantity, _quantity(available_qty))


def revise_plan(old_plan: Mapping[str, Any], new_plan: Mapping[str, Any], state: ProtectionState, *,
                expected_plan_revision: int, expected_state_revision: int,
                approval_record_id: str | None, policy_authorized: bool = False) -> ProtectionState:
    """Restricted development U4 migration; a caller still persists it atomically."""
    _bind(old_plan, state)
    _validate_plan(new_plan)
    if not policy_authorized or not approval_record_id:
        raise TraderError("PLAN_APPROVAL_REQUIRED", "A verified migration authorization is required")
    if expected_plan_revision != state.plan_revision or expected_state_revision != state.state_revision:
        raise TraderError("PLAN_REVISION_CONFLICT", "Migration revision conflict")
    bindings = ("plan_id", "account_key", "position_cycle_id", "contract_id", "trading_day", "direction", "theoretical_entry_price", "initial_open_quantity")
    if any(old_plan[key] != new_plan[key] for key in bindings) or int(new_plan["plan_revision"]) != state.plan_revision + 1:
        raise TraderError("PLAN_MIGRATION_UNSAFE", "Migration changes frozen identity or theory")
    with localcontext() as context:
        context.prec = 80
        if state.mode == "PROTECTED":
            old = old_plan["profit_protection"]; new = new_plan["profit_protection"]
            old_floor = _decimal(old["floor_ratio"]) * _decimal(old_plan["fixed_take_profit"]["distance_points"])
            if not new["enabled"] or _decimal(new["floor_ratio"]) * _decimal(new_plan["fixed_take_profit"]["distance_points"]) < old_floor:
                raise TraderError("PLAN_MIGRATION_UNSAFE", "Migration lowers a latched protected floor")
        elif _decimal(new_plan["fixed_stop_loss"]["distance_points"]) > _decimal(old_plan["fixed_stop_loss"]["distance_points"]):
            raise TraderError("PLAN_MIGRATION_UNSAFE", "Restricted migration cannot loosen the existing stop")
        if (state.phase1_status in _CONSUMED or state.phase2_status in _CONSUMED) and old_plan["partial_reduction"] != new_plan["partial_reduction"]:
            raise TraderError("PLAN_MIGRATION_UNSAFE", "Consumed reduction stages cannot be redefined")
        if state.trailing_activated_event_seq is not None and old_plan["trailing_stop"] != new_plan["trailing_stop"]:
            raise TraderError("PLAN_MIGRATION_UNSAFE", "Active trailing protection cannot be redefined")
        # Reassessing changed live thresholds needs a fresh fact barrier supplied by
        # the owner. This API has none, so G1 permits revision-only rebinding while
        # exposure exists and refuses a purported hot repair using stale last_price.
        mutable_metadata = {"plan_revision", "config_revision", "config_hash", "rule_set_id", "approval_record_id"}
        if state.owned_qty and any(old_plan[key] != new_plan[key] for key in old_plan if key not in mutable_metadata):
            raise TraderError("PLAN_MIGRATION_UNSAFE", "Changed live primitives require a fresh migration fact barrier")
    return replace(state, plan_revision=int(new_plan["plan_revision"]), plan_hash=_hash(dict(new_plan)), state_revision=state.state_revision + 1)
