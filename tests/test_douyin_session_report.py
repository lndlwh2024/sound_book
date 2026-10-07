import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from src.publisher.douyin.session_report import PublishSessionReport
from src.publisher.douyin.models import DouyinAccountConfig
from src.publisher.douyin.daemon import DouyinAutoDeployDaemon, is_file_fully_written


def test_one_report_for_many_results_and_stop(tmp_path):
    report = PublishSessionReport(tmp_path / "reports")
    account = DouyinAccountConfig("test", "测试账号")
    for index in range(23):
        file = tmp_path / f"video_{index}.mp4"
        file.write_bytes(b"x" * 2048)
        item = report.begin_file(account, file)
        report.complete_file(item, "SUCCESS" if index < 21 else "FAILED", "ok" if index < 21 else "封面超时")
    report.finish("FAILED")
    assert len(list((tmp_path / "reports").glob("*"))) == 1
    data = json.loads(report.path.read_text(encoding="utf-8"))
    assert data["success_count"] == 21 and data["failure_count"] == 2
    assert data["success_files"][0]["file_name"] == "video_0.mp4"
    assert data["failure_files"][0]["message"] == "封面超时"
    assert data["files"][0]["size_bytes"] == 2048
    assert data["finished_at"] and data["status"] == "FAILED"


def test_cancel_and_uncertain_are_not_failed_or_success(tmp_path):
    report = PublishSessionReport(tmp_path / "reports")
    file = tmp_path / "video.mp4"
    file.write_bytes(b"x")
    account = DouyinAccountConfig("test", "test")
    for status in ["CANCELLED", "UNCONFIRMED"]:
        other = tmp_path / (status + '.mp4')
        other.write_bytes(b'x')
        report.complete_file(report.begin_file(account, other), status, status)
    report.finish("STOPPED")
    assert report.data["success_count"] == report.data["failure_count"] == 0
    assert report.data["cancelled_count"] == report.data["unconfirmed_count"] == 1


def test_report_finalized_on_unexpected_daemon_exception(tmp_path, monkeypatch):
    daemon = DouyinAutoDeployDaemon(MagicMock(), report_dir=tmp_path / "reports")
    monkeypatch.setattr(daemon, "_run_loop", MagicMock(side_effect=RuntimeError("boom")))
    daemon.run()
    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text(encoding="utf-8"))
    assert report["status"] == "FAILED" and report["finished_at"]


def test_active_writer_not_uploaded_on_windows(tmp_path):
    file = tmp_path / "rendering.mp4"
    with open(file, "wb") as writer:
        writer.write(b"x" * 2048)
        writer.flush()
        if os.name == "nt":
            assert is_file_fully_written(file, min_age_secs=0) is False
    os.utime(file, (1, 1))
    assert is_file_fully_written(file, min_age_secs=0) is True


def test_daemon_aggregates_success_then_failure_into_one_report(tmp_path, monkeypatch):
    from src.publisher.douyin import daemon as daemon_module
    account = DouyinAccountConfig("test", "测试账号", target_dir=str(tmp_path))
    files = [tmp_path / f"video_{index}.mp4" for index in range(3)]
    for file in files:
        file.write_bytes(b"x" * 2048)
        os.utime(file, (1, 1))
    manager = MagicMock()
    manager.get_accounts.return_value = [account]
    daemon = DouyinAutoDeployDaemon(manager, report_dir=tmp_path / "reports")
    monkeypatch.setattr(daemon._stop_event, "wait", lambda timeout: None)
    monkeypatch.setattr(daemon, '_sleep_countdown', lambda timeout: daemon.stop())
    attempted = []

    def factory(**kwargs):
        instance = MagicMock()
        def upload(video_path, **unused):
            attempted.append(video_path)
            if len(attempted) < 3:
                video_path.unlink()
                instance.last_outcome = "SUCCESS"
                return True, "发布成功", None
            instance.last_outcome = "FAILED"
            return False, "封面检测失败", None
        instance.upload_single_video.side_effect = upload
        return instance

    monkeypatch.setattr(daemon_module, "DouyinUploader", factory)
    daemon.run()
    paths = list((tmp_path / "reports").glob("*.json"))
    assert len(paths) == 1
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    assert data["success_count"] == 2 and data["failure_count"] == 1
    assert data["failure_files"][0]["file_name"] == "video_2.mp4"
    assert data["status"] == "FAILED"


