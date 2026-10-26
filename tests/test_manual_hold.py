"""Persistent local holds use verified Drive scope and never imply publishing success."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.test_google_drive import FakeDrive, cloud_file, ingest, target
from src.publisher.drive_ingest import DriveJournal
from src.publisher.manual_hold import (discover_manual_holds, find_cloud_manual_hold,
    find_manual_hold, list_manual_holds, restore_manual_hold)
from src.publisher.multi_deploy import MultiTargetLedger


def held_source(tmp_path):
    first=target(tmp_path)
    second=replace(first,platform='wechat_channels',account=replace(first.account,
        platform='wechat_channels',account_id='second'))
    root=tmp_path/'state'
    api=FakeDrive()
    ingest(root,api).poll(first)
    path=Path(first.account.target_dir)/api.files[0]['name']
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    ledger=MultiTargetLedger(root)
    ledger.register_plan(digest,[first,second])
    ledger.set_result(digest,first.key,'SUCCESS','real receipt')
    ledger.set_result(digest,second.key,'AUTH_REQUIRED','login required',attempts_used=1)
    archive=path.parent/'publiced';archive.mkdir()
    path.rename(archive/path.name)
    return root,first,second,api,path,digest


def test_hold_survives_restart_and_metadata_changes_without_download_or_success(tmp_path):
    root,first,second,api,path,digest=held_source(tmp_path)
    original=(root/'target_ledger.json').read_bytes()
    records=discover_manual_holds(root,[first,second],cloud_account=api.account_identity())
    assert len(records)==1
    record=records[0]
    assert set(record['target_keys'])=={first.key,second.key}
    assert record['source_day']=='2026-10-16'
    assert not path.exists() and Path(record['held_path']).exists()
    assert find_manual_hold(root,path,digest,second)['hold_id']==record['hold_id']
    # Reopening journal / changing only Drive's version must not fetch held bytes.
    DriveJournal(root)
    api.files[0]['version']='2'
    logs=[]
    worker=ingest(root,api,log=logs.append)
    assert worker.poll(second)
    assert api.downloads==['f1']
    assert worker.last_scans[-1]['media_files'][0]['journal_state']=='MANUAL_HOLD'
    assert (root/'target_ledger.json').read_bytes()==original
    assert list_manual_holds(root,[second],api.account_identity())[0]['hold_id']==record['hold_id']
    assert any('人工暂停' in line for line in logs)


def test_hold_scope_excludes_another_owner_folder_or_local_directory(tmp_path):
    root,first,second,api,path,digest=held_source(tmp_path)
    record=discover_manual_holds(root,[first,second],cloud_account=api.account_identity())[0]
    assert find_cloud_manual_hold(root,second,'other-drive-owner',api.files[0]) is None
    other_folder=replace(second,account=replace(second.account,
        drive_binding={**second.account.drive_binding,'folder_id':'other-folder'}))
    assert find_cloud_manual_hold(root,other_folder,api.account_identity(),api.files[0]) is None
    other_local=replace(second,account=replace(second.account,target_dir=str(tmp_path/'other-local')))
    assert find_cloud_manual_hold(root,other_local,api.account_identity(),api.files[0]) is None
    with pytest.raises(ValueError,match='不属于'):
        restore_manual_hold(root,record['hold_id'],second,'other-drive-owner')
    assert list_manual_holds(root)[0]['active']==1


def test_shared_binding_new_target_is_held_but_new_content_is_downloadable(tmp_path):
    root,first,second,api,path,digest=held_source(tmp_path)
    record=discover_manual_holds(root,[first,second],cloud_account=api.account_identity())[0]
    third=replace(second,account=replace(second.account,account_id='third'))
    assert find_cloud_manual_hold(root,third,api.account_identity(),api.files[0])['hold_id']==record['hold_id']
    assert third.key in list_manual_holds(root)[0]['target_keys']
    changed=cloud_file(content=b'changed-video'*1000,version='3')
    assert find_cloud_manual_hold(root,third,api.account_identity(),changed) is None
    api.files=[changed];api.content['f1']=b'changed-video'*1000
    ingest(root,api).poll(third)
    assert path.read_bytes()==b'changed-video'*1000
    assert api.downloads==['f1','f1']
    # The old held content remains paused independently.
    assert list_manual_holds(root)[0]['sha256']==digest


def test_explicit_restore_preserves_all_platform_results_and_attempt_counts(tmp_path):
    root,first,second,api,path,digest=held_source(tmp_path)
    original=(root/'target_ledger.json').read_bytes()
    record=discover_manual_holds(root,[first,second],cloud_account=api.account_identity())[0]
    with pytest.raises(ValueError,match='确认账号'):
        restore_manual_hold(root,record['hold_id'],second)
    assert restore_manual_hold(root,record['hold_id'],second,api.account_identity())==str(path)
    assert path.exists() and not Path(record['held_path']).exists()
    assert list_manual_holds(root)==[]
    assert (root/'target_ledger.json').read_bytes()==original
    ingest(root,api).poll(second)
    assert api.downloads==['f1']
    assert find_manual_hold(root,path,digest,second) is None


@pytest.mark.parametrize('change',['missing','changed','collision'])
def test_restore_does_not_release_missing_changed_or_colliding_content(tmp_path,change):
    root,first,second,api,path,digest=held_source(tmp_path)
    record=discover_manual_holds(root,[first,second],cloud_account=api.account_identity())[0]
    held=Path(record['held_path'])
    if change=='missing':held.unlink()
    elif change=='changed':held.write_bytes(b'different')
    else:path.write_bytes(b'new same-name content')
    with pytest.raises(ValueError):restore_manual_hold(root,record['hold_id'],second,api.account_identity())
    assert list_manual_holds(root)[0]['active']==1
    if change=='collision':assert path.read_bytes()==b'new same-name content'


@pytest.mark.parametrize('completed_status',['SUCCESS','IGNORED'])
def test_all_required_success_or_explicit_ignore_skips_hash_and_does_not_create_hold(tmp_path,monkeypatch,completed_status):
    root,first,second,api,path,digest=held_source(tmp_path)
    MultiTargetLedger(root).set_result(digest,second.key,completed_status,'real receipt or explicit user ignore')
    from src.publisher import drive_ingest
    hasher=MagicMock(side_effect=AssertionError('completed archive must not be rehashed'))
    monkeypatch.setattr(drive_ingest,'file_hashes',hasher)
    assert discover_manual_holds(root,[first,second],cloud_account=api.account_identity())==[]
    hasher.assert_not_called()


def test_existing_hold_skips_ledger_read_and_archive_hash_on_next_poll(tmp_path,monkeypatch):
    root,first,second,api,path,digest=held_source(tmp_path)
    discover_manual_holds(root,[first,second],cloud_account=api.account_identity())
    from src.publisher import drive_ingest
    hasher=MagicMock(side_effect=AssertionError('existing hold must not be rehashed'))
    monkeypatch.setattr(drive_ingest,'file_hashes',hasher)
    (root/'target_ledger.json').write_text('temporarily unreadable',encoding='utf-8')
    assert len(discover_manual_holds(root,[first,second],cloud_account=api.account_identity()))==1
    hasher.assert_not_called()


def test_discovery_reads_large_ledger_once_for_multiple_missing_sources(tmp_path,monkeypatch):
    root,first,second,api,path,digest=held_source(tmp_path)
    # Add one distinct verified source in the same binding.
    data=b'other-video'*1000
    api.files=[cloud_file('f2',data,'other.mp4')];api.content['f2']=data
    ingest(root,api).poll(first)
    other=path.parent/'other.mp4';other.rename(path.parent/'publiced'/other.name)
    # Clear first hold created by ingest to observe both source groups together.
    with DriveJournal(root).connect() as db:db.execute('DELETE FROM manual_holds')
    original=Path.read_text
    reads=[]
    def read(candidate,*args,**kwargs):
        if candidate.name=='target_ledger.json':reads.append(candidate)
        return original(candidate,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    assert len(discover_manual_holds(root,[first,second],cloud_account=api.account_identity()))==2
    assert len(reads)==1


def test_concurrent_poll_and_batch_discovery_create_one_stable_hold(tmp_path,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from src.publisher import drive_ingest
    root,first,second,api,path,digest=held_source(tmp_path)
    barrier=threading.Barrier(2)
    original=drive_ingest.file_hashes
    def hash_together(*args,**kwargs):
        result=original(*args,**kwargs)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(drive_ingest,'file_hashes',hash_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(discover_manual_holds,root,[first,second],cloud_account=api.account_identity()) for _ in range(2)]
        records=[future.result(timeout=10) for future in futures]
    assert len(list_manual_holds(root))==1
    assert records[0][0]['hold_id']==records[1][0]['hold_id']
