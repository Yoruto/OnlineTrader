"""Run the frozen DSV strategy code object against recorded, offline SDK inputs."""

import argparse
import builtins
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import dis
from io import BytesIO
import json
import marshal
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
from threading import Event, Lock
from types import FunctionType, SimpleNamespace
from zipfile import ZipFile

from qa.dsv_parity.verify_reference import MANIFEST, archive_objects, code_objects, verify
from qa.dsv_parity.entry_evidence import SCHEMA, FrozenEntryObserver, SourceEvidenceError


_BEIJING = timezone(timedelta(hours=8))
_HELPERS = """
    _atomic_write_json _is_cffex _next_session_open_old account_mode apply_close_to_net_loss cap_close_points chase_within_cap
    current_session current_session_for decide_continuation decide_half_close
    fmt_time get_holidays get_trading_day half_close_lots half_pct_to_ratio half_stage_points
    hysteresis_ready is_before_session_close is_in_auction is_option_symbol is_trading_day
    next_session_open next_trading_day night_close_hm opening_jump_block parse_hm_str
    safe_num session_intervals session_open_recent set_holidays stop_hit
""".split()


class _Finished(BaseException):
    pass


class _Entity(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class _Bars(list):
    @property
    def iloc(self):
        return self

    def __getitem__(self, key):
        if isinstance(key, str):
            return _Bars(row[key] for row in self)
        result = super().__getitem__(key)
        return _Bars(result) if isinstance(key, slice) else result

    def tolist(self):
        return list(self)


def _load(archive):
    expected = json.loads(MANIFEST.read_text(encoding='utf-8'))['dsv_reference']
    source = archive.read_bytes()
    verify('archive', source, expected['archive_sha256'])
    with ZipFile(BytesIO(source)) as bundle:
        executable = bundle.read('trading/trading.exe')
    verify('exe', executable, expected['exe_sha256'])
    gui, _ = archive_objects(executable, expected['python_archive_version'])
    verify('gui', gui, expected['gui_marshal_sha256'])
    root = marshal.loads(gui)
    codes = {code.co_qualname: code for code in code_objects(root)}
    loop = codes['StrategyThread._run']
    verify('StrategyThread._run', loop.co_code,
           expected['gui_functions']['StrategyThread._run']['bytecode_sha256'])
    name = 'StrategyThread._wait_position_closed'
    verify(name, codes[name].co_code, expected['gui_functions'][name]['bytecode_sha256'])
    return root, codes, loop


def _install_helpers(root, codes, namespace):
    instructions = [op for op in dis.get_instructions(root) if op.opname != 'EXTENDED_ARG']
    constants = {'DAY_SESSIONS_HM', 'NIGHT_OPEN_HM', 'NIGHT_CLOSE_DEFAULT_HM', 'NIGHT_CLOSE_0100_KEYS',
                 'NIGHT_CLOSE_0230_KEYS', 'STALE_SESSION_GRACE_SECONDS'}
    for previous, op in zip(instructions, instructions[1:]):
        if op.opname == 'STORE_NAME' and op.argval in constants:
            namespace[op.argval] = previous.argval
    for name in _HELPERS:
        code = codes[name]
        index = next(i for i, op in enumerate(instructions) if op.argval is code)
        previous = instructions[index - 1]
        defaults = previous.argval if previous.opname == 'LOAD_CONST' and isinstance(previous.argval, tuple) else None
        namespace[name] = FunctionType(code, namespace, name, defaults)


class _SdkFeed:
    def __init__(self, frames, scheduler=None):
        self.frames = iter(frames)
        self.frame = None
        self.clock = datetime.fromisoformat(frames[0]['wall_time'])
        self.rows = []
        self.targets = []
        self.entry_rejections = []
        self.quotes = {}
        self.positions = {}
        self.bars = {}
        self.trades = {}
        self.orders = {}
        self.scheduler = scheduler
        self.account = _Entity(balance=100000, available=100000, margin=0, pre_balance=100000,
                               static_balance=100000, close_profit=0, position_profit=0, commission=0,
                               risk_ratio=0, float_profit=0, frozen_margin=0)
        self._apply(frames[0])

    def _apply(self, frame):
        self.clock = datetime.fromisoformat(frame['wall_time'])
        for market in frame['market']:
            contract = market['contract_id']
            quote = self.quotes.setdefault(contract, _Entity(instrument_id=contract.split('.')[1],
                exchange_id=contract.split('.')[0], volume_multiple=float(market.get('multiplier', 10)),
                price_tick=1, underlying_symbol='', trading_time={}, expired=False, pre_close=1060,
                last_price=float('nan'), highest=float('nan'), lowest=float('nan'), datetime=''))
            quote['expire_datetime'] = market.get('expiry_date', quote.get('expire_datetime', ''))
            for tick in market['ticks']:
                time = datetime.fromisoformat(tick['native_time']).astimezone(_BEIJING)
                quote.update(last_price=float(tick['last']), highest=float(tick['day_high']),
                             lowest=float(tick['day_low']), bid_price1=float(tick['bid1']),
                             ask_price1=float(tick['ask1']), price_tick=float(tick['tick_size']),
                             upper_limit=float(tick['upper_limit']), lower_limit=float(tick['lower_limit']),
                             datetime=time.strftime('%Y-%m-%d %H:%M:%S.%f'), open=float(tick['last']))
                bars = self.bars.setdefault(contract, _Bars())
                if tick['candle_id'] is not None and (not bars or bars[-1]['datetime'] != tick['candle_id']):
                    bars.append({'datetime': tick['candle_id'], 'open': quote.open, 'close': quote.last_price,
                                 'high': quote.highest, 'low': quote.lowest})
            long, short = market['long_qty'], market['short_qty']
            position = self.positions.setdefault(contract, _Entity())
            position.update(pos_long=long, pos_short=short, pos_long_today=long, pos_short_today=short,
                            pos_long_his=0, pos_short_his=0, open_price_long=market.get('open_price_long', 0),
                            open_price_short=market.get('open_price_short', 0), margin=0, float_profit=0)
        for fact in frame.get('native', ()):
            if fact['kind'] == 'ORDER':
                self.orders[fact['native_order_id']] = _Entity(fact | {
                    'status': 'ALIVE' if fact['status'] == 'ALIVE' else 'FINISHED',
                    'order_id': fact['native_order_id']})
            else:
                self.trades[fact['native_trade_id']] = _Entity(fact | {
                    'trade_id': fact['native_trade_id'], 'order_id': fact['native_order_id'],
                    'volume': fact['quantity'], 'price': float(fact['price']),
                    'trade_date_time': int(self.clock.timestamp() * 10**9)})

    def get_quote(self, symbol):
        return self.quotes[symbol]

    def get_position(self, symbol):
        return self.positions[symbol]

    def get_kline_serial(self, symbol, *_args, **_kwargs):
        return self.bars.setdefault(symbol, _Bars())

    def get_account(self):
        return self.account

    def get_order(self):
        return self.orders

    def get_trade(self):
        return self.trades

    def query_quotes(self, **_kwargs):
        return list(self.quotes)

    def is_changing(self, _entity, _key):
        return True

    def wait_update(self, *, deadline):
        caller = sys._getframe(1)
        close_wait = caller.f_code.co_qualname == 'StrategyThread._wait_position_closed'
        if close_wait:
            caller = caller.f_back
        if caller.f_code.co_qualname != 'StrategyThread._run' or (not close_wait and caller.f_lineno != 5892):
            raise _Finished(f'Unexpected SDK update during initialization at line {caller.f_lineno}')
        if self.frame is not None:
            fields = ('direction', 'volume', 'net_loss_points', 'pending_entry', 'days_to_expiry',
                      'expiry_checked', 'no_new_open', 'no_open_reason', 'day_stopped',
                      'last_order_time', 'close_booked', 'last_signal_bar')
            state = {symbol: {key: row.get(key) for key in fields} for symbol, row in caller.f_locals['state'].items()}
            self.rows.append({'id': self.frame['id'], 'targets': self.targets, 'state': deepcopy(state),
                              'entry_trace_schema': SCHEMA, 'entry_rejections': deepcopy(self.entry_rejections)})
            if close_wait or caller.f_locals.get('_close_mode') is not None:
                position_loop = caller.f_locals
                self.rows[-1]['frozen_close'] = {
                    'nested_next_poll': close_wait, 'mode': position_loop.get('_close_mode'),
                    'remaining_seconds': position_loop.get('_secs_left'), 'refresh_allowed': position_loop.get('retry_ok'),
                }
            self.entry_rejections.clear()
            self.targets = []
        self.frame = next(self.frames, None)
        if self.frame is None:
            raise _Finished()
        if self.scheduler is None:
            self._apply(self.frame)
            return True
        try:
            changed = self.scheduler.wait_update(self.frame, self._apply)
        except Exception as error:
            raise _Finished(f'Reference wait_update failed on {self.frame["id"]}: {error}') from error
        if not changed:
            self.clock = datetime.fromisoformat(self.frame['wall_time'])
        return changed


def _deny_external_effects(event, _args):
    if event.startswith(('socket.', 'subprocess.', 'os.spawn', 'os.exec')) or event in {
            'os.system', 'os.startfile', 'ctypes.dlopen'}:
        raise RuntimeError(f'Offline reference blocked {event}')


def replay_original(archive: Path, settings, frames, scheduler=None):
    if sys.version_info[:2] != (3, 14):
        raise ValueError('The frozen application requires the isolated Python 3.14 interpreter')
    root, codes, loop = _load(archive)
    sdk = _SdkFeed(frames, scheduler)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return sdk.clock.astimezone(tz or _BEIJING).replace(tzinfo=tz)

    class Target:
        def __init__(self, api, symbol, **kwargs):
            if api is not sdk:
                raise ValueError('Unexpected SDK resource')
            self.symbol = symbol
            if scheduler is not None:
                scheduler.bind_target(symbol, kwargs['price'])

        def set_target_volume(self, volume):
            sdk.targets.append({'contract_id': self.symbol, 'volume': volume})
            if scheduler is not None:
                scheduler.set_target_volume(volume)

    def import_offline(name, *_args, **_kwargs):
        if name == 'tqsdk':
            return SimpleNamespace(TargetPosTask=Target)
        if name == 'json':
            return json
        if name in ('time', '_strptime'):
            return builtins.__import__(name, *_args, **_kwargs)
        raise ImportError(f'Unexpected reference import: {name}')

    def blocked_thread(**_kwargs):
        raise RuntimeError('Background work disabled during offline replay')

    logs = queue.Queue()
    with tempfile.TemporaryDirectory(prefix='dsv-original-replay-') as directory:
        (Path(directory) / 'state').mkdir()
        namespace = {
            '__builtins__': vars(builtins) | {'__import__': import_offline},
            '__name__': 'offline_reference',
            'time': SimpleNamespace(time=lambda: sdk.clock.timestamp(), monotonic=lambda: sdk.clock.timestamp()),
            'datetime': Clock, 'timedelta': timedelta, 'date': date, 'json': json, 'os': os, 're': re,
            'threading': SimpleNamespace(Thread=blocked_thread, Lock=Lock), 'BASE_DIR': directory,
            'LOG_DIR': directory, 'CONFIG_FILE': str(Path(directory) / 'offline-replay.json'),
            'APP_TITLE': 'DSV', 'APP_VERSION': '', 'APP_SOURCE_VER': '',
            'create_tq_api': lambda *_args, **_kwargs: (sdk, None), 'clear_stale_target_pos': lambda _api: 0,
            '_REPORT_CACHE': {},
            'write_daily_report': blocked_thread, 'save_slip_daily': lambda *_args, **_kwargs: None,
            'record_daily_equity': lambda *_args, **_kwargs: None, 'MARGIN_FEE_TABLE': {},
            '_HOLIDAYS': set(), '_OPTION_RE': re.compile(r'^[A-Za-z]{1,3}\d{3,4}[CP]-?\d+$'),
        }
        _install_helpers(root, codes, namespace)
        namespace['_TPSpy'] = builtins.__build_class__(FunctionType(codes['_TPSpy'], namespace), '_TPSpy')
        owner = SimpleNamespace(config=settings | {'account': {'username': 'OFFLINE', 'password': 'OFFLINE'}},
                                _persistent_state=None, stop_event=Event(), log_queue=logs,
                                account_info={}, account_lock=Lock(), equity_data=[], equity_lock=Lock(),
                                _kline_cache={}, _kline_cache_lock=Lock(), log=logs.put,
                                notify=lambda *_args: None)
        # Run the frozen daily-state writer against this replay's temporary BASE_DIR.
        persist = FunctionType(codes['StrategyThread.persist_daily_state'], namespace, argdefs=(None,))
        owner.persist_daily_state = persist.__get__(owner)
        owner.api = sdk
        wait_closed = FunctionType(codes['StrategyThread._wait_position_closed'], namespace)
        owner._wait_position_closed = wait_closed.__get__(owner)
        observer = FrozenEntryObserver(loop, sdk)
        previous_trace = sys.gettrace()
        if previous_trace is not None:
            raise RuntimeError('Run the source trace in a dedicated process without another tracer')
        try:
            sys.settrace(observer)
            FunctionType(loop, namespace)(owner)
        except SourceEvidenceError as error:
            raise RuntimeError('Frozen entry observation failed; parity is not proved') from error
        except _Finished as stop:
            if stop.args:
                raise RuntimeError(stop.args[0]) from None
        finally:
            sys.settrace(previous_trace)
            if logger := getattr(owner, '_logger', None):
                logger.close()
        if len(sdk.rows) != len(frames):
            raise RuntimeError(f'Reference completed {len(sdk.rows)} of {len(frames)} rounds')
    return sdk.rows, list(logs.queue)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    packet = json.loads(args.input.read_text(encoding='utf-8'))
    sys.addaudithook(_deny_external_effects)
    rows, logs = replay_original(args.archive, packet['settings'], packet['rounds'])
    print(json.dumps({'rounds': rows, 'logs': logs}, ensure_ascii=True))


if __name__ == '__main__':
    main()
