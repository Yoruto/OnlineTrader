"""Resolve a native nanosecond timestamp using an immutable explicit calendar.

This is provenance for a received fact, never entry or recovery authority.
"""
from datetime import datetime, timedelta, timezone
import re

from online_trader.contracts.codec import content_hash
from online_trader.errors import TraderError


def native_day(calendar, contract_id, source_time_ns):
    if (calendar is None or type(source_time_ns) is not str
            or not re.fullmatch(r'[1-9][0-9]{0,18}', source_time_ns)
            or int(source_time_ns) > 9223372036854775807):
        raise TraderError('E_FACT_BARRIER', 'An exact native timestamp and bound calendar are required')
    seconds, nanos = divmod(int(source_time_ns), 1_000_000_000)
    # Integer arithmetic preserves a timestamp immediately before a boundary.
    at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds, microseconds=nanos // 1000)
    window = calendar.query(contract_id, at.isoformat().replace('+00:00', 'Z'))
    if not window['complete'] or not window['is_trading']:
        raise TraderError('E_CALENDAR_INCOMPLETE', 'Native time has no proved active cabinet-day session')
    proof = {'contract_id': contract_id, 'source_time_ns': source_time_ns,
             'trading_day': window['trading_day'], 'calendar_hash': calendar.content_hash,
             'session_id': window['session']['session_id'],
             'source': 'NATIVE_TIMESTAMP_BOUND_EXPLICIT_CALENDAR'}
    return proof | {'evidence_hash': content_hash(proof)}
