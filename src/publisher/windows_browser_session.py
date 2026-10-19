"""Inspect/close only the Chromium instance owning one app-specific profile.

Uses Chromium's named message window and lockfile, not the lifetime of its
launcher process. No cookies, passwords, default profiles or unrelated windows
are accessed. WM_CLOSE is a normal window-close request, never a process kill.
"""
import ctypes
import os
from ctypes import wintypes
from pathlib import Path


def _user32():
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    api.FindWindowExW.restype = wintypes.HWND
    api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    return api


def profile_owner(profile):
    if os.name != 'nt':
        return None
    api = _user32()
    window = api.FindWindowExW(wintypes.HWND(-3), None, 'Chrome_MessageWindow', str(Path(profile).resolve()))
    if not window:
        return None
    pid = wintypes.DWORD()
    if not api.GetWindowThreadProcessId(window, ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pid.value


def profile_busy(profile):
    if os.name != 'nt':
        return False
    if profile_owner(profile):
        return True
    # A message window may disappear just before its profile lock is released.
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(Path(profile).resolve()/'lockfile'), 0x80000000, 0x1|0x4,
                            None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error in (2,3):
            return False
        if error == 32:
            return True
        raise ctypes.WinError(error)
    api.CloseHandle(handle)
    return False


def _process_image(pid):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE,wintypes.DWORD,
        wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    api.QueryFullProcessImageNameW.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x1000,False,pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not api.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return name.value
    finally:
        api.CloseHandle(handle)


def close_profile_windows(profile, executable):
    """Request normal closure of this profile's own visible browser windows."""
    if os.name != 'nt':
        return 0
    pid = profile_owner(profile)
    if not pid:
        return 0
    if os.path.normcase(os.path.realpath(_process_image(pid))) != os.path.normcase(os.path.realpath(executable)):
        raise RuntimeError('专用浏览器进程与配置不符，未关闭任何窗口。')
    api = _user32()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,ctypes.c_ssize_t)
    api.EnumWindows.argtypes = [callback_type,ctypes.c_ssize_t]
    api.EnumWindows.restype = wintypes.BOOL
    api.GetClassNameW.argtypes = [wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    api.GetClassNameW.restype = ctypes.c_int
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.IsWindowVisible.restype = wintypes.BOOL
    api.PostMessageW.argtypes = [wintypes.HWND,wintypes.UINT,ctypes.c_size_t,ctypes.c_ssize_t]
    api.PostMessageW.restype = wintypes.BOOL
    closed, errors = [], []
    @callback_type
    def visit(window, _):
        try:
            owner = wintypes.DWORD()
            api.GetWindowThreadProcessId(window,ctypes.byref(owner))
            if owner.value != pid or not api.IsWindowVisible(window):
                return True
            name = ctypes.create_unicode_buffer(256)
            api.GetClassNameW(window,name,len(name))
            if name.value == 'Chrome_WidgetWin_1' and profile_owner(profile) == pid:
                if not api.PostMessageW(window,0x10,0,0):
                    raise ctypes.WinError(ctypes.get_last_error())
                closed.append(window)
            return True
        except Exception as error:
            errors.append(error)
            return False
    if not api.EnumWindows(visit,0) and not errors:
        raise ctypes.WinError(ctypes.get_last_error())
    if errors:
        raise errors[0]
    return len(closed)
