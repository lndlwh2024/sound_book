"""Bounded Channels uploads, including its actual Wujie shadow-root layout."""
import os
import time
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

from src.publisher import wechat_channels
from src.publisher.creator_studio import CreatorStudioUploader
from src.publisher.wechat_channels import WeChatChannelsUploader
from src.publisher.youtube import PlatformRejected, UploadStopped
from src.publisher.multi_deploy import MultiDeployWorker, PublishTarget
from tests.test_wechat_channels import browser_page, owner, adapter, install_browser


def raw_state(**overrides):
    return dict(dict(selected=False, busy=False, complete=False, progress=[], videos=[], pending=[]), **overrides)


def clock(monkeypatch, instance):
    current = [0.0]
    monkeypatch.setattr(wechat_channels.time, 'monotonic', lambda: current[0])
    instance._upload_guard = {'started': 0, 'changed': 0, 'signature': None}
    return current


def test_repeated_unknown_snapshots_do_not_extend_90_second_deadline(tmp_path, monkeypatch):
    instance = adapter(owner(), tmp_path)
    current = clock(monkeypatch, instance)
    for instant in (0, 30, 60, 89):
        current[0] = instant
        instance._check_upload_progress(raw_state())
    current[0] = 90
    with pytest.raises(RuntimeError, match='90 秒无法识别'):
        instance._check_upload_progress(raw_state())


def test_only_changed_upload_progress_extends_stall_deadline(tmp_path, monkeypatch):
    instance = adapter(owner(), tmp_path)
    current = clock(monkeypatch, instance)
    state = raw_state(selected=True, busy=True, progress=[{'value': 10, 'max': 100}])
    for instant in (0, 120, 299):
        current[0] = instant
        instance._check_upload_progress(state)
    current[0] = 299
    state['progress'][0]['value'] = 20
    instance._check_upload_progress(state)
    current[0] = 598
    instance._check_upload_progress(state)
    current[0] = 599
    with pytest.raises(RuntimeError, match='300 秒无进展'):
        instance._check_upload_progress(state)


def test_progress_does_not_extend_absolute_upload_deadline(tmp_path, monkeypatch):
    instance = adapter(owner(), tmp_path)
    page = MagicMock()
    current = clock(monkeypatch, instance)
    instance.check_rejection = lambda page: None
    def state(page):
        instance._check_upload_progress(raw_state(selected=True, busy=True,
            progress=[{'value': current[0], 'max': 100}]))
        return dict(ready=False, failed=[], pending=['上传中'], incomplete=True,
                    progress=[{'upload': True}], form=True, preview=False, reupload=False)
    instance._read_upload_state = state
    instance._pause = lambda page, ms: current.__setitem__(0, current[0] + ms / 1000)
    instance.upload_seconds = 1
    with pytest.raises(RuntimeError, match='上传与处理完成超时'):
        instance.wait_upload(page)
    assert 1 <= current[0] < 1.4 and instance._upload_guard is None


def test_form_control_wait_is_capped_without_changing_other_adapters(tmp_path, monkeypatch):
    instance = WeChatChannelsUploader(owner(), tmp_path/'profile', tmp_path/'reports')
    seen = []
    monkeypatch.setattr(instance, '_wait_until', lambda p, label, fn, seconds: seen.append(seconds))
    instance.wait(None, '编辑区', lambda: False, 1800)
    instance.wait(None, '选项', lambda: False, 5)
    assert seen == [30, 5]
    other = CreatorStudioUploader(owner(), tmp_path/'other', tmp_path/'reports')
    monkeypatch.setattr(other, '_wait_until', lambda p, label, fn, seconds: seen.append(seconds))
    other.wait(None, '编辑区', lambda: False, 1800)
    assert seen[-1] == 1800


def move_into_shadow(page):
    page.evaluate('''() => {
        const host=document.createElement('wujie-app');document.body.appendChild(host);
        const outer=host.attachShadow({mode:'open'});
        const child=document.createElement('channels-editor');outer.appendChild(child);
        const root=child.attachShadow({mode:'open'});
        for(const node of [...document.body.childNodes])if(node!==host)root.appendChild(node);
        root.appendChild(document.head.querySelector('style').cloneNode(true));
        window.fixtureRoot=root;
    }''')


