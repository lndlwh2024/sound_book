"""No live Google requests or real personal profiles are used in these tests."""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication

from src.publisher import youtube_browser as browsers
from src.publisher.platforms import ChannelAccountStore
from src.app import channel_panel as ui


@pytest.fixture
def installed(tmp_path, monkeypatch):
    chrome = browsers.SystemBrowser('chrome', 'Chrome', tmp_path/'chrome.exe')
    edge = browsers.SystemBrowser('msedge', 'Edge', tmp_path/'msedge.exe')
    monkeypatch.setattr(browsers, 'installed_browsers', lambda: [chrome, edge])
    return chrome, edge


def test_detects_vendor_paths_and_prefers_chrome(tmp_path, monkeypatch):
    for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        monkeypatch.setenv(key, str(tmp_path/key))
    for relative in ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe'):
        path = tmp_path/'LOCALAPPDATA'/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert [b.channel for b in browsers.installed_browsers()] == ['chrome','msedge']


def test_legacy_login_remains_on_its_original_bundled_profile(tmp_path):
    playwright = MagicMock()
    browsers.launch_youtube_context(playwright, tmp_path, headless=False)
    playwright.chromium.launch_persistent_context.assert_called_once_with(str(tmp_path), headless=False,
        no_viewport=True,args=['--start-maximized','--restore-last-session'],channel='chromium',timeout=30000)
    assert not (tmp_path/browsers.MARKER).exists()


@pytest.mark.parametrize('channel', ['chrome', 'msedge'])
def test_manual_login_and_automation_use_same_executable_and_dedicated_profile(tmp_path,monkeypatch,installed,channel):
    browser = next(b for b in installed if b.channel == channel)
    monkeypatch.setattr(browsers, 'installed_browsers', lambda:[browser])
    popen = MagicMock()
    monkeypatch.setattr(browsers.subprocess, 'Popen', popen)
    legacy = tmp_path/'Default'
    legacy.mkdir()
    (legacy/'marker').write_text('original')
    _, chosen = browsers.start_manual_login(tmp_path)
    args = popen.call_args.args[0]
    assert chosen == browser
    assert args[0] == str(browser.executable)
    assert '--user-data-dir='+str(browsers.browser_profile(tmp_path,channel).resolve()) in args
    assert args[-1] == 'https://studio.youtube.com/'
    assert not any(word in ' '.join(args).lower() for word in
                   ('remote-debugging','enable-automation','automationcontrolled','disable-blink','user-agent'))
    playwright = MagicMock()
    browsers.launch_youtube_context(playwright,tmp_path,headless=False)
    call = playwright.chromium.launch_persistent_context.call_args
    assert call.args[0] == str(browsers.browser_profile(tmp_path,channel))
    assert call.kwargs['channel'] == channel
    assert call.kwargs['executable_path'] == args[0]
    assert call.kwargs['no_viewport'] is True and 'viewport' not in call.kwargs
    assert call.kwargs['args']==['--start-maximized','--restore-last-session']
    assert (legacy/'marker').read_text() == 'original'


def test_missing_selected_browser_does_not_switch_or_downgrade(tmp_path,monkeypatch,installed):
    (tmp_path/browsers.MARKER).write_text(json.dumps({'schema':1,'channel':'chrome'}))
    monkeypatch.setattr(browsers,'installed_browsers',lambda:[installed[1]])
    playwright = MagicMock()
    with pytest.raises(RuntimeError,match='未回退'):
        browsers.launch_youtube_context(playwright,tmp_path)
    with pytest.raises(RuntimeError,match='Chrome'):
        browsers.start_manual_login(tmp_path)
    playwright.chromium.launch_persistent_context.assert_not_called()


@pytest.mark.parametrize('content', ['{','[]','{"schema":1,"channel":"../default"}'])
def test_invalid_configuration_does_not_open_another_account(tmp_path,content):
    (tmp_path/browsers.MARKER).write_text(content)
    with pytest.raises(RuntimeError, match='配置无效'):
        browsers.launch_youtube_context(MagicMock(),tmp_path)


