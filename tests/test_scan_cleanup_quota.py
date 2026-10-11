"""Local-only acceptance tests for cleanup, retry budgets and durable account quotas."""
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from src.publisher.platforms import ChannelAccount,CHANNELS
from src.publisher.multi_deploy import PublishTarget,MultiDeployWorker,MultiTargetLedger
from src.publisher.scan_cleanup import ScanCleanupWorker
from src.publisher.upload_quota import DailyUploadQuota,DailyQuotaReached,CHINA_TIME


@pytest.fixture(scope='module',autouse=True)
def app():
    return QApplication.instance() or QApplication([])


def media(directory,name='clip.mp4',content=b'video'*500):
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/name
    path.write_bytes(content)
    os.utime(path,(time.time()-10,time.time()-10))
    return path


def targets(directory):
    return [PublishTarget(platform,ChannelAccount(str(index+1)*32,platform,platform,
        target_dir=str(directory),auto_archive=False),str(directory/platform))
        for index,platform in enumerate(CHANNELS)]


def task(root,selected,factory,group=None):
    worker=MultiDeployWorker(selected,root=root,uploader_factory=factory,interval_seconds=0,archive_targets=group)
    worker.ledger=MultiTargetLedger(root)
    return worker


def test_quota_restart_account_isolation_live_change_and_midnight(tmp_path):
    clock=[datetime(2026,10,11,23,59,tzinfo=CHINA_TIME)]
    quota=DailyUploadQuota(tmp_path,lambda:clock[0])
    assert quota.snapshot('youtube:a')['limit']==10
    quota.set_limit('youtube:a',2)
    for i in range(2):quota.reserve('youtube:a','hash','task',str(i))
    restarted=DailyUploadQuota(tmp_path,lambda:clock[0])
    with pytest.raises(DailyQuotaReached):restarted.reserve('youtube:a','other','new-task','2')
    assert restarted.snapshot('youtube:b')['used']==0
    assert restarted.snapshot('douyin:a')['used']==0
    restarted.set_limit('youtube:a',3)
    restarted.reserve('youtube:a','other','new-task','2')
    restarted.reserve('youtube:a','other','new-task','2')  # Idempotent boundary callback.
    assert restarted.snapshot('youtube:a')['used']==3
    restarted.set_limit('youtube:a',1)
    assert restarted.snapshot('youtube:a')['remaining']==0
    clock[0]+=timedelta(minutes=2)
    assert restarted.snapshot('youtube:a')['used']==0
    restarted.reserve('youtube:a','hash','another-task','3')
    assert restarted.snapshot('youtube:a')['used']==1
    restarted.set_limit('youtube:a',0)
    with pytest.raises(DailyQuotaReached):restarted.reserve('youtube:a','hash','task','4')


def test_concurrent_reservations_cannot_exceed_cap(tmp_path):
    quota=DailyUploadQuota(tmp_path)
    quota.set_limit('kuaishou:a',3)
    def reserve(i):
        try:
            DailyUploadQuota(tmp_path).reserve('kuaishou:a','hash','task',str(i))
            return True
        except DailyQuotaReached:return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve,range(12)))==3
    assert quota.snapshot('kuaishou:a')['used']==3


def test_history_import_counts_proven_uploads_once_not_dedup_or_preparation(tmp_path):
    folder=tmp_path/'publish_reports';folder.mkdir()
    day=datetime.now(CHINA_TIME).isoformat()
    report={'task_id':'old','files':[{'sha256':'a'*64,'results':{'youtube:a':{'attempts':[
        {'status':'SUCCESS','started_at':day},
        {'status':'FAILED','started_at':day,'logs':[{'message':'[选择视频] clip.mp4'}]},
        {'status':'CONFIG_REQUIRED','started_at':day}]},'douyin:a':{'status':'SUCCESS','attempts':[]}}}]}
    (folder/'old.json').write_text(json.dumps(report))
    quota=DailyUploadQuota(tmp_path)
    quota.bootstrap_history();quota.bootstrap_history()
    assert quota.snapshot('youtube:a')['used']==2
    assert quota.snapshot('douyin:a')['used']==0


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_scan_recognizes_renamed_copied_history_per_account(tmp_path,platform):
    path=media(tmp_path/'copied','renamed.mp4')
    target=next(t for t in targets(path.parent) if t.platform==platform)
    other=replace(target,account=replace(target.account,account_id='f'*32))
    ledger=MultiTargetLedger(tmp_path/'state')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    ledger.set_result(digest,target.key,'SUCCESS','confirmed on old directory')
    scanner=ScanCleanupWorker([target],[target,other],tmp_path/'state')
    scanner.run()
    assert not scanner.error and path.exists()
    result=scanner.items[0]['results']
    assert result[target.key]['status']=='SUCCESS'
    assert result[other.key]['status']=='PENDING'
    assert not (path.parent/'published').exists()


