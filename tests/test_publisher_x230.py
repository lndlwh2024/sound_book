"""New-device reuse still checks live identity; no real accounts or uploads."""
import json
import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from playwright.sync_api import sync_playwright
from PySide6.QtWidgets import QApplication,QScrollArea
from src.publisher.dom_readiness import account_dom_state,publishing_dom_state
from src.publisher.page_structure import STRUCTURES
from src.publisher.platforms import CHANNELS,ChannelAccount
from src.publisher.preflight import prerequisite_results,AccountPreflight
from src.publisher.multi_deploy import PublishTarget
from src.publisher.identity import extract_platform_identity,read_homepage_identity
from src.publisher.youtube import PlatformRejected
from src.app.publisher_window import PublisherWindow


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def browser():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_new_account_reuses_structure_without_lying_about_its_capture(tmp_path,platform):
    personal=account_dom_state(tmp_path,platform,'new-account')
    shared=publishing_dom_state(tmp_path,platform,'new-account')
    assert not personal.ready and not shared.reports and not shared.publish_reports
    assert shared.ready and shared.template_revision and '发布前实时校验' in shared.message
    assert not tmp_path.joinpath(platform).exists()  # No fabricated capture is written.
    raw=json.dumps(asdict(STRUCTURES[platform]))
    assert all(x not in raw for x in ('account_id','cookie','nickname','token','profile_dir'))


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_reusable_structure_does_not_replace_new_device_login(browser,tmp_path,platform):
    account=ChannelAccount('a'*32,platform,'test',platform_nickname='本人昵称',channel_id='UC'+'a'*22)
    profile=tmp_path/'profile'
    chosen=PublishTarget(platform,account,str(profile))
    assert '浏览器登录目录' in prerequisite_results([chosen],tmp_path/'reports')[0].message
    profile.mkdir();(profile/'local-marker').write_text('offline test')
    assert not prerequisite_results([chosen],tmp_path/'reports')
    page=browser.new_page()
    page.route('**/*',lambda route:route.fulfill(body='<meta charset="utf-8"><button>立即登录</button>',content_type='text/html'))
    page.goto(CHANNELS[platform].url)
    with pytest.raises(PlatformRejected) as error:
        AccountPreflight(chosen,seconds=.1).verify_page(page)
    assert error.value.status=='AUTH_REQUIRED'
    page.close()


HOME='https://creator.douyin.com/creator-micro/home'
CARD='<section class="userInfo-x"><img style="width:32px;height:32px" src="data:image/png;base64,AA=="><span class="nickname-x">首页本人昵称</span><p>抖音号：123456</p></section>'


def test_homepage_own_card_is_read_without_changing_current_upload(browser):
    context=browser.new_context()
    context.route('**/*',lambda r:r.fulfill(body='<meta charset="utf-8">'+(CARD if r.request.url==HOME else '<input type="file" accept="video/*">'),content_type='text/html'))
    page=context.new_page();page.goto(CHANNELS['douyin'].url)
    assert read_homepage_identity(page,'douyin',seconds=1)['nickname']=='首页本人昵称'
    assert page.url==CHANNELS['douyin'].url and context.pages==[page]
    assert page.locator('input').evaluate('e=>e.files.length')==0
    context.close()


@pytest.mark.parametrize('markup',[
    '<article>'+CARD+'</article>',
    CARD.replace('抖音号：123456','其他作者'),
    CARD.replace('<img style="width:32px;height:32px" src="data:image/png;base64,AA==">',''),
    CARD.replace('</section>','<span class="nickname">另一个昵称</span></section>'),
    CARD+'<section class="userInfo"><img style="width:32px;height:32px"><b class="name">其他昵称</b><p>抖音号：999</p></section>',
])
def test_homepage_authors_partial_cards_and_ambiguity_do_not_bind(browser,markup):
    page=browser.new_page()
    page.route('**/*',lambda r:r.fulfill(body='<meta charset="utf-8">'+markup,content_type='text/html'))
    page.goto(HOME)
    assert extract_platform_identity(page,'douyin') is None
    page.close()


def test_homepage_failure_records_diagnostic_before_its_tab_is_closed(browser):
    context=browser.new_context()
    context.route('**/*',lambda r:r.fulfill(body='<meta charset="utf-8"><p>正在加载</p>',content_type='text/html'))
    page=context.new_page();page.goto(CHANNELS['douyin'].url)
    observed=[]
    assert read_homepage_identity(page,'douyin',seconds=.1,diagnostic=lambda p:observed.append(p.url)) is None
    assert observed==[HOME] and context.pages==[page]
    context.close()


def test_confirmed_placeholder_and_x230_layout_do_not_hide_controls(app,tmp_path):
    window=PublisherWindow(tmp_path)
    window.resize(1366,696)
    try:
        for panel in window.panel.panels:
            account=window.panel.repository.add(panel.platform,'新账号（待登录）')
            account.login_status='USER_CONFIRMED'
            window.panel.repository.save(account);panel.reload()
            assert '待登录' not in panel.accounts.currentText()
            assert '昵称待读取' in panel.accounts.currentText()
            assert panel.current().account_id==account.account_id
        window.show();app.processEvents();window.panel._set_initial_sizes();app.processEvents()
        assert not window.findChildren(QScrollArea)
        for i,panel in enumerate(window.panel.panels):
            window.panel.channel_view.setCurrentIndex(i);app.processEvents()
            split=panel.main.itemAt(0).widget()
            left,right=split.sizes()
            assert 580<=left<=650 and right>left
            assert panel.accounts.width()>380
            for tab in range(2):
                panel.settings_tabs.setCurrentIndex(tab);app.processEvents()
                for options in range(panel.option_pages.count()):
                    panel.option_pages.setCurrentIndex(options);app.processEvents()
                    for control in (panel.save_button,panel.deploy_button,panel.pause_button):
                        assert control.isVisible()
                        assert control.mapTo(panel.left_settings,control.rect().bottomRight()).y()<panel.left_settings.height()
    finally:
        window.closing=True;window.panel.prepare_shutdown()
        for worker in (window.panel.snapshot_worker,window.panel.drive_watch_worker,window.panel.preflight_worker,window.panel.cleanup_worker):
            if worker:worker.wait(3000)
        window.close();window.deleteLater();app.processEvents()
