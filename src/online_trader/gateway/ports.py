"""The only sender interface, injected by independent development QA."""

from typing import Protocol

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
    def bind(self,kernel):
        self.kernel=kernel;self.callback_token=object();self.source_epoch=kernel.kernel_epoch
    def validate_send(self,request):raise NotImplementedError
    def send(self,request):raise NotImplementedError
    def cancel(self,request):raise NotImplementedError
