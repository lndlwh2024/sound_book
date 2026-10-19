"""UI acceptance: cleanup is automatic, cancellable and does not block the UI."""
import os
import hashlib
import json
import threading
import time
from unittest.mock import MagicMock
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from src.app import channel_panel as ui
from src.publisher.platforms import ChannelAccountStore,CHANNELS
from src.publisher.douyin.config import DouyinAccountManager
from src.publisher.multi_deploy import PublishTarget,MultiTargetLedger
from src.publisher.preflight import PreflightResult
from src.publisher.upload_quota import DailyUploadQuota


@pytest.fixture
def panel(tmp_path,monkeypatch):
    # Keep exercising the hidden adapter when its presentation is restored;
    # current default visibility has separate coverage in publishing_options.
    monkeypatch.setattr(ui,'VISIBLE_CHANNELS',{k:CHANNELS[k] for k in ('douyin','xiaohongshu','kuaishou','youtube')})
    app=QApplication.instance() or QApplication([])
    manager=DouyinAccountManager(tmp_path/'douyin.json',tmp_path/'profiles')
    manager.add_account('A',target_dir=str(tmp_path))
    monkeypatch.setattr(ui,'DouyinAccountManager',lambda:manager)
    monkeypatch.setattr(ui.QMessageBox,'warning',MagicMock())
    store=ChannelAccountStore(tmp_path/'state')
    for platform in ('xiaohongshu','kuaishou','youtube'):
        account=store.add(platform,platform)
        account.target_dir=str(tmp_path)
    store.save()
    widget=ui.MultiPlatformPublisherPanel(store=store)
    widget.timer.stop()
    yield widget
    widget.worker=None
    widget.preflight_worker=None
    widget.prepare_shutdown()
    for worker in (widget.cleanup_worker,widget.snapshot_worker):
        if worker:worker.wait(5000)
    app.processEvents()
    widget.close()


def drain(panel):
    deadline=time.monotonic()+5
    while panel.cleanup_worker and time.monotonic()<deadline:
        QApplication.processEvents();time.sleep(.005)
    assert panel.cleanup_worker is None


def local_preflight(panel,monkeypatch):
    target=panel.all_targets()[0]
    monkeypatch.setattr(panel,'choose_targets',lambda:[target])
    monkeypatch.setattr(ui,'prerequisite_results',lambda *args:[])
    check=MagicMock()
    check.targets=[target];check.stopping=threading.Event()
    check.results=[PreflightResult(target.key,'A','SUCCESS','passed')]
    monkeypatch.setattr(ui,'DeploymentPreflightWorker',MagicMock(return_value=check))
    factory=MagicMock()
    factory.return_value.isRunning.return_value=False
    monkeypatch.setattr(ui,'MultiDeployWorker',factory)
    return check,factory


def select_channels(panel,*platforms):
    for key,check in panel.channel_checks.items():
        check.setChecked(key in platforms)


def test_completion_cleanup_history_is_restored_for_late_copy(panel,tmp_path):
    path=tmp_path/'late-copy.mp4';path.write_bytes(b'video'*500)
    target=panel.all_targets()[0]
    item={'file_path':str(path),'file_name':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'size_bytes':path.stat().st_size,'mtime_ns':path.stat().st_mtime_ns,'archived_path':None,
        'cleanup_only':True,'results':{target.key:{'status':'SUCCESS','message':'confirmed','attempts':[]}}}
    folder=panel.store.root/'publish_reports';folder.mkdir(exist_ok=True)
    (folder/'cleanup.json').write_text(json.dumps({'files':[],'completion_cleanup':[item]}),encoding='utf-8')
    panel.restore_results()
    assert panel.results[str(path)]['results'][target.key]['status']=='SUCCESS'
    assert panel.results[str(path)]['cleanup_only']


