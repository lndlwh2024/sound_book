"""Read-only protocol, durable ingestion and real local publishing integration."""
import hashlib
import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen

import pytest
from src.publisher import google_drive as google
from src.publisher.drive_ingest import DriveIngestor, DriveJournal, version_of, CHINA_TIME, safe_name
from src.publisher.platforms import ChannelAccount
from src.publisher.multi_deploy import PublishTarget, MultiTargetLedger, MultiDeployWorker
from src.publisher.douyin.daemon import is_file_fully_written


def target(tmp_path, name='a'):
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    account = ChannelAccount(name, 'xiaohongshu', name, target_dir=str(directory),
        drive_binding=dict(enabled=True, folder_id='series', folder_path='书声/趣味知识', poll_seconds=15))
    return PublishTarget('xiaohongshu', account, str(tmp_path/'profile'))


def cloud_file(identity='f1', content=b'video'*1000, name='海星.mp4', version='1'):
    return dict(id=identity, name=name, mimeType='video/mp4', size=str(len(content)),
        md5Checksum=hashlib.md5(content).hexdigest(), modifiedTime='2026-10-16T00:00:00Z', version=version)


class FakeDrive:
    def __init__(self, files=None):
        self.files = files or [cloud_file()]
        self.downloads = []
        self.dates = []
        self.content = {f['id']:b'video'*1000 for f in self.files}

    def account_identity(self):
        return 'fake-google-account'

    def today_files(self, parent, day):
        self.dates.append((parent, day))
        return {'id':'date'}, self.files

    def metadata(self, identity):
        return next(f for f in self.files if f['id'] == identity)

    def download(self, identity, path, stopped, progress):
        self.downloads.append(identity)
        Path(path).write_bytes(self.content[identity])
        progress(len(self.content[identity]))


def ingest(root, client, **kwargs):
    return DriveIngestor(root, client, clock=lambda:datetime(2026,10,16,23,30,tzinfo=CHINA_TIME), **kwargs)


def test_same_version_skipped_after_restart_and_archive(tmp_path):
    t = target(tmp_path)
    api = FakeDrive()
    root = tmp_path/'state'
    ready = []
    ingest(root, api, ready=ready.append).poll(t)
    path = Path(ready[0])
    assert path.read_bytes() == b'video'*1000 and is_file_fully_written(path)
    path.rename(path.with_suffix('.archived'))
    ingest(root, api).poll(t)
    assert api.downloads == ['f1'] and api.dates == [('series','2026-10-16')]*2


def test_next_day_copy_dedups_and_archives_without_false_suffix_or_upload(tmp_path):
    t = target(tmp_path)
    root = tmp_path/'state'
    digest = hashlib.sha256(b'video'*1000).hexdigest()
    MultiTargetLedger(root).set_result(digest,t.key,'SUCCESS','confirmed earlier')
    api = FakeDrive([cloud_file('new-cloud-id')])
    ingest(root,api).poll(t)
    worker = MultiDeployWorker([t], root=root, interval_seconds=0,
        uploader_factory=lambda *args:(_ for _ in ()).throw(AssertionError('must not upload')))
    worker.ledger = MultiTargetLedger(root)
    worker.run_batch(worker.plan_batch())
    archived = Path(t.account.target_dir)/'publiced'/'海星.mp4'
    assert archived.exists()
    ingest(root,api).poll(t)
    assert len(api.downloads) == 1


def test_new_cloud_file_publishes_via_existing_worker_and_quota(tmp_path):
    t = target(tmp_path)
    root = tmp_path/'state'
    api = FakeDrive()
    ingest(root,api).poll(t)
    calls=[]
    class Uploader:
        def upload_single_video(self,path):
            self.before_upload()
            calls.append(path)
            return True,'confirmed',None
    worker = MultiDeployWorker([t],root=root,interval_seconds=0,uploader_factory=lambda *args:Uploader())
    worker.ledger = MultiTargetLedger(root)
    worker.run_batch(worker.plan_batch())
    assert len(calls)==1
    assert (Path(t.account.target_dir)/'publiced'/'海星.mp4').exists()
    assert worker.quota.snapshot(t.key)['used']==1


@pytest.mark.parametrize('manual_cleanup',[False,True])
def test_success_history_restored_after_download_does_not_add_false_suffix(tmp_path,manual_cleanup):
    t=target(tmp_path);root=tmp_path/'state';api=FakeDrive()
    ingest(root,api).poll(t)
    digest=hashlib.sha256(b'video'*1000).hexdigest()
    # The existing startup cleanup may restore an older platform success later.
    MultiTargetLedger(root).set_result(digest,t.key,'SUCCESS','restored legacy success')
    if manual_cleanup:
        from src.publisher.scan_cleanup import ScanCleanupWorker
        worker=ScanCleanupWorker([t],[t],root)
        worker.run()
        assert not worker.error
    else:
        worker=MultiDeployWorker([t],root=root,interval_seconds=0,
            uploader_factory=lambda *args:(_ for _ in ()).throw(AssertionError('duplicate upload')))
        worker.ledger=MultiTargetLedger(root)
        worker.run_batch(worker.plan_batch())
    assert (Path(t.account.target_dir)/'publiced'/'海星.mp4').exists()


