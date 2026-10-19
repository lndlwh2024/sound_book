"""Common configuration contract, durable account identity and local DOM extraction."""
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from playwright.sync_api import sync_playwright
from src.app import channel_panel as ui, douyin_panel
from src.publisher.platforms import CHANNELS, ChannelAccountStore, ChannelAccount
from src.publisher.douyin.config import DouyinAccountManager
from src.publisher.account_repository import PlatformAccountRepository
from src.publisher.identity import extract_platform_identity
from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger, PublishTarget


@pytest.fixture
def panel(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    manager=DouyinAccountManager(tmp_path/'douyin.json',tmp_path/'profiles')
    manager.add_account('备注',target_dir=str(tmp_path))
    monkeypatch.setattr(ui,'DouyinAccountManager',lambda:manager)
    store=ChannelAccountStore(tmp_path/'new')
    for platform in list(CHANNELS)[1:]:
        account=store.add(platform,'备注')
        account.target_dir=str(tmp_path)
    store.save()
    widget=ui.MultiPlatformPublisherPanel(store=store)
    monkeypatch.setattr(ui.QMessageBox,'warning',MagicMock())
    yield widget
    widget.prepare_shutdown()
    for worker in (widget.cleanup_worker,widget.snapshot_worker,widget.drive_watch_worker,widget.drive_auth_worker):
        if worker:
            worker.wait(3000)
    QApplication.processEvents()
    widget.close()
    # Release Qt-owned signal/child cycles before the next theme/layout test.
    from PySide6.QtCore import QCoreApplication, QEvent
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def finish_cleanup(panel):
    deadline=time.monotonic()+5
    while panel.cleanup_worker and time.monotonic()<deadline:
        QApplication.processEvents()
        time.sleep(.005)
    assert panel.cleanup_worker is None


def test_periodic_scan_does_not_block_tabs_or_other_worker(panel, monkeypatch):
    import threading
    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtWidgets import QTabWidget, QWidget
    from src.publisher import dom_readiness
    started, release = threading.Event(), threading.Event()
    original = dom_readiness.account_dom_state
    threads = []
    def slow_dom(*args):
        threads.append(QThread.currentThread())
        started.set()
        release.wait(3)
        return original(*args)
    monkeypatch.setattr(dom_readiness, 'account_dom_state', slow_dom)
    class Production(QThread):
        def run(self):
            release.wait(3)
    production = Production()
    tabs = QTabWidget()
    tabs.addTab(QWidget(), '生产')
    tabs.addTab(panel, '部署')
    tabs.setCurrentIndex(1)
    beats = []
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
    heartbeat.start(10)
    production.start()
    try:
        panel.refresh_queues()
        worker = panel.snapshot_worker
        assert started.wait(1)
        panel.refresh_queues()
        assert panel.snapshot_worker is worker  # No accumulating scan threads.
        tabs.setCurrentIndex(0)
        deadline = time.monotonic()+0.2
        while time.monotonic()<deadline:
            QApplication.processEvents()
            time.sleep(.005)
        assert len(beats)>=5
        assert tabs.currentIndex()==0 and production.isRunning()
        assert all(thread != QApplication.instance().thread() for thread in threads)
        assert not panel.prepare_shutdown()  # Retain the scan thread until it exits.
    finally:
        release.set()
        heartbeat.stop()
        production.wait(3000)
        worker.wait(3000)
        QApplication.processEvents()
        tabs.removeTab(tabs.indexOf(panel))
        panel.setParent(None)
        tabs.close()


def test_queue_snapshot_ignores_changed_directory_and_reuses_unchanged_rows(panel, tmp_path):
    current=panel.panels[0]
    path=tmp_path/'test.mp4'
    path.write_bytes(b'video')
    request=current.queue_request()
    files=[(path.resolve(),path.stat())]
    panel.apply_queue_snapshots([(current.platform,request,files,None,'')])
    item=current.table_queue.item(0,1)
    panel.apply_queue_snapshots([(current.platform,request,files,None,'')])
    assert current.table_queue.item(0,1) is item
    empty=tmp_path/'empty'
    empty.mkdir()
    current.directory.setText(str(empty))
    panel.apply_queue_snapshots([(current.platform,request,files,None,'')])
    assert current.table_queue.rowCount()==0


def test_result_events_coalesce_and_do_not_recreate_manual_controls(panel, tmp_path):
    path=tmp_path/'test.mp4'
    path.write_bytes(b'video')
    current=panel.panels[0]
    current.scan_queue()
    stat=path.stat()
    result={'file_path':str(path.resolve()),'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
            'results':{'douyin:example':{'status':'UNCONFIRMED','message':'待核对'}}}
    for _ in range(30):
        panel.receive_result(result)
    assert panel.render_timer.isActive()
    panel.render_timer.stop()
    panel.render_results()
    widget=current.table_queue.cellWidget(0,5)
    assert widget is None
    panel.render_results()
    assert current.table_queue.cellWidget(0,5) is widget


@pytest.mark.parametrize('index',range(4))
def test_same_form_saves_and_reloads_common_fields(panel,index):
    current=panel.panels[index]
    assert type(current) is type(panel.panels[0])
    account=current.current()
    original_id,original_profile=account.account_id,panel.repository.profile(account)
    current.name.setText('新备注')
    current.identity.setText('核对备注')
    current.disable_button.click()
    current.auto_archive.setChecked(False)
    for key,check in current.media_checks.items():
        check.setChecked(key in ('audio','image'))
    assert current.save_current()
    loaded=PlatformAccountRepository(ChannelAccountStore(panel.store.root),
        DouyinAccountManager(panel.repository.douyin_manager.config_file,panel.repository.douyin_manager.profiles_base_dir))
    restored=loaded.accounts(current.platform)[0]
    assert isinstance(restored,ChannelAccount)
    assert restored.account_name==account.account_name and restored.identity_note=='核对备注'
    assert current.name.isHidden()  # Editable nickname was explicitly removed.
    assert restored.media_types==['audio','image'] and not restored.enabled and not restored.auto_archive
    assert restored.account_id==original_id and loaded.profile(restored)==original_profile


def test_xiaohongshu_collection_uses_shared_field_and_preserves_identity(panel):
    current=panel.panels[1]
    owner=current.current()
    old_id,old_profile=owner.account_id,panel.repository.profile(owner)
    current.collection.setText('一分钟趣味知识')
    assert current.save_current()
    current.collection.clear()
    current.load_current()
    assert current.collection.text()=='一分钟趣味知识'
    loaded=ChannelAccountStore(panel.store.root).for_channel('xiaohongshu')[0]
    assert loaded.collection_name=='一分钟趣味知识'
    assert loaded.account_id==old_id and panel.repository.profile(loaded)==old_profile


@pytest.mark.parametrize('index',range(4))
def test_identity_updates_captured_account_without_changing_id_profile_or_selection(panel,index):
    current=panel.panels[index]
    captured=current.current()
    original_id,profile=captured.account_id,panel.repository.profile(captured)
    other=panel.repository.add(current.platform,'第二账号')
    current.reload()
    current.accounts.setCurrentIndex(1)
    current.apply_identity(captured,{'nickname':'平台真实昵称','platform_uid':'UC'+'a'*22 if current.platform=='youtube' else ''})
    assert current.current().account_id==other.account_id
    assert current.name.text()=='第二账号'
    saved=panel.repository.accounts(current.platform)[0]
    assert saved.platform_nickname=='平台真实昵称' and saved.account_name=='平台真实昵称'
    assert saved.account_id==original_id and panel.repository.profile(saved)==profile
    assert saved.login_status=='AUTHORIZED' and saved.last_auth_time


def test_youtube_wrong_channel_does_not_override_binding(panel):
    current=panel.panels[3]
    account=current.current()
    account.channel_id='UC'+'a'*22
    current.apply_identity(account,{'nickname':'其他频道','channel_id':'UC'+'b'*22})
    assert account.account_name=='备注' and account.channel_id=='UC'+'a'*22
    assert not account.platform_nickname


def test_capture_button_is_beside_login_and_all_platforms_share_pause_controls(panel):
    from PySide6.QtWidgets import QGridLayout, QPushButton
    for current in panel.panels:
        rows=current.findChildren(QGridLayout)
        assert any(any(row.itemAt(i).widget() is current.login_button for i in range(row.count()))
                   and any(row.itemAt(i).widget() is current.capture_button for i in range(row.count()))
                   for row in rows)
        assert not current.pause_button.isEnabled()
        assert current.capture_button not in current.deployment_controls.findChildren(QPushButton)
    running=MagicMock()
    running.isRunning.return_value=True
    import threading
    running.stopping=threading.Event()
    running.pause_requested=threading.Event()
    running.pause_all.side_effect=running.pause_requested.set
    running.resume.side_effect=running.pause_requested.clear
    panel.worker=running
    panel.sync_deployment_buttons()
    assert all(p.pause_button.isEnabled() for p in panel.panels)
    panel.panels[1].pause_button.click()
    assert running.pause_requested.is_set()
    assert all(p.pause_button.text()=='继续全部平台' for p in panel.panels)
    panel.panels[3].pause_button.click()
    assert not running.pause_requested.is_set()
    assert all(p.pause_button.text()=='暂停全部平台' for p in panel.panels)
    running.stop.side_effect=running.stopping.set
    panel.panels[2].deploy_button.click()
    running.stop.assert_called_once()
    assert all(not p.pause_button.isEnabled() for p in panel.panels)
    panel.worker=None
    panel.sync_deployment_buttons()
    assert all(not p.pause_button.isEnabled() for p in panel.panels)


@pytest.mark.parametrize('platform',['xiaohongshu','kuaishou'])
def test_captured_dom_updates_form_without_bypassing_login_preflight(panel,platform,monkeypatch):
    import json
    from src.publisher.platforms import CHANNELS
    current=next(p for p in panel.panels if p.platform==platform)
    account=current.current()
    path=panel.store.root/'dom_reports'/platform/account.account_id/'session'/'capture.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'schema_version':1,'platform':platform,'account_id':account.account_id,
        'url':CHANNELS[platform].url,'frames':[{'nodes':[
            {'tag':'input','type':'file','accept':'.mp4'},
            {'tag':'div','contentEditable':True,'visible':True}]}]}),encoding='utf-8')
    current.refresh_dom_state()
    assert '已采集发布页' in current.dom_state.text()
    assert current.dom_state.toolTip()==str(path)
    for key,check in panel.channel_checks.items():
        check.setChecked(key==platform)
    panel.choose_targets=MagicMock(return_value=[PublishTarget(platform,account,panel.repository.profile(account))])
    factory=MagicMock()
    monkeypatch.setattr(ui,'MultiDeployWorker',factory)
    warning=MagicMock()
    monkeypatch.setattr(ui.QMessageBox,'warning',warning)
    panel.start_deployment()
    finish_cleanup(panel)
    factory.assert_not_called()
    assert '登录' in warning.call_args.args[-1]
    assert '当前账号尚未采集' not in warning.call_args.args[-1]
    # Existing DOM remains usable. Completing local login prerequisites starts
    # only the check; the deployment still waits for live validation to finish.
    import threading
    from src.publisher.preflight import PreflightResult
    profile = Path(panel.repository.profile(account))
    profile.mkdir(parents=True)
    (profile/'test-marker').write_text('local',encoding='utf-8')
    account.platform_nickname='本人昵称'
    check = MagicMock()
    check.targets=panel.choose_targets.return_value
    check.stopping=threading.Event()
    check.results=[PreflightResult(check.targets[0].key,platform,'SUCCESS','通过')]
    monkeypatch.setattr(ui,'DeploymentPreflightWorker',MagicMock(return_value=check))
    warning.reset_mock()
    panel.start_deployment()
    finish_cleanup(panel)
    check.start.assert_called_once()
    factory.assert_not_called()
    panel.preflight_finished()
    factory.assert_called_once()
    factory.return_value.start.assert_called_once()
    warning.assert_not_called()
    panel.worker=None


