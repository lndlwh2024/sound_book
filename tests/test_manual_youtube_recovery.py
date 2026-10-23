"""Native-frame retry and finite, deduplicated manual publishing regressions."""
import hashlib
import json
import os
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from playwright.sync_api import sync_playwright,Locator,TimeoutError as PlaywrightTimeoutError
from PySide6.QtWidgets import QApplication
from src.publisher.youtube import YouTubeUploader
from src.publisher.multi_deploy import MultiDeployWorker,MultiTargetLedger
from tests.test_youtube_publisher import account,native_thumbnail_html,DEFAULT_DETAILS
from tests.test_scan_cleanup_ui import panel
from tests.test_publisher_portable import window,app
from tests.test_multi_deploy import setup


@pytest.mark.parametrize('loses_selection',[False,True])
def test_already_selected_native_frame_with_disabled_done_is_verified_by_reopening(tmp_path,loses_selection):
    logs=[]
    uploader=YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports',progress_callback=logs.append)
    uploader.account.thumbnail_mode='video_frame'
    html=native_thumbnail_html().replace('el.disabled=false',
        "el.disabled=el.getAttribute('aria-label')==='自动生成的缩略图 1'")
    html=html.replace('<button>取消</button>', '''<button onclick="
        window.cancelCount=(window.cancelCount||0)+1;
        document.querySelector('#frame-dialog').style.display='none';
        if(window.loseSelection)document.querySelectorAll('[role=option]')[1].setAttribute('aria-selected','false');
        ">取消</button>''')
    html += '<script>document.querySelectorAll("[role=option]")[1].setAttribute("aria-selected","true");window.loseSelection='+str(loses_selection).lower()+';</script>'
    if loses_selection:
        original=uploader.wait
        uploader.wait=lambda p,label,condition,seconds=60:original(p,label,condition,min(seconds,1))
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('<ytcp-uploads-dialog><div role="dialog">'+html+'</div></ytcp-uploads-dialog>')
            if loses_selection:
                with pytest.raises(RuntimeError,match='核验平台保留'):uploader.set_thumbnail(page,tmp_path/'video.mp4')
            else:
                uploader.set_thumbnail(page,tmp_path/'video.mp4')
                assert page.evaluate('window.cancelCount')==2
                assert not page.evaluate('Boolean(window.frameSaved||window.frameClicks||window.wrongFrame||window.forbiddenUpload)')
                assert any('核验平台保留原视频帧' in message for message in logs)
        finally:browser.close()


def test_advanced_settings_expansion_is_idempotent_after_click_timeout_and_ignores_background(tmp_path,monkeypatch):
    uploader=YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    original=Locator.click
    clicked=[]
    def transient(locator,*args,**kwargs):
        if locator.inner_text().strip()=='展开':
            clicked.append(True)
            original(locator,*args,**kwargs)
            raise PlaywrightTimeoutError('click took effect before the actionability timeout')
        return original(locator,*args,**kwargs)
    monkeypatch.setattr(Locator,'click',transient)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('<button onclick="window.wrongBackground=true">展开</button>'+
                '<ytcp-uploads-dialog><div role="dialog">'+DEFAULT_DETAILS+'</div></ytcp-uploads-dialog>')
            uploader.apply_default_details(page,uploader.dialog(page))
            assert len(clicked)==1 and not page.evaluate('Boolean(window.wrongBackground)')
            assert page.get_by_role('radio',name='否，我的视频不包含付费宣传内容').get_attribute('aria-checked')=='true'
        finally:browser.close()


@pytest.mark.parametrize('archived',[False,True])
def test_manual_all_succeeded_skips_browser_and_ends_deployment(panel,tmp_path,monkeypatch,archived):
    from src.app import channel_panel as ui
    media=tmp_path/'clip.mp4';media.write_bytes(b'video')
    digest=hashlib.sha256(media.read_bytes()).hexdigest();targets=panel.all_targets()
    ledger=MultiTargetLedger(panel.store.root)
    for target in targets:ledger.set_result(digest,target.key,'SUCCESS','confirmed')
    if archived:
        destination=media.with_suffix('.archived');media.rename(destination)
        panel.results[str(media.resolve())]={'sha256':digest,'archived_path':str(destination)}
    before=ledger.path.read_bytes()
    preflight=MagicMock(side_effect=AssertionError('must not open successful accounts'))
    monkeypatch.setattr(ui,'DeploymentPreflightWorker',preflight)
    launch=MagicMock();monkeypatch.setattr(panel,'launch_deployment',launch)
    panel.begin_preflight(targets,[media])
    preflight.assert_not_called()
    assert panel.worker is None and panel.preflight_worker is None
    if archived:
        launch.assert_not_called()
        assert '部署已自动停止' in panel.panels[0].deploy_status.text()
    else:
        launch.assert_called_once_with(targets,[media])
    assert ledger.path.read_bytes()==before


