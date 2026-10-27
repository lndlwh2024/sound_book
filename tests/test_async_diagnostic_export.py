"""Export work must not own the GUI thread or revive a closing publisher."""
from datetime import datetime
import threading
import time
from unittest.mock import MagicMock

from PySide6.QtCore import QThread,QTimer
from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox
import pytest

from src.publisher import diagnostic_export_worker as background
from src.publisher.diagnostic_export import ExportCancelled,export_diagnostics
from src.publisher.event_log import CHINA_TIME,make_event
from src.publisher.failure_diagnostics import TaskFailureDiagnostics
from tests.test_common_platform_accounts import panel


def pump_until(condition,timeout=5):
    deadline=time.monotonic()+timeout
    while not condition() and time.monotonic()<deadline:
        QApplication.processEvents()
        time.sleep(.005)
    assert condition()


def manifest():
    return {'selected_task_id':'fixture','counts':{'normal_logs':1,'dom':0,'screenshots':0},
            'warnings':[],'missing_failure_archives':[]}


def test_export_is_background_serialized_and_finishes_on_gui_thread(panel,tmp_path,monkeypatch):
    entered,release=threading.Event(),threading.Event()
    gui=QThread.currentThread()
    threads=[]
    def export(*args,**kwargs):
        threads.append(QThread.currentThread())
        entered.set()
        assert release.wait(3)
        return manifest()
    monkeypatch.setattr(background,'export_diagnostics',export)
    save=MagicMock(return_value=(str(tmp_path/'report.zip'),'ZIP'))
    monkeypatch.setattr(QFileDialog,'getSaveFileName',save)
    done=[]
    monkeypatch.setattr(QMessageBox,'information',lambda *a:done.append(QThread.currentThread()))
    ticks=[]
    timer=QTimer();timer.setInterval(10);timer.timeout.connect(lambda:ticks.append(time.monotonic()))
    timer.start()
    try:
        start=time.monotonic()
        panel.export_publish_logs()
        assert time.monotonic()-start<.4
        pump_until(entered.is_set)
        assert all(not p.logs.export_button.isEnabled() for p in panel.panels)
        assert all(p.logs.export_button.text()=='正在导出…' for p in panel.panels)
        panel.export_publish_logs()
        save.assert_called_once()
        pump_until(lambda:len(ticks)>=5)
        release.set()
        pump_until(lambda:panel.diagnostic_export_worker is None)
        assert threads and threads[0]!=gui
        assert done==[gui]
        assert all(p.logs.export_button.isEnabled() for p in panel.panels)
    finally:
        release.set();timer.stop()


def test_context_credentials_and_watcher_are_read_in_worker(panel,tmp_path,monkeypatch):
    gui=QThread.currentThread();seen=[]
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *a:(str(tmp_path/'report.zip'),'ZIP'))
    monkeypatch.setattr(QMessageBox,'information',MagicMock())
    monkeypatch.setattr(panel.drive_credentials,'connected',lambda:seen.append(QThread.currentThread()) or True)
    original=panel.drive_watch_worker
    panel.drive_watch_worker=type('Watcher',(),{'snapshot':lambda self:seen.append(QThread.currentThread()) or {'running':True}})()
    captured=[]
    monkeypatch.setattr(background,'export_diagnostics',lambda *a,**k:captured.append(k) or manifest())
    try:
        panel.export_publish_logs()
        pump_until(lambda:panel.diagnostic_export_worker is None)
        assert len(seen)==2 and all(thread!=gui for thread in seen)
        assert captured[0]['runtime_context']['cloud_watch']['connected'] is True
        assert captured[0]['runtime_context']['cloud_watch']['watcher']=={'running':True}
        assert datetime.fromisoformat(captured[0]['cutoff']).tzinfo is not None
    finally:panel.drive_watch_worker=original


