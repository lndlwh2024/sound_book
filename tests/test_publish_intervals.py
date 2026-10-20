"""Local upload timing stays independent from Drive; no browsers or online posts."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication

from src.publisher import multi_deploy as module
from src.publisher.browser_workflow import BrowserPublishWorkflow
from src.publisher.douyin.models import DouyinAccountConfig
from src.publisher.platforms import ChannelAccount


@pytest.fixture(scope='module', autouse=True)
def app():
    return QApplication.instance() or QApplication([])


def add_video(directory, name, content=b'a'):
    path = directory / name
    path.write_bytes(content * 2048)
    os.utime(path, (module.time.time() - 10, module.time.time() - 10))
    return path


def make_worker(tmp_path, monkeypatch, *, settle=0, wake=False, names=('a.mp4', 'b.mp4'), platforms=2):
    directory = tmp_path / 'media'
    directory.mkdir()
    for index, name in enumerate(names):
        add_video(directory, name, bytes([index + 1]))
    binding = dict(enabled=True, folder_id='series', folder_path='书声/测试', poll_seconds=600)
    first = DouyinAccountConfig('test', '抖音', target_dir=str(directory),
        auto_archive=False, drive_binding=binding)
    second = ChannelAccount('b' * 32, 'youtube', 'YouTube', target_dir=str(directory),
        auto_archive=False, channel_id='UC' + 'a' * 22, made_for_kids=False, drive_binding=binding)
    targets = [module.PublishTarget('douyin', first, str(tmp_path / 'dy-profile')),
               module.PublishTarget('youtube', second, str(tmp_path / 'yt-profile'))][:platforms]
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.now)
    starts = []
    after_upload = []

    class Uploader(BrowserPublishWorkflow):
        supports_upload_boundary = False
        last_outcome = 'SUCCESS'

        def __init__(self, target, log):
            self.target, self._progress = target, log

        def upload_single_video(self, path):
            starts.append((self.target.platform, path.name, round(clock.now, 3)))
            if wake:
                job.wake_requested.set()
            for callback in after_upload:
                callback(path)
            page, context = MagicMock(), MagicMock()
            page.is_closed.return_value = False
            page.wait_for_timeout.side_effect = lambda ms: setattr(clock, 'now', clock.now + ms / 1000)
            self._close_publish_context(context, page, submitted=bool(settle))
            return True, '成功', None

    job = module.MultiDeployWorker(targets, root=tmp_path / 'state', uploader_factory=Uploader)
    job.ledger = module.MultiTargetLedger(job.root)
    job.stopping.wait = lambda seconds: setattr(clock, 'now', clock.now + max(seconds, .0001))

    def file_wait(seconds):
        if job.wake_requested.is_set():
            return True
        clock.now += max(seconds, .0001)
        return False

    job.wake_requested.wait = file_wait
    return job, directory, clock, starts, after_upload


def test_default_local_intervals_do_not_change_saved_drive_interval(tmp_path, monkeypatch):
    job, _, _, _, _ = make_worker(tmp_path, monkeypatch)
    assert job.poll_seconds == job.interval_seconds == 5
    assert all(target.account.drive_binding['poll_seconds'] == 600 for target in job.targets)


@pytest.mark.parametrize('settle', [0, 5])
@pytest.mark.parametrize('wake', [False, True])
def test_serial_uploads_keep_separate_five_second_idle_gap_and_no_final_wait(tmp_path, monkeypatch, settle, wake):
    job, directory, clock, starts, _ = make_worker(tmp_path, monkeypatch, settle=settle, wake=wake)
    job.file_paths = {str(path.resolve()) for path in directory.glob('*.mp4')}
    job.run()
    gap = 5 + settle
    assert starts == [('douyin', 'a.mp4', 100.0), ('youtube', 'a.mp4', 100.0 + gap),
                      ('douyin', 'b.mp4', 100.0 + 2 * gap), ('youtube', 'b.mp4', 100.0 + 3 * gap)]
    finished = 100 + 3 * gap + settle
    assert clock.now == pytest.approx(finished, abs=.001)
    assert job.report['status'] == 'COMPLETED' and job.report['success_count'] == 4
    assert job.wake_requested.is_set() == wake  # Upload gaps must not consume file notifications.
    job.run_batch(job.plan_batch())
    assert len(starts) == 4 and clock.now == pytest.approx(finished, abs=.001)


def test_download_arriving_during_upload_is_handled_without_old_300_second_wait(tmp_path, monkeypatch):
    job, directory, clock, starts, callbacks = make_worker(
        tmp_path, monkeypatch, settle=5, names=('a.mp4',), platforms=1)

    def arrive_or_stop(path):
        if path.name == 'a.mp4':
            add_video(directory, 'new.mp4', b'new')
            job.wake_requested.set()
        else:
            job.stop()

    callbacks.append(arrive_or_stop)
    job.run()
    assert starts == [('douyin', 'a.mp4', 100.0), ('douyin', 'new.mp4', 110.0)]
    assert job.report['success_count'] == 2
    assert all(target.account.drive_binding['poll_seconds'] == 600 for target in job.targets)


def test_no_local_video_checks_again_after_five_seconds_and_does_not_repeat_success(tmp_path, monkeypatch):
    job, directory, _, starts, _ = make_worker(tmp_path, monkeypatch, names=(), platforms=1)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            add_video(directory, 'new.mp4')
        else:
            job.stop()

    job.controlled_wait = wait
    job.run()
    assert waits == [5, 5]
    assert len(starts) == 1 and starts[0][:2] == ('douyin', 'new.mp4')


def test_stop_interrupts_five_second_gap_without_starting_next_upload(tmp_path, monkeypatch):
    job, directory, _, starts, _ = make_worker(tmp_path, monkeypatch)
    job.file_paths = {str(path.resolve()) for path in directory.glob('*.mp4')}
    job.stopping.wait = lambda seconds: job.stop()
    job.run()
    assert starts == [('douyin', 'a.mp4', 100.0)]
    assert job.report['status'] == 'STOPPED'


def test_failed_browser_attempt_still_waits_five_seconds_before_its_only_retry(tmp_path, monkeypatch):
    job, directory, clock, starts, _ = make_worker(
        tmp_path, monkeypatch, names=('a.mp4',), platforms=1)

    class Uploader:
        supports_upload_boundary = True

        def upload_single_video(self, path):
            starts.append(round(clock.now, 3))
            raise RuntimeError('文件选择控件不可用，尚未开始传输')

    job.factory = lambda *args: Uploader()
    job.file_paths = {str(path.resolve()) for path in directory.glob('*.mp4')}
    job.run()
    assert starts == [100.0, 105.0]
    assert job.report['files'][0]['results'][job.targets[0].key]['status'] == 'FAILED2'
    job.run_batch(job.plan_batch())
    assert starts == [100.0, 105.0]
