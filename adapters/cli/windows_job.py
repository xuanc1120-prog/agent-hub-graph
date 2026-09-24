"""Windows Job Object ownership for CLI agent process trees."""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes


class WindowsJobError(RuntimeError):
    """A Windows Job Object operation failed."""


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
    """Own one process tree; assignment must happen before process resume."""

    _BASIC_ACCOUNTING = 1
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

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
        handle = self._api.CreateJobObjectW(None, None)
        if not handle:
            raise WindowsJobError(str(ctypes.WinError(ctypes.get_last_error())))
        self._handle: int | None = int(handle)
        self._lock = threading.Lock()

    def assign(self, pid: int) -> None:
        """Assign a still-suspended process to this job."""

        with self._lock:
            handle = self._require_handle()
            process = self._api.OpenProcess(
                self._PROCESS_SET_QUOTA | self._PROCESS_TERMINATE,
                False,
                pid,
            )
            if not process:
                raise WindowsJobError(str(ctypes.WinError(ctypes.get_last_error())))
            try:
                if not self._api.AssignProcessToJobObject(handle, process):
                    raise WindowsJobError(str(ctypes.WinError(ctypes.get_last_error())))
            finally:
                self._api.CloseHandle(process)

    def terminate_and_close(self, *, timeout_seconds: float = 5.0) -> None:
        """Terminate every job member, verify zero active processes, and close once."""

        with self._lock:
            handle = self._handle
            if handle is None:
                return
            try:
                if not self._api.TerminateJobObject(handle, 1):
                    raise WindowsJobError(str(ctypes.WinError(ctypes.get_last_error())))
                deadline = time.monotonic() + timeout_seconds
                while self._active_processes(handle) != 0:
                    if time.monotonic() >= deadline:
                        raise WindowsJobError("job did not reach zero active processes")
                    time.sleep(0.01)
            finally:
                self._api.CloseHandle(handle)
                self._handle = None

    def has_active_processes(self) -> bool:
        """Return whether the owned job still contains a live process."""

        with self._lock:
            return self._active_processes(self._require_handle()) != 0

    def close_empty(self) -> None:
        """Close a job that never received a process."""

        with self._lock:
            handle = self._handle
            if handle is None:
                return
            self._api.CloseHandle(handle)
            self._handle = None

    def _active_processes(self, handle: int) -> int:
        accounting = _Accounting()
        if not self._api.QueryInformationJobObject(
            handle,
            self._BASIC_ACCOUNTING,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise WindowsJobError(str(ctypes.WinError(ctypes.get_last_error())))
        return int(accounting.active_processes)

    def _require_handle(self) -> int:
        if self._handle is None:
            raise WindowsJobError("job handle is closed")
        return self._handle


__all__ = ["WindowsJob", "WindowsJobError"]
