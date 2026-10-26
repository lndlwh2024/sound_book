"""UI actions use saved cloud bindings and preserve manually paused virtual rows."""
import json
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QMessageBox

from tests.test_common_platform_accounts import panel
from tests.test_google_drive import FakeDrive,target
from tests.test_manual_hold import held_source
from src.publisher.drive_ingest import DriveWatchWorker,DriveIngestor,canonical_local_path
from src.publisher.manual_hold import discover_manual_holds


def test_common_check_all_button_uses_saved_targets_without_saving_current_form(panel,monkeypatch):
    view=panel.douyin
    monkeypatch.setattr(panel.drive_credentials,'connected',lambda:True)
    panel.update_drive_connection()
    saved_directory=view.current().target_dir
    view.directory.setText(str(Path(saved_directory)/'not-saved'))
    save=MagicMock(side_effect=AssertionError('common polling must not save the current form'))
    monkeypatch.setattr(view,'save_current',save)
    sync=MagicMock();monkeypatch.setattr(panel,'sync_drive_watch',sync)
    worker=MagicMock();worker.targets=[object()];worker.check_all_now.return_value=True
    panel.drive_watch_worker=worker
    try:
        assert view.shared_drive.check_all_button.isEnabled()
        view.shared_drive.check_all_button.click()
        worker.check_all_now.assert_called_once()
        sync.assert_called_once()
        save.assert_not_called()
        assert view.current().target_dir==saved_directory
    finally:panel.drive_watch_worker=None


def test_shared_check_all_enabled_tracks_actual_connection_and_switch():
    from src.app.drive_widgets import SharedDriveWidget
    app=QApplication.instance() or QApplication([])
    widget=SharedDriveWidget()
    try:
        for connected,pending,enabled in ((False,False,True),(True,True,True),(True,False,False),(True,False,True)):
            widget.update_connection(connected,pending,enabled)
            assert widget.check_all_button.isEnabled()==(connected and enabled and not pending)
        assert widget.check_all_button.height()==widget.interval.height()==widget.change_button.height()
    finally:widget.close()


def test_missing_original_hold_row_has_restore_action_and_cannot_direct_upload(panel,tmp_path,monkeypatch):
    view=panel.douyin
    path=tmp_path/'video.mp4';held=tmp_path/'publiced'/'video.mp4'
    held.parent.mkdir();held.write_bytes(b'fixture')
    provenance={canonical_local_path(path):dict(manual_hold=True,hold_id='stable-hold-id',
        held_path=str(held),label='2026-10-25',tooltip='人工暂停，不计成功')}
    view.apply_queue_snapshot([(path,held.stat())],provenance)
    assert view.table_queue.rowCount()==1 and view.restore_hold_button.isEnabled()
    item=view.table_queue.item(0,1)
    assert item.data(Qt.UserRole)==str(path)
    assert item.data(Qt.UserRole+4)=='stable-hold-id'
    assert str(held) in item.toolTip()
    restore=MagicMock();monkeypatch.setattr(panel,'restore_manual_holds',restore)
    view.restore_hold_button.click();restore.assert_called_once_with(view)
    view.table_queue.setCurrentCell(0,1)
    start=MagicMock();monkeypatch.setattr(panel,'start_deployment',start)
    monkeypatch.setattr(panel,'busy',lambda:False)
    info=MagicMock();monkeypatch.setattr(QMessageBox,'information',info)
    view.upload_selected()
    start.assert_not_called()
    assert '人工暂停' in info.call_args.args[1]
    view.apply_queue_snapshot([],{})
    assert not view.restore_hold_button.isEnabled()


@pytest.mark.parametrize('missing_held',[False,True])
@pytest.mark.parametrize('new_target',[False,True])
def test_queue_snapshot_discovers_missing_original_and_keeps_hold_provenance(tmp_path,monkeypatch,missing_held,new_target):
    from dataclasses import replace
    from src.app.channel_panel import QueueSnapshotWorker
    from src.publisher import dom_readiness
    app=QApplication.instance() or QApplication([])
    root,first,second,api,path,digest=held_source(tmp_path)
    record=discover_manual_holds(root,[first,second],cloud_account=api.account_identity())[0]
    if new_target:
        second=replace(second,account=replace(second.account,account_id='new-stable-account'))
        assert second.key not in record['target_keys']
    if missing_held:Path(record['held_path']).unlink()
    monkeypatch.setattr(dom_readiness,'publishing_dom_state',lambda *args:None)
    worker=QueueSnapshotWorker(root,[(second.platform,(second.account.account_id,
        second.account.target_dir,second.account.media_types))],None,hold_targets=[first,second])
    received=[];worker.snapshot_ready.connect(received.extend)
    worker.run()
    assert len(received)==1
    files=received[0][2];provenance=received[0][5]
    assert any(p==path for p,stamp in files)
    assert provenance[canonical_local_path(path)]['hold_id']==record['hold_id']
    assert provenance[canonical_local_path(path)]['manual_hold']
    assert second.key in provenance[canonical_local_path(path)]['target_keys']
    assert ('隔离文件当前缺失' in provenance[canonical_local_path(path)]['tooltip'])==missing_held


