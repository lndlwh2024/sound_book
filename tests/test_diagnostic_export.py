import json
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock
from src.publisher.diagnostic_export import export_diagnostics
from src.publisher.event_log import PublishEventLog
from src.publisher.failure_diagnostics import TaskFailureDiagnostics


def test_export_includes_generated_failure_evidence_but_not_accounts_or_success(tmp_path):
    logs=PublishEventLog(tmp_path)
    logs.write('开始',task_id='task')
    task=TaskFailureDiagnostics(tmp_path,'task',logs.task_events('task'))
    operation=task.operation('douyin','account','video.mp4','op')
    page=MagicMock()
    page.url='https://example.test/'
    page.is_closed.return_value=False
    frame=SimpleNamespace(url=page.url,name='',evaluate=lambda js:{'nodes':[{'text':'失败页面'}]})
    page.frames=[frame];page.main_frame=frame
    def screenshot(**kwargs):
        from pathlib import Path
        Path(kwargs['path']).write_bytes(b'PNG-fixture')
    page.screenshot.side_effect=screenshot
    operation.capture(page)
    operation.complete('FAILED','失败')
    task.finish()
    (tmp_path/'accounts.json').write_text('SECRET')
    (tmp_path/'token.json').write_text('SECRET')
    (tmp_path/'failure_diagnostics/抖音/2026-01-01').mkdir()
    (tmp_path/'failure_diagnostics/抖音/2026-01-01/accounts.json').write_text('SECRET')
    path=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,logs.folder,path)
    assert result['counts']==dict(normal_logs=1,dom=1,screenshots=1,task_logs=0,failure_results=1)
    with zipfile.ZipFile(path) as stream:
        assert stream.testzip() is None
        assert not any('accounts.json' in p or 'token.json' in p for p in stream.namelist())
        assert any(p.startswith('failure_diagnostics/抖音/') and p.endswith('.png') for p in stream.namelist())
        assert 'diagnostic-manifest.json' in stream.namelist()
    assert not result['warnings']


def test_missing_failure_archive_is_explicit_in_manifest(tmp_path):
    logs=PublishEventLog(tmp_path)
    logs.write('[失败诊断归档] FAILED：missing',task_id='missing',platform='youtube',file_name='a.mp4')
    result=export_diagnostics(tmp_path,logs.folder,tmp_path/'report.zip')
    assert result['counts']['screenshots']==0
    assert result['missing_failure_archives'][0]['task_id']=='missing'


def test_screenshot_failure_does_not_prevent_dom_and_reports_actual_error(tmp_path):
    task=TaskFailureDiagnostics(tmp_path,'task')
    operation=task.operation('kuaishou')
    page=MagicMock();page.url='https://example.test/';page.is_closed.return_value=False
    frame=SimpleNamespace(url=page.url,name='',evaluate=lambda js:{'nodes':[{'text':'失败页面'}]})
    page.frames=[frame];page.main_frame=frame
    page.screenshot.side_effect=RuntimeError('截图接口不可用')
    operation.capture(page)
    folder=operation.complete('FAILED')
    assert operation.capture_summary()['screenshots']==0
    assert '截图接口不可用' in operation.capture_summary()['errors'][0]
    data=json.loads(next(folder.glob('*.dom.json')).read_text(encoding='utf-8'))
    assert data['dom_status']=='saved' and data['screenshot_status']=='unavailable'
    assert len(page.screenshot.call_args_list)==2
    task.finish()
    result=export_diagnostics(tmp_path,tmp_path/'logs',tmp_path/'report.zip')
    assert result['warnings']


def test_dom_failure_keeps_the_independently_captured_last_screenshot(tmp_path):
    from pathlib import Path
    task=TaskFailureDiagnostics(tmp_path,'task')
    operation=task.operation('youtube')
    page=MagicMock();page.url='https://example.test/';page.is_closed.return_value=False
    frame=MagicMock();frame.url=page.url;frame.name='';frame.evaluate.side_effect=RuntimeError('DOM读取断开')
    page.frames=[frame];page.main_frame=frame
    page.screenshot.side_effect=lambda **k:Path(k['path']).write_bytes(b'PNG-fixture')
    operation.capture(page)
    folder=operation.complete('RECOVERY_REQUIRED')
    assert next(folder.glob('*.png')).read_bytes()==b'PNG-fixture'
    assert operation.capture_summary()['screenshots']==1
    assert any('DOM读取断开' in error for error in operation.capture_summary()['errors'])
    task.finish()


