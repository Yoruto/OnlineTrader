"""Whole-batch DSV symbol CSV previews; no live contract discovery or activation."""

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
import re
from types import MappingProxyType

from online_trader.configuration.dsv import DsvProfile, MAX_SYMBOLS
from online_trader.configuration.files import publish_new_bytes
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError


MAX_CSV_BYTES = 262_144
FIELDS = ("name", "code", "open_offset", "stop_offset", "profit_offset", "order_volume",
          "half_close_ratio", "hysteresis_points", "code_new")
HEADERS = ("品种名", "合约代码", "开仓偏移", "止损偏移", "止盈偏移", "下单手数", "半仓比例", "滞回点", "候选换月合约")
_HEADERS = dict(zip(HEADERS, FIELDS)) | {key: key for key in FIELDS} | {"半仓%": "half_close_ratio", "hyst_pts": "hysteresis_points"}
_REQUIRED = frozenset(FIELDS[:5])
_DECIMALS = frozenset({"open_offset", "stop_offset", "profit_offset", "half_close_ratio", "hysteresis_points"})
_NUMBER = re.compile(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,12})?\Z")
# Data facts from DSV SYMBOL_ALIAS, gui.py 3060-3130; source fingerprint in
# docs/parameter-workspace-contract.md. These aliases are not a contract catalog.
_GROUPS = (
 ("SHFE", "cu", "铜", "CU"), ("SHFE", "al", "铝", "AL"), ("SHFE", "zn", "锌", "ZN"),
 ("SHFE", "pb", "铅", "PB"), ("SHFE", "ni", "镍", "NI"), ("SHFE", "sn", "锡", "SN"),
 ("SHFE", "au", "黄金", "AU"), ("SHFE", "ag", "白银", "AG"), ("SHFE", "rb", "螺纹钢", "螺纹", "RB"),
 ("SHFE", "hc", "热卷", "HC"), ("SHFE", "bu", "沥青", "BU"), ("SHFE", "fu", "燃料油", "FU"),
 ("SHFE", "ru", "橡胶", "RU"), ("SHFE", "sp", "纸浆", "SP"), ("SHFE", "ss", "不锈钢", "SS"),
 ("SHFE", "ao", "氧化铝", "AO"), ("DCE", "a", "豆一", "A"), ("DCE", "b", "豆二", "B"),
 ("DCE", "m", "豆粕", "M"), ("DCE", "y", "豆油", "Y"), ("DCE", "p", "棕榈油", "棕榈", "P"),
 ("DCE", "c", "玉米", "C"), ("DCE", "cs", "淀粉", "CS"), ("DCE", "jd", "鸡蛋", "JD"),
 ("DCE", "lh", "生猪", "LH"), ("DCE", "l", "塑料", "L"), ("DCE", "v", "PVC", "V"),
 ("DCE", "pp", "聚丙烯", "PP"), ("DCE", "eg", "乙二醇", "EG"), ("DCE", "eb", "苯乙烯", "EB"),
 ("DCE", "pg", "液化石油气", "PG"), ("DCE", "j", "焦炭", "J"), ("DCE", "jm", "焦煤", "JM"),
 ("DCE", "i", "铁矿石", "I"), ("DCE", "lg", "原木", "LG"),
 ("CZCE", "TA", "PTA", "精对苯二甲酸", "TA"), ("CZCE", "OI", "菜油", "菜籽油", "OI"),
 ("CZCE", "SR", "白糖", "SR"), ("CZCE", "CF", "棉花", "CF"), ("CZCE", "CY", "棉纱", "CY"),
 ("CZCE", "MA", "甲醇", "MA"), ("CZCE", "FG", "玻璃", "FG"), ("CZCE", "SA", "纯碱", "SA"),
 ("CZCE", "ZC", "动力煤", "ZC"), ("CZCE", "RM", "菜粕", "RM"), ("CZCE", "AP", "苹果", "AP"),
 ("CZCE", "CJ", "红枣", "CJ"), ("CZCE", "PK", "花生", "PK"), ("CZCE", "SF", "硅铁", "SF"),
 ("CZCE", "SM", "锰硅", "SM"), ("CZCE", "UR", "尿素", "UR"), ("CZCE", "PF", "短纤", "PF"),
 ("CZCE", "SH", "烧碱", "SH"), ("CZCE", "PX", "对二甲苯", "PX"),
 ("CFFEX", "IF", "沪深300", "IF"), ("CFFEX", "IH", "上证50", "IH"),
 ("CFFEX", "IC", "中证500", "IC"), ("CFFEX", "IM", "中证1000", "IM"),
 ("CFFEX", "T", "十年国债", "T"), ("CFFEX", "TF", "五年国债", "TF"),
 ("CFFEX", "TS", "二年国债", "TS"), ("CFFEX", "TL", "三十国债", "TL"),
 ("INE", "sc", "原油", "SC"), ("INE", "nr", "20号胶", "NR"), ("INE", "lu", "低硫燃油", "LU"),
 ("INE", "bc", "国际铜", "BC"), ("INE", "ec", "集运指数", "EC"),
 ("GFEX", "si", "工业硅", "SI"), ("GFEX", "lc", "碳酸锂", "LC"), ("GFEX", "ps", "多晶硅", "PS"),
)
ALIASES = MappingProxyType({name.casefold(): (exchange, product) for exchange, product, *names in _GROUPS for name in names})
PRODUCT_NAMES = MappingProxyType({f"{exchange}.{product}": names[0] for exchange, product, *names in _GROUPS})


