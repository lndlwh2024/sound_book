"""Failure evidence and post-publication regressions; all browser pages are local fixtures."""
import json
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from playwright.sync_api import sync_playwright
from src.publisher.failure_diagnostics import TaskFailureDiagnostics
from src.publisher.event_log import PublishEventLog, make_event
from src.publisher.multi_deploy import MultiDeployWorker
from src.publisher.youtube import YouTubeUploader, PlatformRejected
from tests.test_youtube_publisher import account, CHANNEL_ID, VIDEO_ID
from tests.test_multi_deploy import setup
from tests.test_scan_cleanup_ui import panel


def test_completion_dialog_on_video_edit_page_verifies_channel_on_separate_page(tmp_path):
    uploader = YouTubeUploader(account(), tmp_path/'p', tmp_path/'r')
    uploader.video_id, uploader.submitted, uploader._publish_armed = VIDEO_ID, True, True
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            visited = []
            def route(r):
                visited.append(r.request.url)
                r.fulfill(body='<meta charset="utf-8">空列表')
            context.route('**/*', route)
            page = context.new_page()
            page.goto(f'https://studio.youtube.com/video/{VIDEO_ID}/edit')
            page.set_content(f'<div role="dialog"><h2>视频发布时间</h2>目标视频'
                f'<a href="https://youtube.com/shorts/{VIDEO_ID}">视频链接</a></div>')
            assert uploader.verify_persisted_result(context, page, '目标视频', 1)
            assert visited[-1] == f'https://studio.youtube.com/channel/{CHANNEL_ID}/videos'
            assert len(context.pages) == 1  # auxiliary verification page is closed
            assert page.get_by_text('视频发布时间').is_visible()
        finally:
            browser.close()


@pytest.mark.parametrize('case', ['new_upload', 'other_video', 'wrong_channel'])
def test_reset_upload_page_or_wrong_identity_is_never_success(tmp_path, case):
    uploader = YouTubeUploader(account(), tmp_path/'p', tmp_path/'r')
    uploader.video_id, uploader.submitted, uploader._publish_armed = VIDEO_ID, True, True
    uploader._pause = lambda *args: None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.route('**/*', lambda r: r.fulfill(body='<meta charset="utf-8">空列表'))
            page = context.new_page()
            page.goto('https://studio.youtube.com/video/'+(VIDEO_ID if case != 'other_video' else 'zzzzzzzzzzz')+'/edit')
            if case == 'new_upload':
                page.set_content('<h1>上传视频</h1><input type="file">')
            else:
                page.set_content(f'<div role="dialog">视频发布时间 目标视频 <a href="https://youtu.be/{VIDEO_ID}">链接</a></div>')
            if case == 'wrong_channel':
                page.goto('https://studio.youtube.com/channel/UC'+'c'*22)
                page.set_content(f'<div role="dialog">视频发布时间 目标视频 <a href="https://youtu.be/{VIDEO_ID}">链接</a></div>')
                with pytest.raises(PlatformRejected, match='不一致'):
                    uploader.verify_persisted_result(context, page, '目标视频', .3)
            else:
                assert not uploader.verify_persisted_result(context, page, '目标视频', .3)
        finally:
            browser.close()


def test_failure_bundle_has_last_dom_screenshot_identity_and_entire_task_log(tmp_path):
    normal = PublishEventLog(tmp_path, maximum=700, backups=9)
    for i in range(6):
        normal.write('early-'+str(i), task_id='task-1')
    normal.write('unrelated task', task_id='task-2')
    task = TaskFailureDiagnostics(tmp_path, 'task-1', normal.task_events('task-1'))
    operation = task.operation('douyin', 'account-1', 'test.mp4', 'attempt-1')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<button>旧页面</button>')
            operation.capture(page, '上传阶段', 'first state')
            page.set_content('<button>最后失败页面</button><input type="password" value="SECRET">')
            operation.capture(page, '发布核对', 'access_token=SECRET')
            browser.close()
            folder = operation.complete('RECOVERY_REQUIRED', '结果未确认')
        finally:
            browser.close()
    task.record_event(make_event('task finished', task_id='task-1'))
    task.finish()
    assert folder.parent.name == '抖音'
    assert len(list(folder.glob('*.png'))) == 1
    dom = next(folder.glob('*.dom.json'))
    assert 'task-1_' in dom.name and '_attempt-1_last' in dom.name
    data = json.loads(dom.read_text(encoding='utf-8'))
    assert data['task_id'] == 'task-1' and data['timestamp'].endswith('+08:00')
    content = dom.read_text(encoding='utf-8')
    assert '最后失败页面' in content and '旧页面' not in content and 'SECRET' not in content
    assert data['screenshot'] == next(folder.glob('*.png')).name
    logs = next(folder.glob('*.log')).read_text(encoding='utf-8')
    assert all('early-'+str(i) in logs for i in range(6))
    assert 'task finished' in logs and 'unrelated task' not in logs
    assert not task.staging.exists()