def test_manual_preflight_checks_only_the_still_pending_platform(panel,tmp_path,monkeypatch):
    from src.app import channel_panel as ui
    media=tmp_path/'clip.mp4';media.write_bytes(b'video')
    digest=hashlib.sha256(media.read_bytes()).hexdigest();targets=panel.all_targets()
    ledger=MultiTargetLedger(panel.store.root)
    pending=next(t for t in targets if t.platform=='youtube')
    for target in targets:
        if target!=pending:ledger.set_result(digest,target.key,'SUCCESS','confirmed')
    monkeypatch.setattr(ui,'prerequisite_results',lambda *args:[])
    preflight=MagicMock();monkeypatch.setattr(ui,'DeploymentPreflightWorker',preflight)
    panel.begin_preflight(targets,[media])
    assert preflight.call_args.args[0]==[pending]
    assert panel._all_deployment_targets==targets


def test_manual_retries_only_unfinished_target_and_automatically_finishes(tmp_path):
    directory,targets=setup(tmp_path)
    for target in targets:target.account.auto_archive=False
    media=directory/'a.mp4';digest=hashlib.sha256(media.read_bytes()).hexdigest()
    ledger=MultiTargetLedger(tmp_path/'state')
    ledger.set_result(digest,targets[0].key,'SUCCESS','already published',{'receipt':'kept'})
    calls=[]
    def factory(target,log):
        assert target.key!=targets[0].key
        def upload(path):calls.append(target.key);return True,'confirmed',None
        return MagicMock(upload_single_video=upload)
    job=MultiDeployWorker(targets,root=tmp_path/'state',uploader_factory=factory,
        interval_seconds=0,file_paths=[media])
    job.controlled_wait=MagicMock()
    job.run()
    assert calls==[targets[1].key]
    assert job.report['status']=='COMPLETED' and job.report['all_selected_published']
    assert not job.report['all_selected_completed'] and job.report['archive_pending_count']==1
    assert job.report['task_type']=='MANUAL' and job.report['selected_files']==[str(media.resolve())]
    assert all(c.args[0]==0 for c in job.controlled_wait.call_args_list)
    assert job.ledger.result(digest,targets[0].key)['info']=={'receipt':'kept'}
    media.rename(directory/'renamed.mp4')
    repeated=MultiDeployWorker(targets,root=tmp_path/'state',interval_seconds=0,
        uploader_factory=MagicMock(side_effect=AssertionError('success must not upload again')),
        file_paths=[directory/'renamed.mp4'])
    repeated.run()
    assert repeated.report['status']=='COMPLETED' and repeated.report['success_count']==len(targets)


def test_publisher_manual_completion_clears_persistent_auto_restore_intent(window):
    panel=window.panel
    panel.state.intent='RUNNING';panel.state.auto_deploy=True
    panel.restore_timer.start(10000)
    panel.worker=MagicMock(file_paths={'clip.mp4'},report={'all_selected_completed':True,'success_count':1})
    panel.deployment_finished()
    assert panel.worker is None and panel.state.intent=='STOPPED' and not panel.restore_timer.isActive()
    assert json.loads(panel.state_path.read_text(encoding='utf-8'))['intent']=='STOPPED'
    assert '部署已自动停止' in panel.panels[0].deploy_status.text()


def test_manual_pause_or_resume_never_turns_into_continuous_restore(window):
    panel=window.panel
    panel.worker=MagicMock(file_paths={'clip.mp4'})
    for paused in (False,True):
        panel.worker.pause_requested.is_set.return_value=paused
        panel.toggle_pause()
        assert panel.state.intent=='STOPPED'
    panel.worker=None
