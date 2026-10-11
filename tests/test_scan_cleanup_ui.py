"""UI acceptance: cleanup is automatic, cancellable and does not block the UI."""
import os
import threading
import time
from unittest.mock import MagicMock
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from src.app import channel_panel as ui
from src.publisher.platforms import ChannelAccountStore
from src.publisher.douyin.config import DouyinAccountManager
from src.publisher.multi_deploy import PublishTarget
from src.publisher.preflight import PreflightResult
from src.publisher.upload_quota import DailyUploadQuota


@pytest.fixture
def panel(tmp_path,monkeypatch):
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
    assert len(factory.call_args.kwargs['archive_targets'])==4


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