def test_partial_cancel_never_enters_queue_and_retries(tmp_path):
    t=target(tmp_path);api=FakeDrive();root=tmp_path/'state'
    original=api.download
    def interrupted(identity,path,stopped,progress):
        Path(path).write_bytes(b'half')
        raise InterruptedError()
    api.download=interrupted
    with pytest.raises(InterruptedError):ingest(root,api).poll(t)
    assert not list(Path(t.account.target_dir).glob('*.mp4'))
    assert not list(Path(t.account.target_dir).glob('*.part'))
    api.download=original
    ingest(root,api).poll(t)
    assert is_file_fully_written(Path(t.account.target_dir)/'海星.mp4')


def test_crash_after_rename_recovers_without_redownload(tmp_path):
    t=target(tmp_path);api=FakeDrive();root=tmp_path/'state'
    journal=DriveJournal(root);entry=journal.reserve(t,api.files[0]);path=Path(entry['path'])
    path.write_bytes(api.content['f1'])
    marker=path.with_name('.'+path.name+'.shusheng-pending');marker.touch()
    os.utime(path,(time.time()-10,time.time()-10))
    assert not is_file_fully_written(path)
    ingest(root,api).poll(t)
    assert api.downloads==[] and is_file_fully_written(path) and not marker.exists()
    assert journal.entry(journal.identity(t,api.files[0],api.account_identity())[0])['state']=='DOWNLOADED'


def test_bad_checksum_does_not_block_next_file_or_mark_processed(tmp_path):
    t=target(tmp_path);root=tmp_path/'state'
    api=FakeDrive([cloud_file('bad',name='bad.mp4'),cloud_file('good',name='good.mp4')])
    api.content['bad']=b'wrong'*800
    logs=[];ingest(root,api,log=logs.append).poll(t)
    assert not (Path(t.account.target_dir)/'bad.mp4').exists()
    assert (Path(t.account.target_dir)/'good.mp4').exists()
    entry=DriveJournal(root).entry(DriveJournal.identity(t,api.files[0],api.account_identity())[0])
    assert entry['state']=='FETCHING' and any('校验' in text for text in logs)


def test_changed_remote_content_not_installed(tmp_path):
    t=target(tmp_path);api=FakeDrive();root=tmp_path/'state'
    api.metadata=lambda identity:cloud_file(content=b'changed'*1000,version='2')
    ingest(root,api).poll(t)
    assert not list(Path(t.account.target_dir).glob('*.mp4'))


def test_same_cloud_file_independent_per_account_and_directory(tmp_path):
    a,b=target(tmp_path,'a'),target(tmp_path,'b');api=FakeDrive();root=tmp_path/'state'
    ingestor=ingest(root,api)
    ingestor.poll(a);ingestor.poll(b)
    assert api.downloads==['f1','f1']
    digest=hashlib.sha256(b'video'*1000).hexdigest()
    MultiTargetLedger(root).set_result(digest,a.key,'SUCCESS','one account only')
    assert not MultiTargetLedger(root).result(digest,b.key)


def test_filters_disabled_binding_date_and_filename_collision(tmp_path):
    t=target(tmp_path);api=FakeDrive([cloud_file(),cloud_file('audio',name='voice.mp3')]);root=tmp_path/'state'
    existing=Path(t.account.target_dir)/'海星.mp4';existing.write_bytes(b'user file')
    ingest(root,api).poll(t)
    assert existing.read_bytes()==b'user file' and api.downloads==['f1']
    assert len(list(existing.parent.glob('海星_*.mp4')))==1
    t.account.enabled=False
    ingest(root,api).poll(t)
    assert len(api.dates)==1
    assert '/' not in safe_name('../../bad.mp4') and '\\' not in safe_name('a\\b.mp4')


def test_drive_pagination_query_and_no_mutating_endpoints():
    client=google.DriveClient(None);queries=[]
    def get(route,**params):
        queries.append((route,params))
        return {'files':[cloud_file('b' if params.get('pageToken') else 'a')],
            **({} if params.get('pageToken') else {'nextPageToken':'next'})}
    client.get=get
    assert len(client.children("folder_id",True))==2
    assert all(q[0]=='files' and 'trashed = false' in q[1]['q'] for q in queries)
    assert queries[1][1]['pageToken']=='next'
    assert not any(hasattr(client,name) for name in ('delete','move','update','upload'))


@pytest.mark.skipif(os.name!='nt',reason='Windows credential protection')
def test_dpapi_roundtrip_and_disconnect_does_not_delete_binding(tmp_path):
    creds=google.DriveCredentials(tmp_path)
    creds.install({'refresh_token':'test-refresh','access_token':'test-access'},creds.generation)
    assert creds.connected() and creds.read()['refresh_token']=='test-refresh'
    assert b'test-refresh' not in creds.token_path.read_bytes()
    creds.disconnect()
    assert not creds.connected()


