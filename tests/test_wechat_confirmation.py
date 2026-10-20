"""Current content microfrontend receipts finish the shared serial upload loop."""
import hashlib
import os
import time
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger, PublishTarget
from src.publisher.upload_quota import DailyUploadQuota
from src.publisher.wechat_channels import PUBLISH_PATH, PUBLISH_PATHS, acknowledgement, publish_endpoint
from tests import test_wechat_channels as fixture
from tests.test_wechat_channels import adapter, browser_page, install_browser, owner


CURRENT_PATH = '/micro/content'+PUBLISH_PATH


@pytest.mark.parametrize('path', sorted(PUBLISH_PATHS))
def test_exact_current_and_legacy_post_receipts_keep_actual_endpoint_without_query(path, tmp_path):
    url = 'https://channels.weixin.qq.com'+path
    proof = acknowledgement({'errCode': 0, 'data': {'baseResp': {'errcode': 0},
                            'objectId': 'current-video'}}, 200, endpoint=url+'?token=private')
    assert proof['endpoint'] == url and proof['post_id'] == 'current-video'
    instance = adapter(owner(), tmp_path)
    instance.submitted, instance.confirmation = True, proof
    assert instance.reconcile(instance.result_hint())


@pytest.mark.parametrize('url', [
    'https://channels.weixin.qq.com/micro/content'+PUBLISH_PATH.replace('post_create', 'post_preview'),
    'https://channels.weixin.qq.com/micro/content'+PUBLISH_PATH.replace('post_create', 'post_draft'),
    'https://channels.weixin.qq.com/micro/content'+PUBLISH_PATH.replace('post_create', 'post_list'),
    'https://channels.weixin.qq.com/micro/content'+PUBLISH_PATH+'/check',
    'https://channels.weixin.qq.com/other'+PUBLISH_PATH,
    'https://channels.weixin.qq.com.attacker.invalid'+CURRENT_PATH,
    'http://channels.weixin.qq.com'+CURRENT_PATH,
])
def test_similar_endpoints_and_http_success_alone_never_confirm(url):
    assert not publish_endpoint(url)
    assert acknowledgement({'errCode': 0}, 200, endpoint=url) is None


def test_new_endpoint_requires_our_after_click_request_and_logs_unreadable_receipt(tmp_path):
    instance = adapter(owner(), tmp_path)
    callbacks, logs = {}, []
    page = MagicMock()
    page.on.side_effect = lambda name, callback: callbacks.update({name: callback})
    instance.log = logs.append
    instance.attach_responses(page)
    old = MagicMock(method='POST', url='https://channels.weixin.qq.com'+CURRENT_PATH)
    callbacks['request'](old)
    instance.submitted = True
    response = MagicMock(request=old, url=old.url, status=200,
                         json=lambda: {'errCode': 0})
    callbacks['response'](response)
    assert instance.confirmation is None
    current = MagicMock(method='POST', url=old.url+'?token=private')
    response.request, response.url = current, current.url
    callbacks['request'](current)
    response.json = MagicMock(side_effect=ValueError('not JSON'))
    callbacks['response'](response)
    assert instance.confirmation is None and any('响应内容未能解析' in line for line in logs)
    assert not any('private' in line for line in logs)


@pytest.mark.parametrize('shadow', [False, True])
def test_actual_success_toptip_is_recognized_but_editor_list_and_error_are_not(browser_page, tmp_path, shadow):
    if shadow:
        from tests.test_wechat_upload_deadlines import move_into_shadow
        move_into_shadow(browser_page)
    instance = adapter(owner(), tmp_path)
    browser_page.locator('.input-editor').fill('已发表')
    browser_page.locator('article').evaluate("e=>e.innerText='已发表'")
    assert instance.success_notice(browser_page) is None
    browser_page.locator('main').evaluate('''e=>e.insertAdjacentHTML('beforeend',
        '<div class="common-toptip"><span class="tip-icon error"></span>'
        +'<div class="toptip-content"><span>已发表</span></div></div>')''')
    assert instance.success_notice(browser_page) is None
    browser_page.locator('.tip-icon').evaluate("e=>e.className='tip-icon success'")
    assert instance.success_notice(browser_page).inner_text() == '已发表'


