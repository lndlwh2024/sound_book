"""Shared options and real local DOM coverage; never access live accounts."""
import json
import os
import threading
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtWidgets import QApplication,QScrollArea
from PySide6.QtGui import QFontDatabase,QFont
from playwright.sync_api import sync_playwright

from src.publisher.platforms import CHANNELS,VISIBLE_CHANNELS,PUBLISH_ORDER,ChannelAccount,ChannelAccountStore
from src.publisher.titles import resolve_title,title_source
from src.publisher.identity import extract_platform_identity
from src.publisher.channel_probe import ChannelDOMProbe
from src.publisher.preflight import AccountPreflight,PreflightResult,prerequisite_results
from src.publisher.multi_deploy import PublishTarget
from src.publisher.wechat_channels import WeChatChannelsUploader
from src.publisher.youtube import YouTubeUploader,PlatformRejected
from src.publisher.douyin.models import DouyinAccountConfig
from tests.test_common_platform_accounts import panel


@pytest.fixture(scope='module',autouse=True)
def app():
    app=QApplication.instance() or QApplication([])
    font=Path('C:/Windows/Fonts/msyh.ttc')
    if font.exists():QFontDatabase.addApplicationFont(str(font));app.setFont(QFont('Microsoft YaHei',9))
    return app


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        yield browser
        browser.close()


def own_card(name='<span class="nickname">本人视频号</span>'):
    return '<aside style="width:220px"><div class="common-menu-item account-info"><img style="width:30px;height:30px">'+name+'</div></aside>'


def page_with(browser,body):
    page=browser.new_page()
    page.route('**/*',lambda r:r.fulfill(body='<meta charset="utf-8">'+body,content_type='text/html'))
    page.goto(CHANNELS['wechat_channels'].url)
    return page


@pytest.mark.parametrize('name',[
    '<span class="nickname">本人视频号</span>',
    '<span class="nickname" style="display:contents">本人视频号</span>',
    '<style>.account-label::before{content:"本人视频号"}</style><span class="account-label"></span>',
    '<span class="nickname" title="本人视频号">本人视频号</span>',
    '<span class="account-label">本人视频号</span>',
])
def test_actual_sidebar_variants_read_own_name_and_capture_details(browser,tmp_path,name):
    page=page_with(browser,own_card(name)+'<main><span class="nickname">别人的作品</span></main>')
    try:
        assert extract_platform_identity(page,'wechat_channels')['nickname']=='本人视频号'
        owner=ChannelAccount('a'*32,'wechat_channels','remark')
        report,_=ChannelDOMProbe(owner,tmp_path).capture(page,'异常提示')
        data=json.loads(Path(report).read_text(encoding='utf-8'))
        labels=[n.get('ownAccountLabel') for n in data['frames'][0]['nodes'] if n.get('ownAccountLabel')]
        assert any('本人视频号' in json.dumps(n,ensure_ascii=False) for n in labels)
    finally:page.close()


@pytest.mark.parametrize('body',[
    '<main>'+own_card()+'</main>',
    own_card('<span class="nickname" style="display:none">本人视频号</span>'),
    own_card('<span class="nickname">本人视频号</span><span class="nickname">另一个账号</span>'),
    '<article><span class="nickname">本人视频号</span></article>',
    '<div class="share-video-author">视频号 · 本人视频号</div>',
])
def test_authors_hidden_nodes_and_ambiguous_cards_never_bind(browser,body):
    page=page_with(browser,body)
    try:assert extract_platform_identity(page,'wechat_channels') is None
    finally:page.close()


def test_preflight_repairs_missing_wechat_nickname_with_live_identity_without_upload(browser,tmp_path):
    account=ChannelAccount('a'*32,'wechat_channels','新账号',login_status='USER_CONFIRMED')
    profile=tmp_path/'profile';profile.mkdir();(profile/'marker').touch()
    target=PublishTarget('wechat_channels',account,str(profile))
    assert not prerequisite_results([target],tmp_path/'dom')
    page=page_with(browser,own_card()+'<input type="file" accept="video/*">')
    try:
        check=AccountPreflight(target,seconds=.5)
        check.verify_page(page)
        assert account.platform_nickname=='本人视频号' and account.account_id=='a'*32
        assert check.found_identity['nickname']=='本人视频号'
        assert page.locator('input').evaluate('e=>e.files.length')==0
        account.platform_nickname='原来的另一个账号'
        with pytest.raises(PlatformRejected) as error:check.verify_page(page)
        assert error.value.status=='IDENTITY_MISMATCH'
        assert account.platform_nickname=='原来的另一个账号'
    finally:page.close()


