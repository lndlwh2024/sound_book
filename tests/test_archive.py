"""Archive asset integrity and account-scoped backfill; no real platforms."""
import errno
import hashlib
import json

import pytest

from src.publisher import archive
from src.publisher.douyin.models import DouyinAccountConfig
from src.publisher.douyin.uploader import DouyinUploader
from src.publisher.multi_deploy import MultiTargetLedger
from src.publisher.upload_quota import DailyUploadQuota
from tests.test_scan_cleanup_quota import app, media, targets, task


def finalize(source, **kwargs):
    info = source.stat()
    return archive.archive_completed_file(source, (info.st_size, info.st_mtime_ns), **kwargs)


@pytest.mark.parametrize('cloud', [False, True])
def test_all_sources_use_publiced(tmp_path, cloud):
    source = media(tmp_path/'work', '知识.mp4')
    content = source.read_bytes()
    result = finalize(source, cloud_root=tmp_path/'state' if cloud else None)
    assert result == source.parent/'publiced'/source.name
    assert result.read_bytes() == content and not source.exists()
    assert not (source.parent/'published').exists()


@pytest.mark.parametrize('caption', [False, True])
def test_identical_existing_asset_reused_without_changing_archive(tmp_path, caption):
    source = media(tmp_path/'work')
    existing = media(source.parent/'publiced', source.name, source.read_bytes())
    if caption:
        source.with_suffix('.srt').write_bytes(b'caption')
        existing.with_suffix('.srt').write_bytes(b'caption')
    before = existing.stat()
    logs = []
    assert finalize(source, log=logs.append) == existing
    assert not source.exists() and not source.with_suffix('.srt').exists()
    assert existing.stat().st_mtime_ns == before.st_mtime_ns
    assert existing.read_bytes() == b'video'*500
    assert not list(existing.parent.glob('*重复*'))
    assert any('归档复用' in line for line in logs)


def test_distinct_contents_number_collisions_and_reuse_matching_variant(tmp_path):
    source = media(tmp_path/'work', '知识.mp4', b'new'*1000)
    original = media(source.parent/'publiced', source.name, b'old'*1000)
    first = media(original.parent, '知识_重复.mp4', b'another'*500)
    source.with_suffix('.srt').write_text('new subtitle')
    result = finalize(source)
    assert result.name == '知识_重复_2.mp4'
    assert result.read_bytes() == b'new'*1000
    assert result.with_suffix('.srt').read_text() == 'new subtitle'
    assert original.read_bytes() == b'old'*1000 and first.read_bytes() == b'another'*500
    again = media(source.parent, source.name, result.read_bytes())
    assert finalize(again) == result and not again.exists()
    assert len(list(original.parent.glob('*.mp4'))) == 3


def test_same_video_preserves_different_subtitle_as_variant(tmp_path):
    source = media(tmp_path/'work')
    existing = media(source.parent/'publiced', source.name, source.read_bytes())
    source.with_suffix('.srt').write_text('new subtitle')
    existing.with_suffix('.srt').write_text('old subtitle')
    assert finalize(source) == existing
    assert existing.with_suffix('.srt').read_text() == 'old subtitle'
    assert (existing.parent/'clip_重复.srt').read_text() == 'new subtitle'
    assert len(list(existing.parent.glob('*.mp4'))) == 1


@pytest.mark.parametrize('matching', [False, True])
def test_orphan_subtitle_not_paired_with_unrelated_media(tmp_path, matching):
    source = media(tmp_path/'work')
    directory = source.parent/'publiced'; directory.mkdir()
    (directory/'clip.srt').write_text('old subtitle')
    source.with_suffix('.srt').write_text('old subtitle' if matching else 'new subtitle')
    result = finalize(source)
    assert result.name == ('clip.mp4' if matching else 'clip_重复.mp4')
    assert (directory/'clip.srt').read_text() == 'old subtitle'
    assert result.with_suffix('.srt').read_text() == ('old subtitle' if matching else 'new subtitle')


def test_cancel_during_hash_keeps_work_and_existing_archive(tmp_path):
    source = media(tmp_path/'work', content=b'x'*(3*1024*1024))
    existing = media(source.parent/'publiced', source.name, source.read_bytes())
    calls = []
    def stopped():
        calls.append(True)
        return len(calls) == 4
    with pytest.raises(InterruptedError):
        finalize(source, stopped=stopped)
    assert source.exists() and existing.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('mutate', ['source', 'destination'])
def test_changed_asset_before_dedup_cleanup_is_retained(tmp_path, monkeypatch, mutate):
    source = media(tmp_path/'work')
    existing = media(source.parent/'publiced', source.name, source.read_bytes())
    original = archive._archive_subtitle
    def changed(*args):
        (source if mutate == 'source' else existing).write_bytes(b'changed'*500)
        return original(*args)
    monkeypatch.setattr(archive, '_archive_subtitle', changed)
    with pytest.raises(ValueError, match='文件发生变化'):
        finalize(source)
    assert source.exists() and existing.exists()


def test_archive_failure_preserves_work_and_does_not_overwrite(tmp_path, monkeypatch):
    source = media(tmp_path/'work')
    monkeypatch.setattr(archive, '_rename_exclusive', lambda *args: (_ for _ in ()).throw(PermissionError('locked')))
    with pytest.raises(PermissionError):
        finalize(source)
    assert source.exists() and not list((source.parent/'publiced').iterdir())


