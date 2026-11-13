"""Finite, market-only SDK metadata acquisition after the user clicks Start.

The selected account supplies its TQ market credentials. An explicit local
TqSim is used only because TqApi requires an account object: this reader never
logs into SimNow/KQ trading or sends/cancels an order. Its calendar is SDK market
metadata, not cabinet-day reconciliation; the execution owner still performs
its own synchronization. No SDK import or credential read occurs on import.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
import re
import time

from online_trader.domain.values import decimal_text, identifier
from online_trader.errors import TraderError
from online_trader.configuration.rollover import expiry_date, future_product
from online_trader.gateway.tqkq import number
from online_trader.scheduling.calendar import TradingCalendar


BEIJING = timezone(timedelta(hours=8))
SDK_VERSION = '3.10.4'
_CLOCK = re.compile(r'(\d{2}):([0-5]\d):([0-5]\d)\Z')


def _error(message, code='E_STARTUP_METADATA'):
    raise TraderError(code, message) from None


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _get(value, key, default=None):
    return value.get(key, default) if hasattr(value, 'get') else getattr(value, key, default)


def _sdk_factory(auth, *, deadline):
    try:
        if version('tqsdk') != SDK_VERSION:
            _error('启动需要已核验的行情组件。', 'E_DEPENDENCY')
    except PackageNotFoundError:
        _error('未找到启动所需行情组件。', 'E_DEPENDENCY')
    from tqsdk import TqApi, TqAuth, TqSim

    class MetadataApi(TqApi):
        def wait_update(self, deadline=None, **kwargs):
            remaining = self._metadata_deadline - time.monotonic()
            if remaining <= 0:
                return False
            limit = time.time() + remaining
            return super().wait_update(deadline=min(deadline, limit) if deadline is not None else limit, **kwargs)

        def insert_order(self, *args, **kwargs):
            _error('启动资料读取器不能下单。', 'E_READ_ONLY')

        def cancel_order(self, *args, **kwargs):
            _error('启动资料读取器不能撤单。', 'E_READ_ONLY')

    # Set the deadline before TqApi's synchronous initialization starts.
    api = MetadataApi.__new__(MetadataApi)
    api._metadata_deadline = deadline
    try:
        MetadataApi.__init__(api, account=TqSim(), auth=TqAuth(auth['username'], auth['password']),
                             debug=False, web_gui=False, disable_print=True)
    except BaseException:
        # Some SDK setup failures happen before its own cleanup try/finally.
        try:
            api.close()
        except Exception:
            pass
        raise
    return api


def _periods(value, *, night):
    if not isinstance(value, (list, tuple)) or len(value) > 12 or (not value and not night):
        _error('行情服务尚未提供完整交易时段。', 'E_CALENDAR_INCOMPLETE')
    result = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _error('行情交易时段格式不完整。', 'E_CALENDAR_INCOMPLETE')
        seconds = []
        for clock in pair:
            match = _CLOCK.fullmatch(clock) if type(clock) is str else None
            if match is None:
                _error('行情交易时段格式不完整。', 'E_CALENDAR_INCOMPLETE')
            hours, minutes, secs = map(int, match.groups())
            if not (18 <= hours < 48 if night else 0 <= hours < 24):
                _error('行情交易时段超出支持范围。', 'E_CALENDAR_INCOMPLETE')
            seconds.append(hours * 3600 + minutes * 60 + secs)
        if seconds[0] >= seconds[1] or (result and seconds[0] < result[-1][1]):
            _error('行情交易时段不连续或重叠。', 'E_CALENDAR_INCOMPLETE')
        result.append(tuple(seconds))
    return result


def _calendar_dates(raw, start, end):
    records = raw.to_dict('records') if hasattr(raw, 'to_dict') else raw
    if not isinstance(records, list):
        _error('交易日历数据未就绪。', 'E_CALENDAR_INCOMPLETE')
    result = {}
    for row in records:
        value = row.get('date') if isinstance(row, dict) else None
        if isinstance(value, datetime):
            value = value.date()
        if type(value) is str:
            try:
                value = date.fromisoformat(value)
            except ValueError:
                value = None
        if type(value) is not date or type(row.get('trading')) is not bool or value in result:
            _error('交易日历数据不完整。', 'E_CALENDAR_INCOMPLETE')
        result[value] = row['trading']
    expected = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    if set(result) != expected:
        _error('行情日历没有覆盖所需日期。', 'E_CALENDAR_INCOMPLETE')
    return result


def build_metadata(calendar_dates, quote_records, contracts, *, now, provider):
    """Build only one supported cabinet-day plan from explicit SDK evidence.

    SDK date flags do not state special holiday-night arrangements. Rather than
    invent those, a weekday holiday gap is rejected. Ordinary consecutive days
    and Friday-to-Monday night sessions use the source's actual trading dates.
    """
    if now.tzinfo is None:
        _error('启动时间必须包含时区。')
    local = now.astimezone(BEIJING)
    instruments, schedules = {}, {}
    for code in contracts:
        quote = quote_records[code]
        if _get(quote, 'expired') is not False:
            _error('所选合约已到期或状态尚未确认。')
        if _get(quote, 'ins_class') != 'FUTURE':
            _error('所选代码不是已确认的期货合约。')
        if _get(quote, 'instrument_id') != code:
            _error('行情服务返回的合约身份不符。')
        try:
            multiplier = Decimal(str(_get(quote, 'volume_multiple')))
        except InvalidOperation:
            multiplier = Decimal('NaN')
        if not multiplier.is_finite() or not 0 < multiplier <= 1_000_000_000 or multiplier != multiplier.to_integral_value():
            _error('所选合约的乘数尚未确认。')
        periods = _get(quote, 'trading_time')
        day = _periods(_get(periods, 'day'), night=False)
        night = _periods(_get(periods, 'night'), night=True)
        instruments[code] = {'multiplier': decimal_text(multiplier)}
        schedules[code] = {'day': day, 'night': night}
    days = sorted(day for day, trading in calendar_dates.items() if trading)
    candidates = [day for day in days if any(
        datetime.combine(day, datetime.min.time(), BEIJING) + timedelta(seconds=periods['day'][-1][1]) > local
        for periods in schedules.values())]
    if not candidates:
        _error('未取得下一交易日的完整日历。', 'E_CALENDAR_INCOMPLETE')
    target = candidates[0]
    prior = next((day for day in reversed(days) if day < target), None)
    if prior is None:
        _error('未取得上一完整交易日。', 'E_CALENDAR_INCOMPLETE')
    gap = [prior + timedelta(days=i) for i in range(1, (target - prior).days)]
    if any(day.weekday() < 5 and not calendar_dates[day] for day in gap):
        _error('节假日前后的夜盘安排尚未确认，请等待完整交易时段资料。', 'E_CALENDAR_INCOMPLETE')
    rows, opened, closed = [], [], []
    for code in contracts:
        sessions = []
        for kind, actual in (('night', prior), ('day', target)):
            periods = schedules[code][kind]
            midnight = datetime.combine(actual, datetime.min.time(), BEIJING)
            for index, (start, end) in enumerate(periods):
                begin = midnight + timedelta(seconds=start)
                finish = midnight + timedelta(seconds=end)
                sessions.append({'session_id': f'{target.isoformat()}-{kind}-{index}',
                                 'trading_day': target.isoformat(), 'open_utc': _utc(begin),
                                 'close_utc': _utc(finish), 'is_opening_session': index == 0,
                                 'force_close_at_end': index == len(periods) - 1})
                opened.append(begin); closed.append(finish)
        rows.append({'contract_id': code, 'sessions': sessions})
    calendar = {'schema_version': 'TRADING_CALENDAR/1', 'calendar_id': 'desktop-sdk-' + target.isoformat(),
                'calendar_version': 'tqsdk-3.10.4', 'complete': True,
                'coverage_from_utc': _utc(min([local, *opened])),
                'coverage_to_utc': _utc(max(closed) + timedelta(seconds=1)), 'contracts': rows}
    TradingCalendar.from_record(calendar)
    return {'provider': provider, 'trading_day': target.isoformat(), 'last_completed_day': prior.isoformat(),
            'calendar': calendar, 'instruments': instruments, 'observed_at_utc': _utc(now),
            'source': 'TqSdk 3.10.4 get_trading_calendar + quote.trading_time; market metadata, not cabinet reconciliation'}


def read_metadata(credential_reference, contracts, *, now=None, timeout_seconds=60, sdk_factory=None, quote_preview=False):
    """Read market metadata and close its session before returning.

    ``timeout_seconds`` bounds SDK update waits. The SDK's synchronous account
    authentication and calendar HTTP requests have their own 30-second timeouts
    and may overrun that update budget; elapsed time is checked before accepting
    metadata. No result obtained after the budget is accepted.
    A caller requiring a hard process deadline must supervise this child process.
    """
    if not isinstance(contracts, (list, tuple)) or not 1 <= len(contracts) <= 64 or len(set(contracts)) != len(contracts):
        _error('启动品种列表无效。')
    for code in contracts:
        identifier(code)
        if not re.fullmatch(r'(?:SHFE|DCE|CZCE|CFFEX|INE|GFEX)\.[A-Za-z]+\d+', code):
            _error('启动需要明确的期货合约代码。')
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        _error('启动时间必须包含时区。')

    def read(api, deadline, provider):
        quotes = {code: api.get_quote(code) for code in contracts}
        if quote_preview:
            return quote_reference(api, quotes, deadline)
        local_day = instant.astimezone(BEIJING).date()
        start, end = local_day - timedelta(days=20), local_day + timedelta(days=20)
        raw = api.get_trading_calendar(start, end)
        if time.monotonic() >= deadline:
            _error('读取启动资料超时，请检查行情连接。', 'E_STARTUP_METADATA_TIMEOUT')
        dates = _calendar_dates(raw, start, end)
        return build_metadata(dates, quotes, contracts, now=instant, provider=provider)

    return _read_market(credential_reference, read, timeout_seconds=timeout_seconds, sdk_factory=sdk_factory)


def _read_market(credential_reference, read, *, timeout_seconds, sdk_factory):
    from online_trader.accounts import StoredCredential, StoredSimNowCredential
    if type(credential_reference) not in (StoredCredential, StoredSimNowCredential):
        _error('行情查询需要所选账号的本地凭据引用。', 'E_CREDENTIALS')
    if type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 180:
        _error('启动资料等待期限无效。')
    store = credential_reference.store
    row = store.get(credential_reference.account_id)
    if store.read()['selected_id'] != row['account_id']:
        _error('所选账号已经改变，请重新启动。', 'E_SCOPE')
    secrets = credential_reference.resolve()
    auth = ({'username': secrets['kq_username'], 'password': secrets['kq_password']}
            if row['provider'] == 'TQ_SIMNOW' else secrets)
    deadline = time.monotonic() + timeout_seconds
    api = None
    try:
        api = (sdk_factory or _sdk_factory)(auth, deadline=deadline)
        if time.monotonic() >= deadline:
            _error('读取启动资料超时，请检查行情连接。', 'E_STARTUP_METADATA_TIMEOUT')
        result = read(api, deadline, row['provider'])
        if time.monotonic() >= deadline:
            _error('读取行情资料超时，请检查行情连接。', 'E_STARTUP_METADATA_TIMEOUT')
        if store.read()['selected_id'] != row['account_id']:
            _error('所选账号已经改变，请重新查询。', 'E_SCOPE')
        return result
    except TraderError:
        raise
    except Exception:
        _error('读取行情资料失败，请检查行情连接。')
    finally:
        secrets = auth = None
        if api is not None:
            try:
                api.close()
            except Exception:
                _error('行情资料连接未正常关闭，启动已停止。', 'E_STARTUP_METADATA_CLOSE')


def read_startup_metadata(store, account_id, contracts, **kwargs):
    return read_metadata(store.credential_reference(account_id), contracts, **kwargs)


def read_contract_catalog(store, account_id, *, timeout_seconds=30, sdk_factory=None):
    """Query the original FUTURE category using the selected market identity."""
    def read(api, deadline, provider):
        contracts = sorted({code for code in api.query_quotes(ins_class='FUTURE')
                            if future_product(code) is not None})
        if len(contracts) > 20_000:
            _error('返回的期货合约目录超过可读取范围。', 'E_CONTRACT_CATALOG')
        return contracts

    return _read_market(store.credential_reference(account_id), read, timeout_seconds=timeout_seconds,
                        sdk_factory=sdk_factory)


def quote_reference(api, quotes, deadline):
    """Sample the selected and main contracts on the market-only connection."""
    main_refs, resolved = {}, {}
    for code in quotes:
        product = future_product(code)
        if product is not None:
            if product not in main_refs:
                main_refs[product] = api.get_quote(f'KQ.m@{product[0]}.{product[1]}')
            resolved[code] = main_refs[product]

    def wait(ready, seconds):
        end = min(deadline, time.monotonic() + seconds)
        while not ready() and time.monotonic() < end:
            api.wait_update(deadline=time.time()+min(0.3, max(0, end-time.monotonic())))

    def has_price(quote):
        value = number(_get(quote, 'last_price'))
        return value is not None and Decimal(value) > 0

    wait(lambda: all(has_price(quote) for quote in quotes.values()), 5)
    main_quotes = {}
    for code, reference in resolved.items():
        main = _get(reference, 'underlying_symbol')
        if isinstance(main,str) and future_product(main) == future_product(code):
            main_quotes[code] = (main, api.get_quote(main))
    wait(lambda: all(expiry_date(_get(quote,'expire_datetime')) is not None for _,quote in main_quotes.values()), 4)
    rows = []
    for code, quote in quotes.items():
        main, main_quote = main_quotes.get(code, (None, {}))
        expires = expiry_date(_get(main_quote,'expire_datetime'))
        fields = {name:number(_get(quote, source)) for name,source in (
            ('last','last_price'),('margin','margin'),('volume_multiple','volume_multiple'),
        ('commission','commission'),('price_tick','price_tick'))}
        rows.append(fields | {'contract_id':code, 'source_time_utc':_get(quote,'datetime'),
            'main_contract':main,'main_expiry_date':expires.isoformat() if expires else None,
            'main_last':number(_get(main_quote,'last_price')), 'ready':has_price(quote)})
    if time.monotonic() >= deadline:
        _error('读取行情超时。', 'E_STARTUP_METADATA_TIMEOUT')
    return rows
