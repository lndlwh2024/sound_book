"""Optional nickname metadata and durable cloud dates never decide completion."""
import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.publisher import identity
from src.publisher.drive_ingest import DriveJournal, queue_provenance, canonical_local_path
from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger, PublishTarget
from src.publisher.platforms import CHANNELS, ChannelAccount
from src.publisher.preflight import AccountPreflight
from tests.test_common_platform_accounts import panel
from tests.test_publishing_options import app
from tests.test_drive_resume import DateDrive, poll
from tests.test_drive_shared_download import shared_targets
from tests.test_google_drive import cloud_file


@pytest.fixture
def live_page():
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(body='<meta charset="utf-8">',content_type='text/html'))
        yield page
        browser.close()


def live_account(page,tmp_path,platform,custom=''):
    account=ChannelAccount('a'*32,platform,'旧昵称',platform_nickname='旧昵称',custom_nickname=custom,
                           channel_id='UC'+'a'*22)
    page.goto(CHANNELS[platform].url if platform!='youtube' else
              'https://studio.youtube.com/channel/'+account.channel_id)
    page.set_content('''<header class="creator-header"><div class="user-info"><span class="name">新昵称</span></div></header>
        <aside id="side-bar"><div class="side-bar-footer"><div class="account-info"><span class="name">新昵称</span></div></div></aside>
        <ytcp-navigation-drawer><span id="entity-name">新昵称</span></ytcp-navigation-drawer>
        <input placeholder="短标题"><input type=file accept="video/*"><button onclick="window.posted=true">发表</button>''')
    return account,AccountPreflight(PublishTarget(platform,account,str(tmp_path/'profile')),seconds=.5)


@pytest.mark.parametrize('platform',list(CHANNELS))
@pytest.mark.parametrize('custom',['','手工备注'])
def test_all_platforms_sync_rename_without_touching_upload(live_page,tmp_path,platform,custom):
    account,check=live_account(live_page,tmp_path,platform,custom)
    changes=[]
    check.nickname_changed=changes.append
    check.verify_page(live_page)
    assert account.platform_nickname=='新昵称' and account.account_name==(custom or '新昵称')
    assert account.account_id=='a'*32 and account.channel_id=='UC'+'a'*22
    assert changes and changes[0]['nickname']=='新昵称'
    assert live_page.locator('input[type=file]').evaluate('e=>e.files.length')==0
    assert not live_page.evaluate('window.posted===true')


@pytest.mark.parametrize('platform',list(CHANNELS))
@pytest.mark.parametrize('failure',['read','save'])
def test_optional_nickname_failure_does_not_reject_access(live_page,tmp_path,monkeypatch,platform,failure):
    account,check=live_account(live_page,tmp_path,platform)
    logs=[];check.log=logs.append
    if failure=='read':
        monkeypatch.setattr(identity,'extract_platform_identity',MagicMock(side_effect=RuntimeError('昵称暂不可读')))
    else:
        check.nickname_changed=MagicMock(side_effect=OSError('配置暂不可写'))
    check.verify_page(live_page)
    assert any('继续发布' in line for line in logs)
    assert live_page.locator('input[type=file]').evaluate('e=>e.files.length')==0


def test_legacy_douyin_config_without_platform_attribute_uses_same_optional_policy(live_page,tmp_path):
    from src.publisher.douyin.models import DouyinAccountConfig
    live_account(live_page,tmp_path,'douyin')
    account=DouyinAccountConfig('a'*32,'旧昵称',platform_nickname='旧昵称')
    check=AccountPreflight(PublishTarget('douyin',account,str(tmp_path/'profile')),seconds=.5)
    check.verify_page(live_page)
    assert account.platform_nickname=='新昵称'


def test_ui_persists_by_account_id_without_switching_selection_or_overwriting_edits(panel,monkeypatch):
    view=next(p for p in panel.panels if p.platform=='kuaishou')
    selected=view.current(); second=panel.repository.add('kuaishou','另一账号')
    second.custom_nickname='固定备注';panel.repository.save(second);view.reload()
    view.accounts.setCurrentIndex(view.accounts.findData(selected.account_id))
    view.directory.setText('未保存的目录编辑')
    panel.receive_nickname({'platform':'kuaishou','account_id':second.account_id,'nickname':'改名后的昵称'})
    assert view.current().account_id==selected.account_id
    assert view.directory.text()=='未保存的目录编辑'
    assert second.platform_nickname=='改名后的昵称' and second.account_name=='固定备注'
    from src.publisher.platforms import ChannelAccountStore
    restored=next(a for a in ChannelAccountStore(panel.store.root).for_channel('kuaishou') if a.account_id==second.account_id)
    assert restored.platform_nickname=='改名后的昵称'
    monkeypatch.setattr(panel.repository,'save',MagicMock(side_effect=OSError('locked')))
    panel.receive_nickname({'platform':'kuaishou','account_id':second.account_id,'nickname':'保存失败'})
    assert second.platform_nickname=='改名后的昵称'
    panel.repository.delete(second)
    panel.receive_nickname({'platform':'kuaishou','account_id':second.account_id,'nickname':'不复活已删除账号'})
    assert all(a.account_id!=second.account_id for a in panel.repository.accounts('kuaishou'))