def test_success_creates_no_failure_platform_date_folder(tmp_path):
    task = TaskFailureDiagnostics(tmp_path, 'success')
    task.record_event(make_event('normal success', task_id='success'))
    operation = task.operation('youtube', 'account', 'test.mp4')
    operation.complete('SUCCESS', 'confirmed')
    task.finish()
    assert not (tmp_path/'failure_diagnostics').exists()


def test_diagnostic_staging_cannot_escape_task_directory(tmp_path):
    task = TaskFailureDiagnostics(tmp_path, '..')
    task.record_event(make_event('test', task_id='..'))
    marker = tmp_path/'preserve.txt'
    marker.write_text('preserve', encoding='utf-8')
    task.finish()
    assert marker.read_text(encoding='utf-8') == 'preserve'


def test_adapter_creation_failure_is_still_recorded(tmp_path):
    directory, targets = setup(tmp_path)
    targets[1].platform = targets[1].account.platform = 'xiaohongshu'
    worker = MultiDeployWorker([targets[1]], root=tmp_path/'state',
        uploader_factory=MagicMock(side_effect=RuntimeError('无法启动浏览器')),
        file_paths=[directory/'a.mp4'], interval_seconds=0)
    worker.run()
    assert worker.report['failure_count'] == 1
    assert list((tmp_path/'state/failure_diagnostics/小红书').glob('*/*.result.json'))


@pytest.mark.parametrize('confirmed', [True, False])
def test_explicit_manual_recovery_rechecks_submitted_video_without_uploading_again(tmp_path, panel, monkeypatch, confirmed):
    import hashlib
    from src.publisher.multi_deploy import MultiTargetLedger, PublishTarget
    from PySide6.QtWidgets import QMessageBox
    target = next(t for t in panel.all_targets() if t.platform == 'youtube')
    video = Path(target.account.target_dir)/'already-public.mp4'
    video.write_bytes(b'published-video'*200)
    import time
    os.utime(video, (time.time()-10,)*2)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    info = {'video_id': VIDEO_ID, 'channel_id': CHANNEL_ID, 'submitted': True, 'draft': False,
            'title': '目标视频', 'visibility': 'public'}
    ledger = MultiTargetLedger(panel.store.root)
    ledger.set_result(digest, target.key, 'RECOVERY_REQUIRED', '频道核验超时', info, requires_review=True, attempt_count=1)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.Yes)
    assert panel.confirm_retry_budget([target], [video])
    assert MultiTargetLedger(panel.store.root).result(digest, target.key)['info']['submitted']
    adapter = MagicMock()
    adapter.reconcile.return_value = confirmed
    worker = MultiDeployWorker([target], root=panel.store.root, uploader_factory=lambda *a: adapter,
        file_paths=[video], interval_seconds=0)
    worker.run()
    adapter.reconcile.assert_called_once_with(info)
    adapter.upload_single_video.assert_not_called()
    assert MultiTargetLedger(panel.store.root).result(digest, target.key)['status'] == ('SUCCESS' if confirmed else 'RECOVERY_REQUIRED')
    assert (panel.store.root/'failure_diagnostics').exists() != confirmed


