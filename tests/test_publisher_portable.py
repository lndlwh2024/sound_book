"""Publisher isolation, durable intent, duplicate prevention and storage boundaries."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from src.publisher.portable import RunState, StorageBudget, data_root
from src.publisher.windows_startup import task_xml
from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger, PublishTarget
from src.publisher.platforms import ChannelAccount
from src.app.publisher_window import PublisherWindow


@pytest.fixture(scope='module', autouse=True)
def app():
    return QApplication.instance() or QApplication([])


class StubShutdown:
    """Widget tests never arm a process-exit watchdog against their pytest owner."""
    def __init__(self,*args,**kwargs):self.ready=False;self.started=None
    def begin(self):self.started=0
    def poll(self,inventory):self.ready=not any(item.get('running') for item in inventory);return self.ready
    def status(self):return '正在测试退出'
    def flush(self):pass


@pytest.fixture
def window(tmp_path,monkeypatch):
    monkeypatch.setattr('src.app.publisher_window.ShutdownController',StubShutdown)
    result = PublisherWindow(tmp_path)
    yield result
    result.panel.state.intent = 'STOPPED'
    result.closing = True
    result.panel.prepare_shutdown()
    for worker in (result.panel.snapshot_worker, result.panel.drive_watch_worker,
                   result.panel.preflight_worker, result.panel.cleanup_worker):
        if worker:
            worker.wait(3000)
    result.close()
    result.deleteLater()
    QApplication.processEvents()


def test_first_start_never_runs_or_reads_full_client_accounts(window, tmp_path):
    assert window.panel.state.intent == 'STOPPED'
    assert not window.panel.state.auto_deploy
    assert not window.panel.state.auto_watch
    assert window.panel.repository.douyin_manager.config_file == tmp_path/'douyin_accounts.json'
    assert window.panel.store.root == tmp_path/'multi_platform'
    assert all(not window.panel.repository.accounts(key) for key in ('douyin', 'xiaohongshu','kuaishou','youtube'))
    assert window.panel.worker is None


@pytest.mark.parametrize('intent', ['RUNNING', 'STOPPED', 'PAUSED'])
def test_intent_roundtrip_preserves_operator_choice(tmp_path, intent):
    path = tmp_path/'state.json'
    state = RunState(auto_watch=True, auto_deploy=True, intent=intent, targets=['douyin:abc'])
    state.save(path)
    assert RunState.load(path) == state


@pytest.mark.parametrize('content', ['{}', '{', '{"schema":2,"intent":"RUNNING"}'])
def test_corrupt_state_fails_closed_without_replacing_file(tmp_path, content):
    path = tmp_path/'publisher_state.json'
    path.write_text(content)
    with pytest.raises(ValueError):
        RunState.load(path)
    assert path.read_text() == content


@pytest.mark.parametrize('intent', ['STOPPED', 'PAUSED'])
def test_automatic_restore_honours_stop_and_pause(window, monkeypatch, intent):
    window.panel.state.intent = intent
    window.panel.state.auto_watch = window.panel.state.auto_deploy = True
    start = MagicMock()
    monkeypatch.setattr(window.panel, 'begin_cleanup', start)
    window.panel.restore_deployment()
    start.assert_not_called()


def test_removed_or_disabled_account_never_substituted(window, monkeypatch):
    state = window.panel.state
    state.intent, state.auto_deploy, state.targets = 'RUNNING', True, ['youtube:removed']
    start = MagicMock()
    monkeypatch.setattr(window.panel, 'begin_cleanup', start)
    window.panel.restore_deployment()
    start.assert_not_called()
    assert '未替换' in window.panel.douyin.deploy_status.text()


def test_retry_backoff_is_bounded(window):
    window.panel.state.intent = 'RUNNING'
    seen = []
    for _ in range(7):
        window.panel.schedule_retry()
        seen.append(window.panel.restore_timer.interval())
    assert seen == [15000,30000,60000,120000,240000,300000,300000]


def test_login_task_is_interactive_delayed_and_quoted():
    import xml.etree.ElementTree as ET
    xml = task_xml('C:/书声 program/client.exe', ['--data-dir','C:/data & files','--autostart'], 'PC\\user')
    task = ET.fromstring(xml)
    ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert task.find('.//t:LogonType',ns).text == 'InteractiveToken'
    assert task.find('.//t:MultipleInstancesPolicy',ns).text == 'IgnoreNew'
    assert task.find('.//t:Delay',ns).text == 'PT90S'
    assert task.find('.//t:RunLevel',ns).text == 'LeastPrivilege'
    assert '"C:/data & files"' in task.find('.//t:Arguments',ns).text


def test_storage_counts_archives_and_overlapping_roots_once(tmp_path, monkeypatch):
    archive = tmp_path/'publiced'
    archive.mkdir()
    (archive/'old.mp4').write_bytes(b'x'*50)
    monkeypatch.setattr('src.publisher.portable.shutil.disk_usage', lambda _: type('Disk',(),{'free':1000})())
    policy = StorageBudget(lambda: [str(tmp_path), str(archive), str(tmp_path)], maximum=100, reserve=100)
    policy.check(tmp_path, 50)
    with pytest.raises(OSError, match='30GB'):
        policy.check(tmp_path, 51)
    assert (archive/'old.mp4').exists()


def test_storage_reserve_does_not_delete_media(tmp_path, monkeypatch):
    monkeypatch.setattr('src.publisher.portable.shutil.disk_usage', lambda _: type('Disk',(),{'free':105})())
    policy = StorageBudget(lambda: [], maximum=10000, reserve=100)
    with pytest.raises(OSError, match='10GB'):
        policy.check(tmp_path, 6)


def test_second_instance_activates_first_without_taking_ownership(tmp_path):
    from src.app.single_instance import SingleInstance
    activated = []
    first = SingleInstance(tmp_path, lambda: activated.append(True))
    second = SingleInstance(tmp_path, lambda: None)
    try:
        assert first.acquire()
        assert not second.acquire()
        QApplication.processEvents()
        assert activated == [True]
        second.close()
        assert first.lock.isLocked()
    finally:
        first.close()


def test_crash_marker_blocks_reupload_without_charging_quota(tmp_path):
    media = tmp_path/'media'
    media.mkdir()
    path = media/'test.mp4'
    path.write_bytes(b'x'*2000)
    os.utime(path,(time.time()-10,)*2)
    target = PublishTarget('youtube', ChannelAccount('a'*32, 'youtube', 'test', target_dir=str(media)), str(tmp_path/'profile'))
    factory = MagicMock()
    worker = MultiDeployWorker([target],root=tmp_path/'state', uploader_factory=factory, strict_recovery=True)
    worker.ledger = MultiTargetLedger(worker.root)
    jobs = worker.plan_batch()
    worker.ledger.set_result(jobs[0]['digest'],target.key,'IN_PROGRESS','interrupted', requires_review=True)
    assert worker.attempt(jobs[0],target) == 'RECOVERY_REQUIRED'
    factory.assert_not_called()
    assert worker.quota.snapshot(target.key)['used'] == 0
    assert path.exists()


def test_pending_review_ignores_proven_success(window):
    ledger = MultiTargetLedger(window.panel.store.root)
    ledger.set_result('a'*64,'youtube:a','IN_PROGRESS','interrupted',requires_review=True)
    ledger.set_result('b'*64,'youtube:a','SUCCESS','confirmed',requires_review=True)
    assert [(d,k) for d,k,_ in window.panel.pending_reviews()] == [('a'*64,'youtube:a')]


def test_strict_recovery_does_not_retry_unknown_submit(tmp_path):
    media = tmp_path/'media'
    media.mkdir()
    path = media/'test.mp4'
    path.write_bytes(b'x'*2000)
    os.utime(path,(time.time()-10,)*2)
    target = PublishTarget('youtube',ChannelAccount('a'*32,'youtube','test',target_dir=str(media)),str(tmp_path/'profile'))
    calls = []
    class Uploader:
        supports_upload_boundary = True
        def upload_single_video(self, path):
            calls.append(path)
            self.before_upload()
            self.before_submit({'platform':'youtube','confirmation':None})
            raise RuntimeError('connection closed after submission')
    worker = MultiDeployWorker([target],root=tmp_path/'state',uploader_factory=lambda *args:Uploader(),
                              strict_recovery=True,interval_seconds=0)
    worker.ledger = MultiTargetLedger(worker.root)
    jobs = worker.plan_batch()
    worker.run_batch(jobs)
    assert len(calls) == 1
    saved = MultiTargetLedger(worker.root).result(jobs[0]['digest'],target.key)
    assert saved['status'] == 'RECOVERY_REQUIRED' and saved['requires_review']
    assert worker.quota.snapshot(target.key)['used'] == 0
    assert path.exists()


def test_import_does_not_require_production_dependencies():
    command = 'import sys; import src.app.publisher_window; assert not any(x.startswith(("src.audio","src.video","src.tts","torch","src.app.main_window","src.app.task_bridge")) for x in sys.modules)'
    subprocess.run([sys.executable,'-c',command],check=True)


def test_publisher_does_not_open_two_login_browsers(window, monkeypatch):
    first, second = window.panel.panels[:2]
    first.dialog = MagicMock()
    first.dialog.worker.isRunning.return_value = True
    save = MagicMock()
    message = MagicMock()
    monkeypatch.setattr(second, 'save_current', save)
    monkeypatch.setattr(QMessageBox, 'information', message)
    second.open_probe()
    save.assert_not_called()
    message.assert_called_once()
    first.dialog = None


def test_small_window_has_visible_buttons_and_no_form_scroll(window):
    from PySide6.QtWidgets import QScrollArea
    from PySide6.QtGui import QFontDatabase, QFont
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    QApplication.instance().setFont(QFont('Microsoft YaHei',9))
    window.resize(1366,728)
    window.show()
    for index, panel in enumerate(window.panel.panels):
        window.panel.channel_view.setCurrentIndex(index)
        for tab in (0,1):
            panel.settings_tabs.setCurrentIndex(tab)
            QApplication.processEvents()
            assert window.height() <= 728
            assert panel.save_button.height() >= 32
            assert panel.deploy_button.height() >= 32
            assert not panel.left_settings.findChildren(QScrollArea)
            assert panel.save_button.geometry().bottom() <= panel.left_settings.height()


def test_history_transfer_keeps_success_quota_and_date_but_requeues_missing_media(tmp_path):
    from datetime import date
    from src.publisher.platforms import ChannelAccountStore
    from src.publisher.drive_ingest import DriveJournal
    from src.publisher.history_transfer import export_history, import_history
    from src.publisher.upload_quota import DailyUploadQuota
    source = tmp_path/'source'
    store = ChannelAccountStore(source/'multi_platform')
    old_media = tmp_path/'old'
    old_media.mkdir()
    account = store.add('youtube','test')
    account.target_dir = str(old_media)
    account.drive_binding = {'enabled':True,'folder_id':'folder'}
    store.save()
    target = PublishTarget('youtube',account,'')
    journal = DriveJournal(store.root)
    journal.prepare_account('google:owner')
    entries = []
    for number in (1,2):
        entry = journal.reserve(target,{'id':f'f{number}','name':f'f{number}.mp4','version':'1'},'google:owner')
        journal.complete(entry['identity'],str(number)*64,False)
        entries.append(entry)
    journal.save_scan_day(target,date(2026,10,15),'google:owner')
    MultiTargetLedger(store.root).set_result('1'*64,target.key,'SUCCESS','confirmed')
    quota = DailyUploadQuota(store.root)
    quota.reserve(target.key,'1'*64,'task','attempt',10)
    quota.record_success(target.key,'1'*64,'task')
    secret = store.root/'google_drive/credentials.dpapi'
    secret.parent.mkdir()
    secret.write_bytes(b'must not export')
    archive = tmp_path/'history.zip'
    export_history(source,archive)
    import zipfile
    with zipfile.ZipFile(archive) as zipped:
        assert not any('credentials' in name or 'profiles' in name for name in zipped.namelist())
    new_media = tmp_path/'new_media'
    new_media.mkdir()
    destination = tmp_path/'new_data'
    import_history(archive,destination,{target.key:str(new_media)})
    imported = ChannelAccountStore(destination/'multi_platform').accounts[0]
    assert imported.account_id == account.account_id
    assert imported.target_dir == str(new_media.resolve())
    assert imported.login_status == 'UNCONFIRMED'
    new_target = PublishTarget('youtube',imported,'')
    new_journal = DriveJournal(destination/'multi_platform')
    assert new_journal.resume_day(new_target,date(2026,10,18),'google:owner') == date(2026,10,15)
    with new_journal.connect() as db:
        rows = db.execute('SELECT cloud_id,state,path FROM files ORDER BY cloud_id').fetchall()
    assert [(r[0],r[1]) for r in rows] == [('f1','DOWNLOADED'),('f2','PENDING')]
    assert all(Path(r[2]).parent == new_media.resolve() for r in rows)
    assert DailyUploadQuota(destination/'multi_platform').snapshot(target.key)['used'] == 1
    assert MultiTargetLedger(destination/'multi_platform').result('1'*64,target.key)['status'] == 'SUCCESS'
    assert RunState.load(destination/'publisher_state.json').intent == 'STOPPED'
    with pytest.raises(ValueError,match='全新'):
        import_history(archive,destination,{target.key:str(new_media)})
