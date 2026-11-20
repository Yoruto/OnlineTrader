"""An order's possible quantity is independent of cancellation request state."""

from dataclasses import asdict, dataclass

from online_trader.domain.values import quantity


@dataclass
class OrderState:
    child_order_id: str
    intent_id: str
    attempt_id: str
    effect: str
    original_qty: int
    limit_price: str
    filled_qty: int = 0
    broker_state: str = "NOT_SEEN"
    send_state: str = "SEND_POSSIBLE"
    terminal_confirmed: bool = False
    reported_fill_qty: int | None = None
    cancel_status: str | None = None
    revision: int = 1
    kernel_boot_id: str = ""
    parent_child_order_id: str | None = None
    ordinal: int = 1
    native_offset: str | None = None
    native_trading_day: str | None = None
    close_route: dict | None = None
    pricing_quote_id: str | int | None = None
    pricing_source_epoch: int | None = None

    @property
    def potential_remaining(self) -> int:
        quantity(self.original_qty, positive=True)
        quantity(self.filled_qty)
        if (self.terminal_confirmed and self.reported_fill_qty == self.filled_qty
                and self.send_state not in {'UNKNOWN', 'SEND_POSSIBLE'}):
            return 0
        return max(0, self.original_qty - self.filled_qty)

    def to_dict(self) -> dict:
        value=asdict(self)
        if self.native_offset is None:value.pop('native_offset')
        if self.native_trading_day is None:value.pop('native_trading_day')
        if self.close_route is None:value.pop('close_route')
        if self.pricing_quote_id is None:value.pop('pricing_quote_id')
        if self.pricing_source_epoch is None:value.pop('pricing_source_epoch')
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "OrderState":
        return cls(**value)
