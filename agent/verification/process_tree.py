"""Bounded cross-platform cleanup for one verification process tree."""

import ctypes
import os
import signal
import subprocess
from ctypes import wintypes

_TERMINATION_WAIT_SECONDS = 1.0
_TASKKILL_WAIT_SECONDS = 0.5


class ProcessTree:
    """Own an optional Windows Job Object and terminate without infinite waits."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._windows_job = _create_windows_job(process) if os.name == "nt" else None

    def terminate(self) -> None:
        if os.name == "posix":
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            _taskkill(self._process.pid)
            if self._windows_job is not None:
                _terminate_windows_job(self._windows_job)
        _bounded_process_wait(self._process)

    def close(self) -> None:
        if self._windows_job is not None:
            _close_windows_handle(self._windows_job)
            self._windows_job = None


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_windows_job(process: subprocess.Popen[bytes]) -> int | None:
    job: int | None = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        raw_job = kernel32.CreateJobObjectW(None, None)
        if not raw_job:
            return None
        job = int(raw_job)
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        configured = kernel32.SetInformationJobObject(
            raw_job,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            raw_job, wintypes.HANDLE(process._handle)
        )
    except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
        assigned = False
    if not assigned:
        if job is not None:
            _close_windows_handle(job)
        return None
    return job


def _terminate_windows_job(job: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject(wintypes.HANDLE(job), 1)


def _close_windows_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _taskkill(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TASKKILL_WAIT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _bounded_process_wait(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=_TERMINATION_WAIT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
        process.wait(timeout=_TERMINATION_WAIT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return
