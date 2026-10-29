"""Read-only diagnostic evidence must be bounded, secret-free and explainable."""
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler

import pytest

from tools import diagnose_publisher_network as check


@pytest.fixture
def local_http():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append({'path': self.path, 'headers': dict(self.headers)})
            self.send_response(int(self.path.rsplit('/', 1)[-1]) if self.path.startswith('/status/') else 200)
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_no_account_headers_redirects_or_error_body_are_collected(local_http):
    base, received = local_http
    for status in (200, 302, 401, 403, 404, 405, 407):
        result = check._http(base + f'/status/{status}', '')
        assert result['http_status'] == status
        assert result['transport_reached'] is (status != 407)
    assert len(received) == 7
    for request in received:
        assert not {'authorization', 'cookie', 'proxy-authorization'} & {k.lower() for k in request['headers']}


def test_forced_proxy_really_overrides_no_proxy_without_touching_settings(local_http, monkeypatch):
    base, received = local_http
    monkeypatch.setattr('urllib.request.proxy_bypass', lambda host: True)
    result = check._http('http://never-resolve-this.invalid/probe', base)
    assert result['transport_reached']
    assert received[0]['path'] == 'http://never-resolve-this.invalid/probe'


def test_pac_fetch_uses_no_proxy_and_stores_hash_instead_of_script(local_http):
    base, received = local_http
    result = check._fetch_pac(base + '/pac?private-pac-key=SECRET')
    assert result['state'] == 'received'
    assert result['bytes'] == 0 and len(result['sha256']) == 64
    assert 'SECRET' not in json.dumps(result)
    assert len(received) == 1


def test_sensitive_proxy_is_never_used_for_a_network_request(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('credentialed request must not start')
    monkeypatch.setattr(check, 'build_opener', forbidden)
    assert check._http('https://www.googleapis.com/', 'http://alice:SECRET@127.0.0.1:7890')['not_tested']
    assert check._fetch_pac('http://alice:SECRET@127.0.0.1/proxy.pac')['state'] == 'not_tested'


def test_report_excludes_raw_proxy_values_paths_and_queries(tmp_path):
    secret = 'http://alice:SECRET@127.0.0.1:7890/PRIVATE?token=SECRET#PRIVATE'
    report = {'config': {'_pac': secret, '_candidates': [secret], 'active_pac': check.endpoint(secret)},
              'routes': {'x': {'_raw_proxy': secret, 'selected_proxy': check.endpoint(secret)}}}
    folder = check.write_report(report, tmp_path)
    for path in folder.iterdir():
        text = path.read_text(encoding='utf-8-sig')
        for value in ('SECRET', 'PRIVATE', 'alice', '_raw_proxy', '_candidates'):
            assert value not in text
    assert json.loads((folder / 'network-report.json').read_text(encoding='utf-8'))['config']['active_pac']['credentials_present']
    with zipfile.ZipFile(folder.with_suffix('.zip')) as archive:
        assert set(archive.namelist()) == {'network-report.json', '诊断结果.txt'}
        assert all(b'SECRET' not in archive.read(name) for name in archive.namelist())


def test_route_accounts_for_actual_publisher_bypass_and_environment_precedence(monkeypatch):
    monkeypatch.setattr(check.windows_proxy, 'proxy_handlers', lambda url: [ProxyHandler({'https': 'http://127.0.0.1:7890'})])
    monkeypatch.setattr(check, 'getproxies_environment', lambda: {})
    monkeypatch.setattr(check, 'proxy_bypass', lambda host: True)
    result = check._route('https://www.googleapis.com/')
    assert result['source'] == 'PAC_PROXY' and result['bypass_applied']
    assert result['effective_route'] == 'DIRECT' and result['_raw_proxy'] == ''
    monkeypatch.setattr(check.windows_proxy, 'proxy_handlers', lambda url: [])
    monkeypatch.setattr(check, 'getproxies_environment', lambda: {'https': 'http://127.0.0.1:9000'})
    monkeypatch.setattr(check, 'getproxies', lambda: {'https': 'http://127.0.0.1:9000'})
    monkeypatch.setattr(check, 'proxy_bypass', lambda host: False)
    result = check._route('https://www.googleapis.com/')
    assert result['source'] == 'ENVIRONMENT' and result['selected_proxy']['port'] == 9000


def test_comparisons_only_use_configured_local_http_listeners():
    assert check.candidates(['http://127.0.0.1:7890', 'http://127.0.0.1:7890',
                             'http://remote.invalid:7890', 'socks5://127.0.0.1:1080',
                             'http://alice:SECRET@127.0.0.1:9000']) == ['http://127.0.0.1:7890']


def test_interpretation_does_not_claim_authentication_or_download_succeeded():
    report = {'routes': {check.TARGETS[0][0]: {'effective_route': 'DIRECT'}},
              'checks': {'CURRENT:' + check.TARGETS[0][0]: {'transport_reached': False},
                         'LOCAL_PROXY_1:' + check.TARGETS[0][0]: {'transport_reached': True}},
              'local_HTTP_comparison_proxies': [check.endpoint('http://127.0.0.1:7890')]}
    findings = '\n'.join(check.conclusions(report))
    assert '直连未收到HTTP响应' in findings and '优先核对' in findings
    assert '不能单凭本报告解释微信业务码300333' in findings


def test_worker_deadline_terminates_only_owned_child():
    result = check.bounded('selftest_wait', seconds=.5)
    assert result['error_category'] == 'worker_deadline'
    assert not check.multiprocessing.active_children()


def test_worker_really_runs_and_pac_error_retains_only_numeric_code():
    result = check.bounded('selftest_identity')
    assert result['tool_version'] == check.VERSION and result['pid'] != check.os.getpid()
    error = check.windows_proxy.ProxyConfigurationError('Windows PAC 解析失败（错误码12166）；SECRET')
    safe = check.safe_error(error)
    assert safe['pac_error_code'] == 12166 and 'SECRET' not in json.dumps(safe)


def test_complete_collection_is_a_finite_google_only_plan_without_credentials(monkeypatch):
    calls = []
    def fixture(action, values=None, seconds=12):
        calls.append((action, values))
        if action == 'config':
            return {'_pac': 'http://127.0.0.1:8070/proxy.pac',
                    '_candidates': ['http://127.0.0.1:7890', 'http://user:SECRET@127.0.0.1:9000']}
        if action == 'route':
            return {'source': 'PAC_DIRECT', 'effective_route': 'DIRECT', '_raw_proxy': ''}
        if action == 'pac':
            return {'state': 'received', 'sha256': 'fixture'}
        if action == 'http':
            assert values['url'] in dict(check.TARGETS).values()
            assert 'SECRET' not in values.get('proxy', '')
            return {'transport_reached': bool(values.get('proxy'))}
        return {'reachable': True}
    monkeypatch.setattr(check, 'bounded', fixture)
    report = check.collect()
    assert len(calls) == 13
    assert 'SECRET' not in json.dumps(report) and '_raw_proxy' not in json.dumps(report)
    assert len(report['local_HTTP_comparison_proxies']) == 1
    assert report['checks']['CURRENT:' + check.TARGETS[0][0]]['transport_reached'] is False
    assert report['checks']['LOCAL_PROXY_1:' + check.TARGETS[0][0]]['transport_reached'] is True
