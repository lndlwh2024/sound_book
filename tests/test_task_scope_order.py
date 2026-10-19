"""Selected-account completion and deterministic per-content scheduling."""
import hashlib
from dataclasses import replace
from pathlib import Path

from tests.test_scan_cleanup_quota import app, media, targets, task
from src.publisher.multi_deploy import MultiTargetLedger
from src.publisher.platforms import CHANNELS
from src.publisher.scan_cleanup import ScanCleanupWorker
from src.publisher.creator_studio import XiaohongshuUploader
from src.publisher.upload_quota import DailyUploadQuota


def test_selected_success_archives_despite_old_full_plan_and_unselected_failures(tmp_path):
    path=media(tmp_path/'media')
    path.with_suffix('.srt').write_text('subtitle')
    group=targets(path.parent)
    selected=group[1:3]
    for t in selected:t.account.auto_archive=True
    root=tmp_path/'state';ledger=MultiTargetLedger(root)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    ledger.register_plan(digest,group,fixed=True)
    for t,state in zip(group,['FAILED2','SUCCESS','IGNORED','PENDING']):
        ledger.set_result(digest,t.key,state,'history')
    worker=task(root,selected,lambda *args:(_ for _ in ()).throw(AssertionError('must dedup')),group)
    worker.run_batch(worker.plan_batch())
    assert not path.exists() and (path.parent/'published'/path.name).exists()
    assert (path.parent/'published'/path.with_suffix('.srt').name).exists()
    assert worker.report['source_success_count']==1
    assert worker.report['target_count']==2 and worker.report['failure_count']==0
    assert worker.report['success_count']==1 and worker.report['ignored_count']==1
    assert worker.ledger.data['plans'][digest]['required']==[t.key for t in selected]
    assert worker.ledger.result(digest,group[2].key)['status']=='IGNORED'
    assert worker.ledger.result(digest,group[0].key)['status']=='FAILED2'


def test_startup_cleanup_selected_scope_preserves_other_columns_and_cache(tmp_path):
    path=media(tmp_path/'media');group=targets(path.parent);root=tmp_path/'state';cache={}
    digest=hashlib.sha256(path.read_bytes()).hexdigest();ledger=MultiTargetLedger(root)
    for t in group[1:3]:ledger.set_result(digest,t.key,'SUCCESS','history')
    manual=ScanCleanupWorker(group[1:3],group,root,cache=cache,history_targets=group)
    manual.run()
    assert not manual.error and path.exists()
    startup=ScanCleanupWorker(group[1:3],group[1:3],root,cache=cache,history_targets=group)
    logs=[];startup.log.connect(logs.append);startup.run()
    assert not startup.error and not path.exists()
    assert any('沿用已完成的扫描清理摘要' in text for text in logs)
    assert startup.items[0]['results'][group[3].key]['status']=='PENDING'


def test_corrupt_unselected_douyin_history_does_not_block_selected_cleanup(tmp_path):
    path=media(tmp_path/'media');group=targets(path.parent);root=tmp_path/'state'
    reports=path.parent/'reports';reports.mkdir()
    (reports/'publish_ledger_20261006.json').write_text('{broken')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    MultiTargetLedger(root).set_result(digest,group[1].key,'SUCCESS','confirmed')
    scanner=ScanCleanupWorker(group[1:2],group[1:2],root,history_targets=group);scanner.run()
    assert not scanner.error and not path.exists()
    assert scanner.items[0]['results'][group[0].key]['status']=='CONFIG_REQUIRED'


def test_file_then_fixed_platforms_then_local_retry_before_next_file(tmp_path):
    first=media(tmp_path/'media','a.mp4',b'a'*3000)
    media(first.parent,'b.mp4',b'b'*3000)
    group=targets(first.parent);calls=[]
    class Uploader:
        def __init__(self,target):self.target=target
        def upload_single_video(self,path):
            key=(path.name,self.target.platform);calls.append(key)
            success=self.target.platform!='xiaohongshu' or calls.count(key)==2
            self.last_outcome='SUCCESS' if success else 'FAILED'
            return success,'result',None
    worker=task(tmp_path/'state',list(reversed(group)),lambda t,log:Uploader(t),group)
    worker.run_batch(worker.plan_batch())
    platforms=[*CHANNELS,'xiaohongshu']
    assert calls==[(name,p) for name in ('a.mp4','b.mp4') for p in platforms]
    worker.run_batch(worker.plan_batch())
    assert len(calls)==2*len(platforms)  # No completed copies/targets are transferred again.


