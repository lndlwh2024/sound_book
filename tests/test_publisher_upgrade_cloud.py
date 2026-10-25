"""Regression cases for silent old-instance activation and cloud monitoring across login failures/dates."""
import json
import os
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication
from src.app.single_instance import SingleInstance
from src.publisher.drive_ingest import DriveWatchWorker, CHINA_TIME
from src.publisher.event_log import make_event
from src.publisher.failure_diagnostics import TaskFailureDiagnostics
from src.publisher.preflight import PreflightResult
from src.publisher.runtime_diagnostics import runtime_identity
from tests.test_common_platform_accounts import panel
from tests.test_drive_ui import pump_until
from tests.test_google_drive import FakeDrive, target, cloud_file
from tests.test_publishing_options import app


def _identity(version,path):
    return dict(runtime_identity(),version=version,entrypoint=path)


@pytest.mark.parametrize('legacy,other_path',[(False,False),(False,True),(True,False)])
def test_new_launcher_explains_existing_process_without_taking_ownership(tmp_path,app,legacy,other_path):
    activated=[]
    first=SingleInstance(tmp_path,lambda:activated.append(True),identity=_identity('2026.10.23.3','C:/old/ShuShengPublisher.exe'))
    second=SingleInstance(tmp_path,lambda:None,identity=_identity(
        '2026.10.23.3' if other_path else '2026.10.25.1','C:/new/ShuShengPublisher.exe'))
    try:
        assert first.acquire()
        if legacy:first.instance_path.unlink()
        assert not second.acquire()
        app.processEvents()
        assert activated==[True]
        assert first.lock.isLocked()
        assert '尚未启动' in second.upgrade_notice and '托盘' in second.upgrade_notice
        attempt=json.loads((tmp_path/'last-launch-attempt.json').read_text(encoding='utf-8'))
        assert attempt['attempted']['entrypoint']=='C:/new/ShuShengPublisher.exe'
        if legacy:assert second.existing_instance is None
        else:assert second.existing_instance['version']=='2026.10.23.3'
        second.close()
        assert first.lock.isLocked()
    finally:first.close()


def test_same_version_same_path_activation_has_no_upgrade_warning(tmp_path,app):
    identity=_identity('2026.10.25.1','C:/same/ShuShengPublisher.exe')
    first=SingleInstance(tmp_path,lambda:None,identity=identity)
    second=SingleInstance(tmp_path,lambda:None,identity=identity)
    try:
        assert first.acquire() and not second.acquire()
        assert second.upgrade_notice==''
        assert not (tmp_path/'last-launch-attempt.json').exists()
    finally:first.close();second.close()


def test_stale_instance_description_is_not_reported_as_the_current_owner(tmp_path,app):
    first=SingleInstance(tmp_path,lambda:None)
    second=SingleInstance(tmp_path,lambda:None)
    try:
        assert first.acquire()
        stale=dict(runtime_identity(),pid=os.getpid()+999999,version='bogus')
        first.instance_path.write_text(json.dumps(stale),encoding='utf-8')
        assert not second.acquire()
        assert second.existing_instance is None and '未知' in second.upgrade_notice
    finally:first.close();second.close()


def _api():
    a,b=b'video'*1000,b'video-new'*1000
    files=[cloud_file('day25-a',a,'早起.mp4'),cloud_file('day25-b',b,'乌鸦.mp4')]
    api=FakeDrive(files);api.content={'day25-a':a,'day25-b':b}
    return api


def test_cloud_scans_new_date_immediately_even_with_a_day_long_poll(tmp_path,monkeypatch):
    from src.publisher import drive_ingest
    current=[datetime(2026,10,24,23,59,tzinfo=CHINA_TIME)]
    api=_api();original=api.today_files
    def dated(folder,day):
        if day=='2026-10-24':api.dates.append((folder,day));return {'id':'date24'},[]
        return original(folder,day)
    api.today_files=dated
    chosen=target(tmp_path)
    worker=DriveWatchWorker(tmp_path/'state',SimpleNamespace(connected=lambda:True,generation=0),client=api,clock=lambda:current[0])
    worker.configure([chosen],poll_seconds=86400)
    monkeypatch.setattr(drive_ingest.time,'monotonic',lambda:100.0)
    iterations=[]
    def wait(_):
        iterations.append(1)
        if len(iterations)==2:current[0]=datetime(2026,10,25,0,0,tzinfo=CHINA_TIME)
        elif len(iterations)==3:worker.stop()
    monkeypatch.setattr(worker.wake,'wait',wait)
    worker.watch_loop()
    assert api.dates.count(('series','2026-10-24'))==2  # first scan + next day's catch-up, no early same-day poll
    assert ('series','2026-10-25') in api.dates
    assert set(api.downloads)=={'day25-a','day25-b'}
    assert all((tmp_path/'a'/f['name']).is_file() for f in api.files)
    snapshot=worker.snapshot()
    assert snapshot['current_day']=='2026-10-25'
    assert snapshot['last_scans'][-1]['listing'][-1]['entries_seen']==2
    assert snapshot['next_checks'][0]['day']=='2026-10-25'