@pytest.mark.parametrize('index',[1,2])
def test_creator_title_rule_and_description_save_in_shared_account_model(panel,index):
    current=panel.panels[index]
    current.description.setPlainText('用户配置的正文')
    current.creator_fields['title_mode'].setCurrentIndex(1)
    assert current.save_current()
    current.reload()
    assert current.current().description=='用户配置的正文'
    assert current.current().title_mode=='filename'
    assert current.description.toPlainText()=='用户配置的正文'


def test_youtube_channel_id_is_available_in_expanded_platform_parameters(panel):
    current=panel.panels[3]
    panel.channel_view.setCurrentIndex(3)
    panel.resize(1640,940)
    panel.show()
    current.settings_tabs.setCurrentIndex(1)
    QApplication.processEvents()
    assert current.special.isVisible()
    assert current.channel_id.isVisible() and current.special.isAncestorOf(current.channel_id)
    assert not current.special.isCheckable()
    current.channel_id.setText('UC'+'a'*22)
    assert current.save_current()
    reloaded=ChannelAccountStore(panel.store.root).for_channel('youtube')[0]
    assert reloaded.channel_id=='UC'+'a'*22


@pytest.mark.parametrize('index',range(4))
def test_common_account_switches_stay_visible_with_expanded_options(panel,index):
    from PySide6.QtWidgets import QScrollArea
    panel.resize(1280,800)
    panel.channel_view.setCurrentIndex(index)
    panel.show()
    current=panel.panels[index]
    QApplication.processEvents()
    assert current.findChild(QScrollArea) is None
    assert current.settings_tabs.currentIndex()==0
    current.settings_tabs.setCurrentIndex(1)
    assert current.enabled.isHidden()
    for control in (current.auto_archive,current.save_button):
        QApplication.processEvents()
        assert control.isVisible()
        assert panel.rect().contains(control.mapTo(panel,control.rect().center()))
    assert current.deploy_button.isVisible()
    assert not current.configuration_group.isAncestorOf(current.save_button)
    assert set(current.media_checks)=={'video','audio','image'}


