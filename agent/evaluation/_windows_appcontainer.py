"""Win32 implementation behind the public AppContainer execution seam."""

from __future__ import annotations

import ctypes
import math
import msvcrt
import os
import stat
import subprocess
import shutil
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from agent.evaluation.windows_sandbox import (
    SandboxErrorCode,
    SandboxResult,
    SandboxStatus,
    _ValidatedCommand,
)


_ERROR_INSUFFICIENT_BUFFER = 122
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 1
_FILE_SHARE_WRITE = 2
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x00000004
_FILE_TRAVERSE = 0x00000020
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_ALL_ACCESS = 0x001F01FF
_FILE_READ_EXECUTE = 0x001200A9
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_GRANT_ACCESS = 1
_TRUSTEE_IS_SID = 0
_TRUSTEE_IS_USER = 1

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_userenv = ctypes.WinDLL("userenv", use_last_error=True)

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
SIZE_T = ctypes.c_size_t
HRESULT = ctypes.c_long


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", BOOL),
    ]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", DWORD),
        ("Reserved", DWORD),
    ]


class _Trustee(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", DWORD),
        ("TrusteeForm", DWORD),
        ("TrusteeType", DWORD),
        ("ptstrName", ctypes.c_void_p),
    ]


class _ExplicitAccess(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", DWORD),
        ("grfAccessMode", DWORD),
        ("grfInheritance", DWORD),
        ("Trustee", _Trustee),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
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


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class _CpuRateControlInformation(ctypes.Structure):
    _fields_ = [("ControlFlags", DWORD), ("CpuRate", DWORD)]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", DWORD),
        ("TotalProcesses", DWORD),
        ("ActiveProcesses", DWORD),
        ("TotalTerminatedProcesses", DWORD),
    ]


class _Win32Failure(Exception):
    def __init__(self, code: SandboxErrorCode, message: str):
        self.code = code
        self.message = message


class _OutputDrain:
    def __init__(self, handle: int, limit: int):
        self.handle = handle
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.failure: OSError | None = None

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._read, daemon=True)
        thread.start()
        return thread

    def _read(self) -> None:
        fd: int | None = None
        stream = None
        handle_owned = True
        try:
            fd = msvcrt.open_osfhandle(self.handle, os.O_RDONLY | os.O_BINARY)
            handle_owned = False
            stream = os.fdopen(fd, "rb", buffering=0)
            fd = None
            while chunk := stream.read(8192):
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        except OSError as error:
            self.failure = error
        finally:
            try:
                if stream is not None:
                    stream.close()
                elif fd is not None:
                    os.close(fd)
                elif handle_owned and not _close_handle(self.handle):
                    raise OSError("Sandbox output pipe handle could not be closed.")
            except OSError as error:
                self.failure = self.failure or error


