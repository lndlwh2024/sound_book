"""The independent cloud round exports only its bounded raw log, with task auth diffs."""
import json
import zipfile
from pathlib import Path
import pytest

from src.publisher.cloud_diagnostics import CloudScanDiagnostics,latest_cloud_scan
from src.publisher.diagnostic_export import export_diagnostics
from src.publisher.event_log import make_event
from src.publisher.failure_diagnostics import TaskFailureDiagnostics


def scan(root,message):
    value=CloudScanDiagnostics(root,['wechat_channels:a'])
    value.record(make_event(message,cloud_scan_id=value.scan_id,category='cloud'))
    value.finish()
    return value


def test_cloud_only_export_uses_latest_scan_without_publishing_or_history(tmp_path):
    previous=scan(tmp_path,'OLD CLOUD ROUND')
    current=scan(tmp_path,'LATEST CLOUD ROUND')
    report=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',report)
    assert result['selection']=='current_cloud_snapshot'
    assert not result['warnings']
    assert result['counts']['cloud_logs']==1
    with zipfile.ZipFile(report) as archive:
        content=b'\n'.join(archive.read(name) for name in archive.namelist())
        assert b'LATEST CLOUD ROUND' in content and b'OLD CLOUD ROUND' not in content
        cloud=json.loads(archive.read('cloud-scan-summary.json'))
        assert cloud['scan_id']==current.scan_id
        events=[json.loads(line) for line in archive.read('cloud/events.jsonl').decode().splitlines()]
        assert {e['cloud_scan_id'] for e in events}=={current.scan_id}
        assert previous.scan_id.encode() not in content


def test_export_includes_current_task_auth_diffs_and_latest_scan_only(tmp_path):
    old=TaskFailureDiagnostics(tmp_path,'old')
    (old.task_folder/'auth-diff-old.json').write_text('{"note":"OLD AUTH DIFF"}',encoding='utf-8')
    old.finish()
    task=TaskFailureDiagnostics(tmp_path,'current')
    task.record_event(make_event('current task',task_id='current'))
    (task.task_folder/'auth-diff-current.json').write_text(json.dumps({
        'task_id':'current','comparison':'LATEST AUTH DIFF','refresh_token':'RAW-SECRET'}),encoding='utf-8')
    (task.task_folder/'auth-diff-wrong.json').write_text('{"task_id":"old","note":"WRONG TASK"}',encoding='utf-8')
    private=tmp_path/'auth_snapshots/private';private.mkdir(parents=True)
    (private/'raw-cookie.json').write_text('PRIVATE PROFILE',encoding='utf-8')
    task.finish()
    scan(tmp_path,'old cloud');current=scan(tmp_path,'current cloud')
    current.record(make_event('wrong scan row',cloud_scan_id='WRONG-SCAN'))
    current.record(make_event('future cloud row',timestamp='2999-01-01T00:00:00+08:00',cloud_scan_id=current.scan_id))
    report=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',report)
    assert result['selected_task_id']=='current'
    assert result['counts']['auth_summaries']==1
    with zipfile.ZipFile(report) as archive:
        content=b'\n'.join(archive.read(name) for name in archive.namelist())
        assert b'LATEST AUTH DIFF' in content
        for secret in (b'OLD AUTH DIFF',b'WRONG TASK',b'PRIVATE PROFILE',b'RAW-SECRET',b'wrong scan row',b'future cloud row',b'old cloud'):
            assert secret not in content
        assert json.loads(archive.read('auth/auth-diff-current.json'))['refresh_token']=='[已隐藏]'


def test_scan_spool_redacts_structured_and_text_credentials_before_disk(tmp_path):
    value=CloudScanDiagnostics(tmp_path,['youtube:a'])
    value.record(make_event('Authorization: Bearer TOKEN123; refresh_token=TOKEN456',cloud_scan_id=value.scan_id,
        access_token='TOKEN789',details={'cookie':'SESSION-SECRET'}))
    raw=value.path.read_text(encoding='utf-8')
    assert not any(secret in raw for secret in ('TOKEN123','TOKEN456','TOKEN789','SESSION-SECRET'))
    assert '[已隐藏]' in raw


@pytest.mark.parametrize('message',[
    'Authorization: Bearer TOKEN123',
    "Authorization:'Bearer TOKEN123'",
    'Authorization: "Bearer TOKEN123"',
    '{"Authorization":"Bearer TOKEN123", "refresh_token":"TOKEN456"}',
    "{'authorization':'bEaReR TOKEN123'}",
    'authorization=BEARER TOKEN123; refresh_token=TOKEN456',
])
def test_bearer_redaction_handles_authorization_prefix_quotes_and_json(message):
    from src.publisher.event_log import redact
    redacted=redact(message)
    assert 'TOKEN123' not in redacted and 'TOKEN456' not in redacted
    assert '[已隐藏]' in redacted


def test_scan_retention_keeps_two_generated_rounds_without_deleting_unrelated_files(tmp_path):
    old=scan(tmp_path,'old')
    (old.folder/'keep-user-file.txt').write_text('keep',encoding='utf-8')
    middle=scan(tmp_path,'middle');latest=scan(tmp_path,'latest')
    assert not old.path.exists()
    assert (old.folder/'keep-user-file.txt').read_text()=='keep'
    assert middle.path.exists() and latest.path.exists()
    assert latest_cloud_scan(tmp_path)[0]['scan_id']==latest.scan_id


def test_invalid_scan_pointer_never_falls_back_to_old_cloud_logs(tmp_path):
    scan(tmp_path,'old cloud')
    pointer=tmp_path/'.cloud_diagnostics/latest-scan.json'
    pointer.write_text('{"scan_id":"../old"}',encoding='utf-8')
    task=TaskFailureDiagnostics(tmp_path,'current')
    task.record_event(make_event('current task',task_id='current'));task.finish()
    report=tmp_path/'report.zip'
    result=export_diagnostics(tmp_path,tmp_path/'logs',report)
    assert any('最新独立云盘日志不可读取' in w.get('error','') for w in result['warnings'])
    with zipfile.ZipFile(report) as archive:
        assert not any(name.startswith('cloud/') for name in archive.namelist())
