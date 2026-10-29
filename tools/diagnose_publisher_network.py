"""One-shot Windows PAC/Google transport evidence. No account credentials or settings writes."""
import argparse
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
import platform
import re
import socket
import ssl
import sys
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (HTTPRedirectHandler, ProxyHandler, Request, build_opener,
                            getproxies, getproxies_environment, proxy_bypass)

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.publisher import windows_proxy

VERSION = '2026.10.29.2'
TARGETS = (
    ('Google令牌服务', 'https://oauth2.googleapis.com/token'),
    ('Google云盘目录与媒体API', 'https://www.googleapis.com/drive/v3/files?pageSize=1'),
)
CHINA = timezone(timedelta(hours=8))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(value):
    """Never export URL userinfo, query, fragment, or an arbitrary path."""
    if not value:
        return None
    try:
        parsed = urlsplit(value if '://' in value else 'http://' + value)
        if not parsed.hostname or parsed.scheme not in ('http', 'https', 'socks', 'socks5', 'socks5h'):
            return {'state': 'unrecognized'}
        return {'scheme': parsed.scheme, 'host': parsed.hostname, 'port': parsed.port,
                'credentials_present': parsed.username is not None or parsed.password is not None}
    except (ValueError, TypeError):
        return {'state': 'unrecognized'}


def safe_error(error):
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        category = 'tls_certificate'
    elif isinstance(reason, ssl.SSLError):
        category = 'tls'
    elif isinstance(reason, socket.gaierror):
        category = 'dns'
    elif isinstance(reason, TimeoutError):
        category = 'timeout'
    elif isinstance(reason, ConnectionRefusedError):
        category = 'connection_refused'
    else:
        category = type(reason).__name__
    result = {'error_category': category, 'error_code': getattr(reason, 'winerror', None)
              or getattr(reason, 'errno', None)}
    if isinstance(reason, windows_proxy.ProxyConfigurationError):
        # The shared helper constructs this fixed message; retain only its
        # numeric Windows PAC code, never arbitrary transport exception text.
        match = re.search(r'错误码(\d{1,10})', str(reason))
        if match:
            result['pac_error_code'] = int(match.group(1))
    return result