def test_failed_manual_launch_rolls_back_selection(tmp_path,installed,monkeypatch):
    popen = MagicMock(side_effect=OSError('cannot start'))
    monkeypatch.setattr(browsers.subprocess,'Popen',popen)
    with pytest.raises(OSError):
        browsers.start_manual_login(tmp_path)
    assert not (tmp_path/browsers.MARKER).exists()
    original = '{"schema":1,"channel":"msedge"}'
    (tmp_path/browsers.MARKER).write_text(original)
    with pytest.raises(OSError):
        browsers.start_manual_login(tmp_path)
    assert (tmp_path/browsers.MARKER).read_text() == original


@pytest.mark.parametrize('url,rejected', [
    ('https://accounts.google.com/v3/signin/rejected?continue=secret',True),
    ('https://accounts.google.com/signin/rejected',True),
    ('https://accounts.google.com/v3/signin/identifier',False),
    ('https://accounts.google.com.evil.example/v3/signin/rejected',False),
    ('https://studio.youtube.com/channel/UCtest',False),
])
def test_rejected_login_is_distinct_from_a_normal_login_prompt(url,rejected):
    assert browsers.google_login_rejected(url) == rejected


def test_worker_closes_automation_before_manual_login_then_revalidates(tmp_path,monkeypatch):
    app = QApplication.instance() or QApplication([])
    store = ChannelAccountStore(tmp_path)
    account = store.add('youtube','test')
    worker = ui.ChannelProbeWorker(account,store.profile_path(account),tmp_path/'reports')
    actions = []
    def automated():
        actions.append('automated')
        if len(actions) == 1:
            worker.request_google_manual()
        else:
            worker.request_close()
    worker._run_automated = automated
    process = MagicMock()
    process.poll.side_effect = [None,0,0,0]
    def manual(profile):
        actions.append('manual')
        return process,browsers.SystemBrowser('chrome','Chrome',Path('chrome.exe'))
    monkeypatch.setattr(ui,'start_manual_login',manual)
    monkeypatch.setattr(ui.time,'sleep',lambda _:None)
    found = []
    worker.identity_found.connect(found.append)
    worker.run()
    assert actions == ['automated','manual','automated']
    assert not found  # Closing a login window is not proof of authorization.
    assert app


def test_cancel_closes_dialog_promptly_and_preserves_external_browser(tmp_path,monkeypatch):
    app = QApplication.instance() or QApplication([])
    store = ChannelAccountStore(tmp_path)
    worker = ui.ChannelProbeWorker(store.add('youtube','test'),tmp_path/'profile',tmp_path/'reports')
    worker.request_google_manual()
    process = MagicMock()
    def poll():
        worker.request_close()
        return None if process.poll.call_count == 1 else 0
    process.poll.side_effect = poll
    monkeypatch.setattr(ui,'start_manual_login',lambda _: (process,browsers.SystemBrowser('chrome','Chrome',Path('chrome.exe'))))
    monkeypatch.setattr(ui.time,'sleep',lambda _:None)
    worker._run_automated = MagicMock()
    worker.run()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    worker._run_automated.assert_not_called()
    assert process.poll.call_count == 1 and app


def test_preflight_rejection_gives_actionable_manual_login_instructions(tmp_path):
    from src.publisher.preflight import AccountPreflight
    from src.publisher.multi_deploy import PublishTarget
    from src.publisher.youtube import PlatformRejected
    store = ChannelAccountStore(tmp_path)
    account = store.add('youtube','test')
    page = MagicMock(url='https://accounts.google.com/v3/signin/rejected')
    check = AccountPreflight(PublishTarget('youtube',account,str(tmp_path/'profile')))
    with pytest.raises(PlatformRejected,match='手工登录 Google') as error:
        check.verify_page(page)
    assert error.value.status == 'AUTH_REQUIRED'
    page.evaluate.assert_not_called()


