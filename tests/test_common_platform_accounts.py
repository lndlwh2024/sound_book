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
    yield widget
    widget.prepare_shutdown()
    widget.close()


@pytest.mark.parametrize('index',range(4))
def test_same_form_saves_and_reloads_common_fields(panel,index):
    current=panel.panels[index]
    assert type(current) is type(panel.panels[0])
    account=current.current()
    original_id,original_profile=account.account_id,panel.repository.profile(account)
    current.name.setText('新备注')
    current.identity.setText('核对备注')
    current.enabled.setChecked(False)
    current.auto_archive.setChecked(False)
    for key,check in current.media_checks.items():
        check.setChecked(key in ('audio','image'))
    assert current.save_current()
    loaded=PlatformAccountRepository(ChannelAccountStore(panel.store.root),
        DouyinAccountManager(panel.repository.douyin_manager.config_file,panel.repository.douyin_manager.profiles_base_dir))
    restored=loaded.accounts(current.platform)[0]
    assert isinstance(restored,ChannelAccount)
    assert restored.account_name=='新备注' and restored.identity_note=='核对备注'
    assert restored.media_types==['audio','image'] and not restored.enabled and not restored.auto_archive
    assert restored.account_id==original_id and loaded.profile(restored)==original_profile


@pytest.mark.parametrize('index',range(4))
def test_identity_updates_captured_account_without_changing_id_profile_or_selection(panel,index):
    current=panel.panels[index]
    captured=current.current()
    original_id,profile=captured.account_id,panel.repository.profile(captured)
    other=panel.repository.add(current.platform,'第二账号')
    current.reload()
    current.accounts.setCurrentRow(1)
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


def test_youtube_channel_id_is_directly_available_outside_collapsed_parameters(panel):
    current=panel.panels[3]
    panel.channel_view.setCurrentIndex(3)
    panel.resize(1640,940)
    panel.show()
    QApplication.processEvents()
    assert not current.special_toggle.isChecked()
    assert current.channel_id.isVisible() and not current.special.isAncestorOf(current.channel_id)
    current.channel_id.setText('UC'+'a'*22)
    assert current.save_current()
    reloaded=ChannelAccountStore(panel.store.root).for_channel('youtube')[0]
    assert reloaded.channel_id=='UC'+'a'*22


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