def test_recovered_preflight_identity_is_saved_to_same_account_before_deploy(panel,monkeypatch):
    controller=panel
    from src.app import channel_panel as ui
    view=next(p for p in controller.panels if p.platform=='wechat_channels')
    account=view.current();identity=account.account_id;profile=controller.repository.profile(account)
    account.platform_nickname='';account.login_status='USER_CONFIRMED';controller.repository.save(account)
    target=PublishTarget(view.platform,controller.repository.publish_account(account),profile)
    check=MagicMock(targets=deepcopy([target]),stopping=threading.Event())
    check.results=[PreflightResult(target.key,'name','SUCCESS','passed',{'nickname':'本人视频号','platform_uid':''})]
    controller.preflight_worker=check
    controller._all_deployment_targets=[target]
    controller.launch_deployment=MagicMock()
    monkeypatch.setattr(ui.QMessageBox,'warning',MagicMock())
    controller.preflight_finished()
    controller.launch_deployment.assert_called_once()
    saved=ChannelAccountStore(controller.store.root).for_channel('wechat_channels')[0]
    assert saved.account_id==identity and saved.platform_nickname=='本人视频号'
    assert controller.repository.profile(saved)==profile
    assert target.account.platform_nickname=='本人视频号'


@pytest.mark.parametrize('platform',list(CHANNELS))
@pytest.mark.parametrize('source,expected',[
    ('empty',''),('series','趣味知识'),('filename','书声_趣味知识_第1问'),('custom','我的自定义标题')])
def test_all_five_adapters_share_four_title_sources(platform,source,expected):
    account=(DouyinAccountConfig('a','remark') if platform=='douyin' else ChannelAccount('a',platform,'remark'))
    account.title_source=source;account.custom_title='我的自定义标题'
    assert resolve_title(account,'书声_趣味知识_第1问.mp4',platform)==expected


def test_legacy_title_behaviour_is_preserved_and_custom_empty_is_rejected():
    for platform in CHANNELS:
        account=ChannelAccount('a',platform,'remark')
        assert title_source(account)==('empty' if platform=='wechat_channels' else 'series')
    account=ChannelAccount('a','wechat_channels','remark',wechat_short_title='旧短标题')
    assert resolve_title(account,'file.mp4')=='旧短标题'
    account.title_source='custom';account.custom_title=''
    with pytest.raises(ValueError,match='填写自定义'):resolve_title(account,'file.mp4')
    account.title_source='empty'
    assert resolve_title(account,'file.mp4')==''  # Old custom value cannot leak back in.


@pytest.mark.parametrize('source,expected',[('empty',''),('series','趣味知识'),('filename','书声_趣味知识_第1问'),('custom','自定义标题')])
def test_wechat_writes_selected_source_and_clears_stale_short_title(browser,tmp_path,source,expected):
    page=page_with(browser,'<div class="input-editor" contenteditable="true">旧描述</div><input placeholder="填写短标题" value="旧标题">')
    try:
        account=ChannelAccount('a'*32,'wechat_channels','remark',title_source=source,custom_title='自定义标题')
        uploader=WeChatChannelsUploader(account,tmp_path/'profile',tmp_path/'reports')
        uploader.fill_details(page,Path('书声_趣味知识_第1问.mp4'))
        assert page.locator('input').input_value()==expected
        assert page.locator('[contenteditable]').inner_text().strip()==''
    finally:page.close()


def test_hidden_platform_retains_adapter_account_and_selection_history(panel):
    expected=['douyin','kuaishou','wechat_channels','youtube']
    assert list(VISIBLE_CHANNELS)==expected
    assert [panel.channel_view.itemData(i) for i in range(panel.channel_view.count())]==expected
    assert list(panel.channel_checks)==expected
    assert [p.platform for p in panel.panels]==expected
    assert 'xiaohongshu' in CHANNELS and CHANNELS['xiaohongshu'].publish_enabled
    hidden=panel.store.for_channel('xiaohongshu')[0]
    panel.store.selected_channels=['xiaohongshu','douyin'];panel.store.save()
    panel.channel_checks['kuaishou'].setChecked(True)
    restored=ChannelAccountStore(panel.store.root)
    assert 'xiaohongshu' in restored.selected_channels
    assert restored.for_channel('xiaohongshu')[0].account_id==hidden.account_id
    assert all(t.platform!='xiaohongshu' for t in panel.all_targets())
    assert 'xiaohongshu' not in panel.douyin.logs.views
    assert panel.douyin.table_queue.isColumnHidden(5+PUBLISH_ORDER.index('xiaohongshu'))
    for checkbox in panel.channel_checks.values():checkbox.setChecked(True)
    assert [panel.douyin.table_queue.horizontalHeaderItem(i).text()
            for i in range(5,panel.douyin.table_queue.columnCount())
            if not panel.douyin.table_queue.isColumnHidden(i)]==['抖音','快手','微信视频号','YouTube']


def test_ui_saves_four_sources_and_preserves_account_ids(panel):
    for view in panel.panels:
        account_id=view.current().account_id
        panel.channel_view.setCurrentIndex(panel.channel_view.findData(view.platform))
        for source in ('empty','series','filename','custom'):
            view.title_source.setCurrentIndex(view.title_source.findData(source))
            view.custom_title.setText('我的标题')
            assert view.save_current()
            view.reload()
            assert view.current().account_id==account_id
            assert view.current().title_source==source
            assert view.title_source.currentData()==source
            assert view.custom_title.isHidden()==(source!='custom')
        assert [view.title_source.itemText(i) for i in range(4)]==['空','系列名称','文件名','自定义']
    assert not panel.findChildren(QScrollArea)


