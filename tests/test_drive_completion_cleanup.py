"""Late cloud copies are reconciled after a batch without another upload."""
import hashlib
from pathlib import Path

import pytest

from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger
from src.publisher.scan_cleanup import cleanup_completed_cloud_copies
from src.publisher.upload_quota import DailyUploadQuota
from tests.test_drive_shared_download import shared_targets
from tests.test_google_drive import FakeDrive, cloud_file, ingest
from tests.test_scan_cleanup_quota import app


def setup_cloud(tmp_path):
    a,b=shared_targets(tmp_path)
    for t in (a,b):t.account.auto_archive=True
    root=tmp_path/'state';api=FakeDrive()
    ingest(root,api).poll(a);ingest(root,api).poll(b)
    path=next(Path(a.account.target_dir).glob('*.mp4'))
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    return a,b,root,api,path,digest


def test_fixed_task_archives_copy_downloaded_during_upload_without_expanding_uploads(tmp_path):
    a,b,root,api,path,digest=setup_cloud(tmp_path)
    calls=[];results=[]
    class Uploader:
        def __init__(self,target):self.target=target
        def upload_single_video(self,selected):
            self.before_upload()
            calls.append((self.target.key,selected.name))
            if len(calls)==1:
                api.files=[cloud_file('late-copy',version='2')]
                api.content['late-copy']=b'video'*1000
                ingest(root,api).poll(a);ingest(root,api).poll(b)
            return True,'confirmed',None
    worker=MultiDeployWorker([a,b],root=root,interval_seconds=0,file_paths=[path],
        uploader_factory=lambda t,*args:Uploader(t))
    worker.result_changed.connect(results.append)
    worker.run()
    assert worker.report['status']=='COMPLETED'
    assert len(calls)==2 and all(name==path.name for _,name in calls)
    assert len(api.downloads)==2 # One cloud version per physical copy, shared by both accounts.
    assert not list(path.parent.glob('*.mp4'))
    archived=list((path.parent/'publiced').glob('*.mp4'))
    assert len(archived)==2 and not any('_重复' in p.stem for p in archived)
    assert worker.report['source_file_count']==1 and worker.report['target_count']==2
    assert worker.report['success_count']==2
    cleanup=worker.report['completion_cleanup']
    assert len(cleanup)==1 and cleanup[0]['archived_path'] and cleanup[0]['cleanup_only']
    assert all(cleanup[0]['results'][t.key]['status']=='SUCCESS' for t in (a,b))
    assert any(item.get('cleanup_only') for item in results)
    for t in (a,b):
        assert DailyUploadQuota(root).snapshot(t.key)['used']==1
        assert len(worker.report['files'][0]['results'][t.key]['attempts'])==1
    worker.cleanup_completed_copies()
    assert len(calls)==2 and len(worker.report['completion_cleanup'])==1


@pytest.mark.parametrize('other_status',['FAILED2','PENDING','IGNORED'])
def test_incomplete_selected_account_preserves_file_and_displays_history(tmp_path,other_status):
    a,b,root,api,path,digest=setup_cloud(tmp_path)
    ledger=MultiTargetLedger(root)
    ledger.set_result(digest,a.key,'SUCCESS','confirmed')
    ledger.set_result(digest,b.key,other_status,'history')
    original=ledger.path.read_bytes()
    items=cleanup_completed_cloud_copies([a,b],root)
    assert path.exists() and not items[0]['archived_path']
    assert items[0]['results'][a.key]['status']=='SUCCESS'
    assert items[0]['results'][b.key]['status']==other_status
    assert ledger.path.read_bytes()==original
    assert DailyUploadQuota(root).snapshot(a.key)['used']==0


