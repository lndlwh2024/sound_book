"""Failed accounts remain required without polling retries or imaginary starts."""
from datetime import datetime, timedelta
import os
import time
from unittest.mock import MagicMock

from src.publisher import multi_deploy as module
from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger
from tests.test_multi_deploy import setup, worker


def successful_adapter(calls,target):
    adapter=MagicMock(last_outcome='SUCCESS',supports_upload_boundary=True)
    def upload(path):
        calls.append((target.key,path.name))
        adapter.before_upload()
        return True,'已确认成功',{}
    adapter.upload_single_video.side_effect=upload
    return adapter


def test_initially_blocked_and_successful_files_produce_no_events_in_100_later_scans(tmp_path):
    directory,targets=setup(tmp_path,('a.mp4','b.mp4'))
    calls=[]
    job=MultiDeployWorker(targets,root=tmp_path/'state',interval_seconds=0,
        uploader_factory=lambda target,log:successful_adapter(calls,target),
        initial_blocks={targets[1].key:{'status':'AUTH_REQUIRED','message':'登录已失效'}})
    job.ledger=MultiTargetLedger(job.root)
    logs=[];job.log.connect(logs.append)
    job.run_batch(job.plan_batch())
    after_first=list(logs)
    for _ in range(100):job.run_batch(job.plan_batch())
    assert logs==after_first
    assert calls==[(targets[0].key,name) for name in ('a.mp4','b.mp4')]
    assert sum('[账号批次开始]' in line for line in logs)==1
    assert not any('[文件开始]' in line and targets[1].key in line for line in logs)
    assert all(path.exists() for path in (directory/'a.mp4',directory/'b.mp4'))
    for item in job.report['files']:
        assert set(job.ledger.data['plans'][item['sha256']]['required'])=={target.key for target in targets}
        assert item['results'][targets[1].key]['status']=='AUTH_REQUIRED'
        assert item['results'][targets[1].key]['attempts']==[]
        assert item['archived_path'] is None
    job.diagnostics.finish()


def test_mid_batch_auth_failure_stops_remaining_files_retries_and_new_file_attempts(tmp_path):
    directory,targets=setup(tmp_path,('a.mp4','b.mp4'))
    calls=[]
    def factory(target,log):
        if target.key!=targets[0].key:return successful_adapter(calls,target)
        adapter=MagicMock(last_outcome='AUTH_REQUIRED',supports_upload_boundary=True)
        def upload(path):
            calls.append((target.key,path.name))
            adapter.before_upload()
            return False,'需要重新登录',{'submitted':False}
        adapter.upload_single_video.side_effect=upload
        return adapter
    job=worker(tmp_path,targets,factory)
    logs=[];job.log.connect(logs.append)
    job.run_batch(job.plan_batch())
    assert calls==[(targets[0].key,'a.mp4'),(targets[1].key,'a.mp4'),(targets[1].key,'b.mp4')]
    assert sum('[文件开始]' in line and targets[0].key in line for line in logs)==1
    assert not any('[本账号文件重试' in line for line in logs)
    assert any('需要重新登录' in line for line in logs)  # The original failure remains visible.
    settled=len(logs)
    for _ in range(5):job.run_batch(job.plan_batch())
    assert len(logs)==settled
    new=directory/'c.mp4';new.write_bytes(b'new-file'*600)
    os.utime(new,(time.time()-10,)*2)
    job.run_batch(job.plan_batch())
    assert calls[-1]==(targets[1].key,'c.mp4')
    assert len([call for call in calls if call[0]==targets[0].key])==1
    assert not any(targets[0].key in line for line in logs[settled:])
    result=next(item for item in job.report['files'] if item['file_name']=='c.mp4')
    assert result['results'][targets[0].key]['status']=='AUTH_REQUIRED'
    assert result['results'][targets[0].key]['attempts']==[]
    assert all((directory/name).exists() for name in ('a.mp4','b.mp4','c.mp4'))
    job.diagnostics.finish()


