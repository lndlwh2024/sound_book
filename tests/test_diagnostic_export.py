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
    assert result['counts']==dict(normal_logs=1,dom=1,screenshots=1,task_logs=1,failure_results=1)
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