def test_single_file_finalize_no_longer_creates_individual_report(tmp_path):
    from src.publisher.douyin.uploader import DouyinUploader
    account = DouyinAccountConfig("test", "test", target_dir=str(tmp_path), profile_dir=str(tmp_path / "profile"))
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    record = DouyinUploader(account)._finalize_publish_archive_and_ledger(video, "合集", 1)
    assert Path(record.archived_path).exists()
    assert not list((tmp_path / "reports").glob("publish_report_*.txt"))
    assert len(list((tmp_path / "reports").glob("publish_ledger_*.json"))) == 1


def test_two_passes_across_accounts_no_third_attempt_even_without_archive(tmp_path, monkeypatch):
    from src.publisher.douyin import daemon as module
    accounts = []
    for index in range(2):
        folder = tmp_path / str(index)
        folder.mkdir()
        accounts.append(DouyinAccountConfig(str(index), str(index), target_dir=str(folder)))
    for account, names in [(accounts[0], ['a', 'b', 'c']), (accounts[1], ['d'])]:
        for index, name in enumerate(names):
            file = Path(account.target_dir) / (name + '.mp4')
            file.write_bytes(b'x' * 2048)
            os.utime(file, (index + 1, index + 1))
    manager = MagicMock()
    manager.get_accounts.return_value = accounts
    daemon = DouyinAutoDeployDaemon(manager, report_dir=tmp_path / 'reports')
    monkeypatch.setattr(daemon._stop_event, 'wait', lambda timeout: None)
    cycles = []
    def idle(timeout):
        cycles.append(True)
        if len(cycles) == 2:
            daemon.stop()
    monkeypatch.setattr(daemon, '_sleep_countdown', idle)
    calls, counts = [], {}
    def factory(**kwargs):
        instance = MagicMock()
        def upload(video_path, **unused):
            name = video_path.stem
            calls.append(name)
            counts[name] = counts.get(name, 0) + 1
            kwargs['progress_callback']('mock stage')
            outcome = 'FAILED' if name == 'a' else ('UNCONFIRMED' if name == 'c' and counts[name] == 1 else 'SUCCESS')
            instance.last_outcome = outcome
            return outcome == 'SUCCESS', outcome, None
        instance.upload_single_video.side_effect = upload
        return instance
    monkeypatch.setattr(module, 'DouyinUploader', factory)
    daemon.run()
    assert calls == ['a', 'b', 'c', 'd', 'a', 'c']
    data = json.loads(next((tmp_path / 'reports').glob('*.json')).read_text(encoding='utf-8'))
    assert data['success_count'] == 3 and data['failure_count'] == 1
    assert data['unconfirmed_count'] == 0 and data['attempt_count'] == 6
    assert len(data['files']) == 4
    recovered = next(x for x in data['files'] if x['file_name'] == 'c.mp4')
    assert [x['status'] for x in recovered['attempts']] == ['UNCONFIRMED', 'SUCCESS']
    assert recovered['attempts'][0]['logs'][-1]['message'] == 'mock stage'


def test_stop_before_retry_preserves_first_attempt_only(tmp_path, monkeypatch):
    from src.publisher.douyin import daemon as module
    account = DouyinAccountConfig('a', 'a', target_dir=str(tmp_path))
    file = tmp_path / 'a.mp4'
    file.write_bytes(b'x' * 2048)
    os.utime(file, (1, 1))
    manager = MagicMock()
    manager.get_accounts.return_value = [account]
    daemon = DouyinAutoDeployDaemon(manager, report_dir=tmp_path / 'reports')
    monkeypatch.setattr(daemon._stop_event, 'wait', lambda timeout: daemon.stop())
    instance = MagicMock(last_outcome='FAILED')
    instance.upload_single_video.return_value = False, 'fail', None
    monkeypatch.setattr(module, 'DouyinUploader', lambda **kwargs: instance)
    daemon.run()
    instance.upload_single_video.assert_called_once()
    data = json.loads(next((tmp_path / 'reports').glob('*.json')).read_text(encoding='utf-8'))
    assert data['attempt_count'] == 1 and data['failure_count'] == 1