def test_failed_screenshot_partial_file_is_not_reported_as_a_saved_image(tmp_path):
    from pathlib import Path
    task=TaskFailureDiagnostics(tmp_path,'task')
    operation=task.operation('douyin')
    page=MagicMock();page.url='https://example.test/';page.is_closed.return_value=False
    frame=SimpleNamespace(url=page.url,name='',evaluate=lambda js:{'nodes':[]})
    page.frames=[frame];page.main_frame=frame
    def broken_capture(**kwargs):
        Path(kwargs['path']).write_bytes(b'partial-png')
        raise OSError('磁盘写入中断')
    page.screenshot.side_effect=broken_capture
    operation.capture(page)
    folder=operation.complete('FAILED')
    assert not list(folder.glob('*.png'))
    assert operation.capture_summary()['screenshots']==0
    assert operation.capture_summary()['errors']
    task.finish()


def _capture_task(root, identity, count=1):
    from pathlib import Path
    from src.publisher.event_log import make_event
    task=TaskFailureDiagnostics(root,identity)
    task.record_event(make_event(identity+' task log',task_id=identity))
    for index in range(count):
        operation=task.operation('wechat_channels','account','video.mp4','op'+str(index))
        page=MagicMock();page.url='https://example.test/';page.is_closed.return_value=False
        frame=SimpleNamespace(url=page.url,name='',evaluate=lambda js:{'nodes':[{'text':identity+str(index)}]})
        page.frames=[frame];page.main_frame=frame
        page.screenshot.side_effect=lambda **k:Path(k['path']).write_bytes(b'PNG-fixture')
        operation.capture(page,'等待上传完成')
        operation.complete('FAILED','fixture failure')
    return task


def _zip_events(path):
    with zipfile.ZipFile(path) as archive:
        return [json.loads(line) for line in archive.read('task-events.jsonl').decode().splitlines()]


def test_latest_failed_preflight_does_not_export_previous_task(tmp_path):
    from src.publisher.event_log import make_event
    old=_capture_task(tmp_path,'old');old.finish()
    latest=TaskFailureDiagnostics(tmp_path,'preflight')
    latest.record_event(make_event('启动验证失败',task_id='preflight'))
    latest.operation('youtube','account').complete('FAILED','无法启动浏览器')
    latest.finish()
    path=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',path)
    assert result['selected_task_id']=='preflight'
    assert result['counts']['screenshots']==0 and result['counts']['failure_results']==1
    assert {e['task_id'] for e in _zip_events(path)}=={'preflight'}
    with zipfile.ZipFile(path) as archive:
        assert not any('old_' in name for name in archive.namelist())


def test_old_task_finishing_late_cannot_replace_new_latest_success(tmp_path):
    old=_capture_task(tmp_path,'old')
    new=TaskFailureDiagnostics(tmp_path,'success')
    new.record_event(PublishEventLog(tmp_path).write('成功结束',task_id='success'))
    new.finish();old.finish()
    path=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',path)
    assert result['selected_task_id']=='success'
    assert result['counts']==dict(normal_logs=1,dom=0,screenshots=0,task_logs=0,failure_results=0)
    assert not result['warnings']


def test_task_log_survives_rotation_and_preflight_handoff_without_duplicates(tmp_path):
    log=PublishEventLog(tmp_path,maximum=400,backups=1)
    early=log.write('预检',task_id='shared')
    task=TaskFailureDiagnostics(tmp_path,'shared',[early]);task.finish()
    task=TaskFailureDiagnostics(tmp_path,'shared',[early])
    task.record_event(log.write('发布结束',task_id='shared'));task.finish()
    for i in range(10):log.write('无关历史日志'+str(i),task_id='noise')
    path=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,log.folder,path)
    assert result['selected_task_id']=='shared'
    assert [e['message'] for e in _zip_events(path)]==['预检','发布结束']
    canonical=(tmp_path/'.diagnostics/shared/events.jsonl').read_text(encoding='utf-8')
    assert len(canonical.splitlines())==2


