"""Pure DSV entry decisions from explicit, ordered market facts.

The strategy does not own money, consume opportunities, send orders or infer
missing history.  The owner must commit a candidate's opportunity atomically
with its intent and revalidate the facts before any send.
"""

from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext

from online_trader.configuration.dsv import DsvProfile
from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.values import decimal_text, decimal_value, identifier, quantity
from online_trader.errors import TraderError


STATE_VERSION = "DSV_ENTRY_STATE/1"
DIRECTIONS = ("LONG", "SHORT")
_REQUIRED = frozenset({"account_key", "contract_id", "trading_day", "session_id", "source_epoch", "sequence",
    "source_time_utc", "session_open_utc", "session_close_utc", "last", "bid1", "ask1", "day_high", "day_low",
    "tick_size", "quality", "day_range_complete", "path_complete", "is_trading", "calendar_complete", "candle_id"})


def _utc(value):
    if type(value) is not str or not value.endswith("Z"):
        raise TraderError("E_SCHEMA", "Expected an explicit UTC market timestamp")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise TraderError("E_SCHEMA", "Invalid market timestamp") from error
    return result


def _seconds(delta):
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000


def opening_jump_block(last_price, trigger, profit_distance, ratio):
    """DSV gui.opening_jump_block: absolute distance, with inclusive boundary."""
    price, line, take, factor = map(Decimal, (last_price, trigger, profit_distance, ratio))
    return price > 0 and line > 0 and take > 0 and factor > 0 and abs(price - line) >= take * factor


def hysteresis_ready(direction, last_price, trigger, distance, armed):
    """DSV reverse excursion arms once; the trigger condition is a separate gate."""
    price, line, hysteresis = map(Decimal, (last_price, trigger, distance))
    if hysteresis <= 0 or armed:return True, bool(armed)
    reached = price <= line - hysteresis if direction == "LONG" else price >= line + hysteresis
    return bool(reached), bool(reached)


def chase_within_cap(direction, last_price, opponent_price, trigger, cap):
    """DSV helper: one favourable price can override the other adverse price.

    The caller separately requires valid complete prices.  The original helper's
    treatment of absent/zero prices is deliberately not used as fact completeness.
    """
    last, opponent, line, distance = map(Decimal, (last_price, opponent_price, trigger, cap))
    values = (last, opponent)
    if direction == "LONG":
        adverse = any(price > line + distance for price in values)
        favourable = any(0 < price <= line for price in values)
    else:
        adverse = any(0 < price < line - distance for price in values)
        favourable = any(price >= line for price in values)
    return not adverse or favourable