@pytest.mark.parametrize('channel', ['chrome','msedge'])
def test_system_browser_keeps_synthetic_session_across_restarts(tmp_path,channel):
    """Only a disposable profile and intercepted .invalid pages are used."""
    from playwright.sync_api import sync_playwright
    if not any(b.channel == channel for b in browsers.installed_browsers()):
        pytest.skip('System browser is not installed on this test host')
    (tmp_path/browsers.MARKER).write_text(json.dumps({'schema':1,'channel':channel}))
    with sync_playwright() as playwright:
        for cycle in range(2):
            context = browsers.launch_youtube_context(playwright,tmp_path,headless=True)
            try:
                context.route('**/*',lambda route:route.fulfill(body='<p>local fixture</p>',content_type='text/html'))
                page = context.new_page()
                page.goto('https://shusheng-fixture.invalid/')
                if cycle == 0:
                    page.evaluate('localStorage.setItem("synthetic_session", "test-only")')
                else:
                    assert page.evaluate('localStorage.getItem("synthetic_session")') == 'test-only'
                summary = browsers.youtube_runtime_summary(context,tmp_path)
                assert 'Playwright' in summary and '账号专用目录' in summary
                assert '内置Chromium' not in summary
            finally:
                context.close()


def test_actual_context_is_closed_before_handing_profile_to_manual_browser(tmp_path,monkeypatch):
    app = QApplication.instance() or QApplication([])
    store = ChannelAccountStore(tmp_path)
    worker = ui.ChannelProbeWorker(store.add('youtube','test'),tmp_path/'profile',tmp_path/'reports')
    actions = []
    context = MagicMock()
    context.pages = [MagicMock(url='https://studio.youtube.com/')]
    context.close.side_effect = lambda:actions.append('close-automation')
    manager = MagicMock()
    monkeypatch.setattr(ui,'sync_playwright',lambda:manager)
    monkeypatch.setattr(ui,'launch_youtube_context',lambda *args,**kwargs:context)
    worker.ready.connect(worker.request_google_manual)
    process = MagicMock()
    process.poll.side_effect = lambda: (worker.request_close(),0)[1]
    def manual(profile):
        actions.append('manual-start')
        return process,browsers.SystemBrowser('chrome','Chrome',Path('chrome.exe'))
    monkeypatch.setattr(ui,'start_manual_login',manual)
    worker.run()
    assert actions == ['close-automation','manual-start'] and app


def test_failed_context_shutdown_does_not_start_a_second_browser(tmp_path,monkeypatch):
    app = QApplication.instance() or QApplication([])
    store = ChannelAccountStore(tmp_path)
    worker = ui.ChannelProbeWorker(store.add('youtube','test'),tmp_path/'profile',tmp_path/'reports')
    context = MagicMock()
    context.pages = [MagicMock(url='https://studio.youtube.com/')]
    context.close.side_effect = RuntimeError('context did not release profile')
    monkeypatch.setattr(ui,'sync_playwright',MagicMock())
    monkeypatch.setattr(ui,'launch_youtube_context',lambda *args,**kwargs:context)
    manual = MagicMock()
    monkeypatch.setattr(ui,'start_manual_login',manual)
    worker.ready.connect(worker.request_google_manual)
    errors=[]
    worker.error.connect(errors.append)
    worker.run()
    manual.assert_not_called()
    assert 'context did not release profile' in errors[0] and app


