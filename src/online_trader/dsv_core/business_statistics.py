"""D10 nominal stop state for one contract and cabinet trading day."""

from decimal import Decimal, localcontext

from online_trader.domain.values import decimal_text


MONTH_STOP_REASON = "本月连续停机停开"
NET_LOSS_REASON = "净亏达{threshold}点"


def reduce_stops(contract_id, trading_day, *, loss, stop_streak, continuation_stopped,
                 day_stopped, day_stopped_reason, history, month_stop, calendar, stop_offset, trade):
    """Apply daily and monthly stops, then report ordinary and continuation availability."""
    current_month = trading_day[:7]
    monthly_count = trade["month_stop_days"]
    if monthly_count > 0 and month_stop is not None and month_stop != current_month:
        month_stop = None
        if day_stopped_reason == MONTH_STOP_REASON:
            day_stopped = False
            day_stopped_reason = None

    with localcontext() as context:
        context.prec = 80
        threshold = Decimal(stop_offset) * trade["max_net_loss_stops"]
    reasons = []
    if threshold > 0 and loss >= threshold:
        day_stopped = True
        day_stopped_reason = NET_LOSS_REASON.format(threshold=decimal_text(threshold))
        reasons.append("NET_LOSS_LIMIT")
        if monthly_count > 0:
            if trading_day not in history:
                history = [*history, trading_day]
            history = history[-12:]
            eligible = sorted(day for day in calendar if day <= trading_day)
            if trading_day not in eligible or len(eligible) < monthly_count:
                reasons.append("TRADING_CALENDAR_COVERAGE_INCOMPLETE")
            elif all(day in history for day in eligible[-monthly_count:]):
                month_stop = current_month
                day_stopped_reason = MONTH_STOP_REASON
    if monthly_count > 0 and month_stop == current_month:
        day_stopped = True
        day_stopped_reason = MONTH_STOP_REASON
        reasons.append("MONTH_STOP_ACTIVE")
    if day_stopped:
        reasons.append("DAY_STOP_ACTIVE")

    continuation_allowed = not day_stopped
    if not trade["continuation_entry"]:
        continuation_allowed = False
        reasons.append("CONTINUATION_DISABLED")
    if stop_streak >= 2:
        continuation_allowed = False
        reasons.append("CONTINUATION_STOP_STREAK")
    if continuation_stopped and trade["cont_stop_day_stop"]:
        continuation_allowed = False
        reasons.append("CONTINUATION_DAY_STOPPED")

    state = {"contract_id": contract_id, "trading_day": trading_day, "net_loss_points": decimal_text(loss),
             "stop_streak": stop_streak, "continuation_stopped": continuation_stopped,
             "day_stopped": day_stopped, "day_stopped_reason": day_stopped_reason,
             "stop_days": history, "month_stop": month_stop}
    return {"allow_new_entries": not day_stopped, "allow_continuation": continuation_allowed,
            "reasons": reasons, "state": state}