def _windows_config():
    config = {'os': platform.system(), 'os_release': platform.release(),
              'os_version': platform.version(), 'python': platform.python_version(),
              'tool_version': VERSION, 'windows_supported': os.name == 'nt'}
    config['pac_transport_adapter'] = 'SOCKS-first compatibility; explicit HTTP/DIRECT policy retained'
    env = getproxies_environment()
    config['environment'] = {key: endpoint(env.get(key)) for key in ('http', 'https', 'all')}
    config['environment']['no_proxy_present'] = bool(env.get('no'))
    config['_candidates'] = [env.get('https', ''), env.get('http', '')]
    config['_pac'] = ''
    if os.name != 'nt':
        return config
    import winreg
    from urllib.request import getproxies_registry
    raw = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            for name in ('ProxyEnable', 'AutoConfigURL', 'ProxyServer', 'ProxyOverride'):
                try:
                    raw[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    pass
        config['registry'] = {'proxy_enabled': bool(raw.get('ProxyEnable')),
                              'pac_address': endpoint(raw.get('AutoConfigURL')),
                              'bypass_list_present': bool(raw.get('ProxyOverride'))}
    except OSError as error:
        config['registry'] = safe_error(error)
    # urllib ignores disabled static entries. A disabled local HTTP listener is
    # still useful for the explicit comparison, so parse its retained address.
    static = getproxies_registry()
    config['static'] = {key: endpoint(static.get(key)) for key in ('http', 'https')}
    config['_candidates'].extend([static.get('https', ''), static.get('http', '')])
    if raw.get('ProxyServer'):
        try:
            config['_candidates'].append(windows_proxy._proxy_address(raw['ProxyServer'], 'https'))
        except (ValueError, OSError):
            pass
    try:
        config['_pac'] = windows_proxy._pac_url()
        config['active_pac'] = endpoint(config['_pac'])
    except OSError as error:
        config['active_pac_error'] = safe_error(error)
    return config


def _route(url):
    """Use the same PAC selector as publisher, including urllib's bypass rules."""
    scheme, host = urlsplit(url).scheme, urlsplit(url).hostname
    env = getproxies_environment()
    handlers = windows_proxy.proxy_handlers(url)
    if handlers:
        proxies = handlers[0].proxies
        selected = proxies.get(scheme, '')
        source = 'PAC_PROXY' if selected else 'PAC_DIRECT'
    else:
        selected = getproxies().get(scheme, '')
        source = 'ENVIRONMENT' if env.get(scheme) else 'SYSTEM_STATIC_OR_DIRECT'
    bypass = bool(selected and proxy_bypass(host))
    return {'source': source, 'selected_proxy': endpoint(selected), 'bypass_applied': bypass,
            'effective_route': 'DIRECT' if not selected or bypass else 'PROXY',
            '_raw_proxy': '' if bypass else selected}


def _fetch_pac(url):
    address = endpoint(url)
    if not address or address.get('credentials_present') or address.get('state'):
        return {'state': 'not_tested', 'reason': 'no_supported_credential_free_pac_address'}
    request = Request(url, headers={'User-Agent': 'ShuShengNetworkDiagnostic/' + VERSION})
    try:
        with build_opener(NoRedirect, ProxyHandler({})).open(request, timeout=6) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                return {'state': 'oversized', 'http_status': response.status}
            return {'state': 'received', 'http_status': response.status, 'bytes': len(body),
                    'sha256': hashlib.sha256(body).hexdigest(),
                    'contains_FindProxyForURL': b'FindProxyForURL' in body,
                    'contains_SOCKS_tokens': b'SOCKS' in body.upper(),
                    'contains_PROXY_tokens': b'PROXY' in body.upper()}
    except HTTPError as error:
        error.close()
        return {'state': 'http_error', 'http_status': error.code}
    except Exception as error:
        return {'state': 'failed', **safe_error(error)}


def _http(url, proxy):
    if proxy:
        address = endpoint(proxy)
        if not address or address.get('state') or address.get('credentials_present'):
            return {'transport_reached': False, 'not_tested': 'unsupported_or_credentialed_proxy'}
        if address.get('scheme') not in ('http', 'https'):
            return {'transport_reached': False, 'not_tested': 'requires_HTTP_proxy_port_not_SOCKS_port'}
    started = time.monotonic()
    # Explicit handlers prevent environment proxies or NO_PROXY from silently
    # changing a requested DIRECT/forced comparison. No credentials/cookies.
    handler = ProxyHandler({'https': proxy, 'http': proxy} if proxy else {})
    if proxy:
        # ProxyHandler normally applies global proxy_bypass even to explicit
        # mappings. Supply a handler which makes this comparison truly forced.
        class ForcedProxy(ProxyHandler):
            def proxy_open(self, req, selected, kind):
                from urllib.request import _parse_proxy
                scheme, user, password, hostport = _parse_proxy(selected)
                if user is not None or password is not None:
                    raise ValueError('credentialed proxy not tested')
                req.set_proxy(hostport, scheme or kind)
                if kind == 'https' or (scheme or kind) == req.type:
                    return None
                return self.parent.open(req, timeout=req.timeout)
        handler = ForcedProxy({'https': proxy, 'http': proxy})
    request = Request(url, headers={'User-Agent': 'ShuShengNetworkDiagnostic/' + VERSION,
                                    'Accept': 'application/json'})
    try:
        with build_opener(NoRedirect, handler).open(request, timeout=7) as response:
            result = {'transport_reached': True, 'http_status': response.status}
    except HTTPError as error:
        # 401/403/404/405 are expected possibilities for unauthenticated probes;
        # an HTTP response demonstrates transport, not access to user files.
        result = {'transport_reached': error.code != 407, 'http_status': error.code,
                  'proxy_auth_required': error.code == 407}
        error.close()
    except Exception as error:
        result = {'transport_reached': False, **safe_error(error)}
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return result


def _perform(action, values):
    if action == 'config':
        return _windows_config()
    if action == 'route':
        return _route(values['url'])
    if action == 'pac':
        return _fetch_pac(values['url'])
    if action == 'http':
        return _http(values['url'], values.get('proxy', ''))
    if action == 'dns':
        rows = socket.getaddrinfo(values['host'], 443, type=socket.SOCK_STREAM)
        return {'addresses': sorted(set(row[4][0] for row in rows))}
    if action == 'tcp':
        started = time.monotonic()
        with socket.create_connection((values['host'], values['port']), timeout=4):
            return {'reachable': True, 'elapsed_seconds': round(time.monotonic() - started, 3)}
    if action == 'selftest_wait':
        time.sleep(5)
        return {}
    if action == 'selftest_identity':
        return {'tool_version': VERSION, 'pid': os.getpid()}
    raise ValueError('Unknown action')


def _child(connection, action, values):
    try:
        connection.send(_perform(action, values))
    except BaseException as error:
        connection.send({'failed': True, **safe_error(error)})
    finally:
        connection.close()


def bounded(action, values=None, seconds=12):
    """Native PAC/DNS can outlive socket timeouts; own subprocesses are bounded."""
    context = multiprocessing.get_context('spawn')
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(writer, action, values or {}))
    try:
        process.start()
        writer.close()
        if not reader.poll(seconds):
            return {'failed': True, 'error_category': 'worker_deadline', 'limit_seconds': seconds}
        try:
            return reader.recv()
        except EOFError:
            return {'failed': True, 'error_category': 'worker_exited'}
    finally:
        if process.pid is not None:
            process.join(.2)
            if process.is_alive():
                process.terminate()
                process.join(3)
            if process.is_alive():
                process.kill()
                process.join(3)
            process.close()
        reader.close()
        writer.close()


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith('_')}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def candidates(values):
    result = []
    for value in values:
        address = endpoint(value)
        if not address or address.get('state') or address.get('credentials_present'):
            continue
        if address['host'] not in ('127.0.0.1', 'localhost', '::1'):
            continue
        if address['scheme'] not in ('http', 'https') or not address['port']:
            continue
        if value not in result:
            result.append(value)
    return result[:3]


