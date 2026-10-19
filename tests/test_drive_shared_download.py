"""A shared receiving folder stores one cloud version, with separate account results."""
from pathlib import Path
import hashlib
import os
from datetime import date
import pytest

from src.publisher.drive_ingest import DriveJournal
from src.publisher.multi_deploy import MultiTargetLedger, MultiDeployWorker
from tests.test_google_drive import FakeDrive, cloud_file, ingest, target
from src.publisher.google_drive import DriveError, DriveClient
from src.publisher.drive_ingest import canonical_local_path, journal_identity, version_of


def shared_targets(tmp_path):
    a,b=target(tmp_path,'a'),target(tmp_path,'b')
    b.account.target_dir=a.account.target_dir
    return a,b


def test_three_cloud_files_two_accounts_share_three_local_files(tmp_path):
    a,b=shared_targets(tmp_path)
    contents={str(i):(b'video'+str(i).encode())*1000 for i in range(3)}
    api=FakeDrive([cloud_file(str(i),content=contents[str(i)],name=f'clip{i}.mp4') for i in range(3)])
    api.content=contents
    root=tmp_path/'state'
    ingest(root,api).poll(a)
    original={p.name:p.stat().st_mtime_ns for p in Path(a.account.target_dir).glob('*.mp4')}
    ingest(root,api).poll(b)
    assert len(api.downloads)==3
    assert {p.name:p.stat().st_mtime_ns for p in Path(a.account.target_dir).glob('*.mp4')}==original
    journal=DriveJournal(root)
    for file in api.files:
        first=journal.entry(journal.identity(a,file,api.account_identity())[0])
        second=journal.entry(journal.identity(b,file,api.account_identity())[0])
        assert first['path']==second['path']
        assert first['target']!=second['target']
    calls=[]
    class Uploader:
        def __init__(self,key):self.key=key
        def upload_single_video(self,path):
            self.before_upload()
            calls.append((self.key,Path(path).name))
            return True,'confirmed',None
    worker=MultiDeployWorker([a,b],root=root,interval_seconds=0,
        uploader_factory=lambda t,*_:Uploader(t.key))
    worker.ledger=MultiTargetLedger(root)
    worker.run_batch(worker.plan_batch())
    assert len(calls)==6 and len(set(calls))==6
    assert len(list((Path(a.account.target_dir)/'publiced').glob('*.mp4')))==3
    ingest(root,api).poll(a);ingest(root,api).poll(b)
    assert len(api.downloads)==3


