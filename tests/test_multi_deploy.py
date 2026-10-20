import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from src.publisher.platforms import ChannelAccount
from src.publisher.douyin.models import DouyinAccountConfig
from src.publisher.multi_deploy import PublishTarget, MultiDeployWorker, MultiTargetLedger


@pytest.fixture(scope='module', autouse=True)
def app():
    return QApplication.instance() or QApplication([])


def test_atomic_report_retries_temporary_windows_lock(tmp_path,monkeypatch):
    from src.publisher import multi_deploy as module
    path=tmp_path/'report.json'
    path.write_text('{"old":true}',encoding='utf-8')
    replace=Path.replace
    attempts=[]
    def locked(source,target):
        attempts.append(target)
        if len(attempts)<3:
            raise PermissionError('temporarily locked')
        return replace(source,target)
    monkeypatch.setattr(Path,'replace',locked)
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    module.atomic_json(path,{'new':True})
    assert len(attempts)==3 and json.loads(path.read_text())=={'new':True}


def test_atomic_report_permanent_lock_preserves_previous_record(tmp_path,monkeypatch):
    from src.publisher import multi_deploy as module
    path=tmp_path/'report.json'
    path.write_text('{"old":true}',encoding='utf-8')
    monkeypatch.setattr(Path,'replace',MagicMock(side_effect=PermissionError('locked')))
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    with pytest.raises(PermissionError):
        module.atomic_json(path,{'new':True})
    assert json.loads(path.read_text())=={'old':True}


def setup(tmp_path, names=('a.mp4',)):
    directory = tmp_path / 'media'
    directory.mkdir()
    for index, name in enumerate(names):
        path = directory / name
        path.write_bytes(bytes([index+1])*2048)
        os.utime(path, (time.time()-10,time.time()-10))
    douyin = DouyinAccountConfig('d','douyin',target_dir=str(directory),profile_dir=str(tmp_path/'douyin'))
    yt = ChannelAccount('b'*32,'youtube','youtube',target_dir=str(directory),channel_id='UC'+'a'*22,made_for_kids=False)
    targets = [PublishTarget('douyin',douyin,douyin.profile_dir),PublishTarget('youtube',yt,str(tmp_path/'youtube'))]
    return directory, targets


def worker(tmp_path, targets, factory):
    job = MultiDeployWorker(targets,root=tmp_path/'state',uploader_factory=factory,interval_seconds=0)
    job.ledger = MultiTargetLedger(job.root)
    return job


def test_continuous_task_logs_scan_wait_and_retains_unknown_without_reupload(tmp_path):
    directory,targets=setup(tmp_path)
    targets=targets[:1]
    targets[0].account.auto_archive=False
    calls=[]
    def factory(target,log):
        def upload(path):
            calls.append(path.name)
            uploader.last_outcome='UNCONFIRMED'
            return False,'提交后浏览器关闭，无法确认',{'confirmation':None}
        uploader=MagicMock(upload_single_video=upload)
        return uploader
    job=worker(tmp_path,targets,factory)
    job.poll_seconds=5
    logs,statuses,waits=[],[],[]
    job.log.connect(logs.append)
    job.status.connect(statuses.append)
    def wait(seconds):
        if not seconds:
            return
        waits.append(seconds)
        if len(waits)==2:
            job.stop()
    job.controlled_wait=wait
    job.run()
    assert calls==['a.mp4','a.mp4']
    assert waits==[5,5]
    assert sum('[等待本地视频]' in line for line in logs)==2
    assert sum('[检查本地视频]' in line for line in logs)==1
    assert not any('[待核对保留]' in line for line in logs)
    assert any('每5秒' in line and '北京时间' in line for line in logs)
    assert any('等待本地视频（每5秒检查）' in line for line in statuses)
    assert job.report['files'][0]['results'][targets[0].key]['status']=='FAILED2'


def test_manual_batch_does_not_claim_to_enter_periodic_scanning(tmp_path):
    directory,targets=setup(tmp_path)
    for target in targets:
        target.account.auto_archive=False
    job=worker(tmp_path,targets,lambda target,log:MagicMock(
        last_outcome='SUCCESS',upload_single_video=lambda path:(True,'成功',None)))
    job.file_paths={str((directory/'a.mp4').resolve())}
    logs=[]
    job.log.connect(logs.append)
    job.run()
    assert job.report['status']=='COMPLETED'
    assert not any('[等待本地视频]' in line for line in logs)