def test_disabled_archive_is_respected_and_ledger_quota_are_unchanged(tmp_path):
    a,b,root,api,path,digest=setup_cloud(tmp_path)
    ledger=MultiTargetLedger(root)
    for t in (a,b):ledger.set_result(digest,t.key,'SUCCESS','confirmed')
    previous=ledger.path.read_bytes()
    b.account.auto_archive=False
    items=cleanup_completed_cloud_copies([a,b],root)
    assert path.exists() and items[0]['archived_path'] is None
    b.account.auto_archive=True
    items=cleanup_completed_cloud_copies([a,b],root)
    assert not path.exists() and Path(items[0]['archived_path']).exists()
    assert Path(items[0]['archived_path']).name==path.name
    assert ledger.path.read_bytes()==previous


def test_untracked_local_copy_and_unready_or_changed_cloud_file_are_preserved(tmp_path):
    a,b,root,api,path,digest=setup_cloud(tmp_path)
    ledger=MultiTargetLedger(root)
    for t in (a,b):ledger.set_result(digest,t.key,'SUCCESS','confirmed')
    local=path.with_name('untracked-local.mp4');local.write_bytes(path.read_bytes())
    marker=path.with_name('.'+path.name+'.shusheng-pending');marker.touch()
    assert cleanup_completed_cloud_copies([a,b],root)==[]
    marker.unlink()
    path.write_bytes(b'changed video')
    assert cleanup_completed_cloud_copies([a,b],root)==[]
    assert path.exists() and local.exists()


def test_cancellation_and_original_task_paths_prevent_cleanup_moves(tmp_path):
    a,b,root,api,path,digest=setup_cloud(tmp_path)
    ledger=MultiTargetLedger(root)
    for t in (a,b):ledger.set_result(digest,t.key,'SUCCESS','confirmed')
    assert cleanup_completed_cloud_copies([a,b],root,stopped=lambda:True)==[]
    assert cleanup_completed_cloud_copies([a,b],root,exclude_paths=[path])==[]
    assert path.exists()


def test_cleanup_failure_keeps_successful_deployment_results(tmp_path):
    a,b,root,api,path,digest=setup_cloud(tmp_path)
    worker=MultiDeployWorker([a,b],root=root,file_paths=[path])
    worker.report['status']='COMPLETED'
    (root/'target_ledger.json').write_text('{broken',encoding='utf-8')
    worker.cleanup_completed_copies()
    assert path.exists() and worker.report['status']=='COMPLETED'
    assert worker.report['completion_cleanup_error']


@pytest.mark.parametrize('identical',[False,True])
def test_cleanup_collision_uses_content_and_keeps_download_consumption_and_cursor(tmp_path,identical):
    from src.publisher.drive_ingest import DriveJournal
    from tests.test_scan_cleanup_quota import media
    a,b,root,api,path,digest=setup_cloud(tmp_path)
    existing=media(path.parent/'publiced',path.name,path.read_bytes() if identical else b'old'*1000)
    journal=DriveJournal(root)
    with journal.connect() as db:
        cursor=db.execute('SELECT * FROM scan_cursor ORDER BY target').fetchall()
        files=db.execute('SELECT identity,path,state,sha256 FROM files ORDER BY identity').fetchall()
    ledger=MultiTargetLedger(root)
    for target in (a,b):ledger.set_result(digest,target.key,'SUCCESS','confirmed')
    previous=ledger.path.read_bytes()
    items=cleanup_completed_cloud_copies([a,b],root)
    expected=existing if identical else existing.with_stem(existing.stem+'_重复')
    assert items[0]['archived_path']==str(expected) and not path.exists()
    assert expected.read_bytes()==b'video'*1000
    assert ledger.path.read_bytes()==previous
    for target in (a,b):
        ingest(root,api).poll(target)
        assert DailyUploadQuota(root).snapshot(target.key)['used']==0
    assert api.downloads==['f1']  # Missing work file after archive is already consumed.
    with journal.connect() as db:
        assert db.execute('SELECT * FROM scan_cursor ORDER BY target').fetchall()==cursor
        assert db.execute('SELECT identity,path,state,sha256 FROM files ORDER BY identity').fetchall()==files
