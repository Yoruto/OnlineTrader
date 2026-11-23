"""Structured order-blocking diagnostics and broker rejection classification."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger('online_trader.order_diagnostics')

_TRANSIENT_RULES = (
    (('流控', '频率', '超速', 'throttle', 'rate limit', 'frequency'), 'BROKER_RATE_LIMITED'),
    (('繁忙', '排队', 'busy'), 'BROKER_BUSY'),
    (('未就绪', '尚未就绪', '未初始化', '正在初始化', 'not ready', 'initializing'), 'BROKER_NOT_READY'),
    (('断开', '断线', '未连接', 'disconnect', 'reconnect', '通信', '通讯', '网络'), 'BROKER_DISCONNECTED'),
    (('超时', 'timeout'), 'BROKER_TIMEOUT'),
    (('集合竞价', '非连续交易', '暂停', 'temporar', 'try again', '重试', '稍后'), 'BROKER_TEMP_STATE'),
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

    def __init__(self, *, account_key=None, boot_id=None):
        self.account_key = account_key
        self.boot_id = boot_id
        self._active_blocks = {}

    def emit(self, event_type, *, level=logging.WARNING, **fields):
        payload = _compact_fields(
            at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            event=event_type,
            account_key=self.account_key,
            runtime_boot_id=self.boot_id,
            **fields,
        )
        _LOGGER.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return payload

    def observe_block(self, key, *, active, contract_id=None, effect=None,
                      reason=None, detail=None, position_id=None, child_order_id=None,
                      **extra):
        """Log state transitions into or out of a blocked order state without tick spam."""
        if not active:
            previous = self._active_blocks.pop(key, None)
            if previous is not None:
                self.emit(
                    'ORDER_UNBLOCKED',
                    level=logging.INFO,
                    contract_id=contract_id,
                    effect=effect,
                    previous_reason=previous[0],
                    previous_detail=previous[1],
                    position_id=position_id,
                    child_order_id=child_order_id,
                )
            return False
        signature = (reason, detail)
        if self._active_blocks.get(key) == signature:
            return False
        self._active_blocks[key] = signature
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