def legacy_report(target,content,**changes):
    directory=Path(target.account.target_dir)/'reports'
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'publish_ledger_20261006.json'
    record={'account_id':target.account.account_id,'status':'SUCCESS',
        'file_hash':hashlib.md5(content).hexdigest(),**changes}
    path.write_text(json.dumps([record]),encoding='utf-8-sig')
    return path


@pytest.mark.parametrize('selected',[(1,),(2,),(1,2)])
def test_other_platform_cleanup_imports_archive_only_douyin_history(tmp_path,selected):
    path=media(tmp_path/'copies','renamed.mp4')
    group=targets(path.parent)
    group[0].account.target_dir=str(tmp_path/'original-douyin-directory')
    report=legacy_report(group[0],path.read_bytes())
    original=report.read_bytes()
    root=tmp_path/'state'
    scanner=ScanCleanupWorker([group[i] for i in selected],group,root)
    scanner.run()
    assert not scanner.error and path.exists()
    result=scanner.items[0]['results'][group[0].key]
    assert result['status']=='SUCCESS' and result['attempts']==[]
    assert MultiTargetLedger(root).result(scanner.items[0]['sha256'],group[0].key)['status']=='SUCCESS'
    assert DailyUploadQuota(root).snapshot(group[0].key)['used']==0
    assert report.read_bytes()==original


@pytest.mark.parametrize('changes',[{'account_id':'other'}, {'file_hash':'0'*32}, {'status':'FAILED'}])
def test_archive_only_legacy_records_require_correct_account_hash_and_success(tmp_path,changes):
    path=media(tmp_path/'media');group=targets(path.parent)
    legacy_report(group[0],path.read_bytes(),**changes)
    scanner=ScanCleanupWorker(group[1:2],group,tmp_path/'state');scanner.run()
    assert not scanner.error and path.exists()
    assert scanner.items[0]['results'][group[0].key]['status']=='PENDING'


def test_legacy_report_change_invalidates_cache_and_repairs_converted_migration(tmp_path):
    from src.publisher.multi_deploy import LEGACY_FAILED_MESSAGE
    path=media(tmp_path/'copies');group=targets(path.parent);root=tmp_path/'state';cache={}
    group[0].account.target_dir=str(tmp_path/'old-account')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    record={'account_id':group[0].account.account_id,'status':'SUCCESS','file_hash':hashlib.md5(path.read_bytes()).hexdigest()}
    MultiTargetLedger(root).set_result(digest,group[0].key,'FAILED',LEGACY_FAILED_MESSAGE,record)
    first=ScanCleanupWorker(group[1:2],group,root,cache=cache);first.run()
    assert not first.error and first.items[0]['results'][group[0].key]['status']=='PENDING'
    # Embedded info alone is insufficient. A matching original success ledger is required.
    legacy_report(group[0],path.read_bytes())
    second=ScanCleanupWorker(group[1:2],group,root,cache=cache);second.run()
    assert not second.error and second.items is not first.items
    assert second.items[0]['results'][group[0].key]['status']=='SUCCESS'


def test_explicit_review_failure_is_not_overridden_by_legacy_import(tmp_path):
    path=media(tmp_path/'media');group=targets(path.parent);root=tmp_path/'state'
    legacy_report(group[0],path.read_bytes())
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    MultiTargetLedger(root).set_result(digest,group[0].key,'FAILED','人工更正为失败')
    scanner=ScanCleanupWorker(group[1:2],group,root);scanner.run()
    assert not scanner.error and scanner.items[0]['results'][group[0].key]['status']=='PENDING'
    assert MultiTargetLedger(root).result(digest,group[0].key)['status']=='FAILED'


