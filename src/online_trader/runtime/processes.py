"""Windows parent identity, including the venv launcher/runtime distinction."""

import ctypes
from ctypes import wintypes

from online_trader.errors import TraderError


def parent_pid(process_id: int) -> int | None:
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_=[("dwSize",wintypes.DWORD),("cntUsage",wintypes.DWORD),("th32ProcessID",wintypes.DWORD),
            ("th32DefaultHeapID",ctypes.c_size_t),("th32ModuleID",wintypes.DWORD),("cntThreads",wintypes.DWORD),
            ("th32ParentProcessID",wintypes.DWORD),("pcPriClassBase",wintypes.LONG),("dwFlags",wintypes.DWORD),("szExeFile",wintypes.WCHAR*260)]
    kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD];kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==ctypes.c_void_p(-1).value:raise TraderError("E_AUTH","Cannot verify process ancestry")
    try:
        entry=Entry();entry.dwSize=ctypes.sizeof(Entry)
        valid=kernel.Process32FirstW(handle,ctypes.byref(entry))
        while valid:
            if entry.th32ProcessID==process_id:return entry.th32ParentProcessID
            valid=kernel.Process32NextW(handle,ctypes.byref(entry))
        return None
    finally:kernel.CloseHandle(handle)
