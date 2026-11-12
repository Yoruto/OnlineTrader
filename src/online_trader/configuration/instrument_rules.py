"""Reference-only broker rule table matching DSV's margin PDF columns."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from online_trader.configuration.symbol_csv import ALIASES
from online_trader.domain.values import decimal_text, decimal_value
from online_trader.errors import TraderError


MAX_RULE_PAGES = 16
MAX_RULE_TEXT = 262_144
EXCHANGE_LABELS = {
    'CFFEX': '中金所', 'SHFE': '上期所', 'DCE': '大商所',
    'CZCE': '郑商所', 'INE': '能源中心', 'GFEX': '广期所',
}
PRODUCT_EXCHANGES = {product.upper(): exchange for exchange, product in ALIASES.values()}
_CODE = re.compile(r'^[A-Za-z]{1,3}★?$')
_NAME = re.compile(r'^[\u4e00-\u9fffA-Za-z0-9（）()]{1,16}$')
_NUMBER = re.compile(r'^[0-9]+(?:\.[0-9]+)?')
_PERCENT = re.compile(r'^(\d+(?:\.\d+)?)%')
_CONTRACT = re.compile(r'([A-Z]+)\.([a-zA-Z]{1,3})[0-9]{3,4}\Z')


@dataclass(frozen=True)
class ProductRule:
    name: str
    code: str
    multiplier: Decimal
    tick: Decimal
    exchange_margin: Decimal
    company_margin: Decimal

    @property
    def exchange(self):
        return PRODUCT_EXCHANGES[self.code]

    def record(self):
        return {
            'name': self.name, 'code': self.code,
            'multiplier': decimal_text(self.multiplier), 'tick': decimal_text(self.tick),
            'exchange_margin': decimal_text(self.exchange_margin),
            'company_margin': decimal_text(self.company_margin),
        }

    @classmethod
    def from_record(cls, row):
        if not isinstance(row, dict) or set(row) != {'name','code','multiplier','tick','exchange_margin','company_margin'}:
            raise TraderError('E_INSTRUMENT_RULES', '规则表中的品种字段不完整')
        if not isinstance(row['name'], str) or not _NAME.fullmatch(row['name']) or row['code'] not in PRODUCT_EXCHANGES:
            raise TraderError('E_INSTRUMENT_RULES', '规则表中的品种身份无效')
        values = [decimal_value(row[key]) for key in ('multiplier','tick','exchange_margin','company_margin')]
        multiplier, tick, exchange_margin, company_margin = values
        if multiplier <= 0 or tick <= 0 or not 0 <= exchange_margin <= 1 or not 0 <= company_margin <= 1:
            raise TraderError('E_INSTRUMENT_RULES', '规则表中的合约单位、最小变动或保证金率无效')
        return cls(row['name'], row['code'], *values)


def product_code(contract):
    match = _CONTRACT.fullmatch(contract)
    if match is not None and PRODUCT_EXCHANGES.get(match[2].upper()) == match[1]:
        return match[2].upper()
    return None


def _skip_line(line):
    return (not line or '合约单位' in line or '夜盘' in line or '价位整' in line
            or line.startswith(('品种', '交易', '上', '易', '深', '郑', '大')))


def _identity(tokens, following):
    if not tokens or not _NAME.fullmatch(tokens[0]):
        return None
    for offset, value in enumerate(tokens[1:4], 1):
        if _CODE.fullmatch(value):
            return tokens[0], value.rstrip('★').upper(), 0, offset
    if tokens[0].rstrip('★').upper() in PRODUCT_EXCHANGES:
        return None
    for offset, values in enumerate(following[:5], 1):
        code = values[0].rstrip('★').upper()
        if code in PRODUCT_EXCHANGES:
            name = re.sub(r'^\d+手?', '', tokens[0]) or tokens[0]
            return name, code, offset, 0
    return None


def _decimals(tokens, pattern):
    return [Decimal(match[0].rstrip('%')) for token in tokens
            if len(token) <= 64 and (match := pattern.match(token.replace(',', ''))) is not None]


def parse_rule_pages(pages):
    """Read the first 16 pages, including DSV's wrapped name/code and data rows."""
    text = '\n'.join(pages[:MAX_RULE_PAGES])
    if len(text) > MAX_RULE_TEXT:
        raise TraderError('E_INSTRUMENT_RULES', 'PDF 规则表文本超出支持范围')
    lines = [line.split() for raw in text.splitlines() if not _skip_line(line := raw.strip())]
    rows = {}
    for index, tokens in enumerate(lines):
        identity = _identity(tokens, lines[index + 1:index + 6])
        if identity is None:
            continue
        name, code, line_offset, code_index = identity
        if code in rows or code not in PRODUCT_EXCHANGES:
            continue
        start = index + line_offset
        merged = lines[start][code_index + 1:].copy()
        for following in lines[start + 1:start + 9]:
            if (_NAME.fullmatch(following[0]) and any(_CODE.fullmatch(value) for value in following[1:4])
                    or following[0].rstrip('★').upper() in PRODUCT_EXCHANGES):
                break
            merged.extend(following)
        unit_index = next((i for i, token in enumerate(merged) if '元' in token or '点' in token), None)
        if unit_index is None:
            continue
        values = merged[unit_index + 1:]
        numbers, percentages = _decimals(values, _NUMBER), _decimals(values, _PERCENT)
        if len(numbers) < 2 or len(percentages) < 3:
            continue
        multiplier, tick = numbers[:2]
        exchange_margin, company_margin = (value / 100 for value in percentages[1:3])
        if multiplier > 0 and tick > 0 and 0 <= exchange_margin <= 1 and 0 <= company_margin <= 1:
            rule = ProductRule(name, code, multiplier, tick, exchange_margin, company_margin)
            try:
                rows[code] = ProductRule.from_record(rule.record())
            except TraderError:
                continue
    if not rows:
        raise TraderError('E_INSTRUMENT_RULES', 'PDF 未解析出 DSV 版式的品种规则，请检查是否为可选中文本的规则表')
    return tuple(rows.values())


def _positive(value):
    try:
        result = Decimal(value)
        if result.is_finite() and result > 0:
            return result
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def rule_display(quote, rule, margin_add, fee_add):
    """Resolve live display values and DSV table fallback with independent provenance."""
    price = _positive(quote.get('last')) if quote.get('fresh') else None
    multiplier = _positive(quote.get('volume_multiple'))
    margin = _positive(quote.get('margin'))
    fee = _positive(quote.get('commission')) if quote.get('fresh') else None
    live = margin / (price * multiplier) if price is not None and multiplier is not None and margin is not None else None
    table_exchange = rule.exchange_margin if rule and rule.exchange_margin > 0 else None
    table_company = rule.company_margin if rule and rule.company_margin > 0 else None
    difference = live - table_exchange if live is not None and table_exchange is not None else None
    return {
        'exchange_margin': live if live is not None else table_exchange,
        'company_margin': live + margin_add / 100 if live is not None else table_company,
        'margin_source': '行情' if live is not None else '规则表' if table_exchange is not None or table_company is not None else '',
        'fee': fee, 'company_fee': fee + fee_add if fee is not None else None,
        'difference': difference if difference is not None and abs(difference) > Decimal('0.005') else None,
    }