@pytest.mark.parametrize('index',range(4))
def test_unknown_results_have_no_manual_feedback_and_old_reports_are_preserved(panel,tmp_path,index):
    import hashlib,json
    path=tmp_path/'clip.mp4'
    path.write_bytes(b'fixture'*400)
    stat=path.stat()
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    ledger=MultiTargetLedger(panel.store.root)
    results={view.platform+':'+view.current().account_id:ledger.set_result(digest,view.platform+':'+view.current().account_id,'UNCONFIRMED','old result')
        for view in panel.panels}
    item={'file_path':str(path.resolve()),'file_name':path.name,'sha256':digest,
          'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'results':results}
    folder=panel.store.root/'publish_reports';folder.mkdir()
    original=json.dumps({'files':[item]})
    (folder/'test.json').write_text(original,encoding='utf-8')
    panel.restore_results()
    current=panel.panels[index]
    current.scan_queue()
    panel.render_results()
    assert current.table_queue.cellWidget(0,5+index) is None
    assert current.table_queue.item(0,5+index).text()=='待上传'
    assert not hasattr(panel,'feedback_result')
    assert not (panel.store.root/'result_reviews.json').exists()
    assert (folder/'test.json').read_text(encoding='utf-8')==original



@pytest.mark.parametrize('index',range(4))
def test_ui_scan_and_deploy_share_media_selection_including_empty(panel,index,tmp_path):
    current=panel.panels[index]
    for name in ('clip.mp4','song.mp3','cover.jpg'):
        path=tmp_path/name
        path.write_bytes(b'complete'*256)
        os.utime(path,(time.time()-10,time.time()-10))
    for key,check in current.media_checks.items():
        check.setChecked(key in ('audio','image'))
    assert current.save_current()
    assert {current.table_queue.item(r,1).text() for r in range(current.table_queue.rowCount())}=={'song.mp3','cover.jpg'}
    current.table_queue.setCurrentCell(0,1)
    selected=current.table_queue.item(0,1).text()
    current.scan_queue()
    assert current.table_queue.item(current.table_queue.currentRow(),1).text()==selected
    account=panel.repository.publish_account(current.current())
    worker=MultiDeployWorker([PublishTarget(current.platform,account,panel.repository.profile(current.current()))],root=tmp_path/'state')
    worker.ledger=MultiTargetLedger(worker.root)
    assert {j['path'].name for j in worker.plan_batch()}=={'song.mp3','cover.jpg'}
    for check in current.media_checks.values():
        check.setChecked(False)
    assert current.save_current()
    account=panel.repository.publish_account(current.current())
    worker=MultiDeployWorker([PublishTarget(current.platform,account,panel.repository.profile(current.current()))],root=tmp_path/'empty')
    worker.ledger=MultiTargetLedger(worker.root)
    assert current.table_queue.rowCount()==0 and not worker.plan_batch()


def test_account_selector_height_does_not_grow_with_more_accounts(panel):
    from PySide6.QtWidgets import QApplication,QComboBox
    panel.resize(1640,940)
    panel.show()
    app=QApplication.instance()
    for index,current in enumerate(panel.panels):
        panel.channel_view.setCurrentIndex(index)
        app.processEvents()
        initial_height=current.account_group.height()
        for number in range(15):
            panel.repository.add(current.platform,f'additional {number}')
        current.reload()
        app.processEvents()
        assert isinstance(current.accounts,QComboBox)
        assert current.accounts.count()==len(current._accounts)
        assert current.account_selector_group.title()==f'账号选择-{current.accounts.count()}个账号'
        assert abs(current.account_group.height()-initial_height)<=4


@pytest.mark.parametrize('index',range(4))
def test_selector_count_crud_and_enable_state_are_per_account(panel,monkeypatch,index):
    view=panel.panels[index]
    original=view.current()
    view.add_account()
    added=view.current()
    assert added.account_id!=original.account_id and view.accounts.count()==2
    assert view.account_selector_group.title()=='账号选择-2个账号'
    view.disable_button.click()
    assert not view.current().enabled
    view.accounts.setCurrentIndex(view.accounts.findData(original.account_id))
    assert view.current().enabled and view.enable_button.isChecked()
    view.accounts.setCurrentIndex(view.accounts.findData(added.account_id))
    assert view.disable_button.isChecked()
    profile=Path(panel.repository.profile(added));profile.mkdir(parents=True,exist_ok=True)
    marker=profile/'retained-test-marker';marker.write_text('test profile')
    monkeypatch.setattr(ui.QMessageBox,'question',lambda *a,**kw:ui.QMessageBox.No)
    view.delete_account()
    assert view.accounts.count()==2
    monkeypatch.setattr(ui.QMessageBox,'question',lambda *a,**kw:ui.QMessageBox.Yes)
    view.delete_account()
    assert view.accounts.count()==1 and view.current().account_id==original.account_id
    assert marker.exists() and view.account_selector_group.title()=='账号选择-1个账号'
    view.delete_account()
    assert view.accounts.count()==0 and view.current() is None
    assert view.account_selector_group.title()=='账号选择-0个账号'
    assert not view.accounts.isEnabled() and not view.enable_button.isEnabled()
    assert not view.disable_button.isEnabled() and not view.settings_tabs.isEnabled()
    assert not view.enable_button.isChecked() and not view.disable_button.isChecked()
    assert not view.save_button.isEnabled() and view.shared_drive.isEnabled()
    view.add_account()
    assert view.accounts.count()==1 and view.current() is not None
    assert view.settings_tabs.isEnabled() and view.save_button.isEnabled()


@pytest.mark.parametrize('index',range(4))
def test_save_configuration_button_gives_success_feedback(panel,index):
    current=panel.panels[index]
    current.save_button.click()
    assert '已保存' in current.save_button.text()
    assert '[配置保存成功]' in current.logs.toPlainText()


@pytest.mark.parametrize('archive',[True,False])
def test_manual_cleanup_archives_completed_ignored_plan_even_when_auto_archive_disabled(panel,tmp_path,archive):
    import hashlib
    from src.publisher.scan_cleanup import ScanCleanupWorker
    path=tmp_path/'reviewed.mp4'
    path.write_bytes(b'fixture'*400)
    os.utime(path,(time.time()-10,time.time()-10))
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    ledger=MultiTargetLedger(panel.store.root)
    for check in panel.channel_checks.values():check.setChecked(True)
    group=panel.archive_group()
    for target in group:
        target.account.auto_archive=archive
        ledger.set_result(digest,target.key,'IGNORED' if target.platform=='kuaishou' else 'SUCCESS','history')
    scanner=ScanCleanupWorker(group[:1],group,panel.store.root)
    scanner.run()
    assert not scanner.error
    assert not path.exists() and (tmp_path/'published'/path.name).exists()
    assert MultiTargetLedger(panel.store.root).result(digest,group[2].key)['status']=='IGNORED'



def test_queue_columns_align_fit_contents_and_resize_file_name(panel,tmp_path):
    for check in panel.channel_checks.values():
        check.setChecked(True)
    panel.update_selection()
    import hashlib
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication,QToolButton
    current=panel.douyin
    path=tmp_path/'书声_一分钟趣味知识_20261004_第14问_较长的媒体文件名称.mp4'
    path.write_bytes(b'fixture'*400)
    stat=path.stat()
    results={view.platform+':'+view.current().account_id:{'status':'UNCONFIRMED' if view.platform=='kuaishou' else 'SUCCESS','message':'fixture'} for view in panel.panels}
    panel.results[str(path.resolve())]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'results':results}
    current.scan_queue()
    panel.resize(1280,720)
    panel.show()
    app=QApplication.instance()
    app.processEvents()
    table=current.table_queue
    narrow=table.columnWidth(1)
    assert table.horizontalHeader().defaultAlignment()==Qt.AlignCenter
    assert table.verticalHeader().isHidden()
    for column in (0,2,3,4,5,6,7,8):
        assert table.item(0,column).textAlignment()==Qt.AlignCenter
    assert table.cellWidget(0,7) is None
    assert table.columnWidth(7)>=table.fontMetrics().horizontalAdvance(table.item(0,7).text())+8
    narrow_height=table.rowHeight(0)
    panel.resize(1760,940)
    app.processEvents()
    assert table.columnWidth(1)>narrow
    assert table.rowHeight(0)<=narrow_height
    assert table.textElideMode()==Qt.ElideNone and table.wordWrap()


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_local_dom_reads_only_own_identity_and_preserves_unknown(platform):
    account_id='UC'+'a'*22
    url='https://'+CHANNELS[platform].host+('/channel/'+account_id+'/videos' if platform=='youtube' else '/publish')
    own='<ytcp-navigation-drawer><div id="entity-name">真实昵称</div></ytcp-navigation-drawer>' if platform=='youtube' else '<header><span class="user-name nickname">真实昵称</span></header>'
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route:route.fulfill(body='<html><meta charset="utf-8"><body>'+own+'</body></html>',content_type='text/html; charset=utf-8'))
            page.goto(url)
            identity=extract_platform_identity(page,platform)
            assert identity['nickname']=='真实昵称'
            assert identity['channel_id']==(account_id if platform=='youtube' else '')
            page.set_content('<title>错误昵称</title><article><span class="nickname user-name">作品作者</span></article>')
            assert extract_platform_identity(page,platform) is None
            page.set_content(own+'<input type="password">')
            assert extract_platform_identity(page,platform) is None
            if platform!='youtube':
                page.set_content('<header><span class="user-name nickname">甲</span><span class="user-name nickname">乙</span></header>')
                assert extract_platform_identity(page,platform) is None
        finally:
            browser.close()