def test_current_task_captures_and_only_last_two_per_failed_step(tmp_path):
    task=_capture_task(tmp_path,'current',4)
    path=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',path,task_id='current')
    assert result['task']['status']=='RUNNING'
    assert result['counts']['dom']==2 and result['counts']['screenshots']==2
    assert result['omitted_capture_count']==2
    assert result['counts']['task_logs']==0
    with zipfile.ZipFile(path) as archive:
        names=archive.namelist()
        assert len([n for n in names if n.endswith('.jsonl')])==1
        doms=[json.loads(archive.read(n)) for n in names if n.endswith('.dom.json')]
        assert {d['operation_id'] for d in doms}=={'op2','op3'}
    task.finish()


def test_running_unarchived_page_and_explicit_missing_task_never_use_history(tmp_path):
    from pathlib import Path
    old=_capture_task(tmp_path,'old');old.finish()
    task=TaskFailureDiagnostics(tmp_path,'active')
    operation=task.operation('douyin','a','active.mp4')
    page=MagicMock();page.url='https://example.test/';page.is_closed.return_value=False
    page.frames=[]
    page.screenshot.side_effect=lambda **k:Path(k['path']).write_bytes(b'PNG-fixture')
    operation.capture(page,'当前步骤')
    path=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',path,task_id='active')
    assert result['counts']['screenshots']==1 and result['counts']['failure_results']==0
    result=export_diagnostics(tmp_path,tmp_path/'logs',path,task_id='absent')
    assert result['counts']['screenshots']==0 and result['warnings']
    assert result['selected_task_id']=='absent'
    task.finish()


def test_legacy_selection_uses_start_instead_of_last_late_event(tmp_path):
    log=PublishEventLog(tmp_path)
    log.write('old start',timestamp='2026-10-22T10:00:00+08:00',task_id='old')
    log.write('new start',timestamp='2026-10-22T11:00:00+08:00',task_id='new')
    log.write('old late finish',timestamp='2026-10-22T12:00:00+08:00',task_id='old')
    path=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,log.folder,path)
    assert result['selected_task_id']=='new'
    assert len(_zip_events(path))==1


def test_no_task_and_corrupt_latest_index_never_dump_history(tmp_path):
    import pytest
    with pytest.raises(ValueError,match='尚无'):
        export_diagnostics(tmp_path,tmp_path/'logs',tmp_path/'report.zip')
    task=_capture_task(tmp_path,'old');task.finish()
    (tmp_path/'.diagnostics/latest-task.json').write_text('{}')
    with pytest.raises(ValueError,match='未改用历史任务'):
        export_diagnostics(tmp_path,tmp_path/'logs',tmp_path/'report.zip')


def test_summary_keeps_results_but_does_not_repeat_attempt_logs(tmp_path):
    task=TaskFailureDiagnostics(tmp_path,'latest');task.finish()
    folder=tmp_path/'publish_reports';folder.mkdir()
    (folder/'publish_task_20261023_latest.json').write_text(json.dumps({
        'task_id':'latest','status':'COMPLETED','files':[{'file_name':'a.mp4','results':{
        'douyin:a':{'status':'SUCCESS','attempts':[{'logs':['DUPLICATED LOG']}], 'message':'完成'}}}]}))
    path=tmp_path/'report.zip'
    export_diagnostics(tmp_path,tmp_path/'logs',path)
    with zipfile.ZipFile(path) as archive:
        summary=json.loads(archive.read('task-summary.json'))
        assert summary['publish_report']['files'][0]['results']['douyin:a']['status']=='SUCCESS'
        assert b'DUPLICATED LOG' not in archive.read('task-summary.json')


def test_missing_capture_file_is_reported_without_substituting_old_evidence(tmp_path):
    task=_capture_task(tmp_path,'latest');task.finish()
    next((tmp_path/'failure_diagnostics').glob('*/*/*.dom.json')).unlink()
    result=export_diagnostics(tmp_path,tmp_path/'logs',tmp_path/'report.zip')
    assert result['counts']['dom']==0 and result['counts']['screenshots']==0
    assert any(w.get('missing_evidence') for w in result['warnings'])
