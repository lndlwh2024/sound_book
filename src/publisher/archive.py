"""One local archive for every publishing entry point, with content-safe reuse."""
import errno
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


ARCHIVE_DIRECTORY = 'publiced'


def _check_stop(stopped):
    if stopped():
        raise InterruptedError('归档已停止，保留未处理的工作文件')


def _snapshot(path):
    if path.is_symlink():
        raise ValueError('文件是符号链接，保留文件，请人工核对归档位置')
    info = path.stat()
    if not path.is_file():
        raise ValueError('归档同名位置不是普通文件，未覆盖或删除')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(path, stopped):
    before = _snapshot(path)
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            _check_stop(stopped)
            sha.update(block)
    if _snapshot(path) != before:
        raise ValueError('摘要校验期间文件发生变化，保留工作文件')
    return sha.hexdigest(), before


def _unchanged(path, snapshot):
    if _snapshot(path) != snapshot:
        raise ValueError('归档期间文件发生变化，未清理工作副本')


def _rename_exclusive(source, destination):
    """Windows rename refuses existing files; POSIX link has the same boundary."""
    if os.name == 'nt':
        source.rename(destination)
    else:
        os.link(source, destination)
        source.unlink()


def _move_exclusive(source, destination, digest, snapshot, stopped):
    _check_stop(stopped)
    _unchanged(source, snapshot)
    try:
        _rename_exclusive(source, destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    # A legacy caller can select a source on another volume. Copy to a private
    # temporary file, verify it, then publish its name without overwriting.
    descriptor, name = tempfile.mkstemp(prefix='.shusheng-archive-', suffix='.part', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output, source.open('rb') as incoming:
            for block in iter(lambda: incoming.read(1024*1024), b''):
                _check_stop(stopped)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        _unchanged(source, snapshot)
        if _digest(temporary, stopped)[0] != digest:
            raise ValueError('跨磁盘归档校验失败，保留工作文件')
        shutil.copystat(source, temporary)
        _check_stop(stopped)
        _rename_exclusive(temporary, destination)
        _unchanged(source, snapshot)
        source.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def _names(name):
    path = Path(name)
    yield path.name
    yield path.stem+'_重复'+path.suffix
    number = 2
    while True:
        yield f'{path.stem}_重复_{number}{path.suffix}'
        number += 1


def _archive_subtitle(source, destination, log, stopped):
    if not source.exists():
        return
    digest, snapshot = _digest(source, stopped)
    for name in _names(destination.name):
        _check_stop(stopped)
        candidate = destination.with_name(name)
        if candidate.exists():
            other, other_snapshot = _digest(candidate, stopped)
            if other != digest:
                continue
            _unchanged(source, snapshot)
            _unchanged(candidate, other_snapshot)
            _check_stop(stopped)
            source.unlink()
            log(f'[字幕归档复用] {candidate.name} 内容一致，保留已有字幕并清理工作副本')
            return
        try:
            _move_exclusive(source, candidate, digest, snapshot, stopped)
        except FileExistsError:
            # Recheck a concurrently created name; never overwrite it.
            return _archive_subtitle(source, destination, log, stopped)
        log(f'[字幕归档] {candidate.name}'+('；字幕同名但内容不同，已加重复后缀' if name != destination.name else ''))
        return


def archive_completed_file(source, signature, log=lambda message: None, cloud_root=None,
                           *, archive_root=None, stopped=lambda: False):
    """Archive completed media; names never determine publication success.

    cloud_root remains accepted for existing callers. Archive naming is no
    longer derived from cloud provenance or any account's duplicate flag.
    """
    from .douyin.daemon import is_file_fully_written
    source = Path(source)
    _check_stop(stopped)
    if not is_file_fully_written(source) or (source.stat().st_size, source.stat().st_mtime_ns) != signature:
        raise ValueError('目标已完成，但原文件已变化或被占用；保留文件')
    digest, snapshot = _digest(source, stopped)
    directory = (Path(archive_root) if archive_root is not None else source.parent)/ARCHIVE_DIRECTORY
    if directory.is_symlink() or directory.is_junction():
        raise ValueError('归档目录是链接，保留文件，请人工核对归档位置')
    directory.mkdir(exist_ok=True)
    subtitle = source.with_suffix('.srt')
    for name in _names(source.name):
        _check_stop(stopped)
        destination = directory/name
        caption = destination.with_suffix('.srt')
        if destination.exists():
            other, other_snapshot = _digest(destination, stopped)
            if other != digest:
                continue
            # Preserve caption variants before removing an identical video.
            _archive_subtitle(subtitle, caption, log, stopped)
            _unchanged(source, snapshot)
            _unchanged(destination, other_snapshot)
            _check_stop(stopped)
            source.unlink()
            log(f'[归档复用] {source.name}：{destination.name} 的SHA-256一致，跳过重复保存并清理工作副本')
            return destination
        if caption.exists():
            # An orphan subtitle must not become paired with unrelated media.
            if not subtitle.exists() or _digest(caption, stopped)[0] != _digest(subtitle, stopped)[0]:
                continue
        try:
            _move_exclusive(source, destination, digest, snapshot, stopped)
        except FileExistsError:
            # Re-enter selection so newly created identical files can be reused.
            return archive_completed_file(source, signature, log, cloud_root,
                                          archive_root=archive_root, stopped=stopped)
        if name != source.name:
            log(f'[归档同名保留] {source.name}：已有同名文件或配套字幕内容不同，另存为 {destination.name}')
        try:
            _archive_subtitle(subtitle, caption, log, stopped)
        except (OSError, ValueError) as error:
            log(f'[字幕归档未完成] 媒体已归档到 {destination}，字幕仍保留：{error}')
        return destination
