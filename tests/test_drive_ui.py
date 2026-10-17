"""Confirmed Qt controls, persisted bindings and cancellable background polling."""
import threading
import time
from dataclasses import replace
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from tests.test_common_platform_accounts import panel
from tests.test_google_drive import FakeDrive, target, cloud_file
from src.publisher.drive_ingest import DriveWatchWorker, DriveIngestor, CHINA_TIME
from src.publisher.platforms import ChannelAccountStore
from src.publisher.account_repository import PlatformAccountRepository
from src.publisher.douyin.config import DouyinAccountManager
from src.publisher.multi_deploy import MultiTargetLedger, MultiDeployWorker
from datetime import datetime


def test_connection_actions_follow_confirmed_state_and_share_monitoring_row():
    from src.app.drive_widgets import DriveBindingWidget
    app=QApplication.instance() or QApplication([])
    widget=DriveBindingWidget()
    widget.resize(620,200)
    widget.show()
    try:
        app.processEvents()
        assert widget.title()=='云盘绑定' and widget.watch.text()=='启用云盘监听'
        assert widget.watch.geometry().center().y()==widget.connection_state.geometry().center().y()
        widget.update_connection(False,provider_name='示例云盘')
        assert widget.connected_button.isEnabled() and not widget.disconnected_button.isEnabled()
        assert not widget.connected_button.isCheckable() and not widget.disconnected_button.isCheckable()
        assert widget.connection_state.text()=='示例云盘 · 已断开'
        widget.update_connection(False,pending=True)
        assert not widget.connected_button.isEnabled() and not widget.disconnected_button.isEnabled()
        assert not widget.is_connected
        widget.update_connection(True)
        assert not widget.connected_button.isEnabled() and widget.disconnected_button.isEnabled()
        assert widget.change_button.isEnabled()
        widget.update_connection(False)
        assert widget.connected_button.isEnabled() and not widget.disconnected_button.isEnabled()
    finally:
        widget.close()


@pytest.mark.parametrize('connected,pending,connect_enabled,disconnect_enabled',[
    (False,False,True,False), (True,False,False,True),
    (False,True,False,False), (True,True,False,False)])
def test_shared_connection_buttons_are_exclusive_and_disabled_looks_inactive(
        connected,pending,connect_enabled,disconnect_enabled):
    from src.app.drive_widgets import SharedDriveWidget
    from PySide6.QtGui import QPalette
    app=QApplication.instance() or QApplication([])
    widget=SharedDriveWidget()
    widget.update_connection(connected,pending,True)
    widget.show()
    try:
        app.processEvents()
        assert widget.connected_button.isEnabled() == connect_enabled
        assert widget.disconnected_button.isEnabled() == disconnect_enabled
        if not pending:
            active=widget.disconnected_button if connected else widget.connected_button
            inactive=widget.connected_button if connected else widget.disconnected_button
            assert active.palette().color(QPalette.Button) != inactive.palette().color(QPalette.Button)
    finally:
        widget.close()


def test_folder_dialog_has_readable_colors_under_light_system_theme(monkeypatch):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QDialogButtonBox
    from src.app.drive_widgets import DriveFolderDialog
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(DriveFolderDialog, 'load', lambda *_: None)
    dialog = DriveFolderDialog(None)
    dialog.listing.addItem('趣味知识')
    dialog.show()
    try:
        app.processEvents()
        palette = dialog.listing.palette()
        background = palette.color(QPalette.Base)
        foreground = palette.color(QPalette.Text)
        assert background.lightness() < 70
        assert foreground.lightness() > 180
        assert dialog.message.palette().color(QPalette.WindowText).lightness() > 180
        assert dialog.buttons.button(QDialogButtonBox.Cancel).text() == '取消'
    finally:
        dialog.close()


def test_reconnection_clears_stale_disconnected_directory_status():
    from types import SimpleNamespace
    from src.app.drive_widgets import DriveBindingWidget
    from src.app.channel_panel import MultiPlatformPublisherPanel
    app = QApplication.instance() or QApplication([])
    widget = DriveBindingWidget()
    widget.today.setText('云盘已断开；目录配置保留')
    from src.app.drive_widgets import SharedDriveWidget
    panel = SimpleNamespace(drive=widget, shared_drive=SharedDriveWidget(), refresh_cloud_badges=lambda: None)
    controller = SimpleNamespace(
        store=SimpleNamespace(cloud_ingest_enabled=True),
        drive_credentials=SimpleNamespace(connected=lambda: True),
        panels=[panel], drive_auth_worker=None)
    try:
        MultiPlatformPublisherPanel.update_drive_connection(controller)
        assert widget.is_connected
        assert widget.today.text() == '今日目录：等待检查'
        widget.today.setText('今日：2026-10-17 已找到 · 检查完成')
        MultiPlatformPublisherPanel.update_drive_connection(controller)
        assert '已找到' in widget.today.text()
    finally:
        widget.close()