def test_failure_can_be_retried_and_cancelled_file_dialog_does_not_start(panel,tmp_path,monkeypatch):
    save=MagicMock(return_value=('','ZIP'))
    monkeypatch.setattr(QFileDialog,'getSaveFileName',save)
    exporter=MagicMock(side_effect=[ValueError('fixture export failure'),manifest()])
    monkeypatch.setattr(background,'export_diagnostics',exporter)
    errors=MagicMock();done=MagicMock()
    monkeypatch.setattr(QMessageBox,'warning',errors)
    monkeypatch.setattr(QMessageBox,'information',done)
    panel.export_publish_logs()
    assert panel.diagnostic_export_worker is None
    exporter.assert_not_called()
    save.return_value=(str(tmp_path/'report.zip'),'ZIP')
    panel.export_publish_logs()
    pump_until(lambda:panel.diagnostic_export_worker is None)
    errors.assert_called_once()
    assert all(p.logs.export_button.isEnabled() for p in panel.panels)
    panel.export_publish_logs()
    pump_until(lambda:panel.diagnostic_export_worker is None)
    done.assert_called_once()
    assert exporter.call_count==2


def test_shutdown_cancels_export_without_completion_popup_or_new_work(panel,tmp_path,monkeypatch):
    entered=threading.Event()
    def export(*a,**kwargs):
        entered.set()
        while not kwargs['stop_requested']():time.sleep(.005)
        raise ExportCancelled()
    monkeypatch.setattr(background,'export_diagnostics',export)
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *a:(str(tmp_path/'report.zip'),'ZIP'))
    info=MagicMock();warn=MagicMock()
    monkeypatch.setattr(QMessageBox,'information',info)
    monkeypatch.setattr(QMessageBox,'warning',warn)
    panel.export_publish_logs()
    pump_until(entered.is_set)
    assert any(item['name']=='诊断包导出' and item['running'] for item in panel.shutdown_inventory())
    assert not panel.prepare_shutdown()
    pump_until(lambda:panel.diagnostic_export_worker is None)
    info.assert_not_called();warn.assert_not_called()
    assert not panel.timer.isActive()
    panel.export_publish_logs()
    assert panel.diagnostic_export_worker is None


def test_cancelled_export_keeps_old_destination_and_removes_temporary_file(tmp_path):
    task=TaskFailureDiagnostics(tmp_path,'task')
    task.record_event(make_event('one event',task_id='task'))
    task.finish()
    destination=tmp_path/'report.zip';destination.write_bytes(b'previous diagnostic')
    stopped=threading.Event()
    def progress(message):
        if message.startswith('压缩'):stopped.set()
    with pytest.raises(ExportCancelled):
        export_diagnostics(tmp_path,tmp_path/'logs',destination,task_id='task',
                           stop_requested=stopped.is_set,progress=progress)
    assert destination.read_bytes()==b'previous diagnostic'
    assert not list(tmp_path.glob('report.zip.*.tmp'))


def test_export_cutoff_excludes_later_events(tmp_path):
    import json,zipfile
    task=TaskFailureDiagnostics(tmp_path,'task')
    for minute in (1,3):
        task.record_event(make_event(str(minute),timestamp=datetime(2026,10,27,8,minute,tzinfo=CHINA_TIME),task_id='task'))
    task.finish()
    destination=tmp_path/'report.zip'
    export_diagnostics(tmp_path,tmp_path/'logs',destination,task_id='task',cutoff='2026-10-27T08:02:00+08:00')
    with zipfile.ZipFile(destination) as archive:
        events=[json.loads(line) for line in archive.read('task-events.jsonl').splitlines()]
    assert [event['message'] for event in events]==['1']


def test_export_uses_click_time_latest_task_if_another_task_starts_before_worker_reads(tmp_path):
    import json,zipfile
    for name,minute in (('old',1),('new',3)):
        task=TaskFailureDiagnostics(tmp_path,name)
        task.record_event(make_event(name,timestamp=datetime(2026,10,27,8,minute,tzinfo=CHINA_TIME),task_id=name))
        task.finish()
        metadata={'task_id':name,'started_at':f'2026-10-27T08:0{minute}:00+08:00','status':'FINISHED'}
        (tmp_path/'.diagnostics'/name/'task.json').write_text(json.dumps(metadata),encoding='utf-8')
        (tmp_path/'.diagnostics/latest-task.json').write_text(json.dumps(metadata),encoding='utf-8')
    path=tmp_path/'report.zip'
    report=export_diagnostics(tmp_path,tmp_path/'logs',path,cutoff='2026-10-27T08:02:00+08:00')
    assert report['selected_task_id']=='old'
    with zipfile.ZipFile(path) as archive:
        events=[json.loads(line) for line in archive.read('task-events.jsonl').splitlines()]
    assert [event['message'] for event in events]==['old']