def test_same_content_different_directories_finishes_accounts_before_next_digest(tmp_path):
    a=media(tmp_path/'xhs','a.mp4',b'a'*3000)
    b=media(tmp_path/'ks','copy-a.mp4',a.read_bytes())
    media(a.parent,'z.mp4',b'z'*3000)
    media(b.parent,'copy-z.mp4',b'z'*3000)
    group=targets(a.parent)[1:3]
    group[1]=replace(group[1],account=replace(group[1].account,target_dir=str(b.parent)))
    calls=[]
    class Uploader:
        def __init__(self,target):self.target=target
        def upload_single_video(self,path):
            calls.append((path.read_bytes()[0],self.target.platform))
            return True,'result',None
    worker=task(tmp_path/'state',group,lambda t,log:Uploader(t))
    worker.run_batch(worker.plan_batch())
    assert calls==[(ord(content),p) for content in ('a','z') for p in ('xiaohongshu','kuaishou')]
    assert len(worker.report['files'])==4


def test_missing_selected_copy_is_not_uploaded_from_another_account_directory(tmp_path):
    path=media(tmp_path/'xhs');group=targets(path.parent)[1:3]
    empty=tmp_path/'ks';empty.mkdir()
    group[1].account.target_dir=str(empty)
    for t in group:t.account.auto_archive=True
    calls=[]
    class Uploader:
        def upload_single_video(self,path):return True,'result',None
    worker=task(tmp_path/'state',group,lambda t,log:(calls.append(t.key) or Uploader()))
    worker.run_batch(worker.plan_batch())
    assert calls==[group[0].key] and path.exists()
    assert worker.report['files'][0]['results'][group[1].key]['status']=='PENDING'


def test_only_selected_auto_archive_flags_control_task_completion(tmp_path):
    path=media(tmp_path/'media');group=targets(path.parent)
    group[1].account.auto_archive=True
    class Uploader:
        def upload_single_video(self,path):return True,'result',None
    worker=task(tmp_path/'state',group[1:2],lambda *args:Uploader(),group)
    worker.run_batch(worker.plan_batch())
    assert not path.exists()


def test_late_saved_xhs_ack_recovers_after_budget_exhaustion_without_new_upload(tmp_path):
    path=media(tmp_path/'media');target=targets(path.parent)[1];root=tmp_path/'state'
    info={'platform':'xiaohongshu','account_id':target.account.account_id,'submitted':True,
        'confirmation':{'kind':'publish_response','endpoint':'https://edith.xiaohongshu.com/web_api/sns/v2/note',
            'accepted':True,'business_code':0}}
    def factory(t,log):
        return XiaohongshuUploader(t.account,t.profile,root/'dom_reports')
    worker=task(root,[target],factory)
    job=worker.plan_batch()[0]
    worker.attempts_by_target[(job['digest'],target.key)].extend([{'number':1},{'number':2}])
    worker.ledger.set_result(job['digest'],target.key,'FAILED2','closed',info)
    job['item']['results'][target.key]['quota_message']='old daily wait'
    assert worker.attempt(job,target)=='SUCCESS'
    assert 'quota_message' not in job['item']['results'][target.key]
    assert len(job['item']['results'][target.key]['attempts'])==2
    assert DailyUploadQuota(root).snapshot(target.key)['used']==0


def test_xhs_ack_is_saved_as_success_before_page_exception_and_restart(tmp_path):
    path=media(tmp_path/'media');target=targets(path.parent)[1];root=tmp_path/'state';calls=[]
    class Uploader(XiaohongshuUploader):
        def upload_single_video(self,path):
            self.before_upload();calls.append(path)
            info={'platform':'xiaohongshu','account_id':self.account.account_id,'submitted':True,
                'confirmation':{'kind':'publish_response','endpoint':'https://edith.xiaohongshu.com/web_api/sns/v2/note',
                    'accepted':True,'business_code':0}}
            self.before_submit(info)
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            assert MultiTargetLedger(root).result(digest,target.key)['status']=='SUCCESS'
            raise RuntimeError('browser closed after acknowledgement')
    worker=task(root,[target],lambda t,log:Uploader(t.account,t.profile,root/'dom_reports'))
    worker.run_batch(worker.plan_batch())
    assert worker.report['files'][0]['results'][target.key]['status']=='SUCCESS'
    restarted=task(root,[target],lambda *args:(_ for _ in ()).throw(AssertionError('duplicate upload')))
    restarted.run_batch(restarted.plan_batch())
    assert len(calls)==1 and DailyUploadQuota(root).snapshot(target.key)['used']==1