def test_complete_fixed_group_archives_after_restoring_archive_only_success(tmp_path):
    path=media(tmp_path/'media');group=targets(path.parent);root=tmp_path/'state'
    legacy_report(group[0],path.read_bytes())
    digest=hashlib.sha256(path.read_bytes()).hexdigest();ledger=MultiTargetLedger(root)
    for t in group[1:]:ledger.set_result(digest,t.key,'SUCCESS','confirmed')
    scanner=ScanCleanupWorker(group[1:2],group,root);scanner.run()
    assert not scanner.error and not path.exists()
    assert Path(scanner.items[0]['archived_path']).exists()
    assert MultiTargetLedger(root).result(digest,group[0].key)['status']=='SUCCESS'


@pytest.mark.parametrize('case',['success','corrupt','late_report'])
def test_worker_reconciles_archive_only_history_without_uploading_that_account(tmp_path,case):
    path=media(tmp_path/'media');group=targets(path.parent);root=tmp_path/'state'
    calls=[]
    class Uploader:
        def upload_single_video(self,path):return True,'confirmed',None
    def factory(target,log):calls.append(target.key);return Uploader()
    if case!='late_report':
        report=legacy_report(group[0],path.read_bytes())
        if case=='corrupt':report.write_text('{broken')
    worker=task(root,group[1:2],factory,group)
    jobs=worker.plan_batch()
    if case=='late_report':
        assert jobs[0]['item']['results'][group[0].key]['status']=='PENDING'
        legacy_report(group[0],path.read_bytes())
        assert worker.plan_batch()[0] is jobs[0]
    assert jobs[0]['item']['results'][group[0].key]['status']==('CONFIG_REQUIRED' if case=='corrupt' else 'SUCCESS')
    worker.run_batch(jobs)
    assert calls==[group[1].key] and path.exists()
    assert DailyUploadQuota(root).snapshot(group[0].key)['used']==0


def test_cleanup_fixed_full_group_ignored_counts_complete_even_auto_archive_off(tmp_path):
    path=media(tmp_path/'media')
    group=targets(path.parent)
    root=tmp_path/'state'
    ledger=MultiTargetLedger(root)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    for t in group:ledger.set_result(digest,t.key,'IGNORED' if t.platform=='kuaishou' else 'SUCCESS','history')
    scanner=ScanCleanupWorker(group[:1],group,root)
    scanner.run()
    assert not scanner.error and not path.exists()
    assert (path.parent/'published'/path.name).exists()
    assert MultiTargetLedger(root).result(digest,group[2].key)['status']=='IGNORED'
    assert len(MultiTargetLedger(root).data['plans'][digest]['required'])==4


def test_old_failed_unknown_are_pending_without_mutating_history_report(tmp_path):
    path=media(tmp_path/'media')
    group=targets(path.parent);root=tmp_path/'state'
    ledger=MultiTargetLedger(root);digest=hashlib.sha256(path.read_bytes()).hexdigest()
    for t,state in zip(group,['SUCCESS','FAILED2','UNCONFIRMED','CANCELLED']):
        ledger.set_result(digest,t.key,state,'old',{'video_id':'abcdefghijk'})
    folder=root/'publish_reports';folder.mkdir()
    historic='{"files": []}'
    (folder/'history.json').write_text(historic)
    scanner=ScanCleanupWorker(group[:1],group,root);scanner.run()
    assert not scanner.error and path.exists()
    assert [r['status'] for r in scanner.items[0]['results'].values()]==['SUCCESS','PENDING','PENDING','PENDING']
    assert MultiTargetLedger(root).result(digest,group[2].key)['status']=='FAILED'
    assert (folder/'history.json').read_text()==historic


def test_cleanup_cache_reused_but_files_group_or_history_changes_invalidate(tmp_path):
    path=media(tmp_path/'media');group=targets(path.parent);root=tmp_path/'state';cache={}
    first=ScanCleanupWorker(group[:1],group,root,cache=cache);first.run()
    second=ScanCleanupWorker(group[:1],group,root,cache=cache);second.run()
    assert second.items is first.items
    path.write_bytes(b'new video'*400);os.utime(path,(time.time()-10,time.time()-10))
    third=ScanCleanupWorker(group[:1],group,root,cache=cache);third.run()
    assert third.items[0]['sha256']!=first.items[0]['sha256']
    ledger=MultiTargetLedger(root)
    ledger.set_result(third.items[0]['sha256'],group[0].key,'SUCCESS','now confirmed')
    fourth=ScanCleanupWorker(group[:1],group,root,cache=cache);fourth.run()
    assert fourth.items[0]['results'][group[0].key]['status']=='SUCCESS'
    fifth=ScanCleanupWorker(group[:1],group[:1],root,cache=cache);fifth.run()
    assert not path.exists() and fifth.items[0]['archived_path']


