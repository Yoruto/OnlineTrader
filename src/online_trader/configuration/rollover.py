"""DSV contract reminders and an explicit, non-trading rollover preview."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from online_trader.configuration.dsv import DsvProfile
from online_trader.errors import TraderError


CHINA_TIME = timezone(timedelta(hours=8))
_FUTURE = re.compile(r"([A-Z]{2,8})\.([A-Za-z]{1,16})[0-9]{1,2}(0[1-9]|1[0-2])\Z")
_EXPIRY = re.compile(r"([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})(?:$|[T ])")


def future_product(code):
    """Return the exact exchange/product pair of a dated futures contract."""
    match = _FUTURE.fullmatch(code) if isinstance(code, str) else None
    return match.group(1, 2) if match else None


def expiry_date(raw):
    """Decode DSV expiry text and epoch seconds, ms, us or ns."""
    if isinstance(raw, datetime):
        return raw.astimezone(CHINA_TIME).date() if raw.tzinfo is not None else raw.date()
    if isinstance(raw, date):
        return raw
    if type(raw) not in {str, int, float, Decimal}:
        return None
    value = str(raw).strip()
    if len(value) > 48:
        return None
    try:
        if len(value) == 8 and value.isascii() and value.isdecimal():
            return date(int(value[:4]), int(value[4:6]), int(value[6:]))
        text = _EXPIRY.match(value)
        if text:
            return date(*map(int, text.groups()))
        stamp = Decimal(value)
        if not stamp.is_finite() or stamp <= 0 or stamp.adjusted() > 20:
            return None
        for units in (1_000_000_000, 1_000_000, 1_000, 1):
            seconds = stamp / units
            if 946_684_800 <= seconds <= 4_102_444_800:
                return datetime.fromtimestamp(int(seconds), CHINA_TIME).date()
    except (InvalidOperation, OverflowError, ValueError, OSError):
        pass
    return None


def expiry_warning(raw, today):
    expires = expiry_date(raw)
    if expires is None:
        return "到期日未取到（可不换月）", False
    days = (expires - today).days
    if days < 0:
        return f"⚠ 已过最后交易日（{expires}）", True
    if days == 0:
        return f"⚠ 今天是最后交易日（{expires}）", True
    if days <= 3:
        return f"⚠ 还有 {days} 天到期（{expires}），请准备换月", True
    if days <= 30:
        return f"{expires} 到期（还有 {days} 天）", False
    return f"{expires} 到期", False


def rollover_hint(code, main_contract, activity):
    """Compare OI and volume using DSV's source-ordered leader selection."""
    product = future_product(code)
    if product is None:
        return {}
    main = main_contract if future_product(main_contract) == product else None
    result = {"main_contract": main, "candidate": None, "candidate_basis": None}
    if main and main != code:
        result.update(candidate=main, candidate_basis="MAIN")
    current_oi, current_volume = activity.get(code, (None, None))
    comparable = {candidate: measures for candidate, measures in activity.items()
                  if future_product(candidate) == product}
    if current_oi is None or current_oi < 0 or not comparable:
        return result
    oi_leader = max(comparable, key=lambda candidate: comparable[candidate][0]
                    if comparable[candidate][0] is not None else -1)
    volume_leader = max(comparable, key=lambda candidate: comparable[candidate][1]
                        if comparable[candidate][1] is not None else -1)
    if oi_leader == code:
        return result
    if comparable[oi_leader][0] > current_oi:
        result.update(candidate=oi_leader, candidate_basis="OPEN_INTEREST")
    leader_volume = comparable[volume_leader][1]
    if (volume_leader != code and leader_volume is not None and
            leader_volume >= 1000 and leader_volume > max(current_volume or 0, 0)):
        result.update(candidate=volume_leader, candidate_basis="VOLUME")
    positive = {candidate: measures for candidate, measures in comparable.items()
                if all(value is not None and value > 0 for value in measures)}
    if current_volume is not None and current_volume >= 0 and positive:
        dual_leader = max(positive, key=lambda candidate: positive[candidate][0])
        oi, volume = positive[dual_leader]
        if dual_leader != code and oi > current_oi and volume > current_volume:
            result.update(candidate=dual_leader, candidate_basis="DUAL")
    return result


def preview_rollover(profile, old_code, new_code):
    """Consume a selected replacement in a draft while retaining the offsets."""
    old_product = future_product(old_code)
    if (not isinstance(profile, DsvProfile) or old_code == new_code or
            old_product is None or old_product != future_product(new_code)):
        raise TraderError("E_CONFIG", "请选择同一交易所、同一品种的不同月份合约。")
    record = profile.to_record()
    symbols = record["settings"]["symbols"]
    old = next((row for row in symbols if row["code"] == old_code), None)
    if old is None or any(row["code"] == new_code for row in symbols):
        raise TraderError("E_CONFIG", "原合约已不在预览中，或换月目标已在品种列表中。")
    old["code"] = new_code
    old.pop("code_new", None)
    return DsvProfile.from_record(record)
