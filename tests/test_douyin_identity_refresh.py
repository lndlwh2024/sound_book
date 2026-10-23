"""Reproduce the current avatar-only Douyin header without account credentials."""
import os
import json
from dataclasses import asdict
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from playwright.sync_api import sync_playwright
from PySide6.QtWidgets import QApplication
from src.publisher.identity import extract_platform_identity, refresh_platform_identity, identity_read_failure
from src.publisher.platforms import ChannelAccount
from src.publisher.preflight import AccountPreflight
from src.publisher.multi_deploy import PublishTarget
from src.publisher.youtube import PlatformRejected
from src.publisher.douyin.browser import DouyinBrowserManager
from src.app import channel_panel

URL = 'https://creator.douyin.com/creator-micro/content/upload'


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1440,'height':900})
            page.route('**/*', lambda r: r.fulfill(body='<meta charset="utf-8">', content_type='text/html'))
            page.goto(URL)
            yield page
        finally:
            browser.close()


def header(nickname='本人真实昵称', portal=False):
    return '''<meta charset="utf-8"><style>
        #header-avatar{position:absolute;top:12px;right:24px;width:32px;height:32px;background:#ccc}
        #own-menu{position:absolute;top:48px;right:20px;width:200px;min-height:90px;background:#eee}
        </style><div id="header-avatar" class="dropdown-wrapper-sdUW26 avatar-wrapper-DIbwVi"'''+(
        ' aria-controls="own-menu"' if portal else '')+'''>头像</div>
        <input type="file" accept="video/*">
        <article><header><span class="user-name nickname">作品作者，不能作为本人昵称</span></header></article>
        <button onclick="window.clickedUpload=true">上传视频</button>
        <script>localStorage.setItem('userInfo',JSON.stringify({nickname:'缓存中的错误昵称'}));
        document.querySelector('#header-avatar').addEventListener('mouseenter',()=>setTimeout(()=>{
            if(document.querySelector('#own-menu'))return;
            const menu=document.createElement('div');menu.id='own-menu';menu.setAttribute('role','menu');
            menu.innerHTML='<span class="user-name-AbCd">'''+nickname+'''</span><button onclick="window.logout=true">退出登录</button>';
            '''+('document.body' if portal else 'document.querySelector("#header-avatar")')+'''.appendChild(menu);
        },200));</script>'''


@pytest.mark.parametrize('portal',[False,True])
def test_explicit_refresh_reveals_only_own_avatar_menu(page, portal):
    page.set_content(header(portal=portal))
    assert extract_platform_identity(page,'douyin') is None
    identity = refresh_platform_identity(page,'douyin',seconds=1)
    assert identity['nickname'] == '本人真实昵称'
    assert identity['source'] == '#header-avatar own-account-menu'
    assert not page.evaluate('Boolean(window.logout||window.clickedUpload)')
    assert page.locator('input[type=file]').evaluate('e=>e.files.length') == 0
    # Re-reading is idempotent, no logout, login or upload operation is invoked.
    assert refresh_platform_identity(page,'douyin',seconds=.1) == identity


def test_explicit_refresh_accepts_spatially_attached_own_account_portal_without_aria(page):
    page.set_content(header(portal=True))
    page.locator('#header-avatar').evaluate('e=>e.removeAttribute("aria-controls")')
    assert refresh_platform_identity(page,'douyin',seconds=1)['nickname'] == '本人真实昵称'


@pytest.mark.parametrize('markup',[
    '<article><header class="creator-header"><span class="name">作品作者</span></header></article>',
    '<span class="user-info"><span class="nickname">其他作者</span></span>',
    '<div id="header-avatar" style="width:32px;height:32px"><img alt="头像图片" title="账号菜单"></div>',
    '<header><span class="user-name">甲</span><span class="user-name">乙</span></header>',
    '<div id="header-avatar"><span class="nickname">甲</span><span class="nickname">乙</span></div>',
    '<div role="menu"><span class="user-name">推荐作者</span><button>关注</button></div>',
])
def test_other_authors_ambiguous_names_and_avatar_labels_never_overwrite_account(page,markup):
    page.set_content(markup)
    page.evaluate("localStorage.setItem('accountInfo',JSON.stringify({name:'错误缓存',nickname:'其他缓存'}))")
    assert refresh_platform_identity(page,'douyin',seconds=.1) is None


def test_nickname_missing_is_not_reported_as_expired_login(page):
    page.set_content('<div id="header-avatar">头像</div><input type="file" accept="video/*">')
    message = identity_read_failure(page,'douyin')
    assert '无需重新扫码' in message and '原昵称与绑定保留' in message
    page.set_content('<h1>扫码登录</h1>')
    assert '明确要求登录' in identity_read_failure(page,'douyin')


def test_password_or_login_route_never_yields_identity(page):
    page.set_content('<header><span class="user-name">真实昵称</span></header><input type="password">')
    assert refresh_platform_identity(page,'douyin',seconds=.1) is None
    page.goto('https://creator.douyin.com/login')
    page.set_content('<header><span class="user-name">过期昵称</span></header>')
    assert refresh_platform_identity(page,'douyin',seconds=.1) is None


@pytest.mark.parametrize('wanted',['本人真实昵称','另一个账号'])
def test_deployment_preflight_syncs_hidden_nickname_without_rejecting_rename(page,tmp_path,wanted):
    page.set_content(header())
    account=ChannelAccount('stable-id','douyin','备注',platform_nickname=wanted)
    checker=AccountPreflight(PublishTarget('douyin',account,str(tmp_path)),seconds=1)
    checker.verify_page(page)
    assert account.platform_nickname=='本人真实昵称'
    assert page.locator('input[type=file]').evaluate('e=>e.files.length') == 0


def test_legacy_reader_uses_same_scoped_avatar_identity(page):
    page.set_content(header())
    assert DouyinBrowserManager.extract_nickname(page) == '本人真实昵称'


def test_account_popup_diagnostics_include_plain_name_and_aria_relationship(page,tmp_path):
    from pathlib import Path
    from src.publisher.channel_probe import ChannelDOMProbe
    page.set_content(header(portal=True))
    assert refresh_platform_identity(page,'douyin',seconds=1)
    probe=ChannelDOMProbe(ChannelAccount('stable-id','douyin','name'),tmp_path)
    path,_=probe.capture(page,'异常提示')
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    nodes=data['frames'][0]['nodes']
    assert any(n['id']=='header-avatar' and n['ariaControls']=='own-menu' for n in nodes)
    assert any(n['text']=='本人真实昵称' and n['className']=='user-name-AbCd' for n in nodes)


@pytest.mark.parametrize('error',[None,RuntimeError('头像悬停被遮挡')])
def test_failed_explicit_read_keeps_account_and_login_and_saves_dom(page,tmp_path,monkeypatch,error):
    app=QApplication.instance() or QApplication([])
    page.set_content('<div id="header-avatar">头像</div><input type="file" accept="video/*">')
    account=ChannelAccount('stable-id','douyin','原昵称',platform_nickname='原昵称',login_status='AUTHORIZED')
    before=asdict(account)
    monkeypatch.setattr(channel_panel,'refresh_platform_identity',MagicMock(return_value=None,side_effect=error))
    worker=channel_panel.ChannelProbeWorker(account,tmp_path/'profile',tmp_path/'reports')
    captures=[]
    worker.captured.connect(lambda *paths:captures.append(paths))
    with pytest.raises(ValueError, match='无需重新扫码'):
        worker.read_identity(page)
    assert captures and all(os.path.isfile(path) for path in captures[0])
    assert asdict(account)==before
    assert worker.account.login_status=='AUTHORIZED'