def test_dynamic_block_does_not_online_reconcile_an_old_unknown_youtube_post(tmp_path):
    _,targets=setup(tmp_path)
    targets=targets[1:]
    factory=MagicMock()
    job=worker(tmp_path,targets,factory)
    initial=job.plan_batch()[0]
    job.ledger.set_result(initial['digest'],targets[0].key,'FAILED1','old uncertain',
        {'platform':'youtube','video_id':'old-video','submitted':True},attempt_count=1)
    job.jobs_cache.clear()
    job.blocked[targets[0].key]={'status':'AUTH_REQUIRED','message':'登录已失效'}
    logs=[];job.log.connect(logs.append)
    for _ in range(4):job.run_batch(job.plan_batch())
    factory.assert_not_called()
    assert not any('历史结果核验' in line or '[账号批次开始]' in line for line in logs)
    assert job.report['files'][-1]['results'][targets[0].key]['status']=='AUTH_REQUIRED'
    job.diagnostics.finish()


def test_continuous_unchanged_work_logs_wait_and_scan_only_on_state_transition(tmp_path):
    _,targets=setup(tmp_path)
    for target in targets:target.account.auto_archive=False
    calls=[]
    job=worker(tmp_path,targets,lambda target,log:successful_adapter(calls,target))
    waits=[];logs=[];job.log.connect(logs.append)
    def wait(seconds):
        if not seconds:return
        waits.append(seconds)
        if len(waits)==5:job.stop()
    job.controlled_wait=wait
    job.run()
    assert len(calls)==2 and len(waits)==5
    assert sum('[等待本地视频]' in line for line in logs)==1
    assert sum('[检查本地视频]' in line for line in logs)==1
    assert sum('[部署后检查]' in line for line in logs)==1
    assert sum('[文件开始]' in line for line in logs)==2


def test_daily_quota_wait_is_silent_but_higher_limit_resumes_without_a_new_task(tmp_path):
    _,targets=setup(tmp_path)
    targets=targets[:1];targets[0].account.auto_archive=False
    calls=[]
    job=worker(tmp_path,targets,lambda target,log:successful_adapter(calls,target))
    job.quota.set_limit(targets[0].key,0)
    logs=[];job.log.connect(logs.append)
    job.run_batch(job.plan_batch())
    first=list(logs)
    for _ in range(3):job.run_batch(job.plan_batch())
    assert logs==first and not calls
    assert not any('[账号批次开始]' in line or '[文件开始]' in line for line in logs)
    job.quota.set_limit(targets[0].key,2)
    job.run_batch(job.plan_batch())
    assert calls==[(targets[0].key,'a.mp4')]
    job.diagnostics.finish()


def test_server_daily_limit_expires_next_day_while_auth_block_does_not(tmp_path,monkeypatch):
    current=[datetime(2026,10,27,23,59,tzinfo=module.CHINA_TIME)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return current[0]
    monkeypatch.setattr(module,'datetime',Clock)
    _,targets=setup(tmp_path)
    for target in targets:target.account.auto_archive=False
    calls=[]
    job=MultiDeployWorker(targets,root=tmp_path/'state',interval_seconds=0,
        uploader_factory=lambda target,log:successful_adapter(calls,target),
        initial_blocks={targets[0].key:{'status':'ACCOUNT_LIMIT','message':'今日限额'},
                        targets[1].key:{'status':'AUTH_REQUIRED','message':'需要登录'}})
    job.ledger=MultiTargetLedger(job.root)
    job.quota.clock=lambda:current[0]
    job.run_batch(job.plan_batch())
    assert not calls
    current[0]+=timedelta(minutes=2)
    job.run_batch(job.plan_batch())
    assert calls==[(targets[0].key,'a.mp4')]
    assert targets[0].key not in job.initial_blocks
    assert targets[1].key in job.initial_blocks
    assert job.report['files'][0]['results'][targets[1].key]['status']=='AUTH_REQUIRED'
    job.diagnostics.finish()


def test_quiet_projection_does_not_mute_later_real_errors(tmp_path,monkeypatch):
    _,targets=setup(tmp_path)
    job=worker(tmp_path,targets,MagicMock())
    candidate=job.plan_batch()[0]
    monkeypatch.setattr(job,'_attempt_with_diagnostics',MagicMock(side_effect=ValueError('fixture')))
    import pytest
    with pytest.raises(ValueError):job.attempt(candidate,targets[0],quiet=True)
    logs=[];job.log.connect(logs.append)
    job.emit_log('still visible')
    assert logs==['still visible']
    job.diagnostics.finish()
