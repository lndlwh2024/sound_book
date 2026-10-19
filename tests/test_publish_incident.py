"""Regressions reproduced from the 2026-10-18 upload incident."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock
import pytest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from playwright.sync_api import sync_playwright
from src.publisher.google_drive import DriveClient
from src.publisher.event_log import PublishEventLog, display_event
from src.publisher.multi_deploy import MultiTargetLedger,MultiDeployWorker,PublishTarget
from src.publisher.platforms import ChannelAccount


def test_all_same_day_folders_are_read_without_remote_mutation():
    client=DriveClient(None)
    calls=[]
    def children(parent,folders_only=False):
        calls.append((parent,folders_only))
        return {'series':[{'id':'older','name':'2026-10-18'},{'id':'newer','name':'2026-10-18'},
                          {'id':'yesterday','name':'2026-10-17'}],
                'older':[{'id':'a','name':'a.mp4'}],
                'newer':[{'id':'a','name':'a.mp4'},{'id':'b','name':'b.mp4'}]}[parent]
    client.children=children
    folder,files=client.today_files('series','2026-10-18')
    assert folder['existing_folder_ids']==['older','newer']
    assert [f['id'] for f in files]==['a','b']
    assert calls==[('series',True),('older',False),('newer',False)]


def test_empty_first_date_folder_does_not_hide_other_folder():
    client=DriveClient(None)
    client.children=lambda parent,folders_only=False: (
        [{'id':'empty','name':'2026-10-18'},{'id':'populated','name':'2026-10-18'}] if parent=='series'
        else [] if parent=='empty' else [{'id':'video','name':'video.mp4'}])
    assert client.today_files('series','2026-10-18')[1]==[{'id':'video','name':'video.mp4'}]


def test_failed2_never_opens_reconcile_or_upload_on_scan_rename_or_restart(tmp_path):
    app=QApplication.instance() or QApplication([])
    media=tmp_path/'media';media.mkdir()
    video=media/'video.mp4';video.write_bytes(b'x'*2000);os.utime(video,(time.time()-10,)*2)
    target=PublishTarget('youtube',ChannelAccount('a'*32,'youtube','one',target_dir=str(media)),'')
    calls=[]
    class Uploader:
        def upload_single_video(self,path):
            calls.append(path)
            self.last_outcome='FAILED'
            return False,'pre-submit failure',None
    first=MultiDeployWorker([target],root=tmp_path/'state',uploader_factory=lambda *args:Uploader(),interval_seconds=0)
    first.ledger=MultiTargetLedger(first.root)
    first.run_batch(first.plan_batch())
    assert len(calls)==2
    for _ in range(3):first.run_batch(first.plan_batch())
    video.rename(media/'same-content.mp4')
    factory=MagicMock(side_effect=AssertionError('must not open any browser'))
    second=MultiDeployWorker([target],root=first.root,uploader_factory=factory,interval_seconds=0)
    second.ledger=MultiTargetLedger(second.root)
    second.run_batch(second.plan_batch())
    factory.assert_not_called()
    assert len(calls)==2
    result=second.report['files'][0]['results'][target.key]
    assert result['status']=='FAILED2' and result['attempt_count']==2


def test_full_flow_log_is_persistent_filtered_redacted_and_timestamped(tmp_path):
    log=PublishEventLog(tmp_path)
    event=log.write('正在下载 file.mp4 https://example.com/path?access_token=SECRET',
        timestamp='2026-10-18T04:14:07+00:00',platform='youtube',account_id='id',file_name='file.mp4',task_id='task')
    assert event['time']=='2026年10月18日12:14:07'
    assert event['platform']=='youtube' and event['file_name']=='file.mp4'
    assert 'SECRET' not in log.path.read_text(encoding='utf-8')
    assert json.loads(log.path.read_text(encoding='utf-8'))==event
    assert '[2026年10月18日12:14:07]' in display_event(event)
    from src.publisher.event_log import redact
    assert 'ERR_CONNECTION_CLOSED' in redact('请求失败 https://example.com/image?token=secret：ERR_CONNECTION_CLOSED')
    assert 'secret' not in redact('请求失败 https://example.com/image?token=secret：ERR_CONNECTION_CLOSED')


@pytest.mark.parametrize('scenario',['auth_before_transfer','checkpoint_then_crash','transfer_without_id'])
def test_interruption_preserves_retry_budget_and_draft_checkpoint(tmp_path,scenario):
    app=QApplication.instance() or QApplication([])
    media=tmp_path/'media';media.mkdir()
    video=media/'video.mp4';video.write_bytes(b'x'*2000);os.utime(video,(time.time()-10,)*2)
    target=PublishTarget('youtube',ChannelAccount('a'*32,'youtube','one',target_dir=str(media)),'')
    calls=[]
    draft={'video_id':'abcdefghijk','channel_id':'UC'+'a'*22,'draft':True}
    class Uploader:
        supports_upload_boundary=True
        def upload_single_video(self,path):
            calls.append(path)
            if scenario=='auth_before_transfer':
                self.last_outcome='AUTH_REQUIRED'
                return False,'login expired',None
            self.before_upload()
            if scenario=='checkpoint_then_crash':self.on_draft_identified(draft)
            raise RuntimeError('page unexpectedly closed https://example.com/upload?access_token=SECRET')
    worker=MultiDeployWorker([target],root=tmp_path/'state',uploader_factory=lambda *args:Uploader(),interval_seconds=0)
    worker.ledger=MultiTargetLedger(worker.root)
    jobs=worker.plan_batch();worker.attempt(jobs[0],target)
    result=worker.ledger.result(jobs[0]['digest'],target.key)
    assert 'SECRET' not in worker.ledger.path.read_text(encoding='utf-8')
    if scenario=='auth_before_transfer':
        assert result['status']=='AUTH_REQUIRED' and result['attempt_count']==0
    elif scenario=='checkpoint_then_crash':
        assert result['info']['video_id']==draft['video_id']
        assert result['status']=='FAILED1' and result['requires_review'] is False
    else:
        assert result['status']=='RECOVERY_REQUIRED' and result['requires_review'] is True
        assert 'page unexpectedly closed' in result['message']
        worker.attempt(jobs[0],target)
        assert len(calls)==1


def test_log_appends_concurrent_events_without_loss_or_partial_json(tmp_path):
    log=PublishEventLog(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda n:log.write('阶段'+str(n),platform='douyin'),range(80)))
    events=log.tail(100)
    assert len(events)==80 and len({e['id'] for e in events})==80


def test_log_rotates_instead_of_discarding_task_stages(tmp_path):
    log=PublishEventLog(tmp_path,maximum=500,backups=2)
    for number in range(7):log.write('阶段'+str(number)+'x'*80,platform='youtube')
    assert log.path.exists() and (log.folder/'publisher-events.1.jsonl').exists()
    assert len(list(log.folder.glob('publisher-events*.jsonl')))<=3


def test_douyin_slow_dom_load_does_not_fail_after_ten_seconds(tmp_path):
    from src.publisher.douyin.uploader import DouyinUploader
    from src.publisher.douyin.models import DouyinAccountConfig
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    uploader=DouyinUploader(DouyinAccountConfig('a','one',profile_dir=str(tmp_path/'profile')))
    page=MagicMock()
    page.url='https://creator.douyin.com/creator-micro/content/upload'
    page.goto.side_effect=PlaywrightTimeoutError('slow analytics request')
    page.locator.return_value.count.return_value=1
    page.locator.return_value.nth.return_value.get_attribute.return_value='video/*'
    uploader._wait_until=MagicMock(side_effect=lambda p,label,condition,seconds:condition())
    uploader._navigate_upload_page(page)
    assert page.goto.call_args.kwargs=={'wait_until':'commit','timeout':45000}
    assert uploader._wait_until.call_args.kwargs['seconds']==90


def test_douyin_navigation_to_wrong_site_never_selects_a_file(tmp_path):
    from src.publisher.douyin.uploader import DouyinUploader
    from src.publisher.douyin.models import DouyinAccountConfig
    uploader=DouyinUploader(DouyinAccountConfig('a','one',profile_dir=str(tmp_path/'profile')))
    page=MagicMock();page.url='https://example.com/'
    with pytest.raises(RuntimeError,match='未选择文件'):uploader._navigate_upload_page(page)
    page.locator.assert_not_called()


def test_platform_tabs_are_below_log_and_do_not_mix_platforms(tmp_path):
    from src.app.publish_log_view import PublishLogView
    from src.publisher.event_log import make_event
    app=QApplication.instance() or QApplication([])
    controller=MagicMock()
    widget=PublishLogView(controller,'douyin')
    widget.resize(800,320);widget.show();app.processEvents()
    widget.append_event(make_event('抖音独立流程',platform='douyin'))
    widget.append_event(make_event('YouTube独立流程',platform='youtube'))
    assert 'YouTube' not in widget.views['douyin'].toPlainText()
    assert '抖音' not in widget.views['youtube'].toPlainText()
    assert widget.tabs.geometry().top()>widget.stack.geometry().bottom()
    widget.tabs.setCurrentIndex(5);app.processEvents()
    assert widget.stack.currentWidget() is widget.views['youtube']
    widget.close()


def test_manual_retry_resets_only_confirmed_failed_target_and_keeps_draft(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from src.app.channel_panel import MultiPlatformPublisherPanel
    from src.publisher.platforms import ChannelAccountStore
    from src.publisher.douyin.config import DouyinAccountManager
    import hashlib
    app=QApplication.instance() or QApplication([])
    media=tmp_path/'media';media.mkdir();video=media/'video.mp4';video.write_bytes(b'video')
    store=ChannelAccountStore(tmp_path/'state')
    account=store.add('youtube','one');account.target_dir=str(media);store.save()
    panel=MultiPlatformPublisherPanel(store=store,douyin_manager=DouyinAccountManager(tmp_path/'douyin.json',tmp_path/'profiles'))
    target=PublishTarget('youtube',account,'');digest=hashlib.sha256(video.read_bytes()).hexdigest()
    info={'video_id':'abcdefghijk','channel_id':'UC'+'a'*22}
    MultiTargetLedger(store.root).set_result(digest,target.key,'FAILED2','thumbnail failed',info,attempt_count=2)
    try:
        monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.No)
        assert not panel.confirm_retry_budget([target],[video])
        assert MultiTargetLedger(store.root).result(digest,target.key)['attempt_count']==2
        monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.Yes)
        assert panel.confirm_retry_budget([target],[video])
        result=MultiTargetLedger(store.root).result(digest,target.key)
        assert result['attempt_count']==0 and result['info']['video_id']=='abcdefghijk'
        assert result['info']['draft'] is True and result['manual_retry'] is True
        assert video.read_bytes()==b'video'
    finally:
        panel.prepare_shutdown();panel.close();panel.deleteLater();app.processEvents()


def test_actual_named_frame_dialog_is_found_without_old_custom_tag(tmp_path):
    from src.publisher.youtube import YouTubeUploader
    from tests.test_youtube_publisher import account,native_thumbnail_html
    uploader=YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    uploader.account.thumbnail_mode='video_frame'
    html=native_thumbnail_html().replace('ytcp-video-autogenerated-thumbnails-dialog','section')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page();page.set_content('<ytcp-uploads-dialog><div role="dialog">'+html+'</div></ytcp-uploads-dialog>')
            uploader.set_thumbnail(page,tmp_path/'video.mp4')
            assert page.evaluate('window.frameSaved') is True
            assert not page.evaluate('Boolean(window.forbiddenUpload)')
        finally:browser.close()


def test_retry_resumes_saved_youtube_draft_without_reselecting_file(tmp_path,monkeypatch):
    from src.publisher import youtube as module
    from tests.test_youtube_publisher import account,HTML,CHANNEL_ID,VIDEO_ID
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports',headless=True)
    uploader.resume_info={'video_id':VIDEO_ID,'channel_id':CHANNEL_ID,'title':'一分钟趣味知识','is_short':True,'draft':True}
    uploader.before_upload=MagicMock(side_effect=AssertionError('must not create another draft'))
    uploader._close_publish_context=lambda context,*args:context.close()
    callbacks=[]
    uploader.on_draft_identified=callbacks.append
    video=tmp_path/'一分钟趣味知识_1.mp4';video.write_bytes(b'local')
    with sync_playwright() as playwright:
        manager=MagicMock();manager.__enter__.return_value=playwright
        monkeypatch.setattr(module,'sync_playwright',lambda:manager)
        launch=playwright.chromium.launch_persistent_context
        def context(*args,**kwargs):
            browser=launch(*args,**kwargs)
            def route(request):
                if '/videos' in request.request.url:
                    html=f'<meta charset="utf-8"><ytcp-video-row><a href="/video/{VIDEO_ID}/edit" id="title"></a><span>公开</span></ytcp-video-row><script>document.querySelector("#title").textContent=localStorage.getItem("published")||"未发布"</script>'
                else:
                    html=HTML+f'<script>history.replaceState(null,"","/channel/{CHANNEL_ID}")</script>'
                    if '/video/' in request.request.url:
                        html += '<script>document.querySelector("#editor").style.display="block";document.querySelector("#progress").remove();document.querySelector("#status").textContent="检查完毕。未发现任何问题。"</script>'
                request.fulfill(body=html,content_type='text/html')
            browser.route('**/*',route)
            return browser
        monkeypatch.setattr(playwright.chromium,'launch_persistent_context',context)
        success,message,info=uploader.upload_single_video(video)
    assert success,message
    uploader.before_upload.assert_not_called()
    assert info['video_id']==VIDEO_ID and len(callbacks)==1
