"""Resolve the current user's explicit PAC with Windows' own PAC engine.

urllib handles environment and static Windows proxies, but not AutoConfigURL.
No system settings are changed and no credentials are sent to the PAC engine.
"""
import ctypes
import os
import re
from ctypes import wintypes
from urllib.parse import urlsplit
from urllib.request import getproxies_environment, ProxyHandler


class ProxyConfigurationError(OSError):
    pass


def _pac_url():
    # Query the active connection flags as well as its URL. A registry URL may
    # remain after PAC is disabled in favor of a static VPN proxy.
    class Config(ctypes.Structure):
        _fields_ = [('detect', wintypes.BOOL), ('url', ctypes.c_void_p),
                    ('proxy', ctypes.c_void_p), ('bypass', ctypes.c_void_p)]
    api = ctypes.WinDLL('winhttp', use_last_error=True)
    api.WinHttpGetIEProxyConfigForCurrentUser.argtypes = [ctypes.POINTER(Config)]
    api.WinHttpGetIEProxyConfigForCurrentUser.restype = wintypes.BOOL
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel.GlobalFree.restype = ctypes.c_void_p
    config = Config()
    try:
        if not api.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(config)):
            raise ProxyConfigurationError('无法读取Windows当前用户代理配置')
        return ctypes.wstring_at(config.url).strip() if config.url else ''
    finally:
        for pointer in (config.url, config.proxy, config.bypass):
            if pointer:
                kernel.GlobalFree(pointer)


def _resolve_pac(url, pac_url):
    class Options(ctypes.Structure):
        _fields_ = [('flags', wintypes.DWORD), ('detect', wintypes.DWORD),
                    ('url', wintypes.LPCWSTR), ('reserved', ctypes.c_void_p),
                    ('reserved_flags', wintypes.DWORD), ('auto_login', wintypes.BOOL)]

    class ProxyInfo(ctypes.Structure):
        _fields_ = [('access', wintypes.DWORD), ('proxy', ctypes.c_void_p),
                    ('bypass', ctypes.c_void_p)]

    api = ctypes.WinDLL('winhttp', use_last_error=True)
    api.WinHttpOpen.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR,
                               wintypes.LPCWSTR, wintypes.DWORD]
    api.WinHttpOpen.restype = wintypes.HANDLE
    api.WinHttpSetTimeouts.argtypes = [wintypes.HANDLE] + [ctypes.c_int]*4
    api.WinHttpSetTimeouts.restype = wintypes.BOOL
    api.WinHttpGetProxyForUrl.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
                                        ctypes.POINTER(Options), ctypes.POINTER(ProxyInfo)]
    api.WinHttpGetProxyForUrl.restype = wintypes.BOOL
    api.WinHttpCloseHandle.argtypes = [wintypes.HANDLE]
    api.WinHttpCloseHandle.restype = wintypes.BOOL
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel.GlobalFree.restype = ctypes.c_void_p
    session = api.WinHttpOpen('ShuShengPublisher', 1, None, None, 0)
    if not session:
        raise ProxyConfigurationError('Windows PAC 初始化失败')
    info = ProxyInfo()
    try:
        if not api.WinHttpSetTimeouts(session, 5000, 5000, 5000, 5000):
            raise ProxyConfigurationError('Windows PAC 超时配置失败')
        # URL_CONFIG; no WPAD discovery or automatic Windows credential login.
        options = Options(2, 0, pac_url, None, 0, False)
        if not api.WinHttpGetProxyForUrl(session, url, ctypes.byref(options), ctypes.byref(info)):
            code = ctypes.get_last_error()
            raise ProxyConfigurationError(f'Windows PAC 解析失败（错误码{code}）；请检查VPN的PAC服务或切换全局模式')
        if info.access == 1:  # WINHTTP_ACCESS_TYPE_NO_PROXY, PAC returned DIRECT
            return ''
        if info.access != 3 or not info.proxy:
            raise ProxyConfigurationError('Windows PAC 未返回可用代理')
        return ctypes.wstring_at(info.proxy)
    finally:
        for pointer in (info.proxy, info.bypass):
            if pointer:
                kernel.GlobalFree(pointer)
        api.WinHttpCloseHandle(session)


def _proxy_address(value, scheme):
    # WinHTTP returns a proxy list, not the PAC source's "PROXY ...; DIRECT".
    entries = re.split(r'[;\s]+', value.strip())
    for entry in entries:
        if not entry:
            continue
        if '=' in entry:
            protocol, entry = entry.split('=', 1)
            if protocol.lower() != scheme:
                continue
        address = entry if '://' in entry else 'http://'+entry
        parsed = urlsplit(address)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ProxyConfigurationError('PAC 返回不支持的代理类型，请使用HTTP代理或全局模式')
        return address
    raise ProxyConfigurationError('PAC 未返回适用于本次请求的HTTP代理')


def proxy_handlers(url):
    """Explicit environment settings win; static mode stays with urllib."""
    if os.name != 'nt' or getproxies_environment().get(urlsplit(url).scheme):
        return []
    try:
        pac = _pac_url()
        if not pac:
            return []
        scheme = urlsplit(url).scheme
        proxy = _resolve_pac(url, pac)
        # Explicit {} is important: DIRECT must not fall back to a stale registry proxy.
        return [ProxyHandler({scheme: _proxy_address(proxy, scheme)} if proxy else {})]
    except ProxyConfigurationError:
        raise
    except OSError:
        raise ProxyConfigurationError('无法读取Windows PAC配置，请检查VPN代理设置') from None
