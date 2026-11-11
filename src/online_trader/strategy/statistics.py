"""Pure, source-bound DSV point statistics from explicit confirmed facts.

Point statistics never stand for realized currency P&L. The owner authenticates
entry/flat evidence, commits these results and keeps durable full-history receipt
identities. Neither an order target nor a send return is a confirmed fact here.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, localcontext
import re

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.values import MAX_QUANTITY, decimal_text, decimal_value, identifier, quantity
from online_trader.errors import TraderError


SCHEMA_VERSION = "DSV_STATISTICS/1"
MAX_RECEIPTS = 4096
_DIRECTIONS = {"LONG", "SHORT"}
_KINDS = {"ENTRY_CONFIRMED", "CYCLE_CLOSED"}
_NORMAL_REASONS = {"STOP_LOSS", "BREAK_EVEN", "TAKE_PROFIT", "TRAILING_STOP", "PROTECTED_FLOOR"}
_CLOSE_REASONS = _NORMAL_REASONS | {"SESSION_CLOSE", "ADVERSE_SLIPPAGE", "DATA_GAP_EXIT", "MANUAL_EXIT", "NO_FILL"}
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")
_COUNT_FIELDS = ("stop_count", "profit_count", "trade_count", "total_volume", "stop_streak")
_COMMON = {"type", "event_id", "evidence_id", "account_key", "contract_id", "trading_day",
    "position_cycle_id", "plan_id", "plan_hash", "profile_hash", "direction", "entry_kind",
    "theoretical_entry_price", "stop_distance", "take_profit_distance"}


def _invalid(message):
    raise TraderError("E_STATISTICS_INPUT", message)


def _day(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            _invalid("Statistics require an explicit YYYY-MM-DD cabinet-day label")
    except ValueError:
        _invalid("Invalid cabinet-day label")
    return value


def _utc(value):
    if type(value) is not str or not _UTC.fullmatch(value):_invalid("Expected an explicit UTC timestamp")
    try:return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:_invalid("Invalid UTC timestamp")


def _decimal(value, *, positive=False):
    try:return decimal_value(value, positive=positive)
    except TraderError:_invalid("Expected a bounded canonical point value")


def _id(value):
    try:return identifier(value)
    except TraderError:_invalid("Invalid statistics evidence or scope identifier")


def _hash(value):
    if type(value) is not str or not re.fullmatch("[0-9a-f]{64}", value):_invalid("Expected a canonical content hash")
    return value


def initial_state(account_key, contract_id, trading_day):
    """Create an empty aggregate; this does not approve an account or symbol."""
    return {"schema_version": SCHEMA_VERSION, "account_key": _id(account_key),
        "contract_id": _id(contract_id), "trading_day": _day(trading_day),
        "daily_pnl_points": "0", "net_loss_points": "0", "stop_count": 0,
        "profit_count": 0, "trade_count": 0, "total_volume": 0, "stop_streak": 0,
        "continuation_stopped": False, "day_stopped": False, "day_stopped_reason": None,
        "cooldown_until": {"LONG": None, "SHORT": None}, "cooldown_direction": None,
        "cooldown_started_at_utc": None, "cooldown_duration_ms": "0",
        "receipts": {}}


def _state(value):
    if type(value) is not dict: _invalid("Expected a statistics state record")
    canonical_bytes(value)
    expected = set(initial_state(value.get("account_key"), value.get("contract_id"), value.get("trading_day")))
    if set(value) != expected or value.get("schema_version") != SCHEMA_VERSION:
        _invalid("Unsupported statistics state")
    _decimal(value["daily_pnl_points"]); _decimal(value["net_loss_points"])
    for key in _COUNT_FIELDS:
        try:quantity(value[key])
        except TraderError:_invalid("Invalid statistics count")
    for key in ("continuation_stopped", "day_stopped"):
        if type(value[key]) is not bool:_invalid("Invalid statistics stop state")
    if value["day_stopped_reason"] is not None and (type(value["day_stopped_reason"]) is not str or
            len(value["day_stopped_reason"])>256):_invalid("Invalid day-stop reason")
    if type(value["cooldown_until"]) is not dict or set(value["cooldown_until"]) != _DIRECTIONS:
        _invalid("Invalid directional cooldown state")
    for until in value["cooldown_until"].values():
        if until is not None:_utc(until)
    active=[direction for direction, until in value["cooldown_until"].items() if until is not None]
    if len(active)>1 or value["cooldown_direction"] not in _DIRECTIONS|{None}:
        _invalid("Source cooldown has only one active direction")
    duration=_decimal(value["cooldown_duration_ms"])
    if duration<0 or duration>MAX_QUANTITY:_invalid("Invalid cooldown duration")
    if active:
        if value["cooldown_direction"]!=active[0] or value["cooldown_started_at_utc"] is None or duration<=0:
            _invalid("Cooldown lacks its original trigger and duration")
        _utc(value["cooldown_started_at_utc"])
    elif value["cooldown_direction"] is not None or value["cooldown_started_at_utc"] is not None or duration!=0:
        _invalid("Empty cooldown has inconsistent origin fields")
    if type(value["receipts"]) is not dict or len(value["receipts"]) > MAX_RECEIPTS:
        _invalid("Statistics receipt capacity is invalid")
    for key, row in value["receipts"].items():
        if type(key) is not str or type(row) is not dict or set(row) != {"event_hash", "receipt"}:
            _invalid("Invalid statistics receipt")
        _hash(row["event_hash"])
        if type(row["receipt"]) is not dict:_invalid("Invalid statistics receipt payload")


def _event(profile, state, event):
    if not isinstance(profile, DsvProfile) or type(event) is not dict:
        _invalid("Expected an immutable profile and explicit confirmed statistics event")
    canonical_bytes(event)
    if not _COMMON <= event.keys() or event["type"] not in _KINDS:_invalid("Incomplete statistics event")
    fields={"requested_quantity","confirmed_quantity"} if event['type']=='ENTRY_CONFIRMED' else {
        "flat_confirmed","orders_terminal","exit_reason","closed_at_utc","exit_triggered_at_utc",
        "nominal_exit_price","adverse_slippage_points"}
    if not event.keys()<=_COMMON|fields:_invalid('Unsupported statistics event fields')
    for key in ("account_key", "contract_id", "trading_day"):
        if event[key] != state[key]:raise TraderError("E_SCOPE", "Statistics event belongs to another aggregate scope")
    for key in ("event_id", "evidence_id", "position_cycle_id", "plan_id"):_id(event[key])
    _hash(event["plan_hash"]); _hash(event["profile_hash"])
    if event["profile_hash"] != profile.content_hash:raise TraderError("E_CONFIG", "Statistics event differs from its original source profile")
    if event["direction"] not in _DIRECTIONS or event["entry_kind"] not in {"NORMAL", "CONTINUATION"}:
        _invalid("Invalid statistics direction or entry kind")
    row = next((r for r in profile.to_record()["settings"]["symbols"] if r["code"] == event["contract_id"]), None)
    if row is None:_invalid("Statistics contract is not configured")
    for key, saved in (("stop_distance", "stop_offset"), ("take_profit_distance", "profit_offset")):
        if _decimal(event[key], positive=True) != _decimal(row[saved], positive=True):
            raise TraderError("E_CONFIG", "Statistics distances differ from the original source profile")
    _decimal(event["theoretical_entry_price"], positive=True)
    if event["type"] == "ENTRY_CONFIRMED":
        for key in ("requested_quantity", "confirmed_quantity"):
            try:quantity(event.get(key), positive=True)
            except TraderError:_invalid("Confirmed entry needs positive explicit target and actual quantities")
        if event["confirmed_quantity"] > event["requested_quantity"]:_invalid("Entry confirmation exceeds the original target")
    else:
        if event.get("flat_confirmed") is not True or event.get("orders_terminal") is not True:
            raise TraderError("E_FACT_BARRIER", "A statistics close requires proven flat and terminal order facts")
        if event.get("exit_reason") not in _CLOSE_REASONS:_invalid("Unsupported statistics close reason")
        _utc(event.get("closed_at_utc"))
        if event.get("exit_triggered_at_utc") is not None and _utc(event["exit_triggered_at_utc"])>_utc(event['closed_at_utc']):
            raise TraderError('E_FACT_BARRIER','An exit trigger cannot follow its confirmed close evidence')
        if event["exit_reason"] in _NORMAL_REASONS | {"SESSION_CLOSE", "MANUAL_EXIT"}:
            _decimal(event.get("nominal_exit_price"), positive=True)
        if event["exit_reason"] == "ADVERSE_SLIPPAGE":
            slip=_decimal(event.get("adverse_slippage_points"))
            if slip < 0:_invalid("Adverse slippage cannot be negative")
    return row


def apply_event(profile, state, event):
    """Apply one original-cycle entry confirmation or terminal close exactly once.

    The source's trade_count counts first entry confirmation and normal close
    separately; total_volume is its requested target, not confirmed fill volume.
    All actual fill quantities and currency P&L remain in the owner's ledger.
    """
    _state(state);_event(profile,state,event)
    key = event["position_cycle_id"] + ":" + event["type"]
    # Transport/evidence identities remain in the receipt for audit. Renaming
    # them cannot create a second economic event for the same cycle and phase.
    material = {key:value for key,value in event.items() if key not in {"event_id", "evidence_id"}}
    digest=content_hash(material);existing=state["receipts"].get(key)
    if existing is not None:
        if existing["event_hash"] != digest:
            raise TraderError("E_DUPLICATE_CONFLICT", "Original cycle statistics identity changed content")
        return {"state":copy.deepcopy(state), "receipt":copy.deepcopy(existing["receipt"]), "duplicate":True,
                "source_notes":copy.deepcopy(existing["receipt"]["source_notes"])}
    if len(state["receipts"]) >= MAX_RECEIPTS:raise TraderError("E_QUEUE_OVERFLOW", "Statistics daily receipt capacity is full")
    if event['type']=='ENTRY_CONFIRMED' and event['position_cycle_id']+':CYCLE_CLOSED' in state['receipts']:
        raise TraderError('E_FACT_BARRIER','A terminal no-fill statistics cycle cannot acquire an unreviewed late entry')
    if event['type']=='CYCLE_CLOSED':
        entry=state['receipts'].get(event['position_cycle_id']+':ENTRY_CONFIRMED')
        if entry is None and event['exit_reason']!='NO_FILL':
            raise TraderError('E_FACT_BARRIER','Original entry confirmation must be recorded before its close statistics')
        if entry is not None:
            if event['exit_reason']=='NO_FILL':raise TraderError('E_FACT_BARRIER','A confirmed entry cannot close as a no-fill cycle')
            for name in ("plan_id","plan_hash","profile_hash","direction","entry_kind","theoretical_entry_price","stop_distance","take_profit_distance"):
                if entry['receipt'][name]!=event[name]:raise TraderError('E_SCOPE','Close statistics differ from their original confirmed cycle')
    result=copy.deepcopy(state);daily=Decimal(state["daily_pnl_points"]);loss=Decimal(state["net_loss_points"])
    notes=[];daily_delta=Decimal(0);loss_delta=Decimal(0)
    with localcontext() as context:
        context.prec=80
        if event["type"] == "ENTRY_CONFIRMED":
            result["trade_count"]+=1;result["total_volume"]+=event["requested_quantity"]
            notes.append("SOURCE_TOTAL_VOLUME_IS_REQUESTED_TARGET_NOT_CONFIRMED_FILL_VOLUME")
        else:
            reason=event["exit_reason"]
            if reason in _NORMAL_REASONS | {"SESSION_CLOSE", "MANUAL_EXIT"}:
                sign=Decimal(1) if event["direction"]=="LONG" else Decimal(-1)
                pf=sign*(Decimal(event["nominal_exit_price"])-Decimal(event["theoretical_entry_price"]))
            if reason in _NORMAL_REASONS:
                daily_delta=pf;result["trade_count"]+=1
                if reason in {"STOP_LOSS", "BREAK_EVEN"}:
                    loss_delta=Decimal(event["stop_distance"]);result["stop_count"]+=1;result["stop_streak"]+=1
                    if event["entry_kind"]=="CONTINUATION" and profile.to_record()["settings"]["trade"]["cont_stop_day_stop"]:
                        result["continuation_stopped"]=True
                        result['day_stopped']=True;result['day_stopped_reason']='延续开止损后当日停开'
                    trade=profile.to_record()["settings"]["trade"]
                    if trade["stop_cooldown_enabled"] and Decimal(trade["stop_cooldown_minutes"])>0:
                        duration_ms=Decimal(trade["stop_cooldown_minutes"])*60000
                        if duration_ms>MAX_QUANTITY:
                            raise TraderError("E_UNSUPPORTED", "Cooldown exceeds the supported millisecond domain")
                        duration_us=(duration_ms*1000).to_integral_value(rounding=ROUND_CEILING)
                        trigger=event.get('exit_triggered_at_utc') or event['closed_at_utc']
                        try:until=_utc(trigger)+timedelta(microseconds=int(duration_us))
                        except OverflowError as error:raise TraderError('E_UNSUPPORTED','Cooldown UTC deadline exceeds the calendar range') from error
                        result["cooldown_until"]={"LONG":None,"SHORT":None}
                        result["cooldown_until"][event["direction"]]=until.isoformat(timespec="microseconds").replace("+00:00","Z")
                        result['cooldown_direction']=event['direction'];result['cooldown_started_at_utc']=trigger
                        result['cooldown_duration_ms']=decimal_text(duration_ms)
                        if duration_us!=duration_ms*1000:notes.append('COOLDOWN_UTC_DEADLINE_ROUNDED_UP_TO_MICROSECOND')
                else:
                    loss_delta=-Decimal(event["take_profit_distance"]) if reason=="TAKE_PROFIT" else -pf
                    result["profit_count"]+=1;result["stop_streak"]=0
            elif reason=="SESSION_CLOSE":daily_delta=pf;loss_delta=-pf
            elif reason=="ADVERSE_SLIPPAGE":loss_delta=Decimal(event["adverse_slippage_points"])
            elif reason=="MANUAL_EXIT":
                daily_delta=pf;loss_delta=-pf;result['stop_count']+=1;result['trade_count']+=1
                notes.append('MANUAL_NOMINAL_SIGN_AND_UNIT_REPAIRED')
            elif reason=='NO_FILL':notes.append('NO_CONFIRMED_ENTRY_NO_STATISTICS_DELTA')
            result["daily_pnl_points"]=decimal_text(daily+daily_delta)
            result["net_loss_points"]=decimal_text(loss+loss_delta)
        for name in _COUNT_FIELDS:
            if result[name]>MAX_QUANTITY:raise TraderError("E_QUANTITY", "Statistics aggregate count exceeds its bounded domain")
    # Computed results must still fit the registered point domain exactly.
    _decimal(result["daily_pnl_points"]);_decimal(result["net_loss_points"])
    receipt=copy.deepcopy(event)
    receipt.update(event_hash=digest,daily_pnl_delta_points=decimal_text(daily_delta),
                   net_loss_delta_points=decimal_text(loss_delta),source_notes=notes)
    if event["type"]=="CYCLE_CLOSED":receipt["exit_reason"]=event["exit_reason"]
    result["receipts"][key]={"event_hash":digest,"receipt":receipt}
    canonical_bytes(result);canonical_bytes(receipt)
    return {"state":result,"receipt":receipt,"duplicate":False,"source_notes":notes}


def rollover_day(state, trading_day):
    """Reset daily counters only on an explicitly advanced cabinet-day label.

    The directional source cooldown survives day reset. Calendar completeness,
    stop-day history and monthly state belong to the separate owner/stop reducer.
    """
    _state(state);today=_day(trading_day)
    if today<state["trading_day"]:raise TraderError("E_REVISION_CONFLICT", "Statistics cabinet day cannot move backwards")
    if today==state["trading_day"]:return copy.deepcopy(state)
    result=initial_state(state["account_key"],state["contract_id"],today)
    for key in ('cooldown_until','cooldown_direction','cooldown_started_at_utc','cooldown_duration_ms'):
        result[key]=copy.deepcopy(state[key])
    return result


def cooldown_gate(state, *, direction, now_utc, elapsed_ms=None):
    """Interpret an explicit clock fact; it does not authorize new exposure."""
    _state(state)
    if direction not in _DIRECTIONS:_invalid("Unknown cooldown direction")
    now=_utc(now_utc);until=state["cooldown_until"][direction]
    if elapsed_ms is not None:
        try:quantity(elapsed_ms)
        except TraderError:_invalid('Expected a bounded monotonic cooldown elapsed duration')
        remaining=Decimal(state['cooldown_duration_ms'])-elapsed_ms if until is not None else Decimal(0)
        blocked=remaining>0
        remaining=0 if not blocked else int(remaining.to_integral_value(rounding=ROUND_CEILING))
    else:
        blocked=until is not None and now<_utc(until)
        delta=_utc(until)-now if blocked else timedelta(0)
        microseconds=delta.days*86400000000+delta.seconds*1000000+delta.microseconds
        remaining=0 if not blocked else int((Decimal(microseconds)/1000).to_integral_value(rounding=ROUND_CEILING))
    return {"blocked":blocked,"direction":direction,"until_utc":until,"remaining_ms":remaining,
            "clock_basis":'MONOTONIC_ELAPSED' if elapsed_ms is not None else 'EXPLICIT_UTC',
            "reasons":["STOP_COOLDOWN"] if blocked else []}
