"""Title field mapping and stop propagation through the actual upload pipeline."""
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from src.publisher.douyin import uploader as module
from src.publisher.douyin.models import DouyinAccountConfig
from src.publisher.douyin.daemon import DouyinAutoDeployDaemon


def make_uploader(tmp_path, stop=None):
    account = DouyinAccountConfig("test", "test", profile_dir=str(tmp_path / "profile"))
    return module.DouyinUploader(account, stop_requested=stop)


def test_title_mapping_matches_collected_dom():
    report = Path(__file__).resolve().parents[1] / "data/douyin_dom_report_20261006_004004.json"
    if not report.exists():
        pytest.skip("local diagnostic DOM not included in checkout")
    data = json.loads(report.read_text(encoding="utf-8"))
    assert any(item["tag"] == "input" and item["placeholder"] == "填写作品标题，为作品获得更多流量"
               for item in data["inputs"])


def test_real_dom_title_only(tmp_path):
    # Local HTML only: no Douyin URL, account profile, or publish operation.
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<input placeholder="填写作品标题，为作品获得更多流量" maxlength="30">'
                             '<div contenteditable="true" id="body"></div>')
            value = module.resolve_publish_series("书声_一分钟趣味知识_20261004_第14问.mp4")
            uploader._fill_title(uploader._wait_for_title(page, 1), value)
            assert page.locator(module.TITLE_SELECTOR).input_value() == "一分钟趣味知识"
            assert page.locator("#body").inner_text() == ""
            uploader._fill_title(page.locator(module.TITLE_SELECTOR), "测" * 40)
            assert page.locator(module.TITLE_SELECTOR).input_value() == "测" * 30
        finally:
            browser.close()


def test_cancel_during_title_wait(tmp_path):
    stop = threading.Event()
    uploader = make_uploader(tmp_path, stop.is_set)
    page = MagicMock()

    def timeout(**kwargs):
        stop.set()
        raise PlaywrightTimeoutError("waiting")

    page.locator.return_value.count.return_value = 1
    page.get_by_text.return_value.count.return_value = 0
    page.locator.return_value.wait_for.side_effect = timeout
    with pytest.raises(module.PublishCancelled):
        uploader._wait_for_title(page)
    page.locator.return_value.first.fill.assert_not_called()


@pytest.mark.parametrize("stop_phase", ["before_submit", "after_submit"])
def test_stop_pipeline_never_archives_unconfirmed_file(tmp_path, monkeypatch, stop_phase):
    stop = threading.Event()
    uploader = make_uploader(tmp_path, stop.is_set)
    video = tmp_path / "书声_一分钟趣味知识_20261004_第14问.mp4"
    video.write_bytes(b"test")
    page, context = MagicMock(), MagicMock()
    page.url = "https://creator.douyin.com/creator-micro/content/post/video"
    context.pages = [page]
    title = MagicMock()
    title.get_attribute.return_value = "30"
    title.input_value.return_value = "一分钟趣味知识"
    publish = MagicMock()
    absent = MagicMock()
    absent.count.return_value = 0

    def locate(selector):
        result = MagicMock()
        if selector == module.TITLE_SELECTOR + ':visible':
            return title
        if selector == module.TITLE_SELECTOR:
            result.first = title
        elif selector.startswith('button:has-text("发布")'):
            result.first = publish
        else:
            result.first = MagicMock()
        return result

    page.locator.side_effect = locate
    title.count.return_value = 1
    page.get_by_role.return_value = publish
    publish.count.return_value = 1
    page.get_by_text.return_value.first = absent
    page.get_by_text.return_value.count.return_value = 0
    monkeypatch.setattr(module, "sync_playwright", MagicMock())
    monkeypatch.setattr(module.DouyinBrowserManager, "create_persistent_context", lambda *a, **k: context)
    uploader._finalize_publish_archive_and_ledger = MagicMock()
    uploader._select_video_cover = MagicMock()
    uploader._wait_for_upload_ready = MagicMock()

    def collection(*args):
        if stop_phase == "before_submit":
            stop.set()

    uploader._handle_collection_assignment = collection
    if stop_phase == "after_submit":
        publish.click.side_effect = stop.set
    success, msg, record = uploader.upload_single_video(video)
    assert success is False and record is None
    assert msg.startswith("已停止")
    assert video.exists()
    uploader._finalize_publish_archive_and_ledger.assert_not_called()
    page.keyboard.type.assert_not_called()
    if stop_phase == "before_submit":
        publish.click.assert_not_called()
        assert "未点击发布" in msg
    else:
        publish.click.assert_called_once()
        assert "结果尚未确认" in msg