def test_standalone_custom_title_layout_fits_and_hidden_restore_is_paused(app,tmp_path):
    from src.app.publisher_window import PublisherWindow
    window=PublisherWindow(tmp_path)
    window.resize(1366,696)
    try:
        controller=window.panel
        for view in controller.panels:
            controller.repository.add(view.platform,'测试账号');view.reload()
        window.show();app.processEvents();controller._set_initial_sizes();app.processEvents()
        for index,view in enumerate(controller.panels):
            controller.channel_view.setCurrentIndex(index)
            view.settings_tabs.setCurrentIndex(1)
            view.option_pages.setCurrentIndex(0)
            view.title_source.setCurrentIndex(view.title_source.findData('custom'))
            view.custom_title.setText('自定义标题');app.processEvents()
            for control in (view.custom_title,view.title_source,view.save_button,view.deploy_button):
                assert control.isVisible()
                assert control.mapTo(view.left_settings,control.rect().bottomRight()).y()<view.left_settings.height()
            assert view.custom_title.width()>120 and view.title_source.width()>90
            if view.platform=='youtube':
                for control in (view.channel_id,view.playlist,view.description,*view.youtube_fields.values()):
                    assert control.isVisible()
                    assert view.option_pages.currentWidget().rect().contains(
                        control.mapTo(view.option_pages.currentWidget(),control.rect().topLeft()))
                    assert view.option_pages.currentWidget().rect().contains(
                        control.mapTo(view.option_pages.currentWidget(),control.rect().bottomRight()))
                view.option_pages.setCurrentIndex(1);app.processEvents()
                assert not view.title_source.isVisible()
                assert view.daily_limit.isVisible() and view.auto_archive.isVisible()
                view.option_pages.setCurrentIndex(0);app.processEvents()
            if view.platform in ('youtube','wechat_channels'):
                folder=Path('artifacts/publish-intervals-20261020');folder.mkdir(parents=True,exist_ok=True)
                assert window.grab().save(str(folder/(view.platform+'-1366.png')))
        assert not window.findChildren(QScrollArea)
        controller.state.targets=['xiaohongshu:'+'a'*32]
        controller.state.intent='RUNNING';controller.state.auto_deploy=True
        controller.launch_deployment=MagicMock()
        controller._restore_deployment()
        controller.launch_deployment.assert_not_called()
        assert '隐藏' in controller.douyin.deploy_status.text()
    finally:
        window.closing=True;window.panel.prepare_shutdown()
        for worker in (window.panel.snapshot_worker,window.panel.drive_watch_worker):
            if worker:worker.wait(3000)
        window.close();window.deleteLater();app.processEvents()


@pytest.mark.parametrize('platform',['youtube','xiaohongshu','kuaishou'])
@pytest.mark.parametrize('source',['empty','series','filename','custom'])
def test_platform_editors_receive_the_four_title_sources(browser,tmp_path,monkeypatch,platform,source):
    from src.publisher.creator_studio import XiaohongshuUploader,KuaishouUploader
    account=ChannelAccount('a'*32,platform,'remark',title_source=source,custom_title='自定义标题',made_for_kids=False)
    file=Path('书声_趣味知识_第1问.mp4');expected=resolve_title(account,file.name)
    page=browser.new_page()
    if platform=='youtube':
        html='''<ytcp-uploads-dialog><div role="dialog">
            <div contenteditable="true" role="textbox" aria-label="标题" style="min-height:25px">旧标题</div>
            <div contenteditable="true" role="textbox" aria-label="介绍" style="min-height:25px"></div>
            <button role="radio">不，内容不是面向儿童的</button></div></ytcp-uploads-dialog>'''
        uploader=YouTubeUploader(account,tmp_path/'profile',tmp_path/'reports')
        monkeypatch.setattr(uploader,'apply_default_details',lambda *args:None)
        selector='[aria-label="标题"]'
    elif platform=='xiaohongshu':
        html='<input placeholder="填写标题会有更多赞哦" value="旧标题"><div contenteditable="true" role="textbox" class="tiptap" style="min-height:25px"></div>'
        uploader=XiaohongshuUploader(account,tmp_path/'profile',tmp_path/'reports')
        selector='input'
    else:
        html='<div id="work-description-edit" contenteditable="true" style="min-height:25px">旧标题</div>'
        uploader=KuaishouUploader(account,tmp_path/'profile',tmp_path/'reports')
        selector='#work-description-edit'
    try:
        page.route('**/*',lambda route:route.fulfill(body='<meta charset="utf-8">'+html,content_type='text/html'))
        page.goto(CHANNELS[platform].url)
        uploader.fill_details(page,file)
        assert page.locator(selector).evaluate("e=>e.matches('input')?e.value:e.innerText").strip()==expected
        assert uploader.title==expected
    finally:page.close()
