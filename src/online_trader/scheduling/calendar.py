"""Immutable, source-labelled trading windows for isolated development.

Calendar completeness is a supplied fact with an explicit coverage interval. This
module never synthesizes weekdays, holidays, night sessions or a cabinet day.
UTC timestamps describe sessions; they do not extend monotonic execution leases.
"""

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import re

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.domain.values import identifier
from online_trader.errors import TraderError


SCHEMA_VERSION = "TRADING_CALENDAR/1"
MAX_CONTRACTS = 256
MAX_SESSIONS_PER_CONTRACT = 4096
MAX_SESSIONS = 16384
MAX_RECORD_BYTES = 4 * 1024 * 1024
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")
_TOP = {"schema_version", "calendar_id", "calendar_version", "complete", "coverage_from_utc", "coverage_to_utc", "contracts"}
_SESSION = {"session_id", "trading_day", "open_utc", "close_utc", "is_opening_session", "force_close_at_end"}


def _error(message):
    raise TraderError("E_CALENDAR_INPUT", message)


def _object(value, keys):
    if type(value) is not dict or set(value) != keys:
        _error("Missing or unsupported calendar fields")
    return value


def _utc(value):
    if type(value) is not str or not _UTC.fullmatch(value):
        _error("Expected an explicit UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise TraderError("E_CALENDAR_INPUT", "Invalid calendar timestamp") from error


def _day(value):
    if type(value) is not str:
        _error("Expected an explicit trading-day label")
    try:
        if date.fromisoformat(value).isoformat() != value:
            _error("Trading day must be an exact YYYY-MM-DD date")
    except ValueError as error:
        raise TraderError("E_CALENDAR_INPUT", "Invalid trading-day label") from error
    return value


def _microseconds(delta):
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


@dataclass(frozen=True, slots=True)
class _Session:
    session_id: str
    trading_day: str
    open_utc: str
    close_utc: str
    is_opening_session: bool
    force_close_at_end: bool
    start: datetime
    end: datetime

    def record(self):
        return {name: getattr(self, name) for name in _SESSION}


@dataclass(frozen=True, slots=True, init=False)
class TradingCalendar:
    """Validated explicit sessions, with immutable data and fresh query records."""

    _record_bytes: bytes
    _start: datetime
    _end: datetime
    _contracts: tuple
    _calendar_id: str
    _calendar_version: str
    _coverage_from_utc: str
    _coverage_to_utc: str
    _complete: bool
    _hash: str

    def __new__(cls, *args, **kwargs):
        raise TypeError("Use TradingCalendar.from_record()")

    @classmethod
    def from_record(cls, record):
        _object(record, _TOP)
        if record["schema_version"] != SCHEMA_VERSION:
            _error("Unsupported trading calendar version")
        identifier(record["calendar_id"]); identifier(record["calendar_version"])
        if type(record["complete"]) is not bool:
            _error("Calendar completeness must be an explicit boolean")
        start, end = _utc(record["coverage_from_utc"]), _utc(record["coverage_to_utc"])
        if start >= end:
            _error("Calendar coverage must be a nonempty forward interval")
        rows = record["contracts"]
        if type(rows) is not list or len(rows) > MAX_CONTRACTS:
            _error("Expected bounded explicit contract coverage")
        contracts = []; names = set(); total = 0
        for row in rows:
            _object(row, {"contract_id", "sessions"})
            contract = identifier(row["contract_id"])
            if contract in names:
                _error("Contract coverage is duplicated")
            names.add(contract)
            periods = row["sessions"]
            if type(periods) is not list or len(periods) > MAX_SESSIONS_PER_CONTRACT:
                _error("Expected a bounded session list")
            total += len(periods)
            if total > MAX_SESSIONS:
                _error("Calendar session capacity exceeded")
            sessions = []; identities = set(); previous = None
            for period in periods:
                _object(period, _SESSION)
                identity = identifier(period["session_id"])
                if identity in identities:
                    _error("Session identity is duplicated within a contract")
                identities.add(identity)
                trading_day = _day(period["trading_day"])
                if type(period["is_opening_session"]) is not bool:
                    _error("Opening-session evidence must be an explicit boolean")
                if type(period["force_close_at_end"]) is not bool:
                    _error("Forced-close endpoints must be explicit boolean policy facts")
                opened, closed = _utc(period["open_utc"]), _utc(period["close_utc"])
                if not start <= opened < closed <= end:
                    _error("Session lies outside its complete coverage or has invalid bounds")
                if _microseconds(closed - opened) > 2_147_483_647_000:
                    _error("Session exceeds the bounded millisecond fact domain")
                if previous is not None and opened < previous:
                    _error("Sessions must be ordered and nonoverlapping within a contract")
                previous = closed
                sessions.append(_Session(identity, trading_day, period["open_utc"], period["close_utc"],
                                         period["is_opening_session"], period["force_close_at_end"], opened, closed))
            contracts.append((contract, tuple(sessions)))
        raw = canonical_bytes(record)
        if len(raw) > MAX_RECORD_BYTES:
            _error("Calendar record exceeds its byte capacity")
        result = object.__new__(cls)
        object.__setattr__(result, "_record_bytes", raw)
        object.__setattr__(result, "_start", start)
        object.__setattr__(result, "_end", end)
        object.__setattr__(result, "_contracts", tuple(contracts))
        object.__setattr__(result, "_calendar_id", record["calendar_id"])
        object.__setattr__(result, "_calendar_version", record["calendar_version"])
        object.__setattr__(result, "_coverage_from_utc", record["coverage_from_utc"])
        object.__setattr__(result, "_coverage_to_utc", record["coverage_to_utc"])
        object.__setattr__(result, "_complete", record["complete"])
        object.__setattr__(result, "_hash", hashlib.sha256(raw).hexdigest())
        return result

    @property
    def content_hash(self):
        return self._hash

    @property
    def calendar_id(self):
        return self._calendar_id

    @property
    def calendar_version(self):
        return self._calendar_version

    def to_record(self):
        """Return a detached record; callers cannot alter validated coverage."""
        return strict_loads(self._record_bytes)

    def session_times(self, contract_id, trading_day):
        """Return the recorded UTC intervals for one cabinet day."""
        sessions = self._scope(contract_id) or ()
        return [[period.open_utc, period.close_utc] for period in sessions if period.trading_day == trading_day]

    def _scope(self, contract_id):
        identifier(contract_id)
        return next((sessions for contract, sessions in self._contracts if contract == contract_id), None)

    def _references(self):
        return {"calendar_id": self.calendar_id, "calendar_version": self.calendar_version,
                "calendar_hash": self.content_hash}

    def query(self, contract_id, at_utc):
        """Resolve a half-open session or report that coverage is unknown.

        Known closed windows return ``is_trading=False``. Incomplete, absent or
        out-of-coverage facts return ``None`` so callers cannot treat unknown
        time as a proven holiday or as permission to reset existing protection.
        """
        now = _utc(at_utc); sessions = self._scope(contract_id)
        refs = self._references()
        result = refs | {"contract_id": contract_id, "query_utc": at_utc,
            "coverage_from_utc": self._coverage_from_utc, "coverage_to_utc": self._coverage_to_utc,
            "quality": "UNKNOWN", "complete": False, "is_trading": None, "trading_day": None,
            "session": None, "next_session": None, "since_open_ms": None, "until_close_ms": None,
            "next_forced_close_utc": None,
            "reasons": []}
        if not self._complete:
            result["reasons"] = ["CALENDAR_INCOMPLETE"]
        elif sessions is None:
            result["reasons"] = ["CONTRACT_COVERAGE_UNKNOWN"]
        elif not self._start <= now < self._end:
            result["reasons"] = ["CALENDAR_COVERAGE_UNKNOWN"]
        else:
            current = next((session for session in sessions if session.start <= now < session.end), None)
            upcoming = next((session for session in sessions if session.start > now), None)
            result.update(quality="COMPLETE", complete=True, is_trading=current is not None)
            if current is not None:
                result.update(session=current.record() | refs, trading_day=current.trading_day,
                    since_open_ms=_microseconds(now - current.start) // 1000,
                    until_close_ms=_microseconds(current.end - now) // 1000)
                forced = next((session for session in sessions if session.trading_day == current.trading_day
                               and session.end > now and session.force_close_at_end), None)
                if forced is not None:
                    result["next_forced_close_utc"] = forced.close_utc
            if upcoming is not None:
                result["next_session"] = upcoming.record() | refs
        return result

    def phase_query(self, contract_id, at_utc):
        """Derive physical market phase (including pre-open auction) without altering TRADING_CALENDAR/1."""
        base = self.query(contract_id, at_utc)
        if not base["complete"]:
            return base | {
                "market_phase": "UNKNOWN",
                "continuous_matching": False,
                "seconds_to_open": None,
            }
        if base["is_trading"]:
            return base | {
                "market_phase": "CONTINUOUS",
                "continuous_matching": True,
                "seconds_to_open": 0,
            }
        now = _utc(at_utc)
        sessions = self._scope(contract_id) or ()
        prev_session = next((session for session in reversed(sessions) if session.end <= now), None)
        next_session = next((session for session in sessions if session.start > now), None)
        seconds_to_open = None
        if next_session is None:
            phase = "CLOSED"
        else:
            gap_seconds = (next_session.start - now).total_seconds()
            seconds_to_open = max(0, int(gap_seconds))
            if next_session.is_opening_session and 0 < gap_seconds <= 300:
                phase = "AUCTION_MATCH" if gap_seconds <= 60 else "AUCTION_ORDERING"
            elif prev_session is None:
                phase = "PRE_OPEN"
            elif prev_session.trading_day != next_session.trading_day:
                phase = "CLOSED"
            elif next_session.is_opening_session:
                phase = "PRE_OPEN"
            else:
                phase = "INTRADAY_BREAK"
        return base | {
            "market_phase": phase,
            "continuous_matching": False,
            "seconds_to_open": seconds_to_open,
        }

    def active_milliseconds(self, contract_id, from_utc, to_utc):
        """Sum covered session intersections; breaks contribute zero duration."""
        start, end = _utc(from_utc), _utc(to_utc)
        if start > end:
            _error("Active-time interval must not move backwards")
        sessions = self._scope(contract_id)
        if not self._complete or sessions is None or not self._start <= start <= end <= self._end:
            raise TraderError("E_CALENDAR_INCOMPLETE", "Active duration needs complete contract and interval coverage")
        total = sum(_microseconds(min(end, session.end) - max(start, session.start))
                    for session in sessions if max(start, session.start) < min(end, session.end))
        return total // 1000

    def transition(self,contract_id,from_utc,to_utc):
        """Explain explicit cabinet-day transitions; midnight alone changes none.

        Missing coverage is UNKNOWN, never a reset instruction. Every returned
        day is taken from a supplied session, including holiday/night labels.
        """
        start,end=_utc(from_utc),_utc(to_utc)
        if start>end:_error('Day transition cannot move backwards')
        sessions=self._scope(contract_id)
        base=self._references()|{'contract_id':contract_id,'from_utc':from_utc,'to_utc':to_utc,'complete':False,
            'days':[],'changed':None,'entry_reauthorization_required':True,'preserve_original_protection':True}
        if not self._complete or sessions is None or not self._start<=start<=end<self._end:return base
        previous=next((s for s in reversed(sessions) if s.start<=start),None)
        relevant=([previous] if previous else [])+[s for s in sessions if start<s.start<=end]
        days=[]
        for session in relevant:
            if not days or days[-1]!=session.trading_day:days.append(session.trading_day)
        return base|{'complete':True,'days':days,'changed':len(days)>1}

