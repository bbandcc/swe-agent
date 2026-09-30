"""Fail closed before any opt-in AppContainer integration side effect."""

from __future__ import annotations

import os


def require_normal_user_token() -> None:
    if os.name != "nt" or os.environ.get("S5B_RUN_APPCONTAINER_TESTS") != "1":
        raise AssertionError("Real AppContainer tests require Windows and explicit opt-in.")

    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TokenMandatoryLabel(ctypes.Structure):
        _fields_ = [("Label", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise AssertionError("Current process token cannot be queried.")
    try:
        def information(kind: int, value: ctypes.Structure) -> None:
            returned = wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, kind, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)):
                raise AssertionError("Current process token preflight failed.")

        elevated = wintypes.DWORD()
        appcontainer = wintypes.DWORD()
        information(20, elevated)  # TokenElevation
        information(29, appcontainer)  # TokenIsAppContainer
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise AssertionError("Current process integrity could not be queried.")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(token, 25, buffer, required.value, ctypes.byref(required)):
            raise AssertionError("Current process integrity could not be queried.")
        label = ctypes.cast(buffer, ctypes.POINTER(TokenMandatoryLabel)).contents
        count = advapi32.GetSidSubAuthorityCount(label.Label.Sid).contents.value
        integrity = advapi32.GetSidSubAuthority(label.Label.Sid, count - 1).contents.value
        if elevated.value or integrity > 0x2000 or appcontainer.value:
            raise AssertionError("Real AppContainer tests require a normal, non-elevated, non-AppContainer token.")

        expected = os.environ.get("S5B_EXPECTED_TEST_USER")
        if expected:
            username = ctypes.create_unicode_buffer(256)
            size = wintypes.DWORD(len(username))
            advapi32.GetUserNameW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            advapi32.GetUserNameW.restype = wintypes.BOOL
            if not advapi32.GetUserNameW(username, ctypes.byref(size)) or username.value.casefold() != expected.casefold():
                raise AssertionError("Real AppContainer test user does not match the optional expected-user guard.")
    finally:
        kernel32.CloseHandle(token)