def execute_appcontainer(command: _ValidatedCommand) -> SandboxResult:
    started_at = time.monotonic()
    profile_name = "swe-agent-" + uuid.uuid4().hex
    sid = ctypes.c_void_p()
    profile_created = False
    scratch_folder: Path | None = None
    job = HANDLE()
    process_info = _ProcessInformation()
    attr_buffer: ctypes.Array[ctypes.c_char] | None = None
    attribute_values: tuple[object, ...] = ()
    pipe_handles: list[int] = []
    readers: list[_OutputDrain] = []
    reader_threads: list[threading.Thread] = []
    started = False
    token_is_appcontainer = False
    assigned_to_job = False
    job_terminated = False
    exit_code: int | None = None
    timed_out = False
    output_error = False
    cpu_seconds = 0.0
    peak_memory = 0
    cleanup_complete = True
    error_code: SandboxErrorCode | None = None
    message = ""

    try:
        sid = _create_profile(profile_name)
        profile_created = True
        scratch_folder = command.workspace_root / (".s5b-runtime-" + uuid.uuid4().hex)
        scratch_folder.mkdir()
        # AppContainer's Windows temp resolution uses the package's AC\Temp
        # directory below LOCALAPPDATA, even when TEMP is explicitly supplied.
        temp_folder = scratch_folder / "Local" / "Packages" / profile_name / "AC" / "Temp"
        roaming_folder = scratch_folder / "Roaming"
        temp_folder.mkdir(parents=True, exist_ok=True)
        for folder in (roaming_folder, scratch_folder / "Local", scratch_folder / "Profile"):
            folder.mkdir(exist_ok=True)

        _grant_workspace_acl(command.workspace_root, sid)
        if command.python_toolchain_root is not None:
            _grant_python_toolchain_acl(command.python_toolchain_root, sid)
        environment = _environment_block(command, scratch_folder, temp_folder, roaming_folder)
        stdout_read, stdout_write = _create_pipe(pipe_handles)
        stderr_read, stderr_write = _create_pipe(pipe_handles)
        stdin_handle = _open_nul_input(pipe_handles)
        _make_non_inheritable(stdout_read)
        _make_non_inheritable(stderr_read)

        job = _create_limited_job(command)
        startup, attr_buffer, attribute_values = _startup_info(
            sid, stdin_handle, stdout_write, stderr_write
        )
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command.argv)))
        environment_buffer = ctypes.create_unicode_buffer(environment)
        created = _kernel32.CreateProcessW(
            command.argv[0],
            command_line,
            None,
            None,
            True,
            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
            ctypes.cast(environment_buffer, ctypes.c_void_p),
            str(command.cwd),
            ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_StartupInfo)),
            ctypes.byref(process_info),
        )
        if not created:
            raise _Win32Failure(SandboxErrorCode.SPAWN_FAILED, "AppContainer process creation failed.")
        started = True
        _close_handle(stdout_write)
        _close_handle(stderr_write)
        _close_handle(stdin_handle)
        for handle in (stdout_write, stderr_write, stdin_handle):
            if handle in pipe_handles:
                pipe_handles.remove(handle)

        if not _kernel32.AssignProcessToJobObject(job, process_info.hProcess):
            if not _kernel32.TerminateProcess(process_info.hProcess, 1):
                raise _Win32Failure(
                    SandboxErrorCode.CLEANUP_FAILED,
                    "Unassigned AppContainer process could not be terminated.",
                )
            if _kernel32.WaitForSingleObject(process_info.hProcess, 3000) != _WAIT_OBJECT_0:
                raise _Win32Failure(
                    SandboxErrorCode.CLEANUP_FAILED,
                    "Unassigned AppContainer process did not stop within the cleanup bound.",
                )
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "AppContainer process could not be assigned resource limits.")
        assigned_to_job = True
        token_is_appcontainer = _verify_appcontainer_token(process_info.hProcess)

        stdout_drain = _OutputDrain(stdout_read, command.output_limit_bytes)
        stderr_drain = _OutputDrain(stderr_read, command.output_limit_bytes)
        readers = [stdout_drain, stderr_drain]
        reader_threads = [stdout_drain.start(), stderr_drain.start()]
        for handle in (stdout_read, stderr_read):
            if handle in pipe_handles:
                pipe_handles.remove(handle)
        if _kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            if not _kernel32.TerminateJobObject(job, 1):
                raise _Win32Failure(
                    SandboxErrorCode.CLEANUP_FAILED,
                    "Suspended AppContainer process could not be terminated.",
                )
            job_terminated = True
            if _kernel32.WaitForSingleObject(process_info.hProcess, 3000) != _WAIT_OBJECT_0:
                raise _Win32Failure(
                    SandboxErrorCode.CLEANUP_FAILED,
                    "Suspended AppContainer process did not stop within the cleanup bound.",
                )
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "AppContainer process could not be resumed.")
        _close_handle(process_info.hThread)
        process_info.hThread = None

        wait_ms = max(1, min(int(math.ceil(command.timeout_seconds * 1000)), 0xFFFFFFFE))
        wait_result = _kernel32.WaitForSingleObject(process_info.hProcess, wait_ms)
        if wait_result == _WAIT_TIMEOUT:
            timed_out = True
            if not _kernel32.TerminateJobObject(job, 0xE0000001):
                raise _Win32Failure(SandboxErrorCode.CLEANUP_FAILED, "Timed-out AppContainer process tree could not be terminated.")
            job_terminated = True
            if _kernel32.WaitForSingleObject(process_info.hProcess, 3000) != _WAIT_OBJECT_0:
                cleanup_complete = False
                raise _Win32Failure(SandboxErrorCode.CLEANUP_FAILED, "Timed-out AppContainer process did not stop within the cleanup bound.")
        elif wait_result != _WAIT_OBJECT_0:
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "AppContainer process wait failed.")

        exit_value = DWORD()
        if _kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_value)):
            exit_code = int(exit_value.value)
        else:
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "AppContainer exit status could not be read.")
        cpu_seconds, peak_memory = _job_usage(job)

    except _Win32Failure as error:
        error_code = error.code
        message = error.message
    except OSError as error:
        error_code = SandboxErrorCode.SETUP_FAILED
        message = f"AppContainer setup or output collection failed (winerror={error.winerror})."
    finally:
        if job and assigned_to_job and not job_terminated:
            # A command may exit while leaving children holding inherited pipes,
            # or setup may fail while the primary thread is still suspended.
            if not _kernel32.TerminateJobObject(job, 1):
                cleanup_complete = False
            elif _kernel32.WaitForSingleObject(process_info.hProcess, 3000) != _WAIT_OBJECT_0:
                cleanup_complete = False
        if job:
            if not _close_handle(job):
                cleanup_complete = False
            job = HANDLE()
        for thread in reader_threads:
            thread.join(timeout=3)
            if thread.is_alive():
                cleanup_complete = False
        if any(reader.failure is not None for reader in readers):
            output_error = True
        for handle in pipe_handles:
            if not _close_handle(HANDLE(handle)):
                cleanup_complete = False
        pipe_handles.clear()
        if process_info.hThread:
            if not _close_handle(process_info.hThread):
                cleanup_complete = False
        if process_info.hProcess:
            if not _close_handle(process_info.hProcess):
                cleanup_complete = False
        if profile_created:
            if not _delete_profile(profile_name):
                cleanup_complete = False
        if scratch_folder is not None and not _remove_scratch_folder(scratch_folder, command.workspace_root):
            cleanup_complete = False
        if sid:
            _advapi32.FreeSid(sid)
        if attr_buffer is not None:
            _kernel32.DeleteProcThreadAttributeList(attr_buffer)

    stdout = readers[0].data if readers else bytearray()
    stderr = readers[1].data if len(readers) > 1 else bytearray()
    duration = time.monotonic() - started_at
    if not cleanup_complete:
        error_code = SandboxErrorCode.CLEANUP_FAILED
        message = "AppContainer resources could not be fully cleaned up."
    elif output_error and error_code is None:
        error_code = SandboxErrorCode.CLEANUP_FAILED
        message = "AppContainer output pipes could not be fully drained."
    if timed_out:
        status = SandboxStatus.TIMED_OUT
        error_code = error_code or SandboxErrorCode.TIMEOUT
        message = message or "AppContainer wall-time limit was reached."
    elif error_code is not None or exit_code != 0:
        status = SandboxStatus.FAILED
        error_code = error_code or SandboxErrorCode.PROCESS_FAILED
        message = message or "Sandbox command returned a non-zero status."
    else:
        status = SandboxStatus.COMPLETED
    return SandboxResult(
        status=status,
        error_code=error_code,
        exit_code=exit_code,
        message=message,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        stdout_truncated=readers[0].truncated if readers else False,
        stderr_truncated=readers[1].truncated if len(readers) > 1 else False,
        started=started,
        token_is_appcontainer=token_is_appcontainer,
        cleanup_complete=cleanup_complete,
        duration_seconds=duration,
        cpu_time_seconds=cpu_seconds,
        peak_memory_bytes=peak_memory,
    )


