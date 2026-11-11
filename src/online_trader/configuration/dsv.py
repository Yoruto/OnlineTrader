"""Strict, immutable DSV parameter values with a separate canonical representation.

Only the four business sections are accepted.  Source numbers become exact Decimal
strings in ledger records; zero and disabled switches are never interpreted here.
No filesystem, network, account, or trading API is used by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import math
import re

from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.domain.values import decimal_text, decimal_value, quantity
from online_trader.errors import TraderError


SCHEMA_VERSION = "DSV_SETTINGS/1"
MAX_SOURCE_BYTES = 262_144
MAX_SYMBOLS = 64
SECTIONS = frozenset({"trade", "auto_offset", "schedule", "symbols"})
EXCLUDED_SOURCE_SECTIONS = frozenset({"account", "manual_account", "manual_live", "legal", "ui", "notify", "hotkeys", "accounts"})
_BOOLEANS = frozenset("""aggressive_mode auto_start_on_launch cancel_on_stop chase_base_now
cont_stop_day_stop continuation_entry continuation_ignore_no_open entry_slip_abort
entry_slip_abort_stop_day half_breakeven half_close_enabled half_stage2 live_first_one_lot
live_protect manual_close_day_stop max_symbols_enabled min_one_lot_fallback
no_chase_after_act no_open_after_open participate_auction passive_entry_first
protect_80_enabled protect_stop_day require_trigger_update rollover_stop_enabled
stale_close_day_stop stop_cooldown_enabled stop_day_on_trailing tick_diag""".split())
_INTEGERS = frozenset("""close_before_seconds entry_confirm_timeout live_max_lots
max_daily_orders max_net_loss_stops max_symbols_on max_volume min_days_to_expiry month_stop_days""".split())
_DECIMALS = frozenset("""chase_slip_ratio chase_window_minutes entry_slip_abort_ratio
entry_slip_ratio half_close_ratio half_stage2_trigger_ratio hysteresis_points
limit_fallback_ratio live_daily_loss_limit manual_tp_drop_ratio margin_rate_alert_threshold
max_quote_age_seconds max_risk_ratio no_chase_tolerance no_open_after_open_minutes
no_open_before_close_minutes open_gap_max_ratio open_jump_block_ratio position_sync_seconds
protect_80_ratio protect_keep_ratio risk_per_trade_ratio risk_reference_equity
stale_force_close_seconds stop_cooldown_minutes total_margin_cap trailing_activate_ratio
trailing_pullback_ratio""".split())
_TRADE_KEYS = _BOOLEANS | _INTEGERS | _DECIMALS | {"night_session_close", "close_price_mode", "holidays"}
_RATIOS = frozenset(key for key in _DECIMALS if key.endswith("_ratio")) | {"total_margin_cap"}
_SYMBOL_REQUIRED = frozenset({"name", "code", "open_offset", "stop_offset", "profit_offset", "order_volume", "half_close_ratio", "hysteresis_points"})
_SYMBOL_DECIMALS = ("open_offset", "stop_offset", "profit_offset", "half_close_ratio", "hysteresis_points")
_CODE = re.compile(r"[A-Z]{2,8}\.[A-Za-z][A-Za-z0-9]{1,31}\Z")
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{0,63}\Z")
_COEFFICIENT_KEY = re.compile(r"\w[\w.:-]{0,63}\Z", re.UNICODE)
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def _error(message="Invalid business configuration"):
    raise TraderError("E_CONFIG", message)


def _object(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional):
        _error("Missing or unsupported configuration fields")
    return value


def _boolean(value):
    if type(value) is not bool:
        _error("Expected an explicit boolean switch")
    return value


def _integer(value):
    try:
        return quantity(value)
    except TraderError:
        _error("Expected a bounded nonnegative integer")


def _decimal(value, *, canonical=False, ratio=False):
    if canonical:
        if type(value) is not str:
            _error("Canonical decimal settings must be strings")
        try:
            number = decimal_value(value)
        except TraderError:
            _error("Invalid canonical decimal setting")
    else:
        if type(value) not in {int, float, Decimal}:
            _error("Expected a JSON number, not a string or boolean")
        if type(value) is float and not math.isfinite(value):
            _error("Non-finite configuration number")
        try:
            number = Decimal(str(value))
            if not number.is_finite() or number.is_signed():
                _error("Expected a nonnegative finite configuration number")
            parts = number.as_tuple()
            if len(parts.digits) > 64 or not -32 <= parts.exponent <= 32:
                _error("Configuration number exceeds bounded decimal precision")
            # Domain limits apply after normalization; no float arithmetic occurs.
            decimal_value(decimal_text(number))
        except (InvalidOperation, TraderError):
            _error("Configuration decimal exceeds the supported precision or range")
    if number < 0 or (ratio and number > 1):
        _error("Configuration proportion or distance is outside its supported range")
    return decimal_text(number)


def _text(value, limit=64):
    if type(value) is not str or not 1 <= len(value) <= limit or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        _error("Invalid configuration text")
    return value


def _time(value):
    if type(value) is not str or not _TIME.fullmatch(value):
        _error("Expected a 24-hour HH:MM time")
    return value


def _code(value):
    if type(value) is not str or not _CODE.fullmatch(value):
        _error("Invalid saved contract identifier")
    return value


def _normalize(settings, *, canonical=False):
    _object(settings, SECTIONS)
    trade = _object(settings["trade"], _TRADE_KEYS)
    normalized = {key: _boolean(trade[key]) for key in _BOOLEANS}
    normalized.update({key: _integer(trade[key]) for key in _INTEGERS})
    normalized.update({key: _decimal(trade[key], canonical=canonical, ratio=key in _RATIOS) for key in _DECIMALS})
    normalized["night_session_close"] = _time(trade["night_session_close"])
    normalized["close_price_mode"] = _text(trade["close_price_mode"], 24)
    holidays = trade["holidays"]
    if type(holidays) is not list or len(holidays) > 1024:
        _error("Invalid saved holiday list")
    result_holidays = []
    for item in holidays:
        try:
            if type(item) is not str or date.fromisoformat(item).isoformat() != item:
                _error("Holiday dates must be YYYY-MM-DD")
        except ValueError:
            _error("Invalid saved holiday date")
        if item in result_holidays:
            _error("Repeated saved holiday date")
        result_holidays.append(item)
    normalized["holidays"] = result_holidays

    auto = _object(settings["auto_offset"], {"formula", "ceiling", "floor", "coef"})
    formula = _text(auto["formula"])
    if not _KEY.fullmatch(formula):
        _error("Invalid saved parameter formula name")
    coefficients = auto["coef"]
    if type(coefficients) is not dict or len(coefficients) > MAX_SYMBOLS:
        _error("Invalid saved parameter coefficient map")
    new_coefficients = {}
    for key, number in coefficients.items():
        if type(key) is not str or not _COEFFICIENT_KEY.fullmatch(key):
            _error("Invalid coefficient identity")
        new_coefficients[key] = _decimal(number, canonical=canonical)
    new_auto = {"formula": formula, "ceiling": _decimal(auto["ceiling"], canonical=canonical),
                "floor": _decimal(auto["floor"], canonical=canonical), "coef": new_coefficients}

    schedule = _object(settings["schedule"], {"enabled", "night_start_time", "day_start_time", "catch_up_minutes"})
    new_schedule = {"enabled": _boolean(schedule["enabled"]), "night_start_time": _time(schedule["night_start_time"]),
                    "day_start_time": _time(schedule["day_start_time"]), "catch_up_minutes": _integer(schedule["catch_up_minutes"])}
    symbols = settings["symbols"]
    if type(symbols) is not list or not 0 <= len(symbols) <= MAX_SYMBOLS:
        _error("Saved symbol list exceeds the supported capacity")
    new_symbols, seen = [], set()
    for row in symbols:
        _object(row, _SYMBOL_REQUIRED, {"code_new"})
        code = _code(row["code"])
        if code in seen:
            _error("Repeated saved contract identifier")
        seen.add(code)
        new_row = {"name": _text(row["name"]), "code": code, "order_volume": _integer(row["order_volume"])}
        new_row.update({key: _decimal(row[key], canonical=canonical, ratio=key == "half_close_ratio") for key in _SYMBOL_DECIMALS})
        if "code_new" in row:
            new_row["code_new"] = _code(row["code_new"])
        new_symbols.append(new_row)
    return {"trade": normalized, "auto_offset": new_auto, "schedule": new_schedule, "symbols": new_symbols}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error("Duplicate JSON configuration key")
        result[key] = value
    return result


def _source_integer(token):
    if len(token.removeprefix("-")) > 18:
        _error("Invalid JSON configuration integer")
    if token == "-0":
        return Decimal("-0")
    return int(token)


def _source_decimal(token):
    value = Decimal(token)
    # Bound exponent before normalization, including zero with a huge exponent.
    if not value.is_finite() or not -24 <= value.as_tuple().exponent <= 24:
        _error("Invalid JSON configuration decimal")
    return value


def _source_loads(raw):
    if type(raw) not in {str, bytes}:
        _error("Expected explicit JSON text or bytes")
    try:
        text = raw.decode("utf-8-sig", errors="strict") if type(raw) is bytes else raw.removeprefix("\ufeff")
        if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
            _error("Configuration JSON exceeds the supported capacity")
        return json.loads(text, object_pairs_hook=_pairs, parse_int=_source_integer, parse_float=_source_decimal,
                          parse_constant=lambda _: _error("Non-finite configuration number"))
    except TraderError:
        raise
    except (UnicodeError, json.JSONDecodeError, InvalidOperation, RecursionError, ValueError, TypeError, OverflowError):
        _error("Malformed configuration JSON")


def _native_decimal(text):
    number = Decimal(text)
    if number == number.to_integral_value():
        return int(number)
    result = float(number)
    if not math.isfinite(result) or Decimal(str(result)) != number:
        _error("Use to_json() to export this decimal without a lossy float conversion")
    return result


def _native_settings(settings):
    for key in _DECIMALS:
        settings["trade"][key] = _native_decimal(settings["trade"][key])
    for key in ("floor", "ceiling"):
        settings["auto_offset"][key] = _native_decimal(settings["auto_offset"][key])
    settings["auto_offset"]["coef"] = {k: _native_decimal(v) for k, v in settings["auto_offset"]["coef"].items()}
    for row in settings["symbols"]:
        for key in _SYMBOL_DECIMALS:
            row[key] = _native_decimal(row[key])
    return settings


@dataclass(frozen=True, slots=True, init=False)
class DsvProfile:
    """Immutable parameter values; availability is not execution readiness."""

    _record_bytes: bytes

    def __new__(cls, *args, **kwargs):
        raise TypeError("Use a validated DsvProfile factory")

    @classmethod
    def from_settings(cls, settings):
        return cls._make(_normalize(settings))

    @classmethod
    def from_json(cls, raw):
        """Read a business-only export; full DSV files require from_dsv_json()."""
        return cls.from_settings(_source_loads(raw))

    @classmethod
    def from_dsv_json(cls, raw):
        """Explicitly strip known private/non-business sections from a DSV file."""
        source = _source_loads(raw)
        _object(source, SECTIONS, EXCLUDED_SOURCE_SECTIONS)
        return cls.from_settings({key: source[key] for key in SECTIONS})

    @classmethod
    def from_payload(cls, payload):
        _object(payload, {"schema_version", "settings"})
        if payload["schema_version"] != SCHEMA_VERSION:
            _error("Unsupported saved configuration schema version")
        return cls._make(_normalize(payload["settings"], canonical=True))

    from_record = from_payload

    @classmethod
    def _make(cls, settings):
        instance = object.__new__(cls)
        object.__setattr__(instance, "_record_bytes", canonical_bytes({"schema_version": SCHEMA_VERSION, "settings": settings}))
        return instance

    @property
    def content_hash(self):
        return content_hash(self.to_record())

    def to_payload(self):
        return strict_loads(self._record_bytes)

    to_record = to_payload

    def to_settings(self):
        """Return a fresh native-number copy; exact large decimals use to_json()."""
        return _native_settings(self.to_record()["settings"])

    def to_json(self):
        """Export only business values as exact numeric JSON, with no secrets."""
        settings = self.to_record()["settings"]
        # Decimal tokens are tagged internally only while constructing this export.
        class Number(str):
            pass
        for key in _DECIMALS:
            settings["trade"][key] = Number(settings["trade"][key])
        for key in ("floor", "ceiling"):
            settings["auto_offset"][key] = Number(settings["auto_offset"][key])
        settings["auto_offset"]["coef"] = {k: Number(v) for k, v in settings["auto_offset"]["coef"].items()}
        for row in settings["symbols"]:
            for key in _SYMBOL_DECIMALS:
                row[key] = Number(row[key])
        def encode(value):
            if type(value) is Number:
                return str(value)
            if type(value) is dict:
                return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + encode(v) for k, v in sorted(value.items())) + "}"
            if type(value) is list:
                return "[" + ",".join(encode(v) for v in value) + "]"
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return encode(settings)

    def diagnostics(self):
        settings = self.to_record()["settings"]
        trade = settings["trade"]
        result = [{"code": "DSV_BEHAVIOR_UNVERIFIED", "severity": "INFO", "path": "settings",
                   "message": "已保留DSV参数值；配置存在不表示交易行为已核实或已获执行权限。"}]
        for key in ("hysteresis_points", "max_quote_age_seconds", "risk_reference_equity"):
            if trade[key] == "0":
                result.append({"code": "ZERO_SEMANTICS_UNVERIFIED", "severity": "WARNING", "path": "trade." + key,
                               "message": "保存值为0；保持原值，尚未将其解释为自动、关闭或无限。"})
        for switch, parameters in (("live_protect", ["live_max_lots", "live_daily_loss_limit"]),
                                   ("max_symbols_enabled", ["max_symbols_on"]),
                                   ("half_close_enabled", ["half_close_ratio"]),
                                   ("half_stage2", ["half_stage2_trigger_ratio"]),
                                   ("stop_cooldown_enabled", ["stop_cooldown_minutes"])):
            if not trade[switch]:
                result.append({"code": "DORMANT_SAVED_PARAMETERS", "severity": "INFO", "path": "trade." + switch,
                               "parameter_paths": ["trade." + key for key in parameters],
                               "message": "开关关闭，预填参数已保留；不作为已启用限制或动作。"})
        if trade["auto_start_on_launch"]:
            result.append({"code": "START_AUTHORITY_UNVERIFIED", "severity": "INFO", "path": "trade.auto_start_on_launch",
                           "message": "自动启动保存为开启；未由此推定自动恢复开仓权限。"})
        for index, row in enumerate(settings["symbols"]):
            path = f"symbols[{index}]"
            if any(Decimal(row[key]) == 0 for key in ("open_offset", "stop_offset", "profit_offset")):
                result.append({"code": "SYMBOL_OFFSETS_INCOMPLETE", "severity": "WARNING", "path": path,
                               "symbol_code": row["code"], "message": "开仓、止损或止盈偏移含0，保留为待配置行，不补造数值。"})
            if row["hysteresis_points"] == "0":
                result.append({"code": "ZERO_SEMANTICS_UNVERIFIED", "severity": "WARNING", "path": path + ".hysteresis_points",
                               "symbol_code": row["code"], "message": "滞回0值保持原样，未选择自动或关闭。"})
            if "code_new" in row:
                result.append({"code": "SAVED_ROLLOVER_TARGET", "severity": "INFO", "path": path + ".code_new",
                               "symbol_code": row["code"], "message": "保留换月目标字段，不表示已换仓或已启用新合约。"})
        if any(row["half_close_ratio"] != trade["half_close_ratio"] for row in settings["symbols"]):
            result.append({"code": "HALF_RATIO_SCOPE_UNVERIFIED", "severity": "WARNING", "path": "trade.half_close_ratio",
                           "message": "全局与品种同名半仓参数不同；分别保留，不推定其含义或覆盖关系。"})
        return result

    def summary(self):
        settings = self.to_record()["settings"]
        symbols = []
        for row in settings["symbols"]:
            symbols.append(row | {"parameter_status": "COMPLETE" if all(Decimal(row[k]) > 0 for k in ("open_offset", "stop_offset", "profit_offset")) else "INCOMPLETE"})
        configured = sum(row["parameter_status"] == "COMPLETE" for row in symbols)
        return {"schema_version": SCHEMA_VERSION, "profile_hash": self.content_hash, "values_only": True,
                "execution_ready": False, "symbol_count": len(symbols), "configured_symbol_count": configured,
                "incomplete_symbol_count": len(symbols) - configured, "trade": settings["trade"],
                "auto_offset": settings["auto_offset"], "schedule": settings["schedule"], "symbols": symbols,
                "diagnostics": self.diagnostics()}