def cloud_setup(tmp_path,root=None):
    a,b=shared_targets(tmp_path)
    for target in (a,b):target.account.auto_archive=True
    root=root or tmp_path/'state'
    api=DateDrive({'2026-10-22':[cloud_file(name='文件名却写20261023.mp4')]})
    poll(root,api,a,'2026-10-22');poll(root,api,b,'2026-10-22')
    path=next(Path(a.account.target_dir).glob('*.mp4'))
    return a,b,root,api,path,hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('state',['PENDING','FAILED2','RECOVERY_REQUIRED','SUCCESS'])
def test_cross_day_keeps_media_until_all_targets_succeed_and_archive_finishes(tmp_path,monkeypatch,app,state):
    a,b,root,api,path,digest=cloud_setup(tmp_path)
    ledger=MultiTargetLedger(root)
    ledger.set_result(digest,a.key,'SUCCESS','confirmed')
    ledger.set_result(digest,b.key,state,'history',requires_review=state=='RECOVERY_REQUIRED')
    # Date catch-up cannot reset success/unknown results or cause re-downloads.
    poll(root,api,a,'2026-10-23');poll(root,api,b,'2026-10-23')
    assert len(api.downloads)==1 and path.is_file()
    info=queue_provenance(root,[(path,path.stat())])[canonical_local_path(path)]
    assert info['label']=='2026-10-22' and '下载完成时间' in info['tooltip']
    worker=MultiDeployWorker([a,b],root=root,file_paths=[path],interval_seconds=0)
    worker.ledger=ledger
    job=worker.plan_batch()[0]
    if state=='SUCCESS':
        from src.publisher import multi_deploy
        original=multi_deploy.archive_completed_file
        monkeypatch.setattr(multi_deploy,'archive_completed_file',MagicMock(side_effect=OSError('archive locked')))
        worker.archive(job);worker.save_report()
        assert path.is_file() and not worker.report['all_selected_completed']
        assert worker.report['all_selected_published'] and worker.report['archive_pending_count']==1
        monkeypatch.setattr(multi_deploy,'archive_completed_file',original)
        worker.archive(job);worker.save_report()
        assert not path.exists() and Path(job['item']['archived_path']).is_file()
        assert worker.report['all_selected_completed']
        poll(root,api,a,'2026-10-23');poll(root,api,b,'2026-10-23')
        assert len(api.downloads)==1 and not path.exists()
    else:
        worker.archive(job);worker.save_report()
        assert path.is_file() and not job['item']['archived_path']
        assert not worker.report['all_selected_completed']
        assert ledger.result(digest,a.key)['status']=='SUCCESS'


def test_date_column_uses_cloud_day_and_stale_snapshot_does_not_restore_archived_row(panel,tmp_path):
    from src.app.channel_panel import QUEUE_DATE_COLUMN,QUEUE_HEADERS
    a,b,root,api,path,digest=cloud_setup(tmp_path,root=panel.store.root)
    view=panel.douyin
    files=[(path,path.stat())]
    metadata=queue_provenance(root,files)
    view.apply_queue_snapshot(files,metadata)
    panel.resize(1366,768);panel.show();QApplication.processEvents()
    assert view.table_queue.item(0,QUEUE_DATE_COLUMN).text()=='2026-10-22'
    assert view.table_queue.horizontalHeader().visualIndex(QUEUE_DATE_COLUMN)==1
    for column,label in enumerate(QUEUE_HEADERS):
        if column!=1 and not view.table_queue.isColumnHidden(column):
            assert view.table_queue.columnWidth(column)>=view.table_queue.horizontalHeader().fontMetrics().horizontalAdvance(label)+12
    view.table_queue.grab().save(str(tmp_path/'queue-date-layout.png'))
    # An actual archive moves the file; a pre-archive async snapshot must not re-add it.
    path.rename(path.with_suffix('.archived'))
    view.apply_queue_snapshot(files,metadata)
    assert view.table_queue.rowCount()==0