def test_late_same_name_creation_is_rechecked_instead_of_overwritten(tmp_path, monkeypatch):
    source = media(tmp_path/'work')
    original = archive._rename_exclusive
    def race(incoming, destination):
        destination.write_bytes(incoming.read_bytes())
        monkeypatch.setattr(archive, '_rename_exclusive', original)
        raise FileExistsError('concurrent archive')
    monkeypatch.setattr(archive, '_rename_exclusive', race)
    result = finalize(source)
    assert result.name == source.name and not source.exists()
    assert len(list(result.parent.iterdir())) == 1


@pytest.mark.parametrize('fail_copy', [False, True])
def test_cross_volume_archive_is_verified_and_preserves_source_on_failure(tmp_path, monkeypatch, fail_copy):
    source = media(tmp_path/'work')
    original = archive._rename_exclusive
    def cross_volume(incoming, destination):
        if incoming == source:
            raise OSError(errno.EXDEV, 'different volume')
        if fail_copy:
            raise PermissionError('destination locked')
        return original(incoming, destination)
    monkeypatch.setattr(archive, '_rename_exclusive', cross_volume)
    other = tmp_path/'other'; other.mkdir()
    if fail_copy:
        with pytest.raises(PermissionError):
            finalize(source, archive_root=other)
        assert source.exists() and not list((other/'publiced').iterdir())
    else:
        result = finalize(source, archive_root=other)
        assert result == other/'publiced'/source.name
        assert not source.exists() and result.read_bytes() == b'video'*500
    assert not list(other.rglob('*.part'))


def test_stale_job_signature_cannot_archive_changed_work(tmp_path):
    source = media(tmp_path/'work')
    with pytest.raises(ValueError, match='变化或被占用'):
        archive.archive_completed_file(source, (1, 1))
    assert source.exists() and not (source.parent/'publiced').exists()


def test_locked_existing_archive_cannot_trigger_duplicate_deletion(tmp_path):
    import os
    if os.name != 'nt':
        pytest.skip('Windows exclusive file handles')
    import ctypes
    from ctypes import wintypes
    source = media(tmp_path/'work')
    existing = media(source.parent/'publiced', source.name, source.read_bytes())
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(existing), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(PermissionError):
            finalize(source)
        assert source.exists()
    finally:
        kernel.CloseHandle(handle)
    assert finalize(source) == existing and not source.exists()


@pytest.mark.parametrize('existing_same', [False, True])
def test_historical_wechat_backfill_only_publishes_selected_account_then_reuses_archive(tmp_path, existing_same):
    source = media(tmp_path/'work')
    content = source.read_bytes()
    existing = media(source.parent/'publiced', source.name, content if existing_same else b'other'*500)
    group = targets(source.parent)
    chosen = next(t for t in group if t.platform == 'wechat_channels')
    chosen.account.auto_archive = True
    root = tmp_path/'state'; ledger = MultiTargetLedger(root)
    digest = hashlib.sha256(content).hexdigest()
    for target in group:
        if target.key != chosen.key:
            ledger.set_result(digest, target.key, 'SUCCESS', 'historical success')
    calls = []
    class Uploader:
        def upload_single_video(self, path):
            self.before_upload()
            calls.append(path.name)
            return True, 'confirmed by fake platform', None
    factory = lambda target, log: Uploader()
    first = task(root, [chosen], factory, group)
    first.run_batch(first.plan_batch())
    assert calls == [source.name]  # Archive presence is NOT publication proof.
    expected = existing if existing_same else existing.with_stem(existing.stem+'_重复')
    assert expected.read_bytes() == content and not source.exists()
    assert first.report['files'][0]['archived_path'] == str(expected)
    for target in group:
        result = MultiTargetLedger(root).result(digest, target.key)
        assert result['status'] == 'SUCCESS'
        if target.key != chosen.key:
            assert result['message'] == 'historical success'
    assert DailyUploadQuota(root).snapshot(chosen.key)['used'] == 1
    # Simulate another copy of the historical file placed in the work directory.
    media(source.parent, source.name, content)
    restarted = task(root, [chosen], factory, group)
    restarted.run_batch(restarted.plan_batch())
    assert calls == [source.name] and not source.exists()
    assert DailyUploadQuota(root).snapshot(chosen.key)['used'] == 1
    assert len(list(existing.parent.glob('*.mp4'))) == (1 if existing_same else 2)


@pytest.mark.parametrize('auto_archive', [False, True])
def test_legacy_douyin_finalizer_preserves_daily_success_record(tmp_path, auto_archive):
    source = media(tmp_path/'work')
    expected_hash = hashlib.md5(source.read_bytes()).hexdigest()
    account = DouyinAccountConfig('a', 'test', target_dir=str(source.parent),
                                  profile_dir=str(tmp_path/'profile'), auto_archive=auto_archive)
    record = DouyinUploader(account)._finalize_publish_archive_and_ledger(source, '合集', 1)
    assert record.file_hash == expected_hash and record.status == 'SUCCESS'
    assert source.exists() == (not auto_archive)
    assert record.archived_path == (str(source.parent/'publiced'/source.name) if auto_archive else None)
    records = json.loads(next((source.parent/'reports').glob('publish_ledger_*.json')).read_text(encoding='utf-8'))
    assert records[-1] == record.to_dict()
    assert not (source.parent/'published').exists()