def test_render_virtual_hold_preserves_real_ledger_success_and_pauses_only_unfinished_target(panel,tmp_path):
    from src.publisher.multi_deploy import MultiTargetLedger
    from src.publisher.platforms import PUBLISH_ORDER
    from src.publisher.status import STATUS_TEXT
    view=next(p for p in panel.panels if p.platform=='wechat_channels')
    done=panel.douyin
    done_key=done.platform+':'+done.current().account_id
    pending_key=view.platform+':'+view.current().account_id
    path=tmp_path/'virtual.mp4'
    digest='fixture-content-sha256'
    MultiTargetLedger(panel.store.root).set_result(digest,done_key,'SUCCESS','real platform receipt')
    MultiTargetLedger(panel.store.root).set_result(digest,pending_key,'AUTH_REQUIRED','needs login',attempt_count=1)
    held=tmp_path/'publiced'/'virtual.mp4';held.parent.mkdir(exist_ok=True);held.write_bytes(b'fixture')
    provenance={canonical_local_path(path):dict(manual_hold=True,hold_id='hold-id',held_path=str(held),
        target_keys=[done_key,pending_key],sha256=digest,label='2026-10-25',tooltip='人工暂停，未计为成功')}
    panel.apply_queue_snapshots([(view.platform,view.queue_request(),[(path,held.stat())],None,'',provenance)])
    assert view.table_queue.rowCount()==1
    assert view.table_queue.item(0,5+PUBLISH_ORDER.index(done.platform)).text()==STATUS_TEXT['SUCCESS']
    assert view.table_queue.item(0,5+PUBLISH_ORDER.index(view.platform)).text()==STATUS_TEXT['MANUAL_HOLD']
    assert view.restore_hold_button.isEnabled()


def test_queue_new_owner_same_name_file_is_not_overlaid_by_previous_owners_hold(tmp_path,monkeypatch):
    from src.app.channel_panel import QueueSnapshotWorker
    from src.publisher import dom_readiness
    from tests.test_google_drive import ingest
    from src.publisher.drive_ingest import DriveJournal
    app=QApplication.instance() or QApplication([])
    root,first,second,api,path,digest=held_source(tmp_path)
    discover_manual_holds(root,[first,second],cloud_account=api.account_identity())
    new_api=FakeDrive()
    new_api.account_identity=lambda:'new-verified-drive-owner'
    ready=[]
    ingest(root,new_api,ready=ready.append).poll(second)
    assert new_api.downloads==['f1']
    new_path=Path(ready[0])
    # Normal reservation suffixes another owner's claimed name. Construct the
    # supported physical-provenance shape explicitly to exercise the UI guard.
    new_path.rename(path)
    identity,binding=DriveJournal.identity(second,new_api.files[0],new_api.account_identity())
    with DriveJournal(root).connect() as db:
        db.execute('UPDATE files SET path=? WHERE identity=?',(str(path),identity))
    monkeypatch.setattr(dom_readiness,'publishing_dom_state',lambda *args:None)
    worker=QueueSnapshotWorker(root,[(second.platform,(second.account.account_id,
        second.account.target_dir,second.account.media_types))],None,hold_targets=[first,second])
    received=[];worker.snapshot_ready.connect(received.extend);worker.run()
    provenance=received[0][5]
    assert not provenance.get(canonical_local_path(path),{}).get('manual_hold')
    assert any(file==path for file,stamp in received[0][2])


def test_global_request_merges_while_busy_and_runs_serially_once_after_current_round(tmp_path,monkeypatch):
    from dataclasses import replace
    from src.publisher import drive_ingest
    app=QApplication.instance() or QApplication([])
    first=target(tmp_path)
    second=replace(first,account=replace(first.account,account_id='second'))
    worker=DriveWatchWorker(tmp_path/'state',SimpleNamespace(connected=lambda:True,generation=0),client=FakeDrive())
    worker.configure([first,second],poll_seconds=20)
    monkeypatch.setattr(drive_ingest.time,'monotonic',lambda:100.)
    calls=[];requests=[];depth=0
    def poll(ingestor,current):
        nonlocal depth
        depth+=1
        assert depth==1
        calls.append(current.key)
        if len(calls)==1:requests.extend([worker.check_all_now(),worker.check_all_now(),worker.check_all_now()])
        ingestor.cloud_account='verified-owner'
        depth-=1
        return True
    monkeypatch.setattr(DriveIngestor,'poll',poll)
    waits=[]
    def wait(seconds):
        waits.append(seconds)
        if len(waits)==2:worker.stop()
    monkeypatch.setattr(worker.wake,'wait',wait)
    worker.watch_loop()
    assert requests==[True,False,False]
    assert calls==[first.key,second.key,first.key,second.key]
    assert not worker.check_all_pending
    assert worker.snapshot()['cloud_account']=='verified-owner'
    pointer=json.loads((tmp_path/'state/.cloud_diagnostics/latest-scan.json').read_text(encoding='utf-8'))
    assert pointer['reason']=='manual_all' and len(pointer['scans'])==2
    worker.configure([],paused=True)
    assert not worker.check_all_now()
