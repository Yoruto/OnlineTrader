"""Unified per-contract market phase and execution permission evaluation.

Separates market execution permissions (whether the exchange/broker currently
permits opening, closing, and cancelling) from strategy-level opening filters
(such as ``participate_auction=False`` or ``no_open_after_open``).

Pure deterministic module: no SDK, SQLite, UI, I/O, or system clock access.
"""

from __future__ import annotations

from datetime import datetime, timezone


MARKET_PHASES = frozenset({
    'PRE_OPEN',
    'AUCTION_ORDERING',
    'AUCTION_MATCH',
    'CONTINUOUS',
    'INTRADAY_BREAK',
    'HALTED',
    'CLOSED',
    'UNKNOWN',
})

EVIDENCE_STATES = frozenset({'CONFIRMED', 'INFERRED', 'UNKNOWN'})

_PHASE_WAIT_REASONS = {
    'AUCTION_ORDERING': 'DEFERRED_AUCTION_ORDERING',
    'AUCTION_MATCH': 'DEFERRED_AUCTION_MATCH',
    'HALTED': 'DEFERRED_HALTED',
    'PRE_OPEN': 'DEFERRED_OUTSIDE_SESSION',
    'INTRADAY_BREAK': 'DEFERRED_OUTSIDE_SESSION',
    'CLOSED': 'DEFERRED_OUTSIDE_SESSION',
    'UNKNOWN': 'DEFERRED_STATUS_UNKNOWN',
}

_TRANSIENT_PHASE_TOKENS = (
    '集合竞价',
    '非连续交易',
    '撮合',
    '未开盘',
    '未开市',
    '休市',
    '停牌',
    '暂停交易',
    '当前状态禁止',
    '当前时间禁止',
    'non-continuous',
    'auction',
    'not trading',
    'market closed',
    'halted',
)


def normalize_trading_status(raw_status) -> str | None:
    """Normalize TqSdk/broker live trading status strings (handles TqSdk's CONTINOUS spelling)."""
    if not isinstance(raw_status, str):
        return None
    cleaned = raw_status.strip().upper()
    if not cleaned:
        return None
    if cleaned in {'CONTINOUS', 'CONTINUOUS'}:
        return 'CONTINUOUS'
    if cleaned in {'AUCTIONORDERING', 'AUCTION_ORDERING'}:
        return 'AUCTIONORDERING'
    if cleaned in {'AUCTIONMATCH', 'AUCTION_MATCH'}:
        return 'AUCTIONMATCH'
    if cleaned in {'NOTRADING', 'NO_TRADING', 'HALTED', 'CLOSED'}:
        return 'NOTRADING'
    return None


def classify_market_phase_rejection(message) -> dict | None:
    """Detect whether a broker rejection text explicitly indicates a non-tradable market phase."""
    text = str(message or '').strip()
    if not text:
        return None
    lowered = text.lower()
    if not any(token in lowered for token in _TRANSIENT_PHASE_TOKENS):
        return None
    if any(token in lowered for token in ('撮合', 'auctionmatch')):
        return {'phase': 'AUCTION_MATCH', 'can_cancel': False, 'reason': 'BROKER_AUCTION_MATCH_REJECTED'}
    if any(token in lowered for token in ('集合竞价', 'auction')):
        # Broker rejected an order during call auction; do not assume cancel is allowed unless confirmed.
        return {'phase': 'AUCTION_ORDERING', 'can_cancel': False, 'reason': 'BROKER_AUCTION_REJECTED'}
    if any(token in lowered for token in ('停牌', '暂停交易', 'halted')):
        return {'phase': 'HALTED', 'can_cancel': False, 'reason': 'BROKER_HALTED_REJECTED'}
    return {'phase': 'HALTED', 'can_cancel': False, 'reason': 'BROKER_NON_CONTINUOUS_REJECTED'}


def _wait_reason_for(market_phase: str, *, has_valid_quote: bool, require_live_quote: bool) -> str | None:
    if market_phase == 'CONTINUOUS':
        if require_live_quote and not has_valid_quote:
            return 'DEFERRED_QUOTE_UNAVAILABLE'
        return None
    return _PHASE_WAIT_REASONS.get(market_phase, 'DEFERRED_STATUS_UNKNOWN')