@pytest.mark.parametrize('shadow', [False, True])
@pytest.mark.parametrize('mode', ['success', 'unknown', 'reject', 'notice'])
def test_current_microfrontend_finishes_only_with_explicit_proof(tmp_path, monkeypatch, shadow, mode):
    if mode == 'notice':
        # The real frontend shows this success-only top tip, then navigates away.
        body = fixture.HTML.replace("history.pushState(null,'','/platform/post/list');", '''
            const tip=document.createElement('div');tip.className='common-toptip';
            tip.innerHTML='<span class="tip-icon success"></span><div class="toptip-content">已发表</div>';
            (window.fixtureRoot||document.body).appendChild(tip);
            setTimeout(()=>{tip.remove();history.pushState(null,'','/platform/post/list')},1500);''')
        monkeypatch.setattr(fixture, 'HTML', body)
    video = tmp_path/'为什么水珠沾不湿荷叶.mp4';video.write_bytes(b'fixture')
    records, logs = [], []
    with sync_playwright() as runtime:
        install_browser(monkeypatch, runtime, records, mode, shadow=shadow, publish_path=CURRENT_PATH)
        instance = adapter(owner(), tmp_path);instance.log = logs.append
        success, message, info = instance.upload_single_video(video)
    assert success is (mode in ('success', 'notice')), message
    assert len(records) == 1 and records[0]['description'] == '' and not records[0]['early']
    assert instance.reconcile(info) is success
    if mode == 'success':
        assert info['confirmation']['endpoint'] == 'https://channels.weixin.qq.com'+CURRENT_PATH
    elif mode == 'notice':
        assert info['confirmation'] == {'kind': 'success_notice', 'text': '已发表'}
    else:
        assert info['confirmation'] is None  # Redirect/list/HTTP 200 cannot fabricate success.
    assert not any('平台结果尚未确认' in line for line in logs)


def test_two_files_confirm_close_then_continue_and_restart_skips_both(tmp_path, monkeypatch):
    directory = tmp_path/'media';directory.mkdir()
    videos = []
    for index in range(2):
        video = directory/f'{index}.mp4';video.write_bytes(bytes([index+1])*2048)
        os.utime(video, (time.time()-10, time.time()-10));videos.append(video)
    account = owner();account.target_dir = str(directory)
    target = PublishTarget(account.platform, account, str(tmp_path/'profile'))
    state, records, events = tmp_path/'state', [], []
    quota = DailyUploadQuota(state)
    with sync_playwright() as runtime:
        install_browser(monkeypatch, runtime, records, 'success', shadow=True, publish_path=CURRENT_PATH)
        from src.publisher import media_browser
        launch = media_browser.launch_media_context
        def observe(*args, **kwargs):
            context = launch(*args, **kwargs)
            events.append(('open', quota.snapshot(target.key)['used']))
            context.on('close', lambda _: events.append(('close', quota.snapshot(target.key)['used'])))
            return context
        monkeypatch.setattr(media_browser, 'launch_media_context', observe)
        def factory(target, log):
            instance = adapter(target.account, tmp_path);instance.log = log
            return instance
        for _ in range(2):
            job = MultiDeployWorker([target], root=state, uploader_factory=factory,
                                    file_paths=videos, interval_seconds=0)
            job.run()
    assert len(records) == 2
    assert events == [('open', 0), ('close', 1), ('open', 1), ('close', 2)]
    ledger = MultiTargetLedger(state)
    assert all(ledger.result(hashlib.sha256(video.read_bytes()).hexdigest(), target.key)['status']
               == 'SUCCESS' for video in videos)
    assert quota.snapshot(target.key)['used'] == 2
    assert not (state/'failure_diagnostics/微信视频号').exists()