class SymbolCsvError(TraderError):
    def __init__(self, line, field, reason):
        self.line, self.field, self.reason = line, field, reason
        # Fixed field/reason identifiers only: do not echo untrusted cell contents.
        super().__init__("E_SYMBOL_CSV", f"CSV line {line}, field {field}: {reason}")


def match_contract_catalog(value, contracts, *, limit=100):
    """Filter an already-discovered futures catalog by code, product alias or Chinese name."""
    if type(value) is not str or not isinstance(contracts, (list, tuple)):
        return []
    from online_trader.configuration.rollover import future_product

    term = value.strip().casefold()
    if not term:
        return []
    exchange_part, dot, tail = term.partition('.')
    raw_prefix = (tail if dot else term).rstrip('0123456789')
    num_part = (tail if dot else term)[len(raw_prefix):]
    alias = ALIASES.get(raw_prefix)
    if dot and alias is not None and exchange_part.upper() != alias[0]:
        alias = None
    matches = []
    for code in dict.fromkeys(contracts):
        product = future_product(code)
        if product is None:
            continue
        symbol = '.'.join(product)
        name = PRODUCT_NAMES.get(symbol, '').casefold()
        suffix = code[len(symbol):]
        if (term in code.casefold() or term in name
                or (alias == product and (not num_part or suffix.startswith(num_part) or suffix.endswith(num_part)))):
            matches.append(code)
        if len(matches) >= limit:
            break
    return matches


def suggest_contracts(value, contracts=(), *, limit=6, today=None):
    """Return up to ``limit`` matching catalog contracts, or upcoming months for known aliases."""
    if type(value) is not str or limit <= 0:
        return [], False
    if contracts:
        matched = match_contract_catalog(value, sorted(dict.fromkeys(contracts)), limit=limit)
        if matched:
            return matched, True
    term = value.strip().casefold()
    if not term:
        return [], False
    exchange_part, dot, tail = term.partition('.')
    body = tail if dot else term
    prefix = body.rstrip('0123456789')
    digits = body[len(prefix):]
    alias = ALIASES.get(prefix)
    if alias is None or (dot and exchange_part.upper() != alias[0]):
        return [], False
    exchange, product = alias
    reference = today or datetime.now(timezone(timedelta(hours=8))).date()
    start_offset = 1 if reference.day >= 20 else 0
    results = []
    for step in range(start_offset, start_offset + 12):
        month_index = (reference.year * 12 + (reference.month - 1)) + step
        year, month = divmod(month_index, 12)
        month += 1
        suffix = f'{year % 10}{month:02d}' if exchange == 'CZCE' else f'{year % 100:02d}{month:02d}'
        if digits and not suffix.startswith(digits):
            continue
        results.append(f'{exchange}.{product}{suffix}')
        if len(results) >= limit:
            break
    return results, False


def resolve_contract(value):
    """Resolve a DSV alias plus an explicit month; never infer a main contract."""
    if type(value) is not str:
        raise TraderError("E_SYMBOL_CODE", "Expected an explicit contract code")
    exchange, dot, tail = value.partition(".")
    code = tail if dot else value
    # Chinese aliases can contain digits (20号胶, 沪深300), so try each explicit
    # suffix length against the complete alias rather than splitting at first digit.
    matches = set()
    for size in (3, 4):
        alias, month = code[:-size], code[-size:]
        if not re.fullmatch(r"[0-9]{3,4}", month) or not 1 <= int(month[-2:]) <= 12:
            continue
        resolved = ALIASES.get(alias.casefold())
        if resolved and (not dot or exchange.upper() == resolved[0]):
            matches.add(f"{resolved[0]}.{resolved[1]}{month}")
    if len(matches) == 1:
        return matches.pop()
    # Future products remain possible only with a fully qualified, explicit code.
    if (dot and exchange in {row[0] for row in _GROUPS}
            and re.fullmatch(r"[A-Za-z]{1,8}[0-9]{3,4}", tail)
            and 1 <= int(tail[-2:]) <= 12
            and not any(tail[:-size].casefold() in ALIASES for size in (3, 4))):
        return value
    raise TraderError("E_SYMBOL_CODE", "Use an exchange-qualified contract or a DSV alias with an explicit month")


def _number(value, line, field):
    if not _NUMBER.fullmatch(value):
        raise SymbolCsvError(line, field, "expected_nonnegative_decimal")
    try:
        return decimal_text(Decimal(value))
    except (TraderError, InvalidOperation):
        raise SymbolCsvError(line, field, "invalid_decimal") from None