def evaluate_market_permissions(
    *,
    contract_id: str,
    calendar_phase: dict | None,
    live_trading_status: str | None = None,
    has_valid_quote: bool = False,
    clock_unverified: bool = False,
    broker_phase_override: dict | None = None,
    require_live_quote: bool = True,
) -> dict:
    """Compute unified per-contract market phase and open/close/cancel permissions."""
    if clock_unverified:
        return {
            'contract_id': contract_id,
            'market_phase': 'UNKNOWN',
            'evidence_state': 'UNKNOWN',
            'status_source': 'CLOCK_UNVERIFIED',
            'continuous_matching': False,
            'can_open': False,
            'can_close': False,
            'can_cancel': False,
            'wait_reason': 'DEFERRED_STATUS_UNKNOWN',
            'live_trading_status': normalize_trading_status(live_trading_status),
        }

    cal = calendar_phase or {}
    cal_complete = bool(cal.get('complete'))
    cal_phase = cal.get('market_phase') or ('CONTINUOUS' if cal.get('is_trading') else 'UNKNOWN')
    if cal_phase not in MARKET_PHASES:
        cal_phase = 'UNKNOWN'
    live = normalize_trading_status(live_trading_status)

    if live == 'NOTRADING':
        evidence_state = 'CONFIRMED'
        status_source = 'TQ_TRADING_STATUS'
        if cal_phase == 'CONTINUOUS':
            market_phase = 'HALTED'
        elif cal_phase in {'PRE_OPEN', 'INTRADAY_BREAK', 'CLOSED', 'AUCTION_ORDERING', 'AUCTION_MATCH'}:
            market_phase = cal_phase
        else:
            market_phase = 'CLOSED'
        continuous_matching = False
        can_open = False
        can_close = False
        can_cancel = False
    elif live == 'AUCTIONORDERING':
        evidence_state = 'CONFIRMED'
        status_source = 'TQ_TRADING_STATUS'
        market_phase = 'AUCTION_MATCH' if cal_phase == 'AUCTION_MATCH' else 'AUCTION_ORDERING'
        continuous_matching = False
        can_open = False
        can_close = False
        can_cancel = market_phase == 'AUCTION_ORDERING'
    elif live == 'AUCTIONMATCH':
        evidence_state = 'CONFIRMED'
        status_source = 'TQ_TRADING_STATUS'
        market_phase = 'AUCTION_MATCH'
        continuous_matching = False
        can_open = False
        can_close = False
        can_cancel = False
    elif live == 'CONTINUOUS':
        evidence_state = 'CONFIRMED'
        status_source = 'TQ_TRADING_STATUS'
        market_phase = 'CONTINUOUS'
        continuous_matching = True
        can_cancel = True
        can_open = bool(has_valid_quote) if require_live_quote else True
        can_close = bool(has_valid_quote) if require_live_quote else True
    elif not cal_complete or cal_phase == 'UNKNOWN':
        evidence_state = 'UNKNOWN'
        status_source = 'NONE'
        market_phase = 'UNKNOWN'
        continuous_matching = False
        can_open = False
        can_close = False
        can_cancel = False
    else:
        evidence_state = 'INFERRED'
        status_source = 'CALENDAR_CLOCK_QUOTE'
        market_phase = cal_phase
        if cal_phase == 'CONTINUOUS':
            continuous_matching = True
            can_cancel = True
            can_open = bool(has_valid_quote) if require_live_quote else True
            can_close = bool(has_valid_quote) if require_live_quote else True
        elif cal_phase == 'AUCTION_ORDERING':
            continuous_matching = False
            can_open = False
            can_close = False
            can_cancel = True
        else:
            continuous_matching = False
            can_open = False
            can_close = False
            can_cancel = False

    if isinstance(broker_phase_override, dict):
        override_phase = broker_phase_override.get('phase')
        if override_phase in MARKET_PHASES and override_phase != 'CONTINUOUS':
            market_phase = override_phase
            continuous_matching = False
            can_open = False
            can_close = False
            if not broker_phase_override.get('can_cancel', False):
                can_cancel = False
            status_source = 'BROKER_REJECTION_OVERRIDE'

    wait_reason = _wait_reason_for(
        market_phase,
        has_valid_quote=has_valid_quote,
        require_live_quote=require_live_quote,
    )
    return {
        'contract_id': contract_id,
        'market_phase': market_phase,
        'evidence_state': evidence_state,
        'status_source': status_source,
        'continuous_matching': continuous_matching,
        'can_open': can_open,
        'can_close': can_close,
        'can_cancel': can_cancel,
        'wait_reason': wait_reason,
        'live_trading_status': live,
    }


