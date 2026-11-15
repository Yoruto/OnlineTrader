"""Frozen DSV's once-per-round monthly observation, not a permanent entry gate.

The caller supplies the saved business day BEFORE rolling it forward and a
complete explicit trading calendar. This function has no clock, SDK or I/O.
Source: StrategyThread._run 6005-6040; daily reset follows at 6795-6811.
"""

from datetime import date, timedelta

from online_trader.dsv_core.business_statistics import MONTH_STOP_REASON


def observe_month_stop(trading_day, *, loss, stop_offset, trade, history,
                       month_stop, day_stopped, day_stopped_reason, calendar):
    """Return monthly tracking fields without treating an old marker as a stop.

    Calendar completeness remains an input contract. An unavailable interval
    does not invent trading days or become another entry-blocking condition.
    Native orders, fills, daily counters and user start/stop are not inputs.
    """
    result = {'stop_days': list(history), 'month_stop': month_stop,
              'day_stopped': day_stopped, 'day_stopped_reason': day_stopped_reason}
    count = trade['month_stop_days']
    if count <= 0:
        return result
    current_month = trading_day[:7]
    if month_stop and month_stop != current_month:
        result['month_stop'] = None
        if day_stopped_reason == MONTH_STOP_REASON:
            result.update(day_stopped=False, day_stopped_reason=None)

    # Keep the source's float multiplication and inclusive comparison.
    threshold = float(stop_offset) * trade['max_net_loss_stops']
    if threshold > 0 and float(loss) >= threshold and trading_day not in history:
        result['stop_days'] = [*history, trading_day][-12:]

    # _run 6026-6027 skips this block even after a daily reset cleared day_stop.
    if result['month_stop'] == current_month:
        return result

    # _run 6031 searches at most 40 CIVIL days, including the saved day.
    earliest = (date.fromisoformat(trading_day) - timedelta(days=39)).isoformat()
    eligible = sorted(day for day in calendar if earliest <= day <= trading_day)
    if trading_day not in eligible or len(eligible) < count:
        return result
    if all(day in result['stop_days'] for day in eligible[-count:]):
        result.update(month_stop=current_month, day_stopped=True,
                      day_stopped_reason=MONTH_STOP_REASON)
    return result