def test_corrupt_douyin_configuration_is_preserved(tmp_path):
    path=tmp_path/'accounts.json'
    path.write_text('{broken',encoding='utf-8')
    manager=DouyinAccountManager(path,tmp_path/'profiles')
    assert manager.load_error and not manager.save()
    assert path.read_text(encoding='utf-8')=='{broken'


@pytest.mark.parametrize('index',range(4))
@pytest.mark.parametrize('method',['type','browse'])
def test_new_directory_scans_before_save_for_every_platform(panel,index,tmp_path,monkeypatch,method):
    from src.app.platform_account_panel import QFileDialog
    current=panel.panels[index]
    account=current.current()
    account.target_dir=''
    panel.repository.save(account)
    current.load_current()
    directory=tmp_path/'新媒体目录'
    directory.mkdir()
    video=directory/'待发布视频.MP4'
    video.write_bytes(b'complete'*256)
    saved=(panel.store.path if index else panel.repository.douyin_manager.config_file).read_bytes()
    if method=='browse':
        monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(directory))
        current.browse()
    else:
        current.directory.setText(str(directory))
    current.scan_button.click()
    finish_cleanup(panel)
    assert current.table_queue.rowCount()==1
    assert current.table_queue.item(0,1).text()==video.name
    assert current.queue_file_path(0)==video.resolve()
    assert current.scan_button.text()=='扫描目录清理文件'
    assert current.current().target_dir==str(directory)
    assert (panel.store.path if index else panel.repository.douyin_manager.config_file).read_bytes()!=saved
    # The timer must keep displaying this directory instead of reverting to the saved empty path.
    current.table_queue.setCurrentCell(0,1)
    panel.refresh_queues()
    assert current.table_queue.rowCount()==1 and current.table_queue.currentRow()==0
    assert current.save_current()
    assert panel.repository.accounts(current.platform)[0].target_dir==str(directory)


