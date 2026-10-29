"""Regressions from diagnostic bundle 6; no live logins or publications."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

from src.publisher import windows_proxy as proxy
from src.publisher.multi_deploy import PublishTarget
from src.publisher.platforms import ChannelAccount
from src.publisher.preflight import AccountPreflight
from src.publisher.wechat_channels import WeChatChannelsUploader
from src.publisher.youtube import PlatformRejected
from src.publisher.identity import extract_platform_identity


@pytest.mark.parametrize('platform', ['youtube', 'wechat_channels'])
@pytest.mark.parametrize('url', ['about:blank', 'chrome-error://chromewebdata/'])
def test_network_document_does_not_invalidate_login(tmp_path, platform, url):
    account = ChannelAccount('fixture', platform, '本人')
    check = AccountPreflight(PublishTarget(platform, account, str(tmp_path)))
    with pytest.raises(RuntimeError, match='未据此判定登录失效') as error:
        check._check_duration_notice(MagicMock(url=url))
    assert not isinstance(error.value, PlatformRejected)


def test_wechat_root_keeps_identity_and_is_not_a_login_page(tmp_path):
    from tests.test_wechat_channels import HTML, owner
    account = owner()
    check = AccountPreflight(PublishTarget(account.platform, account, str(tmp_path)))
    uploader = WeChatChannelsUploader(account, tmp_path, tmp_path/'reports')
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route('**/*', lambda route: route.fulfill(body=HTML, content_type='text/html'))
            page.goto('https://channels.weixin.qq.com/platform')
            check._check_duration_notice(page)
            uploader.check_rejection(page)
            assert extract_platform_identity(page, account.platform)['nickname'] == '本人视频号'
            page.goto('https://channels.weixin.qq.com/login.html')
            with pytest.raises(PlatformRejected) as error:
                uploader.check_rejection(page)
            assert error.value.status == 'AUTH_REQUIRED'
        finally:
            browser.close()


def test_pac_selected_per_url_and_direct_overrides_stale_static_proxy(monkeypatch):
    monkeypatch.setattr(proxy.os, 'name', 'nt')
    monkeypatch.setattr(proxy, 'getproxies_environment', lambda: {'no': 'localhost'})
    monkeypatch.setattr(proxy, '_pac_url', lambda: 'http://127.0.0.1/local.pac')
    calls = []
    def resolve(url, pac):
        calls.append(url)
        return '127.0.0.1:7890' if 'googleapis' in url else ''
    monkeypatch.setattr(proxy, '_resolve_pac', resolve)
    assert proxy.proxy_handlers('https://www.googleapis.com/drive/v3/files')[0].proxies == {'https': 'http://127.0.0.1:7890'}
    assert proxy.proxy_handlers('https://direct.invalid/')[0].proxies == {}
    assert len(calls) == 2
    monkeypatch.setattr(proxy, 'getproxies_environment', lambda: {'https': 'http://explicit:9000'})
    assert proxy.proxy_handlers('https://www.googleapis.com/') == []
    assert len(calls) == 2


def test_pac_failure_preserves_authorization_without_sending_request(monkeypatch):
    from src.publisher import google_drive as drive
    def failed(url):
        raise proxy.ProxyConfigurationError('Windows PAC 解析失败')
    opener = MagicMock()
    monkeypatch.setattr(drive, 'proxy_handlers', failed)
    monkeypatch.setattr(drive, 'build_opener', opener)
    with pytest.raises(drive.DriveError, match='PAC.*未据此判定授权失效') as error:
        drive.request(drive.TOKEN_URL, data={'refresh_token': 'private'})
    assert not isinstance(error.value, drive.DriveAuthRequired)
    opener.assert_not_called()


@pytest.mark.parametrize('value,expected', [
    ('127.0.0.1:7890', 'http://127.0.0.1:7890'),
    ('http=one:80;https=two:81', 'http://two:81'),
    ('http://127.0.0.1:7890;other:80', 'http://127.0.0.1:7890'),
])
def test_winhttp_proxy_lists_use_http_connect_for_https(value, expected):
    assert proxy._proxy_address(value, 'https') == expected


def test_pac_proxy_is_used_for_oauth_api_and_download_without_redirects(monkeypatch):
    from src.publisher import google_drive as drive
    from urllib.request import ProxyHandler
    handler = ProxyHandler({'https': 'http://127.0.0.1:7890'})
    monkeypatch.setattr(drive, 'proxy_handlers', lambda url: [handler])
    opener = MagicMock()
    response = opener.open.return_value
    response.read.return_value = b'{"access_token":"local"}'
    factory = MagicMock(return_value=opener)
    monkeypatch.setattr(drive, 'build_opener', factory)
    assert drive.request(drive.TOKEN_URL, data={'grant_type': 'refresh_token'})['access_token'] == 'local'
    assert factory.call_args.args[0] is drive.NoRedirect
    assert isinstance(factory.call_args.args[1], drive.RequiredGoogleProxy)
    assert factory.call_args.args[1].proxies == handler.proxies
    assert drive.request(drive.API_URL+'files/test?alt=media', token='fixture', stream=True) is response
    assert opener.open.call_args.args[0].headers['Authorization'] == 'Bearer fixture'


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows PAC engine')
def test_real_windows_pac_engine_resolves_proxy_and_direct():
    class PAC(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'function FindProxyForURL(url, host) { if (host == "www.googleapis.com") return "PROXY 127.0.0.1:17890"; return "DIRECT"; }'
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ns-proxy-autoconfig')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), PAC)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pac = f'http://127.0.0.1:{server.server_port}/proxy.pac'
        assert proxy._resolve_pac('https://www.googleapis.com/drive/v3/files', pac) == '127.0.0.1:17890'
        assert proxy._resolve_pac('https://direct.invalid/', pac) == ''
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
