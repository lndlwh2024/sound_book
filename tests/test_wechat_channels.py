"""WeChat Channels contract tests. All browser requests use local fixtures."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

from src.publisher import creator_studio
from src.publisher.identity import extract_platform_identity
from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger, PublishTarget
from src.publisher.platforms import CHANNELS, PUBLISH_ORDER, ChannelAccount, ChannelAccountStore
from src.publisher.wechat_channels import (PUBLISH_PATH, WeChatChannelsUploader,
                                          acknowledgement, publish_endpoint)


URL = CHANNELS['wechat_channels'].url


@pytest.mark.parametrize('body,status,accepted', [
    ({'errCode': 0, 'data': {'objectId': 'post-1'}}, 200, True),
    ({'errCode': '0'}, 200, True),
    ({'errCode': 0, 'data': {'baseResp': {'errcode': 0}}}, 200, True),
    ({'errCode': False}, 200, False),
    ({'code': 0}, 200, False),
    ({'errCode': 0, 'success': False}, 200, False),
    ({'errCode': 1, 'message': '发表失败'}, 200, False),
    ({'errCode': 0}, 500, False),
    ({'errCode': 0, 'data': {'baseResp': {'errcode': 12}}}, 200, False),
    ({'errCode': 0, 'data': {'baseResp': {'errcode': False}}}, 200, False),
    ({'errCode': 0, 'data': {'objectId': 'a', 'feedId': 'b'}}, 200, False),
    ([], 200, False),
])
def test_only_explicit_post_acceptance_counts(body, status, accepted):
    assert bool(acknowledgement(body, status)) is accepted


@pytest.mark.parametrize('suffix', ['/post_preview', '/post_ad_draft', '/post_create/check', '/post_list'])
def test_preview_draft_and_management_endpoints_never_confirm(suffix):
    assert not publish_endpoint('https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post'+suffix)
    assert publish_endpoint('https://channels.weixin.qq.com'+PUBLISH_PATH+'?ignored=secret')
    assert not publish_endpoint('https://channels.weixin.qq.com.attacker.invalid'+PUBLISH_PATH)
    assert not publish_endpoint('http://channels.weixin.qq.com'+PUBLISH_PATH)


def owner():
    return ChannelAccount('a'*32, 'wechat_channels', '本地备注', platform_nickname='本人视频号',
                          title_mode='filename', auto_archive=False,
                          collection_name='一分钟趣味知识', wechat_short_title='松果的天气预报')


HTML = '''<meta charset="utf-8"><style>.input-editor{width:400px;min-height:40px} .filter-wrap[hidden]{display:none}</style>
<aside id="side-bar"><div class="side-bar-footer"><div class="account-info"><img><span class="name">本人视频号</span></div></div></aside>
<main><article><span class="name">其他作者</span></article>
<div class="post-upload-wrap"><input type="file" accept="video/*,.mp4" onchange="upload(this)">
  <section id="media" hidden><video width="120" height="90"></video><progress max="100" value="30"></progress><span id="upload-status">上传中</span></section></div>
<div class="post-desc-box"><div class="input-editor" contenteditable></div></div>
<input placeholder="填写短标题有机会获得更多流量" maxlength="16">
<div class="form-item"><span>位置</span><div class="post-position-wrap">
  <div class="position-display-wrap" style="cursor:pointer" onclick="document.querySelector('.location-filter-wrap').hidden=false">不显示位置</div>
  <div class="location-filter-wrap" hidden><div class="location-item" onclick="document.querySelector('.position-display-wrap').innerText=this.innerText;this.parentElement.hidden=true">不显示位置</div></div>
</div></div>
<div class="form-item"><span>添加到合集</span><div class="post-album-wrap">
  <div class="post-album-display" style="cursor:pointer" onclick="document.querySelector('.filter-wrap').hidden=false">选择合集</div>
  <div class="filter-wrap" hidden><div class="item" onclick="choose(this)"><span class="name">一分钟趣味知识</span></div></div>
</div></div>
<div class="form-item"><span>定时发表</span><label><input type="radio" name="time" value="0" checked>不定时</label><label><input type="radio" name="time" value="1">定时</label></div>
<button onclick="window.wrong='draft'">保存草稿</button><button onclick="window.wrong='preview'">手机预览</button>
<button id="publish" disabled onclick="publish()">发表</button><div role="alert"></div>
</main><script>
window.wrong=null;window.selected=0;
function choose(e){document.querySelector('.post-album-display').innerText=e.innerText;document.querySelector('.filter-wrap').hidden=true;}
function upload(e){window.selected++;document.querySelector('#media').hidden=false;
 let v=document.querySelector('video');Object.defineProperty(v,'readyState',{value:2});
 Object.defineProperty(v,'currentSrc',{value:'blob:current-video'});
 setTimeout(()=>{document.querySelector('progress').remove();document.querySelector('#upload-status').innerText='上传完成';document.querySelector('#publish').disabled=false;},600);
}
async function publish(){await fetch('''+json.dumps(PUBLISH_PATH)+''',{method:'POST'});history.pushState(null,'','/platform/post/list');}
</script>'''


@pytest.fixture
def browser_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(body=HTML, content_type='text/html; charset=utf-8'))
        page.goto(URL)
        yield page
        browser.close()


def test_identity_reads_only_signed_in_channels_sidebar(browser_page):
    identity = extract_platform_identity(browser_page, 'wechat_channels')
    assert identity['nickname'] == '本人视频号'
    browser_page.locator('#side-bar .name').evaluate("e=>e.insertAdjacentHTML('afterend','<span class=\"name\">另一个账号</span>')")
    assert extract_platform_identity(browser_page, 'wechat_channels') is None
    browser_page.goto('https://channels.weixin.qq.com/login.html')
    assert extract_platform_identity(browser_page, 'wechat_channels') is None


def test_success_words_in_description_or_list_are_not_receipts(browser_page, tmp_path):
    instance = WeChatChannelsUploader(owner(), tmp_path/'profile', tmp_path/'reports')
    browser_page.locator('.input-editor').fill('发表成功')
    browser_page.locator('article').evaluate("e=>e.innerText='发表成功'")
    assert instance.success_notice(browser_page) is None
    browser_page.locator('[role=alert]').evaluate("e=>e.innerText='发表成功'")
    assert instance.success_notice(browser_page) is not None


def install_browser(monkeypatch, playwright, records, mode, avatar_only=False, shadow=False):
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    monkeypatch.setattr(creator_studio, 'sync_playwright', lambda: manager)
    from src.publisher import media_browser
    def launch(_playwright, profile, **kwargs):
        context = playwright.chromium.launch_persistent_context(str(profile), headless=True)
        def route(request):
            if request.request.method == 'POST':
                assert request.request.url == 'https://channels.weixin.qq.com'+PUBLISH_PATH
                page = context.pages[0]
                records.append(page.evaluate('''() => {const root=window.fixtureRoot||document;return ({description:root.querySelector('.input-editor').innerText,
                    short:root.querySelector('input[placeholder*=短标题]').value,
                    collection:root.querySelector('.post-album-display').innerText,
                    location:root.querySelector('.position-display-wrap').innerText,
                    wrong:window.wrong,selected:window.selected,early:!!root.querySelector('progress')})}'''))
                request.fulfill(json=({'errCode': 0, 'data': {'objectId': 'local-post'}} if mode == 'success'
                                     else {'errCode': 17, 'message': '发表失败，服务异常'} if mode == 'reject' else {}))
            else:
                body = HTML.replace('本人视频号</span>', '错误视频号</span>') if mode == 'identity' else HTML
                if avatar_only:
                    body=body.replace('<img><span class="name">本人视频号</span>',
                        '<img class="avatar" style="width:32px;height:32px">')
                if shadow:
                    body=body.replace('document.querySelector(', 'window.fixtureRoot.querySelector(')
                    body=body.replace('</main><script>', '</main><script>'+'''const host=document.createElement('wujie-app');
                        document.body.appendChild(host);window.fixtureRoot=host.attachShadow({mode:'open'});
                        for(const node of [...document.body.childNodes]){
                            if(node!==host&&node!==document.currentScript)window.fixtureRoot.appendChild(node);
                        }
                        window.fixtureRoot.appendChild(document.head.querySelector('style').cloneNode(true));''')
                request.fulfill(body=body, content_type='text/html; charset=utf-8')
        context.route('**/*', route)
        return context
    monkeypatch.setattr(media_browser, 'launch_media_context', launch)


def adapter(account, tmp_path):
    instance = WeChatChannelsUploader(account, tmp_path/'profile', tmp_path/'dom_reports', headless=True)
    instance.upload_seconds, instance.publish_seconds, instance.stable_seconds = 4, .5, .05
    instance.control_seconds = 1
    instance._close_publish_context = lambda context, page, submitted: context.close()
    return instance


@pytest.mark.parametrize('mode', ['success', 'unknown', 'reject', 'identity'])
def test_real_shared_workflow_fills_waits_publishes_or_retains_failure(tmp_path, monkeypatch, mode):
    records = []
    account = owner()
    account.description = '补充正文'
    video = tmp_path/'为什么松果晴天张开_20261019_书声2.3.mp4'
    video.write_bytes(b'local-test-video')
    with sync_playwright() as playwright:
        install_browser(monkeypatch, playwright, records, mode)
        instance = adapter(account, tmp_path)
        result, message, info = instance.upload_single_video(video)
    assert result is (mode == 'success')
    if mode == 'identity':
        assert instance.last_outcome == 'IDENTITY_MISMATCH' and not records and not info['submitted']
    else:
        assert len(records) == 1 and records[0] == {
            'description': '', 'short': account.wechat_short_title,
            'collection': account.collection_name, 'location': '不显示位置',
            'wrong': None, 'selected': 1, 'early': False}
        assert info['submitted'] is True
        assert instance.reconcile(info) is (mode == 'success')
        if mode == 'reject':
            assert instance.business_rejected is True
        if mode == 'unknown':
            assert info['confirmation'] is None


def test_configured_short_title_over_limit_is_shortened_before_filling(browser_page, tmp_path):
    account = owner()
    account.wechat_short_title = '过长'*20
    instance = adapter(account, tmp_path)
    instance.fill_details(browser_page, Path('test.mp4'))
    assert len(instance.title)<=16 and '…' not in instance.title
    assert browser_page.locator('input[placeholder*=短标题]').input_value()==instance.title
    assert account.wechat_short_title=='过长'*20
    assert browser_page.evaluate('window.wrong') is None


def test_collection_missing_never_creates_collection_or_publishes(browser_page, tmp_path):
    account = owner()
    account.collection_name = '不存在的合集'
    instance = adapter(account, tmp_path)
    instance.wait = lambda page, label, condition, seconds=60: condition() or (_ for _ in ()).throw(RuntimeError('超时'))
    with pytest.raises(creator_studio.PlatformRejected, match='未找到或回读失败'):
        instance.select_collection(browser_page)
    assert browser_page.evaluate('window.wrong') is None


def test_location_is_forced_off_and_other_defaults_are_untouched(browser_page, tmp_path):
    instance = adapter(owner(), tmp_path)
    browser_page.locator('.position-display-wrap').evaluate("e=>e.innerText='北京市'")
    browser_page.locator('main').evaluate('''e=>e.insertAdjacentHTML('beforeend',
        '<input id="original" type="checkbox"><input id="ai" type="checkbox"><input id="link" value="">')''')
    browser_page.locator('.input-editor').evaluate('''e=>{
        e.addEventListener('input',()=>window.descriptionEdited=true);
    }''')
    instance.fill_details(browser_page, Path('file.mp4'))
    instance.select_collection(browser_page)
    assert browser_page.locator('.position-display-wrap').inner_text() == '不显示位置'
    assert browser_page.locator('.input-editor').inner_text().strip() == ''
    assert not browser_page.evaluate('!!window.descriptionEdited')
    assert not browser_page.locator('#original').is_checked()
    assert not browser_page.locator('#ai').is_checked()
    assert browser_page.locator('#link').input_value() == ''


def test_only_current_after_click_post_response_can_confirm(tmp_path):
    instance = adapter(owner(), tmp_path)
    callbacks = {}
    page = MagicMock()
    page.on.side_effect = lambda name, callback: callbacks.update({name: callback})
    instance.attach_responses(page)
    def request(path=PUBLISH_PATH):
        return MagicMock(method='POST', url='https://channels.weixin.qq.com'+path)
    def response(req):
        return MagicMock(request=req, url=req.url, status=200,
                         json=lambda: {'errCode': 0, 'data': {'objectId': 'post-1'}})
    old = request()
    callbacks['request'](old)
    instance.submitted = True
    callbacks['response'](response(old))
    preview = request(PUBLISH_PATH.replace('post_create', 'post_preview'))
    callbacks['request'](preview)
    callbacks['response'](response(preview))
    assert instance.confirmation is None
    current = request()
    callbacks['request'](current)
    callbacks['response'](response(current))
    assert instance.confirmation['post_id'] == 'post-1'


def test_saved_proof_bound_to_account_and_official_submission(tmp_path):
    instance = adapter(owner(), tmp_path)
    info = {'platform': 'wechat_channels', 'account_id': instance.account.account_id,
            'nickname': instance.account.platform_nickname, 'submitted': True,
            'confirmation': acknowledgement({'errCode': 0}, 200)}
    assert instance.reconcile(info)
    for key, value in [('account_id', 'b'*32), ('nickname', '其他视频号'), ('submitted', False), ('platform', 'kuaishou')]:
        assert not instance.reconcile(dict(info, **{key: value}))
    info['confirmation']['endpoint'] = 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_preview'
    assert not instance.reconcile(info)


@pytest.mark.parametrize('mode', ['success', 'unknown'])
@pytest.mark.parametrize('custom',[False,True])
def test_worker_restart_never_reuploads_success_or_unknown_submission(tmp_path, monkeypatch, mode, custom):
    records = []
    account = owner()
    if custom:
        account.platform_nickname=''
        account.custom_nickname='本地显示名'
    directory = tmp_path/'media'
    directory.mkdir()
    video = directory/'test.mp4'
    video.write_bytes(b'local-video'*256)
    os.utime(video, (time.time()-10, time.time()-10))
    account.target_dir = str(directory)
    target = PublishTarget(account.platform, account, str(tmp_path/'profile'))
    with sync_playwright() as playwright:
        install_browser(monkeypatch, playwright, records, mode, avatar_only=custom)
        def factory(target, log):
            instance = adapter(target.account, tmp_path)
            instance.log = log
            return instance
        for _ in range(2):
            worker = MultiDeployWorker([target], root=tmp_path/'state', uploader_factory=factory,
                                      file_paths=[video], interval_seconds=0)
            worker.run()
        assert len(records) == 1
        ledger = MultiTargetLedger(tmp_path/'state')
        import hashlib
        result = ledger.result(hashlib.sha256(video.read_bytes()).hexdigest(), target.key)
        assert result['status'] == ('SUCCESS' if mode == 'success' else 'RECOVERY_REQUIRED')
    assert video.exists()
    folder = tmp_path/'state/failure_diagnostics/微信视频号'
    if mode == 'success':
        assert not folder.exists()
    else:
        assert list(folder.glob('*/*.png')) and list(folder.glob('*/*.dom.json'))
        assert any('[上传流程结束]' in p.read_text(encoding='utf-8') for p in folder.glob('*/*.log'))


def test_new_account_default_selection_and_roundtrip_do_not_change_existing_platforms(tmp_path):
    store = ChannelAccountStore(tmp_path)
    existing = store.add('kuaishou', '旧账号')
    existing.description = '原有正文'
    store.selected_channels = ['kuaishou']
    new = store.add('wechat_channels', '微信视频号账号')
    new.wechat_short_title, new.collection_name = '短标题', '合集'
    store.save()
    restored = ChannelAccountStore(tmp_path)
    assert restored.selected_channels == ['kuaishou']
    assert restored.for_channel('kuaishou')[0].description == '原有正文'
    assert restored.for_channel('wechat_channels')[0].wechat_short_title == '短标题'
    assert restored.for_channel('wechat_channels')[0].collection_name == '合集'


@pytest.mark.parametrize('standalone', [False, True])
def test_both_editions_offer_only_two_wechat_parameters(tmp_path, standalone):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QScrollArea
    from src.app.channel_panel import MultiPlatformPublisherPanel, QUEUE_HEADERS
    from src.app.publisher_workspace import PublisherWorkspace
    from src.publisher.douyin.config import DouyinAccountManager
    app = QApplication.instance() or QApplication([])
    store = ChannelAccountStore(tmp_path/'multi_platform')
    store.cloud_ingest_enabled = False
    account = store.add('wechat_channels', '测试账号')
    if standalone:
        widget = PublisherWorkspace(tmp_path)
        controller = widget.panel
    else:
        controller = widget = MultiPlatformPublisherPanel(store=store,
            douyin_manager=DouyinAccountManager(tmp_path/'douyin.json', tmp_path/'profiles'))
    try:
        widget.resize(1366, 700)
        widget.show()
        controller.channel_view.setCurrentIndex(controller.channel_view.findData('wechat_channels'))
        panel = next(p for p in controller.panels if p.platform == 'wechat_channels')
        panel.settings_tabs.setCurrentIndex(1)
        app.processEvents()
        assert [title.text() for title, _, _ in panel._field_specs] == ['短标题来源:', '添加到合集:']
        assert not hasattr(panel, 'description') and not hasattr(panel, 'wechat_video_label')
        assert not panel.findChildren(QScrollArea)
        assert not controller.channel_checks['wechat_channels'].isChecked()
        assert QUEUE_HEADERS[5+PUBLISH_ORDER.index('wechat_channels')] == '微信视频号' and panel.table_queue.columnCount() == 10
        panel.title_source.setCurrentIndex(panel.title_source.findData('custom'))
        panel.wechat_short_title.setText('测试短标题')
        panel.collection.setText('测试合集')
        assert panel.save_current()
        restored = ChannelAccountStore(tmp_path/'multi_platform').for_channel('wechat_channels')[0]
        assert restored.wechat_short_title == '测试短标题' and restored.collection_name == '测试合集'
        assert panel.current().account_id == account.account_id
    finally:
        controller.prepare_shutdown()
        for worker in (controller.snapshot_worker, controller.cleanup_worker, controller.drive_watch_worker):
            if worker:
                worker.wait(5000)
        app.processEvents()
        widget.close()
        widget.deleteLater()
        app.processEvents()
