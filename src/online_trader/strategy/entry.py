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
CHINA_TIME = timezone(timedelta(hours=8))
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
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


def epoch_milliseconds(moment):
    elapsed = moment - UNIX_EPOCH
    return (elapsed.days * 86400 + elapsed.seconds) * 1000 + elapsed.microseconds // 1000


def is_night_session(moment):
    hour = moment.astimezone(CHINA_TIME).hour
    return hour >= 18 or hour < 3


def _native_tick_id(value):
    if value is None:
        return None
    if type(value) is not str or not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise TraderError("E_SCHEMA", "Invalid native tick identity")
    return int(value)


def continuation_direction(price, night_last, night_high, night_low, offset, fallback=None):
    """DSV continuation uses the overnight range and allows only favourable gaps."""
    if price >= night_low + offset:
        direction = "LONG"
    elif price <= night_high - offset:
        direction = "SHORT"
    else:
        direction = None
    if price == night_last:
        return direction or fallback
    if (price > night_last and direction == "SHORT") or (price < night_last and direction == "LONG"):
        return direction
    return None


def market_fingerprint(event, opening_session):
    return content_hash({key: event[key] for key in _REQUIRED} | {"is_opening_session": opening_session} |
                        ({"session_policy": event["session_policy"]} if "session_policy" in event else {}))


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
            "open_half_reached": False, "need_hyst": False, "last_signal_bar": None, "night_reference": None,
            "night_close_fallback_cleared": False, "last_ordinary_attempt": None}

    @staticmethod
    def _validate_night_reference(reference, event_time):
        if reference is None:
            return
        if (type(reference) is not dict or set(reference) !=
                {"session_id", "source_time_utc", "source_epoch", "sequence", "native_tick_id",
                 "high", "low", "last", "boundary_proved"}):
            raise TraderError("E_SCHEMA", "Invalid overnight continuation reference")
        identifier(reference["session_id"])
        quantity(reference["source_epoch"], positive=True)
        quantity(reference["sequence"])
        _native_tick_id(reference["native_tick_id"])
        moment = _utc(reference["source_time_utc"])
        if moment > event_time or not is_night_session(moment) or type(reference["boundary_proved"]) is not bool:
            raise TraderError("E_SCHEMA", "Invalid overnight continuation boundary")
        high, low, last = (decimal_value(reference[key], positive=True) for key in ("high", "low", "last"))
        if not low <= last <= high:
            raise TraderError("E_SCHEMA", "Overnight prices disagree with their proved range")

    @staticmethod
    def _night_reference(state, event, start, current):
        reference = state.get("night_reference")
        if is_night_session(start):
            high, low = Decimal(event["day_high"]), Decimal(event["day_low"])
            if reference is not None and reference["session_id"] == event["session_id"]:
                high = max(high, Decimal(reference["high"]))
                low = min(low, Decimal(reference["low"]))
            return {"session_id": event["session_id"], "source_time_utc": event["source_time_utc"],
                    "source_epoch": event["source_epoch"], "sequence": event["sequence"],
                    "native_tick_id": event.get("native_tick_id"), "high": decimal_text(high),
                    "low": decimal_text(low), "last": event["last"], "boundary_proved": False}
        # A daytime restart may see the previous session in the original
        # native serial even if this ledger never observed it live.
        boundary = event.get("night_reference")
        if boundary is not None:
            if reference is not None and reference["boundary_proved"]:
                if (reference["session_id"] != boundary["session_id"]
                        or _utc(reference["source_time_utc"]) != _utc(boundary["source_time_utc"])
                        or Decimal(reference["last"]) != Decimal(boundary["last"])):
                    raise TraderError("E_DUPLICATE_CONFLICT", "Accepted overnight boundary changed")
            if (reference is not None and reference["session_id"] == boundary["session_id"]
                    and _utc(reference["source_time_utc"]) <= _utc(boundary["source_time_utc"])):
                boundary = boundary | {"high": decimal_text(max(Decimal(reference["high"]), Decimal(boundary["high"]))),
                                       "low": decimal_text(min(Decimal(reference["low"]), Decimal(boundary["low"])))}
            return copy.deepcopy(boundary)
        if reference is None or reference["boundary_proved"]:
            return reference
        if (current.astimezone(CHINA_TIME).hour == 9 and start.astimezone(CHINA_TIME).strftime("%H:%M") == "09:00"
                and reference["source_epoch"] == event["source_epoch"]):
            previous_id = _native_tick_id(reference["native_tick_id"])
            current_id = _native_tick_id(event.get("native_tick_id"))
            adjacent = (reference["sequence"] + 1 == event["sequence"] if previous_id is None and current_id is None
                        else previous_id is not None and current_id is not None and previous_id + 1 == current_id)
            if adjacent:
                return reference | {"boundary_proved": True}
        return reference

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
        if type(state.get("night_close_fallback_cleared", False)) is not bool:
            raise TraderError("E_SCHEMA", "Invalid overnight fallback state")
        last_attempt = state.get("last_ordinary_attempt")
        if last_attempt is not None:
            if (type(last_attempt) is not dict or set(last_attempt) != {"direction", "at_utc"}
                    or last_attempt["direction"] not in DIRECTIONS):
                raise TraderError("E_SCHEMA", "Invalid ordinary entry attempt state")
            _utc(last_attempt["at_utc"])
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
        _native_tick_id(event.get("native_tick_id"))
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
        state_time = state["last_source_time_utc"]
        self._validate_night_reference(state.get("night_reference"), current if state_time is None else _utc(state_time))
        supplied_night = event.get("night_reference")
        self._validate_night_reference(supplied_night, start)
        if supplied_night is not None and (not supplied_night["boundary_proved"] or is_night_session(start)
                                           or supplied_night["source_epoch"] != event["source_epoch"]):
            raise TraderError("E_SCHEMA", "Expected a proved overnight-to-day boundary")
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
            local = start.astimezone(CHINA_TIME)
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

    def evaluate(self, state, event, *, continuation_allowed=True, continuation_attempted=False,
                 fallback_direction=None, ordinary_quote_stale=False, decision_time=None):
        if decision_time is not None and (not isinstance(decision_time, datetime) or decision_time.tzinfo is None):
            raise TraderError("E_SCHEMA", "Expected a timezone-aware entry decision time")
        with localcontext() as context:
            context.prec = 80
            return self._evaluate(state, event, continuation_allowed, continuation_attempted,
                                  fallback_direction, ordinary_quote_stale, decision_time)

    def _evaluate(self, state, event, continuation_allowed, continuation_attempted,
                  fallback_direction, ordinary_quote_stale, decision_time):
        tick, start, current, end, opening_session = self._validate(state, event)
        result = {"state": copy.deepcopy(state), "candidates": [], "reasons": [], "source_notes": [], "entry_log": []}
        updated, reasons = result["state"], result["reasons"]
        fingerprint = market_fingerprint(event, opening_session)
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
        updated["night_reference"] = self._night_reference(updated, event, start, current)
        last, high, low = (Decimal(event[k]) for k in ("last", "day_high", "day_low"))
        lines = {"LONG": low + O, "SHORT": high - O}
        updated["last_trigger_prices"] = {d: decimal_text(price) for d, price in lines.items()}
        # Live DSV tests the local decision clock. An archived market replay
        # supplies no live clock and reconstructs observations on source time.
        decided_at = decision_time or current
        if not start <= decided_at < end:
            reasons.append("OUTSIDE_TRADING_SESSION")
            return result
        elapsed = _seconds(decided_at - start)
        remaining = _seconds(end - decided_at)
        first_minute = 0 <= _seconds(current - start) <= 60
        policy=event.get('session_policy')
        wait_limit=Decimal(trade['no_open_after_open_minutes'])*60
        geometry_limit = 2 * O - S
        identity = {key: event[key] for key in ("account_key", "contract_id", "trading_day", "candle_id")}
        candle_key = "candle-" + content_hash(identity)
        night = updated["night_reference"]
        # DSV calls the generic daytime chase-window helper for eligibility.
        # The ordinary strategy's inclusive wait, activation and per-bar
        # gates run separately from continuation's strict wait and day key.
        day_open = start.astimezone(CHINA_TIME).strftime("%H:%M") == "09:00"
        if (trade["continuation_entry"] and continuation_allowed and not continuation_attempted
                and day_open and 0 <= elapsed <= Decimal(trade["chase_window_minutes"]) * 60
                and night is not None and night["boundary_proved"]):
            waits = trade["no_open_after_open"] and not trade["continuation_ignore_no_open"] and elapsed < wait_limit
            if waits or (geometry_limit > 0 and high - low > geometry_limit):
                # The source discards the closeout fallback, retaining the
                # observed overnight range and last quote for a later signal.
                updated["night_close_fallback_cleared"] = True
            else:
                fallback = None if updated.get("night_close_fallback_cleared", False) else fallback_direction
                direction = continuation_direction(last, Decimal(night["last"]), Decimal(night["high"]),
                                                   Decimal(night["low"]), O, fallback)
                if direction is not None and direction not in event.get("blocked_directions", []):
                    opponent = event["ask1"] if direction == "LONG" else event["bid1"]
                    if opponent is not None and Decimal(opponent) % tick == 0:
                        result["candidates"].append(self._candidate(
                            event, direction, last, opponent, O, S, T, True, candle_key,
                            self.continuation_key(event), "CONTINUATION", decided_at))
                        canonical_bytes(result)
                        return result
        if policy is not None:
            remaining=None if policy['entry_close_utc'] is None else _seconds(_utc(policy['entry_close_utc'])-decided_at)
        entry_log = result["entry_log"]
        if trade["no_open_after_open"] and wait_limit > 0 and opening_session and elapsed <= wait_limit:
            reasons.append("OPENING_WAIT")
        elif remaining is not None and remaining <= Decimal(trade["no_open_before_close_minutes"]) * 60:
            reasons.append("CLOSE_SESSION_NO_OPEN")
        elif ordinary_quote_stale:
            reasons.append("DSV_QUOTE_AGE_EXCEEDED")
        if reasons:
            entry_log.extend(reasons)
            return result
        geometry_block = high - low > geometry_limit
        if geometry_block:
            reasons.append("GEOMETRY_BLOCK")
            entry_log.append("GEOMETRY_BLOCK")
        entry_log.append("TRIGGER_HINT")
        if updated["last_signal_bar"] == event["candle_id"] or candle_key in event.get("consumed_candle_keys", []):
            reasons.append("CANDLE_OPPORTUNITY_CONSUMED");return result
        activation = T * Decimal(trade["protect_80_ratio"] if trade["protect_80_enabled"] else trade["trailing_activate_ratio"])
        if trade["no_chase_after_act"] and activation > 0:
            hits = (last >= lines["LONG"] and high >= lines["LONG"] + activation,
                    last <= lines["SHORT"] and low <= lines["SHORT"] - activation)
            for direction, hit in zip(DIRECTIONS, hits):
                if hit:
                    reason = direction + ":NO_CHASE_AFTER_ACT"
                    reasons.append(reason)
                    entry_log.append(reason)
                    updated["last_signal_bar"] = event["candle_id"]
                    return result
        if geometry_block:
            return result
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
        recent_open = opening_session and Decimal(0) <= elapsed <= Decimal(trade["chase_window_minutes"]) * 60
        ratio = Decimal(trade["chase_slip_ratio"] if recent_open else trade["entry_slip_ratio"])
        cap = max(Decimal(1), Decimal(round(O * ratio)))
        for direction in DIRECTIONS:
            line = lines[direction]
            if direction in event.get("blocked_directions", []):
                reasons.append(direction + ":OWNER_DAY_BLOCK");continue
            reached = last >= line if direction == "LONG" else last <= line
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
            result["candidates"].append(self._candidate(
                event, direction, line, entry_price, O, S, T, near_open, candle_key,
                "decision-" + content_hash(identity), "NORMAL", decided_at))
            break
        result["reasons"] = list(dict.fromkeys(reasons))
        canonical_bytes(result)
        return result

    @staticmethod
    def continuation_key(event):
        """One durable owner intent per cabinet day, independent of the minute bar."""
        identity = {key: event[key] for key in ("account_key", "contract_id", "trading_day")}
        return "decision-" + content_hash(identity | {"entry_kind": "CONTINUATION"})

    def _candidate(self, event, direction, theoretical, limit, opening, stop, take,
                   near_open, candle_key, decision_key, entry_kind, decided_at):
        return {"account_key": event["account_key"], "contract_id": event["contract_id"],
                "trading_day": event["trading_day"], "direction": direction,
                "theoretical_price": decimal_text(theoretical), "limit_price": limit,
                "stop_distance": decimal_text(stop), "take_profit_distance": decimal_text(take),
                "entry_near_open": near_open, "open_offset": decimal_text(opening),
                "decision_key": decision_key, "candle_key": candle_key, "source_epoch": event["source_epoch"],
                "market_sequence": event["sequence"], "source_time_utc": event["source_time_utc"],
                "session_id": event["session_id"], "profile_hash": self.profile.content_hash, "entry_kind": entry_kind,
                "decision_time_utc": decided_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