def _create_profile(profile_name: str) -> ctypes.c_void_p:
    sid = ctypes.c_void_p()
    _userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _userenv.CreateAppContainerProfile.restype = HRESULT
    result = _userenv.CreateAppContainerProfile(
        profile_name,
        profile_name,
        "Disposable task command sandbox",
        None,
        0,
        ctypes.byref(sid),
    )
    if result < 0 or not sid:
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "AppContainer profile creation failed for this user.")
    return sid


def _delete_profile(profile_name: str) -> bool:
    _userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    _userenv.DeleteAppContainerProfile.restype = HRESULT
    return _userenv.DeleteAppContainerProfile(profile_name) >= 0


def _remove_scratch_folder(path: Path, workspace_root: Path) -> bool:
    try:
        if path.parent.resolve(strict=True) != workspace_root:
            return False
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            return False
        if not stat.S_ISDIR(path.stat(follow_symlinks=False).st_mode):
            return False
        shutil.rmtree(path)
        return not path.exists()
    except OSError:
        return False


def _verify_appcontainer_token(process: HANDLE) -> bool:
    token = HANDLE()
    _advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    _advapi32.OpenProcessToken.restype = BOOL
    _advapi32.GetTokenInformation.argtypes = [
        HANDLE,
        DWORD,
        ctypes.c_void_p,
        DWORD,
        ctypes.POINTER(DWORD),
    ]
    _advapi32.GetTokenInformation.restype = BOOL
    if not _advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Lowbox process token could not be queried.")
    try:
        value = BOOL()
        returned = DWORD()
        if not _advapi32.GetTokenInformation(
            token, 29, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)
        ):
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Lowbox token identity could not be verified.")
        if not value.value:
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Created process is not an AppContainer.")
        return True
    finally:
        _close_handle(token)