def collect():
    report = {'schema': 1, 'tool_version': VERSION,
              'started_at': datetime.now(CHINA).isoformat(),
              'limits': {'no_google_or_wechat_credentials': True, 'no_proxy_settings_changed': True,
                         'no_file_download_or_upload': True, 'no_wechat_requests': True,
                         'http_status_is_transport_evidence_only': True}}
    print('1/4 读取当前用户代理设置（不修改设置）...', flush=True)
    config = bounded('config')
    report['config'] = config
    print('2/4 检查PAC文件及Google实际分流...', flush=True)
    work = [('route:' + label, 'route', {'url': url}) for label, url in TARGETS]
    if config.get('_pac'):
        work.append(('pac_file', 'pac', {'url': config['_pac']}))
    routes = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(bounded, action, values): label for label, action, values in work}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            value = future.result()
            if key.startswith('route:'):
                routes[key[6:]] = value
            else:
                report[key] = value
            print('  已完成：' + key, flush=True)
    report['routes'] = routes
    raw_candidates = config.get('_candidates', []) + [v.get('_raw_proxy', '') for v in routes.values()]
    local = candidates(raw_candidates)
    report['local_HTTP_comparison_proxies'] = [endpoint(v) for v in local]
    print('3/4 检查DNS、代理端口及HTTPS（无账号请求，约1—2分钟）...', flush=True)
    work = []
    for label, url in TARGETS:
        work.append(('DNS:' + label, 'dns', {'host': urlsplit(url).hostname}))
        route = routes.get(label, {})
        if not route.get('failed') and 'effective_route' in route:
            work.append(('CURRENT:' + label, 'http', {'url': url, 'proxy': route.get('_raw_proxy', '')}))
        work.append(('DIRECT:' + label, 'http', {'url': url}))
        for i, proxy in enumerate(local):
            work.append((f'LOCAL_PROXY_{i + 1}:' + label, 'http', {'url': url, 'proxy': proxy}))
    for i, value in enumerate(local):
        address = endpoint(value)
        work.append((f'LOCAL_PROXY_{i + 1}:TCP', 'tcp', address))
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(bounded, action, values): label for label, action, values in work}
        for future in concurrent.futures.as_completed(futures):
            label = futures[future]
            results[label] = future.result()
            print('  已完成：' + label, flush=True)
    report['checks'] = results
    report['finished_at'] = datetime.now(CHINA).isoformat()
    return public(report)


