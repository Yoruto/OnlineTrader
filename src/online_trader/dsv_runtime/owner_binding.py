"""Explicit owner bindings for Replacement and Standalone DSV sessions."""

from dataclasses import dataclass

from online_trader.errors import TraderError
from online_trader.runtime.instance import AccountLock, RuntimeLock


@dataclass(frozen=True, slots=True)
class StandaloneOwnerBinding:
    """Prove that a DSV session belongs to the current host Runtime boot.

    The account lease prevents another legacy/replacement sender on this host
    during migration.  The host Runtime lock is the product-level owner; neither
    lock crosses computers or turns local state into broker authority.
    """

    boot_id: str
    account_key: str
    provider: str
    runtime_lock: RuntimeLock
    account_lock: AccountLock

    def validate(self):
        if (not isinstance(self.boot_id, str) or not self.boot_id
                or not isinstance(self.runtime_lock, RuntimeLock) or not self.runtime_lock.matches()
                or not isinstance(self.account_lock, AccountLock)
                or not self.account_lock.matches(self.account_key)):
            raise TraderError('E_DSV_SCOPE', 'Standalone DSV owner binding is not held by this Runtime boot')
        return self