@pytest.mark.parametrize('scenario', ['fresh', 'stale', 'busy', 'completed_label', 'failed'])
def test_shadow_state_keeps_current_file_and_completion_evidence(browser_page, tmp_path, scenario):
    instance = adapter(owner(), tmp_path)
    instance.file_name = 'current.mp4'
    instance.file_selected = True
    instance.prior_sources = {'https://channels.weixin.qq.com/old.mp4'}
    instance.completion_reset = scenario != 'stale'
    move_into_shadow(browser_page)
    source = tmp_path/instance.file_name
    source.write_bytes(b'local fixture')
    control = browser_page.locator('input[type=file]')
    control.evaluate('e=>e.onchange=null')
    control.set_input_files(str(source))
    browser_page.locator('#media').evaluate('e=>e.hidden=false')
    browser_page.locator('video').evaluate('''(e,src)=>{
        Object.defineProperty(e,'readyState',{value:2});
        Object.defineProperty(e,'currentSrc',{value:src});
    }''', 'https://channels.weixin.qq.com/'+('old.mp4' if scenario == 'stale' else 'new.mp4'))
    if scenario != 'busy':
        browser_page.locator('progress').evaluate('e=>e.remove()')
        browser_page.locator('#upload-status').evaluate("e=>e.innerText=''")
    if scenario in ('completed_label', 'failed'):
        browser_page.locator('video').evaluate('e=>e.remove()')
        browser_page.locator('#upload-status').evaluate('''(e,text)=>{
            e.className='msg-tip';e.innerText=text;
        }''', '视频已上传' if scenario == 'completed_label' else '网络出错，请重新上传')
    if scenario == 'failed':
        with pytest.raises(PlatformRejected, match='网络出错'):
            instance.read_upload_state(browser_page)
        return
    state = instance._read_upload_state(browser_page)
    assert state['ready'] is (scenario != 'stale')
    assert state['incomplete'] is (scenario == 'busy')
    assert instance.read_upload_state(browser_page)['selected']
    if scenario == 'completed_label':
        assert instance.read_upload_state(browser_page)['complete']


@pytest.mark.parametrize('message', ['标题包含特殊字符，逗号可用空格代替', '标题超过16字限制'])
def test_visible_title_validation_in_shadow_stops_before_publish(browser_page, tmp_path, message):
    instance = adapter(owner(), tmp_path)
    move_into_shadow(browser_page)
    browser_page.locator('main').evaluate('(e,text)=>{const n=document.createElement("span");n.textContent=text;e.appendChild(n)}', message)
    with pytest.raises(PlatformRejected) as error:
        instance.check_rejection(browser_page)
    assert error.value.status == 'CONFIG_REQUIRED'
    assert not instance.submitted and browser_page.evaluate('window.wrong') is None


def test_title_content_and_hidden_validation_never_count_as_rejection(browser_page, tmp_path):
    instance = adapter(owner(), tmp_path)
    move_into_shadow(browser_page)
    browser_page.locator('.input-editor').fill('标题包含特殊字符，是一段正文')
    browser_page.locator('main').evaluate('''e=>e.insertAdjacentHTML('beforeend',
        '<span hidden>标题超过16字限制</span>')''')
    instance.check_rejection(browser_page)


def test_complete_workflow_inside_wujie_publishes_once_after_upload(tmp_path, monkeypatch):
    records = []
    video = tmp_path/'为什么水珠在荷叶上滚来滚去，就是沾不湿它？_20261020_书声2.3.mp4'
    video.write_bytes(b'fixture')
    account = owner()
    account.title_source = 'series'
    with sync_playwright() as runtime:
        install_browser(monkeypatch, runtime, records, 'success', shadow=True)
        instance = adapter(account, tmp_path)
        success, message, info = instance.upload_single_video(video)
    assert success, message
    assert len(records) == 1 and not records[0]['early'] and records[0]['selected'] == 1
    assert records[0]['short'] == '为何水珠在荷叶上滚动 沾不湿它'
    assert info['submitted'] and instance.reconcile(info)


@pytest.mark.parametrize('stage', ['upload', 'confirmation'])
def test_user_stop_exits_event_pump_without_another_attempt(tmp_path, monkeypatch, stage):
    instance = adapter(owner(), tmp_path)
    instance.check_rejection = lambda page: None
    page = MagicMock()
    instance.stop_requested = lambda: page.wait_for_timeout.call_count >= 2
    instance._read_upload_state = lambda page: dict(ready=False, failed=[], pending=['上传中'],
        incomplete=True, progress=[], form=True, preview=False, reupload=False)
    with pytest.raises(UploadStopped):
        if stage == 'upload': instance.wait_upload(page)
        else: instance._wait_for_confirmation(page, lambda: None, 600, poll_ms=300)
    assert [call.args[0] for call in page.wait_for_timeout.call_args_list] == [300, 300]


def test_upload_timeout_stops_at_two_attempts_and_restart_skips_browser(tmp_path):
    account = owner()
    account.auto_archive = False
    folder = tmp_path/'media'
    folder.mkdir()
    files = []
    for index in range(2):
        video = folder/f'{index}.mp4'
        video.write_bytes(bytes([index+1])*2048)
        os.utime(video, (time.time()-10, time.time()-10))
        files.append(video)
    account.target_dir = str(folder)
    target = PublishTarget(account.platform, account, str(tmp_path/'profile'))
    calls = []
    def factory(target, log):
        instance = MagicMock(last_outcome='FAILED')
        def upload(file):
            calls.append(file.name)
            return False, '微信视频号上传状态连续300秒无进展；未点击发表', {'submitted': False}
        instance.upload_single_video = upload
        return instance
    for _ in range(2):
        job = MultiDeployWorker([target], root=tmp_path/'state', uploader_factory=factory,
                                file_paths=files, interval_seconds=0)
        job.run()
    assert sorted(calls) == ['0.mp4', '0.mp4', '1.mp4', '1.mp4'], job.report
    assert all(item['results'][target.key]['status'] == 'FAILED2' for item in job.report['files'])
    assert all(file.exists() for file in files)
