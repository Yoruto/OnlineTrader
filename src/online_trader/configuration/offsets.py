"""Versioned DSV offset formulas over explicit, immutable offline daily bars.

No clock inference, pandas, network, credentials, SDK or trading owner is used.
Rational arithmetic preserves ties-even rounding at exact decimal boundaries.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from fractions import Fraction
from io import StringIO
import csv
from pathlib import Path
import re
from types import MappingProxyType

from online_trader.configuration.dsv import DsvProfile
from online_trader.configuration.editor import number_text
from online_trader.configuration.files import publish_new_bytes
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.errors import TraderError


FORMULA_VERSION = "DSV_OFFSETS/1"
FORMULAS = MappingProxyType({
    "minrange": "双窗平均振幅取小（默认）", "range3x09_contract": "三日振幅原式 · 具体合约",
    "range3x09_main": "三日振幅原式 · 主连", "atr20_main": "主连ATR20",
    "atr_mixw_main": "主连ATR(5/10/20)Wilder", "range3_main": "主连三日振幅均值"})
_NAMES = ("塑料", "沥青", "菜油", "棕榈油", "PTA")
COEFFICIENTS = MappingProxyType({
    "minrange": ("0.45", "0.38", "0.45", "0.45", "0.45"),
    "range3x09_contract": ("0.45", "0.4", "0.45", "0.45", "0.45"),
    "range3x09_main": ("0.45", "0.31", "0.45", "0.45", "0.45"),
    "atr20_main": ("0.36", "0.37", "0.375", "0.39", "0.33"),
    "atr_mixw_main": ("0.355", "0.35", "0.39", "0.38", "0.335"),
    "range3_main": ("0.38", "0.3", "0.445", "0.38", "0.45")})
STOPS = MappingProxyType(dict(zip(_NAMES, (20, 12, 15, 16, 18))))


def dsv_startup_profile(profile):
    """DSV StrategyThread._run 3907-3933 interpretation, for comparison.

    Its integer conversion/zero repair is distinct from the daily calculator.
    Never write the original DSV configuration or an existing protection plan.
    """
    record=profile.to_record()
    for row in record['settings']['symbols']:
        for field,fallback in (('open_offset',100),('stop_offset',15),('profit_offset',60)):
            row[field]=str(int(Decimal(row[field])) or fallback)
    return DsvProfile.from_record(record)

MAX_DAILY_BYTES = 2_097_152
MAX_DAILY_ROWS = 16_384
MAX_SERIES_BARS = 512
DAILY_HEADERS = ("series_id", "date", "high", "low", "close")


def _error(reason, code="E_OFFSET"):
    raise TraderError(code, reason)


def _date(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value: _error("Expected explicit YYYY-MM-DD trading date")
        return value
    except (ValueError, TypeError): _error("Expected explicit YYYY-MM-DD trading date")


def _series(value):
    if type(value) is not str or not re.fullmatch(r"(?:[A-Z]{2,8}\.[A-Za-z]{1,8}[0-9]{3,4}|KQ\.m@[A-Z]{2,8}\.[A-Za-z]{1,8})", value):
        _error("Invalid explicit daily-series identity")
    return value


class DailyCsvError(TraderError):
    def __init__(self, line, reason):
        self.line, self.reason = line, reason
        super().__init__("E_DAILY_CSV", f"Daily CSV line {line}: {reason}")


@dataclass(frozen=True, slots=True)
class DailyDataset:
    _bytes: bytes

    @classmethod
    def from_csv(cls, raw):
        reader = None
        try:
            if type(raw) not in {bytes, str}: raise DailyCsvError(1, "expected_utf8_csv")
            text = raw.decode("utf-8-sig", "strict") if type(raw) is bytes else raw.removeprefix("\ufeff")
            if len(text.encode("utf-8")) > MAX_DAILY_BYTES: raise DailyCsvError(1, "size_limit")
            reader = csv.reader(StringIO(text, newline=""), strict=True)
            header = next(reader, None)
            if not header or len(header) != 5 or set(header) != set(DAILY_HEADERS): raise DailyCsvError(1, "expected_series_date_high_low_close")
            rows, last, counts = [], {}, {}
            for cells in reader:
                line = reader.line_num
                if len(cells) != 5: raise DailyCsvError(line, "column_count")
                source = dict(zip(header, cells))
                try:
                    identity, day = _series(source["series_id"]), _date(source["date"])
                    numbers = {key: number_text(source[key], "daily." + key) for key in ("high", "low", "close")}
                except TraderError: raise DailyCsvError(line, "invalid_series_date_or_decimal") from None
                high, low, close = (Decimal(numbers[key]) for key in ("high", "low", "close"))
                if not 0 < low <= close <= high: raise DailyCsvError(line, "inconsistent_ohlc")
                if identity in last and day <= last[identity]: raise DailyCsvError(line, "duplicate_or_unsorted_date")
                last[identity] = day; counts[identity] = counts.get(identity, 0) + 1
                if len(last) > 128 or counts[identity] > MAX_SERIES_BARS or len(rows) >= MAX_DAILY_ROWS:
                    raise DailyCsvError(line, "row_or_series_limit")
                rows.append({"series_id": identity, "date": day, **numbers})
            if not rows: raise DailyCsvError(reader.line_num, "empty_daily_data")
            rows.sort(key=lambda row: (row["series_id"], row["date"]))
            return cls(canonical_bytes(rows))
        except (UnicodeError, csv.Error):
            raise DailyCsvError(reader.line_num if reader is not None else 1, "malformed_utf8_csv") from None

    @property
    def content_hash(self): return content_hash(self.rows)

    @property
    def rows(self): return strict_loads(self._bytes)

    def summary(self):
        rows = self.rows
        return {"data_hash": self.content_hash, "bars": len(rows), "series": sorted({row["series_id"] for row in rows})}


def load_daily(path):
    try:
        with Path(path).open("rb") as stream: raw = stream.read(MAX_DAILY_BYTES + 1)
    except OSError as error: raise TraderError("E_DAILY_IO", "Cannot read selected offline daily CSV") from error
    return DailyDataset.from_csv(raw)


def formula_source(formula, name, code):
    if type(formula) is not str or formula not in FORMULAS: _error("Unsupported offset formula")
    if formula == "range3x09_contract" or formula == "minrange" and name != "沥青":
        return "contract", code
    match = re.fullmatch(r"([A-Z]{2,8})\.([A-Za-z]{1,8})[0-9]{3,4}", code)
    if not match: _error("Main-series calculation requires an explicit contract")
    return "main", f"KQ.m@{match[1]}.{match[2]}"


def _mean(values): return sum(values, Fraction(0)) / len(values)


def _base(bars, formula, name):
    ranges = [Fraction(row["high"]) - Fraction(row["low"]) for row in bars]
    if formula == "minrange":
        short, long = (20, 20) if name == "沥青" else (3, 30)
        means = [_mean(ranges[-size:]) for size in sorted({short, long}) if len(ranges) >= size]
        if not means: _error("INSUFFICIENT_COMPLETED_BARS")
        return min(means), [short, long], len(ranges) < long
    if formula in {"range3x09_contract", "range3x09_main", "range3_main"}:
        if len(ranges) < 3: _error("INSUFFICIENT_COMPLETED_BARS")
        return _mean(ranges[-3:]), [3], False
    if not ranges: _error("INSUFFICIENT_COMPLETED_BARS")
    true_ranges, previous = [], None
    for row, amplitude in zip(bars, ranges):
        high, low, close = (Fraction(row[key]) for key in ("high", "low", "close"))
        true_ranges.append(max(amplitude, abs(high - previous), abs(low - previous)) if previous is not None else amplitude)
        previous = close
    if formula == "atr20_main":
        if len(true_ranges) < 20: _error("INSUFFICIENT_COMPLETED_BARS")
        return _mean(true_ranges[-20:]), [20], False
    averages = []
    for window in (5, 10, 20):
        value = true_ranges[0]
        for next_value in true_ranges[1:]: value = value * Fraction(window - 1, window) + next_value / window
        averages.append(value)
    return _mean(averages), [5, 10, 20], False


def _display(value):
    with localcontext() as context:
        context.prec = 80
        result = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    text = format(result, "f").rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True, slots=True)
class OffsetCalculation:
    _bytes: bytes

    def to_record(self): return strict_loads(self._bytes)

    @property
    def content_hash(self): return content_hash(self.to_record())

    def apply(self, profile, contracts):
        report = self.to_record()
        if not isinstance(profile, DsvProfile) or profile.content_hash != report["profile_hash"]:
            _error("Preview changed; recalculate before applying offsets", "E_OFFSET_CONFLICT")
        if (type(contracts) not in {list, tuple} or not contracts or any(type(code) is not str for code in contracts)
                or len(contracts) != len(set(contracts))): _error("Select explicit successful contract rows")
        successful = {row["contract_id"]: row for row in report["rows"] if row["status"] == "CALCULATED"}
        if not set(contracts) <= successful.keys(): _error("Selected contract has no valid calculation")
        record = profile.to_record()
        for row in record["settings"]["symbols"]:
            if row["code"] in contracts:
                calculated = successful[row["code"]]
                for field, key in (("open_offset", "open"), ("stop_offset", "stop"), ("profit_offset", "profit")):
                    row[field] = calculated[key]
        record["settings"]["auto_offset"].update(formula=report["formula"], ceiling=report["ceiling"], floor=report["floor"])
        return DsvProfile.from_record(record)

    def text(self):
        report = self.to_record()
        lines = [f"{report['formula_version']} | {report['formula']} | 截止交易日 {report['as_of_date']}", "合约\t来源\t当前开/损/盈\t计算开/损/盈\t状态"]
        for row in report["rows"]:
            current = "/".join(row["current"])
            values = "/".join(row[key] for key in ("open", "stop", "profit")) if row["status"] == "CALCULATED" else row["reason"]
            lines.append(f"{row['contract_id']}\t{row['series_id']}\t{current}\t{values}\t{row['status']}")
        return "\n".join(lines)


def calculate_offsets(profile, dataset, *, as_of_date, realized_ranges=None, formula=None, ceiling=None, floor=None):
    if not isinstance(profile, DsvProfile) or not isinstance(dataset, DailyDataset): _error("Expected validated profile and daily dataset")
    _date(as_of_date)
    settings = profile.to_record()["settings"]; auto = settings["auto_offset"]
    formula = auto["formula"] if formula is None else formula
    if type(formula) is not str or formula not in FORMULAS: _error("Unsupported offset formula")
    ceiling = number_text(auto["ceiling"] if ceiling is None else ceiling, "ceiling")
    floor = number_text(auto["floor"] if floor is None else floor, "floor")
    realized_ranges = {} if realized_ranges is None else realized_ranges
    codes = {row["code"] for row in settings["symbols"]}
    if type(realized_ranges) is not dict or not realized_ranges.keys() <= codes: _error("Unknown contract in realized-range inputs")
    realized = {code: number_text(value, "realized_range") for code, value in realized_ranges.items()}
    series = {}
    for row in dataset.rows:
        if row["date"] < as_of_date: series.setdefault(row["series_id"], []).append(row)
    results = []
    for symbol in settings["symbols"]:
        source, identity = formula_source(formula, symbol["name"], symbol["code"])
        row = {"contract_id": symbol["code"], "name": symbol["name"], "source": source, "series_id": identity,
               "current": [symbol[k] for k in ("open_offset", "stop_offset", "profit_offset")]}
        bars = series.get(identity, [])
        try:
            if not bars: _error("NO_COMPLETED_SOURCE_BARS")
            base, windows, short_only = _base(bars, formula, symbol["name"])
            default = COEFFICIENTS[formula][_NAMES.index(symbol["name"])] if symbol["name"] in _NAMES else "0.35"
            override = auto["coef"].get(symbol["name"])
            coefficient = override if override is not None and Decimal(override) > 0 else default
            cfg = max(round(base * Fraction(coefficient)), 1)
            used = cfg; amplitude = Fraction(realized.get(symbol["code"], "0"))
            if amplitude > 0 and Fraction(ceiling) > 0:
                used = max(int(max(min(cfg, amplitude * Fraction(ceiling)), min(cfg * Fraction(floor), cfg))), 1)
            profit = max(round(base * Fraction(coefficient) * Fraction(9, 10)), 1)
            if max(cfg, used, profit) >= 10 ** 18: _error("CALCULATED_VALUE_OUT_OF_RANGE")
            row.update(status="CALCULATED", coefficient=coefficient, coefficient_source="override" if coefficient == override else "DSV_default",
                base=_display(base), base_exact={"numerator": str(base.numerator), "denominator": str(base.denominator)},
                windows=windows, completed_bars=len(bars), last_completed_date=bars[-1]["date"], short_window_only=short_only,
                open_cfg=str(cfg), open=str(used), stop=str(STOPS.get(symbol["name"], 15)), profit=str(profit),
                capped=used != cfg, realized_range=realized.get(symbol["code"]), cap_used=amplitude > 0 and Fraction(ceiling) > 0)
        except TraderError as error:
            row.update(status="UNAVAILABLE", reason=str(error))
        results.append(row)
    return OffsetCalculation(canonical_bytes({"formula_version": FORMULA_VERSION, "formula": formula,
        "as_of_date": as_of_date, "profile_hash": profile.content_hash, "data_hash": dataset.content_hash,
        "ceiling": ceiling, "floor": floor, "runtime_activation": False, "rows": results}))


def export_calculation(calculation, path):
    if not isinstance(calculation, OffsetCalculation): _error("Expected a calculated preview")
    return publish_new_bytes(canonical_bytes(calculation.to_record()), path)
