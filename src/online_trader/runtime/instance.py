"""Host-wide account lock, independent of data path and terminal session."""

import ctypes
from ctypes import wintypes
import hashlib
import os
import threading

from online_trader.errors import TraderError


CLOCK_GATE = r"Global\OnlineTrader.ClockMaintenance.Gate.v1"
CLOCK_OWNERS = r"Global\OnlineTrader.ClockMaintenance.Owners.v1"
HOST_RUNTIME_KEY = 'HOST:RUNTIME:OWNER:V1'


class ClockActivity:
    """Shared owner participation in the Windows clock maintenance protocol.

    The gate serializes admission with maintenance. Every execution owner keeps
    one handle to the same event; Windows closes it on a process crash.
    """

    def __init__(self):
        if os.name != 'nt':
            raise TraderError('E_PLATFORM', 'Windows is required for clock maintenance coordination')
        self.handle = None
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        handles = wintypes.HANDLE
        pointer = ctypes.c_void_p
        descriptors = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        descriptors.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), pointer)
        descriptors.restype = wintypes.BOOL
        kernel.CreateMutexW.argtypes = (pointer, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = handles
        kernel.CreateEventW.argtypes = (pointer, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateEventW.restype = handles
        kernel.WaitForSingleObject.argtypes = (handles, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        for function in (kernel.ReleaseMutex, kernel.CloseHandle, kernel.LocalFree):
            function.argtypes = (handles,)
        kernel.LocalFree.restype = handles

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [('length', wintypes.DWORD), ('descriptor', pointer), ('inherit', wintypes.BOOL)]

        descriptor = pointer()
        if not descriptors('D:P(A;;GA;;;AU)(A;;GA;;;SY)', 1, ctypes.byref(descriptor), None):
            raise TraderError('E_CLOCK_MAINTENANCE', 'Cannot establish the clock coordination ACL')
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        gate = None
        locked = False
        try:
            gate = kernel.CreateMutexW(ctypes.byref(attrs), False, CLOCK_GATE)
            if not gate:
                raise TraderError('E_CLOCK_MAINTENANCE', 'Clock maintenance gate is unavailable')
            locked = kernel.WaitForSingleObject(gate, 250) in (0, 0x80)
            if not locked:
                raise TraderError('E_CLOCK_MAINTENANCE', 'Clock maintenance is active; owner startup deferred')
            self.handle = kernel.CreateEventW(ctypes.byref(attrs), True, False, CLOCK_OWNERS)
            if not self.handle:
                raise TraderError('E_CLOCK_MAINTENANCE', 'Cannot register the execution owner for clock maintenance')
            self.kernel = kernel
        finally:
            if locked:
                kernel.ReleaseMutex(gate)
            if gate:
                kernel.CloseHandle(gate)
            kernel.LocalFree(descriptor)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class AccountLock:
    def __init__(self, account_key: str) -> None:
        self._handle = None
        self.account_key=account_key
        self.owner_thread=threading.get_ident()
        if os.name != "nt":
            raise TraderError("E_PLATFORM", "Windows is required for the execution owner")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

        class Attributes(ctypes.Structure):
            _fields_ = [("length",wintypes.DWORD),("descriptor",ctypes.c_void_p),("inherit",wintypes.BOOL)]

        descriptor = ctypes.c_void_p()
        # OWNER RIGHTS + SYSTEM. An inaccessible existing mutex is a denial, never a bypass.
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;;GA;;;OW)(A;;GA;;;SY)",1,ctypes.byref(descriptor),None):
            raise TraderError("E_INSTANCE_LOCK", "Cannot create account lock security")
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        digest = hashlib.sha256(account_key.encode("utf-8")).hexdigest()
        try:
            ctypes.set_last_error(0)
            handle = kernel.CreateMutexW(ctypes.byref(attributes),True,"Global\\OnlineTrader.Account."+digest)
            error = ctypes.get_last_error()
        finally:
            kernel.LocalFree(descriptor)
        if not handle:
            raise TraderError("E_INSTANCE_LOCK", "Account execution lock unavailable")
        if error == 183:
            kernel.CloseHandle(handle)
            raise TraderError("E_INSTANCE_EXISTS", "An execution owner already holds this account")
        self._kernel, self._handle = kernel, handle

    def close(self) -> None:
        if self._handle is not None:
            self._kernel.ReleaseMutex(self._handle)
            self._kernel.CloseHandle(self._handle)
            self._handle = None

    def matches(self,account_key):
        return self._handle is not None and self.account_key==account_key and self.owner_thread==threading.get_ident()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class RuntimeLock:
    """One Standalone Runtime owner per Windows host.

    Windows named mutexes are machine-local.  Using a constant host key blocks
    a second Runtime on this computer while deliberately imposing no limit on
    another computer logging into the same broker account.
    """

    def __init__(self):
        self._lock = AccountLock(HOST_RUNTIME_KEY)
        self.owner_thread = threading.get_ident()

    def matches(self):
        return self._lock._handle is not None and self.owner_thread == threading.get_ident()

    def close(self):
        self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