def _grant_workspace_acl(root: Path, sid: ctypes.c_void_p) -> None:
    _add_sid_access(root.parent, sid, _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES, 0)
    _add_sid_access(root, sid, _FILE_ALL_ACCESS, _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
    pending = [root]
    visited = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 100_000:
                        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Task workspace ACL entry limit was exceeded.")
                    path = Path(entry.path)
                    if entry.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Task workspace contains a link or junction.")
                    metadata = path.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        _add_sid_access(path, sid, _FILE_ALL_ACCESS, _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        _add_sid_access(path, sid, _FILE_ALL_ACCESS, 0)
                    else:
                        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Task workspace contains an unsafe filesystem entry.")
        except OSError as error:
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Task workspace ACL could not be applied recursively.") from error


def _grant_python_toolchain_acl(root: Path, sid: ctypes.c_void_p) -> None:
    """Grant the lowbox only read/execute on its copied runtime sibling."""
    pending = [root]
    visited = 0
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or directory.is_junction():
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Python toolchain contains an unsafe link.")
        _add_sid_access(directory, sid, _FILE_READ_EXECUTE, _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 20_000:
                        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Python toolchain ACL entry limit was exceeded.")
                    path = Path(entry.path)
                    if entry.is_symlink() or path.is_junction():
                        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Python toolchain contains an unsafe link.")
                    metadata = path.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        _add_sid_access(path, sid, _FILE_READ_EXECUTE, 0)
                    else:
                        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Python toolchain contains an unsafe entry.")
        except OSError as error:
            raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Python toolchain ACL could not be applied.") from error


def _add_sid_access(path: Path, sid: ctypes.c_void_p, mask: int, inheritance: int) -> None:
    old_dacl = ctypes.c_void_p()
    security_descriptor = ctypes.c_void_p()
    get_security = _advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        DWORD,
        DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = DWORD
    result = get_security(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(old_dacl),
        None,
        ctypes.byref(security_descriptor),
    )
    if result != 0:
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Workspace ACL could not be inspected.")

    entry = _ExplicitAccess()
    entry.grfAccessPermissions = mask
    entry.grfAccessMode = _GRANT_ACCESS
    entry.grfInheritance = inheritance
    entry.Trustee.pMultipleTrustee = None
    entry.Trustee.MultipleTrusteeOperation = 0
    entry.Trustee.TrusteeForm = _TRUSTEE_IS_SID
    entry.Trustee.TrusteeType = _TRUSTEE_IS_USER
    entry.Trustee.ptstrName = sid
    new_dacl = ctypes.c_void_p()
    set_acl = _advapi32.SetEntriesInAclW
    set_acl.argtypes = [DWORD, ctypes.POINTER(_ExplicitAccess), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    set_acl.restype = DWORD
    result = set_acl(1, ctypes.byref(entry), old_dacl, ctypes.byref(new_dacl))
    if result != 0:
        _free_acl(new_dacl, security_descriptor)
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Workspace ACL could not be prepared.")
    set_security = _advapi32.SetNamedSecurityInfoW
    set_security.argtypes = [
        wintypes.LPWSTR,
        DWORD,
        DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security.restype = DWORD
    result = set_security(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        new_dacl,
        None,
    )
    _free_acl(new_dacl, security_descriptor)
    if result != 0:
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Workspace ACL could not be applied.")


def _free_acl(dacl: ctypes.c_void_p, descriptor: ctypes.c_void_p) -> None:
    if dacl:
        _kernel32.LocalFree(dacl)
    if descriptor:
        _kernel32.LocalFree(descriptor)


def _environment_block(
    command: _ValidatedCommand,
    scratch_folder: Path,
    temp_folder: Path,
    roaming_folder: Path,
) -> str:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve(strict=True)
    system32 = system_root / "System32"
    drive = command.workspace_root.drive
    if not drive:
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Task workspace drive is unavailable.")
    appcontainer_home = scratch_folder / "Profile"
    system_drive = system_root.drive or drive
    program_data = str(Path(system_drive + "\\") / "ProgramData")
    program_files = str(Path(system_drive + "\\") / "Program Files")
    public_folder = str(Path(system_drive + "\\") / "Users" / "Public")
    values = {
        "ALLUSERSPROFILE": program_data,
        "APPDATA": str(roaming_folder),
        "ComSpec": str(system32 / "cmd.exe"),
        "LOCALAPPDATA": str(scratch_folder / "Local"),
        "OS": "Windows_NT",
        "Path": str(system32),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "ProgramData": program_data,
        "ProgramFiles": program_files,
        "PUBLIC": public_folder,
        "SystemDrive": system_drive,
        "SystemRoot": str(system_root),
        "TEMP": str(temp_folder),
        "TMP": str(temp_folder),
        "USERPROFILE": str(appcontainer_home),
        "WINDIR": str(system_root),
        "=" + drive: str(command.cwd),
    }
    if command.python_toolchain_root is not None:
        values["PYTHONHOME"] = str(command.python_toolchain_root)
        values["PYTHONNOUSERSITE"] = "1"
        values["PYTHONDONTWRITEBYTECODE"] = "1"
    entries = [f"{key}={value}" for key, value in sorted(values.items(), key=lambda item: item[0].casefold())]
    return "\0".join(entries) + "\0\0"


def _create_pipe(handles: list[int]) -> tuple[int, int]:
    read_handle = HANDLE()
    write_handle = HANDLE()
    security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(HANDLE),
        ctypes.POINTER(HANDLE),
        ctypes.POINTER(_SecurityAttributes),
        DWORD,
    ]
    _kernel32.CreatePipe.restype = BOOL
    if not _kernel32.CreatePipe(ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(security), 0):
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Sandbox output pipes could not be created.")
    handles.extend((int(read_handle.value), int(write_handle.value)))
    return int(read_handle.value), int(write_handle.value)


def _open_nul_input(handles: list[int]) -> int:
    security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        DWORD,
        DWORD,
        ctypes.POINTER(_SecurityAttributes),
        DWORD,
        DWORD,
        HANDLE,
    ]
    _kernel32.CreateFileW.restype = HANDLE
    handle = _kernel32.CreateFileW(
        "NUL",
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        ctypes.byref(security),
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if not handle or handle == HANDLE(-1).value:
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Sandbox stdin could not be initialized.")
    handles.append(int(handle))
    return int(handle)


def _make_non_inheritable(handle: int) -> None:
    if not _kernel32.SetHandleInformation(HANDLE(handle), _HANDLE_FLAG_INHERIT, 0):
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Sandbox pipe inheritance could not be restricted.")


def _create_limited_job(command: _ValidatedCommand) -> HANDLE:
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = HANDLE
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Windows Job Object could not be created.")

    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_PROCESS_TIME
        | _JOB_OBJECT_LIMIT_JOB_TIME
        | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | _JOB_OBJECT_LIMIT_JOB_MEMORY
        | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    limits.BasicLimitInformation.PerProcessUserTimeLimit = int(command.cpu_time_seconds * 10_000_000)
    limits.BasicLimitInformation.PerJobUserTimeLimit = int(command.cpu_time_seconds * 10_000_000)
    limits.ProcessMemoryLimit = command.memory_limit_bytes
    limits.JobMemoryLimit = command.memory_limit_bytes
    if not _kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        _close_handle(job)
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Windows CPU/memory limits could not be applied.")

    cpu = _CpuRateControlInformation(
        _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
        command.cpu_rate_percent * 100,
    )
    if not _kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
        ctypes.byref(cpu),
        ctypes.sizeof(cpu),
    ):
        _close_handle(job)
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Windows CPU rate cap could not be applied.")
    return job


def _startup_info(
    sid: ctypes.c_void_p,
    stdin_handle: int,
    stdout_handle: int,
    stderr_handle: int,
) -> tuple[_StartupInfoEx, ctypes.Array[ctypes.c_char], tuple[object, ...]]:
    size = SIZE_T()
    _kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    if size.value == 0:
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "AppContainer process attributes are unavailable.")
    buffer = ctypes.create_string_buffer(size.value)
    pointer = ctypes.cast(buffer, ctypes.c_void_p)
    if not _kernel32.InitializeProcThreadAttributeList(pointer, 2, 0, ctypes.byref(size)):
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "AppContainer process attributes could not be initialized.")
    security = _SecurityCapabilities(sid, None, 0, 0)
    if not _kernel32.UpdateProcThreadAttribute(
        pointer,
        0,
        _PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        ctypes.byref(security),
        ctypes.sizeof(security),
        None,
        None,
    ):
        _kernel32.DeleteProcThreadAttributeList(pointer)
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Lowbox security capabilities could not be assigned.")
    handle_list = (HANDLE * 3)(HANDLE(stdin_handle), HANDLE(stdout_handle), HANDLE(stderr_handle))
    if not _kernel32.UpdateProcThreadAttribute(
        pointer,
        0,
        _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        ctypes.cast(handle_list, ctypes.c_void_p),
        ctypes.sizeof(handle_list),
        None,
        None,
    ):
        _kernel32.DeleteProcThreadAttributeList(pointer)
        raise _Win32Failure(SandboxErrorCode.SETUP_FAILED, "Inherited handles could not be restricted.")
    startup = _StartupInfoEx()
    startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
    startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
    startup.StartupInfo.hStdInput = HANDLE(stdin_handle)
    startup.StartupInfo.hStdOutput = HANDLE(stdout_handle)
    startup.StartupInfo.hStdError = HANDLE(stderr_handle)
    startup.lpAttributeList = pointer
    return startup, buffer, (security, handle_list)


def _job_usage(job: HANDLE) -> tuple[float, int]:
    accounting = _BasicAccountingInformation()
    if not _kernel32.QueryInformationJobObject(
        job,
        _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
        ctypes.byref(accounting),
        ctypes.sizeof(accounting),
        None,
    ):
        accounting.TotalUserTime = 0
        accounting.TotalKernelTime = 0
    extended = _ExtendedLimitInformation()
    if not _kernel32.QueryInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(extended),
        ctypes.sizeof(extended),
        None,
    ):
        extended.PeakJobMemoryUsed = 0
    return (
        max(0, accounting.TotalUserTime + accounting.TotalKernelTime) / 10_000_000,
        int(extended.PeakJobMemoryUsed),
    )


