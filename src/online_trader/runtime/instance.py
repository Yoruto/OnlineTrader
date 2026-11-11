"""Host-wide account lock, independent of data path and terminal session."""

import ctypes
from ctypes import wintypes
import hashlib
import os
import threading

from online_trader.errors import TraderError


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