def _parse_utc(text: str | None) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def infer_offline_market_permission(market: dict, wall_time: str | None, *, clock_unverified: bool = False) -> dict:
    """Resolve explicit or legacy-derived market_permission for offline/test rounds."""
    explicit = market.get('market_permission')
    if isinstance(explicit, dict):
        phase = explicit.get('market_phase') or 'UNKNOWN'
        has_quote = bool(market.get('ticks') or market.get('tick') is not None)
        wait_reason = explicit.get('wait_reason')
        if wait_reason is None and not (explicit.get('can_open') and explicit.get('can_close')):
            wait_reason = _wait_reason_for(phase, has_valid_quote=has_quote, require_live_quote=True)
        return {
            'contract_id': market['contract_id'],
            'market_phase': phase,
            'evidence_state': explicit.get('evidence_state', 'INFERRED'),
            'status_source': explicit.get('status_source', 'EXPLICIT_FRAME'),
            'continuous_matching': bool(explicit.get('continuous_matching', phase == 'CONTINUOUS')),
            'can_open': bool(explicit.get('can_open', False)),
            'can_close': bool(explicit.get('can_close', False)),
            'can_cancel': bool(explicit.get('can_cancel', False)),
            'wait_reason': wait_reason,
            'live_trading_status': normalize_trading_status(explicit.get('live_trading_status')),
        }

    if clock_unverified:
        return evaluate_market_permissions(
            contract_id=market['contract_id'],
            calendar_phase=None,
            clock_unverified=True,
        )

    has_quote = bool(market.get('ticks') or market.get('tick') is not None)
    active_flag = market.get('market_session_active')
    effective_wall = (market.get('wall_reads') or {}).get('session', wall_time)
    now_dt = _parse_utc(effective_wall)
    open_dt = _parse_utc(market.get('session_open'))
    close_dt = _parse_utc(market.get('session_close'))
    opening_session = bool(market.get('opening_session', True))

    if active_flag is False:
        phase = 'CLOSED'
        if now_dt is not None and open_dt is not None and now_dt < open_dt:
            gap = (open_dt - now_dt).total_seconds()
            if opening_session and 0 < gap <= 300:
                phase = 'AUCTION_MATCH' if gap <= 60 else 'AUCTION_ORDERING'
            else:
                phase = 'PRE_OPEN'
        elif now_dt is not None and close_dt is not None and now_dt >= close_dt:
            phase = 'CLOSED'
        cal_phase = {'complete': True, 'is_trading': False, 'market_phase': phase}
    elif now_dt is not None and open_dt is not None and close_dt is not None:
        if open_dt <= now_dt < close_dt:
            cal_phase = {'complete': True, 'is_trading': True, 'market_phase': 'CONTINUOUS'}
        elif now_dt < open_dt:
            gap = (open_dt - now_dt).total_seconds()
            if opening_session and 0 < gap <= 300:
                phase = 'AUCTION_MATCH' if gap <= 60 else 'AUCTION_ORDERING'
            else:
                phase = 'PRE_OPEN'
            cal_phase = {'complete': True, 'is_trading': False, 'market_phase': phase}
        else:
            cal_phase = {'complete': True, 'is_trading': False, 'market_phase': 'CLOSED'}
    else:
        cal_phase = {'complete': True, 'is_trading': True, 'market_phase': 'CONTINUOUS'}

    return evaluate_market_permissions(
        contract_id=market['contract_id'],
        calendar_phase=cal_phase,
        has_valid_quote=has_quote,
        clock_unverified=clock_unverified,
        require_live_quote=True,
    )