def test_shared_file_already_archived_for_second_account_is_not_fetched_again(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    ingest(root,api).poll(a)
    path=next(Path(a.account.target_dir).glob('*.mp4'))
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    MultiTargetLedger(root).set_result(digest,b.key,'SUCCESS','shared batch already published')
    path.rename(path.with_suffix('.archived'))
    ingest(root,api).poll(b)
    assert api.downloads==['f1']
    entry=DriveJournal(root).entry(DriveJournal.identity(b,api.files[0],api.account_identity())[0])
    assert entry['state']=='DOWNLOADED'


def test_account_success_does_not_leak_to_other_account_on_shared_file(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    digest=hashlib.sha256(b'video'*1000).hexdigest()
    MultiTargetLedger(root).set_result(digest,a.key,'SUCCESS','earlier upload')
    ingest(root,api).poll(a);ingest(root,api).poll(b)
    journal=DriveJournal(root)
    assert journal.entry(journal.identity(a,api.files[0],api.account_identity())[0])['duplicate']==1
    assert journal.entry(journal.identity(b,api.files[0],api.account_identity())[0])['duplicate']==0
    assert len(api.downloads)==1


def test_four_platforms_reuse_one_file_when_cloud_metadata_changes_between_polls(tmp_path):
    from dataclasses import replace
    targets=[]
    for platform in ('douyin','xiaohongshu','kuaishou','youtube'):
        item=target(tmp_path,platform)
        account=replace(item.account,platform=platform,target_dir=str(tmp_path/'media'))
        targets.append(type(item)(platform,account,item.profile))
    Path(targets[0].account.target_dir).mkdir()
    api=FakeDrive();root=tmp_path/'state';paths=[]
    for index,item in enumerate(targets,1):
        api.files=[{**api.files[0],'version':str(index),'modifiedTime':f'2026-10-16T00:00:0{index}Z'}]
        ingest(root,api).poll(item)
        paths.append(next(Path(item.account.target_dir).glob('*.mp4')))
        if index==1:mtime=paths[0].stat().st_mtime_ns
        assert paths[0].stat().st_mtime_ns==mtime
    assert api.downloads==['f1'] and len(set(paths))==1
    assert len(list(paths[0].parent.glob('*.mp4')))==1
    journal=DriveJournal(root)
    with journal.connect() as db:
        assert db.execute('SELECT count(DISTINCT target) FROM files').fetchone()[0]==4
        assert db.execute('SELECT count(DISTINCT path) FROM files').fetchone()[0]==1
    api.files=[{**api.files[0],'version':'5'}]
    ingest(root,api).poll(targets[0])  # Same account and a later poll also reuse bytes.
    assert api.downloads==['f1'] and paths[0].stat().st_mtime_ns==mtime


def test_metadata_changes_during_download_do_not_discard_verified_bytes(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    api.metadata=lambda identity:{**api.files[0],'version':'2','modifiedTime':'2026-10-16T04:00:00Z'}
    ingest(root,api).poll(a)
    assert len(list(Path(a.account.target_dir).glob('*.mp4')))==1
    api.files=[api.metadata('f1')]
    ingest(root,api).poll(b)
    assert api.downloads==['f1']


def test_changed_bytes_with_same_cloud_id_still_download_separately(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    ingest(root,api).poll(a)
    api.files=[cloud_file(content=b'other'*1000,version='2')]
    api.content['f1']=b'other'*1000
    ingest(root,api).poll(b)
    assert api.downloads==['f1','f1']
    assert len(list(Path(a.account.target_dir).glob('*.mp4')))==2


def test_metadata_change_recovers_owned_final_file_left_before_journal_commit(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    journal=DriveJournal(root);journal.prepare_account(api.account_identity())
    reserved=journal.reserve(a,api.files[0],api.account_identity())
    path=Path(reserved['path']);path.write_bytes(api.content['f1'])
    marker=path.with_name('.'+path.name+'.shusheng-pending');marker.touch()
    api.files=[{**api.files[0],'version':'2'}]
    ingest(root,api).poll(b)
    assert not api.downloads and not marker.exists()
    assert len(list(path.parent.glob('*.mp4')))==1


def test_untracked_same_name_is_not_adopted_even_when_its_bytes_match(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    untracked=Path(a.account.target_dir)/api.files[0]['name'];untracked.write_bytes(api.content['f1'])
    ingest(root,api).poll(a)
    assert api.downloads==['f1']
    assert len(list(untracked.parent.glob('*.mp4')))==2


def test_changed_or_corrupt_owned_source_is_not_shared_with_other_account(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    ingest(root,api).poll(a)
    original=next(Path(a.account.target_dir).glob('*.mp4'));original.write_bytes(b'wrong'*1000)
    api.files=[{**api.files[0],'version':'2'}]
    ingest(root,api).poll(b)
    assert len(api.downloads)==2 and original.read_bytes()==b'wrong'*1000
    assert len(list(original.parent.glob('*.mp4')))==2


def test_reuse_hash_check_observes_updated_cancellation_callback(tmp_path,monkeypatch):
    import src.publisher.drive_ingest as module
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    ingest(root,api).poll(a)
    api.files=[{**api.files[0],'version':'2'}]
    worker=ingest(root,api)
    cancelled=[False];worker.stopped=lambda:cancelled[0]
    original=module.file_hashes
    def cancel(path,stopped=lambda:False):
        cancelled[0]=True
        return original(path,stopped)
    monkeypatch.setattr(module,'file_hashes',cancel)
    with pytest.raises(InterruptedError):worker.poll(b)
    assert api.downloads==['f1']


def test_other_series_and_new_cloud_copy_are_not_silently_consumed(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    ingest(root,api).poll(a)
    b.account.drive_binding['folder_id']='other-series'
    ingest(root,api).poll(b)
    assert len(api.downloads)==2
    api.files=[cloud_file('new-cloud-id')]
    api.content['new-cloud-id']=b'video'*1000
    ingest(root,api).poll(a)
    assert len(api.downloads)==3


def test_old_unfinished_second_account_reservation_reuses_completed_file(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    journal=DriveJournal(root)
    waiting=journal.reserve(b,api.files[0])
    # Simulate the suffixed destination reserved by the old downloader before a crash.
    with journal.connect() as db:
        db.execute('UPDATE files SET path=? WHERE identity=?',
            (str(Path(b.account.target_dir)/'old_suffix.mp4'),waiting['identity']))
    ingest(root,api).poll(a);ingest(root,api).poll(b)
    assert len(api.downloads)==1
    assert not (Path(b.account.target_dir)/'old_suffix.mp4').exists()
    assert journal.entry(journal.identity(b,api.files[0],api.account_identity())[0])['state']=='DOWNLOADED'


@pytest.mark.skipif(os.name!='nt',reason='Windows directory semantics')
def test_windows_path_case_slashes_and_trailing_separator_share_download(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    b.account.target_dir=a.account.target_dir.upper().replace('\\','/')+'/'
    assert canonical_local_path(a.account.target_dir)==canonical_local_path(b.account.target_dir)
    ingest(root,api).poll(a);ingest(root,api).poll(b)
    assert len(api.downloads)==1
    assert len(list(Path(a.account.target_dir).glob('*.mp4')))==1


def test_switching_google_accounts_separates_files_and_date_cursors(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    ingest(root,api).poll(a)
    journal=DriveJournal(root)
    journal.save_scan_day(a,date(2026,10,14),api.account_identity())
    api.account_identity=lambda:'another-google-account'
    assert journal.resume_day(a,date(2026,10,16),api.account_identity())==date(2026,10,16)
    ingest(root,api).poll(a);ingest(root,api).poll(b)
    assert len(api.downloads)==2
    assert len(list(Path(a.account.target_dir).glob('*.mp4')))==2
    api.account_identity=lambda:'fake-google-account'
    ingest(root,api).poll(a)
    assert len(api.downloads)==2
    assert journal.resume_day(a,date(2026,10,16),api.account_identity())==date(2026,10,16)


def test_another_cloud_account_cannot_take_a_missing_reserved_path(tmp_path):
    a,_=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    journal=DriveJournal(root);journal.prepare_account(api.account_identity())
    old=journal.reserve(a,api.files[0],api.account_identity())
    api.account_identity=lambda:'another-google-account'
    ingest(root,api).poll(a)
    new=journal.entry(journal.identity(a,api.files[0],api.account_identity())[0])
    assert new['path']!=old['path'] and not Path(old['path']).exists()
    api.account_identity=lambda:'fake-google-account'
    ingest(root,api).poll(a)
    assert len(api.downloads)==2 and Path(old['path']).exists()


@pytest.mark.parametrize('error',[DriveError('identity unavailable'),None])
def test_unknown_google_identity_never_uses_history_or_downloads(tmp_path,error):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    ingest(root,api).poll(a)
    def unknown():
        if error:raise error
        return ''
    api.account_identity=unknown
    with pytest.raises(DriveError):ingest(root,api).poll(b)
    assert len(api.downloads)==1
    with DriveJournal(root).connect() as db:
        assert db.execute('SELECT count(*) FROM files').fetchone()[0]==1


def test_legacy_migration_keeps_consumption_cursor_and_single_original_owner(tmp_path):
    a,b=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state'
    journal=DriveJournal(root);file=api.files[0]
    legacy_binding=str(Path(a.account.target_dir).resolve()).upper()+'|series'
    legacy_id=journal_identity(a.key,legacy_binding,file['id'],version_of(file))
    path=str(Path(a.account.target_dir)/file['name'])
    digest=hashlib.sha256(b'video'*1000).hexdigest()
    with journal.connect() as db:
        db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)',
            (legacy_id,a.key,legacy_binding,file['id'],version_of(file),path,'DOWNLOADED',digest,1))
        db.execute('INSERT INTO scan_cursor VALUES (?,?,?)',(a.key,legacy_binding,'2026-10-14'))
    journal.pin_legacy_account(api.account_identity())
    # Even a reconnect before the first post-upgrade poll cannot claim old rows.
    journal.pin_legacy_account('another-google-account')
    journal.prepare_account('another-google-account')
    migrated=journal.entry(journal.identity(a,file,api.account_identity())[0])
    assert migrated['state']=='DOWNLOADED' and migrated['sha256']==digest and migrated['duplicate']==1
    assert migrated['path']==path and not Path(path).exists() # Already archived stays consumed.
    assert journal.resume_day(a,date(2026,10,16),api.account_identity())==date(2026,10,14)
    journal.prepare_account('another-google-account')
    assert journal.entry(journal.identity(a,file,'another-google-account')[0]) is None
    assert journal.resume_day(a,date(2026,10,16),'another-google-account')==date(2026,10,16)
    ingest(root,api).poll(a)
    assert api.downloads==[]


def test_identity_cache_tracks_authorization_generation_and_rejects_races(tmp_path,monkeypatch):
    from src.publisher.google_drive import DriveCredentials
    credentials=DriveCredentials(tmp_path);client=DriveClient(credentials);calls=[]
    def get(route,**params):
        calls.append((route,params))
        return {'user':{'permissionId':'actual-drive-user'}}
    monkeypatch.setattr(client,'get',get)
    original=client.account_identity()
    assert original==client.account_identity() and len(calls)==1
    assert calls==[('about',{'fields':'user(permissionId)'})]
    credentials.generation+=1 # Token replacement; same account must preserve the scope.
    assert client.account_identity()==original and len(calls)==2
    credentials.generation+=1
    monkeypatch.setattr(client,'get',lambda *a,**kw:{'user':{'permissionId':'different-user'}})
    assert client.account_identity()!=original
    credentials.generation+=1
    def raced(*args,**kwargs):
        credentials.disconnect()
        return {'user':{'permissionId':'different-user'}}
    monkeypatch.setattr(client,'get',raced)
    with pytest.raises(InterruptedError):client.account_identity()
    monkeypatch.setattr(client,'get',lambda *a,**kw:{'user':{}})
    with pytest.raises(DriveError):client.account_identity()


@pytest.mark.skipif(os.name!='nt',reason='Windows directory semantics')
def test_case_variants_in_legacy_history_merge_without_losing_completed_state(tmp_path):
    a,_=shared_targets(tmp_path);api=FakeDrive();root=tmp_path/'state';journal=DriveJournal(root)
    file=api.files[0];path=str(Path(a.account.target_dir)/file['name'])
    digest=hashlib.sha256(b'video'*1000).hexdigest()
    for binding,state,day in [(str(Path(a.account.target_dir).resolve())+'|series','FETCHING','2026-10-15'),
                              (str(Path(a.account.target_dir).resolve()).upper()+'|series','DOWNLOADED','2026-10-14')]:
        identity=journal_identity(a.key,binding,file['id'],version_of(file))
        with journal.connect() as db:
            db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)',
                (identity,a.key,binding,file['id'],version_of(file),path,state,digest,1))
            db.execute('INSERT INTO scan_cursor VALUES (?,?,?)',(a.key,binding,day))
    journal.prepare_account(api.account_identity())
    entry=journal.entry(journal.identity(a,file,api.account_identity())[0])
    assert entry['state']=='DOWNLOADED' and entry['sha256']==digest and entry['duplicate']==1
    assert journal.resume_day(a,date(2026,10,16),api.account_identity())==date(2026,10,14)
    with journal.connect() as db:
        assert db.execute('SELECT count(*) FROM files').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM scan_cursor').fetchone()[0]==1