def test_date_change_does_not_override_explicit_pause(tmp_path,monkeypatch):
    api=_api();chosen=target(tmp_path)
    worker=DriveWatchWorker(tmp_path/'state',SimpleNamespace(connected=lambda:True,generation=0),client=api,
                            clock=lambda:datetime(2026,10,25,tzinfo=CHINA_TIME))
    worker.configure([chosen],paused=True,pause_reason='用户已暂停')
    monkeypatch.setattr(worker.wake,'wait',lambda _:worker.stop())
    worker.watch_loop()
    assert api.dates==[] and api.downloads==[]
    assert worker.snapshot()['pause_reason']=='用户已暂停'


def test_video_login_preflight_failure_keeps_requested_cloud_download_running(panel,tmp_path,monkeypatch):
    from src.app import channel_panel as ui
    from src.publisher import drive_ingest
    view=next(p for p in panel.panels if p.platform=='wechat_channels')
    account=view.current();account.drive_binding={'enabled':True,'folder_id':'series','folder_path':'书声/趣味知识'}
    panel.store.save();view.reload()
    chosen=next(t for t in panel.all_targets() if t.platform=='wechat_channels')
    api=_api()
    monkeypatch.setattr(panel.drive_credentials,'connected',lambda:True)
    actual=DriveWatchWorker(panel.store.root,panel.drive_credentials,client=api,
        clock=lambda:datetime(2026,10,25,8,48,tzinfo=CHINA_TIME))
    monkeypatch.setattr(drive_ingest,'DriveWatchWorker',lambda *args,**kwargs:actual)
    check=MagicMock();check.targets=[chosen];check.stopping=threading.Event();check.isRunning.return_value=False
    check.results=[PreflightResult(chosen.key,'视频号','AUTH_REQUIRED','当前浏览器显示视频号登录页')]
    monkeypatch.setattr(ui,'DeploymentPreflightWorker',MagicMock(return_value=check))
    monkeypatch.setattr(ui,'prerequisite_results',lambda *a:[])
    monkeypatch.setattr(panel,'choose_targets',lambda:[chosen])
    def begin(targets,force):
        panel._pending_deployment=None
        panel.begin_preflight(targets,None)
    monkeypatch.setattr(panel,'begin_cleanup',begin)
    launch=MagicMock();monkeypatch.setattr(panel,'launch_deployment',launch)
    for platform,toggle in panel.channel_checks.items():toggle.setChecked(platform=='wechat_channels')
    panel.start_deployment()
    check.start.assert_called_once()
    panel.preflight_finished()
    launch.assert_not_called()
    pump_until(lambda:len(api.downloads)==2 and all((tmp_path/f['name']).is_file() for f in api.files))
    assert actual.isRunning() and not actual.paused and panel.worker is None
    assert all((tmp_path/f['name']).is_file() for f in api.files)
    pump_until(lambda:view.table_queue.rowCount()==2)
    assert '2026-10-25' in view.source_state.text()
    context=panel.diagnostic_context()['cloud_watch']
    assert context['watcher']['last_scans'][-1]['listing'][-1]['entries_seen']==2
    panel.set_cloud_ingest_enabled(False)
    assert actual.targets==[] and '已关闭' in view.source_state.text()


def test_paused_or_stale_cloud_status_never_labels_yesterday_as_today(panel,monkeypatch):
    view=next(p for p in panel.panels if p.platform=='wechat_channels')
    account=view.current();account.drive_binding={'enabled':True,'folder_id':'series'}
    key='wechat_channels:'+account.account_id
    panel.drive_states[key]='今日：2026-10-24 已找到 · 检查完成'
    monkeypatch.setattr(panel.drive_credentials,'connected',lambda:True)
    fake=MagicMock();fake.isRunning.return_value=True
    fake.snapshot.return_value={'paused':True,'pause_reason':'用户已暂停','target_keys':[key]}
    panel.drive_watch_worker=fake
    try:
        panel.refresh_cloud_summary(view,today='2026-10-25')
        assert '已暂停' in view.source_state.text() and '上次检查 2026-10-24' in view.source_state.text()
        assert '今日：2026-10-24' not in view.source_state.text()
        fake.snapshot.return_value={'paused':False,'target_keys':[key]}
        panel.refresh_cloud_summary(view,today='2026-10-25')
        assert '当前 2026-10-25' in view.source_state.text() and '待检查' in view.source_state.text()
    finally:panel.drive_watch_worker=None


def test_export_includes_current_runtime_and_cloud_state_without_old_task_logs(tmp_path):
    import zipfile
    from src.publisher.diagnostic_export import export_diagnostics
    task=TaskFailureDiagnostics(tmp_path,'current')
    task.record_event(make_event('current task',task_id='current'));task.finish()
    cloud={'cloud_watch':{'enabled':True,'watcher':{'paused':True,'pause_reason':'用户暂停','current_day':'2026-10-25'}}}
    path=tmp_path/'result.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',path,runtime_context=cloud)
    with zipfile.ZipFile(path) as archive:
        summary=json.loads(archive.read('task-summary.json'))
        assert summary['runtime']['pid']==os.getpid() and summary['runtime']['entrypoint']
        assert summary['runtime_context']==cloud
        assert len([name for name in archive.namelist() if name.endswith('.jsonl')])==1
    assert result['selected_task_id']=='current'
