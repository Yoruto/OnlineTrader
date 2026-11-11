"""Typed offline form conversion; a successful edit returns a new preview only."""
from decimal import Decimal
import json
import re

from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.symbol_csv import resolve_contract
from online_trader.domain.values import decimal_text, decimal_value
from online_trader.errors import TraderError


class EditError(TraderError):
    def __init__(self, path, reason):
        self.path, self.reason = path, reason
        super().__init__("E_CONFIG_EDIT", f"Invalid parameter at {path}: {reason}")


def number_text(value, path):
    if type(value) is not str or not re.fullmatch(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?", value.strip()):
        raise EditError(path, "nonnegative_decimal")
    result = decimal_text(Decimal(value.strip()))
    decimal_value(result)
    return result


def integer_text(value, path):
    if type(value) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,9}", value.strip()):
        raise EditError(path, "nonnegative_integer")
    parsed = int(value.strip())
    if parsed > 2_147_483_647: raise EditError(path, "integer_limit")
    return parsed


def form_values(profile):
    """Editable values: native booleans; every other scalar has explicit text."""
    if not isinstance(profile, DsvProfile): raise EditError("settings", "form_shape")
    settings = profile.to_record()["settings"]
    for section in ("trade", "auto_offset", "schedule"):
        for key, value in settings[section].items():
            if type(value) is int: settings[section][key] = str(value)
            elif type(value) in {list, dict}: settings[section][key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for row in settings["symbols"]:
        row["order_volume"] = "自动" if row["order_volume"] == 0 else str(row["order_volume"])
        row.setdefault("code_new", "")
    return settings


def _json(raw, path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise EditError(path, "duplicate_json_key")
            result[key] = value
        return result
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > 32_768:
            raise EditError(path, "json_limit")
        return json.loads(raw, object_pairs_hook=pairs, parse_float=lambda token: token, parse_int=lambda token: token,
                          parse_constant=lambda _: (_ for _ in ()).throw(EditError(path, "finite_number")))
    except EditError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise EditError(path, "json_format") from None


def profile_from_form(profile, values):
    if not isinstance(profile, DsvProfile) or type(values) is not dict:
        raise EditError("settings", "form_shape")
    reference = profile.to_record()["settings"]
    if values.keys() != reference.keys(): raise EditError("settings", "form_shape")
    record = profile.to_record(); result = record["settings"]
    for section in ("trade", "auto_offset", "schedule"):
        if type(values[section]) is not dict or values[section].keys() != reference[section].keys():
            raise EditError(section, "form_shape")
        for key, old in reference[section].items():
            path, value = section + "." + key, values[section][key]
            if type(old) is bool:
                if type(value) is not bool: raise EditError(path, "explicit_boolean")
            elif type(old) is int: value = integer_text(value, path)
            elif key == "holidays": value = _json(value, path)
            elif key == "coef":
                source = _json(value, path)
                if type(source) is not dict: raise EditError(path, "coefficient_map")
                value = {}
                for name, coefficient in source.items():
                    if type(coefficient) not in {str, int, Decimal}:
                        raise EditError(path, "coefficient_value")
                    # Keep error paths fixed; do not echo a supplied map key.
                    value[name] = number_text(str(coefficient), path)
            elif key not in {"formula", "night_session_close", "close_price_mode", "night_start_time", "day_start_time"}:
                value = number_text(value, path)
            elif type(value) is not str: raise EditError(path, "text")
            result[section][key] = value
            try: DsvProfile.from_record(record)
            except TraderError: raise EditError(path, "range_or_format") from None
    rows = values["symbols"]
    if type(rows) is not list or len(rows) > 64: raise EditError("symbols", "symbol_limit")
    converted = []
    expected = {"name", "code", "open_offset", "stop_offset", "profit_offset", "order_volume", "half_close_ratio", "hysteresis_points", "code_new"}
    for index, row in enumerate(rows):
        path = f"symbols[{index}]"
        if type(row) is not dict or row.keys() != expected: raise EditError(path, "row_shape")
        new = {}
        for key, value in row.items():
            location = path + "." + key
            if type(value) is not str: raise EditError(location, "text")
            if key == "name": new[key] = value
            elif key in {"code", "code_new"}:
                if key == "code_new" and not value.strip(): continue
                try: new[key] = resolve_contract(value.strip())
                except TraderError: raise EditError(location, "explicit_contract") from None
            elif key == "order_volume": new[key] = 0 if value.strip() == "自动" else integer_text(value, location)
            else: new[key] = number_text(value, location)
        trial = profile.to_record(); trial["settings"]["symbols"] = [new]
        try: new = DsvProfile.from_record(trial).to_record()["settings"]["symbols"][0]
        except TraderError: raise EditError(path, "range_or_format") from None
        converted.append(new)
    result["symbols"] = converted
    try: return DsvProfile.from_record(record)
    except TraderError: raise EditError("symbols", "duplicate_or_invalid_row") from None
