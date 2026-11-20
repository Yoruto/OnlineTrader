"""Compile source-bound DSV protection into the versioned offline contract.

This compiler has no transport, broker, clock or QA dependency.  Policy identifiers
are references for the development owner to resolve against explicit market facts;
they do not assert calendar completeness or authorize an order.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_FLOOR, localcontext
import re

from online_trader.configuration.baseline import load_reference_approval
from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.contracts.validation import validate_plan
from online_trader.domain.values import decimal_text, decimal_value, identifier, quantity as check_quantity, sequence
from online_trader.errors import TraderError


def _unsupported(message):
    raise TraderError("E_UNSUPPORTED", message)


def capability_notes(profile: DsvProfile, *, execution_version="1.1") -> list[str]:
    """Report deliberate compatibility limits outside the strict plan schema."""
    if not isinstance(profile, DsvProfile):
        raise TraderError("E_CONFIG", "Expected an immutable DSV profile")
    if execution_version not in ("1.1", "1.2"):
        _unsupported("Unsupported DSV development execution version")
    trade = profile.to_record()["settings"]["trade"]
    notes = [
        "DEVELOPMENT_ONLY_REFERENCE_APPROVAL_DOES_NOT_AUTHORIZE_TRADING",
        "DSV_FIRST_MINUTE_UNDEFINED_CLOCK_REQUIRES_EXPLICIT_SESSION_CLOCK_REPAIR",
        "DSV_PLAN_1_1_KEEPS_G0_PLAN_1_0_SEMANTICS_UNCHANGED",
        "DSV_PARTIAL_REDUCTION_USES_ROUND_HALF_CURRENT_AFTER_ENTRY_TERMINAL_RECONCILIATION",
    ]
    if execution_version == "1.1":
        notes.extend(["OPPONENT_FIRST_REQUEST_ONLY_NO_DSV_CANCEL_REPRICE_ALGORITHM",
                      "CANCEL_CHECK_5000MS_IS_DEVELOPMENT_POLICY"])
    else:
        notes.extend(["DSV_PLAN_1_2_EXTENDS_EXECUTION_WITHOUT_REWRITING_1_0_OR_1_1",
                      "DSV_PRICE_WORSENING_CANCEL_REQUIRES_TERMINAL_AND_FILL_BARRIER_BEFORE_REPLACE",
                      "CANCEL_CHECK_3000MS_IS_CLOSE_TARGET_REFRESH_NOT_SDK_CANCEL_INTERVAL",
                      "DSV_PASSIVE_WAIT_3000MS_IS_OBSERVED_HIDDEN_SOURCE_DEFAULT",
                      "DSV_MANUAL_CLOSE_PRICE_MODE_DOES_NOT_OVERRIDE_STRATEGY_TASK_PRICE",
                      "DSV_PENDING_ENTRY_TTL_DOES_NOT_RELEASE_UNKNOWN_ORDER_QUANTITY"])
    if trade["protect_80_enabled"]:
        notes.append("NORMAL_TRAILING_SUPPRESSED_BY_PROFIT_PROTECTION")
    if trade["half_breakeven"]:
        notes.append("DSV_BREAKEVEN_UNDEFINED_GLOBAL_REPAIRED_WITH_SAVED_SWITCH_AND_CONFIRMED_REDUCTION")
    if trade["stop_day_on_trailing"]:
        notes.append("DSV_TRAILING_DAYSTOP_SHADOWED_BRANCH_REPAIRED_WITH_SAVED_SWITCH")
    if not trade["half_close_enabled"] and (trade["half_stage2"] or trade["half_breakeven"]):
        notes.append("HALF_SUBSWITCHES_DORMANT_WITH_HALF_DISABLED")
    if profile.content_hash != load_reference_approval()["profile_hash"]:
        notes.append("DEVELOPMENT_VARIANT_IS_NOT_THE_APPROVED_SAVED_CONFIGURATION")
    return notes


def build_development_plan(profile, candidate, *, account_key, trading_day,
                           config_revision: str, config_hash, rule_set_id,
                           quantity: int, plan_id, position_cycle_id, execution_version="1.1"):
    """Bind an entry candidate to exact source distances and a DEVELOPMENT plan.

    The default preserves the existing 1.1 first-request execution contract.
    Explicit 1.2 adds the source-bound DSV cancel/reprice policy, while keeping
    1.1 protection primitives and existing immutable plans unchanged.
    """
    if not isinstance(profile, DsvProfile) or type(candidate) is not dict:
        raise TraderError("E_CONFIG", "Expected a DSV profile and entry candidate")
    if execution_version not in ("1.1", "1.2"):
        _unsupported("Unsupported DSV development execution version")
    canonical_bytes(candidate)
    for value in (account_key, rule_set_id, plan_id, position_cycle_id):
        identifier(value)
    sequence(config_revision, positive=True)
    check_quantity(quantity, positive=True)
    if type(config_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", config_hash):
        raise TraderError("E_CONFIG", "Expected a canonical configuration hash")
    try:
        if type(trading_day) is not str or date.fromisoformat(trading_day).isoformat() != trading_day:
            raise ValueError
    except ValueError as error:
        raise TraderError("E_SCOPE", "Expected an exact trading date") from error
    if (candidate.get("account_key") != account_key or candidate.get("trading_day") != trading_day
            or candidate.get("profile_hash") != profile.content_hash):
        raise TraderError("E_SCOPE", "Entry candidate scope or profile differs")
    if candidate.get("execution_scope", "DEVELOPMENT") != "DEVELOPMENT":
        raise TraderError("E_SCOPE", "Only isolated development candidates are supported")
    contract = identifier(candidate.get("contract_id"))
    if candidate.get("direction") not in ("LONG", "SHORT"):
        raise TraderError("E_SCOPE", "Expected an explicit entry direction")
    # The source contract is exact; a code_new hint is not an activated rollover.
    settings = profile.to_record()["settings"]
    symbols = [row for row in settings["symbols"] if row["code"] == contract]
    if len(symbols) != 1:
        raise TraderError("E_SCOPE", "Contract is absent from the selected DSV profile")
    symbol, trade = symbols[0], settings["trade"]
    for key, source_key in (("open_offset", "open_offset"), ("stop_distance", "stop_offset"),
                            ("take_profit_distance", "profit_offset")):
        source = decimal_value(symbol[source_key], positive=True)
        if decimal_value(candidate.get(key), positive=True) != source:
            raise TraderError("E_CONFIG", "Candidate distances differ from the source profile")
    theoretical = candidate.get("theoretical_price")
    decimal_value(theoretical, positive=True)
    decimal_value(candidate.get("limit_price"), positive=True)
    if execution_version == "1.1" and (trade["passive_entry_first"] or trade["close_price_mode"] != "对手价"):
        _unsupported("This development policy supports only a legal opponent first request")
    if trade["close_before_seconds"] > 86400:
        _unsupported("Close advance exceeds the frozen plan contract")

    identity = {"schema_version": execution_version, "profile_hash": profile.content_hash, "contract_id": contract,
                "trading_day": trading_day, "config_revision": config_revision,
                "config_hash": config_hash, "rule_set_id": rule_set_id}

    def reference(kind):
        return "dsv-dev-" + kind + "-" + content_hash({"kind": kind, **identity})[:40]

    gap_seconds = decimal_value(trade["stale_force_close_seconds"])
    with localcontext() as context:
        context.prec = 80
        # The source is strict `quiet_seconds > threshold`.  G0 compares integer
        # milliseconds with >=, so its first eligible value is floor(ms) + 1.
        gap_ms = int((gap_seconds * 1000).to_integral_value(rounding=ROUND_FLOOR)) + 1 if gap_seconds else 0
        # DSV's numeric initialization uses `value or 0.4` for this ratio.
        slip_ratio = decimal_value(trade["entry_slip_abort_ratio"]) or Decimal("0.4")
        slippage_threshold = max(Decimal(2), decimal_value(symbol["stop_offset"]) * slip_ratio)
        # DSV only falls back for missing/None at the source row; a present zero
        # is not the global ratio. Validated profiles always contain the row key.
        first_ratio = decimal_value(symbol["half_close_ratio"])
        first_enabled = trade["half_close_enabled"] and 0 < first_ratio < 1
        second_ratio = min(Decimal("0.95"), max(Decimal("0.05"), decimal_value(trade["half_stage2_trigger_ratio"])))
        if second_ratio <= first_ratio:
            second_ratio = first_ratio * Decimal("1.5")
        activation_ratio = decimal_value(trade["protect_80_ratio"]) or Decimal("0.8")
        floor_ratio = decimal_value(trade["protect_keep_ratio"]) or Decimal("0.5")
        trailing_activation = decimal_value(trade["trailing_activate_ratio"])
    if gap_ms > 2_147_483_647:
        _unsupported("Data gap duration exceeds the frozen millisecond contract")
    approval = load_reference_approval()
    partial = {"enabled": False}
    if first_enabled:
        partial = {"enabled": True, "first_trigger_ratio": decimal_text(first_ratio),
            "quantity_rule": "DSV_ROUND_HALF_CURRENT", "q0_freeze_policy": "ENTRY_TERMINAL_AND_RECONCILED",
            "second_stage": ({"enabled": True, "trigger_ratio": decimal_text(second_ratio)}
                             if trade["half_stage2"] else {"enabled": False}),
            "break_even": ({"enabled": True, "anchor": "THEORETICAL_ENTRY",
                            "activate_after": "FIRST_STAGE_CONFIRMED_REDUCTION"}
                           if trade["half_breakeven"] else {"enabled": False})}
        if trade["half_stage2"] and trade.get("half_pullback_close", False):
            partial["second_stage"]["pullback_to_first"] = True
    plan = {
        "schema_version": execution_version, "plan_id": plan_id, "plan_revision": "1",
        "account_key": account_key, "position_cycle_id": position_cycle_id,
        "contract_id": contract, "trading_day": trading_day, "direction": candidate["direction"],
        "theoretical_entry_price": theoretical, "initial_open_quantity": quantity,
        "rule_set_id": rule_set_id, "config_revision": config_revision, "config_hash": config_hash,
        "execution_scope": "DEVELOPMENT", "approval_record_id": approval["record_id"] if profile.content_hash == approval["profile_hash"] else None,
        "market_quality_policy_id": reference("market"), "price_field": "LAST",
        "fixed_stop_loss": {"enabled": True, "distance_points": symbol["stop_offset"]},
        "fixed_take_profit": {"enabled": True, "distance_points": symbol["profit_offset"]},
        "profit_protection": ({"enabled": True, "activation_ratio": decimal_text(activation_ratio),
                              "floor_ratio": decimal_text(floor_ratio), "block_new_for_trading_day": trade["protect_stop_day"]}
                              if trade["protect_80_enabled"] else {"enabled": False}),
        "trailing_stop": ({"enabled": True, "activation_ratio": decimal_text(trailing_activation),
            "drawdown_mode": "DSV_MAX_TP_AND_PEAK_RATIO", "drawdown_value": trade["trailing_pullback_ratio"],
            "block_new_for_trading_day": trade["stop_day_on_trailing"]} if trailing_activation > 0 else {"enabled": False}),
        "partial_reduction": partial,
        "close_session": {"enabled": True, "schedule_policy_id": reference("schedule"),
                          "calendar_version": reference("calendar"), "advance_seconds": trade["close_before_seconds"]},
        "slippage_exit": ({"enabled": True, "adverse_threshold_points": decimal_text(slippage_threshold),
                           "block_new_for_trading_day": trade["entry_slip_abort_stop_day"]}
                          if trade["entry_slip_abort"] else {"enabled": False}),
        "data_gap_exit": ({"enabled": True, "data_quality_policy_id": reference("gap"),
                           "active_trading_gap_ms": int(gap_ms), "missing_legal_price_action": "ALERT_AND_TAKEOVER",
                           "block_new_for_trading_day": trade["stale_close_day_stop"]}
                          if gap_ms else {"enabled": False}),
        "emergency_exit": {"enabled": False},
        "execution_policy": {"policy_id": reference("first-request"), "first_price_mode": "OPPONENT_LIMIT",
                             "cancel_check_interval_ms": 5000, "max_child_order_attempts": 1,
                             "price_steps": [], "close_offset_policy_id": reference("close-offset")},
    }
    if execution_version == "1.2":
        with localcontext() as context:
            context.prec = 80
            chase_window_ms = int((decimal_value(trade["chase_window_minutes"]) * 60_000).to_integral_value(rounding=ROUND_FLOOR))
        timeout_ms = trade["entry_confirm_timeout"] * 1000
        passive_wait_ms = int(decimal_value(trade.get("passive_wait_seconds", "3")) * 1000)
        if max(chase_window_ms, timeout_ms, passive_wait_ms) > 2_147_483_647:
            _unsupported("Execution duration exceeds the bounded millisecond contract")
        plan["execution_policy"] = {
            "policy_id": reference("execution"),
            "first_price_mode": "PASSIVE_LIMIT" if trade["passive_entry_first"] else "OPPONENT_LIMIT",
            # The source SDK reprices on update notifications, not this timer.
            "cancel_check_interval_ms": 3000, "max_child_order_attempts": None,
            "price_steps": [], "close_offset_policy_id": reference("close-offset"),
            "algorithm": "DSV_TARGET_POSITION", "entry_confirm_timeout_ms": timeout_ms,
            "passive_entry_first": trade["passive_entry_first"], "passive_wait_ms": passive_wait_ms,
            "close_retry_ms": 3000, "extra_tick_remaining_ms": 10_000,
            "exchange_limit_remaining_ms": 5000, "force_retry_ms": 5000,
            "max_consecutive_exit_rejections": 3,
            "passive_ttl_ms": 90_000, "aggr_ttl_ms": 60_000,
            "market_ttl_ms": 120_000, "cap_ttl_ms": 90_000,
            "entry_slip_ratio": trade["entry_slip_ratio"], "chase_slip_ratio": trade["chase_slip_ratio"],
            "chase_window_ms": chase_window_ms, "open_offset": symbol["open_offset"],
        }
    validate_plan(plan)
    return plan


def build_simulation_plan(profile,candidate,*,grant,_active_profile_hash=None,**values):
    """Reuse the DSV compiler, then bind a distinct ONLINE_SIMULATION version."""
    from online_trader.online_scope import DsvSimulationGrant
    if type(grant) is not DsvSimulationGrant or profile.content_hash!=(_active_profile_hash or grant.value['profile_hash']):
        raise TraderError('E_ONLINE_SCOPE','Strategy plan must match its independently scoped profile')
    values.pop('execution_version',None)
    plan=build_development_plan(profile,candidate,execution_version='1.2',**values)
    plan.update(schema_version='1.3',execution_scope='ONLINE_SIMULATION',approval_record_id=grant.value['grant_id'],
                online_binding={'grant_hash':grant.content_hash,'profile_hash':grant.value['profile_hash'],'calendar_hash':grant.value['calendar_hash']})
    from online_trader.protection.reducer import DsvSimulationPlanPermit
    DsvSimulationPlanPermit.issue(plan,grant);validate_plan(plan)
    return plan