def test_handoff_uses_profile_release_even_if_launcher_process_stays_alive(tmp_path,installed,monkeypatch):
    process = MagicMock()
    process.poll.return_value = None
    busy = iter([True,False,False,False])
    monkeypatch.setattr(browsers,'profile_busy',lambda _:next(busy))
    handoff = browsers.ManualBrowserHandoff(tmp_path,installed[0],process)
    assert [handoff.released() for _ in range(4)] == [False,False,False,True]
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_manual_finish_button_remains_enabled_and_requests_verification(tmp_path,monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui.ChannelProbeWorker,'start',lambda _:None)
    store = ChannelAccountStore(tmp_path)
    dialog = ui.ChannelProbeDialog(store.add('youtube','test'),tmp_path/'profile',tmp_path/'reports')
    confirmed=[]
    dialog.login_confirmed.connect(lambda:confirmed.append(True))
    dialog.worker.request_manual_finish = MagicMock()
    dialog.on_manual_changed(True)
    assert dialog.confirm_button.isEnabled()
    assert dialog.confirm_button.text() == '登录完成，验证频道'
    assert not dialog.capture_button.isEnabled() and not dialog.nickname_button.isEnabled()
    dialog.confirm_button.click()
    dialog.worker.request_manual_finish.assert_called_once()
    assert not confirmed
    dialog.on_error('browser not yet closed')
    assert not dialog.capture_button.isEnabled()
    assert dialog.close_button.isEnabled()
    dialog.close()
    assert app


def test_existing_manual_session_is_resumed_without_starting_duplicate_browser(tmp_path,installed,monkeypatch):
    (tmp_path/browsers.MARKER).write_text(json.dumps({'schema':1,'channel':'chrome'}))
    monkeypatch.setattr(browsers,'profile_busy',lambda _:True)
    popen=MagicMock()
    monkeypatch.setattr(browsers.subprocess,'Popen',popen)
    process, browser=browsers.start_manual_login(tmp_path)
    assert process is None and browser == installed[0]
    popen.assert_not_called()
    with pytest.raises(RuntimeError,match='登录完成，验证频道'):
        browsers.launch_youtube_context(MagicMock(),tmp_path)


def test_handoff_timeout_reports_action_and_cancel_does_not_hang(tmp_path,monkeypatch):
    app = QApplication.instance() or QApplication([])
    store=ChannelAccountStore(tmp_path)
    worker=ui.ChannelProbeWorker(store.add('youtube','test'),tmp_path/'profile',tmp_path/'reports')
    worker.request_google_manual()
    monkeypatch.setattr(ui,'manual_browser_open',lambda _:False)
    monkeypatch.setattr(ui,'start_manual_login',lambda _: (MagicMock(),browsers.SystemBrowser('chrome','Chrome',Path('chrome.exe'))))
    handoff=MagicMock()
    handoff.released.return_value=False
    handoff.close_windows.return_value=1
    monkeypatch.setattr(ui,'ManualBrowserHandoff',lambda *args:handoff)
    clock=iter([0,30])
    monkeypatch.setattr(ui.time,'monotonic',lambda:next(clock))
    monkeypatch.setattr(ui.time,'sleep',lambda _:None)
    worker.manual_changed.connect(lambda active:worker.request_manual_finish() if active else None)
    errors=[]
    def error(message):
        errors.append(message)
        worker.request_close()
    worker.error.connect(error)
    worker._run_automated=MagicMock()
    worker.run()
    assert errors and '尚未释放' in errors[0] and '取消等待' in errors[0]
    assert not worker.manual_active.is_set() and app
    worker._run_automated.assert_not_called()