@pytest.mark.parametrize('index',range(4))
def test_scan_feedback_explains_invalid_directory_or_empty_media_filter(panel,index,tmp_path):
    current=panel.panels[index]
    current.directory.clear()
    current.scan_button.click()
    assert '请先选择或填写工作目录' in current.logs.toPlainText()
    current.directory.setText(str(tmp_path/'不存在'))
    current.scan_button.click()
    assert '不存在或无法访问' in current.logs.toPlainText()
    current.directory.setText(str(tmp_path))
    for check in current.media_checks.values():
        check.setChecked(False)
    current.scan_button.click()
    assert '未选择媒体类型' in current.logs.toPlainText()
    assert current.table_queue.rowCount()==0


@pytest.mark.parametrize('index',range(4))
def test_unsaved_directory_result_and_manual_upload_use_actual_scanned_path(panel,index,tmp_path):
    current=panel.panels[index]
    stored=tmp_path/'old_media'
    displayed=tmp_path/'new_media'
    stored.mkdir()
    displayed.mkdir()
    for directory in (stored,displayed):
        (directory/'same.mp4').write_bytes(b'complete'*256)
    current.current().target_dir=str(stored)
    panel.repository.save(current.current())
    current.load_current()
    current.directory.setText(str(displayed))
    video=displayed/'same.mp4'
    key=current.platform+':'+current.current().account_id
    stat=video.stat()
    panel.results[str(video.resolve())]={
        'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,
        'results':{key:{'status':'FAILED','message':'new directory diagnostic'}}}
    panel.render_results()
    assert current.table_queue.item(0,5+index).text()=='失败'
    current.table_queue.setCurrentCell(0,1)
    panel.start_deployment=MagicMock()
    current.upload_selected()
    panel.start_deployment.assert_called_once_with(file_paths=[video.resolve()])
    assert panel.repository.accounts(current.platform)[0].target_dir==str(displayed)
