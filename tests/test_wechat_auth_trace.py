"""Local intercepted pages only: prove lifecycle evidence and credential redaction."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

from src.publisher import auth_snapshots, media_browser
from src.publisher.wechat_auth_trace import (AUTH_KEYS, ORIGIN, WechatAuthTrace,
    attach_wechat_auth_trace, checkpoint_wechat_auth, material_difference)


@pytest.fixture
def config(tmp_path,monkeypatch):
    monkeypatch.setattr(auth_snapshots,'_protect',lambda value,decrypt=False:value)
    target=SimpleNamespace(platform='wechat_channels',profile=str(tmp_path/'profile'),
        account=SimpleNamespace(account_id='fixture-account'))
    logs=[]
    return {'root':tmp_path/'state','target':target,'log':logs.append,'task_id':'trace-task',
            'file_name':'fixture.mp4','source':'真实发布测试入口'},logs


def raw(local=None,session=None):
    return {'local':{'available':True,'count':len(local or {}),'values':{k:(local or {}).get(k) for k in AUTH_KEYS}},
            'session':{'available':True,'count':len(session or {}),'values':{k:(session or {}).get(k) for k in AUTH_KEYS}}}


def context_fixture():
    context=MagicMock()
    context.cookies.return_value=[dict(name='sessionid',value='COOKIE_SECRET',domain='channels.weixin.qq.com')]
    page=MagicMock();page.url=ORIGIN+'/platform/post/create?credential=URL_SECRET'
    page.is_closed.return_value=False
    context.pages=[page]
    return context,page


def texts(root,logs):
    return '\n'.join(logs)+''.join(p.read_text(encoding='utf-8') for p in Path(root).rglob('*.json'))


def test_materials_distinguish_missing_empty_unavailable_and_keep_good_success(config):
    settings,logs=config
    context,page=context_fixture()
    trace=WechatAuthTrace(context,settings)
    baseline=trace.checkpoint('发布成功回执后',page,True,raw({'finder_raw':'TOKEN_SECRET','finder_username':'NAME_SECRET','finder_uin':''}))
    path=trace.publish_latest
    saved=path.read_bytes()
    assert baseline['materials']['local']['known']['finder_raw']['state']=='present'
    assert baseline['materials']['local']['known']['finder_uin']['state']=='empty'
    assert baseline['materials']['local']['known']['finder_login_token']['state']=='absent'
    failed=trace.checkpoint('失败页面认证材料',page,False,raw({'finder_username':'NAME_SECRET'}))
    assert any(c['item']=='local.finder_raw' and c['now']=='absent' for c in failed['difference']['changes'])
    assert failed['difference']['baseline_file_name']=='fixture.mp4'
    assert failed['difference']['baseline_source']=='真实发布测试入口'
    assert path.read_bytes()==saved
    trace.checkpoint('核验通过',page,True,{'local':{'available':False},'session':{'available':False}})
    assert path.read_bytes()==saved
    assert 'local' in trace.last_evidence['difference']['unavailable']
    rendered=texts(settings['root'],logs)
    assert all(secret not in rendered for secret in ('COOKIE_SECRET','TOKEN_SECRET','NAME_SECRET','URL_SECRET'))
    assert list((settings['root']/'.diagnostics/trace-task').glob('auth-diff-wechat_channels-*.json'))


def test_auth_response_is_numeric_bounded_and_deduplicated(config):
    settings,logs=config
    context,page=context_fixture()
    page.evaluate.return_value=raw({'finder_raw':'TOKEN_SECRET'})
    trace=WechatAuthTrace(context,settings)
    response=MagicMock(status=200)
    response.headers={'content-type':'application/json','content-length':'92'}
    response.body.return_value=json.dumps({'errCode':0,'data':{'baseResp':{'errcode':300334}},
                                         'message':'RESPONSE_SECRET'}).encode()
    request=MagicMock(url=ORIGIN+'/cgi-bin/mmfinderassistant-bin/auth/auth_data?token=QUERY_SECRET')
    request.response.return_value=response
    request.frame.page=page
    trace._response(request)
    trace._response(request)
    observed=trace.last_evidence['observation']
    assert observed['business_code']==300334 and observed['other_device_login_code']
    assert len(logs)==1
    response.body.reset_mock()
    response.headers['content-length']='10000000'
    trace._response(request)
    response.body.assert_not_called()
    assert trace.last_evidence['observation']['body_omitted']=='body_size_limit'
    request.url='https://other.invalid/cgi-bin/mmfinderassistant-bin/auth/auth_data'
    request.response.reset_mock();trace._response(request);request.response.assert_not_called()
    assert all(s not in texts(settings['root'],logs) for s in ('RESPONSE_SECRET','QUERY_SECRET','TOKEN_SECRET','COOKIE_SECRET'))


def test_observation_errors_do_not_echo_secrets_or_flood(config):
    settings,logs=config
    context,page=context_fixture()
    trace=WechatAuthTrace(context,settings)
    request=MagicMock(url=ORIGIN+'/cgi-bin/mmfinderassistant-bin/auth/auth_data')
    request.response.side_effect=RuntimeError('ERROR_SECRET')
    for _ in range(20):trace._response(request)
    assert len(logs)==1 and 'ERROR_SECRET' not in logs[0]
    page.evaluate.return_value=raw()
    for _ in range(100):trace.checkpoint('页面脚本执行前',page)
    assert len([m for m in logs if 'wechat_auth_trace_limit' in m])==1
    assert trace.stage_counts['页面脚本执行前']==64


def test_real_browser_captures_initial_script_storage_mutation_and_auth_response(tmp_path,config,monkeypatch):
    settings,logs=config
    executable=Path(__file__).resolve().parents[1]/'artifacts/publisher-media-browser/chrome-win64/chrome.exe'
    assert executable.exists()
    monkeypatch.setenv('SHUSHENG_MEDIA_BROWSER',str(executable))
    with sync_playwright() as playwright:
        context=media_browser.launch_media_context(playwright,settings['target'].profile,headless=True,
            wechat_auth_trace=settings)
        try:
            def route(request):
                if '/cgi-bin/' in request.request.url:
                    request.fulfill(status=200,content_type='application/json',
                        body='{"err_code":300334,"message":"REAL_RESPONSE_SECRET"}')
                else:
                    request.fulfill(status=200,content_type='text/html',body='''<html><script>
                        window.beforeSite=localStorage.getItem('finder_raw');
                        localStorage.setItem('finder_raw','REAL_TOKEN_SECRET');
                        localStorage.setItem('finder_username','REAL_NAME_SECRET');
                        sessionStorage.setItem('finder_uin','REAL_UIN_SECRET');
                    </script>fixture</html>''')
            context.route('**/*',route)
            context.add_cookies([dict(name='sessionid',value='REAL_COOKIE_SECRET',url=ORIGIN,expires=2000000000)])
            page=context.pages[0]
            page.goto(ORIGIN+'/platform/post/create?ticket=REAL_QUERY_SECRET')
            page.wait_for_timeout(400)
            success=checkpoint_wechat_auth(context,'发布成功回执后',page,True)
            assert success['materials']['local']['known']['finder_raw']['state']=='present'
            page.evaluate("localStorage.removeItem('finder_raw')")
            page.wait_for_timeout(400)
            page.evaluate("fetch('/cgi-bin/mmfinderassistant-bin/auth/auth_data?key=API_QUERY_SECRET')")
            page.wait_for_timeout(400)
            trace=context._shusheng_wechat_auth_trace
            events=[json.loads(p.read_text(encoding='utf-8')) for p in (settings['root']/'.diagnostics/trace-task').glob('*.json')]
            assert any(e['stage']=='重开后首次导航前' for e in events)
            document=next(e for e in events if e['stage']=='页面脚本执行前')
            assert document['materials']['local']['known']['finder_raw']['state']=='absent'
            assert any(e['stage']=='认证存储变更' and e['observation']['action']=='removed' for e in events)
            assert any(e['stage']=='认证接口响应' and e['observation']['business_code']==300334 for e in events)
        finally:media_browser.close_media_context(context)
    output=texts(settings['root'],logs)
    assert '浏览器关闭前' in output
    assert all(secret not in output for secret in ('REAL_TOKEN_SECRET','REAL_COOKIE_SECRET','REAL_NAME_SECRET',
        'REAL_UIN_SECRET','REAL_QUERY_SECRET','API_QUERY_SECRET','REAL_RESPONSE_SECRET'))


def test_trace_config_does_not_reach_chromium_and_non_wechat_is_ignored(config):
    settings,_=config
    context,page=context_fixture()
    chromium=MagicMock();chromium.launch_persistent_context.return_value=context
    media_browser.open_persistent_context(chromium,settings['target'].profile,wechat_auth_trace=settings)
    assert 'wechat_auth_trace' not in chromium.launch_persistent_context.call_args.kwargs
    settings['target'].platform='youtube'
    assert attach_wechat_auth_trace(context,settings) is None


def test_initialization_binding_never_calls_synchronous_browser_api(config):
    settings,logs=config
    context,page=context_fixture()
    trace=WechatAuthTrace(context,settings)
    context.cookies.side_effect=AssertionError('binding must not reenter dispatcher')
    page.evaluate.side_effect=AssertionError('binding already has storage payload')
    frame=SimpleNamespace(url=ORIGIN+'/platform/post/create')
    trace._document_start({'frame':frame,'page':page},raw({'finder_raw':'BINDING_SECRET'}))
    assert trace.last_evidence['stage']=='页面脚本执行前'
    assert trace.last_evidence['materials']['local']['known']['finder_raw']['state']=='present'
    assert '非此回调实时读取' in trace.last_evidence['observation']['cookie_sample']
    context.cookies.assert_not_called()
    page.evaluate.assert_not_called()
    assert 'BINDING_SECRET' not in texts(settings['root'],logs)


def test_long_real_publication_batch_keeps_the_latest_success_baseline(config):
    settings,logs=config
    context,page=context_fixture()
    trace=WechatAuthTrace(context,settings)
    for number in range(66):
        trace.update(dict(settings,file_name=f'video-{number}.mp4'))
        result=trace.checkpoint('发布成功回执后',page,True,raw({'finder_raw':'TOKEN_'+str(number)}))
        assert result['baseline_update']=='updated'
    baseline=json.loads(trace.publish_latest.read_text(encoding='utf-8'))
    assert baseline['file_name']=='video-65.mp4'
    assert baseline['materials']['local']['known']['finder_raw']['state']=='present'
    assert not any('wechat_auth_trace_limit' in item for item in logs)
