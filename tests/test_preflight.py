"""Preflight never uploads/posts; live page checks use only local browser routes."""
import json
import threading
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

from src.publisher import preflight
from src.publisher.multi_deploy import PublishTarget
from src.publisher.platforms import CHANNELS, ChannelAccount
from src.publisher.youtube import PlatformRejected, UploadStopped


CHANNEL_ID = 'UC' + 'a' * 22


def target(tmp_path, platform='kuaishou', account_id='a'*32):
    account = ChannelAccount(account_id,platform,'备注',platform_nickname='本人昵称',
        login_status='AUTHORIZED',channel_id=CHANNEL_ID)
    profile = tmp_path/'profiles'/platform/account_id
    profile.mkdir(parents=True,exist_ok=True)
    (profile/'local-marker').write_text('test',encoding='utf-8')
    return PublishTarget(platform,account,str(profile))


def capture(root, target):
    path = root/'dom_reports'/target.platform/target.account.account_id/'session'/'capture.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'schema_version':1,'platform':target.platform,
        'account_id':target.account.account_id,'url':CHANNELS[target.platform].url,
        'frames':[{'nodes':[{'tag':'input','type':'file','accept':'video/*'},
                           {'tag':'div','contentEditable':True,'visible':True}]}]}),encoding='utf-8')
    return path


@pytest.fixture(scope='module')
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route('**/*',lambda route:route.fulfill(body='<html><body></body></html>',content_type='text/html'))
        yield context.new_page()
        browser.close()


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_without_builtin_adapter_dom_gate_accepts_only_selected_account(tmp_path,platform,monkeypatch):
    from src.publisher import page_structure
    monkeypatch.setattr(page_structure,'STRUCTURES',{})
    chosen = target(tmp_path,platform)
    capture(tmp_path,target(tmp_path,platform,'b'*32))
    failures = preflight.prerequisite_results([chosen],tmp_path/'dom_reports')
    assert len(failures)==1 and '当前账号尚未采集' in failures[0].message
    path = capture(tmp_path,chosen)
    before = path.read_bytes()
    assert not preflight.prerequisite_results([chosen],tmp_path/'dom_reports')
    assert path.read_bytes()==before


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_cached_authorized_and_valid_dom_do_not_override_live_login_page(page,tmp_path,platform):
    chosen = target(tmp_path,platform)
    capture(tmp_path,chosen)
    assert not preflight.prerequisite_results([chosen],tmp_path/'dom_reports')
    page.goto(CHANNELS[platform].url)
    page.set_content('<button>立即登录</button>')
    with pytest.raises(PlatformRejected) as error:
        preflight.AccountPreflight(chosen,seconds=.1).verify_page(page)
    assert error.value.status=='AUTH_REQUIRED'
    assert chosen.account.login_status=='AUTHORIZED'  # Cache is not mutated or accepted as proof.


def test_legacy_douyin_structure_passes_dom_gate_but_cannot_override_live_login(page,tmp_path):
    from tests.test_dom_readiness import legacy_report
    root,path=legacy_report(tmp_path)
    chosen=target(tmp_path,'douyin','01_5006c0')
    assert not preflight.prerequisite_results([chosen],root)
    page.goto(CHANNELS['douyin'].url)
    page.set_content('<meta charset="utf-8"><button>立即登录</button>')
    with pytest.raises(PlatformRejected) as error:
        preflight.AccountPreflight(chosen,seconds=.1).verify_page(page)
    assert error.value.status=='AUTH_REQUIRED'


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_google_or_platform_login_redirect_stops_preflight(page,tmp_path,platform):
    page.goto('https://accounts.google.com/signin' if platform=='youtube' else 'https://'+CHANNELS[platform].host+'/login')
    with pytest.raises(PlatformRejected) as error:
        preflight.AccountPreflight(target(tmp_path,platform),seconds=.1).verify_page(page)
    assert error.value.status=='AUTH_REQUIRED'


@pytest.mark.parametrize('html,state',[
    ('<header><div class="user-info"><span class="name">TA</span></div></header><h2>账号已被封禁</h2><p>无法访问创作者中心</p>','ACCOUNT_RESTRICTED'),
    ('<div role="alert">系统繁忙，请稍后再试</div>','PLATFORM_BUSY'),
    ('<div role="dialog">扫码验证 请使用App扫码验证身份 二维码</div>','VERIFICATION_REQUIRED')])
