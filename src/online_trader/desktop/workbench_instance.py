"""Same-user, non-trading activation channel for the sole Runtime Workbench."""

import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from online_trader.errors import TraderError


_SERVER_NAME = 'OnlineTrader.RuntimeWorkbench.v1'
_SHOW = b'SHOW_WORKBENCH\n'


class WorkbenchActivationServer(QObject):
    activation_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = QLocalServer(self)
        self._sockets = set()
        self.server.newConnection.connect(self._accept)

    def listen(self):
        # The caller already owns the host-wide Workbench mutex, so an endpoint
        # with this name is stale and cannot belong to another live Workbench.
        QLocalServer.removeServer(_SERVER_NAME)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(_SERVER_NAME):
            raise TraderError('E_INSTANCE_LOCK', 'Cannot open the Workbench activation channel')

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._sockets.add(socket)
            socket.readyRead.connect(lambda current=socket: self._read(current))
            socket.disconnected.connect(lambda current=socket: self._discard(current))
            self._read(socket)

    def _read(self, socket):
        if not socket.bytesAvailable():
            return
        if bytes(socket.readAll()) == _SHOW:
            self.activation_requested.emit()
        if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            socket.disconnectFromServer()

    def _discard(self, socket):
        # QLocalServer owns accepted sockets.  By the time disconnected is
        # delivered on Windows, Qt may already have destroyed the native
        # object; only release our Python reference here.
        self._sockets.discard(socket)


def request_existing_workbench(timeout_ms=1200):
    """Ask the existing Workbench to restore itself during a duplicate launch."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        socket = QLocalSocket()
        socket.connectToServer(_SERVER_NAME)
        if socket.waitForConnected(200):
            if socket.write(_SHOW) == len(_SHOW) and socket.waitForBytesWritten(500):
                socket.disconnectFromServer()
                return True
            socket.abort()
        time.sleep(0.05)
    return False