def test_deferred_archive_and_only_failed_target_retries(tmp_path):
    directory, targets = setup(tmp_path)
    calls = []
    def factory(target, log):
        def upload(path):
            calls.append(target.key)
            assert path.exists()  # Douyin must not move the file before YouTube sees it.
            success = target.platform=='douyin' or calls.count(target.key)==2
            uploader.last_outcome = 'SUCCESS' if success else 'FAILED'
            return success,'result',{'video_id':'abcdefghijk'}
        uploader = MagicMock(upload_single_video=upload)
        return uploader
    job = worker(tmp_path,targets,factory)
    job.run_batch(job.plan_batch())
    assert calls == [targets[0].key,targets[1].key,targets[1].key]
    assert not (directory/'a.mp4').exists()
    assert (directory/'publiced'/'a.mp4').exists()
    report = json.loads(job.report_path.read_text(encoding='utf-8'))
    assert report['source_file_count']==1 and report['target_count']==2 and report['success_count']==2
    assert len(report['files'][0]['results'][targets[0].key]['attempts'])==1
    assert len(report['files'][0]['results'][targets[1].key]['attempts'])==2


@pytest.mark.parametrize('stop_in_pause',[False,True])
@pytest.mark.parametrize('first_fails',[False,True])
def test_all_targets_pause_after_active_file_then_resume_or_stop(tmp_path,stop_in_pause,first_fails):
    import threading
    directory,targets=setup(tmp_path,('a.mp4','b.mp4'))
    calls=[]
    def factory(target,log):
        def upload(path):
            calls.append((target.key,path.name))
            if len(calls)==1:
                job.pause_all()
            success=not (first_fails and len(calls)==1)
            uploader.last_outcome='SUCCESS' if success else 'FAILED'
            return success,'result',None
        uploader=MagicMock(upload_single_video=upload)
        return uploader
    job=worker(tmp_path,targets,factory)
    job.file_paths={str(p.resolve()) for p in directory.glob('*.mp4')}
    task_id=job.report['task_id']
    thread=threading.Thread(target=job.run)
    thread.start()
    try:
        deadline=time.monotonic()+5
        while job.report['status']!='PAUSED' and time.monotonic()<deadline:
            time.sleep(.01)
        assert job.report['status']=='PAUSED' and len(calls)==1
        time.sleep(.15)
        assert len(calls)==1  # Other account/platform must not start either.
        assert not (directory/'publiced').exists()
        if stop_in_pause:
            job.stop()
        else:
            job.resume()
        thread.join(5)
        assert not thread.is_alive()
        assert job.report['task_id']==task_id
        if stop_in_pause:
            assert len(calls)==1 and job.report['status']=='STOPPED'
            assert all((directory/name).exists() for name in ('a.mp4','b.mp4'))
        else:
            assert len(calls)==(5 if first_fails else 4) and len(set(calls))==4
            assert job.report['status']=='COMPLETED'
            assert job.report['success_count']==4
    finally:
        job.stop()
        thread.join(5)


def test_restart_subset_archives_selected_success_without_unselected_failure(tmp_path):
    directory, targets = setup(tmp_path)
    calls = []
    def factory(target, log):
        uploader = MagicMock()
        def upload(path):
            calls.append(target.key)
            uploader.last_outcome='FAILED' if target.platform=='youtube' else 'SUCCESS'
            return target.platform=='douyin','result',None
        uploader.upload_single_video=upload
        return uploader
    first = worker(tmp_path,targets,factory)
    first.run_batch(first.plan_batch())
    assert (directory/'a.mp4').exists()
    second = worker(tmp_path,targets[:1],factory)
    second.run_batch(second.plan_batch())
    assert calls.count(targets[0].key)==1
    assert not (directory/'a.mp4').exists()
    item=second.report['files'][0]
    assert item['results'][targets[0].key]['status']=='SUCCESS'
    assert second.ledger.result(item['sha256'],targets[1].key)['status']=='FAILED2'
    assert second.report['source_success_count']==1
    assert (directory/'publiced'/'a.mp4').exists()


