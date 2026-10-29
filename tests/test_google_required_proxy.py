"""Google credentials must never be sent on a silent DIRECT fallback."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import MagicMock
from urllib.request import ProxyHandler, build_opener

import pytest

from src.publisher import google_drive as drive


@pytest.mark.parametrize('url,stream', [(drive.TOKEN_URL, False),
    (drive.API_URL+'files?q=SECRET', False), (drive.API_URL+'files/SECRET?alt=media', True)])
@pytest.mark.parametrize('pac_direct', [False, True])
def test_google_missing_proxy_or_explicit_pac_direct_stops_before_transport(monkeypatch,url,stream,pac_direct):
    opener = MagicMock()
    monkeypatch.setattr(drive,'build_opener',opener)
    monkeypatch.setattr(drive,'proxy_handlers',lambda url:[ProxyHandler({})] if pac_direct else [])
    # Explicit PAC DIRECT must not borrow a stale system proxy either.
    monkeypatch.setattr(drive,'getproxies',lambda:{'https':'http://127.0.0.1:9000'} if pac_direct else {})
    with pytest.raises(drive.DriveError,match='强制代理.*不会直连') as error:
        drive.request(url,token='SECRET',stream=stream)
    assert not isinstance(error.value,drive.DriveAuthRequired)
    assert 'SECRET' not in str(error.value)
    opener.assert_not_called()


@pytest.mark.parametrize('selected',['socks5://127.0.0.1:1080','http://127.0.0.1:0',
    'http://127.0.0.1:not-a-port','http://user:SECRET@127.0.0.1:7890/private?SECRET'])
def test_invalid_proxy_is_not_replaced_by_direct(selected):
    with pytest.raises(drive.ProxyConfigurationError,match='不会直连') as error:
        drive.required_google_proxy(drive.TOKEN_URL,[ProxyHandler({'https':selected})])
    assert 'SECRET' not in str(error.value)


def test_unreachable_proxy_is_attempted_once_and_never_falls_back(monkeypatch):
    opener=MagicMock();opener.open.side_effect=TimeoutError('SECRET')
    factory=MagicMock(return_value=opener)
    monkeypatch.setattr(drive,'proxy_handlers',lambda url:[ProxyHandler({'https':'http://127.0.0.1:58591'})])
    monkeypatch.setattr(drive,'proxy_bypass',lambda host:True)
    monkeypatch.setattr(drive,'build_opener',factory)
    with pytest.raises(drive.DriveError) as error:
        drive.request(drive.TOKEN_URL,token='SECRET')
    assert '强制HTTP代理 127.0.0.1:58591' in str(error.value)
    assert 'SECRET' not in str(error.value)
    assert factory.call_count==opener.open.call_count==1
    assert isinstance(factory.call_args.args[1],drive.RequiredGoogleProxy)


def test_actual_proxy_request_ignores_global_bypass_without_global_setting_changes(monkeypatch):
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.02),daemon=True);thread.start()
    try:
        monkeypatch.setattr('urllib.request.proxy_bypass',lambda host:True)
        handler=drive.RequiredGoogleProxy({'http':f'http://127.0.0.1:{server.server_port}'})
        with build_opener(handler).open('http://never-direct.invalid/probe',timeout=2) as response:
            assert response.status==200
        assert seen==['http://never-direct.invalid/probe']
    finally:
        server.shutdown();server.server_close();thread.join(2)


def test_google_guard_does_not_rewrite_domestic_platform_or_youtube_browser_policy(monkeypatch):
    direct=[ProxyHandler({})]
    monkeypatch.setattr(drive,'getproxies',lambda:(_ for _ in ()).throw(AssertionError('not Google API')))
    for host in ['creator.douyin.com','cp.kuaishou.com','channels.weixin.qq.com','studio.youtube.com']:
        assert drive.required_google_proxy('https://'+host+'/',direct) is direct


def test_frozen_policy_self_check_never_reads_settings_or_creates_transport(monkeypatch):
    forbidden=lambda *a,**k:(_ for _ in ()).throw(AssertionError('must stay offline'))
    monkeypatch.setattr(drive,'getproxies',forbidden)
    monkeypatch.setattr(drive,'build_opener',forbidden)
    assert drive.required_proxy_self_check()
