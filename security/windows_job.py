"""Windows test-process ownership, including descendants whose parent exits."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("total_user", ctypes.c_longlong),
        ("total_kernel", ctypes.c_longlong),
        ("period_user", ctypes.c_longlong),
        ("period_kernel", ctypes.c_longlong),
        ("total_page_faults", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("terminated_processes", wintypes.DWORD),
    ]


class WindowsJob:
    """Only instantiate on Windows; assignment must precede process resume."""

    def __init__(self) -> None:
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self._api, name)
            function.argtypes = arguments
            function.restype = result
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid: int) -> None:
        process = self._api.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._api.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._api.CloseHandle(process)

    def terminate(self) -> None:
        if not self._api.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 5
        while True:
            accounting = _Accounting()
            if not self._api.QueryInformationJobObject(
                self._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.active_processes == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("test process job did not terminate")
            time.sleep(0.01)

    def close(self) -> None:
        self._api.CloseHandle(self._handle)