def test_quota_blocks_only_one_account_other_files_continue(tmp_path):
    directory, targets = setup(tmp_path,('a.mp4','b.mp4'))
    calls=[]
    def factory(target,log):
        uploader=MagicMock()
        def upload(path):
            calls.append((target.platform,path.name))
            uploader.last_outcome='ACCOUNT_LIMIT' if target.platform=='douyin' else 'SUCCESS'
            return target.platform=='youtube','daily limit',None
        uploader.upload_single_video=upload
        return uploader
    job=worker(tmp_path,targets,factory)
    job.run_batch(job.plan_batch())
    assert sum(p=='douyin' for p,_ in calls)==1
    assert sum(p=='youtube' for p,_ in calls)==2
    assert len(list(directory.glob('*.mp4')))==2
    assert job.ledger.quota_block(targets[0].key)


def test_restricted_account_stops_without_clearing_unknown_or_blocking_other_targets(tmp_path):
    import hashlib
    directory,targets=setup(tmp_path,('a.mp4','b.mp4','c.mp4'))
    calls=[]
    def factory(target,log):
        uploader=MagicMock()
        def upload(path):
            calls.append((target.key,path.name))
            uploader.last_outcome='ACCOUNT_RESTRICTED' if target==targets[0] else 'SUCCESS'
            return target==targets[1],'账号已被封禁' if target==targets[0] else '成功',None
        uploader.upload_single_video=upload
        return uploader
    job=worker(tmp_path,targets,factory)
    digest=hashlib.sha256((directory/'a.mp4').read_bytes()).hexdigest()
    job.ledger.set_result(digest,targets[0].key,'UNCONFIRMED','之前浏览器关闭，待核对')
    logs=[]
    job.log.connect(logs.append)
    job.run_batch(job.plan_batch())
    assert [name for key,name in calls if key==targets[0].key]==['a.mp4']
    assert len([key for key,_ in calls if key==targets[1].key])==3
    assert job.ledger.result(digest,targets[0].key)['status']=='FAILED1'
    assert job.blocked[targets[0].key]['status']=='ACCOUNT_RESTRICTED'
    assert any('[本轮账号限制]' in line and '账号受限' in line and '封禁' in line for line in logs)
    assert not job.ledger.quota_block(targets[0].key)  # Restrictions are checked live next task.
    assert all((directory/name).exists() for name in ('a.mp4','b.mp4','c.mp4'))


@pytest.mark.parametrize('state',['PERMANENT_REJECT','DURATION_LIMIT'])
def test_non_retryable_never_uploads_twice(tmp_path,state):
    directory,targets=setup(tmp_path)
    calls=[]
    def factory(target,log):
        uploader=MagicMock(last_outcome=state)
        def upload(path):
            calls.append(path)
            return False,'reason',None
        uploader.upload_single_video=upload
        uploader.reconcile.return_value=False
        return uploader
    job=worker(tmp_path,targets[1:],factory)
    job.run_batch(job.plan_batch())
    second=worker(tmp_path,targets[1:],factory)
    second.run_batch(second.plan_batch())
    assert len(calls)==1 and (directory/'a.mp4').exists()


@pytest.mark.parametrize('platform',['douyin','xiaohongshu','kuaishou','youtube','wechat_channels'])
def test_ignored_target_stays_in_report_and_never_uploads_or_counts_as_failure(tmp_path,platform):
    import hashlib
    directory,targets=setup(tmp_path)
    targets=[PublishTarget(platform,targets[0].account,targets[0].profile)]
    targets[0].account.auto_archive=False
    digest=hashlib.sha256((directory/'a.mp4').read_bytes()).hexdigest()
    factory=MagicMock()
    job=worker(tmp_path,targets[:1],factory)
    job.ledger.set_result(digest,targets[0].key,'IGNORED','用户删除跟踪目标，保留行和媒体')
    job.run_batch(job.plan_batch())
    factory.assert_not_called()
    assert job.report['files'][0]['results'][targets[0].key]['status']=='IGNORED'
    assert job.report['ignored_count']==1 and job.report['failure_count']==0 and job.report['success_count']==0
    assert job.report['platform_counts'][platform]['ignored']==1
    assert (directory/'a.mp4').exists() and not (directory/'publiced').exists()
    copied=directory/'copied.mp4'
    copied.write_bytes((directory/'a.mp4').read_bytes())
    os.utime(copied,(time.time()-10,time.time()-10))
    second=worker(tmp_path,targets[:1],factory)
    second.run_batch(second.plan_batch())
    factory.assert_not_called()
    assert second.report['ignored_count']==2