def test_restricted_account_is_rejected_before_identity_or_file_selection(page,tmp_path,html,state):
    page.goto(CHANNELS['kuaishou'].url)
    page.set_content(html)
    with pytest.raises(PlatformRejected) as error:
        preflight.AccountPreflight(target(tmp_path),seconds=.1).verify_page(page)
    assert error.value.status==state


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_current_identity_passes_without_touching_file_input_or_submit(page,tmp_path,platform):
    url = CHANNELS[platform].url if platform!='youtube' else 'https://studio.youtube.com/channel/'+CHANNEL_ID
    page.goto(url)
    identity = ('<header class="creator-header"><span class="name">本人昵称</span></header>' if platform=='douyin'
        else '<header><div class="user-info"><span class="name">本人昵称</span></div></header>')
    if platform == 'wechat_channels':
        identity = '<aside id="side-bar"><div class="side-bar-footer"><div class="account-info"><span class="name">本人昵称</span></div></div></aside>'
    page.set_content(identity+'<input placeholder="短标题"><input type=file accept="video/*"><button onclick="window.clicked=true">发布</button>')
    preflight.AccountPreflight(target(tmp_path,platform),seconds=.1).verify_page(page)
    assert page.locator('input[type=file]').evaluate('e=>e.files.length')==0
    assert not page.evaluate('window.clicked===true')


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_only_stable_channel_mismatch_blocks_but_nickname_change_does_not(page,tmp_path,platform):
    account=target(tmp_path,platform)
    page.goto(CHANNELS[platform].url if platform!='youtube' else 'https://studio.youtube.com/channel/UC'+'b'*22)
    identity = '<header class="creator-header"><div class="user-info"><span class="name">新昵称</span></div></header>'
    if platform == 'wechat_channels':
        identity = '<aside id="side-bar"><div class="side-bar-footer"><div class="account-info"><span class="name">新昵称</span></div></div></aside><input placeholder="短标题">'
    page.set_content(identity+'<input type=file accept="video/*">')
    if platform=='youtube':
        with pytest.raises(PlatformRejected) as error:
            preflight.AccountPreflight(account,seconds=.1).verify_page(page)
        assert error.value.status=='IDENTITY_MISMATCH'
    else:
        preflight.AccountPreflight(account,seconds=.1).verify_page(page)
        assert account.account.platform_nickname=='新昵称'


def test_stop_interrupts_identity_wait(page,tmp_path):
    page.goto(CHANNELS['kuaishou'].url)
    page.set_content('<header><div class="user-info"><span class="name">TA</span></div></header>')
    stop = threading.Event()
    timer = threading.Timer(.05,stop.set)
    timer.start()
    try:
        with pytest.raises(UploadStopped):
            preflight.AccountPreflight(target(tmp_path),stop.is_set,seconds=2).verify_page(page)
    finally:
        timer.cancel()


@pytest.mark.parametrize('html,passes',[
    ('<input type=file accept="video/*">',True),
    ('<input type=file accept="video/*" disabled>',False),
    ('<h1>上传视频</h1>',False)])
def test_old_douyin_account_uses_live_protected_input_without_requiring_new_nickname_binding(page,tmp_path,html,passes):
    chosen = target(tmp_path,'douyin')
    chosen.account.platform_nickname=''
    page.goto(CHANNELS['douyin'].url)
    page.set_content(html)
    checker = preflight.AccountPreflight(chosen,seconds=.1)
    if passes:
        checker.verify_page(page)
        assert page.locator('input').evaluate('e=>e.files.length')==0
    else:
        with pytest.raises(RuntimeError,match='超时'):
            checker.verify_page(page)


def test_preflight_closes_each_profile_before_producing_a_result(tmp_path,monkeypatch):
    chosen = target(tmp_path)
    checker = preflight.AccountPreflight(chosen)
    playwright = MagicMock()
    context = playwright.chromium.launch_persistent_context.return_value
    context.browser.is_connected.return_value=False
    context.pages = [MagicMock()]
    monkeypatch.setattr(checker,'verify_page',lambda page:None)
    result = checker.check(playwright)
    assert result.passed
    assert playwright.chromium.launch_persistent_context.call_args.args==(chosen.profile,)
    context.close.assert_called_once()
    context.pages[0].locator.assert_not_called()


def test_missing_media_codecs_blocks_before_navigation_or_upload(tmp_path,monkeypatch):
    checker=preflight.AccountPreflight(target(tmp_path))
    playwright=MagicMock()
    context=playwright.chromium.launch_persistent_context.return_value
    context.browser.is_connected.return_value=False
    page=MagicMock();page.evaluate.return_value=dict(h264=False,aac=False)
    context.pages=[page]
    verify=MagicMock();monkeypatch.setattr(checker,'verify_page',verify)
    result=checker.check(playwright)
    assert result.status=='CONFIG_REQUIRED' and 'H.264/AAC' in result.message
    verify.assert_not_called()
    page.goto.assert_not_called()
    page.locator.assert_not_called()
    context.close.assert_called_once()
    assert not (tmp_path/'target_ledger.json').exists()


def test_missing_prerequisites_never_launches_browser_or_creates_task_state(tmp_path,monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(preflight,'sync_playwright',factory)
    chosen=target(tmp_path)
    chosen.profile=str(tmp_path/'missing-profile')
    worker = preflight.DeploymentPreflightWorker([chosen],tmp_path)
    worker.run()
    factory.assert_not_called()
    assert preflight.failure_message(worker.results)
    assert not (tmp_path/'target_ledger.json').exists()
    assert not (tmp_path/'publish_reports').exists()


def test_one_failure_blocks_whole_plan_without_opening_later_platforms(tmp_path,monkeypatch):
    chosen = [target(tmp_path,p) for p in CHANNELS]
    for t in chosen:
        capture(tmp_path,t)
    calls = []
    def check(self,playwright):
        calls.append(self.target.key)
        state = 'AUTH_REQUIRED' if self.target.platform=='kuaishou' else 'SUCCESS'
        return preflight.PreflightResult(self.target.key,preflight.target_name(self.target),state,'请登录')
    monkeypatch.setattr(preflight,'sync_playwright',MagicMock())
    monkeypatch.setattr(preflight.AccountPreflight,'check',check)
    worker = preflight.DeploymentPreflightWorker(chosen,tmp_path)
    chosen[0].account.account_name='检查中改备注'
    worker.run()
    assert calls == [t.key for t in chosen[:3]]
    assert '快手 / 备注' in preflight.failure_message(worker.results)
    assert worker.targets[0].account.account_name=='备注'
    assert not (tmp_path/'target_ledger.json').exists()
