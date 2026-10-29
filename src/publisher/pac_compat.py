"""Adapt PAC return transports for WinHTTP without changing the user's policy.

WinHTTP treats a SOCKS-first PAC result as DIRECT even if HTTP follows it.
Evaluate the original policy, then remove only transports urllib cannot use.
The rewritten script is served briefly on loopback; no system PAC is modified.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

MAX_SCRIPT = 1024 * 1024
UNSUPPORTED = 'pac-unsupported.shusheng.invalid:9'

ADAPTER = r'''
;(function(original) {
  FindProxyForURL = function(url, host) {
    var result = String(original.call(this, url, host));
    if (result.length > 32768) return "PROXY pac-unsupported.shusheng.invalid:9";
    var entries = result.split(";"), usable = [];
    for (var i = 0; i < entries.length && i < 64; i++) {
      var entry = entries[i].replace(/^\s+|\s+$/g, "");
      if (/^DIRECT$/i.test(entry)) usable.push("DIRECT");
      else if (/^PROXY\s+[^\s@]+$/i.test(entry)) usable.push(entry.replace(/^PROXY\s+/i, "PROXY "));
    }
    return usable.length ? usable.join("; ") : "PROXY pac-unsupported.shusheng.invalid:9";
  };
})(FindProxyForURL);
'''


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_script(pac_url):
    from .windows_proxy import ProxyConfigurationError
    parsed = urlsplit(pac_url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ProxyConfigurationError('PAC地址必须是无内嵌密码的HTTP或HTTPS地址')
    try:
        request = Request(pac_url, headers={'Accept': 'application/x-ns-proxy-autoconfig, */*'})
        # Fetch the configured PAC directly, as the native engine does. Never
        # attach OAuth headers, platform cookies or automatic proxy credentials.
        with build_opener(_NoRedirect, ProxyHandler({})).open(request, timeout=5) as response:
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_SCRIPT):
                raise ProxyConfigurationError('PAC脚本超过读取上限')
            body = response.read(MAX_SCRIPT + 1)
        if len(body) > MAX_SCRIPT:
            raise ProxyConfigurationError('PAC脚本超过读取上限')
        if body.startswith((b'\xff\xfe', b'\xfe\xff')):
            return body.decode('utf-16')
        try:
            return body.decode('utf-8-sig')
        except UnicodeDecodeError:
            return body.decode('mbcs')
    except ProxyConfigurationError:
        raise
    except HTTPError as error:
        code = error.code
        error.close()
        raise ProxyConfigurationError(f'PAC脚本读取失败（HTTP {code}）；未改用直连') from None
    except Exception as error:
        # Do not expose a configured PAC URL/query or arbitrary exception text.
        kind = '超时' if isinstance(error, TimeoutError) else '失败'
        raise ProxyConfigurationError('PAC脚本读取' + kind + '；请检查PAC服务，未改用直连') from None


def compatible_script(script):
    if len(script.encode('utf-8')) > MAX_SCRIPT:
        from .windows_proxy import ProxyConfigurationError
        raise ProxyConfigurationError('PAC脚本超过读取上限')
    return (script + '\n' + ADAPTER).encode('utf-8')


@contextmanager
def _serve_pac(body):
    """Keep a private, short-lived HTTP PAC endpoint alive for one resolution."""
    path = '/' + uuid.uuid4().hex + '.pac'
    class Handler(BaseHTTPRequestHandler):
        timeout = 2
        def do_GET(self):
            self.respond(False)
        def do_HEAD(self):
            self.respond(True)
        def respond(self, head):
            if self.path != path:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ns-proxy-autoconfig; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(body)
                except ConnectionError:
                    pass
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05),
                              name='publisher-pac-resolution', daemon=True)
    try:
        thread.start()
        yield f'http://127.0.0.1:{server.server_port}{path}'
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        if thread.ident is not None:
            thread.join(2)


@contextmanager
def compatible_pac(pac_url):
    with _serve_pac(compatible_script(read_script(pac_url))) as url:
        yield url


def offline_self_check():
    """Exercise the real Windows engine with loopback PAC only, no accounts."""
    from .windows_proxy import _resolve_pac
    script = b'''function FindProxyForURL(url,host) {
      if (host == 'oauth2.googleapis.com' || host == 'www.googleapis.com' || host == 'studio.youtube.com')
        return 'SOCKS5 127.0.0.1:51837; SOCKS 127.0.0.1:51837; PROXY 127.0.0.1:58591';
      return 'DIRECT';
    }'''
    urls = ['https://oauth2.googleapis.com/token', 'https://www.googleapis.com/drive/v3/files',
            'https://studio.youtube.com/', 'https://creator.douyin.com/',
            'https://cp.kuaishou.com/', 'https://channels.weixin.qq.com/']
    with _serve_pac(script) as source:
        actual = [_resolve_pac(url, source) for url in urls]
    return actual == ['127.0.0.1:58591'] * 3 + [''] * 3