def test_shared_cloud_switch_persists_and_preserves_account_bindings(panel,monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setattr(panel.drive_credentials,'connected',lambda:True)
    watch=MagicMock()
    panel.drive_watch_worker=watch
    current=panel.douyin
    current.drive.watch.setChecked(True)
    current.drive.folder_id='shared-series'
    current.drive.folder.setText('生产/趣味知识')
    assert current.save_current()
    saved=dict(current.current().drive_binding)
    panel.update_drive_connection()
    panel.set_cloud_ingest_enabled(False)
    assert not ChannelAccountStore(panel.store.root).cloud_ingest_enabled
    assert current.current().drive_binding == saved
    assert watch.configure.call_args.args[0] == []
    for view in panel.panels:
        assert not view.shared_drive.enabled.isChecked()
        assert not view.drive.check.isEnabled()
        assert not view.drive.choose.isEnabled()
        assert '已连接' in view.shared_drive.connection_state.text()
    panel.check_drive_now(current)
    watch.check_now.assert_not_called()
    panel.set_cloud_ingest_enabled(True)
    assert ChannelAccountStore(panel.store.root).cloud_ingest_enabled
    assert current.drive.check.isEnabled()
    assert current.current().drive_binding == saved
    panel.drive_watch_worker=None


@pytest.mark.parametrize('index',range(4))
def test_shared_cloud_controls_stay_above_account_tabs(panel,index):
    from PySide6.QtWidgets import QScrollArea
    panel.channel_view.setCurrentIndex(index)
    panel.resize(1280,800)
    panel.show()
    view=panel.panels[index]
    for tab in (0,1):
        view.settings_tabs.setCurrentIndex(tab)
        QApplication.processEvents()
        assert view.shared_drive.isVisible()
        assert view.shared_drive.geometry().bottom() < view.account_group.geometry().top()
        assert view.account_group.geometry().bottom() < view.settings_tabs.geometry().top()
        assert not view.settings_tabs.isAncestorOf(view.shared_drive)
        assert view.shared_drive.title()=='公共配置-跨平台云盘绑定'
        assert view.settings_tabs.tabText(0)=='目录绑定'
        assert view.settings_tabs.tabText(1)=='部署配置'
        assert not view.shared_drive.connection_state.wordWrap()
        assert view.shared_drive.change_button.y() == view.shared_drive.disconnected_button.y()
        controls = (view.shared_drive.enabled,view.shared_drive.change_button,view.shared_drive.connected_button,view.shared_drive.disconnected_button)
        assert len({control.height() for control in controls}) == 1
        assert view.shared_drive.connection_state.y() < view.shared_drive.enabled.y()
        assert panel.rect().contains(view.save_button.mapTo(panel,view.save_button.rect().center()))
        assert view.findChild(QScrollArea) is None
    assert view.drive.connected_button.isHidden()


def pump_until(condition, seconds=3):
    deadline=time.monotonic()+seconds
    while not condition() and time.monotonic()<deadline:
        QApplication.processEvents()
        time.sleep(.005)
    assert condition()


@pytest.mark.parametrize('index',range(4))
def test_account_selector_and_status_fit_without_list_rows(panel,index):
    from PySide6.QtWidgets import QComboBox
    panel.channel_view.setCurrentIndex(index)
    panel.resize(1280,800)
    panel.show()
    view=panel.panels[index]
    QApplication.processEvents()
    assert isinstance(view.accounts,QComboBox)
    assert view.accounts.currentData()==view.current().account_id
    assert view.account_status.isVisible()
    assert view.accounts.mapTo(view.account_selector_group,view.accounts.rect().bottomLeft()).y()<view.account_status.y()
    assert view.accounts.height()<=56


@pytest.mark.parametrize('connected',[False,True])
def test_authorization_dialog_has_explicit_readable_colors_and_cancel_keeps_connection(panel,monkeypatch,connected):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QLabel
    from src.app import drive_widgets
    class FakeAuthWorker(QObject):
        open_url=Signal(str)
        error=Signal(str)
        success=Signal()
        finished=Signal()
        def __init__(self,credentials,parent):super().__init__(parent)
        def start(self):pass
        def stop(self):pass
        def isRunning(self):return False
    monkeypatch.setattr(drive_widgets,'DriveAuthWorker',FakeAuthWorker)
    monkeypatch.setattr(panel.drive_credentials,'client',lambda:{})
    monkeypatch.setattr(panel.drive_credentials,'connected',lambda:connected)
    panel.connect_drive(panel.panels[0])
    dialog=panel.drive_auth_dialog
    QApplication.processEvents()
    assert dialog.palette().color(QPalette.Window).name()=='#222a36'
    assert dialog.findChild(QLabel).palette().color(QPalette.WindowText).name()=='#e4edf8'
    assert ('更换' in dialog.windowTitle())==connected
    dialog.reject()
    panel.drive_auth_worker.finished.emit()
    assert panel.drive_credentials.connected()==connected


@pytest.mark.parametrize('index',range(4))
def test_binding_persists_without_changing_identity_and_no_nickname_editor(panel,index):
    view=panel.panels[index];account=view.current();identity=account.account_id
    profile=panel.repository.profile(account)
    view.drive.watch.setChecked(True)
    view.drive.folder_id='series_'+str(index)
    view.drive.folder.setText('书声 / 分类'+str(index))
    view.drive.interval.setValue(25)
    assert view.save_current()
    repo=PlatformAccountRepository(ChannelAccountStore(panel.store.root),DouyinAccountManager(
        panel.repository.douyin_manager.config_file,panel.repository.douyin_manager.profiles_base_dir))
    restored=repo.accounts(view.platform)[0]
    assert restored.drive_binding==view.drive.value()
    assert restored.account_id==identity and repo.profile(restored)==profile
    assert view.name.isHidden() and view.nickname.isHidden()
    assert not view.configuration_group.isAncestorOf(view.save_button)
    view.disable_button.click()
    assert not view.current().enabled
    assert not panel.repository.accounts(view.platform)[0].enabled
    view.enable_button.click()
    assert view.current().enabled


def test_disconnect_keeps_all_bindings_local_files_and_local_deployment(panel,tmp_path,monkeypatch):
    from unittest.mock import MagicMock
    view=panel.douyin
    local=tmp_path/'clip.mp4';local.write_bytes(b'fixture'*300)
    view.drive.watch.setChecked(True);view.drive.folder_id='series';view.drive.folder.setText('书声/知识')
    assert view.save_current()
    fake=MagicMock();fake.isRunning.return_value=True;fake.targets=[]
    fake.stopping=threading.Event();fake.pause_requested=threading.Event()
    panel.worker=fake
    panel.disconnect_drive()
    assert local.exists() and view.current().drive_binding['folder_id']=='series'
    assert not view.drive.choose.isEnabled() and not view.drive.check.isEnabled()
    fake.stop.assert_not_called()
    panel.worker=None


def test_background_poll_keeps_gui_responsive_and_pause_prevents_new_downloads(tmp_path):
    app=QApplication.instance() or QApplication([])
    t=target(tmp_path);api=FakeDrive()
    class Credentials:
        generation=0
        def connected(self):return True
    ready=[];started=threading.Event();release=threading.Event()
    original=api.today_files
    def slow(*args):
        started.set();release.wait(2)
        return original(*args)
    api.today_files=slow
    worker=DriveWatchWorker(tmp_path/'state',Credentials(),client=api)
    worker.file_ready.connect(ready.append)
    worker.configure([t],paused=False)
    beats=[];timer=QTimer();timer.setInterval(10);timer.timeout.connect(lambda:beats.append(1));timer.start()
    worker.start()
    try:
        pump_until(started.is_set)
        pump_until(lambda:len(beats)>3)
        worker.configure([t],paused=True)
        release.set()
        pump_until(lambda:worker.wake.is_set() is False)
        assert not api.downloads
        worker.check_now(t.key)
        pump_until(lambda:bool(ready))
        assert len(api.downloads)==1
    finally:
        worker.stop();release.set();assert worker.wait(3000);timer.stop()


def test_different_cloud_series_archive_independently_in_same_task(tmp_path):
    a,b=target(tmp_path,'a'),target(tmp_path,'b')
    b.account.drive_binding['folder_id']='other_series'
    root=tmp_path/'state'
    api=FakeDrive()
    ingest=DriveIngestor(root,api,clock=lambda:datetime.now(CHINA_TIME))
    ingest.poll(a)
    calls=[]
    class Uploader:
        def upload_single_video(self,path):calls.append(path);return True,'confirmed',None
    worker=MultiDeployWorker([a,b],root=root,interval_seconds=0,uploader_factory=lambda *args:Uploader())
    worker.ledger=MultiTargetLedger(root)
    worker.run_batch(worker.plan_batch())
    assert len(calls)==1 and (tmp_path/'a'/'publiced'/'海星.mp4').exists()
    assert worker.report['target_count']==1 and worker.report['source_success_count']==1
    assert not list((tmp_path/'b').glob('*.mp4'))