def test_daemon_stop_propagates_to_active_uploader(tmp_path, monkeypatch):
    from src.publisher.douyin import daemon as daemon_module
    account = DouyinAccountConfig("test", "test", target_dir=str(tmp_path))
    manager = MagicMock()
    manager.get_accounts.return_value = [account]
    daemon = DouyinAutoDeployDaemon(manager, report_dir=tmp_path / "reports")
    (tmp_path / "test.mp4").write_bytes(b"test video")
    daemon._scan_account_pending_files = MagicMock(return_value=[tmp_path / "test.mp4"])
    daemon._find_matching_cover = MagicMock(return_value=None)
    calls = []

    def factory(**kwargs):
        def upload(**unused):
            assert not kwargs["stop_requested"]()
            daemon.stop()
            assert kwargs["stop_requested"]()
            calls.append(True)
            return False, "已停止：当前任务已取消", None
        result = MagicMock()
        result.upload_single_video.side_effect = upload
        return result

    monkeypatch.setattr(daemon_module, "DouyinUploader", factory)
    daemon.run()
    assert calls == [True]


def test_real_dom_old_publish_selector_hits_sidebar(tmp_path):
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<button id="sidebar">作品发布</button>'
                             '<button id="submit" type="submit">发布</button>'
                             '<button>暂存离开</button>')
            old = page.locator('button:has-text("发布"), button[type="primary"]:has-text("发布")').first
            assert old.get_attribute("id") == "sidebar"
            assert uploader._get_publish_button(page).get_attribute("id") == "submit"
            page.locator('#submit').evaluate('(el) => el.disabled = true')
            with pytest.raises(RuntimeError, match="不可用"):
                uploader._get_publish_button(page)
            page.locator('#submit').evaluate('(el) => el.disabled = false')
            page.locator('body').evaluate('(el) => el.insertAdjacentHTML("beforeend", "<button>发布</button>")')
            with pytest.raises(RuntimeError, match="唯一"):
                uploader._get_publish_button(page)
        finally:
            browser.close()


def test_real_dom_cover_and_collection_controls(tmp_path):
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''
                <div class="semi-select select-mix-type-test">合集</div>
                <div class="semi-select select-collection-test" id="collection"
                     onclick="document.querySelector('#options').style.display='block'">请选择合集</div>
                <div class="semi-select-dropdown" id="options" style="display:none">
                    <span onclick="document.querySelector('#collection').textContent=this.textContent;
                         document.querySelector('#options').style.display='none'">一分钟趣味知识</span>
                </div>
                <div onclick="document.querySelector('#cover').style.display='block'">选择封面</div>
                <div role="dialog" id="cover" style="display:none"><video></video>
                    <button onclick="this.parentElement.style.display='none'">完成</button></div>
            ''')
            uploader._select_video_cover(page)
            assert not page.locator('#cover').is_visible()
            uploader._handle_collection_assignment(page, '一分钟趣味知识', 14)
            assert page.locator('#collection').inner_text() == '一分钟趣味知识'
            assert page.locator('.select-mix-type-test').inner_text() == '合集'
            with pytest.raises(RuntimeError, match="未找到唯一合集"):
                uploader._handle_collection_assignment(page, '不存在的合集', 14)
            # Real browser: confirmation takes longer than the former 2-second limit.
            page.set_content('''<div onclick="document.querySelector('#cover').style.display='block'">选择封面</div>
                <div role="dialog" id="cover" style="display:none"><video></video>
                <button onclick="setTimeout(()=>this.parentElement.style.display='none',2500)">完成</button></div>''')
            uploader._select_video_cover(page)
            assert not page.locator('#cover').is_visible()
        finally:
            browser.close()


def test_required_stage_failure_aborts_instead_of_silent_skip(tmp_path):
    uploader = make_uploader(tmp_path)
    uploader._save_diagnostics = MagicMock()
    with pytest.raises(RuntimeError, match="未执行发布"):
        uploader._required_stage(MagicMock(), "选择封面", MagicMock(side_effect=RuntimeError("missing control")))
    uploader._save_diagnostics.assert_called_once()


@pytest.mark.parametrize('attribute', ['disabled', 'readonly'])
def test_real_dom_title_waits_for_editable_after_two_seconds(tmp_path, attribute):
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(2000)
            page.set_content(f'''<input style="display:none" placeholder="填写作品标题，为作品获得更多流量">
                <input id="title" {attribute} placeholder="填写作品标题，为作品获得更多流量">
                <div contenteditable="true" id="body"></div>
                <script>setTimeout(()=>document.querySelector('#title').removeAttribute('{attribute}'),2500)</script>''')
            uploader._fill_title(uploader._wait_for_title(page, 5), '一分钟趣味知识')
            assert page.locator('#title').input_value() == '一分钟趣味知识'
            assert page.locator('#body').inner_text() == ''
        finally:
            browser.close()


def test_real_dom_title_fill_recovers_when_readiness_changes(tmp_path):
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(2000)
            page.set_content('<input id="title" placeholder="填写作品标题，为作品获得更多流量">')
            title = uploader._wait_for_title(page, 1)
            page.evaluate('''() => {document.querySelector('#title').readOnly=true;
                setTimeout(()=>document.querySelector('#title').outerHTML=
                    '<input id="title" placeholder="填写作品标题，为作品获得更多流量">',2500);}''')
            uploader._fill_title(title, '一分钟趣味知识', 5)
            assert page.locator('#title').input_value() == '一分钟趣味知识'
        finally:
            browser.close()


def test_real_dom_title_failure_saves_state_before_browser_closes(tmp_path):
    uploader = make_uploader(tmp_path)
    uploader.account.target_dir = str(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''<fieldset disabled><input readonly
                placeholder="填写作品标题，为作品获得更多流量"></fieldset>''')
            with pytest.raises(RuntimeError, match='未执行发布'):
                uploader._required_stage(page, '等待并填写作品标题', lambda: uploader._wait_for_title(page, .4))
            data = json.loads(next((tmp_path / 'reports').glob('*.json')).read_text(encoding='utf-8'))
            assert data['diagnostic']['stage'] == '等待并填写作品标题'
            assert data['inputs'][0]['disabledByFieldset'] is True
            assert data['inputs'][0]['readOnly'] is True
            assert list((tmp_path / 'reports').glob('*.png'))
        finally:
            browser.close()


