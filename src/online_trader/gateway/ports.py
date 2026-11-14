"""The only sender interface, injected by independent development QA."""

from typing import Protocol

from online_trader.contracts.codec import content_hash
from online_trader.domain.values import identifier
from online_trader.errors import TraderError


class OrderPort(Protocol):
    def send(self, request: dict) -> object: ...
    def cancel(self, request: dict) -> object: ...


class UnavailablePort:
    def send(self, request: dict) -> object:
        raise TraderError("E_UNSUPPORTED", "No broker adapter is installed")

    def cancel(self, request: dict) -> object:
        raise TraderError("E_UNSUPPORTED", "No broker adapter is installed")


class OnlineSimulationPort:
    """Trusted in-process optional adapter boundary; never accepted by DEVELOPMENT."""
    gateway_identity = 'TQKQ'
    trade_prefix = 'kq-'

    @property
    def _encoded_trade_prefix(self):
        return self.trade_prefix.removesuffix('-') + ':sha256:'

    def trade_key(self, native_id):
        if type(native_id) is not str or not native_id:
            raise TraderError('E_FACT_BARRIER', 'Invalid native trade identity')
        legacy = self.trade_prefix + native_id
        try:
            return identifier(legacy)
        except TraderError:
            pass
        if len(native_id) > 512 or not native_id.isprintable() or not native_id.strip():
            raise TraderError('E_FACT_BARRIER', 'Invalid native trade identity')
        # The prefix is outside the legacy namespace even when the original
        # SDK ID happens to spell another trade's encoded identity.
        return self._encoded_trade_prefix + content_hash(native_id)

    def trade_identity(self, native_id):
        key = self.trade_key(native_id)
        if key.startswith(self._encoded_trade_prefix):
            return {'trade_key': key, 'native_trade_id': native_id}
        return {'trade_key': key}

    def native_trade_id(self, fill):
        key = fill['trade_key']
        if key.startswith(self.trade_prefix):
            return key.removeprefix(self.trade_prefix)
        native_id = fill.get('native_trade_id')
        if native_id is None or self.trade_key(native_id) != key:
            raise TraderError('E_FACT_BARRIER', 'Trade key does not bind its original native identity')
        return native_id

    def is_native_trade_key(self, key):
        return key.startswith((self.trade_prefix, self._encoded_trade_prefix))

    def bind(self,kernel):
        self.kernel=kernel;self.callback_token=object();self.source_epoch=kernel.kernel_epoch
    def validate_send(self,request):raise NotImplementedError
    def send(self,request):raise NotImplementedError
    def cancel(self,request):raise NotImplementedError
