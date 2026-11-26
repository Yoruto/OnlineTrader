"""Structured order-blocking diagnostics and broker rejection classification."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger('online_trader.order_diagnostics')

_TRANSIENT_RULES = (
    (('集合竞价', '非连续交易', '撮合', '未开盘', '未开市', '休市', '停牌', '暂停交易',
      '当前状态禁止', '当前时间禁止', 'non-continuous', 'auction', 'not trading',
      'market closed', 'halted'), 'BROKER_MARKET_PHASE'),
    (('流控', '频率', '超速', 'throttle', 'rate limit', 'frequency'), 'BROKER_RATE_LIMITED'),
    (('繁忙', '排队', 'busy'), 'BROKER_BUSY'),
    (('未就绪', '尚未就绪', '未初始化', '正在初始化', 'not ready', 'initializing'), 'BROKER_NOT_READY'),
    (('断开', '断线', '未连接', 'disconnect', 'reconnect', '通信', '通讯', '网络'), 'BROKER_DISCONNECTED'),
    (('超时', 'timeout'), 'BROKER_TIMEOUT'),
    (('暂停', 'temporar', 'try again', '重试', '稍后'), 'BROKER_TEMP_STATE'),
)

_STRUCTURAL_RULES = (
    (('平仓量超过持仓', '平今仓位不足', '平昨仓位不足', '可平量不足', '可用持仓不足', '持仓不足', '仓位不足',
      'insufficient position', 'close volume'), 'INSUFFICIENT_CLOSE_POSITION'),
    (('无交易权限', '没有交易权限', '禁止交易', '禁止开仓', '禁止平仓', '未授权', 'permission',
      'not authorized', 'not permitted'), 'NO_TRADING_PERMISSION'),
    (('合约不存在', '代码不存在', 'unknown instrument', 'invalid symbol'), 'INVALID_INSTRUMENT'),
    (('涨跌停', '超出价格', '最小变动价位', 'price limit', 'price tick'), 'ILLEGAL_ORDER_PRICE'),
    (('非法', '无效', '委托数量', '开平标志', '不合法', 'illegal', 'invalid', 'bad offset'), 'INVALID_ORDER_PARAMETERS'),
    (('资金不足', '可用资金不足', 'insufficient fund'), 'INSUFFICIENT_FUNDS'),
)


def classify_broker_rejection(message):
    """Classify a raw broker rejection message into a safe category and detail code."""
    text = str(message or '').strip()
    if not text:
        return 'UNCLASSIFIED', 'UNCLASSIFIED_BROKER_REJECT'
    lowered = text.lower()
    for tokens, detail in _TRANSIENT_RULES:
        if any(token in lowered for token in tokens):
            return 'TRANSIENT', detail
    for tokens, detail in _STRUCTURAL_RULES:
        if any(token in lowered for token in tokens):
            return 'STRUCTURAL', detail
    return 'UNCLASSIFIED', 'UNCLASSIFIED_BROKER_REJECT'


def _compact_fields(**fields):
    cleaned = {}
    for key, value in fields.items():
        if value is not None:
            cleaned[key] = value
    return cleaned


class OrderDiagnosticTracker:
    """Emit deduplicated order-blocking and recovery diagnostics to Python logging."""

    def __init__(self, *, account_key=None, boot_id=None, evidence_sink=None):
        self.account_key = account_key
        self.boot_id = boot_id
        self.evidence_sink = evidence_sink
        self._active_blocks = {}
        self._transitions = {}

    def bind_evidence_sink(self, sink):
        self.evidence_sink = sink
        return self

    def emit(self, event_type, *, level=logging.WARNING, component='ORDER', priority=None,
             log_to_logger=True, **fields):
        payload = _compact_fields(
            at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            event=event_type,
            account_key=self.account_key,
            runtime_boot_id=self.boot_id,
            **fields,
        )
        if log_to_logger:
            _LOGGER.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if self.evidence_sink is not None:
            try:
                correlation = {
                    key: str(fields[key])
                    for key in (
                        'position_id', 'child_order_id', 'effect_id',
                        'contract_id', 'candle_id', 'native_order_id', 'native_trade_id',
                        'command_id',
                    )
                    if fields.get(key) is not None
                }
                safe = {
                    key: fields[key]
                    for key in (
                        'contract_id', 'effect', 'reason', 'previous_reason',
                        'stage', 'category', 'broker_state', 'native_offset',
                        'sequence', 'attempt', 'entry_stopped', 'direction',
                        'quantity', 'filled_qty', 'original_qty', 'limit_price',
                        'cancel_reason', 'terminal_confirmed', 'owned_after',
                        'price', 'theory', 'cap', 'last_price', 'day_high',
                        'day_low', 'day_stop', 'target_count', 'candle_id',
                        'long_trigger', 'short_trigger', 'open_offset',
                        'stop_offset', 'profit_offset', 'configuration_revision',
                        'entry_authority', 'runtime_mode', 'strategy_intent',
                    )
                    if fields.get(key) is not None
                    and type(fields[key]) in {str, int, float, bool}
                }
                error_code = fields.get('error_code')
                if not isinstance(error_code, str) or not error_code.startswith('E_'):
                    error_code = None
                reason_code = fields.get('reason')
                if not isinstance(reason_code, str) or not reason_code:
                    reason_code = None
                resolved_priority = priority or (
                    'CRITICAL' if event_type in {'EFFECT_DISPATCH_UNKNOWN', 'SDK_SEND_UNCERTAIN_EXCEPTION'}
                    else 'HIGH'
                )
                self.evidence_sink.record(
                    event_type, component=component, priority=resolved_priority,
                    severity='ERROR' if level >= logging.ERROR or error_code else
                    'WARNING' if level >= logging.WARNING else 'INFO',
                    reason_code=reason_code,
                    error_code=error_code,
                    correlation=correlation,
                    payload=safe,
                )
            except Exception:
                pass
        return payload

    def emit_transition(self, key, signature, event_type, **kwargs):
        """Emit an evidence event only when ``signature`` changes for ``key``."""
        if self._transitions.get(key) == signature:
            return None
        self._transitions[key] = signature
        return self.emit(event_type, log_to_logger=False, **kwargs)

    def observe_block(self, key, *, active, contract_id=None, effect=None,
                      reason=None, detail=None, position_id=None, child_order_id=None,
                      dedupe_signature=None, **extra):
        """Log state transitions into or out of a blocked order state without tick spam."""
        if not active:
            previous = self._active_blocks.pop(key, None)
            if previous is not None:
                self.emit(
                    'ORDER_UNBLOCKED',
                    level=logging.INFO,
                    contract_id=contract_id,
                    effect=effect,
                    previous_reason=previous[1],
                    previous_detail=previous[2],
                    position_id=position_id,
                    child_order_id=child_order_id,
                )
            return False
        signature = (reason, detail) if dedupe_signature is None else dedupe_signature
        existing = self._active_blocks.get(key)
        if existing is not None and existing[0] == signature:
            return False
        self._active_blocks[key] = (signature, reason, detail)
        self.emit(
            'ORDER_BLOCKED',
            level=logging.WARNING,
            contract_id=contract_id,
            effect=effect,
            reason=reason,
            detail=detail,
            position_id=position_id,
            child_order_id=child_order_id,
            **extra,
        )
        return True


class _JsonLineFileHandler(logging.FileHandler):
    """Append-only UTF-8 file handler writing pure JSON lines emitted by OrderDiagnosticTracker."""

    def emit(self, record):
        if self.stream is None:
            self.stream = self._open()
        try:
            message = record.getMessage()
            self.stream.write(message + '\n')
            self.flush()
        except Exception:
            self.handleError(record)


@contextmanager
def attach_order_blocking_log(output_dir, *, filename='order-blocking-log.jsonl'):
    """Temporarily attach a persistent JSONL file handler to order diagnostics for one boot."""
    path = Path(output_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _JsonLineFileHandler(path, mode='a', encoding='utf-8', delay=True)
    handler.setLevel(logging.INFO)
    _LOGGER.addHandler(handler)
    old_level = _LOGGER.level
    if old_level == logging.NOTSET or old_level > logging.INFO:
        _LOGGER.setLevel(logging.INFO)
    try:
        yield path
    finally:
        _LOGGER.removeHandler(handler)
        if old_level == logging.NOTSET or old_level > logging.INFO:
            _LOGGER.setLevel(old_level)
        handler.close()