def test_cancel_during_title_fill_does_not_retry_or_publish(tmp_path):
    stop = threading.Event()
    uploader = make_uploader(tmp_path, stop.is_set)
    title = MagicMock()
    title.get_attribute.return_value = '30'
    def blocked_fill(*args, **kwargs):
        assert kwargs['timeout'] <= 300
        stop.set()
        raise PlaywrightTimeoutError('not editable')
    title.fill.side_effect = blocked_fill
    with pytest.raises(module.PublishCancelled):
        uploader._fill_title(title, '一分钟趣味知识')
    title.fill.assert_called_once()


def test_publish_response_logs_business_code_without_auth_query(tmp_path):
    uploader = make_uploader(tmp_path)
    page = MagicMock()
    uploader._attach_publish_diagnostics(page)
    callbacks = {call.args[0]: call.args[1] for call in page.on.call_args_list}
    response = MagicMock()
    response.url = 'https://creator.douyin.com/web/api/aweme/create/?token=secret'
    response.status = 200
    response.request.method = 'POST'
    response.json.return_value = {'status_code': 9, 'status_msg': '请稍后再试', 'token': 'secret'}
    callbacks['response'](response)
    assert uploader._network_events[0]['result']['status_code'] == '9'
    assert 'secret' not in json.dumps(uploader._network_events)


def test_daily_limit_detected_even_when_diagnostic_buffer_is_full(tmp_path):
    uploader = make_uploader(tmp_path)
    uploader._network_events = [{} for _ in range(40)]
    page = MagicMock()
    uploader._attach_publish_diagnostics(page)
    callbacks = {call.args[0]: call.args[1] for call in page.on.call_args_list}
    response = MagicMock()
    response.url = 'https://creator.douyin.com/web/api/media/aweme/create_v2/'
    response.status = 200
    response.request.method = 'POST'
    response.json.return_value = {'status_code': 2158, 'status_msg': '抱歉，今天投稿次数已达到上限，请明天再试'}
    callbacks['response'](response)
    assert '今天投稿次数' in uploader.account_limit_reason
    assert len(uploader._network_events) == 40
    assert uploader._network_events[-1]['result']['status_code'] == '2158'