def test_readonly_reconcile_confirms_without_uploading_again(tmp_path):
    directory,targets=setup(tmp_path)
    job=worker(tmp_path,targets[1:],MagicMock())
    jobs=job.plan_batch()
    job.ledger.set_result(jobs[0]['digest'],targets[1].key,'UNCONFIRMED','stopped',{'video_id':'abcdefghijk'})
    checker=MagicMock()
    checker.reconcile.return_value=True
    job.factory=lambda *args:checker
    job.run_batch(jobs)
    checker.upload_single_video.assert_not_called()
    checker.reconcile.assert_called_once()
    assert (directory/'publiced'/'a.mp4').exists()


@pytest.mark.parametrize('other',['SUCCESS','IGNORED','UNCONFIRMED','FAILED'])
def test_ignored_is_archive_complete_but_original_target_state_persists(tmp_path,other):
    import hashlib
    directory,targets=setup(tmp_path)
    digest=hashlib.sha256((directory/'a.mp4').read_bytes()).hexdigest()
    uploader=MagicMock(last_outcome=other)
    uploader.reconcile.return_value=False
    uploader.upload_single_video.return_value=(False,'fixture',None)
    job=worker(tmp_path,targets,lambda *args:uploader)
    job.ledger.set_result(digest,targets[0].key,'IGNORED','不再跟踪')
    job.ledger.set_result(digest,targets[1].key,other,'fixture')
    job.run_batch(job.plan_batch())
    completed=other in ('SUCCESS','IGNORED')
    assert (directory/'publiced'/'a.mp4').exists()==completed
    assert (directory/'a.mp4').exists()!=completed
    assert MultiTargetLedger(job.root).result(digest,targets[0].key)['status']=='IGNORED'


def test_stop_after_first_target_preserves_original_and_reports_cancel(tmp_path):
    directory,targets=setup(tmp_path)
    job=worker(tmp_path,targets,MagicMock())
    def factory(target,log):
        def upload(path):
            job.stop()
            return True,'success',None
        return MagicMock(upload_single_video=upload)
    job.factory=factory
    job.run()
    assert (directory/'a.mp4').exists()
    assert job.report['cancelled_count']==1
    assert job.report['platform_counts']['youtube']['cancelled']==1
    assert job.report['platform_counts']['youtube']['failure']==0


def test_douyin_adapter_disables_archive_without_mutating_original(tmp_path):
    _,targets=setup(tmp_path)
    job=MultiDeployWorker(targets,root=tmp_path/'state')
    adapter=job.make_uploader(targets[0],lambda message:None)
    assert adapter.account.auto_archive is False
    assert targets[0].account.auto_archive is True


def test_archive_never_overwrites_existing_media(tmp_path):
    directory,targets=setup(tmp_path)
    (directory/'publiced').mkdir()
    (directory/'publiced'/'a.mp4').write_bytes(b'keep')
    job=worker(tmp_path,targets,lambda *args:MagicMock(upload_single_video=lambda path:(True,'success',None)))
    job.run_batch(job.plan_batch())
    assert not (directory/'a.mp4').exists() and (directory/'publiced'/'a.mp4').read_bytes()==b'keep'
    destination=Path(job.report['files'][0]['archived_path'])
    assert destination.name=='a_重复.mp4' and destination.exists()


def test_submission_boundary_is_durable_before_platform_click(tmp_path):
    directory,targets=setup(tmp_path)
    job=worker(tmp_path,targets[1:],MagicMock())
    jobs=job.plan_batch()
    class Uploader:
        last_outcome='UNCONFIRMED'
        def upload_single_video(self,path):
            self.before_submit({'video_id':'abcdefghijk'})
            disk=MultiTargetLedger(job.root)
            assert disk.result(jobs[0]['digest'],targets[1].key)['status']=='IN_PROGRESS'
            return False,'simulated interruption',{'video_id':'abcdefghijk'}
    job.factory=lambda *args:Uploader()
    job.run_batch(jobs)
    assert (directory/'a.mp4').exists()


def test_legacy_douyin_success_is_migrated_without_reupload(tmp_path):
    directory,targets=setup(tmp_path)
    from src.publisher.douyin.uploader import compute_file_md5
    reportdir=directory/'reports'
    reportdir.mkdir()
    (reportdir/'publish_ledger_20261006.json').write_text(json.dumps([{
        'account_id':targets[0].account.account_id,'status':'SUCCESS','file_hash':compute_file_md5(directory/'a.mp4')}]),encoding='utf-8')
    factory=MagicMock()
    job=worker(tmp_path,targets[:1],factory)
    job.run_batch(job.plan_batch())
    factory.assert_not_called()
    assert job.report['files'][0]['results'][targets[0].key]['status']=='SUCCESS'
    assert (directory/'publiced'/'a.mp4').exists()