def test_cleanup_cancel_invalid_root_and_corrupt_ledger_prevent_moves(tmp_path):
    path=media(tmp_path/'media');group=targets(path.parent);root=tmp_path/'state'
    cancelled=ScanCleanupWorker(group,group,root);cancelled.stop();cancelled.run()
    assert cancelled.cancelled and path.exists()
    root.mkdir(exist_ok=True);(root/'target_ledger.json').write_text('{broken')
    broken=ScanCleanupWorker(group,group,root);broken.run()
    assert broken.error and path.exists()
    invalid=replace(group[0],account=replace(group[0].account,target_dir=str(tmp_path/'missing')))
    scanner=ScanCleanupWorker([invalid],group,root);scanner.run()
    assert scanner.error and path.exists()


def test_archive_collision_preserves_existing_media_and_subtitle(tmp_path):
    path=media(tmp_path/'media');group=targets(path.parent)[:1];root=tmp_path/'state'
    path.with_suffix('.srt').write_text('new subtitle')
    published=path.parent/'published';published.mkdir()
    (published/path.name).write_bytes(b'old')
    (published/path.with_suffix('.srt').name).write_text('old subtitle')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    MultiTargetLedger(root).set_result(digest,group[0].key,'SUCCESS','old')
    scanner=ScanCleanupWorker(group,group,root);scanner.run()
    dest=Path(scanner.items[0]['archived_path'])
    assert not scanner.error and dest.name!=path.name and not path.exists()
    assert dest.with_suffix('.srt').read_text()=='new subtitle'
    assert (published/path.name).read_bytes()==b'old'
    assert (published/path.with_suffix('.srt').name).read_text()=='old subtitle'


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_duplicate_copies_share_two_attempts_poll_does_not_reset_new_task_does(tmp_path,platform):
    path=media(tmp_path/'media');media(path.parent,'copy.mp4',path.read_bytes())
    target=next(t for t in targets(path.parent) if t.platform==platform);calls=[]
    class Uploader:
        supports_upload_boundary=True
        last_outcome='UNCONFIRMED'
        def upload_single_video(self,path):
            self.before_upload();calls.append(path)
            self.before_submit({'platform':platform})
            raise RuntimeError('after submit disconnected')
    root=tmp_path/'state'
    first=task(root,[target],lambda *args:Uploader())
    first.run_batch(first.plan_batch());first.run_batch(first.plan_batch())
    assert len(calls)==2
    assert all(item['results'][target.key]['status']=='FAILED2' for item in first.report['files'])
    assert first.quota.snapshot(target.key)['used']==2
    second=task(root,[target],lambda *args:Uploader())
    jobs=second.plan_batch()
    assert all(j['item']['results'][target.key]['status']=='PENDING' for j in jobs)
    second.run_batch(jobs)
    assert len(calls)==4 and second.quota.snapshot(target.key)['used']==4


def test_failed_retry_waits_for_quota_then_rollover_without_resetting_failure2(tmp_path):
    path=media(tmp_path/'media');target=targets(path.parent)[0]
    target.account.daily_upload_limit=1;calls=[]
    class Uploader:
        supports_upload_boundary=True
        last_outcome='FAILED'
        def upload_single_video(self,path):
            self.before_upload();calls.append(path)
            return False,'failed',None
    worker=task(tmp_path/'state',[target],lambda *args:Uploader())
    stamp=[datetime(2026,10,11,23,59,tzinfo=CHINA_TIME)]
    worker.quota=DailyUploadQuota(worker.root,lambda:stamp[0])
    worker.run_batch(worker.plan_batch())
    item=worker.report['files'][0]['results'][target.key]
    assert len(calls)==1 and item['status']=='FAILED1' and item['quota_message']
    stamp[0]+=timedelta(days=1)
    worker.run_batch(worker.plan_batch())
    assert len(calls)==2 and item['status']=='FAILED2'
    worker.quota.set_limit(target.key,20)
    worker.run_batch(worker.plan_batch())
    assert len(calls)==2


