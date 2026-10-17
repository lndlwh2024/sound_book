"""Durable date catch-up without changing file-version or account deduplication."""
from datetime import datetime
from pathlib import Path

import pytest

from src.publisher.drive_ingest import DriveIngestor, DriveJournal, CHINA_TIME
from src.publisher.google_drive import DriveError
from tests.test_google_drive import FakeDrive, cloud_file, target


class DateDrive(FakeDrive):
    def __init__(self, dates):
        super().__init__([f for files in dates.values() for f in files])
        self.by_day = dates
        self.fail_day = None

    def today_files(self, parent, day):
        self.dates.append((parent, day))
        if day == self.fail_day:
            raise DriveError('temporary request failure')
        return ({'id': day}, self.by_day[day]) if day in self.by_day else (None, [])


def poll(root, api, account, day, **kwargs):
    DriveIngestor(root, api,
        clock=lambda: datetime.fromisoformat(day+'T12:00:00').replace(tzinfo=CHINA_TIME),
        **kwargs).poll(account)


def test_restart_catches_up_from_last_day_and_skips_consumed_versions(tmp_path):
    account = target(tmp_path)
    api = DateDrive({f'2026-10-{d}': [cloud_file(str(d),name=f'{d}.mp4')] for d in range(16,20)})
    root = tmp_path/'state'
    poll(root,api,account,'2026-10-16')
    api.dates.clear()
    poll(root,api,account,'2026-10-19')
    assert [day for _,day in api.dates] == [f'2026-10-{d}' for d in range(16,20)]
    assert api.downloads == ['16','17','18','19']
    api.dates.clear()
    poll(root,api,account,'2026-10-19')
    assert api.dates == [('series','2026-10-19')]
    assert len(api.downloads) == 4


def test_network_failure_preserves_unfinished_day_and_missing_dates_are_skipped(tmp_path):
    account = target(tmp_path)
    api = DateDrive({'2026-10-16': [], '2026-10-19':[cloud_file()]})
    root = tmp_path/'state'
    poll(root,api,account,'2026-10-16')
    api.fail_day = '2026-10-17'
    with pytest.raises(DriveError):
        poll(root,api,account,'2026-10-19')
    assert DriveJournal(root).resume_day(account,datetime(2026,10,19).date(),api.account_identity()).isoformat() == '2026-10-17'
    api.fail_day = None
    api.dates.clear()
    poll(root,api,account,'2026-10-19')
    assert [day for _,day in api.dates] == ['2026-10-17','2026-10-18','2026-10-19']
    assert api.downloads == ['f1']


def test_unfinished_file_keeps_old_date_without_blocking_later_files(tmp_path):
    account = target(tmp_path)
    old = cloud_file('old',name='old.mp4')
    api = DateDrive({'2026-10-16':[old], '2026-10-18':[cloud_file('new',name='new.mp4')]})
    api.content['old'] = b'wrong'
    root = tmp_path/'state'
    poll(root,api,account,'2026-10-16')
    poll(root,api,account,'2026-10-18')
    assert (Path(account.account.target_dir)/'new.mp4').exists()
    assert DriveJournal(root).resume_day(account,datetime(2026,10,18).date(),api.account_identity()).isoformat() == '2026-10-16'
    api.content['old'] = b'video'*1000
    poll(root,api,account,'2026-10-18')
    assert DriveJournal(root).resume_day(account,datetime(2026,10,18).date(),api.account_identity()).isoformat() == '2026-10-18'
    assert api.downloads.count('new') == 1


def test_first_use_and_new_account_or_binding_do_not_inherit_history(tmp_path):
    a,b=target(tmp_path,'a'),target(tmp_path,'b')
    api=DateDrive({})
    root=tmp_path/'state'
    poll(root,api,a,'2026-10-16')
    api.dates.clear()
    poll(root,api,b,'2026-10-19')
    assert api.dates == [('series','2026-10-19')]
    a.account.drive_binding['folder_id']='other-series'
    api.dates.clear()
    poll(root,api,a,'2026-10-19')
    assert api.dates == [('other-series','2026-10-19')]


def test_cancel_before_scan_does_not_advance_checkpoint(tmp_path):
    account=target(tmp_path);api=DateDrive({});root=tmp_path/'state'
    poll(root,api,account,'2026-10-16')
    with pytest.raises(InterruptedError):
        poll(root,api,account,'2026-10-19',stopped=lambda:True)
    api.dates.clear()
    poll(root,api,account,'2026-10-19')
    assert [day for _,day in api.dates] == [f'2026-10-{d}' for d in range(16,20)]


@pytest.mark.parametrize('field,value',[('md5Checksum',''),('size','0'),('capabilities',{'canDownload':False})])
def test_unready_media_cannot_be_left_behind_at_midnight(tmp_path,field,value):
    account=target(tmp_path);file=cloud_file();file[field]=value
    api=DateDrive({'2026-10-16':[file]});root=tmp_path/'state'
    poll(root,api,account,'2026-10-16')
    poll(root,api,account,'2026-10-18')
    assert DriveJournal(root).resume_day(account,datetime(2026,10,18).date(),api.account_identity()).isoformat()=='2026-10-16'
    assert not api.downloads


def test_existing_database_gets_cursor_table_without_losing_version_history(tmp_path):
    import sqlite3
    root=tmp_path/'state';root.mkdir()
    with sqlite3.connect(root/'drive_ingest.sqlite3') as db:
        db.execute('''CREATE TABLE files (
            identity TEXT PRIMARY KEY, target TEXT NOT NULL, binding TEXT NOT NULL,
            cloud_id TEXT NOT NULL, version TEXT NOT NULL, path TEXT NOT NULL,
            state TEXT NOT NULL, sha256 TEXT, duplicate INTEGER NOT NULL DEFAULT 0)''')
        db.execute("INSERT INTO files VALUES ('old','target','binding','cloud','v','path','DOWNLOADED','digest',0)")
    journal=DriveJournal(root)
    assert journal.entry('old')['sha256']=='digest'
    account=target(tmp_path)
    assert journal.resume_day(account,datetime(2026,10,19).date()).isoformat()=='2026-10-19'