def test_oauth_loopback_pkce_state_and_cancel_preserve_connection(tmp_path,monkeypatch):
    creds=google.DriveCredentials(tmp_path)
    monkeypatch.setattr(creds,'client',lambda:{'client_id':'test-client','client_secret':'test-secret'})
    installed=[];monkeypatch.setattr(creds,'install',lambda data,generation:installed.append(data))
    exchanges=[]
    def exchange(url,**kwargs):
        exchanges.append((url,kwargs))
        return {'refresh_token':'test','access_token':'test'}
    monkeypatch.setattr(google,'request',exchange)
    urls=[];ready=threading.Event()
    def opened(url):urls.append(url);ready.set()
    errors=[]
    def run():
        try:google.authorize(creds,opened,lambda:False,seconds=5)
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=run);thread.start();assert ready.wait(2)
    params=parse_qs(urlsplit(urls[0]).query)
    callback=params['redirect_uri'][0]
    assert urlsplit(callback).hostname=='127.0.0.1'
    assert params['scope']==[google.SCOPE] and params['code_challenge_method']==['S256']
    with pytest.raises(Exception):urlopen(callback+'?code=bad&state=wrong')
    assert not exchanges
    urlopen(callback+'?code=good&state='+params['state'][0]).close()
    thread.join(3)
    assert not errors and len(installed)==1
    verifier=exchanges[0][1]['data']['code_verifier']
    import base64
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')==params['code_challenge'][0]
    installed.clear()
    with pytest.raises(InterruptedError):google.authorize(creds,lambda url:None,lambda:True)
    assert not installed


def test_invalid_folder_and_clients_are_rejected_without_external_calls(tmp_path):
    for bad in ('https://evil.example/folders/a','../secret','a/b',''):
        with pytest.raises(google.DriveError):google.folder_id(bad)
    assert google.folder_id('https://drive.google.com/drive/folders/abc_123?x=1')=='abc_123'
    path=tmp_path/'web-client.json';path.write_text(json.dumps({'web':{'client_id':'x'}}))
    with pytest.raises(google.DriveError):google.DriveCredentials(tmp_path).import_client(path)


@pytest.mark.skipif(os.name!='nt',reason='Windows credential protection')
def test_disconnect_during_refresh_is_responsive_and_cannot_restore_token(tmp_path,monkeypatch):
    creds=google.DriveCredentials(tmp_path)
    creds.install({'refresh_token':'test','access_token':'old','expires_in':-1},creds.generation)
    monkeypatch.setattr(creds,'client',lambda:{'client_id':'test','client_secret':'test'})
    started,release=threading.Event(),threading.Event()
    def refresh(*args,**kwargs):
        started.set();release.wait(3)
        return {'access_token':'new','expires_in':3600}
    monkeypatch.setattr(google,'request',refresh)
    errors=[]
    def run():
        try:creds.access_token()
        except google.DriveAuthRequired as error:errors.append(str(error))
    thread=threading.Thread(target=run);thread.start();assert started.wait(2)
    before=time.monotonic();creds.disconnect()
    assert time.monotonic()-before<.5
    release.set();thread.join(3)
    assert errors and not creds.connected() and not creds.token_path.exists()


@pytest.mark.parametrize('reason,expected',[
    (TimeoutError('secret'), '超时'),
    (__import__('socket').gaierror(11001,'secret'), 'DNS'),
    (ConnectionRefusedError(10061,'secret'), '连接被拒绝'),
])
def test_transport_errors_explain_category_without_leaking_proxy_text(monkeypatch,reason,expected):
    from unittest.mock import MagicMock
    from urllib.error import URLError
    from src.publisher import google_drive
    opener=MagicMock()
    opener.open.side_effect=URLError(reason)
    monkeypatch.setattr(google_drive,'build_opener',lambda *a:opener)
    with pytest.raises(google_drive.DriveError) as error:
        google_drive.request('https://example.invalid/?access_token=secret')
    assert expected in str(error.value) and 'secret' not in str(error.value)


def test_error_separates_google_request_stage_and_redacts_proxy_credentials(monkeypatch):
    from unittest.mock import MagicMock
    from urllib.request import ProxyHandler
    from src.publisher import google_drive
    opener=MagicMock()
    opener.open.side_effect=TimeoutError('SECRET')
    monkeypatch.setattr(google_drive,'build_opener',lambda *args:opener)
    monkeypatch.setattr(google_drive,'proxy_bypass',lambda host:False)
    monkeypatch.setattr(google_drive,'proxy_handlers',lambda url:[ProxyHandler({'https':'http://user:SECRET@127.0.0.1:58591'})])
    for url,stream,kind in [(google_drive.TOKEN_URL,False,'Google令牌请求'),
                            (google_drive.API_URL+'files?q=SECRET',False,'云盘目录/API请求'),
                            (google_drive.API_URL+'files/SECRET?alt=media',True,'云盘文件下载')]:
        with pytest.raises(google_drive.DriveError) as error:
            google_drive.request(url,stream=stream,token='SECRET')
        assert kind in str(error.value) and '强制HTTP代理 127.0.0.1:58591' in str(error.value)
        assert 'SECRET' not in str(error.value) and 'user' not in str(error.value)
