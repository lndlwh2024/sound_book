"""Avatar-only WeChat regressions; local DOM routes and temporary account data."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
import threading

import pytest
from playwright.sync_api import sync_playwright

from src.publisher.platforms import ChannelAccount, ChannelAccountStore, CHANNELS
from src.publisher.multi_deploy import PublishTarget
from src.publisher.identity import protected_upload_access, extract_platform_identity
from src.publisher.preflight import AccountPreflight, PreflightResult
from src.publisher.wechat_channels import WeChatChannelsUploader
from src.publisher.youtube import PlatformRejected
from tests.test_common_platform_accounts import panel
from tests.test_publishing_options import app


AVATAR_FORM = '''<aside style="width:220px"><div class="common-menu-item account-info default">
<div class="account-info"><img class="avatar" style="width:32px;height:32px"></div>
</div></aside><input type="file" accept="video/mp4,video/x-m4v,video/*" style="display:none">
<input placeholder="填写短标题有机会获得更多流量"><button onclick="window.posted=true">发表</button>'''


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        yield browser
        browser.close()


def local_page(browser,body=AVATAR_FORM,shadow=False):
    page=browser.new_page()
    page.route('**/*',lambda route:route.fulfill(body='<meta charset="utf-8">',content_type='text/html'))
    page.goto(CHANNELS['wechat_channels'].url)
    if shadow:
        page.evaluate("body=>{let e=document.createElement('wujie-app');document.body.append(e);e.attachShadow({mode:'open'}).innerHTML=body}",body)
    else:
        page.set_content(body)
    return page


def owner(custom='我的视频号'):
    return ChannelAccount('a'*32,'wechat_channels','本地备注',custom_nickname=custom,login_status='USER_CONFIRMED')


@pytest.mark.parametrize('shadow',[False,True])
def test_avatar_only_custom_name_passes_preflight_and_actual_uploader_without_upload(browser,tmp_path,shadow):
    page=local_page(browser,shadow=shadow)
    try:
        account=owner()
        assert extract_platform_identity(page,'wechat_channels') is None
        assert protected_upload_access(page,'wechat_channels')
        check=AccountPreflight(PublishTarget('wechat_channels',account,str(tmp_path/'profile')),seconds=.5)
        check.verify_page(page)
        uploader=WeChatChannelsUploader(account,tmp_path/'profile',tmp_path/'reports')
        uploader.verify_identity(page)
        assert account.platform_nickname=='' and account.login_status=='USER_CONFIRMED'
        assert page.locator('input[type=file]').evaluate('e=>e.files.length')==0
        assert not page.evaluate('window.posted===true')
    finally:page.close()


def test_avatar_only_without_custom_name_returns_configuration_prompt_without_name_timeout(browser,tmp_path):
    page=local_page(browser)
    try:
        check=AccountPreflight(PublishTarget('wechat_channels',owner(''),str(tmp_path)),seconds=.5)
        with pytest.raises(PlatformRejected) as error:check.verify_page(page)
        assert error.value.status=='CONFIG_REQUIRED'
        assert check.nickname_missing and '自定义昵称' in str(error.value)
        assert '超时' not in str(error.value)
    finally:page.close()


@pytest.mark.parametrize('body',[
    '<h1>发表动态</h1>',
    AVATAR_FORM.replace('type="file"','disabled type="file"'),
    AVATAR_FORM.replace('class="avatar"','class="other-avatar"'),
    AVATAR_FORM.replace('placeholder="填写短标题有机会获得更多流量"','placeholder="搜索"'),
    AVATAR_FORM+'<input type=password>',
    AVATAR_FORM+'<button>扫码登录</button>',
    AVATAR_FORM+'<input type=file accept="video/*">',
])
def test_custom_label_cannot_substitute_for_live_authenticated_upload_form(browser,body):
    page=local_page(browser,body,shadow=True)
    try:assert not protected_upload_access(page,'wechat_channels')
    finally:page.close()


@pytest.mark.parametrize('shadow',[False,True])
def test_custom_name_still_rejects_actual_wrong_account_and_login_redirect(browser,tmp_path,shadow):
    page=local_page(browser,shadow=shadow)
    try:
        # Put a readable own-account name in the account card, not arbitrary text.
        page.locator('.common-menu-item').evaluate("e=>e.insertAdjacentHTML('beforeend','<span class=nickname>另一个账号</span>')")
        account=owner();account.platform_nickname='原账号'
        check=AccountPreflight(PublishTarget('wechat_channels',account,str(tmp_path)),seconds=.5)
        with pytest.raises(PlatformRejected) as error:check.verify_page(page)
        assert error.value.status=='IDENTITY_MISMATCH'
        page.goto('https://channels.weixin.qq.com/login')
        with pytest.raises(PlatformRejected) as error:check.verify_page(page)
        assert error.value.status=='AUTH_REQUIRED'
    finally:page.close()


@pytest.mark.parametrize('platform',['kuaishou','xiaohongshu'])
def test_custom_name_checks_initial_creator_upload_before_title_editor_exists(browser,tmp_path,platform):
    from src.publisher.creator_studio import KuaishouUploader,XiaohongshuUploader
    page=browser.new_page()
    page.route('**/*',lambda r:r.fulfill(body='<meta charset="utf-8"><header><img class="avatar" '
        'style="width:32px;height:32px"></header><input type=file accept="video/*">',content_type='text/html'))
    page.goto(CHANNELS[platform].url)
    try:
        account=ChannelAccount('a'*32,platform,'备注',custom_nickname='本地显示名')
        cls=KuaishouUploader if platform=='kuaishou' else XiaohongshuUploader
        cls(account,tmp_path/'profile',tmp_path/'reports').verify_identity(page)
        assert page.locator('input').evaluate('e=>e.files.length')==0
        assert not page.locator('[contenteditable], input[placeholder]').count()
    finally:page.close()


def test_custom_nickname_preserves_all_platform_account_ids_profiles_login_and_actual_name(panel):
    for view in panel.panels:
        original=view.current()
        original.login_status='USER_CONFIRMED'
        old=deepcopy(original)
        profile=panel.repository.profile(original)
        assert view.set_custom_nickname(value='本地备注 '+view.platform)
        saved=next(a for a in panel.repository.accounts(view.platform) if a.account_id==old.account_id)
        assert saved.custom_nickname=='本地备注 '+view.platform
        assert saved.platform_nickname==old.platform_nickname
        assert saved.login_status==old.login_status
        assert saved.account_id==old.account_id and panel.repository.profile(saved)==profile
        assert '（自定义）' in view.account_label(saved)
        publishing=panel.repository.publish_account(saved)
        assert publishing.custom_nickname==saved.custom_nickname
        assert publishing.platform_nickname==old.platform_nickname
    reloaded=ChannelAccountStore(panel.store.root)
    assert all(a.custom_nickname for a in reloaded.accounts if a.platform!='xiaohongshu')


def test_missing_nickname_prompts_locally_after_preflight_without_opening_another_browser(panel,monkeypatch):
    view=next(p for p in panel.panels if p.platform=='wechat_channels')
    account=view.current()
    target=PublishTarget(view.platform,account,panel.repository.profile(account))
    worker=MagicMock(targets=[target],stopping=threading.Event())
    worker.results=[PreflightResult(target.key,'微信视频号','CONFIG_REQUIRED','头像无昵称',nickname_missing=True)]
    panel.preflight_worker=worker
    panel._preflight_file_paths=None
    view.set_custom_nickname=MagicMock(return_value=True)
    view.open_probe=MagicMock()
    panel.launch_deployment=MagicMock()
    panel.preflight_finished()
    view.set_custom_nickname.assert_called_once_with(account=account)
    view.open_probe.assert_not_called()
    panel.launch_deployment.assert_not_called()
    assert panel.preflight_worker is None


@pytest.mark.parametrize('close_raises',[False,True])
def test_failure_capture_then_context_close_then_browser_fallback_before_result(tmp_path,monkeypatch,close_raises):
    from src.publisher import media_browser
    events=[]
    context=MagicMock();page=MagicMock();context.pages=[page]
    context.browser.is_connected.side_effect=[close_raises,False] if close_raises else [False,False]
    def close():
        events.append('context_close')
        if close_raises:raise RuntimeError('context teardown interrupted')
    context.close.side_effect=close
    context.browser.close.side_effect=lambda:events.append('browser_close')
    monkeypatch.setattr(media_browser,'launch_media_context',lambda *args,**kwargs:context)
    monkeypatch.setattr(media_browser,'media_capabilities',lambda p:dict(h264=True,aac=True))
    check=AccountPreflight(PublishTarget('wechat_channels',owner(),str(tmp_path)))
    check.failure_capture=lambda *args,**kwargs:events.append('capture')
    def fail(p):raise PlatformRejected('AUTH_REQUIRED','请先登录')
    monkeypatch.setattr(check,'verify_page',fail)
    result=check.check(MagicMock())
    events.append('result')
    assert result.status=='AUTH_REQUIRED'
    assert events==(['capture','context_close','browser_close','result'] if close_raises else ['capture','context_close','result'])
    page.set_input_files.assert_not_called()


def test_wechat_custom_name_reconciliation_requires_stable_account_bound_real_success(tmp_path):
    account=owner()
    uploader=WeChatChannelsUploader(account,tmp_path/'profile',tmp_path/'reports')
    uploader.submitted=True
    uploader.confirmation={'kind':'wechat_publish_response','accepted':True,'business_code':0,
        'endpoint':'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create'}
    info=uploader.result_hint()
    assert uploader.reconcile(info)
    for changed in ({'account_id':'other'},{'profile_id':'other'},{'submitted':False},
                    {'nickname':'another'},{'confirmation':{}},{'confirmation':{'kind':'draft'}}):
        assert not uploader.reconcile(dict(info,**changed))


@pytest.mark.parametrize('standalone',[False,True])
def test_custom_name_entry_fits_both_editions_and_does_not_add_form_scroll(app,tmp_path,monkeypatch,standalone):
    from PySide6.QtWidgets import QInputDialog,QDialog,QScrollArea
    from src.app import platform_account_panel as ui
    from src.app.channel_panel import MultiPlatformPublisherPanel
    from src.app.publisher_window import PublisherWindow
    from src.publisher.douyin.config import DouyinAccountManager
    artifacts=Path('artifacts/custom-nickname-20261020')
    artifacts.mkdir(parents=True,exist_ok=True)
    if standalone:
        window=PublisherWindow(tmp_path)
        controller=window.panel
    else:
        import ast
        store=ChannelAccountStore(tmp_path/'state')
        store.cloud_ingest_enabled=False
        window=controller=MultiPlatformPublisherPanel(store=store,
            douyin_manager=DouyinAccountManager(tmp_path/'douyin.json',tmp_path/'profiles'))
        source=ast.parse(Path('src/app/main_window.py').read_text(encoding='utf-8'))
        style=next(n.value.value for n in ast.walk(source) if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='style_text' for t in n.targets)
            and isinstance(n.value,ast.Constant))
        window.setStyleSheet(style)
    try:
        account=controller.repository.add('wechat_channels','新账号（待登录）')
        account.login_status='USER_CONFIRMED';controller.repository.save(account)
        view=next(p for p in controller.panels if p.platform=='wechat_channels');view.reload()
        controller.channel_view.setCurrentIndex(controller.channel_view.findData(view.platform))
        window.resize(1366 if standalone else 1640,696 if standalone else 920)
        window.show();app.processEvents()
        controller._set_initial_sizes();app.processEvents()
        class CaptureInput(QInputDialog):
            def exec(self):
                self.setTextValue('朴真趣科');self.show();app.processEvents()
                assert self.width()<=650 and self.height()<=300
                self.grab().save(str(artifacts/'custom-name-input.png'))
                self.accept();return QDialog.Accepted
        monkeypatch.setattr(ui,'QInputDialog',CaptureInput)
        view.custom_nickname_button.click();app.processEvents()
        assert view.current().custom_nickname=='朴真趣科'
        assert view.custom_nickname_button.isVisible() and view.custom_nickname_button.isEnabled()
        for button in (view.login_button,view.custom_nickname_button,view.capture_button,
                       view.save_button,view.deploy_button,view.pause_button):
            top=button.mapTo(window,button.rect().topLeft())
            assert 0<=top.x() and top.x()+button.width()<=window.width()
            assert 0<=top.y() and top.y()+button.height()<=window.height()
            assert button.width()>=button.fontMetrics().horizontalAdvance(button.text())+10
        assert not view.findChildren(QScrollArea)
        window.grab().save(str(artifacts/('standalone-1366.png' if standalone else 'full-1640.png')))
        if standalone:
            controller.automatic=True
            target=PublishTarget(view.platform,view.current(),controller.repository.profile(view.current()))
            worker=MagicMock(targets=[target],stopping=threading.Event())
            worker.results=[PreflightResult(target.key,'微信视频号','CONFIG_REQUIRED','设置自定义昵称',nickname_missing=True)]
            controller.preflight_worker=worker
            controller.schedule_retry=MagicMock()
            controller.preflight_finished()
            controller.schedule_retry.assert_not_called()
    finally:
        controller.prepare_shutdown()
        for worker in (controller.snapshot_worker,controller.cleanup_worker,controller.drive_watch_worker):
            if worker:worker.wait(5000)
        if standalone:window.closing=True
        app.processEvents();window.close();window.deleteLater();app.processEvents()
