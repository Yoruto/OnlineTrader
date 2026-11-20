"""Process-owned execution storage for one broker-synchronized Runtime boot."""

from pathlib import Path
from tempfile import TemporaryDirectory


class RuntimeSessionStorage:
    """Create a disposable ledger that is never a cross-boot authority."""

    def __init__(self):
        self._temporary = None
        self.directory = None
        self.ledger = None

    def __enter__(self):
        self._temporary = TemporaryDirectory(prefix='online-trader-runtime-session-')
        self.directory = Path(self._temporary.name).resolve()
        self.ledger = self.directory / 'ledger.db'
        return self

    def __exit__(self, *_):
        temporary, self._temporary = self._temporary, None
        if temporary is not None:
            temporary.cleanup()