def import_symbols(raw, profile, *, mode="replace", escaped_names=True):
    """Return a complete immutable preview or fail with a physical CSV line.

    Replace changes the list; merge updates matching codes and appends new ones.
    A missing legacy code_new column preserves that field on existing contracts.
    """
    if not isinstance(profile, DsvProfile) or type(mode) is not str or mode not in {"replace", "merge"}:
        raise TraderError("E_SYMBOL_CSV", "Expected validated preview and an explicit import mode")
    try:
        if type(raw) not in {str, bytes}:
            raise SymbolCsvError(1, "file", "expected_utf8_csv")
        text = raw.decode("utf-8-sig", "strict") if type(raw) is bytes else raw.removeprefix("\ufeff")
        if len(text.encode("utf-8")) > MAX_CSV_BYTES:
            raise SymbolCsvError(1, "file", "size_limit")
        reader = csv.reader(StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        if not header:
            raise SymbolCsvError(1, "header", "missing_header")
        fields = [_HEADERS.get(value.strip()) for value in header]
        if None in fields or len(set(fields)) != len(fields) or not _REQUIRED <= set(fields):
            raise SymbolCsvError(1, "header", "unknown_duplicate_or_missing_columns")
        base = profile.to_record()
        existing = {row["code"]: row for row in base["settings"]["symbols"]}
        rows, seen = [], set()
        for cells in reader:
            line = reader.line_num
            if len(cells) != len(fields):
                raise SymbolCsvError(line, "row", "column_count")
            if len(rows) >= MAX_SYMBOLS:
                raise SymbolCsvError(line, "row", "symbol_limit")
            values = dict(zip(fields, cells))
            row = {}
            for field in ("name", "code"):
                value = values[field] if field == "name" else values[field].strip()
                if field == "name":
                    # A reversible apostrophe escape protects CSV opened in Excel.
                    rest = value[1:]
                    if escaped_names and value.startswith("'") and (rest[:1] in {"=", "+", "-", "@", "'"}
                            or rest != rest.strip()): value = rest
                    row[field] = value
                else:
                    try: row[field] = resolve_contract(value)
                    except TraderError: raise SymbolCsvError(line, field, "unresolved_explicit_contract") from None
            if row["code"] in seen:
                raise SymbolCsvError(line, "code", "duplicate_contract")
            seen.add(row["code"])
            for field in _DECIMALS:
                default = "0.5" if field == "half_close_ratio" else "0"
                row[field] = _number(values.get(field, default).strip(), line, field)
            volume = values.get("order_volume", "自动").strip()
            if volume == "自动": row["order_volume"] = 0
            elif re.fullmatch(r"0|[1-9][0-9]{0,9}", volume): row["order_volume"] = int(volume)
            else: raise SymbolCsvError(line, "order_volume", "expected_integer_or_auto")
            if "code_new" in values:
                if values["code_new"].strip():
                    try: row["code_new"] = resolve_contract(values["code_new"].strip())
                    except TraderError: raise SymbolCsvError(line, "code_new", "unresolved_explicit_contract") from None
            elif row["code"] in existing and "code_new" in existing[row["code"]]:
                row["code_new"] = existing[row["code"]]["code_new"]
            # Validate each row in the existing complete profile for precise errors.
            trial = profile.to_record(); trial["settings"]["symbols"] = [row]
            try: row = DsvProfile.from_record(trial).to_record()["settings"]["symbols"][0]
            except TraderError: raise SymbolCsvError(line, "row", "invalid_symbol_values") from None
            rows.append(row)
        if not rows:
            raise SymbolCsvError(reader.line_num, "row", "empty_symbol_list")
        if mode == "merge":
            merged = dict(existing)
            merged.update({row["code"]: row for row in rows})
            rows = list(merged.values())
        if len(rows) > MAX_SYMBOLS:
            raise SymbolCsvError(reader.line_num, "row", "symbol_limit")
        base["settings"]["symbols"] = rows
        return DsvProfile.from_record(base)
    except (UnicodeError, csv.Error):
        raise SymbolCsvError(locals().get("reader").line_num if "reader" in locals() else 1, "file", "malformed_utf8_csv") from None


def load_symbols(path, profile, *, mode="replace"):
    try:
        with Path(path).open("rb") as stream: raw = stream.read(MAX_CSV_BYTES + 1)
    except OSError as error:
        raise TraderError("E_SYMBOL_CSV_IO", "Cannot read the selected symbol CSV") from error
    return import_symbols(raw, profile, mode=mode)


def export_symbols(profile, path):
    if not isinstance(profile, DsvProfile):
        raise TraderError("E_SYMBOL_CSV", "Expected a validated parameter profile")
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(HEADERS)
    for row in profile.to_record()["settings"]["symbols"]:
        name = row["name"]
        if name[0] in "=+-@'" or name != name.strip(): name = "'" + name
        writer.writerow([name, row["code"], *(row[k] for k in FIELDS[2:5]), row["order_volume"],
                         row["half_close_ratio"], row["hysteresis_points"], row.get("code_new", "")])
    return publish_new_bytes(stream.getvalue().encode("utf-8-sig"), path)
