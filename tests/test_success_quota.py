"""Successful publications consume allowance; transfer attempts never do."""
import json
import sqlite3
from datetime import datetime,timedelta

import pytest

from src.publisher.upload_quota import CHINA_TIME,DailyUploadQuota,DailyQuotaReached


def test_failed_transfers_do_not_exhaust_limit_and_success_is_idempotent(tmp_path):
    quota=DailyUploadQuota(tmp_path)
    quota.set_limit('douyin:a',1)
    for number in range(15):
        quota.reserve('douyin:a','video','task',str(number))
        assert quota.snapshot('douyin:a')['used']==0
    quota.record_success('douyin:a','video','task')
    quota.record_success('douyin:a','video','task')
    restarted=DailyUploadQuota(tmp_path)
    assert restarted.snapshot('douyin:a')['used']==1
    with pytest.raises(DailyQuotaReached,match='已成功发布1个'):
        restarted.reserve('douyin:a','next','task','next')
    assert restarted.snapshot('douyin:b')['used']==0


def test_old_fifteen_attempts_are_recounted_from_confirmed_successes(tmp_path):
    day='2026-10-20T12:00:00+08:00'
    with sqlite3.connect(tmp_path/'upload_quota.sqlite3') as db:
        db.execute('CREATE TABLE limits (target TEXT PRIMARY KEY, maximum INTEGER NOT NULL)')
        db.execute('INSERT INTO limits VALUES (?,?)',('douyin:a',15))
        db.execute('CREATE TABLE uploads (id TEXT PRIMARY KEY, day TEXT, target TEXT, digest TEXT, task TEXT, started_at TEXT)')
        db.executemany('INSERT INTO uploads VALUES (?,?,?,?,?,?)',
                      [(str(n),'2026-10-20','douyin:a','hash'+str(n),'old',day) for n in range(15)])
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute("INSERT INTO metadata VALUES ('history_imported','1')")
    folder=tmp_path/'publish_reports';folder.mkdir()
    report={'task_id':'old','files':[
        {'sha256':'a'*64,'results':{'douyin:a':{'status':'SUCCESS','attempts':[
            {'id':'11','status':'FAILED1','upload_started':True,'started_at':day},
            {'id':'12','status':'SUCCESS','upload_started':True,'started_at':day,'finished_at':day}]}}},
        {'sha256':'b'*64,'results':{'douyin:a':{'status':'SUCCESS','attempts':[
            {'id':'13','status':'SUCCESS','upload_started':True,'started_at':day,'finished_at':day}]}}},
        {'sha256':'c'*64,'results':{'douyin:a':{'status':'FAILED1','attempts':[
            {'id':'14','status':'FAILED1','upload_started':True,'started_at':day}]}}},
    ]}
    (folder/'old.json').write_text(json.dumps(report),encoding='utf-8')
    (folder/'copied-report.json').write_text(json.dumps(report),encoding='utf-8')
    quota=DailyUploadQuota(tmp_path,lambda:datetime(2026,10,20,13,tzinfo=CHINA_TIME))
    assert quota.snapshot('douyin:a')=={'day':'2026-10-20','limit':15,'used':2,'remaining':13}
    quota.bootstrap_history();quota.bootstrap_history()
    quota.reserve('douyin:a','c'*64,'new','retry')
    assert quota.snapshot('douyin:a')['used']==2
    with quota.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0]==16
    quota.record_success('douyin:a','c'*64,'new')
    assert quota.snapshot('douyin:a')['used']==3


def test_success_counts_on_confirmation_day_not_transfer_day(tmp_path):
    clock=[datetime(2026,10,20,23,59,59,tzinfo=CHINA_TIME)]
    quota=DailyUploadQuota(tmp_path,lambda:clock[0]);quota.set_limit('youtube:a',1)
    quota.reserve('youtube:a','first','task','attempt')
    clock[0]+=timedelta(seconds=2)
    quota.record_success('youtube:a','first','task')
    assert quota.snapshot('youtube:a')['day']=='2026-10-21'
    assert quota.snapshot('youtube:a')['used']==1
    clock[0]+=timedelta(days=1)
    quota.record_success('youtube:a','first','read-only-recheck')
    assert quota.snapshot('youtube:a')['used']==0


def test_migration_counts_success_finished_after_midnight_on_correct_day(tmp_path):
    folder=tmp_path/'publish_reports';folder.mkdir()
    report={'task_id':'old','files':[{'sha256':'video','results':{'youtube:a':{'attempts':[
        {'status':'FAILED1','started_at':'2026-10-20T23:59:00+08:00'},
        {'status':'SUCCESS','started_at':'2026-10-20T23:59:59+08:00',
         'finished_at':'2026-10-21T00:00:02+08:00'}]}}}]}
    (folder/'old.json').write_text(json.dumps(report),encoding='utf-8')
    quota=DailyUploadQuota(tmp_path,lambda:datetime(2026,10,21,1,tzinfo=CHINA_TIME))
    assert quota.snapshot('youtube:a')['used']==1
    with quota.connect() as db:
        assert db.execute('SELECT day FROM published').fetchone()[0]=='2026-10-21'


def test_migration_recovers_saved_success_receipt_and_excludes_unconfirmed(tmp_path):
    data={'results':{'a':{'douyin:a':{'status':'SUCCESS','task_id':'old','attempt_count':1,
                                    'updated_at':'2026-10-20T12:41:28+08:00'}},
                     'b':{'douyin:a':{'status':'RECOVERY_REQUIRED','task_id':'old','attempt_count':1,
                                    'updated_at':'2026-10-20T12:43:00+08:00'}}}}
    (tmp_path/'target_ledger.json').write_text(json.dumps(data),encoding='utf-8')
    quota=DailyUploadQuota(tmp_path,lambda:datetime(2026,10,20,13,tzinfo=CHINA_TIME))
    assert quota.snapshot('douyin:a')['used']==1


def test_cancelled_migration_keeps_transfer_history_and_can_retry(tmp_path):
    folder=tmp_path/'publish_reports';folder.mkdir()
    (folder/'report.json').write_text('{"files":[]}',encoding='utf-8')
    quota=DailyUploadQuota(tmp_path)
    with pytest.raises(InterruptedError):quota.bootstrap_history(lambda:True)
    quota.bootstrap_history()
    assert quota.snapshot('douyin:a')['used']==0