def test_manual_cleanup_from_unselected_view_archives_only_checked_accounts(panel,tmp_path):
    path=tmp_path/'clip.mp4';path.write_bytes(b'video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    path.with_suffix('.srt').write_text('subtitle')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    ledger=MultiTargetLedger(panel.store.root)
    for target in panel.all_targets():
        ledger.set_result(digest,target.key,'SUCCESS' if target.platform in ('xiaohongshu','kuaishou') else 'FAILED','history')
    select_channels(panel,'xiaohongshu','kuaishou')
    panel.store.archive_targets=[target.key for target in panel.all_targets()]
    panel.douyin.scan_button.click();drain(panel)
    assert not path.exists() and (tmp_path/'publiced'/'clip.mp4').exists()
    assert (tmp_path/'publiced'/'clip.srt').exists()
    result=MultiTargetLedger(panel.store.root)
    required=result.data['plans'][digest]['required']
    assert required==[target.key for target in panel.all_targets() if target.platform in ('xiaohongshu','kuaishou')]
    assert result.result(digest,'douyin:'+panel.douyin.current().account_id)['status']=='FAILED'
    assert result.result(digest,'youtube:'+panel.new_panels[2].current().account_id)['status']=='FAILED'
    assert '[清理范围]' in panel.douyin.logs.toPlainText()
    assert '归档1个' in panel.douyin.logs.toPlainText()
    assert panel.worker is None
    assert all(DailyUploadQuota(panel.store.root).snapshot(t.key)['used']==0 for t in panel.all_targets())


def test_manual_cleanup_waits_for_failed_selected_account_and_rechecks_changed_scope(panel,tmp_path):
    path=tmp_path/'clip.mp4';path.write_bytes(b'video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    digest=hashlib.sha256(path.read_bytes()).hexdigest();ledger=MultiTargetLedger(panel.store.root)
    for target in panel.all_targets():
        ledger.set_result(digest,target.key,'FAILED2' if target.platform=='xiaohongshu' else 'SUCCESS','history')
    select_channels(panel,'xiaohongshu','kuaishou')
    panel.douyin.scan_button.click();drain(panel)
    assert path.exists() and '归档0个' in panel.douyin.logs.toPlainText()
    select_channels(panel,'kuaishou')
    panel.douyin.scan_button.click();drain(panel)
    assert not path.exists() and '归档1个' in panel.douyin.logs.toPlainText()


def test_manual_cleanup_does_not_fall_back_to_all_accounts_when_none_selected(panel,tmp_path):
    path=tmp_path/'clip.mp4';path.write_bytes(b'video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    select_channels(panel)
    panel.douyin.scan_button.click();drain(panel)
    assert path.exists() and not (tmp_path/'publiced').exists()
    assert panel.worker is None and not (panel.store.root/'target_ledger.json').exists()
    assert '请至少勾选一个平台' in panel.douyin.logs.toPlainText()


def test_manual_cleanup_uses_current_selected_account_not_every_account(panel,tmp_path):
    path=tmp_path/'clip.mp4';path.write_bytes(b'video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    page=panel.new_panels[0];first=page.current()
    other=panel.repository.add('xiaohongshu','Other')
    other.target_dir=str(tmp_path);panel.repository.save(other)
    page.reload();page.accounts.setCurrentIndex(1)
    select_channels(panel,'xiaohongshu')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    MultiTargetLedger(panel.store.root).set_result(digest,'xiaohongshu:'+other.account_id,'SUCCESS','history')
    panel.douyin.scan_button.click();drain(panel)
    assert not path.exists()
    assert MultiTargetLedger(panel.store.root).data['plans'][digest]['required']==['xiaohongshu:'+other.account_id]
    assert not MultiTargetLedger(panel.store.root).result(digest,'xiaohongshu:'+first.account_id)


def test_manual_cleanup_rejects_disabled_selected_account_without_fallback(panel,tmp_path):
    path=tmp_path/'clip.mp4';path.write_bytes(b'video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    select_channels(panel,'xiaohongshu')
    panel.new_panels[0].enabled.setChecked(False)
    panel.douyin.scan_button.click();drain(panel)
    assert path.exists() and not (tmp_path/'publiced').exists()
    assert '请选择并启用本次清理的账号' in panel.douyin.logs.toPlainText()


def test_start_auto_cleans_before_preflight_and_reuses_valid_manual_scan(panel,tmp_path,monkeypatch):
    path=tmp_path/'clip.mp4';path.write_bytes(b'video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    check,factory=local_preflight(panel,monkeypatch)
    panel.douyin.scan_button.click();drain(panel)
    assert panel.cleanup_cache and '扫描清理完成' in panel.douyin.logs.toPlainText()
    panel.start_deployment();drain(panel)
    assert '沿用已完成的扫描清理' in panel.douyin.logs.toPlainText()
    check.start.assert_called_once();factory.assert_not_called()
    panel.preflight_finished();factory.return_value.start.assert_called_once()
    assert len(factory.call_args.kwargs['archive_targets'])==1
    assert len(factory.call_args.kwargs['history_targets'])==4


def test_direct_start_performs_cleanup_and_changed_directory_invalidates_cache(panel,tmp_path,monkeypatch):
    check,factory=local_preflight(panel,monkeypatch)
    panel.start_deployment();drain(panel)
    check.start.assert_called_once();factory.assert_not_called()
    panel.preflight_worker=None
    path=tmp_path/'new.mp4';path.write_bytes(b'new video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    check.start.reset_mock()
    panel.start_deployment();drain(panel)
    assert str(path.resolve()) in panel.results
    check.start.assert_called_once()
    assert panel.douyin.logs.toPlainText().count('[扫描清理完成]')==2


@pytest.mark.parametrize('platform',['xiaohongshu','kuaishou'])
def test_other_platform_cleanup_displays_old_douyin_success_without_upload(panel,tmp_path,platform):
    import json,hashlib
    path=tmp_path/'copy.mp4';path.write_bytes(b'video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    directory=tmp_path/'reports';directory.mkdir()
    original=json.dumps([{'account_id':panel.douyin.current().account_id,'status':'SUCCESS',
        'file_hash':hashlib.md5(path.read_bytes()).hexdigest()}])
    report=directory/'publish_ledger_20261006.json';report.write_text(original)
    page=next(p for p in panel.panels if p.platform==platform)
    select_channels(panel,platform)
    page.scan_button.click();drain(panel)
    page.scan_queue();panel.render_results()
    assert page.table_queue.item(0,5).text()=='已成功'
    assert panel.worker is None and path.exists() and report.read_text()==original
    assert DailyUploadQuota(panel.store.root).snapshot('douyin:'+panel.douyin.current().account_id)['used']==0


def test_cancel_slow_cleanup_keeps_ui_responsive_and_never_starts_preflight(panel,monkeypatch):
    check,factory=local_preflight(panel,monkeypatch)
    started=threading.Event()
    def slow(worker):
        started.set()
        worker.stopping.wait(3)
        worker.cancelled=True
    monkeypatch.setattr(ui.ScanCleanupWorker,'run',slow)
    beats=[];timer=QTimer();timer.timeout.connect(lambda:beats.append(1));timer.start(10)
    try:
        panel.start_deployment()
        assert started.wait(1)
        deadline=time.monotonic()+.15
        while time.monotonic()<deadline:
            QApplication.processEvents();time.sleep(.005)
        assert len(beats)>=5 and panel.busy()
        panel.start_deployment();drain(panel)
        check.start.assert_not_called();factory.assert_not_called()
        assert '扫描清理已取消' in panel.douyin.deploy_status.text()
    finally:timer.stop()


@pytest.mark.parametrize('index',range(4))
def test_account_limit_is_saved_reloaded_independent_and_live(panel,index):
    current=panel.panels[index];account=current.current()
    assert current.daily_limit.value()==10
    current.daily_limit.setValue(3)
    fake=MagicMock();panel.worker=fake
    assert current.save_current(feedback=True)
    fake.wake_requested.set.assert_called_once()
    assert panel.repository.accounts(current.platform)[0].daily_upload_limit==3
    quota=DailyUploadQuota(panel.store.root)
    key=current.platform+':'+account.account_id
    quota.reserve(key,'digest','task','unique-'+key)
    current.refresh_quota()
    assert '今日已用1' in current.quota_state.text()
    other=panel.repository.add(current.platform,'B')
    assert other.daily_upload_limit==10
    assert quota.snapshot(current.platform+':'+other.account_id)['used']==0
    current.daily_limit.setValue(5);assert current.save_current()
    assert quota.snapshot(key)['remaining']==4
    assert '已保存' in current.save_button.text()


def test_capped_accounts_skip_initial_browser_check_but_keep_deferred_check(panel,monkeypatch):
    check,factory=local_preflight(panel,monkeypatch)
    panel.douyin.daily_limit.setValue(0)
    panel.start_deployment();drain(panel)
    check.start.assert_not_called();factory.return_value.start.assert_called_once()
    assert factory.call_args.kwargs['deferred_preflight']=={panel.all_targets()[0].key}
