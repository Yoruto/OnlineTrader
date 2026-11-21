"""Process-owned execution storage for one broker-synchronized Runtime boot."""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4


class RuntimeSessionStorage:
    """Create a disposable ledger that is never a cross-boot authority."""

    def __init__(self):
        self._temporary = None
        self.directory = None
        self.ledger = None
        self.boot_id = None

    def __enter__(self):
        self._temporary = TemporaryDirectory(prefix='online-trader-runtime-session-')
        self.directory = Path(self._temporary.name).resolve()
        self.ledger = self.directory / 'ledger.db'
        self.boot_id = 'runtime-' + uuid4().hex
        return self

    def __exit__(self, *_):
        temporary, self._temporary = self._temporary, None
        self.boot_id = None
        if temporary is not None:
            temporary.cleanup()

    def owner_binding(self, runtime_lock, account_lock, *, account_key, provider):
        """Bind this entered temporary store to the current Runtime owner."""
        if self._temporary is None or self.boot_id is None or self.ledger is None:
            raise RuntimeError('Runtime session storage is not active')
        from online_trader.dsv_runtime.owner_binding import StandaloneOwnerBinding
        return StandaloneOwnerBinding(self.boot_id, account_key, provider,
                                      runtime_lock, account_lock).validate()
