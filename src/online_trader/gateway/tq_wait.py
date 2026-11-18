"""Keep OnlineTrader's explicit TqSdk pump slices in one monotonic domain."""
from functools import cache
from importlib.metadata import PackageNotFoundError, version

from online_trader.scheduling.clock import sdk_deadline


@cache
def _pinned_sdk_available():
    try:
        return version('tqsdk') == '3.10.4'
    except PackageNotFoundError:
        return False


def is_pinned_tq_api(api):
    return type(api).__module__ == 'tqsdk.api' and _pinned_sdk_available()


def wait_update_slice(api, seconds):
    """Return the SDK result and the timing contract used for this call."""
    loop = getattr(api, '_loop', None)
    expire = getattr(api, '_set_wait_timeout', None)
    if not is_pinned_tq_api(api) or loop is None or not callable(expire):
        return api.wait_update(deadline=sdk_deadline(seconds)), 'SDK_UNIX_COMPATIBILITY'
    schedule = loop.call_later
    saved = vars(loop).get('call_later')
    deadline = loop.time() + max(0, seconds)
    timeout_armed = False

    def call_later(delay, callback, *args, context=None):
        nonlocal timeout_armed
        if callback == expire:
            timeout_armed = True
            return loop.call_at(deadline, callback, *args, context=context)
        return schedule(delay, callback, *args, context=context)

    # TqSdk 3.10.4 schedules this callback inside _run_until_task_done.
    # Preserve its wait, I/O, diff merge and task cancellation behavior.
    loop.call_later = call_later
    try:
        updated = api.wait_update(deadline=sdk_deadline(seconds))
    finally:
        if saved is None:
            del loop.call_later
        else:
            loop.call_later = saved
    return updated, 'SDK_MONOTONIC_TIMEOUT' if timeout_armed else 'SDK_TIMEOUT_NOT_OBSERVED'
