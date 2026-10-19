"""Shared Douyin-derived stage, upload and acknowledgement safety contracts."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.publisher import browser_workflow as module
from src.publisher.creator_studio import XiaohongshuUploader, KuaishouUploader
from src.publisher.douyin.uploader import DouyinUploader, PublishCancelled
from src.publisher.youtube import YouTubeUploader, PlatformRejected, UploadStopped
from src.publisher.wechat_channels import WeChatChannelsUploader


ADAPTERS = [DouyinUploader, XiaohongshuUploader, KuaishouUploader, YouTubeUploader, WeChatChannelsUploader]


def workflow(cls=module.BrowserPublishWorkflow):
    result = object.__new__(cls)
    result._check_stop = MagicMock()
    result._check_duration_notice = MagicMock()
    result._progress = MagicMock()
    result._save_diagnostics = MagicMock()
    result._video_selected = False
    result._publish_armed = False
    result._upload_activity_seen = False
    return result


@pytest.mark.parametrize('cls',ADAPTERS)
@pytest.mark.parametrize('submitted',[True,False])
def test_close_after_publish_holds_page_for_five_seconds(cls,submitted):
    result=workflow(cls)
    page,context=MagicMock(),MagicMock()
    page.is_closed.return_value=False
    events=[]
    page.wait_for_timeout.side_effect=lambda ms:events.append(('wait',ms))
    context.close.side_effect=lambda:events.append(('close',))
    result._close_publish_context(context,page,submitted)
    assert events==([('wait',5000)] if submitted else [])+[('close',)]
    result._check_stop.assert_not_called()
    result._check_duration_notice.assert_not_called()


def test_closed_page_does_not_lose_confirmed_result():
    result=workflow()
    page,context=MagicMock(),MagicMock()
    page.is_closed.return_value=False
    page.wait_for_timeout.side_effect=RuntimeError('page closed')
    result._close_publish_context(context,page,True)
    context.close.assert_called_once()


def fake_clock(monkeypatch, page):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.now)
    page.wait_for_timeout.side_effect = lambda ms: setattr(clock, 'now', clock.now + ms / 1000)
    return clock


def ready_state(**overrides):
    return {'ready':True, 'pending':[], 'incomplete':False, 'progress':[], 'failed':[],
            'form':True, 'preview':True, 'reupload':True, **overrides}


def stages(events, **overrides):
    return {name: (name, lambda name=name: events.append(name) or name)
            for name in ('details', 'cover', 'collection', 'upload', 'submit')} | overrides


def test_all_four_adapters_inherit_one_execution_core():
    for cls in ADAPTERS:
        for method in ('_pause', '_required_stage', '_wait_for_upload_ready',
                       'prepare_publish_form', '_wait_for_confirmation'):
            assert getattr(cls, method) is getattr(module.BrowserPublishWorkflow, method)


def test_douyin_stage_order_and_original_delays():
    result, page, events = workflow(), MagicMock(), []
    result._pause = lambda _, ms: events.append(('pause', ms))
    assert result.prepare_publish_form(page, **stages(events),
                                      details_pause_ms=2000, options_pause_ms=1000) == 'submit'
    assert events == ['details', ('pause', 2000), 'cover', 'collection',
                      ('pause', 1000), 'upload', 'submit']


def test_upload_before_options_capability_uses_same_stages():
    result, page, events = workflow(), MagicMock(), []
    result._pause = MagicMock()
    result.prepare_publish_form(page, **stages(events), uploaded_before_options=True,
                                prepare_submit=('visibility', lambda: events.append('visibility')))
    assert events == ['details', 'upload', 'cover', 'collection', 'visibility', 'submit']


def test_collection_before_cover_runs_before_thumbnail_failure():
    result, page, events = workflow(), MagicMock(), []
    result._pause = MagicMock()
    def fail_cover():
        events.append('cover')
        raise RuntimeError('thumbnail upload failed')
    with pytest.raises(RuntimeError, match='未执行发布'):
        result.prepare_publish_form(page, **stages(events, cover=('cover', fail_cover)),
                                    collection_before_cover=True)
    assert events == ['details', 'collection', 'cover']


@pytest.mark.parametrize('failure_stage', ['details', 'cover', 'collection', 'upload'])
def test_failed_stage_never_reaches_final_submit(failure_stage):
    result, page, events = workflow(), MagicMock(), []
    def fail():
        raise RuntimeError('page refused current file')
    with pytest.raises(RuntimeError, match='未执行发布'):
        result.prepare_publish_form(page, **stages(events, **{failure_stage: (failure_stage, fail)}))
    assert 'submit' not in events
    result._save_diagnostics.assert_called_once_with(page, failure_stage, 'page refused current file')


@pytest.mark.parametrize('cls,interruption', [
    (DouyinUploader, PublishCancelled()), (XiaohongshuUploader, UploadStopped()),
    (KuaishouUploader, PlatformRejected('ACCOUNT_LIMIT', '每日投稿达到上限')),
    (YouTubeUploader, PlatformRejected('PERMANENT_REJECT', '视频过长'))])
def test_stop_and_account_rejections_keep_typed_outcome(cls, interruption):
    result, page, events = workflow(cls), MagicMock(), []
    def fail():
        raise interruption
    with pytest.raises(type(interruption)) as error:
        result.prepare_publish_form(page, **stages(events, collection=('collection', fail)))
    assert error.value is interruption
    assert 'submit' not in events
    result._save_diagnostics.assert_not_called()


@pytest.mark.parametrize('cls', ADAPTERS)
def test_busy_upload_cannot_be_confirmed_by_completion_label(cls, monkeypatch):
    result, page = workflow(cls), MagicMock()
    fake_clock(monkeypatch, page)
    result._read_upload_state = MagicMock(return_value=ready_state(
        pending=['正在上传67%'], incomplete=True, progress=[{'upload': True}]))
    with pytest.raises(RuntimeError, match='未点击发布'):
        result._wait_for_upload_ready(page, timeout_secs=.7)
    assert not result._publish_armed
    result._read_upload_state.return_value = ready_state()
    result._wait_for_upload_ready(page, timeout_secs=.7)


def test_completion_disappearance_requires_current_upload_activity(monkeypatch):
    result, page = workflow(), MagicMock()
    fake_clock(monkeypatch, page)
    result._read_upload_state = MagicMock(return_value=ready_state(ready=False))
    with pytest.raises(RuntimeError, match='超时'):
        result._wait_for_upload_ready(page, timeout_secs=3)
    result._upload_activity_seen = True
    result._wait_for_upload_ready(page, timeout_secs=3)


def test_stop_during_event_pump_checks_every_300ms():
    result, page = workflow(DouyinUploader), MagicMock()
    def stop():
        if page.wait_for_timeout.call_count >= 2:
            raise PublishCancelled()
    result._check_stop = stop
    with pytest.raises(PublishCancelled):
        result._pause(page, 10000)
    assert [call.args[0] for call in page.wait_for_timeout.call_args_list] == [300, 300]


def test_confirmation_keeps_event_pump_and_does_not_reread_old_upload(monkeypatch):
    result, page = workflow(), MagicMock()
    fake_clock(monkeypatch, page)
    result._video_selected = result._publish_armed = True
    result._read_upload_state = MagicMock()
    proof = {'id': 'confirmed-current-post'}
    probe = MagicMock(side_effect=[None, None, proof])
    assert result._wait_for_confirmation(page, probe, timeout_secs=5) is proof
    assert page.wait_for_timeout.call_count == 8  # 2 x (300+300+300+100)
    result._read_upload_state.assert_not_called()


def test_confirmation_timeout_is_unknown_and_rejection_is_terminal(monkeypatch):
    result, page = workflow(), MagicMock()
    fake_clock(monkeypatch, page)
    assert result._wait_for_confirmation(page, lambda: None, timeout_secs=.7) is None
    failure = (False, 'platform rejected the submission')
    assert result._wait_for_confirmation(page, lambda: failure) is failure