def test_legacy_cloud_dates_are_unknown_and_replaced_local_file_is_not_mislabelled(tmp_path):
    a,b,root,api,path,digest=cloud_setup(tmp_path)
    journal=DriveJournal(root)
    with journal.connect() as db:db.execute('DELETE FROM file_provenance')
    assert queue_provenance(root,[(path,path.stat())])[canonical_local_path(path)]['label']=='日期未知'
    poll(root,api,a,'2026-10-23')
    # Backfilled day is sourced from the actual cloud directory, never the file name.
    assert queue_provenance(root,[(path,path.stat())])[canonical_local_path(path)]['label']=='2026-10-22'
    entry=journal.entry(journal.identity(a,api.files[0],api.account_identity())[0])
    journal.record_local_stamp(entry['identity'])
    path.write_bytes(b'local replacement')
    assert queue_provenance(root,[(path,path.stat())]).get(canonical_local_path(path),{}).get('label') in (None,'日期未知')


def test_already_published_manual_task_still_launches_archive_only_work(panel,tmp_path,monkeypatch):
    path=tmp_path/'published.mp4';path.write_bytes(b'published')
    target=panel.all_targets()[0]
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    MultiTargetLedger(panel.store.root).set_result(digest,target.key,'SUCCESS','confirmed')
    launch=MagicMock();monkeypatch.setattr(panel,'launch_deployment',launch)
    panel.begin_preflight([target],[path])
    launch.assert_called_once_with([target],[path])
    assert panel.preflight_worker is None


def test_standalone_date_and_all_platform_headers_fit_x230(app,tmp_path):
    from src.app.publisher_window import PublisherWindow
    from src.app.channel_panel import QUEUE_HEADERS,QUEUE_DATE_COLUMN
    from src.publisher.platforms import VISIBLE_CHANNELS
    window=PublisherWindow(tmp_path/'standalone')
    controller=window.panel
    try:
        for platform in VISIBLE_CHANNELS:
            view=next(p for p in controller.panels if p.platform==platform)
            if view.current() is None:
                controller.repository.add(platform,'测试账号')
            view.reload();controller.channel_checks[platform].setChecked(True)
        controller.update_selection()
        controller.channel_view.setCurrentIndex(controller.channel_view.findData('wechat_channels'))
        view=next(p for p in controller.panels if p.platform=='wechat_channels')
        path=tmp_path/'为什么灯泡偏在开灯时烧掉_熊猫_书声2.3_20261022.mp4'
        path.write_bytes(b'fixture'*500)
        view.apply_queue_snapshot([(path,path.stat())],{canonical_local_path(path):{'label':'2026-10-22','tooltip':'云盘来源日期'}})
        controller.results[str(path)]={'size_bytes':path.stat().st_size,'mtime_ns':path.stat().st_mtime_ns,
            'results':{p.platform+':'+p.current().account_id:{'status':'RECOVERY_REQUIRED' if p.platform=='wechat_channels' else 'SUCCESS'}
                       for p in controller.panels if p.current()}}
        window.resize(1366,768);window.show();QApplication.processEvents()
        controller._set_initial_sizes();controller.render_results();QApplication.processEvents()
        table=view.table_queue
        assert table.horizontalHeader().length()<=table.viewport().width()+2
        for column,label in enumerate(QUEUE_HEADERS):
            if column!=1 and not table.isColumnHidden(column):
                assert table.columnWidth(column)>=table.horizontalHeader().fontMetrics().horizontalAdvance(label)+12
        assert table.item(0,QUEUE_DATE_COLUMN).text()=='2026-10-22'
        artifacts=Path('artifacts/publisher-nickname-date');artifacts.mkdir(parents=True,exist_ok=True)
        window.grab().save(str(artifacts/'standalone-1366x768.png'))
    finally:
        controller.prepare_shutdown()
        for worker in (controller.snapshot_worker,controller.drive_watch_worker,controller.drive_auth_worker):
            if worker:worker.wait(5000)
        window.close()


@pytest.mark.parametrize('save_fails',[False,True])
def test_name_changes_during_actual_upload_and_save_failure_still_publishes_once(tmp_path,monkeypatch,save_fails):
    from tests import test_wechat_channels as fixture
    monkeypatch.setattr(fixture,'HTML',fixture.HTML.replace('window.selected++;',
        "window.selected++;document.querySelector('#side-bar .name').textContent='上传中改名';"))
    account=fixture.owner();video=tmp_path/'test.mp4';video.write_bytes(b'local-fixture')
    records=[];changes=[]
    with sync_playwright() as runtime:
        fixture.install_browser(monkeypatch,runtime,records,'success')
        uploader=fixture.adapter(account,tmp_path)
        def changed(value):
            changes.append(value['nickname'])
            if save_fails:raise OSError('nickname file locked')
        uploader.nickname_changed=changed
        success,message,info=uploader.upload_single_video(video)
    assert success,message
    assert changes[-1]=='上传中改名' and account.platform_nickname=='上传中改名'
    assert len(records)==1 and records[0]['selected']==1 and uploader.reconcile(info)
