"""PAC transport compatibility: reproduce the Trojan SOCKS-first failure locally."""
import concurrent.futures
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
from urllib.request import ProxyHandler, build_opener

import pytest

from src.publisher import pac_compat as compat, windows_proxy as proxy


@contextmanager
def serve(body, status=200, headers=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(dict(self.headers))
            self.send_response(status)
            self.send_header('Content-Type', 'application/x-ns-proxy-autoconfig')
            self.send_header('Content-Length', str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:
                pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/proxy.pac', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def constant(value):
    return ('function FindProxyForURL(url,host){return "' + value + '";}').encode()


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows PAC engine')
@pytest.mark.parametrize('result,expected', [
    ('SOCKS5 127.0.0.1:51837; SOCKS 127.0.0.1:51837; PROXY 127.0.0.1:58591', '127.0.0.1:58591'),
    ('SOCKS5 127.0.0.1:51837; DIRECT', ''),
    ('DIRECT; PROXY 127.0.0.1:58591', ''),
    ('PROXY 127.0.0.1:58591; DIRECT', '127.0.0.1:58591'),
    ('socks5 127.0.0.1:51837; proxy 127.0.0.1:58591', '127.0.0.1:58591'),
    ('DIRECT', ''),
])
def test_native_resolver_preserves_supported_proxy_and_explicit_direct(result, expected):
    with serve(constant(result)) as (pac, requests):
        assert proxy._resolve_pac('https://www.googleapis.com/drive/v3/files', pac) == expected
    assert requests
    assert all(not {'authorization', 'cookie', 'proxy-authorization'} & {k.lower() for k in req} for req in requests)


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows PAC engine')
@pytest.mark.parametrize('value', ['SOCKS5 127.0.0.1:51837', 'HTTPS 127.0.0.1:58591', ''])
def test_unsupported_only_result_is_an_error_never_implicit_direct(value):
    with serve(constant(value)) as (pac, _):
        with pytest.raises(proxy.ProxyConfigurationError, match='不把仅SOCKS代理误判为直连'):
            proxy._resolve_pac('https://oauth2.googleapis.com/token', pac)


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows PAC engine')
def test_parallel_google_youtube_and_domestic_policies_do_not_mix():
    script = b'''function FindProxyForURL(url,host) {
      if (dnsDomainIs(host,'.googleapis.com') || dnsDomainIs(host,'.youtube.com'))
        return 'SOCKS5 127.0.0.1:51837; SOCKS 127.0.0.1:51837; PROXY 127.0.0.1:58591';
      return 'DIRECT';
    }'''
    urls = ['https://oauth2.googleapis.com/token', 'https://www.googleapis.com/drive/v3/files',
            'https://studio.youtube.com/', 'https://creator.douyin.com/',
            'https://cp.kuaishou.com/', 'https://channels.weixin.qq.com/']
    with serve(script) as (pac, _):
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda url: proxy._resolve_pac(url, pac), urls))
    assert results == ['127.0.0.1:58591'] * 3 + [''] * 3


def test_resolution_listener_is_closed_when_native_resolution_raises(monkeypatch):
    seen = []
    def fail(url, pac):
        seen.append(pac)
        with build_opener(ProxyHandler({})).open(pac, timeout=2) as response:
            assert b'FindProxyForURL' in response.read()
        raise proxy.ProxyConfigurationError('fixture')
    monkeypatch.setattr(proxy, '_resolve_pac_winhttp', fail)
    with serve(constant('DIRECT')) as (pac, _):
        with pytest.raises(proxy.ProxyConfigurationError, match='fixture'):
            proxy._resolve_pac('https://www.googleapis.com/', pac)
    with pytest.raises(OSError):
        build_opener(ProxyHandler({})).open(seen[0], timeout=.2)
    assert not any(t.name == 'publisher-pac-resolution' for t in threading.enumerate())


def test_fetch_failure_size_redirect_and_embedded_credentials_never_silently_fallback():
    with serve(b'x' * (compat.MAX_SCRIPT + 1)) as (pac, _):
        with pytest.raises(proxy.ProxyConfigurationError, match='上限'):
            compat.read_script(pac)
    with serve(b'', 302, {'Location': 'http://127.0.0.1:1/private?SECRET'}) as (pac, _):
        with pytest.raises(proxy.ProxyConfigurationError, match='HTTP 302') as error:
            compat.read_script(pac + '?SECRET')
        assert 'SECRET' not in str(error.value)
    with pytest.raises(proxy.ProxyConfigurationError, match='无内嵌密码') as error:
        compat.read_script('http://user:SECRET@127.0.0.1:1/pac?SECRET')
    assert 'SECRET' not in str(error.value)


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows PAC engine')
def test_invalid_javascript_is_not_direct_and_leaves_no_listener():
    with serve(b'not a javascript PAC <html>') as (pac, _):
        with pytest.raises(proxy.ProxyConfigurationError, match='解析失败'):
            proxy._resolve_pac('https://www.googleapis.com/', pac)
    assert not any(t.name == 'publisher-pac-resolution' for t in threading.enumerate())


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows PAC engine')
def test_offline_frozen_self_check_uses_only_private_pac_endpoints():
    assert compat.offline_self_check()
    assert not any(t.name == 'publisher-pac-resolution' for t in threading.enumerate())