def conclusions(report):
    findings = []
    checks = report.get('checks', {})
    for label, _ in TARGETS:
        route = report.get('routes', {}).get(label, {})
        normal = checks.get('CURRENT:' + label, {})
        forced = any(v.get('transport_reached') for k, v in checks.items()
                     if k.startswith('LOCAL_PROXY_') and k.endswith(':' + label))
        if route.get('failed'):
            findings.append(label + '：当前代理选择失败，见routes中的错误类别。')
        elif normal.get('transport_reached'):
            findings.append(label + '：按当前代理选择已收到HTTP响应；只证明基础连通，不代表已验证账号或真实下载。')
        elif route.get('effective_route') == 'DIRECT' and forced:
            findings.append(label + '：当前走直连未收到HTTP响应，指定本机HTTP代理后收到响应；优先核对PAC分流/绕过设置。')
        elif forced:
            findings.append(label + '：当前路径失败，但至少一个本机HTTP代理可达；核对所选代理和绕过规则。')
        else:
            findings.append(label + '：未找到可用对照路径，需结合PAC文件、DNS和端口检查继续定位。')
    if not report.get('local_HTTP_comparison_proxies'):
        findings.append('系统配置中未找到可用于对照的本机HTTP代理端口；未扫描其他端口。')
    findings.append('HTTP401/403/404/405可能是无账号请求的正常响应；不是用户账号失效结论。')
    findings.append('本工具不访问视频号，不能单凭本报告解释微信业务码300333。')
    return findings


def write_report(report, parent):
    stamp = datetime.now(CHINA).strftime('%Y%m%d-%H%M%S-%f')
    folder = Path(parent) / ('ShuSheng-Network-' + stamp)
    folder.mkdir(parents=True)
    report = public(report)
    report['findings'] = conclusions(report) if not report.get('self_test') else ['仅工具自检，没有检查当前网络。']
    (folder / 'network-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    text = ['书声网络诊断 ' + VERSION, '', *report['findings'], '',
            '报告不含Google/微信账号凭据、Cookie或代理密码。',
            '请将此文件夹内的两个报告文件发回；不需要发送VPN订阅或节点密码。', '',
            json.dumps(report, ensure_ascii=False, indent=2)]
    (folder / '诊断结果.txt').write_text('\n'.join(text), encoding='utf-8-sig')
    with zipfile.ZipFile(folder.with_suffix('.zip'), 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ('network-report.json', '诊断结果.txt'):
            archive.write(folder / name, name)
    return folder


def self_test():
    # Standalone smoke validates the actual frozen subprocess path and cleanup.
    value = 'http://alice:very-private@127.0.0.1:7890/private?token=very-private'
    assert 'very-private' not in json.dumps(endpoint(value))
    assert 'very-private' not in json.dumps(public({'_raw_proxy': value, 'safe': endpoint(value)}))
    assert _http(TARGETS[0][1], value).get('not_tested')
    assert not candidates([value, 'socks5://127.0.0.1:1080'])
    child = bounded('selftest_identity')
    assert child.get('tool_version') == VERSION and child.get('pid') != os.getpid()
    assert bounded('selftest_wait', seconds=.5).get('error_category') == 'worker_deadline'
    return {'self_test': True, 'tool_version': VERSION, 'passed': True,
            'children_remaining': len(multiprocessing.active_children()),
            'network_requests_sent': 0}


def main():
    parser = argparse.ArgumentParser(description='只读PAC/Google网络诊断；不读取平台账号凭据。')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--no-wait', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    print('书声网络诊断 ' + VERSION, flush=True)
    print('保持当前PAC模式即可；本工具不切换VPN、不读取Google/微信凭据、不上传视频。', flush=True)
    parent = args.output_dir or (Path(sys.executable).parent if getattr(sys, 'frozen', False)
                                 else Path.cwd() / 'output')
    code = 0
    try:
        report = self_test() if args.self_test else collect()
        report['findings'] = conclusions(report) if not report.get('self_test') else ['仅工具自检，没有检查当前网络。']
        print('4/4 保存报告...', flush=True)
        folder = write_report(report, parent)
        print('\n完成。请发送自动生成的报告ZIP：\n' + str(folder.with_suffix('.zip')), flush=True)
        for finding in report.get('findings', []):
            print(finding, flush=True)
    except Exception as error:
        print('诊断未完成：' + type(error).__name__ + '；请保留此窗口信息。', flush=True)
        code = 1
    if not args.no_wait and sys.stdin and sys.stdin.isatty():
        input('\n按回车退出。')
    return code


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