def test_completed_manual_login_persists_identity_without_implicit_authorization(tmp_path,monkeypatch):
    from src.publisher.account_repository import PlatformAccountRepository
    from src.publisher.douyin.config import DouyinAccountManager
    from src.app.platform_account_panel import PlatformAccountPanel
    app=QApplication.instance() or QApplication([])
    store=ChannelAccountStore(tmp_path/'store')
    account=store.add('youtube','新账号（待登录）')
    manager=DouyinAccountManager(tmp_path/'douyin.json',tmp_path/'profiles')
    controller=ui.MultiPlatformPublisherPanel(store=store,douyin_manager=manager)
    panel=controller.panels[3]
    worker=ui.ChannelProbeWorker(account,store.profile_path(account),tmp_path/'reports')
    worker.identity_found.connect(lambda identity:panel.apply_identity(account,identity))
    channel='UC'+'a'*22
    page=MagicMock(url='https://studio.youtube.com/channel/'+channel+'/videos')
    page.is_closed.return_value=False
    page.evaluate.return_value={'nickname':'已登录测试频道','source':'own-profile'}
    page.wait_for_timeout.side_effect=lambda _:worker.request_close()
    context=MagicMock()
    context.pages=[page]
    monkeypatch.setattr(ui,'sync_playwright',MagicMock())
    monkeypatch.setattr(ui,'manual_browser_open',lambda _:False)
    monkeypatch.setattr(ui,'launch_youtube_context',lambda *args,**kwargs:context)
    process=MagicMock()
    process.poll.return_value=0
    monkeypatch.setattr(ui,'start_manual_login',lambda _: (process,browsers.SystemBrowser('chrome','Chrome',Path('chrome.exe'))))
    monkeypatch.setattr(ui.time,'sleep',lambda _:None)
    rounds=[]
    def ready():
        rounds.append(True)
        if len(rounds)==1:
            worker.request_google_manual()
    worker.ready.connect(ready)
    try:
        worker.run()
        reloaded=ChannelAccountStore(store.root).for_channel('youtube')[0]
        assert len(rounds)==2
        assert reloaded.account_id==account.account_id
        assert reloaded.login_status!='AUTHORIZED'  # A nickname alone is not an upload-entry check.
        assert reloaded.channel_id==channel and reloaded.platform_nickname=='已登录测试频道'
        assert not worker.verify_after_manual and app
    finally:
        controller.prepare_shutdown()
        controller.close()


@pytest.mark.parametrize('channel',['chrome','msedge'])
@pytest.mark.skipif(os.name!='nt',reason='Windows Chromium profile ownership')
def test_real_normal_browser_handoff_closes_owned_window_and_retains_local_session(tmp_path,monkeypatch,channel):
    """Exercises the actual normal process, WM_CLOSE, profile release and reopen.

    The browser opens only a generated local file. All profiles are disposable;
    no Google sign-in, passwords, user's browser or publishing is involved.
    """
    import time
    from playwright.sync_api import sync_playwright
    from src.publisher.windows_browser_session import profile_busy
    browser=next((b for b in browsers.installed_browsers() if b.channel==channel),None)
    if browser is None:
        pytest.skip('System browser not installed')
    monkeypatch.setattr(browsers,'installed_browsers',lambda:[browser])
    fixture=tmp_path/'handoff.html'
    fixture.write_text('<title>ShuSheng disposable handoff fixture</title><script>'
        'if(location.hash==="#seed")localStorage.setItem("shusheng_handoff", "persisted-local-only")'
        '</script><p>Local synthetic session</p>',encoding='utf-8')
    process, _=browsers.start_manual_login(tmp_path/'account',start_url=fixture.as_uri()+'#seed')
    handoff=browsers.ManualBrowserHandoff(tmp_path/'account',browser,process)
    try:
        deadline=time.monotonic()+15
        while not profile_busy(handoff.profile) and time.monotonic()<deadline:
            time.sleep(.1)
        assert profile_busy(handoff.profile)
        assert not handoff.released()
        time.sleep(2)  # Allow the local file script to execute in a normal browser.
        with sync_playwright() as playwright:
            unrelated=playwright.chromium.launch_persistent_context(str(tmp_path/'unrelated'),
                channel=channel,executable_path=str(browser.executable),headless=True,timeout=10000)
            try:
                other=unrelated.new_page()
                other.set_content('<title>unrelated fixture</title>')
                assert handoff.close_windows()>=1
                deadline=time.monotonic()+20
                while not handoff.released() and time.monotonic()<deadline:
                    time.sleep(.2)
                assert not profile_busy(handoff.profile)
                assert other.title()=='unrelated fixture'
            finally:
                unrelated.close()
            context=browsers.launch_youtube_context(playwright,tmp_path/'account',headless=True,timeout=10000)
            try:
                page=context.new_page()
                page.goto(fixture.as_uri())
                assert page.evaluate('localStorage.getItem("shusheng_handoff")')=='persisted-local-only'
            finally:
                context.close()
    finally:
        # Never kill a browser or remove an occupied directory, even on failure.
        if profile_busy(handoff.profile):
            handoff.close_windows()
