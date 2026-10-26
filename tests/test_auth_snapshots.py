import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from src.publisher import auth_snapshots as module


@pytest.fixture
def target(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'_protect',lambda value,decrypt=False:value)
    profile=tmp_path/'profile'
    (profile/'Default/Network').mkdir(parents=True)
    (profile/'Default/Network/Cookies').write_bytes(b'COOKIE_SECRET')
    (profile/'Local State').write_bytes(b'LOCAL_SECRET')
    return SimpleNamespace(platform='wechat_channels',profile=str(profile),account=SimpleNamespace(account_id='stable'))


def test_success_snapshot_rotation_and_failed_comparison_never_overwrites_good_baseline(tmp_path,target):
    logs=[]
    root=tmp_path/'state'
    module.record_auth_result(root,target,'SUCCESS',logs.append)
    latest=root/'auth_snapshots/wechat_channels/stable/closed-latest.json'
    baseline=latest.read_text(encoding='utf-8')
    (module.actual_profile(target)/'Default/Network/Cookies').write_bytes(b'CHANGED_SECRET')
    diff=module.record_auth_result(root,target,'AUTH_REQUIRED',logs.append,task_id='current-task')
    assert diff['difference']['changed_files']==['Default/Network/Cookies']
    assert latest.read_text(encoding='utf-8')==baseline
    assert list((root/'.diagnostics/current-task').glob('auth-diff-*.json'))
    module.record_auth_result(root,target,'SUCCESS',logs.append)
    assert (latest.parent/'closed-previous.json').read_text(encoding='utf-8')==baseline
    assert latest.read_text(encoding='utf-8')!=baseline
    text='\n'.join(logs)+''.join(path.read_text(encoding='utf-8') for path in root.rglob('*.json'))
    assert all(secret not in text for secret in ('COOKIE_SECRET','LOCAL_SECRET','CHANGED_SECRET'))


def test_live_cookie_fingerprints_are_redacted_and_do_not_compare_against_closed_phase(tmp_path,target):
    context=MagicMock()
    context.cookies.return_value=[dict(name='PRIVATE_COOKIE_NAME',value='PRIVATE_COOKIE_VALUE',domain='private.invalid',expires=-1)]
    root=tmp_path/'state'
    module.record_auth_result(root,target,'SUCCESS')
    result=module.record_auth_result(root,target,'AUTH_REQUIRED',context=context,live=True)
    assert result['difference']['baseline_available'] is False
    module.record_auth_result(root,target,'SUCCESS',context=context,live=True)
    context.cookies.return_value=[]
    result=module.record_auth_result(root,target,'AUTH_REQUIRED',context=context,live=True)
    assert result['difference']['cookie_count_before']==1
    assert result['difference']['cookie_count_now']==0
    assert result['difference']['cookie_fingerprints_removed']==1
    text=''.join(path.read_text(encoding='utf-8') for path in root.rglob('*.json'))
    assert all(value not in text for value in ('PRIVATE_COOKIE_NAME','PRIVATE_COOKIE_VALUE','private.invalid'))


def test_large_login_files_are_bounded_and_diagnostic_failure_cannot_fail_task(tmp_path,target,monkeypatch):
    monkeypatch.setattr(module,'MAX_FILE_BYTES',3)
    module.record_auth_result(tmp_path/'state',target,'SUCCESS')
    value=json.loads((tmp_path/'state/auth_snapshots/wechat_channels/stable/closed-latest.json').read_text(encoding='utf-8'))
    assert all(entry.get('skipped')=='size_limit' for entry in value['files'].values())
    monkeypatch.setattr(module,'_protect',MagicMock(side_effect=OSError('fixture key unavailable')))
    assert module.record_auth_result(tmp_path/'other',target,'AUTH_REQUIRED',lambda value:(_ for _ in ()).throw(OSError())) is None


def test_youtube_uses_selected_dedicated_browser_profile(tmp_path,target):
    profile=module.actual_profile(target)
    (profile/'youtube-browser.json').write_text('{"schema":1,"channel":"chrome"}',encoding='utf-8')
    target.platform='youtube'
    assert module.actual_profile(target)==profile/'youtube-system-browser/chrome'


def test_another_process_winning_key_creation_is_not_overwritten(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'_protect',lambda value,decrypt=False:value)
    key_path=tmp_path/'auth_snapshots/fingerprint-key.dpapi'
    def competing_open(*args):
        key_path.write_bytes(b'other-process-key')
        raise FileExistsError()
    monkeypatch.setattr(module.os,'open',competing_open)
    assert module._key(tmp_path)==b'other-process-key'
    assert key_path.read_bytes()==b'other-process-key'