def test_account_limit_skips_remaining_files_and_queued_retry_but_other_account_continues(tmp_path, monkeypatch):
    from src.publisher.douyin import daemon as module
    accounts = []
    for index, names in enumerate([['a', 'b', 'c'], ['d']]):
        folder = tmp_path / str(index)
        folder.mkdir()
        accounts.append(DouyinAccountConfig(str(index), str(index), target_dir=str(folder)))
        for offset, name in enumerate(names):
            file = folder / (name + '.mp4')
            file.write_bytes(b'x' * 2048)
            os.utime(file, (offset + 1, offset + 1))
    manager = MagicMock()
    manager.get_accounts.return_value = accounts
    daemon = DouyinAutoDeployDaemon(manager, report_dir=tmp_path / 'reports')
    monkeypatch.setattr(daemon._stop_event, 'wait', lambda timeout: None)
    monkeypatch.setattr(daemon, '_sleep_countdown', lambda timeout: daemon.stop())
    calls = []
    def factory(**kwargs):
        instance = MagicMock()
        def upload(video_path, **unused):
            name = video_path.stem
            calls.append(name)
            outcome = {'a': 'FAILED', 'b': 'ACCOUNT_LIMIT', 'd': 'SUCCESS'}[name]
            instance.last_outcome = outcome
            return outcome == 'SUCCESS', '今日投稿上限' if name == 'b' else outcome, None
        instance.upload_single_video.side_effect = upload
        return instance
    monkeypatch.setattr(module, 'DouyinUploader', factory)
    daemon.run()
    assert calls == ['a', 'b', 'd']
    data = json.loads(next((tmp_path / 'reports').glob('*.json')).read_text(encoding='utf-8'))
    assert data['success_count'] == 1 and data['failure_count'] == 2
    assert data['blocked_accounts'][0]['account_id'] == '0'
    assert data['failure_files'][1]['status'] == 'ACCOUNT_LIMIT'
    assert (tmp_path / '0/c.mp4').exists()


def test_selected_account_uses_its_own_media_and_profile_regardless_of_list_order(tmp_path, monkeypatch):
    from src.publisher.douyin import daemon as module
    accounts = []
    for name in ['old', 'new']:
        folder = tmp_path / name
        folder.mkdir()
        file = folder / (name + '.mp4')
        file.write_bytes(b'x' * 2048)
        os.utime(file, (1, 1))
        accounts.append(DouyinAccountConfig(name, name, target_dir=str(folder), profile_dir=str(folder / 'profile')))
    manager = MagicMock()
    manager.get_accounts.return_value = accounts
    daemon = DouyinAutoDeployDaemon(manager, report_dir=tmp_path / 'reports', account_ids=['new'])
    monkeypatch.setattr(daemon._stop_event, 'wait', lambda timeout: None)
    monkeypatch.setattr(daemon, '_sleep_countdown', lambda timeout: daemon.stop())
    called = []
    def factory(**kwargs):
        assert kwargs['account'].profile_dir == str(tmp_path / 'new/profile')
        instance = MagicMock(last_outcome='SUCCESS')
        def upload(video_path, **unused):
            called.append(video_path)
            return True, 'success', None
        instance.upload_single_video.side_effect = upload
        return instance
    monkeypatch.setattr(module, 'DouyinUploader', factory)
    daemon.run()
    assert called == [tmp_path / 'new/new.mp4']
    data = json.loads(next((tmp_path / 'reports').glob('*.json')).read_text(encoding='utf-8'))
    assert data['runtime']['publisher_build'] == module.PUBLISHER_BUILD
    assert data['runtime']['account_ids'] == ['new']
    assert data['files'][0]['account_id'] == 'new'


def test_oversize_file_skipped_once_without_delay_or_retry(tmp_path, monkeypatch):
    from src.publisher.douyin import daemon as module
    account = DouyinAccountConfig('a', 'a', target_dir=str(tmp_path))
    for index, name in enumerate(['long', 'short']):
        file = tmp_path / (name + '.mp4')
        file.write_bytes(b'x' * 2048)
        os.utime(file, (index + 1, index + 1))
    manager = MagicMock()
    manager.get_accounts.return_value = [account]
    daemon = DouyinAutoDeployDaemon(manager, report_dir=tmp_path / 'reports')
    waits, calls = [], []
    monkeypatch.setattr(daemon._stop_event, 'wait', lambda timeout: waits.append(timeout))
    monkeypatch.setattr(daemon, '_sleep_countdown', lambda timeout: daemon.stop())
    def factory(**kwargs):
        instance = MagicMock()
        def upload(video_path, **unused):
            calls.append(video_path.stem)
            instance.last_outcome = 'DURATION_LIMIT' if video_path.stem == 'long' else 'SUCCESS'
            return instance.last_outcome == 'SUCCESS', instance.last_outcome, None
        instance.upload_single_video.side_effect = upload
        return instance
    monkeypatch.setattr(module, 'DouyinUploader', factory)
    daemon.run()
    assert calls == ['long', 'short'] and waits == [30]
    data = json.loads(next((tmp_path / 'reports').glob('*.json')).read_text(encoding='utf-8'))
    assert data['failure_files'][0]['status'] == 'DURATION_LIMIT'
    assert len(data['failure_files'][0]['attempts']) == 1