def _close_handle(handle: HANDLE | int) -> bool:
    value = handle if isinstance(handle, HANDLE) else HANDLE(handle)
    if not value or value == HANDLE(-1).value:
        return True
    return bool(_kernel32.CloseHandle(value))


_kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    BOOL,
    DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(_StartupInfo),
    ctypes.POINTER(_ProcessInformation),
]
_kernel32.CreateProcessW.restype = BOOL
_kernel32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
_kernel32.AssignProcessToJobObject.restype = BOOL
_kernel32.TerminateProcess.argtypes = [HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = BOOL
_kernel32.TerminateJobObject.argtypes = [HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = BOOL
_kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
_kernel32.WaitForSingleObject.restype = DWORD
_kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
_kernel32.GetExitCodeProcess.restype = BOOL
_kernel32.ResumeThread.argtypes = [HANDLE]
_kernel32.ResumeThread.restype = DWORD
_kernel32.CloseHandle.argtypes = [HANDLE]
_kernel32.CloseHandle.restype = BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p
_kernel32.SetHandleInformation.argtypes = [HANDLE, DWORD, DWORD]
_kernel32.SetHandleInformation.restype = BOOL
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = HANDLE
_kernel32.SetInformationJobObject.argtypes = [HANDLE, DWORD, ctypes.c_void_p, DWORD]
_kernel32.SetInformationJobObject.restype = BOOL
_kernel32.QueryInformationJobObject.argtypes = [HANDLE, DWORD, ctypes.c_void_p, DWORD, ctypes.c_void_p]
_kernel32.QueryInformationJobObject.restype = BOOL
_kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, DWORD, DWORD, ctypes.POINTER(SIZE_T)]
_kernel32.InitializeProcThreadAttributeList.restype = BOOL
_kernel32.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p,
    DWORD,
    SIZE_T,
    ctypes.c_void_p,
    SIZE_T,
    ctypes.c_void_p,
    ctypes.POINTER(SIZE_T),
]
_kernel32.UpdateProcThreadAttribute.restype = BOOL
_kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
_kernel32.DeleteProcThreadAttributeList.restype = None
_advapi32.FreeSid.argtypes = [ctypes.c_void_p]
_advapi32.FreeSid.restype = ctypes.c_void_p