class DsvEntryStrategy:
    def __init__(self, profile):
        if not isinstance(profile, DsvProfile):
            raise TraderError("E_CONFIG", "Expected a validated DSV profile")
        self.profile = profile
        self._settings = profile.to_record()["settings"]
        self._symbols = {row["code"]: row for row in self._settings["symbols"]}

    def initial_state(self, account_key, contract_id, trading_day):
        identifier(account_key);identifier(contract_id)
        try:
            if date.fromisoformat(trading_day).isoformat() != trading_day:raise ValueError()
        except (ValueError, TypeError) as error:
            raise TraderError("E_SCHEMA", "Invalid strategy trading day") from error
        return {"schema_version": STATE_VERSION, "profile_hash": self.profile.content_hash,
            "account_key": account_key, "contract_id": contract_id, "trading_day": trading_day,
            "source_epoch": 0, "last_sequence": 0, "last_event_hash": None, "last_source_time_utc": None,
            "session_id": None, "observation_key": None, "coverage_lost": False, "hysteresis_armed": {"LONG": False, "SHORT": False},
            "first_minute_blocks": [], "last_trigger_prices": {"LONG": None, "SHORT": None},
            "open_half_reached": False, "need_hyst": False, "last_signal_bar": None}

    def _validate(self, state, event):
        if type(state) is not dict or type(event) is not dict:
            raise TraderError("E_SCHEMA", "Expected strategy and market records")
        canonical_bytes(state);canonical_bytes(event)
        if state.get("schema_version") != STATE_VERSION or state.get("profile_hash") != self.profile.content_hash:
            raise TraderError("E_CONFIG", "Strategy state belongs to another profile or schema")
        required_state = {"source_epoch", "last_sequence", "last_event_hash", "last_source_time_utc", "session_id", "observation_key",
            "coverage_lost", "hysteresis_armed", "first_minute_blocks", "last_trigger_prices", "open_half_reached",
            "need_hyst", "last_signal_bar"}
        if not required_state <= state.keys():raise TraderError("E_SCHEMA", "Incomplete strategy state")
        for key in ("source_epoch", "last_sequence"):quantity(state[key])
        for key in ("coverage_lost", "open_half_reached", "need_hyst"):
            if type(state[key]) is not bool:raise TraderError("E_SCHEMA", "Invalid persisted strategy switch")
        if (type(state["hysteresis_armed"]) is not dict or set(state["hysteresis_armed"]) != set(DIRECTIONS)
                or any(type(value) is not bool for value in state["hysteresis_armed"].values())):
            raise TraderError("E_SCHEMA", "Invalid persisted hysteresis state")
        if type(state["first_minute_blocks"]) is not list or any(item not in DIRECTIONS for item in state["first_minute_blocks"]):
            raise TraderError("E_SCHEMA", "Invalid persisted day block")
        if type(event) is not dict or not _REQUIRED <= event.keys():
            raise TraderError("E_SCHEMA", "Incomplete explicit market fact")
        for key in ("account_key", "contract_id", "trading_day"):
            if event[key] != state.get(key):raise TraderError("E_SCOPE", "Market fact belongs to another strategy scope")
        for key in ("account_key", "contract_id", "session_id", "candle_id"):identifier(event[key])
        for key in ("source_epoch", "sequence"):quantity(event[key], positive=True)
        for key in ("day_range_complete", "path_complete", "is_trading", "calendar_complete"):
            if type(event[key]) is not bool:raise TraderError("E_SCHEMA", "Expected explicit market completeness")
        if event["quality"] not in {"CONTIGUOUS", "DATA_GAP", "UNKNOWN"}:
            raise TraderError("E_SCHEMA", "Unknown market quality")
        for key in ("last", "day_high", "day_low"):decimal_value(event[key], positive=True)
        for key in ("bid1", "ask1"):
            if event[key] is not None:decimal_value(event[key], positive=True)
        tick = decimal_value(event["tick_size"], positive=True)
        last, high, low = (Decimal(event[k]) for k in ("last", "day_high", "day_low"))
        if high < low or not low <= last <= high:
            raise TraderError("E_SCHEMA", "Market prices disagree with their proved day range")
        if event["bid1"] is not None and event["ask1"] is not None and Decimal(event["bid1"]) > Decimal(event["ask1"]):
            raise TraderError("E_SCHEMA", "Crossed opponent prices")
        start, current, end = (_utc(event[k]) for k in ("session_open_utc", "source_time_utc", "session_close_utc"))
        if start >= end:raise TraderError("E_SCHEMA", "Invalid proved session window")
        if "is_opening_session" in event and type(event["is_opening_session"]) is not bool:
            raise TraderError("E_SCHEMA", "Expected an explicit opening-session classification")
        policy=event.get('session_policy')
        if policy is not None:
            if (type(policy) is not dict or set(policy)!={'schema_version','calendar_hash','force_close_at_end','entry_close_utc'}
                    or policy['schema_version']!='DSV_TIME/1' or type(policy['force_close_at_end']) is not bool
                    or type(policy['calendar_hash']) is not str or not re.fullmatch('[0-9a-f]{64}',policy['calendar_hash'])):
                raise TraderError('E_SCHEMA','Invalid explicit session timing policy')
            if policy['entry_close_utc'] is not None:_utc(policy['entry_close_utc'])
        opening_session = event.get("is_opening_session")
        if opening_session is None:
            # Captured DSV helper constants: the two day starts and 21:00 night
            # opening. A noon restart is not a new opening-session window.
            local = start.astimezone(timezone(timedelta(hours=8)))
            opening_session = local.strftime("%H:%M") in {"09:00", "09:30", "21:00"}
        for key in ("blocked_directions", "consumed_candle_keys"):
            if key in event and (type(event[key]) is not list or any(type(item) is not str for item in event[key])):
                raise TraderError("E_SCHEMA", "Invalid owner strategy facts")
        if any(item not in DIRECTIONS for item in event.get("blocked_directions", [])):
            raise TraderError("E_SCHEMA", "Unknown blocked direction")
        stop_prices = event.get("stop_trigger_prices", {})
        if type(stop_prices) is not dict or not stop_prices.keys() <= set(DIRECTIONS):
            raise TraderError("E_SCHEMA", "Invalid stop-trigger history")
        for price in stop_prices.values():
            if price is not None:decimal_value(price, positive=True)
        return tick, start, current, end, opening_session

    def evaluate(self, state, event):
        with localcontext() as context:
            context.prec = 80
            return self._evaluate(state, event)

    def _evaluate(self, state, event):
        tick, start, current, end, opening_session = self._validate(state, event)
        result = {"state": copy.deepcopy(state), "candidates": [], "reasons": [], "source_notes": []}
        updated, reasons = result["state"], result["reasons"]
        fingerprint = content_hash({key: event[key] for key in _REQUIRED} | {"is_opening_session": opening_session} |
                                   ({'session_policy':event['session_policy']} if 'session_policy' in event else {}))
        if event["source_epoch"] == state["source_epoch"]:
            if event["sequence"] == state["last_sequence"]:
                if fingerprint != state["last_event_hash"]:
                    raise TraderError("E_DUPLICATE_CONFLICT", "Repeated market identity changed content")
                reasons.append("DUPLICATE_MARKET_EVENT");return result
            if event["sequence"] < state["last_sequence"]:
                reasons.append("OUT_OF_ORDER_MARKET_EVENT");return result
            if state["last_sequence"] and event["sequence"] != state["last_sequence"] + 1:
                updated["coverage_lost"] = True
        elif state["source_epoch"]:
            updated["coverage_lost"] = True
        if state["last_source_time_utc"] is not None and current < _utc(state["last_source_time_utc"]):
            reasons.append("OUT_OF_ORDER_SOURCE_TIME");return result
        observation_key = content_hash({"trading_day": event["trading_day"], "session_open_utc": event["session_open_utc"]})
        if state["observation_key"] != observation_key:
            updated.update(hysteresis_armed={"LONG": False, "SHORT": False}, open_half_reached=False, need_hyst=False)
        updated.update(source_epoch=event["source_epoch"], last_sequence=event["sequence"], last_event_hash=fingerprint,
                       last_source_time_utc=event["source_time_utc"], session_id=event["session_id"], observation_key=observation_key)
        if event["quality"] != "CONTIGUOUS" or not event["path_complete"]:updated["coverage_lost"] = True
        if updated["coverage_lost"]:reasons.append("MARKET_PATH_INCOMPLETE")
        if not event["calendar_complete"]:reasons.append("CALENDAR_INCOMPLETE")
        if not event["day_range_complete"]:reasons.append("DAY_RANGE_INCOMPLETE")
        if not event["is_trading"] or current < start or current >= end:reasons.append("OUTSIDE_TRADING_SESSION")
        symbol = self._symbols.get(event["contract_id"])
        if symbol is None:reasons.append("CONTRACT_NOT_CONFIGURED");return result
        O, S, T = (Decimal(symbol[k]) for k in ("open_offset", "stop_offset", "profit_offset"))
        if min(O, S, T) <= 0:reasons.append("SYMBOL_OFFSETS_INCOMPLETE");return result
        if reasons:return result
        trade = self._settings["trade"]
        last, high, low = (Decimal(event[k]) for k in ("last", "day_high", "day_low"))
        lines = {"LONG": low + O, "SHORT": high - O}
        updated["last_trigger_prices"] = {d: decimal_text(price) for d, price in lines.items()}
        elapsed = _seconds(current - start)
        remaining = _seconds(end - current)
        first_minute = Decimal(0) <= elapsed <= Decimal(60)
        continuation = bool(first_minute and opening_session and trade["continuation_entry"])
        wait_waived = continuation and trade["continuation_ignore_no_open"]
        policy=event.get('session_policy')
        wait_limit=Decimal(trade['no_open_after_open_minutes'])*60
        waiting=elapsed<=wait_limit if policy is not None else elapsed<wait_limit
        if trade["no_open_after_open"] and opening_session and waiting and not wait_waived:
            reasons.append("OPENING_WAIT")
        if policy is not None:
            remaining=None if policy['entry_close_utc'] is None else _seconds(_utc(policy['entry_close_utc'])-current)
        if remaining is not None and remaining <= Decimal(trade["no_open_before_close_minutes"]) * 60:reasons.append("CLOSE_SESSION_NO_OPEN")
        if high - low > 2 * O - S:reasons.append("GEOMETRY_BLOCK")
        if reasons:return result
        identity = {key: event[key] for key in ("account_key", "contract_id", "trading_day", "candle_id")}
        candle_key = "candle-" + content_hash(identity)
        if updated["last_signal_bar"] == event["candle_id"] or candle_key in event.get("consumed_candle_keys", []):
            reasons.append("CANDLE_OPPORTUNITY_CONSUMED");return result
        if updated["first_minute_blocks"]:
            reasons.append("FIRST_MINUTE_JUMP_DAY_STOP");return result
        # DSV's source intent uses this observation block only after the opening
        # wait gate. Its local/global _secs_o mismatch is repaired using the
        # explicit session clock and is reported rather than hidden.
        if first_minute and opening_session and not updated["need_hyst"]:
            result["source_notes"].append("DSV_UNDEFINED_FIRST_MINUTE_CLOCK_REPAIRED")
            if any(opening_jump_block(last, line, T, trade["open_jump_block_ratio"]) for line in lines.values()):
                updated["first_minute_blocks"] = list(DIRECTIONS)
                reasons.append("FIRST_MINUTE_JUMP_DAY_STOP");return result
            if last >= lines["LONG"] + T / 2 or last <= lines["SHORT"] - T / 2:
                updated["open_half_reached"] = True
            if not updated["open_half_reached"] and any(abs(last - line) <= tick * Decimal("1.5") for line in lines.values()):
                updated["need_hyst"] = True
        if updated["need_hyst"]:
            probe = "LONG" if last < (lines["LONG"] + lines["SHORT"]) / 2 else "SHORT"
            h = Decimal(symbol["hysteresis_points"]) or S
            ready, armed = hysteresis_ready(probe, last, lines[probe], h, updated["hysteresis_armed"][probe])
            updated["hysteresis_armed"][probe] = armed
            if not ready:
                updated["last_signal_bar"] = event["candle_id"]
                reasons.append(probe + ":HYSTERESIS_NOT_READY");return result
        activation = T * Decimal(trade["protect_80_ratio"] if trade["protect_80_enabled"] else trade["trailing_activate_ratio"])
        recent_open = opening_session and Decimal(0) <= elapsed <= Decimal(trade["chase_window_minutes"]) * 60
        ratio = Decimal(trade["chase_slip_ratio"] if recent_open else trade["entry_slip_ratio"])
        cap = max(Decimal(1), Decimal(round(O * ratio)))
        for direction in DIRECTIONS:
            line = lines[direction]
            if direction in event.get("blocked_directions", []):
                reasons.append(direction + ":OWNER_DAY_BLOCK");continue
            reached = last >= line if direction == "LONG" else last <= line
            if trade["no_chase_after_act"] and (high >= line + activation if direction == "LONG" else low <= line - activation):
                if reached:updated["last_signal_bar"] = event["candle_id"]
                reasons.append(direction + ":NO_CHASE_AFTER_ACT");continue
            if not reached:reasons.append(direction + ":TRIGGER_NOT_REACHED");continue
            opponent = event["ask1"] if direction == "LONG" else event["bid1"]
            if opponent is None:
                reasons.append(direction + ":OPPONENT_PRICE_MISSING");continue
            if Decimal(opponent) % tick:
                reasons.append(direction + ":QUOTE_NOT_TICK_ALIGNED");continue
            if not chase_within_cap(direction, last, opponent, line, cap):
                updated["last_signal_bar"] = event["candle_id"]
                reasons.append(direction + ":CHASE_CAP_EXCEEDED");continue
            previous_stop = event.get("stop_trigger_prices", {}).get(direction)
            if (trade["require_trigger_update"] and previous_stop is not None and Decimal(previous_stop) > 0
                    and abs(Decimal(previous_stop) - line) < Decimal("0.000001")):
                updated["last_signal_bar"] = event["candle_id"]
                reasons.append(direction + ":STOP_TRIGGER_UNCHANGED");continue
            updated["last_signal_bar"] = event["candle_id"]
            near_open=opening_session and 0<=elapsed<=Decimal(trade['chase_window_minutes'])*60
            if near_open:entry_price=opponent
            elif trade['passive_entry_first']:entry_price=decimal_text(line)
            else:
                capped=min(Decimal(opponent),line+cap) if direction=='LONG' else max(Decimal(opponent),line-cap)
                entry_price=decimal_text(capped)
            if Decimal(entry_price)%tick:
                reasons.append(direction+':ENTRY_PRICE_NOT_TICK_ALIGNED');continue
            result["candidates"].append({"account_key": event["account_key"], "contract_id": event["contract_id"],
                "trading_day": event["trading_day"], "direction": direction, "theoretical_price": decimal_text(line),
                "limit_price": entry_price, "stop_distance": decimal_text(S), "take_profit_distance": decimal_text(T),
                "entry_near_open":near_open,
                "open_offset": decimal_text(O), "decision_key": "decision-" + content_hash(identity), "candle_key": candle_key,
                "source_epoch": event["source_epoch"], "market_sequence": event["sequence"],
                "source_time_utc": event["source_time_utc"], "session_id": event["session_id"],
                "profile_hash": self.profile.content_hash, "entry_kind": "CONTINUATION" if continuation else "NORMAL"})
            break
        result["reasons"] = list(dict.fromkeys(reasons))
        canonical_bytes(result)
        return result