def test_corrupt_legacy_ledger_blocks_its_account_but_other_target_continues(tmp_path):
    directory,targets=setup(tmp_path,('a.mp4','b.mp4'))
    reportdir=directory/'reports'
    reportdir.mkdir()
    legacy=reportdir/'publish_ledger_20261006.json'
    legacy.write_text('{broken',encoding='utf-8')
    calls=[]
    def factory(target,log):
        calls.append(target.key)
        return MagicMock(upload_single_video=lambda path:(True,'confirmed',None))
    job=worker(tmp_path,targets,factory)
    job.run_batch(job.plan_batch())
    assert calls==[targets[1].key,targets[1].key]
    assert all(f['results'][targets[0].key]['status']=='CONFIG_REQUIRED' for f in job.report['files'])
    assert str(legacy) in job.report['files'][0]['results'][targets[0].key]['message']
    assert legacy.read_text()=='{broken' and len(list(directory.glob('*.mp4')))==2


def test_disappeared_media_is_not_misreported_as_legacy_ledger_failure(tmp_path):
    directory,targets=setup(tmp_path)
    reportdir=directory/'reports'
    reportdir.mkdir()
    (reportdir/'publish_ledger_20261006.json').write_text('{broken',encoding='utf-8')
    factory=MagicMock()
    job=worker(tmp_path,targets[:1],factory)
    jobs=job.plan_batch()
    (directory/'a.mp4').unlink()
    assert job.attempt(jobs[0],targets[0])=='FILE_CHANGED'
    factory.assert_not_called()
    assert not job.blocked


def test_bom_legacy_success_is_preserved_as_success(tmp_path):
    from src.publisher.douyin.uploader import compute_file_md5
    directory,targets=setup(tmp_path)
    reportdir=directory/'reports'
    reportdir.mkdir()
    (reportdir/'publish_ledger_20261006.json').write_text(json.dumps([{
        'account_id':targets[0].account.account_id,'status':'SUCCESS',
        'file_hash':compute_file_md5(directory/'a.mp4')}]),encoding='utf-8-sig')
    factory=MagicMock()
    job=worker(tmp_path,targets[:1],factory)
    job.run_batch(job.plan_batch())
    factory.assert_not_called()
    assert job.report['files'][0]['results'][targets[0].key]['status']=='SUCCESS'


@pytest.mark.parametrize('case',['matched','wrong_account','wrong_hash','failed_record','real_unknown','manual_review'])
def test_only_proven_legacy_migration_downgrade_is_restored(tmp_path,case):
    from src.publisher.douyin.uploader import compute_file_md5
    from src.publisher.multi_deploy import LEGACY_REVIEW_MESSAGE
    directory,targets=setup(tmp_path)
    targets[0].account.auto_archive=False
    uploader=MagicMock(last_outcome='FAILED')
    uploader.upload_single_video.return_value=(False,'not confirmed',None)
    uploader.reconcile.return_value=False
    factory=MagicMock(return_value=uploader)
    job=worker(tmp_path,targets[:1],factory)
    jobs=job.plan_batch()
    record={'account_id':targets[0].account.account_id,'status':'SUCCESS','file_hash':compute_file_md5(directory/'a.mp4')}
    message=LEGACY_REVIEW_MESSAGE
    if case=='wrong_account':record['account_id']='other'
    if case=='wrong_hash':record['file_hash']='0'*32
    if case=='failed_record':record['status']='FAILED'
    if case=='real_unknown':message='disconnected after submit'
    if case=='manual_review':message='old review'
    job.ledger.set_result(jobs[0]['digest'],targets[0].key,'UNCONFIRMED',message,record)
    job.run_batch(jobs)
    result=job.ledger.result(jobs[0]['digest'],targets[0].key)
    assert result['status']==('SUCCESS' if case=='matched' else 'FAILED2')
    assert result['info']==record
    if case=='matched':
        factory.assert_not_called()
        assert not job.report['files'][0]['results'][targets[0].key]['attempts']
    else:
        assert uploader.upload_single_video.call_count==2



