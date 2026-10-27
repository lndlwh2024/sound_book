"""Session handoff, real login redirects, isolated logs and selected-file status regressions."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from src.publisher import media_browser
from src.publisher.channel_probe import CREATOR_NOTICES_JS
from src.publisher.creator_studio import CreatorStudioUploader
from src.publisher.event_log import make_event
from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger, PublishTarget
from src.publisher.platforms import CHANNELS, PUBLISH_ORDER, ChannelAccount
from src.publisher.preflight import AccountPreflight, PreflightResult
from src.publisher.scan_cleanup import ScanCleanupWorker
from src.publisher.status import BLOCKING, fresh_result
from src.publisher.youtube import PlatformRejected
from tests.test_common_platform_accounts import panel
from tests.test_custom_nickname import AVATAR_FORM, local_page, owner
from tests.test_scan_cleanup_ui import drain


@pytest.fixture(scope='module',autouse=True)
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_login_preflight_and_upload_reopen_same_profile_without_losing_session(tmp_path,monkeypatch,platform):
    executable=Path('artifacts/publisher-media-browser/chrome-win64/chrome.exe').resolve()
    if not executable.is_file():pytest.skip('Matched release browser is unavailable')
    monkeypatch.setenv('SHUSHENG_MEDIA_BROWSER',str(executable))
    account=ChannelAccount('a'*32,platform,'fixture',channel_id='UC'+'a'*22)
    profile=tmp_path/'same-account-profile'
    target=PublishTarget(platform,account,str(profile))
    url=CHANNELS[platform].url if platform!='youtube' else 'https://studio.youtube.com/channel/'+account.channel_id
    host=CHANNELS[platform].host
    html=(AVATAR_FORM if platform=='wechat_channels' else
        '<header><div class="user-info"><span class="name">本人昵称</span></div></header>'
        '<input type=file accept="video/*"><button onclick="window.submitted=true">发布</button>')
    with sync_playwright() as runtime:
        for phase in ('login','preflight','upload'):
            context=media_browser.launch_media_context(runtime,profile,headless=True)
            try:
                assert all(page.url=='about:blank' for page in context.pages)
                assert len(context.pages)==1
                context.route('**/*',lambda r:r.fulfill(body='<meta charset="utf-8">'+html,content_type='text/html'))
                if phase=='login':
                    context.add_cookies([
                        {'name':'session_auth_fixture','value':'fixture','domain':host,'path':'/','httpOnly':True,'secure':True},
                        {'name':'persistent_fixture','value':'fixture','domain':host,'path':'/','expires':time.time()+86400,'secure':True}])
                page=context.pages[0];page.goto(url)
                if phase=='login':
                    page.evaluate("sessionStorage.setItem('auth-fixture','same-session');localStorage.setItem('marker','preserved')")
                else:
                    assert {c['name'] for c in context.cookies()}=={'session_auth_fixture','persistent_fixture'}
                    assert page.evaluate("sessionStorage.getItem('auth-fixture')")=='same-session'
                    assert page.evaluate("localStorage.getItem('marker')")=='preserved'
                    AccountPreflight(target,seconds=2).verify_page(page)
                    assert page.locator('input[type=file]').evaluate('e=>e.files.length')==0
                    assert page.evaluate('window.submitted===true') is False
                if phase=='upload':
                    # Real logout/expiry must still block; native restoration is
                    # never accepted as proof of authentication.
                    page.goto('https://'+host+'/login.html')
                    with pytest.raises(PlatformRejected) as error:
                        AccountPreflight(target,seconds=.5).verify_page(page)
                    assert error.value.status=='AUTH_REQUIRED'
            finally:
                media_browser.close_media_context(context)


@pytest.mark.parametrize('redirect',[True,False])
def test_navigation_destroyed_during_preflight_notice_reads_new_document(tmp_path,redirect):
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=local_page(browser)
        class NavigatingPage:
            triggered=False
            def __getattr__(self,name):return getattr(page,name)
            def evaluate(self,expression,*args):
                if expression==CREATOR_NOTICES_JS and not self.triggered:
                    self.triggered=True
                    if redirect:page.goto('https://channels.weixin.qq.com/login.html')
                    else:page.set_content(AVATAR_FORM)
                    raise PlaywrightError('Page.evaluate: Execution context was destroyed, most likely because of a navigation')
                return page.evaluate(expression,*args)
        probe=NavigatingPage()
        target=PublishTarget('wechat_channels',owner(),str(tmp_path/'profile'))
        try:
            if redirect:
                with pytest.raises(PlatformRejected) as error:AccountPreflight(target,seconds=2).verify_page(probe)
                assert error.value.status=='AUTH_REQUIRED' and '登录页面' in str(error.value)
                assert page.url.endswith('/login.html')
            else:AccountPreflight(target,seconds=2).verify_page(probe)
            assert probe.triggered
            assert page.locator('input[type=file]').count()==0 if redirect else page.locator('input[type=file]').evaluate('e=>e.files.length')==0
        finally:browser.close()


@pytest.mark.parametrize('state',sorted(BLOCKING|{'FAILED','FAILED1'}))
def test_scan_preserves_failure_reason_until_that_file_really_starts(state):
    previous={'status':state,'message':'原始原因','attempt_count':1,'updated_at':'2026-10-24T08:00:00+08:00'}
    assert fresh_result(previous)['status']==state
    assert fresh_result(previous)['message']=='原始原因'
    assert previous['status']==state


def _videos(directory,count=4):
    files=[]
    for index in range(count):
        path=directory/f'file-{index}.mp4';path.write_bytes(bytes([index+1])*3000)
        os.utime(path,(time.time()-10,time.time()-10));files.append(path)
    return files


def test_inherited_auth_block_is_persisted_with_actual_reason_for_each_file(tmp_path):
    files=_videos(tmp_path)
    account=owner();account.target_dir=str(tmp_path);account.auto_archive=False
    target=PublishTarget('wechat_channels',account,str(tmp_path/'profile'))
    adapter=MagicMock();adapter.last_outcome='AUTH_REQUIRED';adapter.supports_upload_boundary=True
    adapter.upload_single_video.return_value=(False,'视频号已打开登录二维码页面，请完成登录',{})
    factory=MagicMock(return_value=adapter)
    worker=MultiDeployWorker([target],root=tmp_path/'state',uploader_factory=factory,
                             file_paths=files,interval_seconds=0)
    worker.ledger=MultiTargetLedger(worker.root)
    records=[];worker.log.connect(records.append)
    for job in worker.plan_batch():worker.attempt(job,target)
    assert factory.call_count==1
    ledger=MultiTargetLedger(worker.root)
    for path in files:
        result=ledger.result(hashlib.sha256(path.read_bytes()).hexdigest(),target.key)
        assert result['status']=='AUTH_REQUIRED' and result['attempt_count']==0
        assert '二维码' in result['message']
    assert any('尚未上传文件' in text and '沿用本轮账号检查结果' in text and '需要登录' in text for text in records)
    assert all('原因=' in text for text in records if text.startswith('[失败诊断归档]'))
    worker.diagnostics.finish()


def test_manual_one_file_preflight_does_not_reset_other_files_auth_or_success(panel,tmp_path,monkeypatch):
    from src.app import channel_panel as ui
    files=_videos(tmp_path)
    targets=panel.all_targets()
    chosen=next(t for t in targets if t.platform=='wechat_channels')
    ledger=MultiTargetLedger(panel.store.root)
    for path in files:
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        for target in targets:
            ledger.set_result(digest,target.key,'AUTH_REQUIRED' if target.key==chosen.key else 'SUCCESS',
                '视频号需要扫码登录' if target.key==chosen.key else '已确认成功',attempt_count=0)
    for platform,check in panel.channel_checks.items():check.setChecked(platform=='wechat_channels')
    check=MagicMock();check.targets=[chosen];check.stopping=threading.Event()
    check.results=[PreflightResult(chosen.key,'视频号','AUTH_REQUIRED','已打开登录页面，需要登录')]
    monkeypatch.setattr(panel,'choose_targets',lambda:[chosen])
    monkeypatch.setattr(ui,'prerequisite_results',lambda *args:[])
    monkeypatch.setattr(ui,'DeploymentPreflightWorker',MagicMock(return_value=check))
    launch=MagicMock();monkeypatch.setattr(panel,'launch_deployment',launch)
    panel.start_deployment(file_paths=[files[2]]);drain(panel)
    check.start.assert_called_once()
    panel.preflight_finished()
    launch.assert_called_once_with([chosen],[files[2]])
    assert panel._preflight_blocks[chosen.key]['status']=='AUTH_REQUIRED'
    for path in files:
        results=panel.results[str(path.resolve())]['results']
        assert results[chosen.key]['status']=='AUTH_REQUIRED'
        assert all(r['status']=='SUCCESS' for k,r in results.items() if k!=chosen.key)
    table=next(p for p in panel.panels if p.platform=='wechat_channels').table_queue
    panel.render_results()
    assert table.rowCount()==4
    column=PUBLISH_ORDER.index('wechat_channels')+5
    assert all(table.item(row,column).text()=='需要登录' for row in range(4))


def test_old_report_only_auth_blocks_repaired_without_overwriting_manual_retry(tmp_path):
    files=_videos(tmp_path,2)
    account=owner();account.target_dir=str(tmp_path);account.auto_archive=False
    target=PublishTarget('wechat_channels',account,str(tmp_path/'profile'))
    root=tmp_path/'state';folder=root/'publish_reports';folder.mkdir(parents=True)
    rows=[{'file_name':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'results':{
        target.key:{'status':'AUTH_REQUIRED','message':'历史登录二维码页面','attempt_count':0}}} for p in files]
    (folder/'publish_task_old.json').write_text(json.dumps({'task_id':'old','files':rows}),encoding='utf-8')
    latest=json.loads(json.dumps(rows))
    latest[0]['results'][target.key].update(status='ACCOUNT_RESTRICTED',message='较新的账号受限原因')
    (folder/'publish_task_old_z.json').write_text(json.dumps({'task_id':'later','files':latest}),encoding='utf-8')
    ledger=MultiTargetLedger(root)
    ledger.set_result(rows[1]['sha256'],target.key,'PENDING','用户明确重试',manual_retry=True,attempt_count=0)
    scan=ScanCleanupWorker([target],[target],root,force=True);scan.run()
    assert not scan.error
    ledger=MultiTargetLedger(root)
    assert ledger.result(rows[0]['sha256'],target.key)['status']=='ACCOUNT_RESTRICTED'
    assert ledger.result(rows[0]['sha256'],target.key)['message']=='较新的账号受限原因'
    assert ledger.result(rows[1]['sha256'],target.key)['message']=='用户明确重试'


def test_cloud_and_legacy_cloud_events_never_enter_platform_log_and_read_position_stays(app):
    from src.app.publish_log_view import PublishLogView
    widget=PublishLogView(MagicMock(),'wechat_channels');widget.resize(800,300);widget.show()
    try:
        widget.append_event(make_event('视频号已打开登录二维码页面，需要登录',platform='wechat_channels',level='ERROR'))
        for index in range(80):
            widget.append_event(make_event('[云盘状态] 历史目录扫描 '+str(index),platform='wechat_channels',category='cloud'))
            widget.append_event(make_event('[云盘连接已核验] 旧版本日志 '+str(index),platform='wechat_channels'))
        assert '云盘' not in widget.views['wechat_channels'].toPlainText()
        assert '需要登录' in widget.views['wechat_channels'].toPlainText()
        assert '旧版本日志' in widget.views['common'].toPlainText()
        for index in range(100):widget.append_event(make_event('视频号流程 '+str(index),platform='wechat_channels'))
        widget.tabs.setCurrentIndex(4);app.processEvents()
        scroll=widget.views['wechat_channels'].verticalScrollBar();scroll.setValue(4)
        position=scroll.value()
        widget.append_event(make_event('新一条平台日志',platform='wechat_channels'))
        app.processEvents();assert scroll.value()==position
    finally:widget.close()