def test_quota_rejection_finishes_without_waiting_sixty_seconds_or_archiving(tmp_path, monkeypatch):
    uploader = make_uploader(tmp_path)
    video = tmp_path / '一分钟趣味知识_45.mp4'
    video.write_bytes(b'x')
    page, context, publish = MagicMock(), MagicMock(), MagicMock()
    page.url = 'https://creator.douyin.com/creator-micro/content/post/video'
    context.pages = [page]
    monkeypatch.setattr(module, 'sync_playwright', MagicMock())
    monkeypatch.setattr(module.DouyinBrowserManager, 'create_persistent_context', lambda *a, **k: context)
    uploader._pause = MagicMock()
    uploader._prepare_title = MagicMock()
    uploader._wait_for_upload_ready = MagicMock()
    uploader._select_video_cover = MagicMock()
    uploader._handle_collection_assignment = MagicMock()
    uploader._get_publish_button = MagicMock(return_value=publish)
    uploader._save_diagnostics = MagicMock()
    uploader._finalize_publish_archive_and_ledger = MagicMock()
    publish.click.side_effect = lambda: setattr(uploader, 'account_limit_reason', '今日投稿已达上限')
    success, message, record = uploader.upload_single_video(video)
    assert not success and record is None and uploader.last_outcome == 'ACCOUNT_LIMIT'
    assert '今日投稿' in message
    assert len(uploader._pause.call_args_list) == 4  # No publish confirmation polling pause.
    uploader._finalize_publish_archive_and_ledger.assert_not_called()
    uploader._save_diagnostics.assert_called_once()


@pytest.mark.parametrize('seconds, rejected', [(3600.0, False), (3600.01, True), (4200, True)])
def test_duration_preflight_uses_actual_seconds_and_skips_browser_for_oversize(tmp_path, monkeypatch, seconds, rejected):
    uploader = make_uploader(tmp_path)
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')
    monkeypatch.setattr(module, 'probe_video_duration', lambda path: seconds)
    launch = MagicMock(side_effect=RuntimeError('launch marker'))
    monkeypatch.setattr(module, 'sync_playwright', launch)
    success, msg, record = uploader.upload_single_video(video)
    assert not success and record is None
    assert (uploader.last_outcome == 'DURATION_LIMIT') == rejected
    assert launch.call_count == (0 if rejected else 1)
    assert video.exists()


def test_platform_duration_notice_aborts_title_wait_immediately(tmp_path):
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<div role="alert">单个视频不得超过60分钟</div>')
            with pytest.raises(module.VideoDurationRejected, match='60分钟'):
                uploader._wait_for_title(page, 1)
        finally:
            browser.close()


def test_horizontal_cover_prompt_chooses_foreground_button_and_completes_video_cover(tmp_path):
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''
                <div onclick="document.querySelector('#editor').style.display='block'">选择封面</div>
                <div role="dialog" id="editor" style="display:none">
                    <canvas></canvas><span id="preview">竖封面预览</span>
                    <button id="background" onclick="window.wrong=true">设置横封面</button>
                    <button onclick="if(window.horizontal){this.parentElement.style.display='none';window.finished=true;}
                        else {document.querySelector('#guide').style.display='block';}">完成</button>
                    <div role="dialog" id="guide" style="display:none">
                        <h2>设置横封面获更多流量</h2><button onclick="window.skip=true">暂不设置</button>
                        <button onclick="window.horizontal=true;this.parentElement.style.display='none';
                            setTimeout(()=>document.querySelector('#preview').textContent='横封面预览（16:9）',400)">设置横封面</button>
                    </div>
                </div>''')
            uploader._select_video_cover(page)
            assert page.evaluate('window.horizontal && window.finished && !window.wrong && !window.skip')
            assert not page.locator('#editor').is_visible()
        finally:
            browser.close()


def test_horizontal_cover_repeated_prompt_does_not_click_twice(tmp_path):
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''<div><h2>设置横封面获更多流量</h2><button>暂不设置</button>
                <button onclick="window.clicks=(window.clicks||0)+1">设置横封面</button></div>''')
            assert uploader._handle_horizontal_cover_prompt(page)
            with pytest.raises(RuntimeError, match='重复出现'):
                uploader._handle_horizontal_cover_prompt(page, allow_click=False)
            assert page.evaluate('window.clicks') == 1
        finally:
            browser.close()


