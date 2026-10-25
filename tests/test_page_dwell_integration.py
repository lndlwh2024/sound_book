"""Factory compatibility and isolated browser telemetry; no real accounts or network."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

from src.publisher import media_browser, page_dwell, youtube_browser
from src.publisher.douyin.browser import DouyinBrowserManager
from src.publisher.event_log import PublishEventLog


@pytest.mark.parametrize('factory', ['media', 'douyin', 'youtube-chrome', 'youtube-msedge'])
def test_timing_callback_never_reaches_playwright_or_changes_profile_and_options(tmp_path, monkeypatch, factory):
    runtime=MagicMock()
    context=runtime.chromium.launch_persistent_context.return_value
    context.pages=[]
    monkeypatch.setattr(media_browser,'media_browser_executable',lambda:None)
    monkeypatch.setattr(media_browser,'prepare_persistent_context',lambda value,*args:value)
    attach=MagicMock()
    monkeypatch.setattr(page_dwell,'attach_page_dwell',attach)
    profile=tmp_path/'账号专用目录'
    if factory.startswith('youtube-'):
        channel=factory.split('-',1)[1]
        browser=youtube_browser.SystemBrowser(channel,channel,tmp_path/(channel+'.exe'))
        monkeypatch.setattr(youtube_browser,'selected_channel',lambda value:channel)
        monkeypatch.setattr(youtube_browser,'installed_browsers',lambda:[browser])
        monkeypatch.setattr(youtube_browser,'profile_busy',lambda value:False)
        expected_profile=youtube_browser.browser_profile(profile,channel)
    else:
        expected_profile=profile.resolve() if factory=='douyin' else profile

    def launch(callback=None):
        options=dict(headless=False,viewport={'width':1280,'height':720},timeout=12345)
        if callback is not None:options['page_timing_log']=callback
        if factory=='douyin':
            return DouyinBrowserManager.create_persistent_context(runtime,profile,**options)
        options.update(args=['--fixture-only-argument'],ignore_default_args=['--fixture-default'])
        if factory.startswith('youtube-'):
            return youtube_browser.launch_youtube_context(runtime,profile,**options)
        return media_browser.launch_media_context(runtime,profile,**options)

    assert launch() is context
    original=runtime.chromium.launch_persistent_context.call_args
    attach.assert_not_called()
    callback=MagicMock()
    assert launch(callback) is context
    timed=runtime.chromium.launch_persistent_context.call_args
    assert timed==original
    assert timed.args==(str(expected_profile),)
    assert 'page_timing_log' not in timed.kwargs
    assert timed.kwargs['timeout']==12345
    assert timed.kwargs['no_viewport'] is True and 'viewport' not in timed.kwargs
    assert '--restore-last-session' in timed.kwargs['args']
    if factory.startswith('youtube-'):
        assert timed.kwargs['channel']==channel
        assert timed.kwargs['executable_path']==str(browser.executable)
    elif factory=='douyin':
        assert '--disable-blink-features=AutomationControlled' in timed.kwargs['args']
    else:
        assert timed.kwargs['channel']=='chromium'
    attach.assert_called_once_with(context,callback)


@pytest.fixture
def isolated_runtime(monkeypatch):
    executable=Path(__file__).resolve().parents[1]/'artifacts/publisher-media-browser/chrome-win64/chrome.exe'
    if not executable.is_file():pytest.skip('Isolated release browser is unavailable')
    monkeypatch.setenv('SHUSHENG_MEDIA_BROWSER',str(executable))
    with sync_playwright() as runtime:
        yield runtime


def _local_routes(context):
    def fulfill(route):
        path=route.request.url.split('?',1)[0]
        if '/first' in path:
            html='<h1>first fixture</h1><iframe src="https://frame.invalid/inside?token=IFRAME_SECRET"></iframe>'
        elif '/inside' in path:
            html='<p>isolated iframe fixture</p>'
        elif '/second' in path:
            html='<h1>second fixture</h1>'
        else:
            html='<h1>extra fixture</h1>'
        route.fulfill(body='<meta charset="utf-8">'+html,content_type='text/html')
    context.route('**/*',fulfill)


def test_real_navigation_iframe_filter_and_close_flush_keep_jsonl_attribution_and_redaction(tmp_path,isolated_runtime):
    events=PublishEventLog(tmp_path/'logs-state')
    identity=dict(platform='wechat_channels',account_id='fixture-account-id',account_name='fixture account',
                  file_name='fixture-video.mp4',task_id='fixture-task-id')
    raw=[]
    def log(message):
        raw.append(message)
        events.write(message,**identity)
    context=media_browser.launch_media_context(isolated_runtime,tmp_path/'profile',headless=True,page_timing_log=log)
    try:
        _local_routes(context)
        page=context.pages[0]
        page.goto('https://USER_SECRET:PASS_SECRET@fixture.invalid/first?access_token=QUERY_SECRET#FRAGMENT_SECRET')
        assert page.frame_locator('iframe').locator('p').inner_text()=='isolated iframe fixture'
        page_dwell.set_page_stage(page,'本地检查')
        assert page.locator('h1').inner_text()=='first fixture'
        page_dwell.finish_page_stage(page,'本地检查')
        page.goto('https://fixture.invalid/second?token=SECOND_QUERY#SECOND_FRAGMENT')
        assert page.locator('h1').inner_text()=='second fixture'
        extra=context.new_page()
        extra.goto('https://fixture.invalid/extra?token=EXTRA_QUERY')
        assert extra.locator('h1').inner_text()=='extra fixture'
        page.close()
        # Leave the extra tab open so context shutdown must flush its dwell record.
    finally:
        media_browser.close_media_context(context)

    records=[json.loads(line) for line in events.path.read_text(encoding='utf-8').splitlines()]
    assert records
    assert all(all(record[key]==value for key,value in identity.items()) for record in records)
    dwell=[record['message'] for record in records if record['message'].startswith('[页面停留]')]
    for address in ('https://fixture.invalid/first','https://fixture.invalid/second','https://fixture.invalid/extra'):
        matching=[message for message in dwell if '地址='+address+'；' in message]
        assert len(matching)==1,(address,matching)
        assert '停留=' in matching[0] and '结束原因=' in matching[0]
    assert any(record['message'].startswith('[页面阶段耗时]') and '阶段=本地检查' in record['message'] for record in records)
    text='\n'.join(raw)+'\n'+events.path.read_text(encoding='utf-8')
    for secret in ('USER_SECRET','PASS_SECRET','QUERY_SECRET','FRAGMENT_SECRET','SECOND_QUERY',
                   'SECOND_FRAGMENT','EXTRA_QUERY','IFRAME_SECRET','frame.invalid'):
        assert secret not in text


def test_real_browser_keeps_navigation_cookies_and_storage_when_timing_logger_raises(tmp_path,isolated_runtime):
    calls=[]
    def broken_log(message):
        calls.append(message)
        raise OSError('fixture telemetry sink unavailable')
    context=media_browser.launch_media_context(isolated_runtime,tmp_path/'profile',headless=True,page_timing_log=broken_log)
    try:
        _local_routes(context)
        context.add_cookies([dict(name='fixture_session',value='preserved',url='https://fixture.invalid/')])
        page=context.pages[0]
        page.goto('https://fixture.invalid/first')
        page.evaluate("sessionStorage.setItem('fixture-storage','preserved')")
        page_dwell.set_page_stage(page,'日志故障对照')
        page_dwell.finish_page_stage(page,'日志故障对照')
        page.goto('https://fixture.invalid/second')
        assert page.locator('h1').inner_text()=='second fixture'
        assert page.evaluate("sessionStorage.getItem('fixture-storage')")=='preserved'
        assert next(cookie['value'] for cookie in context.cookies() if cookie['name']=='fixture_session')=='preserved'
    finally:
        media_browser.close_media_context(context)
    assert calls


def test_login_window_timing_reaches_window_signal_and_platform_event_file(tmp_path):
    from src.app.channel_panel import ChannelProbeWorker
    from src.publisher.platforms import ChannelAccountStore
    store=ChannelAccountStore(tmp_path/'multi_platform')
    account=store.add('wechat_channels','fixture login')
    worker=ChannelProbeWorker(account,tmp_path/'profile',store.root/'dom_reports')
    visible,events=[],[]
    worker.status.connect(visible.append)
    worker.event_logged.connect(events.append)
    message='[页面停留] 页面ID=C1-T1-P1；停留=3.0秒；结束原因=页面关闭'
    worker.record_page_timing(message)
    saved=PublishEventLog(store.root).tail()
    assert visible==[message] and len(events)==len(saved)==1
    assert events[0]==saved[0]
    assert saved[0]['platform']=='wechat_channels'
    assert saved[0]['account_id']==account.account_id
    assert saved[0]['category']=='browser_timing'
    assert not saved[0].get('task_id')  # Login windows do not borrow a publishing task.