@pytest.mark.parametrize('platform', ['douyin', 'xiaohongshu', 'kuaishou', 'youtube'])
def test_each_real_adapter_captures_failure_before_browser_closes(tmp_path, monkeypatch, platform):
    from src.publisher import youtube, creator_studio
    from src.publisher.douyin import uploader as douyin
    from src.publisher.douyin.models import DouyinAccountConfig
    task = TaskFailureDiagnostics(tmp_path, 'failure-task')
    operation = task.operation(platform, 'account', 'video.mp4')
    video = tmp_path/'video.mp4'
    video.write_bytes(b'x')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.route('**/*', lambda r: r.fulfill(body='<meta charset="utf-8"><button>本次失败现场</button>', content_type='text/html'))
            # Keep the actual browser and pages, only replace process creation.
            fake = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a, **k: context))
            if platform == 'douyin':
                monkeypatch.setattr(douyin, 'sync_playwright', lambda: nullcontext(fake))
                monkeypatch.setattr(douyin, 'probe_video_duration', lambda path: 5)
                monkeypatch.setattr(douyin.DouyinBrowserManager, 'create_persistent_context', lambda *a, **k: context)
                adapter = douyin.DouyinUploader(DouyinAccountConfig('account', 'name', target_dir=str(tmp_path)))
                def navigate(page):
                    page.goto('https://creator.douyin.com/upload')
                    raise RuntimeError('模拟页面失败')
                monkeypatch.setattr(adapter, '_navigate_upload_page', navigate)
            else:
                module = youtube if platform == 'youtube' else creator_studio
                monkeypatch.setattr(module, 'sync_playwright', lambda: nullcontext(fake))
                config = account()
                config.platform = platform
                cls = {'youtube': youtube.YouTubeUploader, 'kuaishou': creator_studio.KuaishouUploader,
                       'xiaohongshu': creator_studio.XiaohongshuUploader}[platform]
                adapter = cls(config, tmp_path/'profile', tmp_path/'r')
                if platform != 'youtube':
                    monkeypatch.setattr(creator_studio, 'account_dom_state', lambda *a: SimpleNamespace(publish_reports=True))
                monkeypatch.setattr(adapter, 'verify_channel' if platform == 'youtube' else 'verify_identity',
                                    MagicMock(side_effect=RuntimeError('模拟页面失败')))
            adapter.failure_capture = operation.capture
            success, message, _ = adapter.upload_single_video(video)
            assert not success
            folder = operation.complete(adapter.last_outcome, message)
            dom = json.loads(next(folder.glob('*.dom.json')).read_text(encoding='utf-8'))
            assert '本次失败现场' in json.dumps(dom, ensure_ascii=False)
            assert next(folder.glob('*.png')).stat().st_size > 100
        finally:
            browser.close()
    task.finish()


def test_worker_failed_attempt_keeps_all_logs_but_successful_platform_has_no_bundle(tmp_path):
    directory, targets = setup(tmp_path)
    class Adapter:
        def __init__(self, target, log):
            self.target, self.log = target, log
        def upload_single_video(self, path):
            self.log('完整中间流程')
            if self.target.platform == 'youtube':
                self.last_outcome = 'AUTH_REQUIRED'
                return False, '登录失效', None
            return True, 'confirmed', None
    worker = MultiDeployWorker(targets, root=tmp_path/'state', uploader_factory=Adapter,
        file_paths=[directory/'a.mp4'], interval_seconds=0)
    worker.run()
    folder = tmp_path/'state/failure_diagnostics'
    assert not (folder/'抖音').exists()
    logs = list((folder/'YouTube').glob('*/*.log'))
    assert logs and '完整中间流程' in logs[0].read_text(encoding='utf-8')
    assert '[任务结束]' in logs[0].read_text(encoding='utf-8')
    dom = json.loads(next((folder/'YouTube').glob('*/*.dom.json')).read_text(encoding='utf-8'))
    assert 'capture_error' in dom and '无可用截图' in dom['capture_error']


def test_queue_hides_unselected_platform_columns_without_reindexing_or_changing_archive(panel):
    for key, check in panel.channel_checks.items():
        check.setChecked(key in ('douyin', 'youtube'))
    panel.update_selection()
    for p in panel.panels:
        assert p.table_queue.columnCount() == 9
        assert [p.table_queue.isColumnHidden(c) for c in range(5, 9)] == [False, True, True, False]
    archive_targets = list(panel.store.archive_targets)
    panel._queue_platforms = {'youtube'}  # the launch dialog may choose fewer than the checked channels
    panel.render_results()
    for p in panel.panels:
        assert [p.table_queue.isColumnHidden(c) for c in range(5, 9)] == [True, True, True, False]
    assert panel.store.archive_targets == archive_targets