def test_cap_only_stops_one_account_raise_limit_resumes_same_task(tmp_path):
    path=media(tmp_path/'media');media(path.parent,'other.mp4',b'different'*400)
    a,b=targets(path.parent)[:2];a.account.daily_upload_limit=1
    calls=[]
    class Uploader:
        supports_upload_boundary=True
        def __init__(self,target):self.target=target
        def upload_single_video(self,path):
            self.before_upload();calls.append(self.target.key)
            return True,'confirmed',None
    worker=task(tmp_path/'state',[a,b],lambda target,*args:Uploader(target))
    worker.run_batch(worker.plan_batch())
    assert calls.count(a.key)==1 and calls.count(b.key)==2
    worker.quota.set_limit(a.key,2)
    worker.run_batch(worker.plan_batch())
    assert calls.count(a.key)==2 and calls.count(b.key)==2


def test_login_failure_and_stop_before_file_input_spend_no_quota(tmp_path):
    path=media(tmp_path/'media');target=targets(path.parent)[0]
    class Uploader:
        supports_upload_boundary=True
        last_outcome='AUTH_REQUIRED'
        def upload_single_video(self,path):return False,'please login',None
    worker=task(tmp_path/'state',[target],lambda *args:Uploader())
    worker.run_batch(worker.plan_batch())
    assert worker.quota.snapshot(target.key)['used']==0
    assert not worker.report['files'][0]['results'][target.key]['attempts']
    class Stopped:
        supports_upload_boundary=True
        last_outcome='CANCELLED'
        def upload_single_video(self,path):
            other.stop()
            return False,'stopped before input',None
    other=task(tmp_path/'state',[target],lambda *args:Stopped())
    other.run_batch(other.plan_batch())
    assert other.quota.snapshot(target.key)['used']==0


def test_automatic_confirmation_skips_retry_and_spends_no_quota(tmp_path):
    path=media(tmp_path/'media');target=targets(path.parent)[3];calls=[]
    class Uploader:
        def reconcile(self,info):return info.get('video_id')=='abcdefghijk'
        def upload_single_video(self,path):calls.append(path)
    worker=task(tmp_path/'state',[target],lambda *args:Uploader())
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    worker.ledger.set_result(digest,target.key,'UNCONFIRMED','old',{'video_id':'abcdefghijk'})
    worker.run_batch(worker.plan_batch())
    assert not calls and worker.quota.snapshot(target.key)['used']==0
    assert worker.ledger.result(digest,target.key)['status']=='SUCCESS'


def test_stop_at_upload_hook_is_pending_and_does_not_spend_budget(tmp_path):
    path=media(tmp_path/'media');target=targets(path.parent)[0]
    class Uploader:
        supports_upload_boundary=True
        def upload_single_video(self,path):
            worker.stop()
            self.before_upload()
            raise AssertionError('must not reach transfer')
    worker=task(tmp_path/'state',[target],lambda *args:Uploader())
    worker.run_batch(worker.plan_batch())
    result=worker.report['files'][0]['results'][target.key]
    assert result['status']=='CANCELLED' and not result['attempts']
    assert worker.quota.snapshot(target.key)['used']==0


def test_resumed_deferred_account_checks_dom_before_upload(tmp_path,monkeypatch):
    from src.publisher import preflight
    path=media(tmp_path/'media');target=targets(path.parent)[0];calls=[]
    monkeypatch.setattr(preflight,'prerequisite_results',lambda *args:[
        preflight.PreflightResult(target.key,'account','CONFIG_REQUIRED','missing DOM')])
    worker=task(tmp_path/'state',[target],lambda *args:calls.append(args))
    worker.deferred_preflight={target.key}
    worker.run_batch(worker.plan_batch())
    assert not calls and worker.quota.snapshot(target.key)['used']==0
    assert worker.report['files'][0]['results'][target.key]['status']=='CONFIG_REQUIRED'


def test_corrupt_counter_database_prevents_transfer(tmp_path):
    path=media(tmp_path/'media');target=targets(path.parent)[0];calls=[]
    worker=task(tmp_path/'state',[target],lambda *args:calls.append(args))
    worker.quota.path.parent.mkdir(exist_ok=True)
    worker.quota.path.write_bytes(b'corrupt sqlite')
    worker.file_paths={str(path.resolve())}
    worker.run()
    assert not calls and worker.report['status']=='FAILED'
    assert path.exists() and worker.quota.path.read_bytes()==b'corrupt sqlite'
