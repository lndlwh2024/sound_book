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
    assert (directory/'published'/'a.mp4').exists()
    report = json.loads(job.report_path.read_text(encoding='utf-8'))
    assert report['source_file_count']==1 and report['target_count']==2 and report['success_count']==2
    assert len(report['files'][0]['results'][targets[0].key]['attempts'])==1
    assert len(report['files'][0]['results'][targets[1].key]['attempts'])==2


def test_restart_subset_preserves_full_plan_and_skips_prior_success(tmp_path):
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
    assert (directory/'a.mp4').exists()  # Unselected failed YouTube target remains required.
    item=second.report['files'][0]
    assert item['results'][targets[1].key]['status']=='FAILED'
    def successful(target, log):
        return MagicMock(upload_single_video=lambda path:(True,'success',None))
    third=worker(tmp_path,targets[1:],successful)
    third.run_batch(third.plan_batch())
    assert (directory/'published'/'a.mp4').exists()


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


@pytest.mark.parametrize('state',['PERMANENT_REJECT','DURATION_LIMIT','UNCONFIRMED'])
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
    assert (directory/'published'/'a.mp4').exists()


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
    (directory/'published').mkdir()
    (directory/'published'/'a.mp4').write_bytes(b'keep')
    job=worker(tmp_path,targets,lambda *args:MagicMock(upload_single_video=lambda path:(True,'success',None)))
    job.run_batch(job.plan_batch())
    assert (directory/'a.mp4').exists() and (directory/'published'/'a.mp4').read_bytes()==b'keep'
    assert job.report['files'][0]['archive_error']


def test_submission_boundary_is_durable_before_platform_click(tmp_path):
    directory,targets=setup(tmp_path)
    job=worker(tmp_path,targets[1:],MagicMock())
    jobs=job.plan_batch()
    class Uploader:
        last_outcome='UNCONFIRMED'
        def upload_single_video(self,path):
            self.before_submit({'video_id':'abcdefghijk'})
            disk=MultiTargetLedger(job.root)
            assert disk.result(jobs[0]['digest'],targets[1].key)['status']=='UNCONFIRMED'
            return False,'simulated interruption',{'video_id':'abcdefghijk'}
    job.factory=lambda *args:Uploader()
    job.run_batch(jobs)
    assert (directory/'a.mp4').exists()


def test_legacy_douyin_record_requires_review_and_does_not_reupload(tmp_path):
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
    assert job.report['files'][0]['results'][targets[0].key]['status']=='UNCONFIRMED'
    assert (directory/'a.mp4').exists()


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


def test_bom_legacy_success_is_preserved_and_requires_review(tmp_path):
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
    assert job.report['files'][0]['results'][targets[0].key]['status']=='UNCONFIRMED'


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


@pytest.mark.parametrize('platform', ['douyin','youtube','xiaohongshu','kuaishou'])
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
    assert second.report['files'][0]['results'][targets[0].key]['status']=='UNCONFIRMED'
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
