"""Actual downloads with no deployment, including publishing stop/pause and app restart."""
import json
import threading
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QCheckBox, QPushButton, QLabel
from src.app import channel_panel as ui
from src.app.publisher_window import PublisherWindow
from src.publisher.drive_ingest import DriveWatchWorker, CHINA_TIME
from src.publisher.google_drive import DriveCredentials
from src.publisher.platforms import ChannelAccountStore
from src.publisher.portable import RunState
from tests.test_google_drive import FakeDrive, cloud_file
from tests.test_drive_ui import pump_until
from tests.test_publishing_options import app


def configured_window(tmp_path,monkeypatch,*,intent='STOPPED',legacy_watch=False):
    store=ChannelAccountStore(tmp_path/'multi_platform')
    accounts=[]
    for index,platform in enumerate(('wechat_channels','youtube','kuaishou')):
        account=store.add(platform,'未登录账号'+str(index))
        directory=tmp_path/('media'+str(index));directory.mkdir()
        account.target_dir=str(directory)
        account.drive_binding={'enabled':index<2,'folder_id':'series','folder_path':'生产/趣味知识'}
        accounts.append(account)
    disabled=store.add('wechat_channels','关闭的账号')
    disabled.enabled=False;disabled.target_dir=str(tmp_path/'media0')
    disabled.drive_binding={'enabled':True,'folder_id':'disabled-series'}
    store.selected_channels=['kuaishou']  # Cloud must ignore publishing checkboxes/target selections.
    store.cloud_poll_seconds=5;store.save()
    RunState(intent=intent,auto_watch=legacy_watch,auto_deploy=False,
             targets=['kuaishou:'+accounts[-1].account_id]).save(tmp_path/'publisher_state.json')
    data=b'fixture-video'*1000
    api=FakeDrive([cloud_file('first',data,'first.mp4')]);api.content={'first':data}
    monkeypatch.setattr(DriveCredentials,'connected',lambda self:True)
    from src.publisher import drive_ingest
    def create(*args,**kwargs):
        return DriveWatchWorker(*args,client=api,clock=lambda:datetime(2026,10,25,tzinfo=CHINA_TIME),**kwargs)
    monkeypatch.setattr(drive_ingest,'DriveWatchWorker',create)
    preflight=MagicMock();monkeypatch.setattr(ui,'DeploymentPreflightWorker',preflight)
    window=PublisherWindow(tmp_path)
    return window,api,accounts,preflight


def close_window(window):
    window.closing=True
    window.panel.worker=None
    window.panel.preflight_worker=None
    window.panel.cleanup_worker=None
    window.panel.prepare_shutdown()
    for worker in (window.panel.drive_watch_worker,window.panel.snapshot_worker):
        if worker:assert worker.wait(5000)
    window.close();window.deleteLater();QApplication.processEvents()


@pytest.mark.parametrize('intent,legacy_watch',[('STOPPED',False),('PAUSED',True),('RUNNING',False)])
def test_saved_bindings_download_on_app_start_without_deployment_or_login(tmp_path,monkeypatch,app,intent,legacy_watch):
    window,api,accounts,preflight=configured_window(tmp_path,monkeypatch,intent=intent,legacy_watch=legacy_watch)
    try:
        panel=window.panel
        # No explicit start/deploy request is made; normal startup may run.
        pump_until(lambda:all((tmp_path/('media'+str(i))/'first.mp4').is_file() for i in (0,1)))
        worker=panel.drive_watch_worker
        assert worker.isRunning() and not worker.paused and worker.poll_seconds==5
        assert {t.key for t in worker.targets}=={a.platform+':'+a.account_id for a in accounts[:2]}
        assert not (tmp_path/'media2/first.mp4').exists()
        assert panel.worker is None and panel.preflight_worker is None
        preflight.assert_not_called()
        assert panel.state.intent==intent
        assert panel.diagnostic_context()['cloud_watch']['mode']=='independent'
        assert len(api.downloads)==2
    finally:close_window(window)


def test_publishing_pause_stop_and_manual_scope_never_stop_cloud_polling(tmp_path,monkeypatch,app):
    window,api,accounts,preflight=configured_window(tmp_path,monkeypatch)
    try:
        panel=window.panel
        pump_until(lambda:all((tmp_path/('media'+str(i))/'first.mp4').is_file() for i in (0,1)))
        worker=panel.drive_watch_worker
        publish=MagicMock();publish.isRunning.return_value=True
        publish.stopping=threading.Event();publish.pause_requested=threading.Event();publish.pause_requested.set()
        publish.file_paths=['one-manual-file.mp4'];publish.targets=[]
        panel.worker=publish;panel.sync_drive_watch()
        assert not worker.paused and len(worker.targets)==2
        for name in ('while-paused','after-stop'):
            if name=='after-stop':
                panel.stop_saved_tasks()
                publish.stop.assert_called_once()
                panel.worker=None
                assert panel.state.intent=='STOPPED'
            data=name.encode()*1000
            api.files.append(cloud_file(name,data,name+'.mp4'));api.content[name]=data
            # Let the real five-second scheduler find new files, without check_now/start_deployment.
            pump_until(lambda:all((tmp_path/('media'+str(i))/(name+'.mp4')).is_file() for i in (0,1)),seconds=9)
            assert not worker.paused and len(worker.targets)==2
        preflight.assert_not_called()
        panel.set_cloud_ingest_enabled(False)
        assert worker.targets==[] and worker.paused
        data=b'after-reenable'*1000
        api.files.append(cloud_file('reenable',data,'reenable.mp4'));api.content['reenable']=data
        panel.set_cloud_ingest_enabled(True)
        pump_until(lambda:all((tmp_path/('media'+str(i))/'reenable.mp4').is_file() for i in (0,1)))
    finally:close_window(window)


def test_cloud_only_diagnostics_export_without_any_publish_task(tmp_path):
    import zipfile
    from src.publisher.diagnostic_export import export_diagnostics
    context={'cloud_watch':{'mode':'independent','enabled':True,'watcher':{'last_scans':[
        {'day':'2026-10-25','status':'FAILED','message':'云盘请求失败示例'}]}}}
    path=tmp_path/'cloud.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',path,runtime_context=context)
    assert result['selection']=='current_cloud_snapshot'
    with zipfile.ZipFile(path) as archive:
        summary=json.loads(archive.read('task-summary.json'))
        assert summary['runtime_context']==context
        assert summary['task']['status']=='SNAPSHOT'
        assert not any(n.endswith('.jsonl') for n in archive.namelist())


def test_settings_explain_independent_cloud_and_remove_old_restore_switch(tmp_path,monkeypatch,app):
    window=PublisherWindow(tmp_path)
    try:
        def inspect(dialog):
            assert not any('恢复云盘监听' in box.text() for box in dialog.findChildren(QCheckBox))
            assert any('独立轮询' in label.text() for label in dialog.findChildren(QLabel))
            assert any('停止部署（云盘监听继续）'==button.text() for button in dialog.findChildren(QPushButton))
            return QDialog.Rejected
        monkeypatch.setattr(QDialog,'exec',inspect)
        window.workspace.settings()
    finally:close_window(window)
