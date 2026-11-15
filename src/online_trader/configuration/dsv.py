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
_OPTIONAL_DURATIONS = {"passive_wait_seconds", "resume_gap_seconds"}
# Preserve missing optional fields in canonical records and their historical hashes.
# The replacement runner applies the source default for stop_day_on_profit at use.
_OPTIONAL_SWITCHES = {"half_pullback_close", "stop_day_on_profit"}
_RATIOS = frozenset(key for key in _DECIMALS if key.endswith("_ratio")) | {"total_margin_cap"}
_SYMBOL_REQUIRED = frozenset({"name", "code", "open_offset", "stop_offset", "profit_offset", "order_volume", "half_close_ratio", "hysteresis_points"})
_SYMBOL_DECIMALS = ("open_offset", "stop_offset", "profit_offset", "half_close_ratio", "hysteresis_points")
_CODE = re.compile(r"[A-Z]{2,8}\.[A-Za-z][A-Za-z0-9]{1,31}\Z")
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{0,63}\Z")
_COEFFICIENT_KEY = re.compile(r"\w[\w.:-]{0,63}\Z", re.UNICODE)
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")

# DEFAULT_CONFIG (gui 2222–2334), with the two verified runtime fallbacks.
_DSV_DEFAULT_TRADE = {
    "max_volume": 30, "max_risk_ratio": 0.85, "max_daily_orders": 200, "max_net_loss_stops": 2,
    "close_before_seconds": 20, "trailing_activate_ratio": 0.8, "trailing_pullback_ratio": 0.1,
    "stop_day_on_trailing": True, "protect_80_enabled": True, "protect_80_ratio": 0.8,
    "protect_keep_ratio": 0.5, "protect_stop_day": True, "entry_slip_abort": True,
    "entry_slip_abort_ratio": 0.4, "entry_slip_abort_stop_day": False, "half_close_enabled": False,
    "cont_stop_day_stop": True, "continuation_entry": True, "continuation_ignore_no_open": False,
    "no_chase_after_act": True, "manual_close_day_stop": False, "tick_diag": False, "risk_per_trade_ratio": 0.002,
    "stale_force_close_seconds": 180, "stale_close_day_stop": False, "aggressive_mode": False,
    "passive_entry_first": False, "auto_start_on_launch": True, "chase_slip_ratio": 0.05,
    "chase_base_now": False, "min_one_lot_fallback": True, "stop_cooldown_enabled": False,
    "stop_cooldown_minutes": 5, "require_trigger_update": False, "max_symbols_enabled": False,
    "max_symbols_on": 4, "rollover_stop_enabled": True, "live_protect": False, "live_max_lots": 2,
    "live_daily_loss_limit": 5000, "live_first_one_lot": False, "half_close_ratio": 0.5,
    "manual_tp_drop_ratio": 0.1, "month_stop_days": 4, "night_session_close": "23:00",
    "holidays": [], "entry_slip_ratio": 0.03, "chase_window_minutes": 1, "no_chase_tolerance": 1,
    "no_open_before_close_minutes": 10, "no_open_after_open": True, "no_open_after_open_minutes": 5,
    "open_jump_block_ratio": 0.8, "hysteresis_points": 0, "half_stage2": False,
    "half_stage2_trigger_ratio": 0.6, "half_breakeven": False, "total_margin_cap": 0.7,
    "position_sync_seconds": 2, "participate_auction": False, "close_price_mode": "对手价",
    "cancel_on_stop": True, "margin_rate_alert_threshold": 0.005, "max_quote_age_seconds": 0,
    "min_days_to_expiry": 5, "limit_fallback_ratio": 0.01, "open_gap_max_ratio": 0.2,
    "entry_confirm_timeout": 60, "risk_reference_equity": 0,
}
_DSV_DEFAULT_AUTO = {"formula":"minrange", "ceiling":0.5, "floor":0.4, "coef":{}}
_DSV_DEFAULT_SCHEDULE = {"enabled":True, "night_start_time":"20:55", "day_start_time":"08:55", "catch_up_minutes":60}
_DSV_MIGRATION_MARKERS = {"chase_slip_migrated", "no_open_migrated", "half_pullback_migrated",
                          "no_open_before_open", "no_open_before_open_minutes"}