@pytest.mark.parametrize('case',['wrong_account','changed_content','failed_record'])
def test_unmatched_old_records_do_not_skip_new_upload(tmp_path,case):
    from src.publisher.douyin.uploader import compute_file_md5
    directory,targets=setup(tmp_path)
    reportdir=directory/'reports'
    reportdir.mkdir()
    record={'account_id':targets[0].account.account_id,'status':'SUCCESS',
            'file_hash':compute_file_md5(directory/'a.mp4')}
    if case=='wrong_account':record['account_id']='other'
    if case=='changed_content':record['file_hash']='0'*32
    if case=='failed_record':record['status']='FAILED'
    (reportdir/'publish_ledger_20261006.json').write_text(json.dumps([record]),encoding='utf-8')
    uploader=MagicMock(last_outcome='SUCCESS',upload_single_video=lambda path:(True,'confirmed',None))
    factory=MagicMock(return_value=uploader)
    job=worker(tmp_path,targets[:1],factory)
    job.run_batch(job.plan_batch())
    factory.assert_called_once()


def test_youtube_unsupported_media_does_not_open_browser_or_retry(tmp_path):
    directory,targets=setup(tmp_path,('audio.mp3',))
    targets[1].account.media_types=['audio']
    factory=MagicMock()
    job=worker(tmp_path,targets[1:],factory)
    job.run_batch(job.plan_batch())
    factory.assert_not_called()
    result=job.report['files'][0]['results'][targets[1].key]
    assert result['status']=='PERMANENT_REJECT' and not result['attempts']
    assert (directory/'audio.mp3').exists()


@pytest.mark.parametrize('platform', ['douyin','youtube','xiaohongshu','kuaishou','wechat_channels'])
def test_all_platforms_content_hash_survives_rename_restart_and_separates_accounts(tmp_path, platform):
    from dataclasses import replace
    directory, original = setup(tmp_path)
    source = original[0] if platform=='douyin' else original[1]
    target = PublishTarget(platform,replace(source.account,auto_archive=False),source.profile)
    calls=[]
    def factory(t, log):
        def upload(path):
            calls.append((t.key,path.name))
            return True,'confirmed',None
        return MagicMock(upload_single_video=upload)
    first=worker(tmp_path,[target],factory)
    first.run_batch(first.plan_batch())
    digest=first.report['files'][0]['sha256']
    renamed=directory/'renamed.mp4'
    (directory/'a.mp4').rename(renamed)
    second=worker(tmp_path,[target],factory)
    second.run_batch(second.plan_batch())
    assert len(calls)==1
    assert second.report['files'][0]['sha256']==digest
    other=PublishTarget(platform,replace(target.account,account_id='other-account'),str(tmp_path/'other-profile'))
    third=worker(tmp_path,[other],factory)
    third.run_batch(third.plan_batch())
    assert len(calls)==2 and calls[-1][0]==other.key
    renamed.write_bytes(b'changed contents'*180)
    os.utime(renamed,(time.time()-10,time.time()-10))
    fourth=worker(tmp_path,[target],factory)
    fourth.run_batch(fourth.plan_batch())
    assert len(calls)==3
    assert fourth.report['files'][0]['sha256']!=digest


def test_exception_after_submit_boundary_never_retries_unknown_outcome(tmp_path):
    directory,targets=setup(tmp_path)
    calls=[]
    class BrokenUploader:
        def upload_single_video(self,path):
            calls.append(path)
            self.before_submit({'platform':'douyin'})
            raise RuntimeError('connection disappeared after submit')
    first=worker(tmp_path,targets[:1],lambda *args:BrokenUploader())
    first.run_batch(first.plan_batch())
    second=worker(tmp_path,targets[:1],lambda *args:BrokenUploader())
    second.run_batch(second.plan_batch())
    assert len(calls)==1
    assert second.report['files'][0]['results'][targets[0].key]['status']=='RECOVERY_REQUIRED'
    assert (directory/'a.mp4').exists()


def test_shared_scanner_preserves_douyin_selected_media_types(tmp_path):
    directory,targets=setup(tmp_path)
    audio=directory/'audio.mp3'
    audio.write_bytes(b'audio'*400)
    os.utime(audio,(time.time()-10,time.time()-10))
    targets[0].account.media_types=['audio']
    job=worker(tmp_path,targets,MagicMock())
    jobs=job.plan_batch()
    planned={entry['path'].name:[t.platform for t in entry['targets']] for entry in jobs}
    assert planned=={'a.mp4':['youtube'],'audio.mp3':['douyin']}
