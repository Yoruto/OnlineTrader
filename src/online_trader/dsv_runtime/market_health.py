"""Lightweight feed-level and per-symbol market health classification for Target Owner."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from online_trader.scheduling.clock import NS_PER_SECOND


FEED_HEALTH_STATES = frozenset({
    'HEALTHY',
    'DEGRADED',
    'FEED_SUSPECTED_STALLED',
    'RECONNECTING',
    'RESYNCHRONIZING',
    'CLOCK_UNCERTAIN',
    'OWNER_STALLED',
})

FEED_ENTRY_BLOCKED_STATES = frozenset({
    'FEED_SUSPECTED_STALLED',
    'RECONNECTING',
    'RESYNCHRONIZING',
    'CLOCK_UNCERTAIN',
    'OWNER_STALLED',
})

SYMBOL_HEALTH_STATES = frozenset({
    'FRESH',
    'STALE',
    'QUIET',
    'MARKET_CLOSED',
    'WAITING_FIRST_QUOTE',
    'INVALID_QUOTE',
    'SESSION_MISMATCH',
})

_DEFAULT_STALL_NS = 5 * NS_PER_SECOND
_PUMP_STALL_NS = 1_500_000_000
_PUMP_DEGRADED_NS = 500_000_000
_OPEN_GRACE_NS = 5 * NS_PER_SECOND
_INVALID_REASONS = frozenset({
    'QUOTE_TIMESTAMP_INVALID',
    'QUOTE_TIME_REGRESSION',
    'QUOTE_SESSION_UNKNOWN',
    'QUOTE_REFERENCE_CONFLICT',
})


def _int_or_none(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decimal_or_zero(value) -> Decimal:
    try:
        parsed = Decimal(str(value))
        return parsed if parsed > 0 else Decimal(0)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def evaluate_market_health(
    contracts,
    markets: dict,
    *,
    clock_health: dict | None,
    synchronized: bool,
    connection_complete: bool,
    sdk_pump_gap_ns: int | None,
    sdk_updated=None,
    max_quote_age_seconds=0,
    session_elapsed_ns_by_contract: dict[str, int] | None = None,
    allow_single_contract_stall: bool = True,
) -> dict:
    """Classify global feed health separately from per-symbol quote states.

    ``QUIET`` indicates that other active symbols continue ticking normally so
    a single illiquid symbol does not trigger a global feed stall alarm. It
    never exempts an active symbol from ``max_quote_age_seconds`` entry gating.
    """
    clock_health = clock_health or {}
    age_limit = _decimal_or_zero(max_quote_age_seconds)
    age_limit_ns = int(age_limit * NS_PER_SECOND) if age_limit > 0 else 0
    stall_threshold_ns = max(_DEFAULT_STALL_NS, age_limit_ns)
    session_elapsed_ns_by_contract = session_elapsed_ns_by_contract or {}

    tentative: dict[str, str] = {}
    active_codes: list[str] = []
    active_silences: dict[str, int | None] = {}
    fresh_codes: list[str] = []

    for code in contracts:
        market = markets.get(code) or {}
        obs = market.get('quote_observation') or {}
        reason = obs.get('reason')
        quality = obs.get('age_quality')
        active = market.get('market_session_active') is True
        if not active:
            tentative[code] = 'SESSION_MISMATCH' if reason == 'QUOTE_SESSION_MISMATCH' else 'MARKET_CLOSED'
            continue

        active_codes.append(code)
        age_ns = _int_or_none(obs.get('source_age_raw_ns'))
        silence_ns = _int_or_none(obs.get('silence_ns'))
        effective_silence = silence_ns if silence_ns is not None else age_ns
        active_silences[code] = effective_silence

        if reason in _INVALID_REASONS:
            tentative[code] = 'INVALID_QUOTE'
        elif reason == 'QUOTE_SESSION_MISMATCH':
            tentative[code] = 'SESSION_MISMATCH'
        elif not market.get('ticks') and obs.get('source_time_raw') is None:
            tentative[code] = 'WAITING_FIRST_QUOTE'
        elif quality == 'VALID' and market.get('ticks'):
            if age_limit_ns > 0 and age_ns is not None and age_ns > age_limit_ns:
                tentative[code] = 'STALE'
            else:
                tentative[code] = 'FRESH'
                fresh_codes.append(code)
        else:
            since_open = session_elapsed_ns_by_contract.get(code)
            if since_open is not None and since_open < _OPEN_GRACE_NS and obs.get('source_time_raw') is None:
                tentative[code] = 'WAITING_FIRST_QUOTE'
            else:
                tentative[code] = 'STALE'

    clock_reason = clock_health.get('reason')
    protection_ready = clock_health.get('protection_ready')
    all_active_silent = False
    if len(active_codes) >= 2:
        all_active_silent = all(
            active_silences[code] is not None and active_silences[code] >= stall_threshold_ns
            for code in active_codes
        )
    elif allow_single_contract_stall and len(active_codes) == 1 and sdk_updated is False:
        only_silence = active_silences[active_codes[0]]
        all_active_silent = only_silence is not None and only_silence >= stall_threshold_ns

    if (sdk_pump_gap_ns is not None and sdk_pump_gap_ns > _PUMP_STALL_NS) or clock_reason == 'OWNER_SCHEDULING_GAP':
        feed_status = 'OWNER_STALLED'
    elif clock_reason or protection_ready is False or clock_health.get('reference_status') == 'REFERENCE_CONFLICT':
        feed_status = 'CLOCK_UNCERTAIN'
    elif not connection_complete:
        feed_status = 'RECONNECTING'
    elif not synchronized:
        feed_status = 'RESYNCHRONIZING'
    elif all_active_silent:
        feed_status = 'FEED_SUSPECTED_STALLED'
    elif any(tentative[code] in {'STALE', 'INVALID_QUOTE', 'SESSION_MISMATCH'} for code in active_codes) or (
        sdk_pump_gap_ns is not None and sdk_pump_gap_ns > _PUMP_DEGRADED_NS
    ):
        feed_status = 'DEGRADED'
    else:
        feed_status = 'HEALTHY'

    symbol_states = dict(tentative)
    if feed_status in {'HEALTHY', 'DEGRADED'} and fresh_codes:
        for code in active_codes:
            if symbol_states[code] == 'STALE':
                obs = (markets.get(code) or {}).get('quote_observation') or {}
                if obs.get('age_quality') == 'VALID' and obs.get('source_time_raw') is not None:
                    symbol_states[code] = 'QUIET'

    return {
        'status': feed_status,
        'feed_entry_blocked': feed_status in FEED_ENTRY_BLOCKED_STATES,
        'age_filter_disabled': age_limit <= 0,
        'symbols': symbol_states,
        'active_count': len(active_codes),
        'fresh_count': len(fresh_codes),
        'observed_at_utc': _utc_now(),
    }