_DSV_DEFAULT_SYMBOLS = (
    ("棕榈油","DCE.p2701",55,16,50), ("塑料","DCE.l2701",79,20,72),
    ("沥青","SHFE.bu2701",79,12,72), ("菜油","CZCE.OI701",58,15,53),
)


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
    trade = _object(settings["trade"], _TRADE_KEYS, _OPTIONAL_SWITCHES | _OPTIONAL_DURATIONS)
    normalized = {key: _boolean(trade[key]) for key in _BOOLEANS}
    normalized.update({key: _integer(trade[key]) for key in _INTEGERS})
    normalized.update({key: _decimal(trade[key], canonical=canonical, ratio=key in _RATIOS) for key in _DECIMALS})
    for key in _OPTIONAL_DURATIONS & trade.keys():
        normalized[key] = _decimal(trade[key], canonical=canonical)
    for key in _OPTIONAL_SWITCHES & trade.keys():
        normalized[key] = _boolean(trade[key])
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
    for key in (_DECIMALS | _OPTIONAL_DURATIONS) & settings["trade"].keys():
        settings["trade"][key] = _native_decimal(settings["trade"][key])
    for key in ("floor", "ceiling"):
        settings["auto_offset"][key] = _native_decimal(settings["auto_offset"][key])
    settings["auto_offset"]["coef"] = {k: _native_decimal(v) for k, v in settings["auto_offset"]["coef"].items()}
    for row in settings["symbols"]:
        for key in _SYMBOL_DECIMALS:
            row[key] = _native_decimal(row[key])
    return settings


def _dsv_saved_settings(source):
    """Interpret only business values from the original application's load_config."""
    _object(source, (), SECTIONS | EXCLUDED_SOURCE_SECTIONS)
    trade = dict(_object(source.get("trade", {}), (),
                         _TRADE_KEYS | _OPTIONAL_SWITCHES | _OPTIONAL_DURATIONS | _DSV_MIGRATION_MARKERS))
    chase_migrated = trade.pop("chase_slip_migrated", False)
    chase = trade.get("chase_slip_ratio")
    if type(chase) in {int, float, Decimal} and abs(Decimal(str(chase)) - Decimal("0.03")) < Decimal("1e-9") and not chase_migrated:
        trade["chase_slip_ratio"] = 0.05
    if trade.get("no_open_after_open") is False and not trade.pop("no_open_migrated", False):
        trade["no_open_after_open"] = True
    trade.pop("no_open_migrated", None)
    if trade.get("half_pullback_close") is True and not trade.pop("half_pullback_migrated", False):
        trade["half_pullback_close"] = False
    trade.pop("half_pullback_migrated", None)
    for old, new in (("no_open_before_open","no_open_after_open"),
                     ("no_open_before_open_minutes","no_open_after_open_minutes")):
        if old in trade:
            trade.setdefault(new, trade.pop(old))
    for key, old, new in (("max_quote_age_seconds",120,0), ("max_volume",20,30),
                          ("max_daily_orders",20,200), ("entry_slip_ratio",Decimal("0.05"),0.03),
                          ("chase_window_minutes",3,1)):
        if trade.get(key) == old:
            trade[key] = new
    if trade.get("stale_close_day_stop") is True:
        trade["stale_close_day_stop"] = False
    trade = _DSV_DEFAULT_TRADE | trade

    schedule = _DSV_DEFAULT_SCHEDULE | _object(source.get("schedule", {}), (), _DSV_DEFAULT_SCHEDULE)
    if schedule["enabled"] is False:
        schedule["enabled"] = True
    automatic = _DSV_DEFAULT_AUTO | _object(source.get("auto_offset", {}), (), _DSV_DEFAULT_AUTO)
    if automatic["formula"] in ("atr20_main", "range3x09_contract"):
        automatic["formula"] = "minrange"

    symbols = source.get("symbols")
    if "symbols" not in source:
        fields = ("name","code","open_offset","stop_offset","profit_offset")
        symbols = [dict(zip(fields, values)) for values in _DSV_DEFAULT_SYMBOLS]
    if type(symbols) is not list or len(symbols) > MAX_SYMBOLS:
        _error("Saved symbol list exceeds the supported capacity")
    rows = []
    for raw in symbols:
        row = dict(_object(raw, _SYMBOL_REQUIRED - {"order_volume","half_close_ratio","hysteresis_points"}, _SYMBOL_REQUIRED | {"code_new"}))
        pending = row.pop("code_new", None)
        if isinstance(pending, str):
            pending = pending.strip()
        if pending == row["code"]:
            row["code_new"] = pending
        elif pending:
            row["code"] = pending
        row.setdefault("order_volume", 0)
        if row.get("half_close_ratio") is None:
            row["half_close_ratio"] = trade["half_close_ratio"]
        if row.get("hysteresis_points") is None:
            row["hysteresis_points"] = 0
        rows.append(row)
    return {"trade":trade, "schedule":schedule, "auto_offset":automatic, "symbols":rows}


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
        return cls.from_settings(_dsv_saved_settings(_source_loads(raw)))

    @classmethod
    def from_preview_json(cls, raw):
        """Preserve business exports and recognize files with original DSV fields."""
        source = _source_loads(raw)
        if isinstance(source, dict):
            trade = source.get("trade")
            if (EXCLUDED_SOURCE_SECTIONS.intersection(source)
                    or (isinstance(trade, dict) and _DSV_MIGRATION_MARKERS.intersection(trade))):
                source = _dsv_saved_settings(source)
        return cls.from_settings(source)

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
        for key in (_DECIMALS | _OPTIONAL_DURATIONS) & settings["trade"].keys():
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