def test_upload_wait_does_not_accept_early_form_or_success_label_while_progress_incomplete(tmp_path):
    uploader = make_uploader(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''<input placeholder="填写作品标题，为作品获得更多流量"><span>上传成功</span>
                <div id="busy">正在上传</div><div role="progressbar" class="upload-progress" style="width:200px;height:20px" aria-valuenow="40" aria-valuemax="100"></div>''')
            with pytest.raises(RuntimeError, match='未点击发布'):
                uploader._wait_for_upload_ready(page, .4)
            page.evaluate('''() => {document.querySelector('#busy').textContent='';
                setTimeout(()=>document.querySelector('[role=progressbar]').setAttribute('aria-valuenow','100'),700);}''')
            uploader._wait_for_upload_ready(page, 3)
        finally:
            browser.close()


def test_only_current_create_api_acknowledgement_confirms_publish(tmp_path):
    uploader = make_uploader(tmp_path)
    page = MagicMock()
    uploader._attach_publish_diagnostics(page)
    callback = next(call.args[1] for call in page.on.call_args_list if call.args[0] == 'response')
    response = MagicMock()
    response.request.method = 'POST'
    response.status = 200
    response.json.return_value = {'status_code': 0}
    response.url = 'https://creator.douyin.com/web/api/media/aweme/create_v2/'
    callback(response)
    assert uploader._publish_result is None
    uploader._publish_armed = True
    response.url = 'https://creator.douyin.com/aweme/v1/open/publish/limit_app_groups/'
    callback(response)
    assert uploader._publish_result is None
    response.url = 'https://creator.douyin.com/web/api/media/aweme/create_v2/'
    callback(response)
    assert uploader._publish_result['code'] == '0'
    response.status = 500
    callback(response)
    assert uploader._publish_result['code'] != '0'


def test_upload_progress_disappearance_completes_without_dismissing_preview_tip(tmp_path):
    uploader = make_uploader(tmp_path)
    uploader._video_selected = True
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''<input placeholder="填写作品标题，为作品获得更多流量">
                <canvas width="150" height="200"></canvas><div>重新上传</div>
                <div id="progress" class="upload-progress" style="width:200px;height:20px" role="progressbar" aria-valuenow="67"></div>
                <aside id="tip"><h2>视频预览功能</h2><button onclick="window.dismissed=true">我知道了</button></aside>''')
            # Observe during pre-publish preparation, then the SDK removes progress before the gate.
            uploader._pause(page, 300)
            assert uploader._upload_activity_seen
            page.evaluate("document.querySelector('#progress').remove()")
            uploader._wait_for_upload_ready(page, 4)
            assert page.locator('#tip').is_visible()
            assert not page.evaluate('!!window.dismissed')
            assert uploader._publish_result is None
        finally:
            browser.close()


@pytest.mark.parametrize('scenario', ['no_history', 'no_preview', 'upload_failed', 'pending'])
def test_upload_progress_absence_alone_never_allows_publish(tmp_path, scenario):
    uploader = make_uploader(tmp_path)
    uploader._upload_activity_seen = scenario != 'no_history'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''<input placeholder="填写作品标题，为作品获得更多流量">
                <canvas></canvas><div>重新上传</div>''')
            if scenario == 'no_preview':
                page.locator('canvas').evaluate('(el)=>el.remove()')
            if scenario == 'upload_failed':
                page.evaluate("document.body.insertAdjacentHTML('beforeend','<div>视频上传失败，请重新上传</div>')")
            if scenario == 'pending':
                page.evaluate("document.body.insertAdjacentHTML('beforeend','<div>正在转码</div>')")
            with pytest.raises(RuntimeError):
                uploader._wait_for_upload_ready(page, 2.4)
        finally:
            browser.close()


@pytest.mark.parametrize('route', ['manage', 'upload'])
def test_manage_page_does_not_close_browser_or_archive_before_delayed_create_response(tmp_path, monkeypatch, route):
    uploader = make_uploader(tmp_path)
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'x')
    monkeypatch.setattr(module, 'probe_video_duration', lambda path: 100)
    page, context, publish = MagicMock(), MagicMock(), MagicMock()
    context.pages = [page]
    page.url = 'https://creator.douyin.com/creator-micro/content/' + route
    page.get_by_text.return_value.count.return_value = 0
    page.get_by_text.return_value.first.count.return_value = 0
    monkeypatch.setattr(module, 'sync_playwright', MagicMock())
    monkeypatch.setattr(module.DouyinBrowserManager, 'create_persistent_context', lambda *a, **k: context)
    for name in ['_prepare_title', '_select_video_cover', '_handle_collection_assignment', '_wait_for_upload_ready']:
        setattr(uploader, name, MagicMock())
    uploader._get_publish_button = MagicMock(return_value=publish)
    uploader._finalize_publish_archive_and_ledger = MagicMock()
    waits = []
    def pause(unused, ms):
        if uploader._publish_armed and ms == 1000:
            context.close.assert_not_called()
            uploader._finalize_publish_archive_and_ledger.assert_not_called()
            waits.append(ms)
            if len(waits) == 2:
                uploader._publish_result = {'code': '0', 'message': ''}
    uploader._pause = pause
    success, message, record = uploader.upload_single_video(video)
    assert success and waits == [1000, 1000]
    context.close.assert_called_once()
    uploader._finalize_publish_archive_and_ledger.assert_called_once()
